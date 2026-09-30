"""Litestar application entry point."""

from pathlib import Path

from litestar import Litestar
from litestar.config.cors import CORSConfig
from litestar.static_files import create_static_files_router

from backend.api.routes.catalog import catalog_routes

frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
routes = list(catalog_routes)
if frontend_dist.is_dir():
    routes.append(
        create_static_files_router(
            path="/", directories=[frontend_dist], html_mode=True
        )
    )

app = Litestar(
    route_handlers=routes,
    cors_config=CORSConfig(allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"]),
)
