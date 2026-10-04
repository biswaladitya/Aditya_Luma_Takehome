"""Catalog views and generation provenance, without generation side effects."""

import csv
import io
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from litestar.exceptions import HTTPException

from backend.db import data_directory, database
from backend.repositories import products
from backend.repositories.products import BRIEF_ATTRIBUTES
from backend.repositories.reviews import MAX_APPROVED_PER_BRIEF, MIN_APPROVED_PER_BRIEF

COLUMNS = {
    "SKU": "sku", "Product Name": "product_name", "Category": "category",
    "Color / Finish": "color", "Material": "material", "Price": "price",
    "Photo": "photo", "Shot Idea": "shot_idea", "Notes": "notes",
}
ATTRIBUTES = tuple(COLUMNS.values())
REQUIRED_COLUMNS = tuple(COLUMNS)
MAX_CSV_BYTES = 5 * 1024 * 1024
IMAGES_PER_REQUEST = 4
MAX_IMAGES_PER_BRIEF = 12  # ASSUMPTIONS.md: attempt cap, per brief version
MAX_ATTEMPTS_PER_BRIEF = 2 * MAX_IMAGES_PER_BRIEF  # More options only: failed attempts count here
# Upper end of Luma's published uni-1 reference-image price range (USD per image).
EST_COST_PER_IMAGE_USD = 0.0644
GENERATION_STATUSES = (
    "never_generated", "changed_since_generation", "already_generated", "missing_input"
)
ELIGIBLE = ("never_generated", "changed_since_generation")
ACTIVE = ("queued", "processing")
# The dashboard's tabs, in order: (id, label, stages). Every stage belongs to exactly one tab.
TABS = (
    ("generate", "To generate", ("ready", "failed")),
    ("generating", "Generating", ("generating",)),
    ("post", "Not posted", ("post_failed",)),
    ("ellie", "With Ellie", ("with_ellie",)),
    ("drive", "To Drive", ("approved", "saving")),
    ("done", "In Drive", ("in_drive",)),
    ("input", "Missing attributes", ("needs_input",)),
)


def attributes(row: dict) -> dict:
    return {key: row[key] for key in ATTRIBUTES}


def changes_between(before: dict, after: dict) -> dict:
    return {
        key: {"before": before.get(key), "after": after[key]}
        for key in ATTRIBUTES if before.get(key) != after[key]
    }


def input_issues(row: dict) -> list[str]:
    issues = []
    if not row["sku"]:
        issues.append("Missing SKU: provide a unique SKU before confirming.")
    if not row["product_name"]:
        issues.append("Missing product name")
    if not row["shot_idea"]:
        issues.append("Missing Shot Idea")
    photo = row["photo"]
    if not photo:
        issues.append("Missing source photo")
    else:
        try:
            url = urlsplit(photo)
            valid = url.scheme in ("http", "https") and bool(url.hostname)
            valid = valid and not any(char.isspace() for char in photo)
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            issues.append("Source photo must be a valid HTTP or HTTPS URL")
    return issues


def parse_catalog(content: bytes, filename: str) -> list[dict]:
    """Parse all nine columns; incomplete records must never clear saved fields."""
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Choose a CSV file.")
    if not content:
        raise HTTPException(status_code=400, detail="The CSV file is empty.")
    if len(content) > MAX_CSV_BYTES:
        raise HTTPException(status_code=413, detail="CSV files must be 5 MB or smaller.")
    try:
        csv_text = content.decode("utf-8-sig")
        if "\x00" in csv_text:
            raise HTTPException(status_code=400, detail="CSV contains invalid null characters.")
        reader = csv.DictReader(io.StringIO(csv_text, newline=""), strict=True)
        headers = reader.fieldnames or []
        missing = [column for column in REQUIRED_COLUMNS if column not in headers]
        if missing:
            raise HTTPException(status_code=400, detail=f"Missing required columns: {', '.join(missing)}.")
        if len(headers) != len(set(headers)):
            raise HTTPException(status_code=400, detail="CSV columns must have unique names.")
        rows = []
        for record in reader:
            if None in record or any(value is None for value in record.values()):
                raise HTTPException(
                    status_code=400,
                    detail=f"Row ending at line {reader.line_num} has a different number of cells than the header. Supply all nine columns, including blank cells.",
                )
            rows.append({
                **{key: record[column].strip() for column, key in COLUMNS.items()},
                "row_number": reader.line_num,
            })
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded.") from exc
    except csv.Error as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse CSV: {exc}.") from exc
    if not rows:
        raise HTTPException(status_code=400, detail="The CSV has no product rows.")
    sku_counts = Counter(row["sku"] for row in rows if row["sku"])
    for row in rows:
        row["issues"] = input_issues(row)
        lowercase = row["sku"] != row["sku"].upper()
        row["change_type"] = "invalid" if not row["sku"] or sku_counts[row["sku"]] > 1 or lowercase else "new"
        if row["sku"] and sku_counts[row["sku"]] > 1:
            row["issues"].append(f"Duplicate SKU '{row['sku']}': keep only one row per SKU before confirming.")
        if lowercase:  # ASSUMPTIONS.md: SKUs are uppercase, so each maps to one Drive filename.
            row["issues"].append(f"SKU '{row['sku']}' has lowercase letters: SKUs must be uppercase.")
    return rows


