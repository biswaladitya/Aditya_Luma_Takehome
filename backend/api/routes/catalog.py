"""Catalog persistence endpoints. Upload and confirmation never generate images."""

from dataclasses import dataclass

from litestar import get, post
from litestar.concurrency import sync_to_thread
from litestar.datastructures import UploadFile
from litestar.params import MultipartBody
from litestar.response import File

from backend.services.catalog import MAX_CSV_BYTES, get_catalog, local_image_path
from backend.services.generation import start_generation
from backend.services.imports import confirm_catalog_import, get_catalog_import, preview_catalog_import


@post("/api/catalog/preview", status_code=200)
async def preview_catalog(data: MultipartBody[UploadFile]) -> dict:
    content = await data.read(MAX_CSV_BYTES + 1)
    return await sync_to_thread(preview_catalog_import, content, data.filename or "")


@get("/api/catalog", sync_to_thread=True)
def catalog() -> dict:
    return get_catalog()


@get("/api/catalog/generation-candidates", sync_to_thread=True)
def generation_candidates() -> dict:
    return get_catalog(candidates_only=True)


@get("/api/catalog/imports/{preview_id:str}", sync_to_thread=True)
def catalog_import(preview_id: str) -> dict:
    return get_catalog_import(preview_id)


@post("/api/catalog/imports/{preview_id:str}/confirm", status_code=200, sync_to_thread=True)
def confirm_import(preview_id: str) -> dict:
    return confirm_catalog_import(preview_id)


@get("/api/catalog/images/{image_id:str}", sync_to_thread=True)
def catalog_image(image_id: str) -> File:
    return File(path=local_image_path(image_id), content_disposition_type="inline")


@dataclass
class GenerationRequest:
    skus: list[str]


@post("/api/generations", status_code=202, sync_to_thread=True)
def generate(data: GenerationRequest) -> dict:
    """The only route that spends Luma credits; it runs only on an explicit request."""
    return start_generation(data.skus)


catalog_routes = [generate, preview_catalog, catalog, generation_candidates, catalog_import, confirm_import, catalog_image]
