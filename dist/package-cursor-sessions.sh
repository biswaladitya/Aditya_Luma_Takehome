#!/bin/bash

# Package Cursor session databases for the current repo into gzipped copies.
#
# Cursor stores conversation history in two places:
#   - Global conversation rows in <CURSOR_BASE>/globalStorage/state.vscdb,
#     under the `cursorDiskKV` table as `composerData:<uuid>` and
#     `bubbleId:<uuid>:<bubble>` keys.
#   - Per-workspace session lists in <CURSOR_BASE>/workspaceStorage/<hash>/
#     state.vscdb, under the `ItemTable` table. Keys like
#     `composer.composerData`, `workbench.panel.aichat.view.aichat.chatdata`,
#     and `workbench.backgroundComposer.persistentData` contain JSON that
#     enumerates the composerIds that belong to that workspace. The
#     accompanying workspace.json maps the hash to a project folder.
#
# Primary path: match workspace.json folder against SEARCH_ROOT, read
# composerIds from that workspace's ItemTable, then pull exactly those rows
# out of the global cursorDiskKV. This works even when the candidate never
# @-mentioned a take-home file (which the old text-search filter required).
#
# Fallback path (no matching workspace at all): search the global DB for any
# rows whose value text contains the SEARCH_ROOT path. Used when the
# candidate's project was moved/renamed since Cursor recorded the workspace,
# or when workspaceStorage is missing.
#
# IMPORTANT: We extract only conversation data (composerData + bubbleId rows)
# and exclude large blob entries (agentKv:blob:*, checkpointId:*, etc.) which
# can be >5GB. This reduces the packaged size from ~1GB+ to ~10-50MB.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
# SESSION_SEARCH_ROOT overrides where we look for cwd matches — defaults to
# the current repo root. Candidates who used Cursor from a different dir can
# set this to point at the actual working directory.
SEARCH_ROOT="${SESSION_SEARCH_ROOT:-$REPO_ROOT}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUTPUT_DIR="${1:-$REPO_ROOT/dist}"

# Detect platform
case "$(uname -s)" in
    Darwin)
        CURSOR_BASE="${CURSOR_BASE:-$HOME/Library/Application Support/Cursor/User}"
        ;;
    Linux)
        CURSOR_BASE="${CURSOR_BASE:-$HOME/.config/Cursor/User}"
        ;;
    *)
        echo "Unsupported platform: $(uname -s)"
        exit 1
        ;;
esac

if [ ! -d "$CURSOR_BASE" ]; then
    echo "Cursor data directory not found at $CURSOR_BASE"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

# Read a workspace state.vscdb ItemTable and emit composer UUIDs (one per
# line) found in any of the keys Cursor uses to enumerate per-workspace
# composer/agent/chat sessions. Defensive: extracts UUIDs via regex from the
# JSON values rather than assuming a stable schema, since the keys' shapes
# differ across Cursor versions.
emit_workspace_composer_ids() {
    local ws_db="$1"
    python3 - "$ws_db" <<'PYEOF'
import sqlite3, sys, re

UUID_RE = re.compile(
    r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'
)

# Keys that have historically held the workspace's composer/chat session list.
# composer.composerData is the current-format primary list. The aichat key
# is the legacy chat list. backgroundComposer.persistentData and the panel
# keys are belt-and-suspenders in case Cursor rotates schemas.
SESSION_LIST_KEYS_EQ = (
    "composer.composerData",
    "workbench.panel.aichat.view.aichat.chatdata",
    "workbench.backgroundComposer.persistentData",
)
SESSION_LIST_KEYS_LIKE = (
    "workbench.panel.composer%",
    "workbench.panel.aichat%",
)

try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
except sqlite3.OperationalError:
    sys.exit(0)

# Workspace state.vscdb uses `ItemTable` (key TEXT, value BLOB). Older Cursor
# may not have it at all; guard for that.
try:
    conn.execute("SELECT 1 FROM ItemTable LIMIT 1")
except sqlite3.OperationalError:
    conn.close()
    sys.exit(0)

values = []
for k in SESSION_LIST_KEYS_EQ:
    row = conn.execute(
        "SELECT value FROM ItemTable WHERE key = ?", (k,)
    ).fetchone()
    if row and row[0] is not None:
        values.append(row[0])

for pattern in SESSION_LIST_KEYS_LIKE:
    for row in conn.execute(
        "SELECT value FROM ItemTable WHERE key LIKE ?", (pattern,)
    ):
        if row[0] is not None:
            values.append(row[0])

conn.close()

ids = set()
for v in values:
    if isinstance(v, (bytes, bytearray)):
        try:
            v = v.decode("utf-8", errors="replace")
        except Exception:
            continue
    ids.update(UUID_RE.findall(v))

for cid in sorted(ids):
    print(cid)
PYEOF
}