def product_view(row: dict) -> dict:
    """Derive generation, review and delivery state. An image is outdated when its brief_version is
    lower than the product's; nothing else (price, notes, later approvals) makes it outdated."""
    result = dict(row)
    result["issues"] = list(row.get("issues", input_issues(row)))
    result["ready"] = not result["issues"] and row.get("change_type") != "invalid"
    brief = row.get("brief_version") or 1
    result["images"] = []
    for image in row.get("images", []):
        view = {**image, "outdated": image["brief_version"] < brief,
                # Luma returned it and it is being posted to Slack; the file is already stored.
                "posting": image["status"] == "processing" and image["luma_generation_id"] is not None}
        if image["status"] == "done" or view["posting"]:
            view["image_url"] = f"/api/catalog/images/{image['id']}"
        result["images"].append(view)
    # Only completed images count as generations; queued and failed rows are just attempts.
    done = [image for image in result["images"] if image["status"] == "done"]
    current = [image for image in done if not image["outdated"]]
    result["image_exists"] = bool(done)
    result["brief_changed"] = bool(done) and not current
    result["generation_changes"] = {}
    if result["brief_changed"]:
        latest = max(done, key=lambda image: (image["generated_at"], image["id"]))
        result["generation_changes"] = brief_changes(latest["generated_from"], row)
    if not result["ready"]:
        status = "missing_input"
    elif current:
        status = "already_generated"
    elif done:
        status = "changed_since_generation"
    else:
        status = "never_generated"
    result["generation_status"] = status
    result.update(review_summary(result["images"]))
    result.update(delivery_summary(done))
    result["can_generate"] = status in ELIGIBLE and not result["generating"] and not result["delivering"]
    # Failed attempts don't count against the cap; outdated images belong to an earlier brief's count.
    result["images_used"] = sum(image["status"] != "failed" and not image["outdated"] for image in result["images"])
    result["cap_reached"] = result["images_used"] + IMAGES_PER_REQUEST > MAX_IMAGES_PER_BRIEF
    # More options is also bounded counting failures, or a failing Luma could be retried from Slack forever.
    attempts = sum(not image["outdated"] for image in result["images"])
    result["attempts_exhausted"] = attempts + IMAGES_PER_REQUEST > MAX_ATTEMPTS_PER_BRIEF
    # More options: another batch for a brief that already has candidates and can still take an approval.
    result["more_options_open"] = (
        status == "already_generated" and not result["approval_limit_reached"]
        and not result["cap_reached"] and not result["attempts_exhausted"])
    result["can_request_more"] = result["more_options_open"] and not result["generating"] and not result["delivering"]
    result["stage"] = product_stage(result)
    return result


def product_stage(view: dict) -> str:
    """The one next step for a product (state_machine_plan.md). Order matters: the first match wins."""
    if view["generating"]:
        return "generating"
    if view["delivering"]:
        return "saving"
    if view["can_deliver"]:
        return "approved"
    if view["review_status"] == "awaiting_approval":
        return "with_ellie"
    if view["can_send"]:
        return "post_failed"
    # The most recent request's candidates: the newest IMAGES_PER_REQUEST images by version.
    batch = sorted(view["images"], key=lambda image: image["version"], reverse=True)[:IMAGES_PER_REQUEST]
    if view["can_generate"] and any(image["status"] == "failed" for image in batch):
        return "failed"
    if view["delivery_status"] == "delivered":
        return "in_drive"
    return "ready" if view["can_generate"] else "needs_input"


def brief_changes(before: dict, after: dict) -> dict:
    return {key: change for key, change in changes_between(before, after).items() if key in BRIEF_ATTRIBUTES}


