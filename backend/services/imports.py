"""Durable import review and atomic optimistic catalog updates."""

from litestar.exceptions import HTTPException

from backend.db import database
from backend.repositories import products, reviews
from backend.services import review
from backend.services.catalog import attributes, brief_changes, changes_between, parse_catalog, product_view, summarize

BRIEF_CASES = ("new", "info_only", "unchanged", "invalid", "not_generated", "with_ellie",
               "approved_not_in_drive", "in_drive")


def brief_case(row: dict, saved: dict | None) -> str:
    """What accepting this row does to the product; see state_machine_plan.md."""
    if row["change_type"] in ("invalid", "new", "unchanged"):
        return row["change_type"]
    if not brief_changes(saved, row):
        return "info_only"
    view = product_view(saved)
    # Same precedence as productStage in catalogApi.ts.
    if any((image["delivery"] or {}).get("state") != "delivered"
           for image in view["images"] if image["id"] in view["approved_image_ids"]):
        return "approved_not_in_drive"  # The approvals are kept and can still be saved.
    if view["generation_status"] == "already_generated" and view["review_status"] != "approved":
        return "with_ellie"  # Its candidates become outdated, even after an earlier brief reached Drive.
    if view["delivery_status"] == "delivered":
        return "in_drive"
    return "not_generated"


def candidates_outdated(saved: dict) -> int:
    """Waiting candidates a brief change takes from Ellie; an approved product can still have some."""
    view = product_view(saved)
    if view["approval_limit_reached"]:
        return 0
    return sum(not image["outdated"] and (image["review"] or {}).get("state") == "awaiting_approval"
               for image in view["images"])


def busy(saved: dict) -> bool:
    """A generation (which includes posting to Slack) or a Drive save is in flight."""
    view = product_view(saved)
    return view["generating"] or view["delivering"]


def import_view(pending: dict) -> dict:
    if pending["status"] == "applied" and pending["result"] is not None:
        return pending["result"]
    rows = pending["rows"]
    invalid = [row for row in rows if row["change_type"] == "invalid"]
    return {
        **summarize(rows),
        "preview_id": pending["id"], "filename": pending["filename"],
        "status": pending["status"], "created_at": pending["created_at"],
        "expected_versions": pending["expected_versions"],
        "can_confirm": bool(rows) and not invalid,
        **{f"{kind}_count": sum(row["change_type"] == kind for row in rows)
           for kind in ("new", "changed", "unchanged", "invalid")},
        "brief_case_counts": {case: sum(row.get("brief_case") == case for row in rows) for case in BRIEF_CASES},
        "errors": [f"Row {row['row_number']}: {'; '.join(row['issues'])}" for row in invalid],
        "rows_with_shot_idea": [row for row in rows if row["shot_idea"]],
    }


def preview_catalog_import(content: bytes, filename: str) -> dict:
    incoming = parse_catalog(content, filename)
    with database() as connection:
        current = {row["sku"]: row for row in products.get_products(
            connection, [row["sku"] for row in incoming if row["sku"]]
        )}
        rows = []
        versions = {}
        for row in incoming:
            saved = current.get(row["sku"])
            versions[row["sku"]] = saved["version"] if saved else None
            row["changes"] = changes_between(saved, row) if saved else {}
            if row["change_type"] != "invalid":
                row["change_type"] = "new" if saved is None else "changed" if row["changes"] else "unchanged"
            row["brief_case"] = brief_case(row, saved)
            row["candidates_outdated"] = candidates_outdated(saved) if saved and brief_changes(saved, row) else 0
            row["images"] = saved["images"] if saved else []
            row["version"] = saved["version"] if saved else None
            # As the product would be once accepted, so outdated images show as outdated.
            row["brief_version"] = (saved["brief_version"] + bool(brief_changes(saved, row))) if saved else 1
            rows.append(product_view(row))
        pending = products.create_pending_import(connection, filename, rows, versions)
        return import_view(pending)


def get_catalog_import(preview_id: str) -> dict:
    with database() as connection:
        pending = products.get_pending_import(connection, preview_id)
        if pending is None:
            raise HTTPException(status_code=404, detail="Import preview not found. Upload the CSV to review it again.")
        return import_view(pending)


def confirm_catalog_import(preview_id: str) -> dict:
    # database() holds BEGIN IMMEDIATE through all reads, product writes, and the
    # applied marker. A stale preview can never partially overwrite the catalog.
    with database() as connection:
        pending = products.get_pending_import(connection, preview_id)
        if pending is None:
            raise HTTPException(status_code=404, detail="Import preview not found. Upload the CSV to review it again.")
        if pending["status"] == "applied":
            return pending["result"]
        result = import_view(pending)
        if not result["can_confirm"]:
            raise HTTPException(status_code=400, detail="Correct missing or duplicate SKUs and upload the CSV again. " + " ".join(result["errors"]))
        current = {row["sku"]: row for row in products.get_products(connection, list(pending["expected_versions"]))}
        conflicts = [
            sku for sku, expected in pending["expected_versions"].items()
            if (current[sku]["version"] if sku in current else None) != expected
        ]
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail=f"Catalog changed for SKU(s): {', '.join(conflicts)}. Upload the CSV again for a refreshed review before confirming.",
            )
        rebriefed = [row["sku"] for row in pending["rows"] if row["sku"] in current and brief_changes(current[row["sku"]], row)]
        waiting = [sku for sku in rebriefed if busy(current[sku])]
        if waiting:
            raise HTTPException(
                status_code=409,
                detail=f"Generation, posting to Slack or saving to Drive is in progress for SKU(s): {', '.join(waiting)}. "
                       "Wait for it to finish, then accept the changes again.",
            )
        for row in pending["rows"]:
            products.save_product(connection, attributes(row))
        # Their Approve buttons in Slack are removed after the commit; Approve is refused regardless.
        # Only the brief being replaced still has Approve buttons: older ones are already marked, and
        # a brief at its approval limit shows "Not selected" on the rest.
        outdated = []
        for sku in rebriefed:
            items = [item for item in reviews.reviews_for_sku(connection, sku)
                     if item["brief_version"] == current[sku]["brief_version"]]
            if sum(item["state"] == "approved" for item in items) < reviews.MAX_APPROVED_PER_BRIEF:
                outdated += [item for item in items if item["state"] == "awaiting_approval"]
        # Preserve the original review. GET /api/catalog returns current versions.
        result = {**result, "status": "applied", "can_confirm": False}
        products.mark_import_applied(connection, preview_id, result)
    review.mark_outdated(outdated)
    return result
