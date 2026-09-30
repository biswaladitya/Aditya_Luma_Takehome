"""Minimal Luma Agents API client (image_edit). Standard library only."""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://agents.lumalabs.ai/v1"
MODEL = "uni-1"
POLL_SECONDS = 2
TIMEOUT_SECONDS = 300
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class LumaError(RuntimeError):
    """A failure safe to show to users; never contains credentials."""


def api_key() -> str:
    """Read LUMA_AGENTS_API_KEY from the environment, falling back to .env.local."""
    if key := os.environ.get("LUMA_AGENTS_API_KEY"):
        return key
    env_file = PROJECT_ROOT / ".env.local"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "LUMA_AGENTS_API_KEY" and value.strip():
                return value.strip().strip("'\"")
    raise LumaError("LUMA_AGENTS_API_KEY is not configured (set it in .env.local).")


def _request(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise LumaError(f"Luma API returned {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LumaError(f"Could not reach the Luma API: {exc}") from exc


def generate_image(prompt: str, source_url: str) -> tuple[str, bytes, str]:
    """Edit the source photo per prompt; returns (generation_id, image_bytes, content_type)."""
    created = _request("POST", f"{BASE_URL}/generations", {
        "type": "image_edit", "model": MODEL, "prompt": prompt, "source": {"url": source_url},
    })
    generation_id = created["id"]
    deadline = time.monotonic() + TIMEOUT_SECONDS
    state = created
    while state.get("state") not in ("completed", "failed"):
        if time.monotonic() > deadline:
            raise LumaError("Luma generation timed out.")
        time.sleep(POLL_SECONDS)
        state = _request("GET", f"{BASE_URL}/generations/{generation_id}")
    if state["state"] == "failed":
        raise LumaError(f"Luma generation failed: {state.get('failure_reason') or state.get('failure_code')}")
    try:
        with urllib.request.urlopen(state["output"][0]["url"], timeout=60) as response:
            return generation_id, response.read(), response.headers.get_content_type()
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError) as exc:
        raise LumaError(f"Could not download the generated image: {exc}") from exc
