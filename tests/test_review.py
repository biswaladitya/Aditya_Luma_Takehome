"""Slack review: explicit send, one final approval per product, visible failures. Slack is faked."""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from litestar.testing import TestClient

from backend.app import app
from backend.db import database
from backend.services.review import handle_block_action
from tests.test_catalog_imports import csv_bytes, product

ELLIE = "UELLIE"


class FakeSlack:
    def __init__(self):
        self.posts, self.updates, self.ephemerals, self.uploads = [], [], [], []
        self.fail_posts = 0
        self.fail_uploads = 0

    def post_message(self, channel, text, blocks=None, thread_ts=None):
        if self.fail_posts:
            self.fail_posts -= 1
            raise RuntimeError("slack is down")
        self.posts.append({"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts})
        return f"100.{len(self.posts)}"

    def upload_image(self, path, title, channel, thread_ts):
        if self.fail_uploads:
            self.fail_uploads -= 1
            raise RuntimeError("upload failed")
        self.uploads.append({"title": title, "channel": channel, "thread_ts": thread_ts})
        return f"F{len(self.uploads)}"

    def update_message(self, channel, ts, text, blocks):
        self.updates.append({"ts": ts, "text": text, "blocks": blocks})

    def post_ephemeral(self, channel, user, text):
        self.ephemerals.append((user, text))


def click(image_id, user=ELLIE):
    return {"user": {"id": user}, "channel": {"id": "C1"},
            "actions": [{"action_id": "approve_image", "value": image_id}]}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        env = patch.dict(os.environ, {
            "DATABASE_PATH": str(self.directory / "catalog.sqlite3"), "DATA_DIR": "",
            "SLACK_APP_TOKEN": "", "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_CHANNEL_ID": "C1", "SLACK_APPROVER_USER_ID": ELLIE,
        })
        env.start()
        self.addCleanup(env.stop)
        self.slack = FakeSlack()
        patcher = patch("backend.slack.client", return_value=self.slack)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.enterContext(TestClient(app, raise_server_exceptions=False))
        rows = [product("A-1"), product("B-2")]
        preview = self.client.post("/api/catalog/preview", files={"data": ("c.csv", csv_bytes(rows), "text/csv")}).json()
        self.client.post(f"/api/catalog/imports/{preview['preview_id']}/confirm")
        with patch("backend.luma.generate_image", return_value=("gen", b"img", "image/png")):
            self.client.post("/api/generations", json={"skus": ["A-1", "B-2"]})
            self.until(lambda: all(i["status"] == "done" for r in self.rows() for i in r["images"]) and self.rows()[0]["images"])

    def rows(self):
        return self.client.get("/api/catalog").json()["rows"]

    def row(self, sku="A-1"):
        return next(r for r in self.rows() if r["sku"] == sku)

    def until(self, condition):
        for _ in range(50):
            if condition():
                return
            time.sleep(0.1)
        self.fail("condition not reached")

    def send(self, skus=("A-1",)):
        response = self.client.post("/api/reviews", json={"skus": list(skus)})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def image_ids(self, sku="A-1"):
        return [i["id"] for i in self.row(sku)["images"]]

    def test_generation_alone_sends_nothing(self):
        self.assertEqual(self.slack.posts, [])
        self.assertTrue(self.row()["can_send"])
        self.assertIsNone(self.row()["review_status"])

    def test_send_posts_one_thread_with_a_button_per_candidate(self):
        self.assertEqual(self.send()["queued"], ["A-1"])
        self.until(lambda: self.row()["review_status"] == "awaiting_approval")
        parent, first, second = self.slack.posts
        self.assertIsNone(parent["thread_ts"])
        self.assertEqual(first["thread_ts"], second["thread_ts"])
        self.assertEqual(first["thread_ts"], "100.1")
        self.assertEqual({(u["channel"], u["thread_ts"]) for u in self.slack.uploads}, {("C1", "100.1")})
        for candidate in (first, second):
            action = candidate["blocks"][-1]["elements"][0]
            self.assertEqual(action["action_id"], "approve_image")
            self.assertIn(action["value"], self.image_ids())
        self.assertEqual({i["review"]["state"] for i in self.row()["images"]}, {"awaiting_approval"})
        self.assertFalse(self.row()["can_send"])
        self.assertEqual(self.row("B-2")["review_status"], None)

    def test_sending_again_does_not_repost(self):
        self.send()
        self.until(lambda: self.row()["review_status"] == "awaiting_approval")
        second = self.send()
        self.assertEqual(second["queued"], [])
        self.assertEqual(len(self.slack.posts), 3)

    def test_approval_is_final_and_only_one_image_per_product(self):
        self.send()
        self.until(lambda: self.row()["review_status"] == "awaiting_approval")
        first, second = self.image_ids()
        self.assertEqual(handle_block_action(click(first), self.slack), "approved")
        row = self.row()
        self.assertEqual((row["review_status"], row["approved_image_id"]), ("approved", first))
        self.assertEqual({u["text"] for u in self.slack.updates}, {f"✅ Approved by <@{ELLIE}>", "Not selected — another image was approved"})
        self.assertTrue(all(u["blocks"][-1]["type"] == "context" for u in self.slack.updates))
        self.assertEqual(handle_block_action(click(second), self.slack), "sibling_approved")
        self.assertEqual(handle_block_action(click(first), self.slack), "already_approved")
        self.assertEqual(self.row()["approved_image_id"], first)
        self.assertEqual(len(self.slack.ephemerals), 2)

    def test_only_the_approver_can_decide(self):
        self.send()
        self.until(lambda: self.row()["review_status"] == "awaiting_approval")
        self.assertEqual(handle_block_action(click(self.image_ids()[0], user="USOMEONE"), self.slack), "unauthorized")
        self.assertEqual(self.row()["review_status"], "awaiting_approval")
        self.assertIn("Only the designated approver", self.slack.ephemerals[0][1])

    def test_database_enforces_one_final_approval(self):
        self.send()
        self.until(lambda: self.row()["review_status"] == "awaiting_approval")
        first, second = self.image_ids()
        handle_block_action(click(first), self.slack)
        with database() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE image_reviews SET state = 'approved', approved_by = 'x', approved_at = 'now' WHERE image_id = ?", (second,))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE image_reviews SET state = 'awaiting_approval' WHERE image_id = ?", (first,))

    def test_failed_send_is_visible_and_retryable(self):
        self.slack.fail_posts = 1
        self.send()
        self.until(lambda: self.row()["send_error"])
        row = self.row()
        self.assertIn("slack is down", row["send_error"])
        self.assertEqual(row["review_status"], "pending_send")
        self.assertTrue(row["can_send"])
        self.send()
        self.until(lambda: self.row()["review_status"] == "awaiting_approval")
        self.assertIsNone(self.row()["send_error"])

    def test_partial_failure_retries_only_the_unsent_candidate(self):
        self.slack.fail_uploads = 1
        self.send()
        self.until(lambda: self.row()["send_error"])
        states = sorted(i["review"]["state"] for i in self.row()["images"])
        self.assertEqual(states, ["awaiting_approval", "pending_send"])
        posted = len(self.slack.posts)
        self.send()
        self.until(lambda: self.row()["review_status"] == "awaiting_approval" and not self.row()["can_send"])
        self.assertEqual(len(self.slack.posts), posted + 1)  # One more candidate, same thread, no new parent.
        self.assertEqual(self.slack.posts[-1]["thread_ts"], "100.1")

    def test_missing_slack_configuration_is_reported(self):
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": ""}):
            response = self.client.post("/api/reviews", json={"skus": ["A-1"]})
        self.assertEqual(response.status_code, 503)
        self.assertIn("SLACK_BOT_TOKEN", response.json()["detail"])

    def test_upgrade_from_v2_adds_review_table(self):
        with database() as connection:
            connection.execute("DROP TABLE image_reviews")
            connection.execute("PRAGMA user_version = 2")
        with database() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM image_reviews").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
