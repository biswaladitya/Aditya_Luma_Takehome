"""Brief versions: a brief change makes older images outdated; approvals survive it; Drive keeps every one."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from litestar.testing import TestClient

from backend import db
from backend.app import app
from backend.db import SCHEMA_VERSION, database
from backend.repositories import deliveries, products, reviews
from backend.services.review import OUTDATED, handle_block_action
from tests.test_catalog_imports import csv_bytes, product
from tests.test_delivery import TOKEN, FakeDrive
from tests.test_review import ELLIE, FakeSlack, click

# The v6 schema exactly as v6 created it (sqlite_master, in creation order), so the upgrade is tested
# against what an existing runtime database holds rather than against today's constants.
V6_SCHEMA = """
CREATE TABLE products (
        sku TEXT PRIMARY KEY NOT NULL CHECK (length(trim(sku)) > 0),
        product_name TEXT NOT NULL,
        category TEXT NOT NULL,
        color TEXT NOT NULL,
        material TEXT NOT NULL,
        price TEXT NOT NULL,
        photo TEXT NOT NULL,
        shot_idea TEXT NOT NULL,
        notes TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
CREATE TABLE generated_images (
        id TEXT PRIMARY KEY NOT NULL,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE RESTRICT,
        generated_from TEXT NOT NULL,
        storage_key TEXT NOT NULL CHECK (length(storage_key) > 0),
        luma_generation_id TEXT,
        generated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
        status TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('queued', 'processing', 'done', 'failed')),
        error TEXT
    );
CREATE INDEX generated_images_product_sku ON generated_images(product_sku);
CREATE TRIGGER generated_images_immutable_snapshot
        BEFORE UPDATE OF generated_from ON generated_images
        WHEN NEW.generated_from IS NOT OLD.generated_from
        BEGIN
            SELECT RAISE(ABORT, 'generated_from snapshots are immutable');
        END;
CREATE TABLE pending_imports (
        id TEXT PRIMARY KEY NOT NULL,
        filename TEXT NOT NULL,
        rows TEXT NOT NULL,
        expected_versions TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'applied')),
        created_at TEXT NOT NULL,
        applied_at TEXT,
        result TEXT,
        CHECK ((status = 'pending' AND applied_at IS NULL AND result IS NULL)
            OR (status = 'applied' AND applied_at IS NOT NULL AND result IS NOT NULL))
    );
CREATE TABLE image_reviews (
        image_id TEXT PRIMARY KEY NOT NULL REFERENCES generated_images(id) ON DELETE RESTRICT,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE RESTRICT,
        state TEXT NOT NULL DEFAULT 'pending_send'
            CHECK (state IN ('pending_send', 'awaiting_approval', 'approved')),
        slack_channel TEXT,
        slack_thread_ts TEXT,
        slack_message_ts TEXT,
        slack_file_id TEXT,
        send_error TEXT,
        approved_by TEXT,
        approved_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, superseded_at TEXT CHECK (superseded_at IS NULL OR state = 'approved'),
        CHECK ((state = 'pending_send' AND slack_message_ts IS NULL)
            OR (state = 'awaiting_approval' AND slack_message_ts IS NOT NULL)
            OR (state = 'approved' AND slack_message_ts IS NOT NULL
                AND approved_by IS NOT NULL AND approved_at IS NOT NULL))
    );
CREATE INDEX image_reviews_product_sku ON image_reviews(product_sku);
CREATE TRIGGER image_reviews_forward_only
        BEFORE UPDATE OF state ON image_reviews
        WHEN NEW.state IS NOT OLD.state
            AND NOT (OLD.state = 'pending_send' AND NEW.state = 'awaiting_approval')
            AND NOT (OLD.state = 'awaiting_approval' AND NEW.state = 'approved')
        BEGIN
            SELECT RAISE(ABORT, 'review state can only move forward');
        END;
CREATE TABLE drive_deliveries (
        image_id TEXT PRIMARY KEY NOT NULL REFERENCES generated_images(id) ON DELETE RESTRICT,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE RESTRICT,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'delivered')),
        filename TEXT NOT NULL CHECK (length(filename) > 0),
        drive_file_id TEXT,
        drive_url TEXT,
        delivered_at TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (state = 'pending' OR (drive_file_id IS NOT NULL
            AND drive_url IS NOT NULL AND delivered_at IS NOT NULL))
    );
