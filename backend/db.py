"""SQLite catalog storage. Call repository functions within ``database()``."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 2


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


_SCHEMA = (
    # The current catalog row per SKU. version goes up on any change; brief_version only when a
    # field sent to Luma changes (photo, shot idea, name, color, material).
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
        brief_version INTEGER NOT NULL DEFAULT 1 CHECK (brief_version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    # One row per Luma candidate. generated_from is the exact input snapshot and never changes.
    # A candidate stays queued/processing until it is generated and posted to Slack.
    """CREATE TABLE generated_images (
        id TEXT PRIMARY KEY NOT NULL,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE RESTRICT,
        generated_from TEXT NOT NULL,
        storage_key TEXT NOT NULL CHECK (length(storage_key) > 0),
        luma_generation_id TEXT,
        generated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
        brief_version INTEGER NOT NULL DEFAULT 1 CHECK (brief_version >= 1),
        status TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('queued', 'processing', 'done', 'failed')),
        error TEXT,
        post_error TEXT
    )""",
    "CREATE INDEX generated_images_product_sku ON generated_images(product_sku)",
    """CREATE TRIGGER generated_images_immutable_snapshot
        BEFORE UPDATE OF generated_from ON generated_images
        WHEN NEW.generated_from IS NOT OLD.generated_from
        BEGIN
            SELECT RAISE(ABORT, 'generated_from snapshots are immutable');
        END""",
    # An uploaded CSV awaiting review; the catalog only changes when it is applied.
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
    # Slack review of a posted candidate. Rows exist only once the post succeeded. The approval limit
    # per product and brief version is enforced by reviews.approve, not here. See persistence.md.
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
    """CREATE TRIGGER image_reviews_forward_only
        BEFORE UPDATE OF state ON image_reviews
        WHEN NEW.state IS NOT OLD.state
            AND NOT (OLD.state = 'awaiting_approval' AND NEW.state = 'approved')
        BEGIN
            SELECT RAISE(ABORT, 'review state can only move forward');
        END""",
    # Drive save of an approved image; review state is never changed by it. See google_drive_writeback.md.
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


def _ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the schema in an empty database, or apply the one upgrade (1 to 2); other versions are refused."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version == 1:  # Version 2 allows several approvals per brief: only the unique index goes, rows are kept.
        connection.execute("DROP INDEX IF EXISTS image_reviews_one_approved_per_brief")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return
    if version > SCHEMA_VERSION:  # An older build after a rollback: the data is fine, the code is behind.
        raise RuntimeError(
            f"Unsupported catalog schema version: {version}. {database_path()} was written by a newer version "
            "of the app; deploy that version, or restore a backup taken before the upgrade.")
    if version != 0:
        raise RuntimeError(
            f"Unsupported catalog schema version: {version}. Delete {database_path()} to start a fresh catalog.")
    if connection.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
        raise RuntimeError("Refusing to initialize a nonempty, unversioned database")
    # executescript implicitly commits; individual statements keep DDL atomic.
    for statement in _SCHEMA:
        connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


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
