"""Exercise the real Litestar API with isolated, durable SQLite databases."""

import base64
import csv
import io
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from litestar.testing import TestClient

from backend.app import app
from backend.db import database, data_directory
from backend.repositories.products import BRIEF_ATTRIBUTES, get_pending_import, get_products
from backend.services.catalog import ATTRIBUTES, COLUMNS, record_generated_image


def product(sku="VASE-001", **changes):
    return {
        "sku": sku, "product_name": "Ceramic vase", "category": "Decor",
        "color": "Blue", "material": "Ceramic", "price": "$42.00",
        "photo": "https://example.test/vase.jpg", "shot_idea": "Morning kitchen light",
        "notes": "Keep the glaze visible — café lighting", **changes,
    }


def csv_bytes(rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(COLUMNS)
    writer.writerows([row[key] for key in ATTRIBUTES] for row in rows)
    return output.getvalue().encode("utf-8")


class CatalogImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        env = patch.dict(os.environ, {
            "DATABASE_PATH": str(self.directory / "catalog.sqlite3"), "DATA_DIR": "",
            "SLACK_APP_TOKEN": "", "SLACK_BOT_TOKEN": "",  # Never open a real Slack socket in tests.
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = self.enterContext(TestClient(app, raise_server_exceptions=False))

    def preview(self, rows, filename="catalog.csv"):
        response = self.client.post("/api/catalog/preview", files={"data": (filename, csv_bytes(rows), "text/csv")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def confirm(self, preview):
        return self.client.post(f"/api/catalog/imports/{preview['preview_id']}/confirm")

    def apply(self, rows):
        response = self.confirm(self.preview(rows))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def catalog(self):
        response = self.client.get("/api/catalog")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_t1_preview_only_then_explicit_confirmation_persists_all_attributes(self):
        original = product()
        preview = self.preview([original, product("BLANK", shot_idea="")])
        self.assertEqual(self.catalog()["rows"], [])
        self.assertEqual(preview["new_count"], 2)
        self.assertEqual(preview["expected_versions"], {"VASE-001": None, "BLANK": None})
        self.assertTrue(preview["can_confirm"])
        self.assertEqual(preview["ready_to_generate"], 1)
        self.assertEqual(preview["with_shot_idea"], 1)
        self.assertEqual(preview["without_shot_idea"], 1)
        self.assertEqual(preview["generation_summary"]["missing_input"], 1)
        self.assertEqual(preview["rows"][0]["row_number"], 2)
        result = self.confirm(preview)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "applied")
        self.assertFalse(result.json()["can_confirm"])
        rows = {row["sku"]: row for row in self.catalog()["rows"]}
        self.assertEqual({key: rows["VASE-001"][key] for key in ATTRIBUTES}, original)
        self.assertEqual(rows["BLANK"]["shot_idea"], "")
        self.assertTrue(all(row["version"] == 1 for row in rows.values()))

    def test_t2_identical_csv_retains_versions_timestamps_and_images(self):
        original = product()
        self.apply([original])
        image = record_generated_image(original["sku"], original, "first.png", "test-job")
        before = self.catalog()
        preview = self.preview([original], "renamed.csv")
        self.assertEqual(preview["unchanged_count"], 1)
        self.assertEqual(preview["rows"][0]["changes"], {})
        self.assertEqual(preview["rows"][0]["generation_status"], "already_generated")
        self.assertEqual(preview["rows"][0]["images"][0]["generated_from"], original)
        self.assertEqual(preview["rows"][0]["images"][0]["id"], image["id"])
        self.assertEqual(self.confirm(preview).status_code, 200)
        self.assertEqual(self.catalog(), before)
        self.assertEqual(self.catalog()["ready_to_generate"], 1)
        self.assertEqual(self.client.get("/api/catalog/generation-candidates").json()["rows"], [])

    def test_t3_changed_added_and_blank_idea_rows_preserve_existing_images_and_absent_products(self):
        original = product()
        self.apply([original, product("ABSENT")])
        image = record_generated_image(original["sku"], original, "first.png")
        revised = product(shot_idea="Evening mantel", material="Porcelain", price="$45", notes="New campaign")
        preview = self.preview([revised, product("NEW"), product("BLANK", shot_idea="")])
        self.assertEqual(preview["changed_count"], 1)
        self.assertEqual(preview["new_count"], 2)
        changed = preview["rows"][0]
        self.assertEqual(set(changed["changes"]), {"shot_idea", "material", "price", "notes"})
        self.assertEqual(changed["changes"]["material"], {"before": "Ceramic", "after": "Porcelain"})
        # Price and notes are info-only; only the brief counts against the images.
        self.assertEqual(set(changed["generation_changes"]), {"shot_idea", "material"})
        self.assertEqual(changed["generation_status"], "changed_since_generation")
        self.assertEqual(changed["brief_case"], "with_ellie")
        self.assertEqual(self.confirm(preview).status_code, 200)
        rows = {row["sku"]: row for row in self.catalog()["rows"]}
        self.assertEqual(set(rows), {"VASE-001", "ABSENT", "NEW", "BLANK"})
        self.assertEqual(rows["VASE-001"]["version"], 2)
        self.assertEqual(rows["VASE-001"]["images"][0]["id"], image["id"])
        self.assertEqual(rows["VASE-001"]["images"][0]["generated_from"], original)
        again = self.preview([revised])
        self.assertEqual(again["unchanged_count"], 1)
        self.assertEqual(again["rows"][0]["generation_status"], "changed_since_generation")
        candidates = self.client.get("/api/catalog/generation-candidates").json()
        self.assertEqual({row["sku"] for row in candidates["rows"]}, {"VASE-001", "NEW", "ABSENT"})

    def test_missing_and_duplicate_skus_block_entire_import_without_mutation(self):
        self.apply([product()])
        before = self.catalog()
        preview = self.preview([product(notes="Must not save"), product(), product(""), product("NEW")])
        self.assertEqual(preview["invalid_count"], 3)
        self.assertFalse(preview["can_confirm"])
        self.assertEqual(len(preview["errors"]), 3)
        self.assertIn("Duplicate SKU", preview["errors"][0])
        self.assertIn("Missing SKU", preview["errors"][2])
        self.assertEqual(self.confirm(preview).status_code, 400)
        self.assertEqual(self.catalog(), before)
        self.assertEqual(self.client.get(f"/api/catalog/imports/{preview['preview_id']}").json()["status"], "pending")

    def test_missing_inputs_persist_but_never_appear_as_generation_candidates(self):
        rows = [product("IDEA", shot_idea=""), product("PHOTO", photo=""),
                product("URL", photo="not-a-url"), product("NAME", product_name="")]
        self.apply(rows)
        catalog = self.catalog()
        self.assertEqual(catalog["total_rows"], 4)
        self.assertEqual(catalog["ready_to_generate"], 0)
        self.assertEqual(catalog["generation_summary"]["missing_input"], 4)
        self.assertEqual(self.client.get("/api/catalog/generation-candidates").json()["rows"], [])
        record_generated_image("IDEA", rows[0], "blank.png")
        self.assertEqual(self.catalog()["generation_summary"]["missing_input"], 4)

    def test_repeated_confirmation_returns_original_result_even_after_catalog_changes(self):
        preview = self.preview([product()])
        first = self.confirm(preview).json()
        self.apply([product(notes="Later change")])
        before = self.catalog()
        self.assertEqual(self.confirm(preview).json(), first)
        self.assertEqual(self.client.get(f"/api/catalog/imports/{preview['preview_id']}").json(), first)
        self.assertEqual(self.catalog(), before)

    def test_two_existing_product_previews_conflict_without_partial_insert(self):
        self.apply([product()])
        first = self.preview([product(notes="First")])
        second = self.preview([product("NEW"), product(notes="Second")])
        self.assertEqual(self.confirm(first).status_code, 200)
        before = self.catalog()
        response = self.confirm(second)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("refreshed review", response.json()["detail"])
        self.assertEqual(self.catalog(), before)
        self.assertEqual(self.client.get(f"/api/catalog/imports/{second['preview_id']}").json(), second)

    def test_two_previews_for_absent_sku_race_only_one_confirmation_succeeds(self):
        first = self.preview([product(notes="First")])
        second = self.preview([product(notes="Second")])
        self.assertIsNone(first["expected_versions"]["VASE-001"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(self.confirm, [first, second]))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        winner = [first, second][next(index for index, response in enumerate(responses) if response.status_code == 200)]
        self.assertEqual(self.catalog()["rows"][0]["notes"], winner["rows"][0]["notes"])
        self.assertEqual(self.catalog()["rows"][0]["version"], 1)

    def test_pending_review_survives_client_reload_and_keeps_original_differences(self):
        self.apply([product()])
        preview = self.preview([product(notes="Reviewed difference")])
        self.apply([product(notes="Another reviewer changed this")])
        with TestClient(app) as new_client:
            restored = new_client.get(f"/api/catalog/imports/{preview['preview_id']}")
            self.assertEqual(restored.status_code, 200)
            self.assertEqual(restored.json(), preview)
            self.assertEqual(new_client.post(f"/api/catalog/imports/{preview['preview_id']}/confirm").status_code, 409)

    def test_reverting_a_brief_is_a_new_brief_version(self):
        first = product()
        self.apply([first])
        record_generated_image(first["sku"], first, "first.png")
        second = product(shot_idea="Evening mantel")
        self.apply([second])
        record_generated_image(second["sku"], second, "second.png")
        self.apply([first])
        row = self.catalog()["rows"][0]
        self.assertEqual((row["version"], row["brief_version"]), (3, 3))
        self.assertEqual([image["brief_version"] for image in row["images"]], [1, 2])
        self.assertTrue(all(image["outdated"] for image in row["images"]))
        self.assertEqual(row["generation_status"], "changed_since_generation")
        self.assertTrue(row["brief_changed"])
        self.assertEqual(self.client.get("/api/catalog/generation-candidates").json()["total_rows"], 1)

    def test_generation_result_records_submitted_snapshot_after_catalog_changes(self):
        original = product()
        self.apply([original])
        self.apply([product(notes="Changed while generation was pending")])
        image = record_generated_image(original["sku"], original, "late-result.png")
        original["notes"] = "Caller changed this later"
        row = self.catalog()["rows"][0]
        self.assertEqual(row["images"][0]["generated_from"], product())
        self.assertEqual(row["images"][0]["id"], image["id"])
        self.assertEqual(row["generation_status"], "already_generated")  # Notes are info-only.
        self.assertEqual(row["generation_changes"], {})

    def test_only_brief_fields_make_images_outdated(self):
        self.apply([product()])
        record_generated_image("VASE-001", product(), "first.png")
        for key in ATTRIBUTES:
            if key == "sku":
                continue
            with self.subTest(attribute=key):
                revised = product(**{key: product()[key] + ("?changed=1" if key == "photo" else " changed")})
                row = self.preview([revised])["rows"][0]
                self.assertEqual(set(row["changes"]), {key})
                if key in BRIEF_ATTRIBUTES:
                    self.assertEqual((row["generation_status"], row["brief_case"]), ("changed_since_generation", "with_ellie"))
                    self.assertEqual(set(row["generation_changes"]), {key})
                    self.assertTrue(row["images"][0]["outdated"])
                else:
                    self.assertEqual((row["generation_status"], row["brief_case"]), ("already_generated", "info_only"))
                    self.assertEqual(row["generation_changes"], {})
                    self.assertFalse(row["images"][0]["outdated"])
        self.assertEqual(set(ATTRIBUTES) - set(BRIEF_ATTRIBUTES), {"sku", "category", "price", "notes"})

    def test_lowercase_skus_need_correction(self):
        preview = self.preview([product("vase-001"), product("Vase-002"), product("VASE-003")])
        self.assertEqual([row["change_type"] for row in preview["rows"]], ["invalid", "invalid", "new"])
        self.assertIn("lowercase", preview["errors"][0])
        self.assertFalse(preview["can_confirm"])
        self.assertEqual(self.confirm(preview).status_code, 400)
        self.assertEqual(self.catalog()["rows"], [])

    def test_malformed_empty_and_invalid_encoding_uploads_do_not_mutate_catalog(self):
        self.apply([product()])
        before = self.catalog()
        header = ",".join(COLUMNS).encode()
        cases = [
            b"", header + b"\n", b"SKU,Product Name,Photo,Shot Idea\nX,Vase,https://example.test/x.jpg,Idea\n",
            header + b"\nVASE-001,truncated\n",
            csv_bytes([product()]).rstrip() + b",extra\n",
            header + b'\n"unterminated', header + b"\n\xff",
            header + b"\n\x00", header + b",SKU\n",
        ]
        for payload in cases:
            with self.subTest(payload=payload[:70]):
                response = self.client.post("/api/catalog/preview", files={"data": ("bad.csv", payload, "text/csv")})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.catalog(), before)

    def test_failed_applied_marker_rolls_back_all_product_writes_and_can_retry(self):
        self.apply([product()])
        before = self.catalog()
        preview = self.preview([product(notes="Changed"), product("NEW")])
        with patch("backend.services.imports.products.mark_import_applied", side_effect=RuntimeError("Simulated storage failure")):
            response = self.confirm(preview)
            self.assertEqual(response.status_code, 500)
        self.assertEqual(self.catalog(), before)
        with database() as connection:
            pending = get_pending_import(connection, preview["preview_id"])
            self.assertEqual(pending["status"], "pending")
            self.assertIsNone(pending["result"])
        self.assertEqual(self.confirm(preview).status_code, 200)
        self.assertEqual(self.catalog()["total_rows"], 2)

    def test_safe_image_route_serves_known_local_images_and_blocks_escape(self):
        self.apply([product()])
        root = data_directory() / "images"
        root.mkdir()
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==")
        (root / "good.png").write_bytes(png)
        image = record_generated_image("VASE-001", product(), "good.png")
        view_image = self.catalog()["rows"][0]["images"][0]
        self.assertEqual(view_image["image_url"], f"/api/catalog/images/{image['id']}")
        response = self.client.get(view_image["image_url"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, png)
        outside = self.directory / "outside.png"
        outside.write_bytes(b"private data")
        (root / "symlink.png").symlink_to(outside)
        for key in ("../outside.png", str(outside), "symlink.png", "missing.png"):
            image = record_generated_image("VASE-001", product(), key)
            self.assertEqual(self.client.get(f"/api/catalog/images/{image['id']}").status_code, 404)
        self.assertEqual(self.client.get("/api/catalog/images/unknown").status_code, 404)

    def test_missing_import_returns_404_and_reads_do_not_create_products(self):
        self.assertEqual(self.client.get("/api/catalog/imports/unknown").status_code, 404)
        self.assertEqual(self.client.post("/api/catalog/imports/unknown/confirm").status_code, 404)
        self.assertEqual(self.client.get("/api/catalog/generation-candidates").json()["rows"], [])
        with database() as connection:
            self.assertEqual(get_products(connection), [])
            self.assertEqual(connection.execute("SELECT count(*) FROM generated_images").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