CREATE INDEX drive_deliveries_product_sku ON drive_deliveries(product_sku);
CREATE TRIGGER drive_deliveries_forward_only
        BEFORE UPDATE OF state ON drive_deliveries
        WHEN NEW.state IS NOT OLD.state
            AND NOT (OLD.state = 'pending' AND NEW.state = 'delivered')
        BEGIN
            SELECT RAISE(ABORT, 'delivery state can only move forward');
        END;
CREATE UNIQUE INDEX image_reviews_one_approved_per_sku ON image_reviews(product_sku) WHERE state = 'approved' AND superseded_at IS NULL;
CREATE TRIGGER image_reviews_supersede_once
        BEFORE UPDATE OF superseded_at ON image_reviews
        WHEN NEW.superseded_at IS NOT OLD.superseded_at
            AND NOT (OLD.superseded_at IS NULL AND NEW.superseded_at IS NOT NULL AND OLD.state = 'approved')
        BEGIN
            SELECT RAISE(ABORT, 'superseded_at can only be set once, on an approved review');
        END;
PRAGMA user_version = 6;
"""

V4_DELIVERIES = (
    """CREATE TABLE drive_deliveries (
        image_id TEXT PRIMARY KEY NOT NULL REFERENCES generated_images(id) ON DELETE RESTRICT,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE RESTRICT,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'delivered')),
        filename TEXT NOT NULL CHECK (length(filename) > 0),
        drive_folder_id TEXT, drive_file_id TEXT, drive_url TEXT, delivered_at TEXT, error TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK (state = 'pending' OR (drive_folder_id IS NOT NULL AND drive_file_id IS NOT NULL
            AND drive_url IS NOT NULL AND delivered_at IS NOT NULL)))""",
    "CREATE INDEX drive_deliveries_product_sku ON drive_deliveries(product_sku)",
    """CREATE TRIGGER drive_deliveries_forward_only BEFORE UPDATE OF state ON drive_deliveries
        WHEN NEW.state IS NOT OLD.state AND NOT (OLD.state = 'pending' AND NEW.state = 'delivered')
        BEGIN SELECT RAISE(ABORT, 'delivery state can only move forward'); END""",
)


def historical_schema(version: int) -> tuple:
    """Empty-database DDL for an older version, from the frozen migration constants."""
    core = db._SCHEMA[:-(len(db._REVIEW_SCHEMA) + len(db._DELIVERY_SCHEMA))]
    return {
        2: core, 3: core + db._REVIEW_SCHEMA, 4: core + db._REVIEW_SCHEMA + V4_DELIVERIES,
        5: db._SCHEMA, 6: db._SCHEMA + db._SUPERSEDE_SCHEMA,
    }[version]


def schema(path):
    with sqlite3.connect(path) as connection:
        return sorted(connection.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


def brief(sku, idea, **changes):
    return json.dumps(product(sku, shot_idea=idea, **changes))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "catalog.sqlite3"
        env = patch.dict(os.environ, {"DATABASE_PATH": str(self.path), "DATA_DIR": ""})
        env.start()
        self.addCleanup(env.stop)

    def fresh_schema(self):
        fresh = self.path.parent / "fresh.sqlite3"
        with patch.dict(os.environ, {"DATABASE_PATH": str(fresh)}):
            with database():
                pass
        return schema(fresh)

    def test_upgrade_from_v6_keeps_every_row_and_numbers_briefs(self):
        with sqlite3.connect(self.path) as connection:
            connection.executescript(V6_SCHEMA)
            columns = ", ".join(products.PRODUCT_ATTRIBUTES)

            def add_product(sku, idea, **changes):
                row = product(sku, shot_idea=idea, **changes)
                connection.execute(f"INSERT INTO products ({columns}, version, created_at, updated_at) "
                                   f"VALUES ({', '.join('?' * 9)}, 3, 't', 't')", [row[k] for k in products.PRODUCT_ATTRIBUTES])

            def add_image(image_id, sku, snapshot, at, version=1, status="done"):
                connection.execute("INSERT INTO generated_images (id, product_sku, generated_from, storage_key, generated_at, "
                                   "version, status, luma_generation_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'gen')",
                                   (image_id, sku, snapshot, f"{image_id}.png", at, version, status))

            def add_review(image_id, sku, state, approved_at=None, superseded_at=None, send_error=None):
                message = None if state == "pending_send" else f"ts-{image_id}"
                approver = "UELLIE" if state == "approved" else None
                connection.execute("INSERT INTO image_reviews (image_id, product_sku, state, slack_channel, slack_thread_ts, "
                                   "slack_message_ts, send_error, approved_by, approved_at, superseded_at, created_at, updated_at) "
                                   "VALUES (?, ?, ?, 'C1', '100.1', ?, ?, ?, ?, ?, 't', 't')",
                                   (image_id, sku, state, message, send_error, approver, approved_at, superseded_at))

            # P-1: an approval superseded by a brief change; the new brief's candidates wait or failed to send.
            add_product("P-1", "B")
            add_image("p1-a1", "P-1", brief("P-1", "A"), "01", 1)
            add_image("p1-a2", "P-1", brief("P-1", "A"), "02", 2)
            add_review("p1-a1", "P-1", "approved", "03", "04")
            add_image("p1-b1", "P-1", brief("P-1", "B"), "05", 3)
            add_image("p1-b2", "P-1", brief("P-1", "B"), "06", 4)
            add_review("p1-b1", "P-1", "awaiting_approval")
            add_review("p1-b2", "P-1", "pending_send", send_error="slack is down")
            connection.execute("INSERT INTO drive_deliveries (image_id, product_sku, filename, error, created_at, updated_at) "
                               "VALUES ('p1-a1', 'P-1', 'P-1_styled_01.png', 'quota', 't', 't')")
            # P-2: in Drive under the old name, then the brief changed (v6 kept that approval live).
            add_product("P-2", "C")
            add_image("p2-a1", "P-2", brief("P-2", "A"), "01")
            add_review("p2-a1", "P-2", "approved", "02")
            connection.execute("INSERT INTO drive_deliveries (image_id, product_sku, state, filename, drive_file_id, drive_url, "
                               "delivered_at, created_at, updated_at) VALUES ('p2-a1', 'P-2', 'delivered', 'P-2_styled_01.png', "
                               "'F1', 'https://drive.google.com/file/d/F1/view', 't', 't', 't')")
            # P-3: brief A, then B, then back to A without generating again.
            add_product("P-3", "A")
            add_image("p3-a1", "P-3", brief("P-3", "A"), "01", 1)
            add_review("p3-a1", "P-3", "awaiting_approval")
            add_image("p3-b1", "P-3", brief("P-3", "B"), "02", 2)
            add_review("p3-b1", "P-3", "pending_send")
            # P-4: a notes-only change superseded an approval, then a new image of the same brief was approved.
            add_product("P-4", "A", notes="new notes")
            add_image("p4-a1", "P-4", brief("P-4", "A"), "01", 1)
            add_review("p4-a1", "P-4", "approved", "02", "03")
            add_image("p4-a2", "P-4", brief("P-4", "A"), "04", 2)
            add_review("p4-a2", "P-4", "approved", "05")
            add_image("p4-a3", "P-4", brief("P-4", "A"), "04", 3, status="failed")
            # P-5: nothing generated yet.
            add_product("P-5", "A")
            connection.execute("INSERT INTO pending_imports (id, filename, rows, expected_versions, status, created_at) "
                               "VALUES ('imp-1', 'c.csv', '[]', '{}', 'pending', 't')")

        with database() as connection:
            self.assertEqual((connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION), (7, 7))
            catalog = {row["sku"]: row for row in products.get_products(connection)}
            versions = {sku: (row["brief_version"], {i["id"]: i["brief_version"] for i in row["images"]})
                        for sku, row in catalog.items()}
            self.assertEqual(versions, {
                "P-1": (2, {"p1-a1": 1, "p1-a2": 1, "p1-b1": 2, "p1-b2": 2}),
                "P-2": (2, {"p2-a1": 1}),
                "P-3": (2, {"p3-a1": 2, "p3-b1": 1}),  # The current brief's group is numbered last.
                "P-4": (2, {"p4-a1": 1, "p4-a2": 2, "p4-a3": 2}),  # An image made after an approval starts a group.
                "P-5": (1, {}),
            })
            rows = {r["image_id"]: dict(r) for r in connection.execute("SELECT * FROM image_reviews")}
            self.assertEqual({k: (r["state"], r["brief_version"]) for k, r in rows.items()}, {
                "p1-a1": ("approved", 1), "p1-b1": ("awaiting_approval", 2), "p2-a1": ("approved", 1),
                "p3-a1": ("awaiting_approval", 2), "p4-a1": ("approved", 1), "p4-a2": ("approved", 2),
            })
            self.assertEqual((rows["p1-a1"]["approved_by"], rows["p1-a1"]["slack_message_ts"]), ("UELLIE", "ts-p1-a1"))
            self.assertNotIn("superseded_at", rows["p1-a1"])
            self.assertNotIn("send_error", rows["p1-a1"])
            post_errors = {i["id"]: i["post_error"] for row in catalog.values() for i in row["images"] if i["post_error"]}
            self.assertEqual(post_errors, {"p1-b2": "slack is down", "p3-b1": "Not posted"})
            self.assertEqual(deliveries.get_delivery(connection, "p1-a1")["error"], "quota")
            # Unsaved v6 rows take the versioned name, so they can't overwrite another image's _styled_01 file.
            self.assertEqual(deliveries.get_delivery(connection, "p1-a1")["filename"], "P-1_styled_v1.png")
            self.assertEqual(deliveries.get_delivery(connection, "p2-a1")["filename"], "P-2_styled_01.png")
            self.assertEqual(products.get_pending_import(connection, "imp-1")["status"], "pending")
            with self.assertRaises(sqlite3.IntegrityError):  # One approval per product and brief.
                connection.execute("INSERT INTO image_reviews (image_id, product_sku, brief_version, state, slack_message_ts, "
                                   "approved_by, approved_at, created_at, updated_at) "
                                   "VALUES ('p1-a2', 'P-1', 1, 'approved', 'x', 'U', 't', 't', 't')")
            with self.assertRaises(sqlite3.IntegrityError):  # Forward-only trigger recreated.
                connection.execute("UPDATE image_reviews SET state = 'awaiting_approval' WHERE image_id = 'p1-a1'")
            self.assertEqual(reviews.approve(connection, "p1-b1", ELLIE), "approved")  # p1-a1 is another brief's.
            self.assertEqual(reviews.approve(connection, "p3-a1", ELLIE), "approved")
        self.assertEqual(schema(self.path), self.fresh_schema())  # Upgraded and fresh installs match.

    def test_every_older_version_upgrades_to_the_fresh_schema(self):
        for version in (2, 3, 4, 5, 6):
            with self.subTest(version=version):
                self.path.unlink(missing_ok=True)
                with sqlite3.connect(self.path) as connection:
                    for statement in historical_schema(version):
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {version}")
                with database() as connection:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(schema(self.path), self.fresh_schema())

    def test_upgrade_from_v4_drops_the_folder_column_and_keeps_rows(self):
        with sqlite3.connect(self.path) as connection:
            for statement in historical_schema(4):
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 4")
            columns = ", ".join(products.PRODUCT_ATTRIBUTES)
            row = product("VASE-042")
            connection.execute(f"INSERT INTO products ({columns}, version, created_at, updated_at) "
                               f"VALUES ({', '.join('?' * 9)}, 1, 't', 't')", [row[k] for k in products.PRODUCT_ATTRIBUTES])
            connection.execute("INSERT INTO generated_images (id, product_sku, generated_from, storage_key, generated_at) "
                               "VALUES ('img', 'VASE-042', ?, 'a.png', 't')", (json.dumps(row),))
            connection.execute("INSERT INTO drive_deliveries (image_id, product_sku, filename, error, created_at, updated_at) "
                               "VALUES ('img', 'VASE-042', 'VASE-042_styled_01.png', 'old error', 't', 't')")
        with database() as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(drive_deliveries)")]
            self.assertNotIn("drive_folder_id", columns)
            self.assertEqual(deliveries.get_delivery(connection, "img")["error"], "old error")
            deliveries.mark_delivered(connection, "img", "F1", "https://drive.google.com/file/d/F1/view")
            with self.assertRaises(sqlite3.IntegrityError):  # The forward-only trigger was recreated.
                connection.execute("UPDATE drive_deliveries SET state = 'pending' WHERE image_id = 'img'")


class BriefVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {
            "DATABASE_PATH": str(Path(temporary.name) / "catalog.sqlite3"), "DATA_DIR": "",
            "SLACK_APP_TOKEN": "", "SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C1",
            "SLACK_APPROVER_USER_ID": ELLIE, "GOOGLE_CLIENT_ID": "cid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.drive = FakeDrive()
        self.slack = FakeSlack()
        for patcher in (patch("backend.drive.client", side_effect=self.drive.client),
                        patch("backend.slack.client", return_value=self.slack)):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("backend.luma.generate_image", side_effect=lambda prompt, url: ("gen", prompt.encode(), "image/png"))
        self.luma = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.enterContext(TestClient(app, raise_server_exceptions=False))
        self.confirm(self.preview([product("VASE-042"), product("LAMP-7")]))

    def preview(self, rows):
        response = self.client.post("/api/catalog/preview", files={"data": ("c.csv", csv_bytes(rows), "text/csv")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def confirm(self, preview, status=200):
        response = self.client.post(f"/api/catalog/imports/{preview['preview_id']}/confirm")
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def change(self, sku="VASE-042", status=200, **fields):
        rows = {"VASE-042": product("VASE-042"), "LAMP-7": product("LAMP-7")}
        rows[sku] = product(sku, **{"shot_idea": "Evening table", **fields})
        preview = self.preview(list(rows.values()))
        case = next(r["brief_case"] for r in preview["rows"] if r["sku"] == sku)
        self.confirm(preview, status)
        return case

    def row(self, sku="VASE-042"):
        return next(r for r in self.client.get("/api/catalog").json()["rows"] if r["sku"] == sku)

    def until(self, condition):
        for _ in range(50):
            if condition():
                return
            time.sleep(0.1)
        self.fail("condition not reached")

    def generate(self, sku="VASE-042"):
        response = self.client.post("/api/generations", json={"skus": [sku]}).json()
        self.assertEqual(response["queued"], [sku], response)
        self.until(lambda: not self.row(sku)["generating"])
        return [i["id"] for i in self.row(sku)["images"][-2:]]

    def approve(self, sku="VASE-042"):
        image_id = self.generate(sku)[0]
        self.assertEqual(handle_block_action(click(image_id), self.slack), "approved")
        return image_id

    def deliver(self, sku="VASE-042"):
        response = self.client.post("/api/deliveries", json={"skus": [sku], "access_token": TOKEN})
        self.assertEqual(response.status_code, 202, response.text)
        self.until(lambda: not self.row(sku)["delivering"])
        return response.json()

    def test_brief_change_before_any_generation(self):
        with patch("backend.luma.generate_image", side_effect=RuntimeError("boom")):
            self.client.post("/api/generations", json={"skus": ["VASE-042"]})
            self.until(lambda: not self.row()["generating"])
        self.assertEqual(self.change(), "not_generated")  # Only failed attempts so far.
        row = self.row()
        self.assertEqual((row["brief_version"], row["generation_status"], row["can_generate"]), (2, "never_generated", True))

    def test_brief_change_while_waiting_on_ellie_outdates_the_candidates(self):
        waiting = self.generate()
        self.assertEqual(self.change(), "with_ellie")
        self.until(lambda: len(self.slack.updates) == 2)
        self.assertEqual({u["text"] for u in self.slack.updates}, {OUTDATED})
        self.assertTrue(all(u["blocks"][-1]["type"] == "context" for u in self.slack.updates))  # No Approve button.
        row = self.row()
        self.assertTrue(all(i["outdated"] for i in row["images"]))
        self.assertEqual((row["review_status"], row["generation_status"], row["can_generate"]),
                         (None, "changed_since_generation", True))
        self.assertEqual(handle_block_action(click(waiting[0]), self.slack), "outdated")
        self.assertIn("older brief", self.slack.ephemerals[-1][1])
        self.assertEqual(self.row()["images"][0]["review"]["state"], "awaiting_approval")
        self.assertEqual(self.luma.call_count, 2)  # Regenerating needs an explicit click.
        fresh = self.generate()
        self.assertEqual(handle_block_action(click(fresh[1]), self.slack), "approved")

    def test_approval_not_in_drive_is_kept_and_both_are_saved(self):
        first = self.approve()
        self.assertEqual(self.change(), "approved_not_in_drive")
        row = self.row()
        self.assertEqual((row["approved_image_id"], row["can_deliver"], row["brief_changed"], row["can_generate"]),
                         (first, True, True, True))
        self.assertIsNone(row["review_status"])  # Nothing approved for the new brief yet.
        second = self.approve()  # The optional regenerate, then Ellie approves the new brief.
        row = self.row()
        self.assertEqual((row["approved_image_id"], row["approved_image_ids"]), (second, [first, second]))
        self.assertEqual(self.deliver()["queued"], ["VASE-042"])
        self.assertEqual(sorted(f["name"] for f in self.drive.files.values()),
                         ["VASE-042_styled_v1.png", "VASE-042_styled_v3.png"])
        row = self.row()
        self.assertEqual((row["delivery_status"], row["can_deliver"]), ("delivered", False))

    def test_in_drive_product_is_kept_with_an_optional_regenerate(self):
        self.approve()
        self.deliver()
        self.assertEqual(self.change(), "in_drive")
        row = self.row()
        self.assertEqual((row["delivery_status"], row["brief_changed"], row["can_generate"]), ("delivered", True, True))
        self.assertEqual(self.luma.call_count, 2)  # Never regenerated by an import.

    def test_new_candidates_after_a_save_to_drive_are_with_ellie(self):
        self.approve()
        self.deliver()
        self.change()
        waiting = self.generate()  # The optional regenerate; its candidates wait on Ellie.
        self.assertEqual(self.change(shot_idea="Third idea"), "with_ellie")
        with database() as connection:
            messages = {reviews.get_review(connection, image_id)["slack_message_ts"] for image_id in waiting}
        self.until(lambda: messages <= {u["ts"] for u in self.slack.updates if u["text"] == OUTDATED})
        time.sleep(0.2)
        # The first brief's unselected candidate keeps its "Not selected" note.
        self.assertEqual(sorted(u["ts"] for u in self.slack.updates if u["text"] == OUTDATED), sorted(messages))

    def test_info_only_change_keeps_state_and_images(self):
        approved = self.approve()
        self.assertEqual(self.change(shot_idea=product()["shot_idea"], price="$1", notes="n", category="c"), "info_only")
        row = self.row()
        self.assertEqual((row["brief_version"], row["version"], row["approved_image_id"], row["review_status"]),
                         (1, 2, approved, "approved"))
        self.assertFalse(row["can_generate"] or row["brief_changed"] or any(i["outdated"] for i in row["images"]))
        self.assertEqual(self.slack.updates[-1]["text"], "Not selected — another image was approved")  # Nothing marked outdated.

    def test_accept_waits_for_generation_and_drive_saves(self):
        with database() as connection:
            queued = products.save_generated_image(connection, "VASE-042", product("VASE-042"), "q.png", status="queued")
        detail = self.confirm(self.preview([product("VASE-042", shot_idea="x")]), 409)["detail"]
        self.assertIn("VASE-042", detail)
        self.assertIn("Wait for it to finish", detail)
        self.assertEqual(self.row()["brief_version"], 1)  # Nothing was applied.
        self.assertEqual(self.change("LAMP-7"), "not_generated")  # Other products are not held up.
        self.assertEqual(self.change(shot_idea=product()["shot_idea"], notes="x"), "info_only")  # Info-only is allowed.
        with database() as connection:
            products.update_generated_image(connection, queued["id"], status="failed", error="stopped")
        self.change()
        approved = self.approve()
        with database() as connection:
            deliveries.queue(connection, "VASE-042", approved, "VASE-042_styled_v5.png")
        self.change(shot_idea="Third idea", status=409)
        with database() as connection:
            deliveries.mark_error(connection, [approved], "quota")
        self.change(shot_idea="Third idea")

    def test_generation_is_refused_while_busy(self):
        approved = self.approve("LAMP-7")
        self.change("LAMP-7")
        with database() as connection:
            products.save_generated_image(connection, "VASE-042", product("VASE-042"), "q.png", status="queued")
            deliveries.queue(connection, "LAMP-7", approved, "LAMP-7_styled_v1.png")
        result = self.client.post("/api/generations", json={"skus": ["VASE-042", "LAMP-7"]}).json()
        self.assertEqual({s["sku"]: s["reason"] for s in result["skipped"]},
                         {"VASE-042": "Generation already in progress.", "LAMP-7": "Saving to Drive is in progress."})

    def test_attempt_cap_counts_per_brief(self):
        with database() as connection:
            for version in range(1, 7):  # Six images of brief 1 would have reached a per-product cap.
                products.save_generated_image(connection, "VASE-042", product("VASE-042"), "x.png", version=version)
        self.change()
        self.generate()  # Brief 2 starts its own count.
        self.assertEqual([i["brief_version"] for i in self.row()["images"][-2:]], [2, 2])

    def test_old_delivery_filenames_are_not_renamed(self):
        first = self.approve()
        with database() as connection:
            deliveries.queue(connection, "VASE-042", first, "VASE-042_styled_01.png")
            deliveries.mark_delivered(connection, first, "F0", "https://drive.google.com/file/d/F0/view")
        self.change()
        second = self.approve()
        self.deliver()
        with database() as connection:
            names = {image_id: deliveries.get_delivery(connection, image_id)["filename"] for image_id in (first, second)}
        self.assertEqual(names, {first: "VASE-042_styled_01.png", second: "VASE-042_styled_v3.png"})
        self.assertEqual([f["name"] for f in self.drive.files.values()], ["VASE-042_styled_v3.png"])


if __name__ == "__main__":
    unittest.main()
