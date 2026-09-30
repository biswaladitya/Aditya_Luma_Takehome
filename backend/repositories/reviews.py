"""Slack review state per generated image. Like products.py, never commits or calls Slack."""

import sqlite3
from datetime import datetime, timezone

STATES = ("pending_send", "awaiting_approval", "approved")


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


def queue_for_send(connection: sqlite3.Connection, sku: str, image_ids: list[str]) -> None:
    """Create pending_send rows for unsent images and clear errors so failed sends retry."""
    now = _now()
    for image_id in image_ids:
        connection.execute(
            "INSERT OR IGNORE INTO image_reviews (image_id, product_sku, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)", (image_id, sku, now, now),
        )
    connection.execute(
        "UPDATE image_reviews SET send_error = NULL, updated_at = ? "
        "WHERE product_sku = ? AND state = 'pending_send'", (now, sku),
    )


def thread_for_sku(connection: sqlite3.Connection, sku: str) -> tuple[str, str] | None:
    """The product's existing (channel, thread_ts), so later sends reuse one thread."""
    row = connection.execute(
        "SELECT slack_channel, slack_thread_ts FROM image_reviews "
        "WHERE product_sku = ? AND slack_thread_ts IS NOT NULL LIMIT 1", (sku,)
    ).fetchone()
    return (row[0], row[1]) if row else None


def set_thread(connection: sqlite3.Connection, sku: str, channel: str, thread_ts: str) -> None:
    connection.execute(
        "UPDATE image_reviews SET slack_channel = ?, slack_thread_ts = ?, updated_at = ? "
        "WHERE product_sku = ? AND state = 'pending_send'", (channel, thread_ts, _now(), sku),
    )


def mark_sent(connection: sqlite3.Connection, image_id: str, message_ts: str, file_id: str) -> None:
    connection.execute(
        "UPDATE image_reviews SET state = 'awaiting_approval', slack_message_ts = ?, slack_file_id = ?, "
        "send_error = NULL, updated_at = ? WHERE image_id = ? AND state = 'pending_send'",
        (message_ts, file_id, _now(), image_id),
    )


def mark_send_error(connection: sqlite3.Connection, image_ids: list[str], error: str) -> None:
    for image_id in image_ids:
        connection.execute(
            "UPDATE image_reviews SET send_error = ?, updated_at = ? "
            "WHERE image_id = ? AND state = 'pending_send'", (error, _now(), image_id),
        )


def fail_unsent(connection: sqlite3.Connection, reason: str) -> int:
    """Rows left mid-send by a restart get an error so the UI offers a retry."""
    return connection.execute(
        "UPDATE image_reviews SET send_error = ? WHERE state = 'pending_send' AND send_error IS NULL",
        (reason,),
    ).rowcount


def approve(connection: sqlite3.Connection, image_id: str, user_id: str) -> str:
    """Approve an image; one approval per product and the decision is final.

    Returns approved, already_approved, sibling_approved or not_awaiting.
    """
    review = get_review(connection, image_id)
    if review is None or review["state"] == "pending_send":
        return "not_awaiting"
    if review["state"] == "approved":
        return "already_approved"
    taken = connection.execute(
        "SELECT 1 FROM image_reviews WHERE product_sku = ? AND state = 'approved'", (review["product_sku"],)
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
