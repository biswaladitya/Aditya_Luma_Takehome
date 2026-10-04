"""Brief versions: a brief change makes older images outdated; approvals survive it; Drive keeps every one."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from litestar.testing import TestClient

from backend.app import app
from backend.db import database
from backend.repositories import deliveries, products, reviews
from backend.services.catalog import IMAGES_PER_REQUEST
from backend.services.review import OUTDATED, handle_block_action
from tests.test_catalog_imports import csv_bytes, product
from tests.test_delivery import TOKEN, FakeDrive
from tests.test_review import ELLIE, FakeSlack, click


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
        return [i["id"] for i in self.row(sku)["images"][-IMAGES_PER_REQUEST:]]

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
        self.until(lambda: len(self.slack.updates) == 4)
        self.assertEqual({u["text"] for u in self.slack.updates}, {OUTDATED})
        self.assertTrue(all(u["blocks"][-1]["type"] == "context" for u in self.slack.updates))  # No Approve button.
        row = self.row()
        self.assertTrue(all(i["outdated"] for i in row["images"]))
        self.assertEqual((row["review_status"], row["generation_status"], row["can_generate"]),
                         (None, "changed_since_generation", True))
        self.assertEqual(handle_block_action(click(waiting[0]), self.slack), "outdated")
        self.assertIn("older product attributes", self.slack.ephemerals[-1][1])
        self.assertEqual(self.row()["images"][0]["review"]["state"], "awaiting_approval")
        self.assertEqual(self.luma.call_count, 4)  # Regenerating needs an explicit click.
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
                         ["VASE-042_styled_v1.png", "VASE-042_styled_v5.png"])
        row = self.row()
        self.assertEqual((row["delivery_status"], row["can_deliver"]), ("delivered", False))

    def test_in_drive_product_is_kept_with_an_optional_regenerate(self):
        self.approve()
        self.deliver()
        self.assertEqual(self.change(), "in_drive")
        row = self.row()
        self.assertEqual((row["delivery_status"], row["brief_changed"], row["can_generate"]), ("delivered", True, True))
        self.assertEqual(self.luma.call_count, 4)  # Never regenerated by an import.

    def test_new_candidates_after_a_save_to_drive_are_with_ellie(self):
        self.approve()
        self.deliver()
        outdated = lambda: [u["ts"] for u in self.slack.updates if u["text"] == OUTDATED]
        self.change()
        # One approval left the first brief's review open, so its other three candidates are marked now.
        self.until(lambda: len(outdated()) == 3)
        earlier = outdated()
        waiting = self.generate()  # The optional regenerate; its candidates wait on Ellie.
        self.assertEqual(self.change(shot_idea="Third idea"), "with_ellie")
        with database() as connection:
            messages = {reviews.get_review(connection, image_id)["slack_message_ts"] for image_id in waiting}
        self.until(lambda: messages <= set(outdated()))
        time.sleep(0.2)
        # Only the brief being replaced is marked; the first brief's candidates are not marked twice.
        self.assertEqual(sorted(outdated()), sorted(earlier + list(messages)))

    def test_info_only_change_keeps_state_and_images(self):
        approved = self.approve()
        self.assertEqual(self.change(shot_idea=product()["shot_idea"], price="$1", notes="n", category="c"), "info_only")
        row = self.row()
        self.assertEqual((row["brief_version"], row["version"], row["approved_image_id"], row["review_status"]),
                         (1, 2, approved, "approved"))
        self.assertFalse(row["can_generate"] or row["brief_changed"] or any(i["outdated"] for i in row["images"]))
        self.assertNotIn(OUTDATED, [u["text"] for u in self.slack.updates])  # Nothing marked outdated.

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
            for version in range(1, 13):  # Twelve images of brief 1 would have reached a per-product cap.
                products.save_generated_image(connection, "VASE-042", product("VASE-042"), "x.png", version=version)
        self.change()
        self.generate()  # Brief 2 starts its own count.
        self.assertEqual([i["brief_version"] for i in self.row()["images"][-2:]], [2, 2])

    def test_approvals_are_counted_per_brief(self):
        for image_id in self.generate()[:3]:
            self.assertEqual(handle_block_action(click(image_id), self.slack), "approved")
        self.assertTrue(self.row()["approval_limit_reached"])
        self.assertEqual(self.change(), "approved_not_in_drive")
        row = self.row()  # The new brief starts at zero; the three earlier approvals are kept.
        self.assertEqual((row["approved_count"], len(row["approved_image_ids"]), row["can_generate"]), (0, 3, True))
        fresh = self.approve()
        row = self.row()
        self.assertEqual((row["approved_count"], row["approval_limit_reached"]), (1, False))
        self.assertEqual(row["approved_image_ids"][-1], fresh)

    def test_import_review_warns_when_an_approved_product_still_has_waiting_candidates(self):
        self.approve()
        self.deliver()
        preview = self.preview([product("VASE-042", shot_idea="Evening table"), product("LAMP-7")])
        row = next(r for r in preview["rows"] if r["sku"] == "VASE-042")
        # In Drive, yet accepting takes three waiting candidates from Ellie.
        self.assertEqual((row["brief_case"], row["candidates_outdated"]), ("in_drive", 3))
        self.assertEqual(next(r for r in preview["rows"] if r["sku"] == "LAMP-7")["candidates_outdated"], 0)

    def test_a_later_approval_saves_only_the_new_image(self):
        first, second, third, _ = self.generate()
        handle_block_action(click(first), self.slack)
        self.assertTrue(self.row()["can_deliver"])  # One approval is enough for Drive.
        self.deliver()
        row = self.row()
        self.assertEqual((row["delivery_status"], row["can_deliver"]), ("delivered", False))
        handle_block_action(click(second), self.slack)
        handle_block_action(click(third), self.slack)
        row = self.row()
        self.assertEqual((row["can_deliver"], row["unsaved_image_ids"]), (True, [second, third]))
        self.deliver()
        self.assertEqual(sorted(f["name"] for f in self.drive.files.values()),
                         [f"VASE-042_styled_v{version}.png" for version in (1, 2, 3)])
        self.assertEqual((len(self.drive.uploads), self.drive.overwrites), (3, []))  # The first was not written again.
        self.assertFalse(self.row()["can_deliver"])

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
        self.assertEqual(names, {first: "VASE-042_styled_01.png", second: "VASE-042_styled_v5.png"})
        self.assertEqual([f["name"] for f in self.drive.files.values()], ["VASE-042_styled_v5.png"])


if __name__ == "__main__":
    unittest.main()
