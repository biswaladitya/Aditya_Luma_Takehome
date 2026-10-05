"""Stages and tabs: the backend decides each product's stage and the dashboard's tab counts. Slack, Luma and Drive are faked."""

import unittest
from unittest.mock import patch

import tests.test_brief_versions as brief_tests
from backend.db import database
from backend.repositories import deliveries, products
from backend.services.catalog import TABS
from backend.services.review import handle_block_action
from tests.test_catalog_imports import product
from tests.test_review import click

SKU = "VASE-042"


class StageTests(unittest.TestCase):
    # The catalog (VASE-042 and LAMP-7), the fakes and the helpers are the brief version tests'.
    preview, confirm, change, row, until, generate, approve, deliver = (
        getattr(brief_tests.BriefVersionTests, name)
        for name in ("preview", "confirm", "change", "row", "until", "generate", "approve", "deliver"))

    setUp = brief_tests.BriefVersionTests.setUp

    def stage(self, sku=SKU):
        return self.row(sku)["stage"]

    def queue_image(self, sku=SKU, version=9):
        with database() as connection:
            return products.save_generated_image(connection, sku, product(sku), "q.png", version=version, status="queued")

    def fail_generation(self, sku=SKU):
        with patch("backend.luma.generate_image", side_effect=RuntimeError("boom")):
            self.client.post("/api/generations", json={"skus": [sku]})
            self.until(lambda: self.row(sku)["images"] and not self.row(sku)["generating"])

    def test_missing_attributes_is_needs_input(self):
        self.confirm(self.preview([product("BLANK", shot_idea="")]))
        self.assertEqual(self.stage("BLANK"), "needs_input")

    def test_never_generated_is_ready(self):
        self.assertEqual(self.stage(), "ready")

    def test_running_generation_is_generating(self):
        self.queue_image()
        self.assertEqual(self.stage(), "generating")

    def test_failed_latest_batch_is_failed_not_ready(self):
        self.fail_generation()
        row = self.row()
        self.assertEqual((row["can_generate"], row["stage"]), (True, "failed"))

    def test_candidates_that_did_not_reach_slack_are_post_failed(self):
        self.slack.fail_posts = 1
        self.generate()
        self.assertEqual(self.stage(), "post_failed")

    def test_posted_candidates_are_with_ellie(self):
        self.generate()
        self.assertEqual(self.stage(), "with_ellie")

    def test_approved_image_not_in_drive_is_approved(self):
        self.approve()
        self.assertEqual(self.stage(), "approved")

    def test_drive_write_in_flight_is_saving(self):
        approved = self.approve()
        with database() as connection:
            deliveries.queue(connection, SKU, approved, "VASE-042_styled_v1.png")
        self.assertEqual(self.stage(), "saving")

    def test_delivered_approval_is_in_drive(self):
        self.approve()
        self.deliver()
        self.assertEqual(self.stage(), "in_drive")

    def test_in_drive_product_with_newer_waiting_candidates_is_with_ellie(self):
        self.approve()
        self.deliver()
        self.change()
        self.assertEqual(self.stage(), "in_drive")  # The regenerate is optional.
        self.generate()
        row = self.row()
        self.assertEqual((row["delivery_status"], row["stage"]), ("delivered", "with_ellie"))

    def test_generating_wins_over_everything(self):
        approved = self.approve()
        with database() as connection:
            deliveries.queue(connection, SKU, approved, "VASE-042_styled_v1.png")
        self.queue_image()
        row = self.row()
        self.assertEqual((row["delivering"], row["review_status"], row["stage"]), (True, "approved", "generating"))

    def test_failed_stage_looks_only_at_the_latest_batch(self):
        self.fail_generation()
        self.generate()
        handle_block_action(click(self.row()["images"][-1]["id"]), self.slack)
        self.deliver()
        self.change()  # Can generate again; the failed batch is no longer the latest.
        row = self.row()
        self.assertEqual((row["can_generate"], row["stage"]), (True, "in_drive"))

    def test_import_preview_rows_carry_a_stage(self):
        self.generate()
        rows = self.preview([product(SKU, notes="x"), product("NEW"), product("BLANK", photo=""), product("")])["rows"]
        self.assertEqual([row["stage"] for row in rows], ["with_ellie", "ready", "needs_input", "needs_input"])

    def test_catalog_returns_the_seven_tabs_in_order_with_counts(self):
        self.confirm(self.preview([product("BLANK", shot_idea=""), product("MUG-1")]))
        self.approve()
        self.generate("LAMP-7")
        catalog = self.client.get("/api/catalog").json()
        tabs = catalog["tabs"]
        self.assertEqual([(tab["id"], tab["label"], tab["stages"]) for tab in tabs],
                         [(tab, label, list(stages)) for tab, label, stages in TABS])
        self.assertEqual([tab["id"] for tab in tabs], ["generate", "generating", "post", "ellie", "drive", "done", "input"])
        self.assertEqual({tab["id"]: tab["count"] for tab in tabs},
                         {"generate": 1, "generating": 0, "post": 0, "ellie": 1, "drive": 1, "done": 0, "input": 1})
        self.assertEqual(sum(tab["count"] for tab in tabs), catalog["total_rows"])
        for tab in tabs:
            self.assertEqual(tab["count"], sum(row["stage"] in tab["stages"] for row in catalog["rows"]))
