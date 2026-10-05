"""Slack review: Generate posts candidates, up to three final approvals per brief, visible failures. Slack is faked."""

import json
import os
import sqlite3
import tempfile
import threading
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
        self.fail_permalinks = False
        self.permalinks = []

    def post_message(self, channel, text, blocks=None, thread_ts=None):
        if self.fail_posts:
            self.fail_posts -= 1
            raise RuntimeError("slack is down")
        self.posts.append({"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts})
        return f"100.{len(self.posts)}"

    def permalink(self, channel, ts):
        if self.fail_permalinks:
            raise RuntimeError("no permalink")
        self.permalinks.append((channel, ts))
        return f"https://slack.test/archives/{channel}/p{ts.replace('.', '')}"

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


def more_click(post, ts="100.6", user=ELLIE):
    """A click on a posted More options message (one of FakeSlack.posts)."""
    return {"user": {"id": user}, "channel": {"id": "C1"}, "message": {"ts": ts},
            "actions": [post["blocks"][-1]["elements"][0]]}


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
        parent, *candidates, more = self.slack.posts
        first, second = candidates[:2]
        self.assertEqual(len(candidates), 4)
        self.assertEqual((more["thread_ts"], more["blocks"][-1]["elements"][0]["action_id"]), ("100.1", "more_options"))
        self.assertIsNone(parent["thread_ts"])
        self.assertEqual(first["thread_ts"], second["thread_ts"])
        self.assertEqual(first["thread_ts"], "100.1")
        self.assertEqual({(u["channel"], u["thread_ts"]) for u in self.slack.uploads}, {("C1", "100.1")})
        for candidate in candidates:
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
        self.assertEqual(len(self.slack.posts), 6)  # Parent, four candidates, More options.

    def test_approval_is_final_and_up_to_three_images_per_brief(self):
        self.generate()
        first, second, third, fourth = self.image_ids()
        self.assertEqual(handle_block_action(click(first), self.slack), "approved")
        row = self.row()
        # One approval is enough for Drive, but the review stays open for two more.
        self.assertEqual((row["review_status"], row["approved_count"], row["can_deliver"], row["approval_limit_reached"]),
                         ("approved", 1, True, False))
        approved, *waiting = self.slack.updates
        self.assertEqual((approved["text"], approved["blocks"][-1]["type"]), (f"✅ Approved by <@{ELLIE}> — 1 of 3", "context"))
        for update in waiting:  # The other three keep their Approve button, now stating the count.
            button = update["blocks"][-1]["elements"][0]
            self.assertEqual(button["action_id"], "approve_image")
            self.assertIn("1 of 3 approved so far", button["confirm"]["text"]["text"])
        self.assertEqual(len(waiting), 3)
        self.assertEqual(handle_block_action(click(second), self.slack), "approved")
        self.assertEqual(self.row()["approved_count"], 2)
        self.slack.updates.clear()
        self.assertEqual(handle_block_action(click(third), self.slack), "approved")
        # The third approval closes the review: the last candidate loses its button.
        self.assertEqual([(u["text"], u["blocks"][-1]["type"]) for u in self.slack.updates],
                         [(f"✅ Approved by <@{ELLIE}> — 3 of 3", "context"), ("Not selected — 3 images were approved", "context")])
        self.assertEqual(handle_block_action(click(fourth), self.slack), "approval_limit")
        self.assertEqual(handle_block_action(click(first), self.slack), "already_approved")
        row = self.row()
        self.assertEqual(row["approved_image_ids"], [first, second, third])
        self.assertEqual((row["approval_limit_reached"], row["can_request_more"], row["can_generate"]), (True, False, False))
        self.assertEqual(len(self.slack.ephemerals), 2)
        self.assertIn("3 images are already approved", self.slack.ephemerals[0][1])

    def test_simultaneous_clicks_approve_exactly_three(self):
        self.generate()
        outcomes = []
        threads = [threading.Thread(target=lambda image_id=image_id: outcomes.append(handle_block_action(click(image_id), self.slack)))
                   for image_id in self.image_ids()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["approval_limit", "approved", "approved", "approved"])
        self.assertEqual(self.row()["approved_count"], 3)
        # Slack ends matching the database: the one refused candidate has no button left.
        final = {u["ts"]: u for u in self.slack.updates}
        self.assertEqual(sorted(u["text"][:1] for u in final.values()), ["N", "✅", "✅", "✅"])

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
        first, second = self.image_ids()[:2]
        self.assertEqual(handle_block_action(click(first, user="USOMEONE"), self.slack), "approved")
        with database() as connection:
            approved_by = connection.execute("SELECT approved_by FROM image_reviews WHERE image_id = ?", (first,)).fetchone()[0]
        self.assertEqual(approved_by, "USOMEONE")
        self.assertIn("✅ Approved by <@USOMEONE> — 1 of 3", [u["text"] for u in self.slack.updates])
        self.assertEqual(self.slack.ephemerals, [])
        self.assertEqual(handle_block_action(click(second, user="UOTHER"), self.slack), "approved")

    def test_configured_approver_is_enforced(self):
        self.generate()
        first = self.image_ids()[0]
        with patch.dict(os.environ, {"SLACK_APPROVER_USER_ID": ELLIE}):
            self.assertEqual(handle_block_action(click(first, user="USOMEONE"), self.slack), "unauthorized")
            self.assertEqual(self.row()["review_status"], "awaiting_approval")
            self.assertIn("Only the designated approver", self.slack.ephemerals[0][1])
            self.assertEqual(handle_block_action(click(first), self.slack), "approved")

    def test_database_keeps_approvals_final(self):
        self.generate()
        first, second = self.image_ids()[:2]
        handle_block_action(click(first), self.slack)
        with database() as connection:
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
        self.assertEqual(sorted(bool(i["review"]) for i in row["images"]), [False, True, True, True])
        self.assertIn("upload failed", row["post_error"])
        posted = len(self.slack.posts)
        self.retry()
        self.until(lambda: not self.row()["can_send"] and not self.row()["generating"] and len(self.slack.posts) == posted + 2)
        # One more candidate and the batch's one More options button, same thread, no new parent.
        self.assertEqual(self.slack.posts[-1]["text"], "None of these work?")
        self.assertEqual(self.slack.posts[-2]["thread_ts"], "100.1")
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

    def test_more_options_queues_another_batch_in_the_same_thread(self):
        self.generate()
        button = self.slack.posts[-1]
        self.assertEqual(json.loads(button["blocks"][-1]["elements"][0]["value"]), {"sku": "A-1", "brief_version": 1})
        release = threading.Event()  # Holds Luma so the second click lands while the batch is running.
        with patch("backend.luma.generate_image", side_effect=lambda *_: release.wait(5) and ("gen", b"img", "image/png")):
            self.assertEqual(handle_block_action(more_click(button), self.slack), "queued")
            self.assertEqual(handle_block_action(more_click(button), self.slack), "generating")
            release.set()
            self.until(lambda: len(self.row()["images"]) == 8 and not self.row()["generating"])
        self.assertEqual(self.slack.updates[0]["text"], f"More options requested by <@{ELLIE}>")
        self.assertEqual(self.slack.updates[0]["ts"], "100.6")
        row = self.row()
        self.assertEqual([i["version"] for i in row["images"]], [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual({i["review"]["state"] for i in row["images"]}, {"awaiting_approval"})
        self.assertEqual((row["images_used"], row["can_request_more"]), (8, True))
        self.assertEqual({p["thread_ts"] for p in self.slack.posts[1:]}, {"100.1"})
        self.assertEqual(self.slack.posts[-1]["text"], "None of these work?")
        # One approval doesn't end the review, so another batch can still be requested.
        self.assertEqual(handle_block_action(click(row["images"][0]["id"]), self.slack), "approved")
        self.assertTrue(self.row()["can_request_more"])

    def test_more_options_batch_that_fails_posts_a_fresh_button(self):
        from backend.luma import LumaError
        self.generate()
        with patch("backend.luma.generate_image", side_effect=LumaError("boom")):
            self.assertEqual(handle_block_action(more_click(self.slack.posts[-1]), self.slack), "queued")
            self.until(lambda: self.slack.posts[-1]["text"] == "Generation failed, try again.")
        self.assertEqual(self.slack.posts[-1]["blocks"][-1]["elements"][0]["action_id"], "more_options")
        self.assertEqual((self.row()["images_used"], self.row()["review_status"]), (4, "awaiting_approval"))

    def test_missing_slack_configuration_is_reported(self):
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": ""}), patch("backend.slack.client", side_effect=REAL_SLACK_CLIENT):
            self.generate()
            self.assertIn("SLACK_BOT_TOKEN", self.row()["post_error"])
            response = self.client.post("/api/reviews", json={"skus": ["A-1"]})
        self.assertEqual(response.status_code, 503)
        self.assertIn("SLACK_BOT_TOKEN", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
