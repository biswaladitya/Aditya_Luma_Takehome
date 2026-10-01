"""SQLite catalog storage. Call repository functions within ``database()``."""

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 7


def _absolute_path(value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def database_path() -> Path:
    """Use DATABASE_PATH, or catalog.sqlite3 in DATA_DIR (default: runtime).

    Relative configuration paths are anchored to the project, independent of cwd.
    Calling this function does not create files or directories.
    """
    if configured := os.environ.get("DATABASE_PATH"):
        return _absolute_path(configured)
    return _absolute_path(os.environ.get("DATA_DIR") or "runtime") / "catalog.sqlite3"


def data_directory() -> Path:
    """Local storage root; image storage keys are relative to its images folder."""
    if configured := os.environ.get("DATA_DIR"):
        return _absolute_path(configured)
    return database_path().parent


# Review state lives beside generated_images, never inside it. See persistence.md.
# This is the v3-v6 shape; v7 rebuilds the table (_BRIEF_REVIEW_SCHEMA).
_REVIEW_SCHEMA = (
    """CREATE TABLE image_reviews (
        image_id TEXT PRIMARY KEY NOT NULL REFERENCES generated_images(id) ON DELETE RESTRICT,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE RESTRICT,
        state TEXT NOT NULL DEFAULT 'pending_send'
            CHECK (state IN ('pending_send', 'awaiting_approval', 'approved')),
        slack_channel TEXT,
        slack_thread_ts TEXT,
        slack_message_ts TEXT,
        slack_file_id TEXT,
        send_error TEXT,
        approved_by TEXT,
        approved_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK ((state = 'pending_send' AND slack_message_ts IS NULL)
            OR (state = 'awaiting_approval' AND slack_message_ts IS NOT NULL)
            OR (state = 'approved' AND slack_message_ts IS NOT NULL
                AND approved_by IS NOT NULL AND approved_at IS NOT NULL))
    )""",
    "CREATE INDEX image_reviews_product_sku ON image_reviews(product_sku)",
    # MVP rule: at most one approved image per product, enforced by the database. v6 narrows it to live approvals.
    "CREATE UNIQUE INDEX image_reviews_one_approved_per_sku ON image_reviews(product_sku) WHERE state = 'approved'",
    """CREATE TRIGGER image_reviews_forward_only
        BEFORE UPDATE OF state ON image_reviews
        WHEN NEW.state IS NOT OLD.state
            AND NOT (OLD.state = 'pending_send' AND NEW.state = 'awaiting_approval')
            AND NOT (OLD.state = 'awaiting_approval' AND NEW.state = 'approved')
        BEGIN
            SELECT RAISE(ABORT, 'review state can only move forward');
        END""",
)

# v6: an accepted CSV change replaced ("superseded") an approval not yet in Drive. v7 retires the rule.
_SUPERSEDE_SCHEMA = (
    "ALTER TABLE image_reviews ADD COLUMN superseded_at TEXT CHECK (superseded_at IS NULL OR state = 'approved')",
    # At most one live (not superseded) approval per product.
    "DROP INDEX image_reviews_one_approved_per_sku",
    "CREATE UNIQUE INDEX image_reviews_one_approved_per_sku ON image_reviews(product_sku) "
    "WHERE state = 'approved' AND superseded_at IS NULL",
    """CREATE TRIGGER image_reviews_supersede_once
        BEFORE UPDATE OF superseded_at ON image_reviews
        WHEN NEW.superseded_at IS NOT OLD.superseded_at
            AND NOT (OLD.superseded_at IS NULL AND NEW.superseded_at IS NOT NULL AND OLD.state = 'approved')
        BEGIN
            SELECT RAISE(ABORT, 'superseded_at can only be set once, on an approved review');
        END""",
)

# v7: brief versions. Reviews exist only for posted candidates and remember the image's brief version,
# so the database allows one approval per product and brief. See persistence.md.
_BRIEF_REVIEW_SCHEMA = (
    """CREATE TABLE image_reviews (
        image_id TEXT PRIMARY KEY NOT NULL REFERENCES generated_images(id) ON DELETE RESTRICT,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE RESTRICT,
        brief_version INTEGER NOT NULL CHECK (brief_version >= 1),
        state TEXT NOT NULL DEFAULT 'awaiting_approval' CHECK (state IN ('awaiting_approval', 'approved')),
        slack_channel TEXT,
        slack_thread_ts TEXT,
        slack_message_ts TEXT NOT NULL,
        slack_file_id TEXT,
        approved_by TEXT,
        approved_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (state = 'awaiting_approval' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
    )""",
    "CREATE INDEX image_reviews_product_sku ON image_reviews(product_sku)",
    "CREATE UNIQUE INDEX image_reviews_one_approved_per_brief ON image_reviews(product_sku, brief_version) "
    "WHERE state = 'approved'",
    """CREATE TRIGGER image_reviews_forward_only
        BEFORE UPDATE OF state ON image_reviews
        WHEN NEW.state IS NOT OLD.state
            AND NOT (OLD.state = 'awaiting_approval' AND NEW.state = 'approved')
        BEGIN
            SELECT RAISE(ABORT, 'review state can only move forward');
        END""",
)
# Frozen with the migration: the product fields sent to Luma.
_BRIEF = ("photo", "shot_idea", "product_name", "color", "material")

# Drive delivery of approved images; review state is never changed by it. See google_drive_writeback.md.
_DELIVERY_SCHEMA = (
    """CREATE TABLE drive_deliveries (
        image_id TEXT PRIMARY KEY NOT NULL REFERENCES generated_images(id) ON DELETE RESTRICT,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE RESTRICT,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'delivered')),
        filename TEXT NOT NULL CHECK (length(filename) > 0),
        drive_file_id TEXT,
        drive_url TEXT,
        delivered_at TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (state = 'pending' OR (drive_file_id IS NOT NULL
            AND drive_url IS NOT NULL AND delivered_at IS NOT NULL))
    )""",
    "CREATE INDEX drive_deliveries_product_sku ON drive_deliveries(product_sku)",
    """CREATE TRIGGER drive_deliveries_forward_only
        BEFORE UPDATE OF state ON drive_deliveries
        WHEN NEW.state IS NOT OLD.state
            AND NOT (OLD.state = 'pending' AND NEW.state = 'delivered')
        BEGIN
            SELECT RAISE(ABORT, 'delivery state can only move forward');
        END""",
)

_SCHEMA = (
    """CREATE TABLE products (
        sku TEXT PRIMARY KEY NOT NULL CHECK (length(trim(sku)) > 0),
        product_name TEXT NOT NULL,
        category TEXT NOT NULL,
        color TEXT NOT NULL,
        material TEXT NOT NULL,
        price TEXT NOT NULL,
        photo TEXT NOT NULL,
        shot_idea TEXT NOT NULL,
        notes TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE generated_images (
        id TEXT PRIMARY KEY NOT NULL,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE RESTRICT,
        generated_from TEXT NOT NULL,
        storage_key TEXT NOT NULL CHECK (length(storage_key) > 0),
        luma_generation_id TEXT,
        generated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
        status TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('queued', 'processing', 'done', 'failed')),
        error TEXT
    )""",
    "CREATE INDEX generated_images_product_sku ON generated_images(product_sku)",
    """CREATE TRIGGER generated_images_immutable_snapshot
        BEFORE UPDATE OF generated_from ON generated_images
        WHEN NEW.generated_from IS NOT OLD.generated_from
        BEGIN
            SELECT RAISE(ABORT, 'generated_from snapshots are immutable');
        END""",
    """CREATE TABLE pending_imports (
        id TEXT PRIMARY KEY NOT NULL,
        filename TEXT NOT NULL,
        rows TEXT NOT NULL,
        expected_versions TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'applied')),
        created_at TEXT NOT NULL,
        applied_at TEXT,
        result TEXT,
        CHECK ((status = 'pending' AND applied_at IS NULL AND result IS NULL)
            OR (status = 'applied' AND applied_at IS NOT NULL AND result IS NOT NULL))
    )""",
) + _REVIEW_SCHEMA + _DELIVERY_SCHEMA


def _ensure_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version == 0:
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if existing is not None:
            raise RuntimeError("Refusing to initialize a nonempty, unversioned database")
        # executescript implicitly commits; individual statements keep DDL atomic.
        # Fresh installs replay the v5 tables and every later step, so both paths end with the same schema.
        for statement in _SCHEMA + _SUPERSEDE_SCHEMA:
            connection.execute(statement)
        _upgrade_to_v7(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return
    # Upgrade one version at a time so older databases pass through every step.
    if version == 1:
        # v1 -> v2: generation lifecycle columns; existing rows were completed images.
        connection.execute("ALTER TABLE generated_images ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        connection.execute("ALTER TABLE generated_images ADD COLUMN status TEXT NOT NULL DEFAULT 'done'")
        connection.execute("ALTER TABLE generated_images ADD COLUMN error TEXT")
        version = 2
    if version == 2:
        # v2 -> v3: Slack review state; existing images simply have no review row yet.
        for statement in _REVIEW_SCHEMA:
            connection.execute(statement)
        version = 3
    if version == 3:
        # v3 -> v4: Drive delivery state; nothing has been delivered yet.
        for statement in _DELIVERY_SCHEMA:
            connection.execute(statement)
        version = 4
    if version == 4:
        # v4 -> v5: files go to the signed-in user's My Drive root, so drop drive_folder_id.
        # SQLite cannot drop a column named in a CHECK, so rebuild the table and keep its rows.
        connection.execute("DROP TRIGGER drive_deliveries_forward_only")
        connection.execute("DROP INDEX drive_deliveries_product_sku")
        connection.execute("ALTER TABLE drive_deliveries RENAME TO drive_deliveries_v4")
        for statement in _DELIVERY_SCHEMA:
            connection.execute(statement)
        columns = ("image_id, product_sku, state, filename, drive_file_id, drive_url, "
                   "delivered_at, error, created_at, updated_at")
        connection.execute(f"INSERT INTO drive_deliveries ({columns}) SELECT {columns} FROM drive_deliveries_v4")
        connection.execute("DROP TABLE drive_deliveries_v4")
        version = 5
    if version == 5:
        # v5 -> v6: existing approvals stay live; nothing has been superseded yet.
        for statement in _SUPERSEDE_SCHEMA:
            connection.execute(statement)
        version = 6
    if version == 6:
        _upgrade_to_v7(connection)
        version = 7
    if version != SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported catalog schema version: {version}")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _upgrade_to_v7(connection: sqlite3.Connection) -> None:
    """v6 -> v7: brief versions replace superseded approvals, and posting moves onto generated_images.

    Keeps every image, approval and delivery. Superseded approvals become ordinary approvals of their
    (older) brief; unposted pending_send reviews are removed and their error moves to post_error.
    """
    for statement in ("DROP TRIGGER image_reviews_forward_only", "DROP TRIGGER image_reviews_supersede_once",
                      "DROP INDEX image_reviews_product_sku", "DROP INDEX image_reviews_one_approved_per_sku",
                      "ALTER TABLE image_reviews RENAME TO image_reviews_v6",
                      "ALTER TABLE products ADD COLUMN brief_version INTEGER NOT NULL DEFAULT 1 CHECK (brief_version >= 1)",
                      "ALTER TABLE generated_images ADD COLUMN brief_version INTEGER NOT NULL DEFAULT 1 "
                      "CHECK (brief_version >= 1)",
                      "ALTER TABLE generated_images ADD COLUMN post_error TEXT"):
        connection.execute(statement)
    _backfill_brief_versions(connection)
    for statement in _BRIEF_REVIEW_SCHEMA:
        connection.execute(statement)
    columns = ("image_id, product_sku, state, slack_channel, slack_thread_ts, slack_message_ts, "
               "slack_file_id, approved_by, approved_at, created_at, updated_at")
    connection.execute(
        f"INSERT INTO image_reviews (brief_version, {columns}) "
        f"SELECT (SELECT brief_version FROM generated_images WHERE id = image_id), {columns} "
        "FROM image_reviews_v6 WHERE state != 'pending_send'")
    connection.execute(
        "UPDATE generated_images SET post_error = (SELECT coalesce(send_error, 'Not posted') FROM image_reviews_v6 r "
        "WHERE r.image_id = generated_images.id) "
        "WHERE id IN (SELECT image_id FROM image_reviews_v6 WHERE state = 'pending_send')")
    connection.execute("DROP TABLE image_reviews_v6")
    # v6 named every save <SKU>_styled_01, so an undelivered row could share its name with another
    # image's delivered file and overwrite it. Unsaved rows take the versioned name (delivery_filename);
    # delivered rows keep theirs.
    for image_id, sku, filename, version in connection.execute(
            "SELECT d.image_id, d.product_sku, d.filename, i.version FROM drive_deliveries d "
            "JOIN generated_images i ON i.id = d.image_id WHERE d.state = 'pending'").fetchall():
        extension = Path(filename).suffix.lower().lstrip(".") or "jpg"
        connection.execute("UPDATE drive_deliveries SET filename = ? WHERE image_id = ?",
                           (f"{sku.strip().upper()}_styled_v{version}.{extension}", image_id))


def _backfill_brief_versions(connection: sqlite3.Connection) -> None:
    """Number each product's briefs from its image history, oldest first.

    Images form one group while their saved brief stays the same. An image made after its group's
    approval starts a new group too: v6 allowed that only once the approval was superseded, and
    v7 allows one approval per brief version. The group matching the product's current brief (if
    no superseded approval retired it) is numbered last, so every other image counts as outdated.
    """
    approvals = {row[0]: (row[1], row[2]) for row in connection.execute(
        "SELECT image_id, approved_at, superseded_at FROM image_reviews_v6 WHERE state = 'approved'")}
    for product in connection.execute(f"SELECT sku, {', '.join(_BRIEF)} FROM products").fetchall():
        sku, current = product[0], list(product[1:])
        groups: list[dict] = []
        for image_id, generated_from, generated_at in connection.execute(
                "SELECT id, generated_from, generated_at FROM generated_images "
                "WHERE product_sku = ? ORDER BY generated_at, id", (sku,)).fetchall():
            snapshot = json.loads(generated_from)
            brief = [snapshot.get(key, "") for key in _BRIEF]
            group = groups[-1] if groups else None
            if group is None or group["brief"] != brief or (group["approved_at"] and generated_at > group["approved_at"]):
                group = {"brief": brief, "ids": [], "approved_at": None, "superseded": False}
                groups.append(group)
            group["ids"].append(image_id)
            if image_id in approvals:
                approved_at, superseded_at = approvals[image_id]
                group["approved_at"] = max(group["approved_at"] or "", approved_at)
                group["superseded"] = group["superseded"] or superseded_at is not None
        live = [group for group in groups if group["brief"] == current and not group["superseded"]]
        if live:
            groups.remove(live[-1])
            groups.append(live[-1])
        for number, group in enumerate(groups, start=1):
            connection.executemany("UPDATE generated_images SET brief_version = ? WHERE id = ?",
                                   [(number, image_id) for image_id in group["ids"]])
        connection.execute("UPDATE products SET brief_version = ? WHERE sku = ?",
                           (len(groups) if live else len(groups) + 1, sku))


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    """Ensure schema, lock for a transaction, commit/rollback, and always close.

    Repository operations never commit. Services must read the preview, validate
    expected versions, save products, and mark the import applied in one context.
    BEGIN IMMEDIATE serializes writers before they read expected versions.
    """
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        _ensure_schema(connection)
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
