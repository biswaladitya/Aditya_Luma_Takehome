"""Slack review: post candidates to a product thread and record Ellie's approval.

Generation posts its candidates itself (generation.py); /api/reviews only retries failed posts.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from litestar.exceptions import HTTPException

from backend import slack
from backend.db import data_directory, database
from backend.repositories import products, reviews
from backend.services.catalog import product_view

logger = logging.getLogger(__name__)
APPROVE_ACTION = "approve_image"
OUTDATED = "Outdated: product attributes changed"

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack")


def review_blocks(brief: dict, version: int, image_id: str, footer: str | None = None) -> list:
    """Message blocks for one candidate; the image itself is the message posted just above.

    `brief` is what the image was made from. Without a footer they carry the Approve button.
    """
    name = brief["product_name"] or brief["sku"]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{name}* · `{brief['sku']}` · candidate v{version} (image above)\n_{brief['shot_idea']}_"}},
    ]
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    else:
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "style": "primary", "action_id": APPROVE_ACTION, "value": image_id,
            "text": {"type": "plain_text", "text": "Approve"},
            "confirm": {
                "title": {"type": "plain_text", "text": "Approve this image?"},
                "text": {"type": "mrkdwn", "text": "Only one image can be approved for these product attributes, and it cannot be undone."},
                "confirm": {"type": "plain_text", "text": "Approve"},
                "deny": {"type": "plain_text", "text": "Cancel"},
            },
        }]})
    return blocks


def _brief(product: dict, image: dict) -> dict:
    return {**product, **image["generated_from"]}


def start_review(skus: list[str]) -> dict:
    """Retry posting: post a product's current-brief candidates that never reached Slack."""
    if not skus:
        raise HTTPException(status_code=400, detail="Select at least one product.")
    try:
        slack.require("SLACK_BOT_TOKEN"), slack.require("SLACK_CHANNEL_ID")
    except slack.SlackError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    queued, skipped, jobs = [], [], []
    with database() as connection:
        found = {row["sku"]: product_view(row) for row in products.get_products(connection, skus)}
        for sku in dict.fromkeys(skus):
            row = found.get(sku)
            if row is None:
                skipped.append({"sku": sku, "reason": "Unknown SKU."})
            elif row["review_status"] == "approved":
                skipped.append({"sku": sku, "reason": "An image is already approved."})
            elif row["generating"]:
                skipped.append({"sku": sku, "reason": "Already generating or posting to Slack."})
            elif not row["can_send"]:
                skipped.append({"sku": sku, "reason": "No candidates are waiting to be posted."})
            else:
                # Posting is the last step of a candidate's generation, so it is processing again until posted.
                for image_id in row["reviewable_image_ids"]:
                    products.update_generated_image(connection, image_id, status="processing", post_error=None)
                jobs.append((sku, row["reviewable_image_ids"]))
                queued.append(sku)
    for job in jobs:  # Submit only after the processing marks are committed.
        _executor.submit(post_candidates, *job)
    return {"queued": queued, "skipped": skipped}


def post_candidates(sku: str, image_ids: list[str]) -> None:
    """Post the parent message (once per product) and each candidate. Each candidate becomes done:
    posted with a review row waiting on Ellie, or with a post_error that Retry posting clears."""
    def failed(ids: list[str], error: str) -> None:
        with database() as connection:
            for image_id in ids:
                products.update_generated_image(connection, image_id, status="done", post_error=error[:500])

    try:
        with database() as connection:
            product = products.get_products(connection, [sku])[0]
            thread = reviews.thread_for_sku(connection, sku)
        images = {image["id"]: image for image in product["images"]}
        name = product["product_name"] or sku
        try:
            client = slack.client()
            channel = thread[0] if thread else slack.require("SLACK_CHANNEL_ID")
            thread_ts = thread[1] if thread else client.post_message(
                channel, f"{name} ({sku}) — shot idea: {product['shot_idea']}. Review the candidates below.")
        except Exception as exc:  # Nothing was posted for these images; they can be retried.
            failed(image_ids, str(exc))
            return
        for image_id in image_ids:
            image = images[image_id]
            try:
                path = data_directory() / "images" / image["storage_key"]
                if not path.is_file():
                    raise slack.SlackError("The generated image file is missing.")
                file_id = client.upload_image(Path(path), f"{name} v{image['version']}", channel, thread_ts)
                message_ts = client.post_message(
                    channel, f"{name} candidate v{image['version']}",
                    blocks=review_blocks(_brief(product, image), image["version"], image_id), thread_ts=thread_ts)
                with database() as connection:
                    reviews.record_post(connection, image_id, channel, thread_ts, message_ts, file_id)
                    products.update_generated_image(connection, image_id, status="done", post_error=None)
            except Exception as exc:
                failed([image_id], str(exc))
    except Exception:
        logger.exception("Posting to Slack crashed for %s", sku)
        with database() as connection:
            unfinished = [image["id"] for image in products.get_products(connection, [sku])[0]["images"]
                          if image["id"] in image_ids and image["status"] == "processing"]
        failed(unfinished, "Posting to Slack failed unexpectedly. Retry posting.")


