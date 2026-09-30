"""Explicit Slack review: send candidates to a product thread and record Ellie's approval."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from litestar.exceptions import HTTPException

from backend import slack
from backend.db import data_directory, database
from backend.repositories import products, reviews
from backend.services.catalog import product_view

logger = logging.getLogger(__name__)
APPROVE_ACTION = "approve_image"

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack")
_sending: set[str] = set()
_sending_lock = threading.Lock()


def _claim(sku: str) -> bool:
    with _sending_lock:
        if sku in _sending:
            return False
        _sending.add(sku)
        return True


def review_blocks(product: dict, version: int, image_id: str, footer: str | None = None) -> list:
    """Message blocks for one candidate; the image itself is the message posted just above.

    Without a footer they carry the Approve button.
    """
    name = product["product_name"] or product["sku"]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{name}* · `{product['sku']}` · candidate v{version} (image above)\n_{product['shot_idea']}_"}},
    ]
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    else:
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "style": "primary", "action_id": APPROVE_ACTION, "value": image_id,
            "text": {"type": "plain_text", "text": "Approve"},
            "confirm": {
                "title": {"type": "plain_text", "text": "Approve this image?"},
                "text": {"type": "mrkdwn", "text": "Only one image can be approved for this product, and it cannot be undone."},
                "confirm": {"type": "plain_text", "text": "Approve"},
                "deny": {"type": "plain_text", "text": "Cancel"},
            },
        }]})
    return blocks


def start_review(skus: list[str]) -> dict:
    """Queue eligible products for posting to Slack. Only this explicit request sends anything."""
    if not skus:
        raise HTTPException(status_code=400, detail="Select at least one product.")
    try:
        slack.require("SLACK_BOT_TOKEN"), slack.require("SLACK_CHANNEL_ID")
    except slack.SlackError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    queued, skipped = [], []
    with database() as connection:
        found = {row["sku"]: product_view(row) for row in products.get_products(connection, skus)}
        for sku in dict.fromkeys(skus):
            row = found.get(sku)
            if row is None:
                skipped.append({"sku": sku, "reason": "Unknown SKU."})
            elif row["review_status"] == "approved":
                skipped.append({"sku": sku, "reason": "An image is already approved."})
            elif not row["can_send"]:
                skipped.append({"sku": sku, "reason": "No generated images are ready to send."})
            elif not _claim(sku):
                skipped.append({"sku": sku, "reason": "Already sending to Slack."})
            else:
                reviews.queue_for_send(connection, sku, row["reviewable_image_ids"])
                queued.append(sku)
    for sku in queued:  # Submit only after the pending rows are committed.
        _executor.submit(_deliver, sku)
    return {"queued": queued, "skipped": skipped}


def _deliver(sku: str) -> None:
    """Post the parent message and each pending candidate, recording each step as it lands."""
    try:
        with database() as connection:
            product = products.get_products(connection, [sku])[0]
            pending = [r for r in reviews.reviews_for_sku(connection, sku) if r["state"] == "pending_send"]
            thread = reviews.thread_for_sku(connection, sku)
        if not pending:
            return
        ids = [r["image_id"] for r in pending]
        images = {image["id"]: image for image in product["images"]}
        name = product["product_name"] or sku
        try:
            client = slack.client()
            channel = thread[0] if thread else slack.require("SLACK_CHANNEL_ID")
            if thread:
                thread_ts = thread[1]
            else:
                thread_ts = client.post_message(
                    channel, f"{name} ({sku}) — shot idea: {product['shot_idea']}. Review the candidates below.")
                with database() as connection:
                    reviews.set_thread(connection, sku, channel, thread_ts)
        except Exception as exc:  # Nothing was posted for these images; they stay pending and retryable.
            with database() as connection:
                reviews.mark_send_error(connection, ids, str(exc)[:500])
            return
        for image_id in ids:
            image = images[image_id]
            try:
                path = data_directory() / "images" / image["storage_key"]
                if not path.is_file():
                    raise slack.SlackError("The generated image file is missing.")
                file_id = client.upload_image(Path(path), f"{name} v{image['version']}", channel, thread_ts)
                message_ts = client.post_message(
                    channel, f"{name} candidate v{image['version']}",
                    blocks=review_blocks(product, image["version"], image_id), thread_ts=thread_ts)
                with database() as connection:
                    reviews.mark_sent(connection, image_id, message_ts, file_id)
            except Exception as exc:
                with database() as connection:
                    reviews.mark_send_error(connection, [image_id], str(exc)[:500])
    except Exception:
        logger.exception("Slack delivery crashed for %s", sku)
        with database() as connection:
            reviews.fail_unsent(connection, "Sending to Slack failed unexpectedly. Try again.")
    finally:
        with _sending_lock:
            _sending.discard(sku)


def recover_unsent() -> None:
    with database() as connection:
        reviews.fail_unsent(connection, "Interrupted by a server restart. Send again.")


def handle_block_action(payload: dict, client=None) -> str | None:
    """Apply an Approve click. Only the configured approver may decide; the first approval is final."""
    action = (payload.get("actions") or [{}])[0]
    if action.get("action_id") != APPROVE_ACTION:
        return None
    client = client or slack.client()
    user = payload["user"]["id"]
    channel = payload.get("channel", {}).get("id") or ""
    if user != slack.setting("SLACK_APPROVER_USER_ID"):
        client.post_ephemeral(channel, user, "Only the designated approver can approve images.")
        return "unauthorized"
    image_id = action["value"]
    with database() as connection:
        outcome = reviews.approve(connection, image_id, user)
        review = reviews.get_review(connection, image_id)
        siblings = reviews.reviews_for_sku(connection, review["product_sku"]) if review else []
        product = products.get_products(connection, [review["product_sku"]])[0] if review else None
    messages = {
        "sibling_approved": "Another image for this product is already approved. Only one can be approved.",
        "already_approved": "This image is already approved.",
        "not_awaiting": "This image is not waiting for approval.",
    }
    if outcome in messages:
        client.post_ephemeral(channel, user, messages[outcome])
        return outcome
    versions = {image["id"]: image["version"] for image in product["images"]}
    for item in siblings:
        if item["state"] == "pending_send" or not item["slack_message_ts"]:
            continue
        footer = (f"✅ Approved by <@{user}>" if item["image_id"] == image_id
                  else "Not selected — another image was approved")
        try:
            client.update_message(
                item["slack_channel"], item["slack_message_ts"], footer,
                review_blocks(product, versions[item["image_id"]], item["image_id"], footer))
        except Exception:  # The decision is already saved; a stale message is only cosmetic.
            logger.exception("Could not update Slack message for %s", item["image_id"])
    return outcome
