"""Drive delivery endpoints. Writing to Drive happens only on an explicit request."""

from dataclasses import dataclass

from litestar import get, post

from backend.services.delivery import start_delivery
from backend.settings import setting


@dataclass
class DeliveryRequest:
    skus: list[str]
    access_token: str = ""


@get("/api/drive/config", sync_to_thread=True)
def drive_config() -> dict:
    """The public OAuth client ID the browser needs to show Google sign-in."""
    return {"client_id": setting("GOOGLE_CLIENT_ID")}


@post("/api/deliveries", status_code=202, sync_to_thread=True)
def save_to_drive(data: DeliveryRequest) -> dict:
    """Save each product's approved image to the signed-in user's My Drive; progress appears in the catalog."""
    return start_delivery(data.skus, data.access_token)


delivery_routes = [drive_config, save_to_drive]