# Extract composerData + bubbleId rows for the UNION of two signals:
#   1. workspace_ids_file — composer UUIDs Cursor enumerated for matching
#      workspaces (one UUID per line; file may be empty).
#   2. repo_path text search — sessions whose composerData or bubble values
#      mention repo_path as a substring.
#
# Both signals are necessary. Workspace enumeration catches sessions where
# the candidate never @-mentioned a take-home file. Text search catches
# sessions Cursor has dropped from its workspace `composer.composerData`
# enumeration (only stores a recent window; older / background / aborted
# sessions live in global cursorDiskKV but aren't in any workspace list).
# Real-world data (kazokui's 2GB DB) showed ~63% of repo-related sessions
# would be missed by workspace-only.
extract_conversations() {
    local src_db="$1"
    local dest_db="$2"
    local repo_path="$3"
    local workspace_ids_file="$4"

    if command -v python3 &>/dev/null; then
        python3 - "$src_db" "$dest_db" "$repo_path" "$workspace_ids_file" <<'PYEOF'
import sqlite3, sys

src, dest, repo_path, ids_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

workspace_ids = set()
try:
    with open(ids_path) as f:
        workspace_ids = {line.strip() for line in f if line.strip()}
except FileNotFoundError:
    pass

src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
dest_conn = sqlite3.connect(dest)
dest_conn.execute("CREATE TABLE IF NOT EXISTS cursorDiskKV(key TEXT, value BLOB)")

# Text-search side: find composer IDs whose bubble or composerData values
# reference repo_path. Catches tool call args, file mentions, workspace
# URIs — anywhere the absolute path appears.
text_ids = set()
for row in src_conn.execute(
    "SELECT DISTINCT substr(key, 10, 36) FROM cursorDiskKV "
    "WHERE key LIKE 'bubbleId:%' AND cast(value as text) LIKE ?",
    (f"%{repo_path}%",),
):
    text_ids.add(row[0])
for row in src_conn.execute(
    # composerData: is 13 chars; SQLite substr is 1-based, so the UUID
    # starts at position 14.
    "SELECT DISTINCT substr(key, 14) FROM cursorDiskKV "
    "WHERE key LIKE 'composerData:%' AND cast(value as text) LIKE ?",
    (f"%{repo_path}%",),
):
    text_ids.add(row[0])

union = workspace_ids | text_ids
only_workspace = workspace_ids - text_ids
only_text = text_ids - workspace_ids
both = workspace_ids & text_ids
print(
    f"Composer IDs to extract: {len(union)} total "
    f"(workspace-only: {len(only_workspace)}, text-only: {len(only_text)}, both: {len(both)})"
)

if not union:
    dest_conn.close()
    src_conn.close()
    sys.exit(0)

count = 0
matched_composers = 0
for cid in sorted(union):
    row = src_conn.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key = ?",
        (f"composerData:{cid}",),
    ).fetchone()
    if row:
        dest_conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", row)
        count += 1
        matched_composers += 1

    batch = src_conn.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
        (f"bubbleId:{cid}:%",),
    ).fetchall()
    if batch:
        dest_conn.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", batch)
        count += len(batch)

dest_conn.commit()
dest_conn.close()
src_conn.close()
print(f"Extracted {count} rows for {matched_composers}/{len(union)} sessions")
PYEOF
    elif command -v sqlite3 &>/dev/null; then
        # sqlite3 CLI fallback — no workspace lookup, no path filter; copies
        # every composerData/bubbleId row. Strictly worse than the python
        # path but better than silently failing on a python-less environment.
        echo "Warning: python3 not found, falling back to sqlite3 CLI (no filtering)"
        sqlite3 "$src_db" <<SQL
ATTACH '$dest_db' AS out;
CREATE TABLE out.cursorDiskKV(key TEXT, value BLOB);
INSERT INTO out.cursorDiskKV
  SELECT key, value FROM cursorDiskKV
  WHERE key LIKE 'composerData:%'
     OR key LIKE 'bubbleId:%';
SQL
    else
        echo "Error: neither python3 nor sqlite3 found, cannot package Cursor sessions"
        return 1
    fi
}

