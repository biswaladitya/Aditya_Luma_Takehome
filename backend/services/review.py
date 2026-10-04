"""Slack review: post candidates to a product thread and record Ellie's approval.

Generation posts its candidates itself (generation.py); /api/reviews only retries failed posts.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from litestar.exceptions import HTTPException

from backend import slack
from backend.db import data_directory, database
from backend.repositories import products, reviews
from backend.services import catalog
from backend.services.catalog import product_view

logger = logging.getLogger(__name__)
APPROVE_ACTION = "approve_image"
MORE_ACTION = "more_options"
OUTDATED = "Outdated: product attributes changed"
MORE_PROMPT = "None of these work?"
MORE_FAILED = "Generation failed, try again."
LIMIT = reviews.MAX_APPROVED_PER_BRIEF
NOT_SELECTED = f"Not selected — {LIMIT} images were approved"

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack")
# Held from reading the approval count to rendering it, so two approvals, or an approval and a post,
# can't write Slack messages from stale counts. The database decides; this only keeps Slack matching it.
_render_lock = threading.Lock()


def review_blocks(brief: dict, version: int, image_id: str, footer: str | None = None, approved: int = 0) -> list:
    """Message blocks for one candidate; the image itself is the message posted just above.

    `brief` is what the image was made from. Without a footer they carry the Approve button, whose
    confirmation says how many of the brief's images are `approved` so far.
    """
    so_far = f"{approved} of {LIMIT} approved so far" if approved else f"Up to {LIMIT} images can be approved"
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
                "text": {"type": "mrkdwn", "text": f"{so_far} for these product attributes. Approving cannot be undone."},
                "confirm": {"type": "plain_text", "text": "Approve"},
                "deny": {"type": "plain_text", "text": "Cancel"},
            },
        }]})
    return blocks


def more_options_blocks(sku: str, brief_version: int, text: str = MORE_PROMPT) -> list:
    """One button per batch; its value names the product and brief, since it is about the whole set."""
    count = catalog.IMAGES_PER_REQUEST
    cost = count * catalog.EST_COST_PER_IMAGE_USD
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [{
            "type": "button", "action_id": MORE_ACTION,
            "value": json.dumps({"sku": sku, "brief_version": brief_version}, separators=(",", ":")),
            "text": {"type": "plain_text", "text": "More options"},
            "confirm": {
                "title": {"type": "plain_text", "text": "Generate more options?"},
                "text": {"type": "mrkdwn", "text": f"This generates {count} more images for about ${cost:.2f} and posts them to this thread. The images above can still be approved."},
                "confirm": {"type": "plain_text", "text": "Generate"},
                "deny": {"type": "plain_text", "text": "Cancel"},
            },
        }]},
    ]


def post_more_options(sku: str, text: str = MORE_PROMPT, settling: str | None = None) -> bool:
    """Post the More options button to the product's thread, unless the product can't use it.

    `settling` is the candidate post_candidates marks done right after this, so it counts as done.
    Best effort: the candidates are already posted, so a failure here is only logged.
    """
    try:
        with database() as connection:
            found = products.get_products(connection, [sku])
            thread = reviews.thread_for_sku(connection, sku)
        for image in found[0]["images"] if found else []:
            if image["id"] == settling:
                image["status"] = "done"
        row = product_view(found[0]) if found else None
        # A Drive save in flight refuses a click, but must not cost the thread its button.
        if not (row and thread and row["more_options_open"] and not row["generating"]):
            return False
        blocks = more_options_blocks(sku, row["brief_version"], text)
        try:
            slack.client().post_message(thread[0], text, blocks=blocks, thread_ts=thread[1])
        except Exception:  # Nothing stores this message, so it can't be retried later: try once more now.
            slack.client().post_message(thread[0], text, blocks=blocks, thread_ts=thread[1])
        return True
    except Exception:
        logger.exception("Could not post More options for %s", sku)
        return False


def more_options_after_failure(sku: str) -> None:
    """In the background (startup must not wait on Slack): the failure note with a fresh button."""
    _executor.submit(post_more_options, sku, MORE_FAILED)


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
            elif row["approval_limit_reached"]:
                skipped.append({"sku": sku, "reason": f"{LIMIT} images are already approved."})
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
        posted, last = 0, None
        for image_id in image_ids:
            image = images[image_id]
            try:
                with _render_lock:
                    with database() as connection:
                        view = product_view(products.get_products(connection, [sku])[0])
                        # The approval limit was reached while this batch was at Luma or being posted
                        # (More options): the candidate can't be approved, so it is kept unposted and
                        # shows as Not selected.
                        if view["approval_limit_reached"]:
                            products.update_generated_image(connection, image_id, status="done", post_error=None)
                            continue
                    last = _post_candidate(client, product, image, channel, thread_ts, view["approved_count"],
                                           settle=image_id != image_ids[-1]) or last
                posted += 1
            except Exception as exc:
                failed([image_id], str(exc))
        if last:
            # Only once the whole call is posted: a partly posted batch gets its one button from Retry posting.
            if posted == len(image_ids):
                post_more_options(sku, settling=last)
            with database() as connection:
                products.update_generated_image(connection, last, status="done", post_error=None)
    except Exception:
        logger.exception("Posting to Slack crashed for %s", sku)
        with database() as connection:
            unfinished = [image["id"] for image in products.get_products(connection, [sku])[0]["images"]
                          if image["id"] in image_ids and image["status"] == "processing"]
        failed(unfinished, "Posting to Slack failed unexpectedly. Retry posting.")


def _post_candidate(client, product: dict, image: dict, channel: str, thread_ts: str, approved: int, settle: bool) -> str | None:
    """Upload and post one candidate and record its review. Returns its id when it is left processing
    (the batch's last one, until the More options button is posted)."""
    name = product["product_name"] or product["sku"]
    path = data_directory() / "images" / image["storage_key"]
    if not path.is_file():
        raise slack.SlackError("The generated image file is missing.")
    file_id = client.upload_image(Path(path), f"{name} v{image['version']}", channel, thread_ts)
    message_ts = client.post_message(
        channel, f"{name} candidate v{image['version']}",
        blocks=review_blocks(_brief(product, image), image["version"], image["id"], approved=approved), thread_ts=thread_ts)
    with database() as connection:
        reviews.record_post(connection, image["id"], channel, thread_ts, message_ts, file_id)
        if settle:
            products.update_generated_image(connection, image["id"], status="done", post_error=None)
    return None if settle else image["id"]


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
            by_sku = {row["sku"]: row for row in products.get_products(connection, list({i["product_sku"] for i in items}))}
    except Exception:
        logger.exception("Could not mark outdated Slack messages")
        return
    for item in items:
        product = by_sku[item["product_sku"]]
        image = next(image for image in product["images"] if image["id"] == item["image_id"])
        try:
            client.update_message(item["slack_channel"], item["slack_message_ts"], OUTDATED,
                                  review_blocks(_brief(product, image), image["version"], image["id"], OUTDATED))
        except Exception:
            logger.exception("Could not mark Slack message for %s outdated", item["image_id"])


def handle_block_action(payload: dict, client=None) -> str | None:
    """Dispatch a button click. Anyone may click unless SLACK_APPROVER_USER_ID is set, then only that user."""
    action = (payload.get("actions") or [{}])[0]
    handlers = {APPROVE_ACTION: (_approve, "approve images"), MORE_ACTION: (_more_options, "request more options")}
    if action.get("action_id") not in handlers:
        return None
    handler, verb = handlers[action["action_id"]]
    client = client or slack.client()
    user = payload["user"]["id"]
    channel = payload.get("channel", {}).get("id") or ""
    approver = slack.setting("SLACK_APPROVER_USER_ID")
    if approver and user != approver:
        client.post_ephemeral(channel, user, f"Only the designated approver can {verb}.")
        return "unauthorized"
    return handler(action, payload, client, user, channel)


def _more_options(action: dict, payload: dict, client, user: str, channel: str) -> str:
    """Queue another batch for the button's product and brief, then retire the button so it can't be
    tapped twice. A refusal leaves the button and tells the clicker why."""
    from backend.services import generation  # generation imports this module.

    try:
        value = json.loads(action["value"])
        sku, brief_version = str(value["sku"]), value["brief_version"]
        if type(brief_version) is not int:
            raise ValueError("brief_version must be an integer")
    except (KeyError, TypeError, ValueError):
        outcome = "unknown_sku"
    else:
        outcome = generation.request_more(sku, brief_version)
    messages = {
        "unknown_sku": "This product is no longer in the catalog.",
        "invalid_inputs": "This product's attributes are incomplete, so nothing can be generated. Fix them in the CSV and import it again.",
        "generating": "Images for this product are already being generated.",
        "delivering": "This product is being saved to Drive. Try again in a moment.",
        "approval_limit": f"{LIMIT} images are already approved for these product attributes, the most allowed.",
        "outdated": "The product attributes changed since this was posted. New images can be generated in the web app.",
        "no_candidates": "This product has no candidates for its current attributes. Generate them in the web app.",
        "cap_reached": f"The limit of {catalog.MAX_IMAGES_PER_BRIEF} images for these product attributes is reached.",
    }
    if outcome in messages:
        client.post_ephemeral(channel, user, messages[outcome])
        return outcome
    text = f"More options requested by <@{user}>"
    ts = (payload.get("message") or {}).get("ts") or (payload.get("container") or {}).get("message_ts")
    try:
        client.update_message(channel, ts, text, [{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}])
    except Exception:  # The batch is already queued; a stale button is refused while it runs.
        logger.exception("Could not update the More options message for %s", sku)
    return outcome


def _approve(action: dict, payload: dict, client, user: str, channel: str) -> str:
    """Apply an Approve click: only on an image made from the product's current brief, and only
    while that brief is under its approval limit."""
    image_id = action["value"]
    with _render_lock:
        with database() as connection:
            outcome = reviews.approve(connection, image_id, user)
            review = reviews.get_review(connection, image_id)
            siblings = [item for item in reviews.reviews_for_sku(connection, review["product_sku"])
                        if item["brief_version"] == review["brief_version"]] if review else []
            product = products.get_products(connection, [review["product_sku"]])[0] if review else None
        messages = {
            "outdated": "This image was made from older product attributes, so it can't be approved. New images can be generated in the web app.",
            "approval_limit": f"{LIMIT} images are already approved for these product attributes. No more can be approved.",
            "already_approved": "This image is already approved.",
            "not_awaiting": "This image is not waiting for approval.",
        }
        if outcome in messages:
            client.post_ephemeral(channel, user, messages[outcome])
            return outcome
    images = {image["id"]: image for image in product["images"]}
    count = sum(item["state"] == "approved" for item in siblings)
    for item in siblings:
        image = images[item["image_id"]]
        if item["image_id"] == image_id:
            footer = f"✅ Approved by <@{user}> — {count} of {LIMIT}"
        elif item["state"] == "approved":
            continue  # Keeps the note from its own approval.
        else:  # Waiting candidates keep Approve, with the new count, until the limit is reached.
            footer = NOT_SELECTED if count >= LIMIT else None
        try:
            client.update_message(
                item["slack_channel"], item["slack_message_ts"],
                footer or f"{product['product_name'] or product['sku']} candidate v{image['version']}",
                review_blocks(_brief(product, image), image["version"], item["image_id"], footer, count))
        except Exception:  # The decision is already saved; a stale message is only cosmetic.
            logger.exception("Could not update Slack message for %s", item["image_id"])
    return outcome
