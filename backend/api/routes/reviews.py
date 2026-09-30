"""Slack review endpoint. Sending happens only on an explicit request."""

from dataclasses import dataclass

from litestar import post

from backend.services.review import start_review


@dataclass
class ReviewRequest:
    skus: list[str]


@post("/api/reviews", status_code=202, sync_to_thread=True)
def send_for_review(data: ReviewRequest) -> dict:
    """Post generated candidates to the product's Slack thread; approval arrives via Socket Mode."""
    return start_review(data.skus)


review_routes = [send_for_review]
