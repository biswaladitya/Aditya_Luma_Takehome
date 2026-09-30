"""SQLite catalog storage. Call repository functions within ``database()``."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1


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
        generated_at TEXT NOT NULL
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
)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version != 0:
        raise RuntimeError(f"Unsupported catalog schema version: {version}")
    existing = connection.execute(
        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    if existing is not None:
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
