"""Database contract checks; every database lives in a temporary directory."""

import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.db import PROJECT_ROOT, SCHEMA_VERSION, data_directory, database, database_path
from backend.repositories.products import (
    BRIEF_ATTRIBUTES,
    PRODUCT_ATTRIBUTES,
    create_pending_import,
    get_pending_import,
    get_products,
    mark_import_applied,
    save_generated_image,
    save_product,
)


def product(sku="VASE-001", **changes):
    return {
        "sku": sku,
        "product_name": "Ceramic vase",
        "category": "Decor",
        "color": "Blue",
        "material": "Ceramic",
        "price": "42.00",
        "photo": "https://example.test/vase.jpg",
        "shot_idea": "Morning light on a kitchen counter",
        "notes": "Keep the glaze visible — café lighting",
        **changes,
    }


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "catalog.sqlite3"
        env = patch.dict(os.environ, {"DATABASE_PATH": str(self.path), "DATA_DIR": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_paths_are_absolute_and_configuration_precedence_is_explicit(self):
        self.assertEqual(database_path(), self.path.resolve())
        self.assertEqual(data_directory(), self.path.parent.resolve())
        with patch.dict(os.environ, {"DATA_DIR": str(self.directory / "assets")}):
            self.assertEqual(database_path(), self.path.resolve())
            self.assertEqual(data_directory(), (self.directory / "assets").resolve())
        with patch.dict(os.environ, {"DATABASE_PATH": "", "DATA_DIR": str(self.directory)}):
            self.assertEqual(database_path(), self.path.resolve())
        # Path helpers do not open the real default catalog.
        with patch.dict(os.environ, {"DATABASE_PATH": "", "DATA_DIR": ""}):
            self.assertEqual(database_path(), PROJECT_ROOT / "runtime" / "catalog.sqlite3")
        with patch.dict(os.environ, {"DATABASE_PATH": "relative/catalog.sqlite3"}):
            self.assertEqual(database_path(), PROJECT_ROOT / "relative" / "catalog.sqlite3")

    def test_initializes_nested_directory_schema_and_closes_connection(self):
        path = self.directory / "nested" / "catalog.sqlite3"
        with patch.dict(os.environ, {"DATABASE_PATH": str(path)}):
            with database() as connection:
                self.assertTrue(connection.in_transaction)
                self.assertEqual(connection.row_factory, sqlite3.Row)
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        self.assertTrue(path.is_file())

    def test_all_attributes_and_blank_idea_persist_across_connections(self):
        attributes = product(shot_idea="")
        with database() as connection:
            saved = save_product(connection, attributes)
        with database() as connection:
            self.assertEqual(get_products(connection), [saved])
        self.assertEqual({key: saved[key] for key in PRODUCT_ATTRIBUTES}, attributes)
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["images"], [])
        self.assertEqual(saved["created_at"], saved["updated_at"])

    def test_each_attribute_change_bumps_version_only_once(self):
        attributes = product()
        with database() as connection:
            original = save_product(connection, attributes)
            self.assertEqual(save_product(connection, dict(attributes)), original)
            for version, key in enumerate(PRODUCT_ATTRIBUTES[1:], start=2):
                attributes[key] += " changed"
                updated = save_product(connection, attributes)
                self.assertEqual(updated["version"], version)
                self.assertEqual(updated["created_at"], original["created_at"])
                self.assertEqual(save_product(connection, attributes), updated)

    def test_brief_version_bumps_only_when_a_brief_field_changes(self):
        attributes = product()
        with database() as connection:
            self.assertEqual(save_product(connection, attributes)["brief_version"], 1)
            for key in PRODUCT_ATTRIBUTES[1:]:
                attributes[key] += " changed"
                saved = save_product(connection, attributes)
                expected = 1 + sum(k in BRIEF_ATTRIBUTES for k in PRODUCT_ATTRIBUTES[1:PRODUCT_ATTRIBUTES.index(key) + 1])
                self.assertEqual(saved["brief_version"], expected, key)
            self.assertEqual((saved["version"], saved["brief_version"]), (9, 6))
            image = save_generated_image(connection, "VASE-001", attributes, "one.png")
            self.assertEqual(image["brief_version"], 6)

    def test_images_keep_submitted_snapshots_and_survive_catalog_changes(self):
        submitted = product()
        with database() as connection:
            save_product(connection, product(shot_idea="New idea while generation runs"))
            first = save_generated_image(connection, submitted["sku"], submitted, "vase/one.png", "luma-1")
            second = save_generated_image(connection, submitted["sku"], submitted, "vase/two.png")
            submitted["shot_idea"] = "Mutated caller data"
            updated = save_product(connection, product(shot_idea="Another idea"))
            self.assertEqual(updated["images"], [first, second])
            self.assertEqual(updated["version"], 2)
            self.assertEqual(first["generated_from"], product())
            self.assertEqual(first["luma_generation_id"], "luma-1")
            self.assertIsNone(second["luma_generation_id"])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE generated_images SET generated_from = ? WHERE id = ?",
                    ('{"shot_idea":"overwrite"}', first["id"]),
                )
        with database() as connection:
            self.assertEqual(get_products(connection)[0]["images"], [first, second])

    def test_foreign_keys_reject_orphan_images_and_product_deletion(self):
        with database() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                save_generated_image(connection, "absent", product(), "one.png")
            save_product(connection, product())
            save_generated_image(connection, "VASE-001", product(), "one.png")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM products WHERE sku = ?", ("VASE-001",))

    def test_filtered_reads_parameterize_skus_and_keep_absent_products(self):
        hostile_sku = "quote'); DROP TABLE products; --"
        with database() as connection:
            for sku in ("B", "A", hostile_sku):
                save_product(connection, product(sku))
            self.assertEqual(get_products(connection, []), [])
            self.assertEqual(get_products(connection, ["missing"]), [])
            selected = get_products(connection, [hostile_sku, "A", hostile_sku])
            self.assertEqual([row["sku"] for row in selected], ["A", hostile_sku])
            save_product(connection, product("A", notes="Changed only A"))
            self.assertEqual(len(get_products(connection)), 3)

    def test_preview_is_durable_without_catalog_writes(self):
        rows = [{**product(), "issues": [], "row_number": 2}]
        with database() as connection:
            preview = create_pending_import(connection, "customer.csv", rows, {"VASE-001": None})
            self.assertEqual(get_products(connection), [])
            self.assertEqual(preview["status"], "pending")
            self.assertIsNone(preview["result"])
            self.assertIsNone(preview["applied_at"])
        rows[0]["notes"] = "Caller mutation"
        with database() as connection:
            self.assertEqual(get_pending_import(connection, preview["id"]), preview)
            self.assertEqual(preview["expected_versions"], {"VASE-001": None})
            self.assertIsNone(get_pending_import(connection, "unknown"))

    def test_catalog_and_applied_result_commit_together_and_reapplication_is_noop(self):
        with database() as connection:
            preview = create_pending_import(connection, "x.csv", [product()], {"VASE-001": None})
        with database() as connection:
            saved = save_product(connection, product())
            mark_import_applied(connection, preview["id"], {"products": [saved]})
        with database() as connection:
            applied = get_pending_import(connection, preview["id"])
            self.assertEqual(applied["status"], "applied")
            self.assertIsNotNone(applied["applied_at"])
            self.assertEqual(applied["result"], {"products": get_products(connection)})
            mark_import_applied(connection, preview["id"], {"different": "result"})
            self.assertEqual(get_pending_import(connection, preview["id"]), applied)
            with self.assertRaises(KeyError):
                mark_import_applied(connection, "unknown", {})

    def test_failure_rolls_back_product_images_and_applied_status_and_closes(self):
        with database() as connection:
            saved = save_product(connection, product())
            preview = create_pending_import(connection, "x.csv", [product(notes="new")], {"VASE-001": 1})
        with self.assertRaisesRegex(RuntimeError, "simulated failure"):
            with database() as connection:
                save_product(connection, product(notes="new"))
                save_product(connection, product("NEW"))
                save_generated_image(connection, "VASE-001", product(), "one.png")
                mark_import_applied(connection, preview["id"], {"ok": True})
                raise RuntimeError("simulated failure")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with database() as connection:
            self.assertEqual(get_products(connection), [saved])
            self.assertEqual(get_pending_import(connection, preview["id"]), preview)

    def test_initial_schema_and_preview_rollback_can_be_retried(self):
        with self.assertRaises(RuntimeError):
            with database() as connection:
                create_pending_import(connection, "x.csv", [], {})
                raise RuntimeError("first transaction failed")
        with database() as connection:
            self.assertEqual(get_products(connection), [])
            self.assertEqual(connection.execute("SELECT count(*) FROM pending_imports").fetchone()[0], 0)

    def test_immediate_transaction_prevents_concurrent_writer_before_validation(self):
        with database() as connection:
            save_product(connection, product())
        with database() as connection:
            self.assertEqual(get_products(connection)[0]["version"], 1)
            other = sqlite3.connect(self.path, timeout=0, isolation_level=None)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    other.execute("BEGIN IMMEDIATE")
            finally:
                other.close()

    def test_waiting_transaction_reads_committed_version(self):
        started = threading.Event()
        results = []
        errors = []

        def read_after_lock():
            started.set()
            try:
                with database() as connection:
                    results.append(get_products(connection)[0]["version"])
            except Exception as error:
                errors.append(error)

        with database() as connection:
            save_product(connection, product())
            save_product(connection, product(notes="changed"))
            worker = threading.Thread(target=read_after_lock)
            worker.start()
            self.assertTrue(started.wait(timeout=2))
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [2])

    def test_refuses_unknown_schema_version_without_modifying_it(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(RuntimeError, "Unsupported catalog schema version"):
            with database():
                self.fail("Unsupported schema should not be opened")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)

    def test_refuses_unversioned_existing_database_without_altering_it(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE legacy (value TEXT)")
            connection.execute("INSERT INTO legacy VALUES ('preserved')")
        with self.assertRaisesRegex(RuntimeError, "nonempty, unversioned"):
            with database():
                self.fail("Legacy database should not be initialized")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT value FROM legacy").fetchone()[0], "preserved")
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