def mark_outdated(items: list[dict]) -> None:
    """After a brief change, replace the Approve button on waiting candidates in the background.

    Best effort: failures are logged and never undo the import; Approve on them is refused regardless.
    """
    if items:
        _executor.submit(_mark_outdated, items)


def _mark_outdated(items: list[dict]) -> None:
    try:
        client = slack.client()
        with database() as connection:
            catalog = {row["sku"]: row for row in products.get_products(connection, list({i["product_sku"] for i in items}))}
    except Exception:
        logger.exception("Could not mark outdated Slack messages")
        return
    for item in items:
        product = catalog[item["product_sku"]]
        image = next(image for image in product["images"] if image["id"] == item["image_id"])
        try:
            client.update_message(item["slack_channel"], item["slack_message_ts"], OUTDATED,
                                  review_blocks(_brief(product, image), image["version"], image["id"], OUTDATED))
        except Exception:
            logger.exception("Could not mark Slack message for %s outdated", item["image_id"])


def handle_block_action(payload: dict, client=None) -> str | None:
    """Apply an Approve click. Anyone may decide unless SLACK_APPROVER_USER_ID is set, then only that
    user; only on an image made from the product's current brief, and only while that brief has no approval."""
    action = (payload.get("actions") or [{}])[0]
    if action.get("action_id") != APPROVE_ACTION:
        return None
    client = client or slack.client()
    user = payload["user"]["id"]
    channel = payload.get("channel", {}).get("id") or ""
    approver = slack.setting("SLACK_APPROVER_USER_ID")
    if approver and user != approver:
        client.post_ephemeral(channel, user, "Only the designated approver can approve images.")
        return "unauthorized"
    image_id = action["value"]
    with database() as connection:
        outcome = reviews.approve(connection, image_id, user)
        review = reviews.get_review(connection, image_id)
        siblings = [item for item in reviews.reviews_for_sku(connection, review["product_sku"])
                    if item["brief_version"] == review["brief_version"]] if review else []
        product = products.get_products(connection, [review["product_sku"]])[0] if review else None
    messages = {
        "outdated": "This image was made from older product attributes, so it can't be approved. New images can be generated in the web app.",
        "sibling_approved": "Another image for these product attributes is already approved. Only one can be approved.",
        "already_approved": "This image is already approved.",
        "not_awaiting": "This image is not waiting for approval.",
    }
    if outcome in messages:
        client.post_ephemeral(channel, user, messages[outcome])
        return outcome
    images = {image["id"]: image for image in product["images"]}
    for item in siblings:
        footer = (f"✅ Approved by <@{user}>" if item["image_id"] == image_id
                  else "Not selected — another image was approved")
        image = images[item["image_id"]]
        try:
            client.update_message(
                item["slack_channel"], item["slack_message_ts"], footer,
                review_blocks(_brief(product, image), image["version"], item["image_id"], footer))
        except Exception:  # The decision is already saved; a stale message is only cosmetic.
            logger.exception("Could not update Slack message for %s", item["image_id"])
    return outcome
