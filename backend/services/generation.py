"""Explicit, paid image generation: queue rows, run Luma in background, store files, post to Slack."""

import re
from concurrent.futures import ThreadPoolExecutor

from litestar.exceptions import HTTPException

from backend import luma
from backend.db import data_directory, database
from backend.repositories import products
from backend.services import catalog, review
from backend.services.catalog import attributes, product_view

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
        product = products.get_products(connection, [sku])[0]
        images = [image for image in product["images"] if image["id"] in batch]
    if any(image["status"] == "queued" or (image["status"] == "processing" and image["luma_generation_id"] is None)
           for image in images):
        return
    generated = [image["id"] for image in images if image["status"] == "processing"]
    if generated:
        review.post_candidates(sku, generated)
    elif product_view(product)["can_request_more"]:
        # A More options batch that failed entirely would leave the thread without a button.
        review.post_more_options(sku, review.MORE_FAILED)


def _queue(connection, row: dict) -> list[tuple]:
    """Queue one batch of candidates for the product's current brief and return its jobs."""
    snapshot = attributes(row)
    first = max((image["version"] for image in row["images"]), default=0) + 1
    versions = range(first, first + catalog.IMAGES_PER_REQUEST)
    batch = [
        products.save_generated_image(
            connection, row["sku"], snapshot, image_filename(row, version), None,
            version=version, status="queued", brief_version=row["brief_version"],
        )["id"]
        for version in versions
    ]
    return [(image_id, row["sku"], snapshot, version, batch) for image_id, version in zip(batch, versions)]


def start_generation(skus: list[str]) -> dict:
    """Queue IMAGES_PER_REQUEST candidates per eligible SKU, which are posted to Slack once generated.

    Only this and request_more (the More options button in Slack) spend credits.
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
            elif row["approval_limit_reached"]:  # New candidates could never be approved.
                skipped.append({"sku": sku, "reason": f"{review.LIMIT} images are already approved."})
            elif not row["can_generate"]:
                skipped.append({"sku": sku, "reason": "Not eligible for generation."})
            elif row["cap_reached"]:
                skipped.append({"sku": sku, "reason": f"Attempt cap of {catalog.MAX_IMAGES_PER_BRIEF} images reached."})
            else:
                jobs.extend(_queue(connection, row))
                queued.append(sku)
    for job in jobs:  # Submit only after the queue rows are committed.
        _executor.submit(_run, *job)
    return {"queued": queued, "skipped": skipped, "images_queued": len(jobs)}


def request_more(sku: str, brief_version: int) -> str:
    """More options: queue another batch for a brief that already has candidates.

    Returns queued, or the refusal: unknown_sku, outdated, generating, delivering, approval_limit
    (the brief has all the approvals it can take), invalid_inputs, no_candidates or cap_reached.
    Checking and queueing share one transaction, so a double click cannot queue twice.
    """
    with database() as connection:
        found = products.get_products(connection, [sku])
        row = product_view(found[0]) if found else None
        if row is None:
            return "unknown_sku"
        if brief_version != row["brief_version"]:
            return "outdated"
        if row["generating"]:
            return "generating"
        if row["delivering"]:
            return "delivering"
        if row["approval_limit_reached"]:
            return "approval_limit"
        if not row["ready"]:
            return "invalid_inputs"
        if row["generation_status"] != "already_generated":
            return "no_candidates"
        if row["cap_reached"] or row["attempts_exhausted"]:
            return "cap_reached"
        jobs = _queue(connection, row)
    for job in jobs:  # Submit only after the queue rows are committed.
        _executor.submit(_run, *job)
    return "queued"


def recover_interrupted() -> None:
    with database() as connection:
        # Only a batch cut off before Luma returned anything; one cut off while posting isn't a failure.
        cut_off = [row["sku"] for row in products.get_products(connection)
                   if (active := [image for image in row["images"] if image["status"] in catalog.ACTIVE])
                   and not any(image["luma_generation_id"] for image in active)]
        products.fail_unfinished_images(
            connection, "Interrupted by a server restart. Generate again.",
            "Posting was interrupted by a restart. Retry posting.")
        rows = [product_view(row) for row in products.get_products(connection, cut_off)]
    # A cut-off More options batch already consumed its button; one with a candidate left gets it on Retry posting.
    for row in rows:
        if row["can_request_more"] and not row["can_send"]:
            review.more_options_after_failure(row["sku"])
