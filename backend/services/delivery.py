"""Explicit Drive delivery: write each product's approved image to the signed-in user's My Drive."""

import logging
import mimetypes
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from litestar.exceptions import HTTPException

from backend import drive
from backend.db import data_directory, database
from backend.repositories import deliveries, products
from backend.services.catalog import product_view

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="drive")
_delivering: set[str] = set()
_delivering_lock = threading.Lock()


def _claim(sku: str) -> bool:
    with _delivering_lock:
        if sku in _delivering:
            return False
        _delivering.add(sku)
        return True


def delivery_filename(sku: str, extension: str) -> str:
    """`<SKU>_styled_01.<ext>`; ASSUMPTIONS.md: SKUs are case-unique and filename-safe.

    `_01` leaves room for the customer's 2-3 approved images later.
    """
    return f"{sku.strip().upper()}_styled_01.{extension.lower().lstrip('.')}"


def _skip_reason(row: dict | None) -> str | None:
    if row is None:
        return "Unknown SKU."
    if row["review_status"] != "approved":
        return "No approved image."
    if row["delivery_status"] == "delivered":
        return "Already saved to Drive."
    if not row["can_deliver"]:
        return "Already saving to Drive."
    return None


def start_delivery(skus: list[str], access_token: str) -> dict:
    """Queue approved images for Drive. Only this explicit request writes anything; approval never does.

    The access token comes from the user's Google sign-in in the browser. It is passed to the
    workers in memory for this batch only and never stored.
    """
    if not skus:
        raise HTTPException(status_code=400, detail="Select at least one product.")
    if not access_token.strip():
        raise HTTPException(status_code=401, detail="Sign in with Google to save to Drive.")
    skus = list(dict.fromkeys(skus))
    with database() as connection:
        found = {row["sku"]: product_view(row) for row in products.get_products(connection, skus)}
    if not any(_skip_reason(found.get(sku)) is None for sku in skus):
        return {"queued": [], "skipped": [{"sku": sku, "reason": _skip_reason(found.get(sku))} for sku in skus]}
    client = drive.client(access_token)
    try:  # Fail fast on an expired token, outside any transaction, before queuing anything.
        client.check_access()
    except drive.DriveError as exc:
        raise HTTPException(status_code=401 if exc.status == 401 else 503, detail=str(exc)) from exc
    queued, skipped = [], []
    with database() as connection:  # Re-read: state may have moved during the access check.
        found = {row["sku"]: product_view(row) for row in products.get_products(connection, skus)}
        for sku in skus:
            row = found.get(sku)
            if reason := _skip_reason(row):
                skipped.append({"sku": sku, "reason": reason})
            elif not _claim(sku):
                skipped.append({"sku": sku, "reason": "Already saving to Drive."})
            else:
                image = next(image for image in row["images"] if image["id"] == row["approved_image_id"])
                deliveries.queue(connection, sku, image["id"],
                                 delivery_filename(sku, Path(image["storage_key"]).suffix or ".jpg"))
                queued.append(sku)
    for sku in queued:  # Submit only after the pending rows are committed.
        _executor.submit(_deliver, sku, client)
    return {"queued": queued, "skipped": skipped}


def _deliver(sku: str, client: drive.DriveClient) -> None:
    """Create or overwrite the named file at the top of My Drive, recording each result as it lands."""
    try:
        with database() as connection:
            pending = [d for d in deliveries.deliveries_for_sku(connection, sku) if d["state"] == "pending"]
            images = {image["id"]: image for image in products.get_products(connection, [sku])[0]["images"]}
        for delivery in pending:
            try:
                path = data_directory() / "images" / images[delivery["image_id"]]["storage_key"]
                if not path.is_file():
                    raise drive.DriveError("The approved image file is missing from local storage.")
                mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                existing = client.find_root_files(delivery["filename"])
                if len(existing) > 1:
                    raise drive.DriveError(
                        f"{len(existing)} files named {delivery['filename']} are already in your Drive. "
                        "Remove the extra copies, then retry.")
                if existing:  # A retry after a crash or a database reset replaces rather than adds a copy.
                    file_id, url = client.overwrite_file(existing[0], Path(path), mime_type)
                else:
                    file_id, url = client.upload_file(Path(path), delivery["filename"], mime_type)
                with database() as connection:
                    deliveries.mark_delivered(connection, delivery["image_id"], file_id, url)
            except Exception as exc:
                with database() as connection:
                    deliveries.mark_error(connection, [delivery["image_id"]], str(exc)[:500])
    except Exception:
        logger.exception("Drive delivery crashed for %s", sku)
        with database() as connection:
            deliveries.fail_unfinished(connection, "Saving to Drive failed unexpectedly. Try again.", sku)
    finally:
        with _delivering_lock:
            _delivering.discard(sku)


def recover_unfinished() -> None:
    with database() as connection:
        deliveries.fail_unfinished(connection, "Interrupted by a server restart. Save to Drive again.")