if ! command -v python3 &>/dev/null; then
    echo "Warning: python3 not found — workspace-based session lookup unavailable."
    echo "Falling back to unfiltered sqlite3 dump (may include unrelated sessions)."
fi

# ---------------------------------------------------------------------------
# Phase 1: walk workspaceStorage, find workspaces whose folder matches
# SEARCH_ROOT, and union the composer IDs they enumerate. This catches
# sessions the candidate never @-mentioned the repo in (e.g., chat opened
# via Composer panel with the file selector instead of a path mention).
# ---------------------------------------------------------------------------

WORKSPACE_DIR="$CURSOR_BASE/workspaceStorage"
MATCHED_WORKSPACES=0
IDS_FILE="$(mktemp -t luma-cursor-ids.XXXXXX)"
trap 'rm -f "$IDS_FILE"' EXIT

if [ -d "$WORKSPACE_DIR" ] && command -v python3 &>/dev/null; then
    for ws in "$WORKSPACE_DIR"/*/workspace.json; do
        [ -f "$ws" ] || continue
        ws_dir="$(dirname "$ws")"
        # workspace.json contains {"folder": "file:///path/to/project"}
        # Extract the folder URI and strip the file:// prefix
        folder="$(grep -o '"folder":"[^"]*"' "$ws" 2>/dev/null | head -1 | sed 's/"folder":"//; s/"$//; s|^file://||')" || true
        # Match SEARCH_ROOT or any path under it — some candidates open a
        # subfolder of the repo in Cursor rather than the repo root. Also
        # match the other direction: SEARCH_ROOT under folder, in case the
        # candidate opened a parent dir and worked on a subfolder.
        match=0
        if [ -n "$folder" ]; then
            if [ "$folder" = "$SEARCH_ROOT" ] || [[ "$folder" == "$SEARCH_ROOT"/* ]] || [[ "$SEARCH_ROOT" == "$folder"/* ]]; then
                match=1
            fi
        fi
        if [ "$match" = 1 ] && [ -f "$ws_dir/state.vscdb" ]; then
            echo "Matched Cursor workspace: $ws_dir (folder: $folder)"
            MATCHED_WORKSPACES=$((MATCHED_WORKSPACES + 1))
            emit_workspace_composer_ids "$ws_dir/state.vscdb" >> "$IDS_FILE" || true
        fi
    done
fi

UNIQ_ID_COUNT=0
if [ -s "$IDS_FILE" ]; then
    # dedup in-place
    sort -u "$IDS_FILE" -o "$IDS_FILE"
    UNIQ_ID_COUNT="$(wc -l < "$IDS_FILE" | tr -d ' ')"
fi

# ---------------------------------------------------------------------------
# Phase 2: extract from global cursorDiskKV using the UNION of workspace IDs
# and text-search hits. Real Cursor DBs (validated on a 2GB engineer
# install) show that workspace `composer.composerData` enumerates only a
# recent window of sessions — older / aborted / background sessions are
# still in cursorDiskKV but absent from any workspace's list. Text search
# is the only way to recover those; workspace IDs are the only way to catch
# sessions that never mentioned the repo path. Both are needed.
# ---------------------------------------------------------------------------

GLOBAL_DB="$CURSOR_BASE/globalStorage/state.vscdb"
packaged=0

if [ ! -f "$GLOBAL_DB" ]; then
    echo "Global Cursor state.vscdb not found at $GLOBAL_DB"
else
    echo "Packaging global Cursor state (workspace IDs: $UNIQ_ID_COUNT across $MATCHED_WORKSPACES workspace(s); also scanning bubble text for $SEARCH_ROOT)..."
    DEST_DB="$OUTPUT_DIR/cursor-global-sessions-$TIMESTAMP.vscdb"
    extract_conversations "$GLOBAL_DB" "$DEST_DB" "$SEARCH_ROOT" "$IDS_FILE"
    # extract_conversations only writes the DB if union is non-empty. If both
    # signals come back empty, skip gzip silently.
    if [ -s "$DEST_DB" ]; then
        gzip -f "$DEST_DB"
        echo "Created: ${DEST_DB}.gz"
        packaged=1
    else
        rm -f "$DEST_DB"
        echo "No Cursor sessions matched $SEARCH_ROOT"
    fi
fi

if [ "$packaged" -eq 0 ]; then
    echo "No Cursor AI sessions found for $SEARCH_ROOT (skipping)"
    exit 0
fi

echo "Done packaging Cursor sessions."
