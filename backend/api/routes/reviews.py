"""Slack endpoints: review posts and the status report. Sending happens only on an explicit request."""

from dataclasses import dataclass

from litestar import post

from backend.services.review import start_review
from backend.services.status_report import send_report


@dataclass
class ReviewRequest:
    skus: list[str]


@post("/api/reviews", status_code=202, sync_to_thread=True)
def send_for_review(data: ReviewRequest) -> dict:
    """Post generated candidates to the product's Slack thread; approval arrives via Socket Mode."""
    return start_review(data.skus)


@post("/api/status-report", status_code=200, sync_to_thread=True)
def send_status_report() -> dict:
    """Post the dashboard's tabs and spend to the Slack status channel now."""
    return send_report()


review_routes = [send_for_review, send_status_report]
