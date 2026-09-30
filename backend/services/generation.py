"""Explicit, paid image generation: queue rows, run Luma in background, store files."""

import re
from concurrent.futures import ThreadPoolExecutor

from litestar.exceptions import HTTPException

from backend import luma
from backend.db import data_directory, database
from backend.repositories import products
from backend.services.catalog import IMAGES_PER_REQUEST, attributes, product_view

MAX_IMAGES_PER_SKU = 6  # ASSUMPTIONS.md: six-attempt cap
ELIGIBLE = ("never_generated", "changed_since_generation")
ACTIVE = ("queued", "processing")
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="luma")


def slug(value: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:limit].strip("-") or "na"


def image_filename(row: dict, version: int, extension: str = ".jpg") -> str:
    """SKU_name_category_color_material_vN; hyphens inside fields, underscores between."""
    fields = [row[key] for key in ("sku", "product_name", "category", "color", "material")]
    return "_".join(slug(field) for field in fields) + f"_v{version}{extension}"


def build_prompt(row: dict) -> str:
    return (
        f"Create a styled lifestyle product photo of this {row['product_name']} "
        f"({row['color']}, {row['material']}). Scene: {row['shot_idea']}. "
        "Keep the product's shape, color, material and proportions exactly as in the source photo; "
        "replace the white background with the described scene, with natural lighting and realistic shadows."
    )


def _run(image_id: str, sku: str, row: dict, version: int) -> None:
    def record(**fields):
        with database() as connection:
            products.update_generated_image(connection, image_id, **fields)

    try:
        record(status="processing")
        generation_id, content, content_type = luma.generate_image(build_prompt(row), row["photo"])
        key = image_filename(row, version, EXTENSIONS.get(content_type, ".jpg"))
        path = data_directory() / "images" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        record(status="done", storage_key=key, luma_generation_id=generation_id, error=None)
    except Exception as exc:  # Surface every failure on the row instead of losing it in a thread.
        record(status="failed", error=str(exc)[:500])


def start_generation(skus: list[str]) -> dict:
    """Queue IMAGES_PER_REQUEST candidates per eligible SKU. Nothing else spends credits."""
    if not skus:
        raise HTTPException(status_code=400, detail="Select at least one product.")
    skus = list(dict.fromkeys(skus))
    queued, skipped, jobs = [], [], []
    with database() as connection:
        found = {row["sku"]: product_view(row) for row in products.get_products(connection, skus)}
        for sku in skus:
            row = found.get(sku)
            if row is None:
                skipped.append({"sku": sku, "reason": "Unknown SKU."})
            elif any(image["status"] in ACTIVE for image in row["images"]):
                skipped.append({"sku": sku, "reason": "Generation already in progress."})
            elif row["generation_status"] not in ELIGIBLE:
                skipped.append({"sku": sku, "reason": "Not eligible for generation."})
            else:
                used = [image for image in row["images"] if image["status"] != "failed"]
                if len(used) + IMAGES_PER_REQUEST > MAX_IMAGES_PER_SKU:
                    skipped.append({"sku": sku, "reason": f"Attempt cap of {MAX_IMAGES_PER_SKU} images reached."})
                    continue
                snapshot = attributes(row)
                first = max((image["version"] for image in row["images"]), default=0) + 1
                for version in range(first, first + IMAGES_PER_REQUEST):
                    image = products.save_generated_image(
                        connection, sku, snapshot, image_filename(row, version), None,
                        version=version, status="queued",
                    )
                    jobs.append((image["id"], sku, snapshot, version))
                queued.append(sku)
    for job in jobs:  # Submit only after the queue rows are committed.
        _executor.submit(_run, *job)
    return {"queued": queued, "skipped": skipped, "images_queued": len(jobs)}


def recover_interrupted() -> None:
    with database() as connection:
        products.fail_unfinished_images(connection, "Interrupted by a server restart. Generate again.")
