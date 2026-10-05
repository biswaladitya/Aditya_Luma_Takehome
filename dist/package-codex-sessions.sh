#!/bin/bash

# Package Codex sessions for the current repo into a tar.gz archive.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
# SESSION_SEARCH_ROOT overrides where we look for cwd matches — defaults to
# the current repo root. Candidates who ran Codex from a different dir can
# set this to point at the actual working directory.
SEARCH_ROOT="${SESSION_SEARCH_ROOT:-$REPO_ROOT}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUTPUT_DIR="${1:-$REPO_ROOT/dist}"
OUTPUT_FILE="$OUTPUT_DIR/codex-sessions-$TIMESTAMP.tar.gz"

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
SESSIONS_DIR="$CODEX_HOME/sessions"
SNAPSHOTS_DIR="$CODEX_HOME/shell_snapshots"

if [ ! -d "$SESSIONS_DIR" ]; then
    echo "No Codex sessions directory found at $SESSIONS_DIR"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

MATCH_FILE="$(mktemp)"
LIST_FILE="$(mktemp)"
trap 'rm -f "$MATCH_FILE" "$LIST_FILE"' EXIT

# Match sessions whose cwd is SEARCH_ROOT or any subpath under it. Some
# candidates run Codex from <repo>/subfolder; that session should still be
# packaged for review.
#
# Use grep rather than rg — rg isn't reliably on candidate PATH when
# submit.sh shells out, and the previous version silently failed.
# grep -E doesn't support \s; substitute [[:space:]]* for portability.
PATTERN="\"cwd\":[[:space:]]*\"$SEARCH_ROOT(/[^\"]*)?\""
grep -r -l -E "$PATTERN" "$SESSIONS_DIR" >"$MATCH_FILE" || true

if [ ! -s "$MATCH_FILE" ]; then
    echo "No Codex sessions found for $SEARCH_ROOT"
    exit 1
fi

while IFS= read -r session_file; do
    if [ -f "$session_file" ]; then
        session_rel="${session_file#$CODEX_HOME/}"
        printf '%s\n' "$session_rel" >>"$LIST_FILE"

        session_id="$(
            grep -m1 -o -E '"id":"[^"]+"' "$session_file" \
                | sed 's/"id":"//; s/"$//'
        )"

        if [ -n "$session_id" ] && [ -f "$SNAPSHOTS_DIR/$session_id.sh" ]; then
            printf 'shell_snapshots/%s.sh\n' "$session_id" >>"$LIST_FILE"
        fi
    fi
done <"$MATCH_FILE"

sort -u "$LIST_FILE" -o "$LIST_FILE"

echo "Packaging Codex sessions for $SEARCH_ROOT..."
tar -czvf "$OUTPUT_FILE" -C "$CODEX_HOME" -T "$LIST_FILE"

echo "Created: $OUTPUT_FILE"
