"""HTTP routes for catalog import preview."""

from litestar import post
from litestar.datastructures import UploadFile
from litestar.params import MultipartBody

from backend.services.catalog import MAX_CSV_BYTES, parse_catalog


@post("/api/catalog/preview", status_code=200)
async def preview_catalog(data: MultipartBody[UploadFile]) -> dict:
    content = await data.read(MAX_CSV_BYTES + 1)
    return parse_catalog(content, data.filename or "")
