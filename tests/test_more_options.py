"""More options: a Slack click on a product with four candidates generates four more. Slack and Luma are faked."""

import os
import threading
import time
import unittest
from unittest.mock import patch

import tests.test_review as review_tests
from backend.db import database
from backend.luma import LumaError
from backend.repositories import products
from backend.services.catalog import attributes
from backend.services.generation import recover_interrupted
from backend.services.catalog import IMAGES_PER_REQUEST
from backend.services.review import MORE_FAILED, handle_block_action
from tests.test_catalog_imports import csv_bytes, product
from tests.test_review import ELLIE, click, more_click

IMAGE = ("gen", b"img", "image/png")
BATCH = IMAGES_PER_REQUEST


class MoreOptionsTests(unittest.TestCase):
    # The catalog, fake Slack and helpers are the review tests'.
    rows, row, until = (getattr(review_tests.ReviewTests, name) for name in ("rows", "row", "until"))
    generate = review_tests.ReviewTests.generate

    setUp = review_tests.ReviewTests.setUp

    def button(self):
        """The newest More options message in the thread."""
        return next(post for post in reversed(self.slack.posts)
                    if post["blocks"] and post["blocks"][-1]["elements"][0].get("action_id") == "more_options")

    def more(self, user=ELLIE, button=None):
        """Click More options and wait for the batch it queued, if any, to be generated and posted."""
        before = len(self.row()["images"])
        with patch("backend.luma.generate_image", return_value=IMAGE):
            outcome = handle_block_action(more_click(button or self.button(), user=user), self.slack)
            if outcome == "queued":
                self.until(lambda: len(self.row()["images"]) == before + BATCH and not self.row()["generating"])
        return outcome

    def versions(self, state=None):
        return [i["version"] for i in self.row()["images"] if state is None or (i["review"] or {}).get("state") == state]

    def test_four_candidates_then_more_options_generates_four_more(self):
        self.generate()
        self.assertEqual(self.versions("awaiting_approval"), [1, 2, 3, 4])
        first_button = self.button()
        posts_before = len(self.slack.posts)

        self.assertEqual(self.more(), "queued")

        row = self.row()
        self.assertEqual(self.versions(), [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual({i["status"] for i in row["images"]}, {"done"})
        self.assertEqual(row["images_used"], 8)
        # The new candidates use the same product attributes as the first four.
        self.assertEqual({i["brief_version"] for i in row["images"]}, {1})
        self.assertEqual(row["images"][4]["generated_from"], row["images"][0]["generated_from"])
        # Four new candidates and one fresh button, all in the product's existing thread.
        new_posts = self.slack.posts[posts_before:]
        self.assertEqual([p["blocks"][-1]["elements"][0]["action_id"] for p in new_posts],
                         ["approve_image"] * BATCH + ["more_options"])
        self.assertEqual({p["thread_ts"] for p in new_posts}, {"100.1"})
        self.assertEqual(len(self.slack.uploads), 8)
        self.assertIsNot(self.button(), first_button)
        # The tapped button is retired, naming who asked.
        self.assertEqual([u["text"] for u in self.slack.updates], [f"More options requested by <@{ELLIE}>"])
        self.assertEqual(self.slack.ephemerals, [])

    def test_earlier_candidates_stay_approvable_after_more_options(self):
        self.generate()
        self.more()
        # Nothing is rejected: all eight wait on Ellie, and she can still pick one of the first four.
        self.assertEqual(self.versions("awaiting_approval"), [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(self.row()["review_status"], "awaiting_approval")
        second = self.row()["images"][1]["id"]
        self.assertEqual(handle_block_action(click(second), self.slack), "approved")
        row = self.row()
        self.assertEqual((row["approved_image_ids"], row["review_status"]), ([second], "approved"))

    def test_more_options_is_allowed_until_three_are_approved(self):
        self.generate()
        first, second, third = (image["id"] for image in self.row()["images"][:3])
        handle_block_action(click(first), self.slack)
        self.assertEqual(self.more(), "queued")  # One approval leaves room for two more.
        handle_block_action(click(second), self.slack)
        self.assertTrue(self.row()["can_request_more"])
        handle_block_action(click(third), self.slack)
        self.assertEqual(self.more(), "approval_limit")
        self.assertEqual(len(self.row()["images"]), 8)
        self.assertIn("3 images are already approved", self.slack.ephemerals[-1][1])

    def test_confirm_dialog_states_count_and_cost(self):
        self.generate()
        confirm = self.button()["blocks"][-1]["elements"][0]["confirm"]["text"]["text"]
        self.assertIn("4 more images", confirm)
        self.assertIn("$0.26", confirm)

    def test_double_click_queues_one_batch(self):
        self.generate()
        button = self.button()
        release = threading.Event()  # Holds Luma so the second click lands while the batch is running.
        with patch("backend.luma.generate_image", side_effect=lambda *_: release.wait(5) and IMAGE):
            self.assertEqual(handle_block_action(more_click(button), self.slack), "queued")
            self.assertEqual(handle_block_action(more_click(button), self.slack), "generating")
            release.set()
            self.until(lambda: len(self.row()["images"]) == 8 and not self.row()["generating"])
        self.assertEqual(len(self.row()["images"]), 8)
        self.assertEqual(len(self.slack.ephemerals), 1)

    def test_only_the_approver_can_request_more(self):
        self.generate()
        with patch.dict(os.environ, {"SLACK_APPROVER_USER_ID": ELLIE}):
            self.assertEqual(self.more(user="UINTERN"), "unauthorized")
            self.assertEqual(len(self.row()["images"]), 4)
            self.assertEqual(self.slack.ephemerals[0][0], "UINTERN")
            self.assertEqual(self.slack.updates, [])  # The button stays for Ellie.
            self.assertEqual(self.more(), "queued")

    def test_button_from_an_older_brief_is_refused(self):
        self.generate()
        old_button = self.button()
        changed = [product("A-1", shot_idea="Evening table"), product("B-2")]
        preview = self.client.post("/api/catalog/preview", files={"data": ("c.csv", csv_bytes(changed), "text/csv")}).json()
        self.assertEqual(self.client.post(f"/api/catalog/imports/{preview['preview_id']}/confirm").status_code, 200)
        self.assertEqual(self.more(button=old_button), "outdated")
        self.assertEqual(len(self.row()["images"]), 4)
        self.assertEqual(len(self.slack.ephemerals), 1)

    def test_cap_stops_further_batches(self):
        self.generate()
        self.assertEqual(self.more(), "queued")
        last_button = self.button()
        self.assertEqual(self.more(), "queued")  # 12 images: the cap for one set of product attributes.
        row = self.row()
        self.assertEqual((row["images_used"], row["cap_reached"], row["can_request_more"]), (12, True, False))
        self.assertIs(self.button(), last_button)  # No new button is offered at the cap.
        self.assertEqual(self.more(), "cap_reached")
        self.assertEqual(len(self.row()["images"]), 12)

    def test_malformed_or_unknown_button_queues_nothing(self):
        self.generate()
        for value in ("not json", '{"sku":"NOPE-9","brief_version":1}', '{"sku":"A-1"}',
                      '{"sku":"A-1","brief_version":Infinity}', '{"sku":"A-1","brief_version":true}'):
            with self.subTest(value=value):
                button = {"blocks": [{"elements": [{"action_id": "more_options", "value": value}]}]}
                self.assertEqual(self.more(button=button), "unknown_sku")
        self.assertEqual(len(self.row()["images"]), 4)
        self.assertEqual(len(self.slack.ephemerals), 5)

    def test_more_options_before_any_candidates_is_refused(self):
        button = {"blocks": [{"elements": [{"action_id": "more_options", "value": '{"sku":"B-2","brief_version":1}'}]}]}
        self.assertEqual(self.more(button=button), "no_candidates")
        self.assertEqual(self.row("B-2")["images"], [])

    def approve_posts(self):
        return [p for p in self.slack.posts if p["blocks"] and p["blocks"][-1]["elements"][0].get("action_id") == "approve_image"]

    def test_reaching_the_approval_limit_during_a_running_batch_posts_no_new_approve_buttons(self):
        self.generate()
        release = threading.Event()
        with patch("backend.luma.generate_image", side_effect=lambda *_: release.wait(5) and IMAGE):
            self.assertEqual(handle_block_action(more_click(self.button()), self.slack), "queued")
            for image in self.row()["images"][:3]:
                self.assertEqual(handle_block_action(click(image["id"]), self.slack), "approved")
            release.set()
            self.until(lambda: len(self.row()["images"]) == 8 and not self.row()["generating"])
        row = self.row()
        self.assertEqual(len(self.approve_posts()), BATCH)  # Only the first four ever had Approve buttons.
        self.assertEqual([(i["status"], i["review"], i["post_error"]) for i in row["images"][BATCH:]],
                         [("done", None, None)] * BATCH)
        self.assertEqual((row["review_status"], row["can_send"], row["can_request_more"]), ("approved", False, False))

    def test_failing_batches_cannot_be_retried_from_slack_forever(self):
        self.generate()
        outcomes = []
        with patch("backend.luma.generate_image", side_effect=LumaError("boom")):
            for _ in range(10):
                outcomes.append(handle_block_action(more_click(self.button()), self.slack))
                if outcomes[-1] != "queued":
                    break
                self.until(lambda: not self.row()["generating"])
                attempts = len(self.row()["images"])
                if attempts < 24:  # Each failed batch but the last hands back a button.
                    self.until(lambda: sum(p["text"] == MORE_FAILED for p in self.slack.posts) == attempts // BATCH - 1)
        self.assertEqual(outcomes, ["queued"] * 5 + ["cap_reached"])
        row = self.row()
        self.assertEqual((len(row["images"]), row["images_used"], row["can_request_more"]), (24, 4, False))

    def test_restart_before_luma_returns_posts_the_failure_note(self):
        self.generate()
        with database() as connection:
            row = products.get_products(connection, ["A-1"])[0]
            for version in (5, 6, 7, 8):
                products.save_generated_image(connection, "A-1", attributes(row), f"v{version}.png", None,
                                              version=version, status="queued", brief_version=1)
        recover_interrupted()
        self.until(lambda: self.slack.posts[-1]["text"] == MORE_FAILED)
        self.assertEqual(self.slack.posts[-1]["blocks"][-1]["elements"][0]["action_id"], "more_options")
        self.assertEqual([i["status"] for i in self.row()["images"][BATCH:]], ["failed"] * BATCH)

    def test_restart_while_posting_is_not_reported_as_a_failed_generation(self):
        self.generate()
        last = self.row()["images"][-1]["id"]
        with database() as connection:  # As a restart finds it: posted, but not yet marked done.
            products.update_generated_image(connection, last, status="processing")
        posts = len(self.slack.posts)
        recover_interrupted()
        time.sleep(0.3)  # A wrongly queued note would be posted in the background.
        self.assertEqual(len(self.slack.posts), posts)
        self.assertEqual(self.row()["review_status"], "awaiting_approval")


if __name__ == "__main__":
    unittest.main()
