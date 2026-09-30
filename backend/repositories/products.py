"""Catalog persistence without transaction ownership or external side effects."""

import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

PRODUCT_ATTRIBUTES = (
    "sku", "product_name", "category", "color", "material", "price",
    "photo", "shot_idea", "notes",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _image(row: sqlite3.Row) -> dict:
    image = dict(row)
    image["generated_from"] = json.loads(image["generated_from"])
    return image


def get_products(connection: sqlite3.Connection, skus: list[str] | None = None) -> list[dict]:
    """Return products ordered by SKU, with generation history oldest first.

    None selects the full catalog; an empty list selects no products.
    Queries are chunked to support SQLite builds with low parameter limits.
    """
    if skus is None:
        rows = connection.execute("SELECT * FROM products ORDER BY sku").fetchall()
    else:
        rows = []
        unique_skus = list(dict.fromkeys(skus))
        for offset in range(0, len(unique_skus), 500):
            batch = unique_skus[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows.extend(connection.execute(
                f"SELECT * FROM products WHERE sku IN ({placeholders})", batch
            ).fetchall())
        rows.sort(key=lambda row: row["sku"])
    products = {row["sku"]: {**dict(row), "images": []} for row in rows}
    product_skus = list(products)
    for offset in range(0, len(product_skus), 500):
        batch = product_skus[offset:offset + 500]
        placeholders = ",".join("?" for _ in batch)
        images = connection.execute(
            f"SELECT * FROM generated_images WHERE product_sku IN ({placeholders}) "
            "ORDER BY generated_at, id", batch
        )
        for row in images:
            products[row["product_sku"]]["images"].append(_image(row))
    return list(products.values())


def save_product(connection: sqlite3.Connection, attributes: dict) -> dict:
    """Save exactly nine normalized CSV attributes; only real changes bump version.

    CSV values, including price, are stored as strings. Blank Shot Ideas are valid.
    No fields are normalized here and no image records are replaced.
    """
    if set(attributes) != set(PRODUCT_ATTRIBUTES):
        raise ValueError("Product attributes must contain exactly the nine normalized CSV keys")
    if any(not isinstance(attributes[key], str) for key in PRODUCT_ATTRIBUTES):
        raise ValueError("Normalized CSV attributes must be strings")
    now = _now()
    columns = ", ".join(PRODUCT_ATTRIBUTES)
    assignments = ", ".join(f"{key} = excluded.{key}" for key in PRODUCT_ATTRIBUTES[1:])
    changed = " OR ".join(
        f"products.{key} IS NOT excluded.{key}" for key in PRODUCT_ATTRIBUTES[1:]
    )
    connection.execute(
        f"INSERT INTO products ({columns}, version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
        f"ON CONFLICT(sku) DO UPDATE SET {assignments}, "
        "version = products.version + 1, updated_at = excluded.updated_at "
        f"WHERE {changed}",
        [attributes[key] for key in PRODUCT_ATTRIBUTES] + [now, now],
    )
    return get_products(connection, [attributes["sku"]])[0]


def create_pending_import(
    connection: sqlite3.Connection,
    filename: str,
    rows: list[dict],
    expected_versions: dict[str, int | None],
) -> dict:
    """Store a durable preview without writing any products."""
    preview_id = str(uuid4())
    connection.execute(
        "INSERT INTO pending_imports "
        "(id, filename, rows, expected_versions, status, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (preview_id, filename, _json(rows), _json(expected_versions), _now()),
    )
    return get_pending_import(connection, preview_id)


def get_pending_import(connection: sqlite3.Connection, preview_id: str) -> dict | None:
    row = connection.execute(
        "SELECT * FROM pending_imports WHERE id = ?", (preview_id,)
    ).fetchone()
    if row is None:
        return None
    preview = dict(row)
    for key in ("rows", "expected_versions", "result"):
        if preview[key] is not None:
            preview[key] = json.loads(preview[key])
    return preview


def mark_import_applied(connection: sqlite3.Connection, preview_id: str, result: dict) -> None:
    """Record the first application result; repeats preserve it and its timestamp.

    Raises KeyError for an unknown preview. Version validation is the service's job.
    """
    cursor = connection.execute(
        "UPDATE pending_imports SET status = 'applied', applied_at = ?, result = ? "
        "WHERE id = ? AND status = 'pending'",
        (_now(), _json(result), preview_id),
    )
    if cursor.rowcount == 0 and get_pending_import(connection, preview_id) is None:
        raise KeyError(preview_id)


def save_generated_image(
    connection: sqlite3.Connection,
    sku: str,
    generated_from: dict,
    storage_key: str,
    luma_generation_id: str | None = None,
) -> dict:
    """Append a reference and the exact submitted snapshot, never current attributes.

    Local storage_key values are relative to data_directory() / 'images'.
    This function only stores metadata; it does not read or write image files.
    """
    image_id = str(uuid4())
    connection.execute(
        "INSERT INTO generated_images "
        "(id, product_sku, generated_from, storage_key, luma_generation_id, generated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (image_id, sku, _json(generated_from), storage_key, luma_generation_id, _now()),
    )
    return _image(connection.execute(
        "SELECT * FROM generated_images WHERE id = ?", (image_id,)
    ).fetchone())
