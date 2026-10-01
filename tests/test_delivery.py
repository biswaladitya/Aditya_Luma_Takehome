"""Drive delivery: Google sign-in token per request, My Drive root, overwrite in place, visible failures. Drive is faked."""

import io
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from litestar.testing import TestClient

from backend import drive
from backend.app import app
from backend.db import data_directory, database
from backend.repositories import deliveries, products, reviews
from backend.services.delivery import delivery_filename, recover_unfinished
from tests.test_catalog_imports import csv_bytes, product

TOKEN = "ya29.browser-token"


class FakeDrive:
    """In-memory My Drive root, with switches to inject failures."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.tokens, self.uploads, self.overwrites = [], [], []
        self.token_expired = False
        self.fail_uploads = 0
        self.crash_after_upload = 0  # Store the file, then fail, as a process dying before the record would.
        self._lock = threading.Lock()  # Two delivery workers may upload at once.

    def client(self, access_token):
        self.tokens.append(access_token)
        return self

    def check_access(self):
        if self.token_expired:
            raise drive.DriveError(drive.SIGN_IN_AGAIN, 401)

    def add_file(self, name, content=b"old"):
        with self._lock:
            file_id = f"F{len(self.files) + 1}"
            self.files[file_id] = {"name": name, "content": content, "mime": "image/png"}
        return file_id

    def find_root_files(self, name):
        return [fid for fid, f in self.files.items() if f["name"] == name]

    def upload_file(self, path, name, mime_type):
        with self._lock:
            fail, self.fail_uploads = bool(self.fail_uploads), max(self.fail_uploads - 1, 0)
        if fail:
            raise drive.DriveError("Google Drive returned 503: backend error")
        file_id = self.add_file(name, path.read_bytes())
        self.files[file_id]["mime"] = mime_type
        self.uploads.append(file_id)
        if self.crash_after_upload:
            self.crash_after_upload -= 1
            raise drive.DriveError("connection reset after upload")
        return file_id, f"https://drive.google.com/file/d/{file_id}/view"

    def overwrite_file(self, file_id, path, mime_type):
        self.files[file_id].update(content=path.read_bytes(), mime=mime_type)
        self.overwrites.append(file_id)
        return file_id, f"https://drive.google.com/file/d/{file_id}/view"


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {
            "DATABASE_PATH": str(Path(temporary.name) / "catalog.sqlite3"), "DATA_DIR": "",
            "SLACK_APP_TOKEN": "", "SLACK_BOT_TOKEN": "",
            "GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com",
        })
        env.start()
        self.addCleanup(env.stop)
        self.drive = FakeDrive()
        patcher = patch("backend.drive.client", side_effect=self.drive.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.enterContext(TestClient(app, raise_server_exceptions=False))
        rows = [product("VASE-042"), product("LAMP-7"), product("MUG-1")]
        preview = self.client.post("/api/catalog/preview", files={"data": ("c.csv", csv_bytes(rows), "text/csv")}).json()
        self.client.post(f"/api/catalog/imports/{preview['preview_id']}/confirm")
        self.approved = {sku: self.approve(sku, content=f"{sku} pixels".encode()) for sku in ("VASE-042", "LAMP-7")}
        self.add_image("MUG-1", "mug.png")  # Generated but never approved.

    def add_image(self, sku, key, content=b"pixels"):
        path = data_directory() / "images" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        with database() as connection:
            snapshot = {k: v for k, v in products.get_products(connection, [sku])[0].items()
                        if k in products.PRODUCT_ATTRIBUTES}
            return products.save_generated_image(connection, sku, snapshot, key)["id"]

    def approve(self, sku, content=b"pixels"):
        image_id = self.add_image(sku, f"{sku.lower()}.png", content)
        with database() as connection:
            reviews.record_post(connection, image_id, "C1", "100.0", "100.1", "F0")
            self.assertEqual(reviews.approve(connection, image_id, "UELLIE"), "approved")
        return image_id

    def row(self, sku="VASE-042"):
        return next(r for r in self.client.get("/api/catalog").json()["rows"] if r["sku"] == sku)

    def until(self, condition):
        for _ in range(50):
            if condition():
                return
            time.sleep(0.1)
        self.fail("condition not reached")

    def deliver(self, skus=("VASE-042",)):
        response = self.client.post("/api/deliveries", json={"skus": list(skus), "access_token": TOKEN})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def settled(self, sku="VASE-042"):
        row = self.row(sku)
        return row["delivery_status"] == "delivered" or bool(row["delivery_error"])

    def test_approval_alone_writes_nothing(self):
        row = self.row()
        self.assertEqual((row["review_status"], row["delivery_status"], row["can_deliver"]), ("approved", None, True))
        self.assertFalse(self.row("MUG-1")["can_deliver"])
        self.assertEqual((self.drive.files, self.drive.tokens), ({}, []))

    def test_only_approved_images_are_delivered(self):
        result = self.deliver(["MUG-1", "NOPE"])
        self.assertEqual(result["queued"], [])
        self.assertEqual({s["sku"]: s["reason"] for s in result["skipped"]},
                         {"MUG-1": "No approved image.", "NOPE": "Unknown SKU."})
        self.assertEqual(self.drive.files, {})

    def test_writes_named_file_to_my_drive_root_with_the_request_token(self):
        self.assertEqual(self.deliver()["queued"], ["VASE-042"])
        self.until(self.settled)
        row = self.row()
        self.assertEqual(row["delivery_status"], "delivered", row["delivery_error"])
        (file_id, stored), = self.drive.files.items()
        self.assertEqual((stored["name"], stored["mime"], stored["content"]), ("VASE-042_styled_v1.png", "image/png", b"VASE-042 pixels"))
        self.assertEqual(row["drive_url"], f"https://drive.google.com/file/d/{file_id}/view")
        self.assertEqual(set(self.drive.tokens), {TOKEN})
        self.assertFalse(row["can_deliver"])
        self.assertEqual(row["review_status"], "approved")

    def test_access_token_is_never_stored(self):
        self.deliver()
        self.until(self.settled)
        with database() as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn(TOKEN, dump)

    def test_filename_uses_sku_image_version_and_real_extension(self):
        self.assertEqual(delivery_filename("VASE-042", 3, ".PNG"), "VASE-042_styled_v3.png")
        self.assertEqual(delivery_filename("VASE-042", 12, "jpg"), "VASE-042_styled_v12.jpg")

    def test_second_click_does_nothing(self):
        self.deliver()
        self.until(self.settled)
        result = self.deliver()
        self.assertEqual(result["skipped"], [{"sku": "VASE-042", "reason": "Already saved to Drive."}])
        self.assertEqual((len(self.drive.files), len(self.drive.uploads), self.drive.overwrites), (1, 1, []))

    def test_retry_after_crash_overwrites_the_same_file(self):
        self.drive.crash_after_upload = 1
        self.deliver()
        self.until(self.settled)
        self.assertEqual(self.row()["delivery_status"], "pending")
        (file_id,) = self.drive.files
        self.deliver()
        self.until(lambda: self.row()["delivery_status"] == "delivered")
        self.assertEqual(list(self.drive.files), [file_id])
        self.assertEqual(self.drive.overwrites, [file_id])
        self.assertIn(file_id, self.row()["drive_url"])

    def test_existing_same_named_file_gets_the_new_content(self):
        file_id = self.drive.add_file("VASE-042_styled_v1.png", b"old")
        self.deliver()
        self.until(self.settled)
        self.assertEqual(self.row()["delivery_status"], "delivered")
        self.assertEqual((list(self.drive.files), self.drive.uploads), ([file_id], []))
        self.assertEqual(self.drive.files[file_id]["content"], b"VASE-042 pixels")

    def test_two_same_named_files_are_an_error_and_nothing_is_written(self):
        for _ in range(2):
            self.drive.add_file("VASE-042_styled_v1.png", b"old")
        self.deliver()
        self.until(self.settled)
        row = self.row()
        self.assertEqual(row["delivery_status"], "pending")
        self.assertIn("2 files named VASE-042_styled_v1.png", row["delivery_error"])
        self.assertTrue(row["can_deliver"])
        self.assertEqual((self.drive.uploads, self.drive.overwrites), ([], []))
        self.assertEqual({f["content"] for f in self.drive.files.values()}, {b"old"})

    def test_missing_token_asks_for_sign_in(self):
        for body in ({"skus": ["VASE-042"]}, {"skus": ["VASE-042"], "access_token": "  "}):
            response = self.client.post("/api/deliveries", json=body)
            self.assertEqual(response.status_code, 401)
            self.assertIn("Sign in with Google", response.json()["detail"])
        self.assertIsNone(self.row()["delivery_status"])

    def test_expired_token_refuses_the_batch_before_queuing(self):
        self.drive.token_expired = True
        response = self.client.post("/api/deliveries", json={"skus": ["VASE-042", "LAMP-7"], "access_token": TOKEN})
        self.assertEqual(response.status_code, 401)
        self.assertIn("Save to Drive again", response.json()["detail"])
        self.assertIsNone(self.row()["delivery_status"])
        self.assertEqual(self.drive.files, {})

    def test_nothing_eligible_makes_no_google_call(self):
        self.deliver(["MUG-1"])
        self.assertEqual(self.drive.tokens, [])

    def test_config_exposes_only_the_public_client_id(self):
        self.assertEqual(self.client.get("/api/drive/config").json(), {"client_id": "cid.apps.googleusercontent.com"})
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": ""}):
            self.assertEqual(self.client.get("/api/drive/config").json(), {"client_id": None})

    def test_failure_is_visible_retryable_and_isolated(self):
        self.drive.fail_uploads = 1  # Only the first upload fails; workers may run in either order.
        self.deliver(["VASE-042", "LAMP-7"])
        self.until(lambda: self.settled("VASE-042") and self.settled("LAMP-7"))
        rows = {sku: self.row(sku) for sku in ("VASE-042", "LAMP-7")}
        failed = [sku for sku, row in rows.items() if row["delivery_error"]]
        self.assertEqual(len(failed), 1)
        self.assertIn("503", rows[failed[0]]["delivery_error"])
        self.assertEqual(rows[failed[0]]["review_status"], "approved")  # A Drive failure never undoes a decision.
        self.assertTrue(rows[failed[0]]["can_deliver"])
        self.assertEqual({row["delivery_status"] for row in rows.values()}, {"pending", "delivered"})
        self.assertEqual(self.deliver(failed)["queued"], failed)
        self.until(lambda: self.row(failed[0])["delivery_status"] == "delivered")
        self.assertIsNone(self.row(failed[0])["delivery_error"])

    def test_missing_local_file_is_an_error_on_that_row_only(self):
        (data_directory() / "images" / "vase-042.png").unlink()
        self.deliver(["VASE-042", "LAMP-7"])
        self.until(lambda: self.settled("VASE-042") and self.settled("LAMP-7"))
        self.assertIn("missing", self.row()["delivery_error"])
        self.assertEqual(self.row("LAMP-7")["delivery_status"], "delivered")

    def test_startup_recovery_marks_stale_pending_rows(self):
        with database() as connection:
            deliveries.queue(connection, "VASE-042", self.approved["VASE-042"], "VASE-042_styled_01.png")
        self.assertFalse(self.row()["can_deliver"])  # In flight: no retry offered yet.
        recover_unfinished()
        row = self.row()
        self.assertIn("restart", row["delivery_error"])
        self.assertTrue(row["can_deliver"])

    def test_delivered_requires_drive_fields_and_state_only_moves_forward(self):
        image_id = self.approved["VASE-042"]
        with database() as connection:
            deliveries.queue(connection, "VASE-042", image_id, "VASE-042_styled_01.png")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE drive_deliveries SET state = 'delivered' WHERE image_id = ?", (image_id,))
            deliveries.mark_delivered(connection, image_id, "F1", "https://drive.google.com/file/d/F1/view")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE drive_deliveries SET state = 'pending' WHERE image_id = ?", (image_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE drive_deliveries SET state = 'lost' WHERE image_id = ?", (image_id,))


class DriveClientTests(unittest.TestCase):
    """The real client's request shapes, with the network replaced."""

    def setUp(self):
        self.client = drive.DriveClient(TOKEN)

    def test_query_literals_escape_quotes_and_backslashes(self):
        self.assertEqual(drive.quote("quinn's paper\\essay"), "'quinn\\'s paper\\\\essay'")

    def test_multipart_body_puts_metadata_before_media(self):
        body, content_type = drive.multipart_body({"name": "A_styled_01.png"}, b"\x89PNG", "image/png")
        boundary = content_type.split("boundary=")[1]
        self.assertTrue(content_type.startswith("multipart/related; "))
        self.assertEqual(body, (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f'{{"name": "A_styled_01.png"}}\r\n--{boundary}\r\nContent-Type: image/png\r\n\r\n'
        ).encode() + b"\x89PNG" + f"\r\n--{boundary}--\r\n".encode())

    def test_root_lookup_queries_my_drive_root(self):
        with patch.object(self.client, "_request", return_value={"files": [{"id": "F1"}]}) as request:
            self.assertEqual(self.client.find_root_files("A_styled_01.png"), ["F1"])
        method, url, params = request.call_args.args
        self.assertEqual((method, url), ("GET", drive.FILES_URL))
        self.assertIn("name = 'A_styled_01.png' and 'root' in parents", params["q"])
        self.assertIn("trashed = false", params["q"])

    def test_upload_and_overwrite_use_multipart_upload_endpoint(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as handle:
            path = Path(handle.name)
            with patch.object(self.client, "_request", return_value={"id": "F1", "webViewLink": "L"}) as request:
                self.assertEqual(self.client.upload_file(path, "A_styled_01.png", "image/png"), ("F1", "L"))
                self.assertEqual(self.client.overwrite_file("F1", path, "image/png"), ("F1", "L"))
        (create, update) = request.call_args_list
        params = {"uploadType": "multipart", "fields": "id,webViewLink"}
        self.assertEqual(create.args[:3], ("POST", drive.UPLOAD_URL, params))
        self.assertIn(b'"name": "A_styled_01.png"', create.args[3])
        self.assertNotIn(b"parents", create.args[3])  # No parent: the top of My Drive.
        self.assertEqual(update.args[:3], ("PATCH", f"{drive.UPLOAD_URL}/F1", params))
        self.assertNotIn(b"parents", update.args[3])

    def test_bearer_token_is_sent_and_never_in_errors(self):
        seen = {}

        def reject(request, timeout):
            seen["auth"] = request.get_header("Authorization")
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))

        with patch("urllib.request.urlopen", side_effect=reject):
            with self.assertRaises(drive.DriveError) as caught:
                self.client.check_access()
        self.assertEqual(seen["auth"], f"Bearer {TOKEN}")
        self.assertEqual((caught.exception.status, str(caught.exception)), (401, drive.SIGN_IN_AGAIN))
        self.assertNotIn(TOKEN, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
