"""Durable import review and atomic optimistic catalog updates."""

from litestar.exceptions import HTTPException

from backend.db import database
from backend.repositories import products
from backend.services.catalog import attributes, changes_between, parse_catalog, product_view, summarize


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
            row["images"] = saved["images"] if saved else []
            row["version"] = saved["version"] if saved else None
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
        for row in pending["rows"]:
            products.save_product(connection, attributes(row))
        # Preserve the original review. GET /api/catalog returns current versions.
        result = {**result, "status": "applied", "can_confirm": False}
        products.mark_import_applied(connection, preview_id, result)
        return result
