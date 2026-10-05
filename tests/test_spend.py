"""Generation spend: the backend counts every image Luma returned, for the dashboard and the Slack report alike."""

import unittest

import tests.test_brief_versions as brief_tests
from backend.db import database
from backend.repositories import products
from backend.services.catalog import EST_COST_PER_IMAGE_USD, IMAGES_PER_REQUEST, spend_total
from tests.test_catalog_imports import product

SKU = "VASE-042"


class SpendTests(unittest.TestCase):
    # The catalog (VASE-042 and LAMP-7), the fakes and the helpers are the brief version tests'.
    preview, confirm, change, row, until, generate, approve, deliver = (
        getattr(brief_tests.BriefVersionTests, name)
        for name in ("preview", "confirm", "change", "row", "until", "generate", "approve", "deliver"))

    setUp = brief_tests.BriefVersionTests.setUp

    def spend(self):
        return self.client.get("/api/catalog").json()["spend"]

    def image(self, status, generation_id=None, version=9):
        with database() as connection:
            return products.save_generated_image(
                connection, SKU, product(SKU), f"{version}.png", generation_id, version=version, status=status)

    def test_no_images_is_zero(self):
        self.assertEqual(self.spend(), {"images": 0, "est_cost_usd": 0})
        self.assertEqual(spend_total([]), {"images": 0, "est_cost_usd": 0})

    def test_images_luma_never_returned_do_not_count(self):
        for version, status in enumerate(("queued", "processing", "failed"), 1):
            self.image(status, version=version)
        self.assertEqual(self.spend()["images"], 0)

    def test_only_images_with_a_generation_id_count(self):
        self.image("queued", version=1)
        self.image("done", "gen", version=2)
        self.image("failed", "gen", version=3)  # Returned by Luma, so paid for.
        self.assertEqual(self.spend()["images"], 2)

    def test_image_still_posting_counts(self):
        self.image("processing", "gen")
        self.assertTrue(self.row()["images"][0]["posting"])
        self.assertEqual(self.spend()["images"], 1)

    def test_outdated_approved_and_delivered_images_still_count(self):
        self.approve()
        self.deliver()
        self.change()
        row = self.row()
        self.assertTrue(all(image["outdated"] for image in row["images"]))
        self.assertEqual(row["delivery_status"], "delivered")
        self.assertEqual(self.spend()["images"], IMAGES_PER_REQUEST)
        self.generate()
        self.assertEqual(self.spend()["images"], 2 * IMAGES_PER_REQUEST)

    def test_cost_is_count_times_the_price_per_image(self):
        self.generate()
        self.generate("LAMP-7")
        spend = self.spend()
        self.assertEqual(spend["images"], 2 * IMAGES_PER_REQUEST)
        self.assertAlmostEqual(spend["est_cost_usd"], spend["images"] * EST_COST_PER_IMAGE_USD)
        returned = [{"images": [{"luma_generation_id": "gen"}] * 3}]
        self.assertEqual(spend_total(returned)["est_cost_usd"], 0.1932)  # Rounded: 3 × 0.0644 is 0.19319999999999998.

    def test_catalog_response_includes_spend(self):
        self.generate()
        catalog = self.client.get("/api/catalog").json()
        self.assertEqual(set(catalog["spend"]), {"images", "est_cost_usd"})
        self.assertEqual(catalog["spend"]["images"],
                         sum(image["luma_generation_id"] is not None for row in catalog["rows"] for image in row["images"]))

    def test_candidates_route_reports_the_whole_catalog(self):
        self.generate()
        full, candidates = (self.client.get(path).json() for path in ("/api/catalog", "/api/catalog/generation-candidates"))
        self.assertEqual([row["sku"] for row in candidates["rows"]], ["LAMP-7"])
        self.assertEqual(candidates["spend"], full["spend"])
        self.assertEqual(candidates["tabs"], full["tabs"])
        self.assertEqual(full["spend"]["images"], IMAGES_PER_REQUEST)

    def test_import_preview_has_no_spend_or_tabs(self):
        self.generate()
        preview = self.preview([product(SKU, notes="x"), product(SKU, notes="y")])  # A duplicate SKU would count twice.
        self.assertNotIn("spend", preview)
        self.assertNotIn("tabs", preview)
        self.assertEqual(spend_total([{"sku": "OLD"}]), {"images": 0, "est_cost_usd": 0})  # A row without images.
