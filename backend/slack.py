"""Slack Web API client and Socket Mode listener. Credentials come from the environment or .env.local."""

import logging
import os
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


class SlackError(RuntimeError):
    """A failure safe to show to users; never contains credentials."""


def setting(name: str) -> str | None:
    """Read a setting from the environment, falling back to .env.local.

    A variable that is present but empty counts as unset and is not looked up in the file.
    """
    if name in os.environ:
        return os.environ[name].strip() or None
    env_file = PROJECT_ROOT / ".env.local"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip().strip("'\"")
    return None


def require(name: str) -> str:
    if value := setting(name):
        return value
    raise SlackError(f"{name} is not configured (set it in .env.local).")


class SlackClient:
    """The few Slack calls the app needs. Tests substitute a fake with the same methods."""

    def __init__(self, token: str):
        from slack_sdk import WebClient
        self._web = WebClient(token=token)

    def post_message(self, channel: str, text: str, blocks: list | None = None, thread_ts: str | None = None) -> str:
        return self._web.chat_postMessage(channel=channel, text=text, blocks=blocks, thread_ts=thread_ts)["ts"]

    def update_message(self, channel: str, ts: str, text: str, blocks: list) -> None:
        self._web.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)

    def post_ephemeral(self, channel: str, user: str, text: str) -> None:
        self._web.chat_postEphemeral(channel=channel, user=user, text=text)

    def upload_image(self, path: Path, title: str, channel: str, thread_ts: str) -> str:
        """Upload straight into the thread; Slack rejects image blocks for unshared files."""
        response = self._web.files_upload_v2(
            file=str(path), filename=path.name, title=title, channel=channel, thread_ts=thread_ts)
        return (response.get("file") or response["files"][0])["id"]


def client() -> SlackClient:
    return SlackClient(require("SLACK_BOT_TOKEN"))


def start_listener(on_block_action: Callable[[dict], None]):
    """Open the Socket Mode connection so button clicks reach this process.

    Returns None when tokens are missing. Exactly one backend process may run this,
    otherwise every process receives each click.
    """
    app_token, bot_token = setting("SLACK_APP_TOKEN"), setting("SLACK_BOT_TOKEN")
    if not (app_token and bot_token):
        logger.warning("Slack tokens are not configured; button clicks will not be received.")
        return None
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    socket = SocketModeClient(app_token=app_token, web_client=WebClient(token=bot_token))

    def listen(connection, request) -> None:
        if request.type != "interactive":
            return
        # Acknowledge first; Slack re-delivers anything not acknowledged within seconds.
        connection.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        if request.payload.get("type") == "block_actions":
            try:
                on_block_action(request.payload)
            except Exception:
                logger.exception("Could not process a Slack action")

    socket.socket_mode_request_listeners.append(listen)
    socket.connect()
    return socket
