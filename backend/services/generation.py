"""Explicit, paid image generation: queue rows, run Luma in background, store files, post to Slack."""

import re
from concurrent.futures import ThreadPoolExecutor

from litestar.exceptions import HTTPException

from backend import luma
from backend.db import data_directory, database
from backend.repositories import products
from backend.services import review
from backend.services.catalog import IMAGES_PER_REQUEST, attributes, product_view

MAX_IMAGES_PER_BRIEF = 6  # ASSUMPTIONS.md: six-attempt cap, per brief version
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


def _run(image_id: str, sku: str, row: dict, version: int, batch: list[str]) -> None:
    """Generate one candidate. It stays processing until the whole batch has finished and been posted."""
    try:
        with database() as connection:
            products.update_generated_image(connection, image_id, status="processing")
        generation_id, content, content_type = luma.generate_image(build_prompt(row), row["photo"])
        key = image_filename(row, version, EXTENSIONS.get(content_type, ".jpg"))
        path = data_directory() / "images" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        fields = {"storage_key": key, "luma_generation_id": generation_id, "error": None}
    except Exception as exc:  # Surface every failure on the row instead of losing it in a thread.
        fields = {"status": "failed", "error": str(exc)[:500]}
    # Recording and checking the batch share one transaction, so exactly one worker sees it finished.
    with database() as connection:
        products.update_generated_image(connection, image_id, **fields)
        images = [image for image in products.get_products(connection, [sku])[0]["images"] if image["id"] in batch]
    if any(image["status"] == "queued" or (image["status"] == "processing" and image["luma_generation_id"] is None)
           for image in images):
        return
    generated = [image["id"] for image in images if image["status"] == "processing"]
    if generated:
        review.post_candidates(sku, generated)


def start_generation(skus: list[str]) -> dict:
    """Queue IMAGES_PER_REQUEST candidates per eligible SKU, which are posted to Slack once generated.

    Nothing else spends credits.
    """
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
            elif row["generating"]:
                skipped.append({"sku": sku, "reason": "Generation already in progress."})
            elif row["delivering"]:
                skipped.append({"sku": sku, "reason": "Saving to Drive is in progress."})
            elif row["review_status"] == "approved":  # New candidates could never be approved.
                skipped.append({"sku": sku, "reason": "Already approved."})
            elif not row["can_generate"]:
                skipped.append({"sku": sku, "reason": "Not eligible for generation."})
            else:
                used = [image for image in row["images"]
                        if image["status"] != "failed" and image["brief_version"] == row["brief_version"]]
                if len(used) + IMAGES_PER_REQUEST > MAX_IMAGES_PER_BRIEF:
                    skipped.append({"sku": sku, "reason": f"Attempt cap of {MAX_IMAGES_PER_BRIEF} images reached."})
                    continue
                snapshot = attributes(row)
                first = max((image["version"] for image in row["images"]), default=0) + 1
                batch = [
                    products.save_generated_image(
                        connection, sku, snapshot, image_filename(row, version), None,
                        version=version, status="queued", brief_version=row["brief_version"],
                    )["id"]
                    for version in range(first, first + IMAGES_PER_REQUEST)
                ]
                jobs.extend((image_id, sku, snapshot, version, batch)
                            for image_id, version in zip(batch, range(first, first + IMAGES_PER_REQUEST)))
                queued.append(sku)
    for job in jobs:  # Submit only after the queue rows are committed.
        _executor.submit(_run, *job)
    return {"queued": queued, "skipped": skipped, "images_queued": len(jobs)}


def recover_interrupted() -> None:
    with database() as connection:
        products.fail_unfinished_images(
            connection, "Interrupted by a server restart. Generate again.",
            "Posting was interrupted by a restart. Retry posting.")
