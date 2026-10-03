"""Slack review: Generate posts candidates, one final approval per brief, visible failures. Slack is faked."""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from litestar.testing import TestClient

from backend import slack
from backend.app import app
from backend.db import database
from backend.repositories import products
from backend.services.generation import recover_interrupted
from backend.services.review import handle_block_action
from tests.test_catalog_imports import csv_bytes, product

ELLIE = "UELLIE"
REAL_SLACK_CLIENT = slack.client  # Refuses before any network call when the token is missing.


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
            "SLACK_CHANNEL_ID": "C1", "SLACK_APPROVER_USER_ID": "",
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

    def generate(self, skus=("A-1",)):
        """Generate, which posts the candidates to Slack, and wait until every candidate settles."""
        with patch("backend.luma.generate_image", return_value=("gen", b"img", "image/png")):
            response = self.client.post("/api/generations", json={"skus": list(skus)})
            self.assertEqual(response.status_code, 202, response.text)
            self.until(lambda: all(r["images"] and not r["generating"] for r in self.rows() if r["sku"] in skus))

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

    def retry(self, skus=("A-1",)):
        response = self.client.post("/api/reviews", json={"skus": list(skus)})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def image_ids(self, sku="A-1"):
        return [i["id"] for i in self.row(sku)["images"]]

    def test_generate_posts_one_thread_with_a_button_per_candidate(self):
        self.generate()
        row = self.row()
        self.assertEqual((row["review_status"], row["can_send"], row["post_error"]), ("awaiting_approval", False, None))
        parent, first, second = self.slack.posts
        self.assertIsNone(parent["thread_ts"])
        self.assertEqual(first["thread_ts"], second["thread_ts"])
        self.assertEqual(first["thread_ts"], "100.1")
        self.assertEqual({(u["channel"], u["thread_ts"]) for u in self.slack.uploads}, {("C1", "100.1")})
        for candidate in (first, second):
            action = candidate["blocks"][-1]["elements"][0]
            self.assertEqual(action["action_id"], "approve_image")
            self.assertIn(action["value"], self.image_ids())
        self.assertEqual({(i["status"], i["review"]["state"]) for i in row["images"]}, {("done", "awaiting_approval")})
        self.assertEqual(self.row("B-2")["images"], [])  # Only what was generated is posted.

    def test_candidates_stay_processing_until_posted(self):
        seen = []
        original = self.slack.upload_image

        def upload(path, title, channel, thread_ts):
            seen.append({i["status"] for i in self.row()["images"]})
            return original(path, title, channel, thread_ts)

        self.slack.upload_image = upload
        self.generate()
        self.assertEqual(seen[0], {"processing"})
        self.assertTrue(all(image["posting"] is False for image in self.row()["images"]))

    def test_retry_posting_with_nothing_to_post_does_not_repost(self):
        self.generate()
        self.assertEqual(self.retry()["queued"], [])
        self.assertEqual(len(self.slack.posts), 3)

    def test_approval_is_final_and_only_one_image_per_brief(self):
        self.generate()
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

    # Ellie-only is relaxed during tester access; remove the marker when the client deployment sets SLACK_APPROVER_USER_ID.
    @unittest.expectedFailure
    def test_only_the_approver_can_decide(self):
        self.generate()
        self.assertEqual(handle_block_action(click(self.image_ids()[0], user="USOMEONE"), self.slack), "unauthorized")
        self.assertEqual(self.row()["review_status"], "awaiting_approval")
        self.assertIn("Only the designated approver", self.slack.ephemerals[0][1])

    # Testing phase only: retire once the client deployment restricts approval to Ellie.
    def test_anyone_can_approve(self):
        self.generate()
        first, second = self.image_ids()
        self.assertEqual(handle_block_action(click(first, user="USOMEONE"), self.slack), "approved")
        with database() as connection:
            approved_by = connection.execute("SELECT approved_by FROM image_reviews WHERE image_id = ?", (first,)).fetchone()[0]
        self.assertEqual(approved_by, "USOMEONE")
        self.assertIn("✅ Approved by <@USOMEONE>", [u["text"] for u in self.slack.updates])
        self.assertEqual(self.slack.ephemerals, [])
        self.assertEqual(handle_block_action(click(second, user="UOTHER"), self.slack), "sibling_approved")

    def test_configured_approver_is_enforced(self):
        self.generate()
        first = self.image_ids()[0]
        with patch.dict(os.environ, {"SLACK_APPROVER_USER_ID": ELLIE}):
            self.assertEqual(handle_block_action(click(first, user="USOMEONE"), self.slack), "unauthorized")
            self.assertEqual(self.row()["review_status"], "awaiting_approval")
            self.assertIn("Only the designated approver", self.slack.ephemerals[0][1])
            self.assertEqual(handle_block_action(click(first), self.slack), "approved")

    def test_database_enforces_one_final_approval_per_brief(self):
        self.generate()
        first, second = self.image_ids()
        handle_block_action(click(first), self.slack)
        with database() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE image_reviews SET state = 'approved', approved_by = 'x', approved_at = 'now' WHERE image_id = ?", (second,))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE image_reviews SET state = 'awaiting_approval' WHERE image_id = ?", (first,))
            with self.assertRaises(sqlite3.IntegrityError):  # pending_send no longer exists.
                connection.execute("UPDATE image_reviews SET state = 'pending_send' WHERE image_id = ?", (second,))

    def test_failed_post_is_visible_and_retryable(self):
        self.slack.fail_posts = 1
        self.generate()
        row = self.row()
        self.assertIn("slack is down", row["post_error"])
        self.assertEqual({(i["status"], i["review"]) for i in row["images"]}, {("done", None)})
        self.assertIsNone(row["review_status"])
        self.assertTrue(row["can_send"])
        self.assertEqual(self.retry()["queued"], ["A-1"])
        self.until(lambda: self.row()["review_status"] == "awaiting_approval" and not self.row()["generating"])
        row = self.row()
        self.assertIsNone(row["post_error"])
        self.assertFalse(row["can_send"])

    def test_partial_failure_retries_only_the_unposted_candidate(self):
        self.slack.fail_uploads = 1
        self.generate()
        row = self.row()
        self.assertEqual(sorted(bool(i["review"]) for i in row["images"]), [False, True])
        self.assertIn("upload failed", row["post_error"])
        posted = len(self.slack.posts)
        self.retry()
        self.until(lambda: not self.row()["can_send"] and not self.row()["generating"])
        self.assertEqual(len(self.slack.posts), posted + 1)  # One more candidate, same thread, no new parent.
        self.assertEqual(self.slack.posts[-1]["thread_ts"], "100.1")
        self.assertEqual({i["review"]["state"] for i in self.row()["images"]}, {"awaiting_approval"})

    def test_restart_while_posting_leaves_a_retryable_candidate(self):
        with database() as connection:
            image = products.save_generated_image(connection, "A-1", product("A-1"), "a.png", None, status="processing")
            products.update_generated_image(connection, image["id"], luma_generation_id="gen")  # Luma returned it.
            products.save_generated_image(connection, "B-2", product("B-2"), "b.png", None, status="processing")
        self.assertTrue(self.row()["images"][0]["posting"])
        recover_interrupted()
        a, b = self.row(), self.row("B-2")
        self.assertEqual((a["images"][0]["status"], a["can_send"]), ("done", True))
        self.assertIn("restart", a["post_error"])
        self.assertEqual(b["images"][0]["status"], "failed")

    def test_missing_slack_configuration_is_reported(self):
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": ""}), patch("backend.slack.client", side_effect=REAL_SLACK_CLIENT):
            self.generate()
            self.assertIn("SLACK_BOT_TOKEN", self.row()["post_error"])
            response = self.client.post("/api/reviews", json={"skus": ["A-1"]})
        self.assertEqual(response.status_code, 503)
        self.assertIn("SLACK_BOT_TOKEN", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
