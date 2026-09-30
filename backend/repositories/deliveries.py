"""Drive delivery state per approved image. Like reviews.py, never commits or calls Google."""

import sqlite3
from datetime import datetime, timezone

STATES = ("pending", "delivered")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def get_delivery(connection: sqlite3.Connection, image_id: str) -> dict | None:
    row = connection.execute("SELECT * FROM drive_deliveries WHERE image_id = ?", (image_id,)).fetchone()
    return dict(row) if row else None


def deliveries_for_sku(connection: sqlite3.Connection, sku: str) -> list[dict]:
    rows = connection.execute(
        "SELECT * FROM drive_deliveries WHERE product_sku = ? ORDER BY created_at, image_id", (sku,)
    )
    return [dict(row) for row in rows]


def queue(connection: sqlite3.Connection, sku: str, image_id: str, filename: str) -> None:
    """Create a pending row for an approved image and clear its error so a failed write retries.

    The primary key makes repeat clicks idempotent; delivered rows are left untouched.
    """
    now = _now()
    connection.execute(
        "INSERT OR IGNORE INTO drive_deliveries (image_id, product_sku, filename, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)", (image_id, sku, filename, now, now),
    )
    connection.execute(
        "UPDATE drive_deliveries SET error = NULL, updated_at = ? WHERE image_id = ? AND state = 'pending'",
        (now, image_id),
    )


def mark_delivered(connection: sqlite3.Connection, image_id: str, file_id: str, url: str) -> None:
    now = _now()
    connection.execute(
        "UPDATE drive_deliveries SET state = 'delivered', drive_file_id = ?, drive_url = ?, "
        "delivered_at = ?, error = NULL, updated_at = ? WHERE image_id = ? AND state = 'pending'",
        (file_id, url, now, now, image_id),
    )


def mark_error(connection: sqlite3.Connection, image_ids: list[str], error: str) -> None:
    for image_id in image_ids:
        connection.execute(
            "UPDATE drive_deliveries SET error = ?, updated_at = ? "
            "WHERE image_id = ? AND state = 'pending'", (error, _now(), image_id),
        )


def fail_unfinished(connection: sqlite3.Connection, reason: str, sku: str | None = None) -> int:
    """Rows left mid-write (by a restart, or one SKU's crash) get an error so the UI offers a retry."""
    query = "UPDATE drive_deliveries SET error = ?, updated_at = ? WHERE state = 'pending' AND error IS NULL"
    params: list = [reason, _now()]
    if sku is not None:
        query += " AND product_sku = ?"
        params.append(sku)
    return connection.execute(query, params).rowcount
