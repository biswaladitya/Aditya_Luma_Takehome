"""Drive delivery endpoints. Writing to Drive happens only on an explicit request."""

import re
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
    """Public browser identifiers, not secrets: the OAuth client ID for Google sign-in, plus the
    API key and app ID (project number, the client ID's leading digits) that Google Picker needs."""
    client_id = setting("GOOGLE_CLIENT_ID")
    project = re.match(r"(\d+)-", client_id or "")
    return {"client_id": client_id, "api_key": setting("GOOGLE_CLOUD_API_KEY"), "app_id": project and project.group(1)}


@post("/api/deliveries", status_code=202, sync_to_thread=True)
def save_to_drive(data: DeliveryRequest) -> dict:
    """Save each product's approved image to the signed-in user's My Drive; progress appears in the catalog."""
    return start_delivery(data.skus, data.access_token)


delivery_routes = [drive_config, save_to_drive]
