"""Parse a customer CSV into a reviewable catalog preview."""

import csv
import io
from collections import Counter

from litestar.exceptions import HTTPException

REQUIRED_COLUMNS = ("SKU", "Product Name", "Photo", "Shot Idea")
MAX_CSV_BYTES = 5 * 1024 * 1024


def parse_catalog(content: bytes, filename: str) -> dict:
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Choose a CSV file.")
    if not content:
        raise HTTPException(status_code=400, detail="The CSV file is empty.")
    if len(content) > MAX_CSV_BYTES:
        raise HTTPException(status_code=413, detail="CSV files must be 5 MB or smaller.")

    try:
        csv_text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(csv_text, newline=""), strict=True)
        headers = reader.fieldnames or []
        missing = [column for column in REQUIRED_COLUMNS if column not in headers]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required columns: {', '.join(missing)}.",
            )
        if len(headers) != len(set(headers)):
            raise HTTPException(status_code=400, detail="CSV columns must have unique names.")

        rows = []
        for line_number, record in enumerate(reader, start=2):
            if None in record or all(not (value or "").strip() for value in record.values()):
                if None in record:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Row {line_number} has more cells than the header.",
                    )
                continue
            values = {key: (value or "").strip() for key, value in record.items()}
            rows.append(
                {
                    "row_number": line_number,
                    "sku": values["SKU"],
                    "product_name": values["Product Name"],
                    "category": values.get("Category", ""),
                    "color": values.get("Color / Finish", ""),
                    "photo": values["Photo"],
                    "shot_idea": values["Shot Idea"],
                }
            )
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded.") from exc
    except csv.Error as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse CSV: {exc}.") from exc

    sku_counts = Counter(row["sku"] for row in rows if row["sku"])
    for row in rows:
        row["issues"] = []
        if not row["sku"]:
            row["issues"].append("Missing SKU")
        elif sku_counts[row["sku"]] > 1:
            row["issues"].append("Duplicate SKU")
        if not row["product_name"]:
            row["issues"].append("Missing product name")
        if not row["photo"]:
            row["issues"].append("Missing source photo")
        row["ready"] = bool(row["shot_idea"] and not row["issues"])

    with_ideas = [row for row in rows if row["shot_idea"]]
    return {
        "filename": filename,
        "total_rows": len(rows),
        "with_shot_idea": len(with_ideas),
        "without_shot_idea": len(rows) - len(with_ideas),
        "ready_to_generate": sum(row["ready"] for row in rows),
        "with_issues": sum(bool(row["issues"]) for row in rows),
        "rows_with_shot_idea": with_ideas,
    }