def review_summary(images: list[dict]) -> dict:
    """Derive review state from the images; nothing is stored on the product.

    A generation's candidates stay queued/processing until they are posted, so "generating" covers posting.
    Approvals are counted for the current brief only: review is "approved" (ready for Drive) at
    MIN_APPROVED_PER_BRIEF and closed at MAX_APPROVED_PER_BRIEF; until then more can be posted and approved.
    """
    current = [image for image in images if not image["outdated"]]
    approved = sorted((image for image in images if (image["review"] or {}).get("state") == "approved"),
                      key=lambda image: image["review"]["approved_at"])
    states = {image["review"]["state"] for image in current if image["review"]}
    count = sum(not image["outdated"] for image in approved)
    full = count >= MAX_APPROVED_PER_BRIEF
    generating = any(image["status"] in ACTIVE for image in images)
    # Done candidates for the current brief that never reached Slack; Retry posting sends them.
    unposted = [image for image in current if image["status"] == "done" and image["review"] is None]
    return {
        "generating": generating,
        "review_status": "approved" if count >= MIN_APPROVED_PER_BRIEF else "awaiting_approval" if states else None,
        "approved_count": count,
        "approvals_max": MAX_APPROVED_PER_BRIEF,
        "approval_limit_reached": full,
        "approved_image_id": approved[-1]["id"] if approved else None,
        "approved_image_ids": [image["id"] for image in approved],
        "post_error": next((image["post_error"] for image in unposted if image["post_error"]), None),
        "reviewable_image_ids": [] if full else [image["id"] for image in unposted],
        "can_send": bool(unposted) and not full and not generating,
    }


def delivery_summary(done: list[dict]) -> dict:
    """Derive Drive state over every approved image, of any brief; nothing is stored on the product."""
    approved = sorted((image for image in done if (image["review"] or {}).get("state") == "approved"),
                      key=lambda image: image["review"]["approved_at"])
    pending = [image["delivery"] for image in approved if (image["delivery"] or {}).get("state") == "pending"]
    delivered = [image["delivery"] for image in approved if (image["delivery"] or {}).get("state") == "delivered"]
    # A pending row without an error is in flight; one with an error is waiting for a retry.
    delivering = any(not delivery["error"] for delivery in pending)
    unsaved = [image for image in approved if not image["delivery"] or image["delivery"]["state"] == "pending"]
    return {
        "delivery_status": "pending" if pending else "delivered" if delivered else None,
        "delivery_error": next((delivery["error"] for delivery in pending if delivery["error"]), None),
        "drive_url": delivered[-1]["drive_url"] if delivered else None,
        "delivering": delivering,
        "unsaved_image_ids": [image["id"] for image in unsaved],
        "can_deliver": bool(unsaved) and not delivering,
    }


def summarize(rows: list[dict]) -> dict:
    with_idea = sum(bool(row["shot_idea"]) for row in rows)
    return {
        "rows": rows, "total_rows": len(rows),
        "generation_config": {"images_per_request": IMAGES_PER_REQUEST, "est_cost_per_image_usd": EST_COST_PER_IMAGE_USD}, "with_shot_idea": with_idea,
        "without_shot_idea": len(rows) - with_idea,
        # Eligibility is independent of whether a matching image already exists.
        "ready_to_generate": sum(bool(row["ready"]) for row in rows),
        "with_issues": sum(bool(row["issues"]) for row in rows),
        "generation_summary": {
            status: sum(row["generation_status"] == status for row in rows)
            for status in GENERATION_STATUSES
        },
        "tabs": [
            {"id": tab, "label": label, "stages": list(stages),
             "count": sum(row.get("stage") in stages for row in rows)}  # Previews saved before stages have none.
            for tab, label, stages in TABS
        ],
    }


def get_catalog(*, candidates_only: bool = False) -> dict:
    with database() as connection:
        rows = [product_view(row) for row in products.get_products(connection)]
    if candidates_only:
        rows = [row for row in rows if row["generation_status"] in ELIGIBLE]
    return summarize(rows)


def record_generated_image(
    sku: str, generated_from: dict, storage_key: str, luma_generation_id: str | None = None
) -> dict:
    """Record the exact inputs used by a completed generation, even after edits."""
    if set(generated_from) != set(ATTRIBUTES) or generated_from.get("sku") != sku:
        raise ValueError("generated_from must contain the exact nine generation input attributes for this SKU")
    with database() as connection:
        return products.save_generated_image(
            connection, sku, dict(generated_from), storage_key, luma_generation_id
        )


def local_image_path(image_id: str) -> Path:
    with database() as connection:
        image = next((
            image for product in products.get_products(connection)
            for image in product["images"] if image["id"] == image_id
        ), None)
    if image is None:
        raise HTTPException(status_code=404, detail="Generated image not found.")
    root = (data_directory() / "images").resolve()
    key = Path(image["storage_key"])
    path = (root / key).resolve()
    if key.is_absolute() or not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="Generated image file is unavailable.")
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}:
        raise HTTPException(status_code=404, detail="Generated image format is unavailable.")
    return path
