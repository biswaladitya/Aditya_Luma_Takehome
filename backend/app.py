"""Litestar application entry point."""

from pathlib import Path

from litestar import Litestar
from litestar.config.cors import CORSConfig
from litestar.static_files import create_static_files_router

from backend import slack
from backend.api.routes.catalog import catalog_routes
from backend.api.routes.reviews import review_routes
from backend.services.generation import recover_interrupted
from backend.services.review import handle_block_action, recover_unsent

frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
routes = [*catalog_routes, *review_routes]
if frontend_dist.is_dir():
    routes.append(
        create_static_files_router(
            path="/", directories=[frontend_dist], html_mode=True
        )
    )

_socket = None


def start_slack() -> None:
    global _socket
    _socket = slack.start_listener(handle_block_action)


def stop_slack() -> None:
    if _socket is not None:
        _socket.close()


app = Litestar(
    route_handlers=routes,
    on_startup=[recover_interrupted, recover_unsent, start_slack],
    on_shutdown=[stop_slack],
    cors_config=CORSConfig(allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"]),
)
