"""Slack review state per posted image. Like products.py, never commits or calls Slack."""

import sqlite3
from datetime import datetime, timezone

STATES = ("awaiting_approval", "approved")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def get_review(connection: sqlite3.Connection, image_id: str) -> dict | None:
    row = connection.execute("SELECT * FROM image_reviews WHERE image_id = ?", (image_id,)).fetchone()
    return dict(row) if row else None


def reviews_for_sku(connection: sqlite3.Connection, sku: str) -> list[dict]:
    rows = connection.execute(
        "SELECT * FROM image_reviews WHERE product_sku = ? ORDER BY created_at, image_id", (sku,)
    )
    return [dict(row) for row in rows]


def thread_for_sku(connection: sqlite3.Connection, sku: str) -> tuple[str, str] | None:
    """The product's existing (channel, thread_ts), so later posts reuse one thread."""
    row = connection.execute(
        "SELECT slack_channel, slack_thread_ts FROM image_reviews "
        "WHERE product_sku = ? AND slack_thread_ts IS NOT NULL LIMIT 1", (sku,)
    ).fetchone()
    return (row[0], row[1]) if row else None


def record_post(connection: sqlite3.Connection, image_id: str, channel: str, thread_ts: str,
                message_ts: str, file_id: str) -> None:
    """A posted candidate waits on Ellie; the review keeps the image's brief version."""
    now = _now()
    connection.execute(
        "INSERT OR IGNORE INTO image_reviews (image_id, product_sku, brief_version, slack_channel, slack_thread_ts, "
        "slack_message_ts, slack_file_id, created_at, updated_at) "
        "SELECT id, product_sku, brief_version, ?, ?, ?, ?, ?, ? FROM generated_images WHERE id = ?",
        (channel, thread_ts, message_ts, file_id, now, now, image_id),
    )


def approve(connection: sqlite3.Connection, image_id: str, user_id: str) -> str:
    """Approve an image made from the product's current brief; one approval per brief, always final.

    Returns approved, already_approved, sibling_approved, outdated or not_awaiting.
    """
    review = get_review(connection, image_id)
    if review is None:
        return "not_awaiting"
    if review["state"] == "approved":
        return "already_approved"
    current = connection.execute(
        "SELECT brief_version FROM products WHERE sku = ?", (review["product_sku"],)).fetchone()[0]
    if review["brief_version"] < current:
        return "outdated"
    taken = connection.execute(
        "SELECT 1 FROM image_reviews WHERE product_sku = ? AND brief_version = ? AND state = 'approved'",
        (review["product_sku"], review["brief_version"]),
    ).fetchone()
    if taken:
        return "sibling_approved"
    now = _now()
    try:  # The unique index is the backstop if two approvals race.
        connection.execute(
            "UPDATE image_reviews SET state = 'approved', approved_by = ?, approved_at = ?, updated_at = ? "
            "WHERE image_id = ? AND state = 'awaiting_approval'", (user_id, now, now, image_id),
        )
    except sqlite3.IntegrityError:
        return "sibling_approved"
    return "approved"
