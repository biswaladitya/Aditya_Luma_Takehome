"""Slack status report: the dashboard's tabs and spend as one message, sent only on request.

Built from get_catalog(), the same response the dashboard renders, so the two cannot disagree.
"""

import logging
from datetime import date

from litestar.exceptions import HTTPException

from backend import slack
from backend.db import database
from backend.repositories import reviews
from backend.services.catalog import get_catalog

logger = logging.getLogger(__name__)
REFRESH_ACTION = "refresh_status"
LINKED_TAB = "ellie"  # Its products link to their Slack review thread.
MAX_LISTED = 20  # Products listed per tab; the rest are counted in "and N more".
MAX_NAME = 60
SECTION_LIMIT = 3000  # Slack's limit for a section's text.
_TAIL = len("\n_and 999999 more_")
# Slack's error codes for the usual setup mistakes, in words the dashboard can show.
_HINTS = {
    "not_in_channel": "the bot is not in the status channel. Invite it to that channel",
    "channel_not_found": "the status channel was not found. Check SLACK_STATUS_CHANNEL_ID and invite the bot to that channel",
    "is_archived": "the status channel is archived",
}


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _line(row: dict, link: str | None) -> str:
    name = " ".join((row["product_name"] or "").split())
    if len(name) > MAX_NAME:
        name = name[:MAX_NAME - 1] + "…"
    sku = _escape(row["sku"])
    return f"• {f'<{link}|{sku}>' if link else sku}{' · ' + _escape(name) if name else ''}"


def tab_rows(catalog: dict, tab: dict) -> list[dict]:
    """The products a tab may list: its first MAX_LISTED by SKU."""
    rows = sorted((row for row in catalog["rows"] if row["stage"] in tab["stages"]), key=lambda row: row["sku"])
    return rows[:MAX_LISTED]


def _section(catalog: dict, tab: dict, links: dict) -> dict:
    lines = [f"*{tab['label']}* ({tab['count']})"]
    used, listed = len(lines[0]) + _TAIL, 0
    for row in tab_rows(catalog, tab):
        line = _line(row, links.get(row["sku"]) if tab["id"] == LINKED_TAB else None)
        if used + len(line) + 1 > SECTION_LIMIT:  # Escaping or long links can outgrow the cap's estimate.
            break
        lines.append(line)
        used, listed = used + len(line) + 1, listed + 1
    if not tab["count"]:
        lines.append("_None_")
    elif tab["count"] > listed:
        lines.append(f"_and {tab['count'] - listed} more_")
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


def build_report(catalog: dict, links: dict | None = None, requested_by: str | None = None) -> tuple[str, list]:
    """The report's fallback text and blocks; `links` maps a SKU to its Slack thread's URL."""
    today = date.today()
    total, spend = catalog["total_rows"], catalog["spend"]
    title = f"Catalog status · {today.day} {today:%b %Y} · {total} product{'' if total == 1 else 's'}"
    cost = f"{spend['images']} image{'' if spend['images'] == 1 else 's'} · about ${spend['est_cost_usd']:.2f}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"Generation spend (estimate): {cost}"}},
        *(_section(catalog, tab, links or {}) for tab in catalog["tabs"]),
    ]
    if requested_by:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Requested by <@{requested_by}>"}]})
    blocks.append({"type": "actions", "elements": [{
        "type": "button", "action_id": REFRESH_ACTION, "text": {"type": "plain_text", "text": "Refresh"}}]})
    return f"{title} · {cost}", blocks


def _thread_links(client, skus: list[str]) -> dict:
    """Best effort: a product whose link can't be fetched is listed without one."""
    with database() as connection:
        threads = {sku: reviews.thread_for_sku(connection, sku) for sku in skus}
    links = {}
    for sku, thread in threads.items():
        try:
            if thread:
                links[sku] = client.permalink(*thread)
        except Exception:
            logger.warning("Could not get the Slack thread link for %s", sku, exc_info=True)
    return links


def send_report(requested_by: str | None = None) -> dict:
    """Post a new report to the status channel, never the review channel."""
    try:
        slack.require("SLACK_BOT_TOKEN")
        channel = slack.require("SLACK_STATUS_CHANNEL_ID")
    except slack.SlackError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    catalog = get_catalog()
    if not catalog["total_rows"]:
        raise HTTPException(status_code=409, detail="Import a catalog first; there is nothing to report yet.")
    try:
        client = slack.client()
        linked = next((tab for tab in catalog["tabs"] if tab["id"] == LINKED_TAB), None)
        links = _thread_links(client, [row["sku"] for row in tab_rows(catalog, linked)]) if linked else {}
        text, blocks = build_report(catalog, links, requested_by)
        ts = client.post_message(channel, text, blocks=blocks)
    except Exception as exc:
        logger.exception("Could not send the status report")
        response = getattr(exc, "response", None)  # slack_sdk's SlackApiError carries Slack's error code.
        code = response.get("error") if hasattr(response, "get") else None
        reason = _HINTS.get(code) or code or str(exc).rstrip(".")
        raise HTTPException(status_code=502, detail=f"Slack did not accept the status report: {reason}.") from exc
    return {"sent": True, "channel": channel, "ts": ts, "products": catalog["total_rows"]}
