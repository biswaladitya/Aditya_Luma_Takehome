"""Generation queues four candidates per SKU, stores files by name, and reports failures."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from litestar.testing import TestClient

from backend.app import app
from backend.services.generation import image_filename, slug
from tests.test_catalog_imports import product, csv_bytes


class GenerationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        env = patch.dict(os.environ, {"DATABASE_PATH": str(self.directory / "catalog.sqlite3"), "DATA_DIR": "", "SLACK_APP_TOKEN": "", "SLACK_BOT_TOKEN": ""})
        env.start()
        self.addCleanup(env.stop)
        self.client = self.enterContext(TestClient(app, raise_server_exceptions=False))
        rows = [product("A-1"), product("B-2", shot_idea="")]
        preview = self.client.post("/api/catalog/preview", files={"data": ("c.csv", csv_bytes(rows), "text/csv")}).json()
        self.client.post(f"/api/catalog/imports/{preview['preview_id']}/confirm")

    def wait(self):
        for _ in range(50):
            images = [i for r in self.client.get("/api/catalog").json()["rows"] for i in r["images"]]
            if images and all(i["status"] in ("done", "failed") for i in images):
                return images
            time.sleep(0.1)
        self.fail("generation did not finish")

    def test_filename_slug(self):
        row = product("A-1", product_name="Ceramic Vase!", color="Blue / Matte")
        self.assertEqual(image_filename(row, 2), "a-1_ceramic-vase_decor_blue-matte_ceramic_v2.jpg")
        self.assertEqual(slug("///"), "na")

    def test_generates_four_candidates_and_skips_ineligible(self):
        with patch("backend.luma.generate_image", return_value=("gen-1", b"img", "image/png")):
            response = self.client.post("/api/generations", json={"skus": ["A-1", "B-2", "NOPE"]})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["queued"], ["A-1"])
            self.assertEqual({s["sku"] for s in response.json()["skipped"]}, {"B-2", "NOPE"})
            images = self.wait()
        self.assertEqual(sorted(i["version"] for i in images), [1, 2, 3, 4])
        self.assertTrue(all(i["status"] == "done" and i["storage_key"].endswith(".png") for i in images))
        for image in images:
            self.assertEqual(self.client.get(image["image_url"]).content, b"img")
        row = self.client.get("/api/catalog").json()["rows"][0]
        self.assertEqual(row["generation_status"], "already_generated")

    def test_failure_is_visible_and_retryable(self):
        from backend.luma import LumaError
        with patch("backend.luma.generate_image", side_effect=LumaError("boom")):
            self.client.post("/api/generations", json={"skus": ["A-1"]})
            images = self.wait()
        self.assertTrue(all(i["status"] == "failed" and "boom" in i["error"] for i in images))
        self.assertEqual(self.client.get("/api/catalog").json()["rows"][0]["generation_status"], "never_generated")
        with patch("backend.luma.generate_image", return_value=("g", b"x", "image/jpeg")):
            self.assertEqual(self.client.post("/api/generations", json={"skus": ["A-1"]}).json()["queued"], ["A-1"])
            self.wait()


if __name__ == "__main__":
    unittest.main()
