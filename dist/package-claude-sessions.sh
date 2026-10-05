#!/bin/bash

# Package Claude sessions from the current repo into a tar.gz archive.
#
# Matches both exact-cwd sessions and subfolder-cwd sessions: some candidates
# run Claude from a subdirectory of the repo (e.g. <repo>/my-app), and Claude
# creates a separate project dir for that cwd. We include every ~/.claude/projects
# dir whose stored cwd is under $REPO_ROOT.

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
# SESSION_SEARCH_ROOT overrides where we look for cwd matches — defaults to
# the current repo root. Candidates who ran Claude from a different dir can
# set this to point at the actual working directory.
SEARCH_ROOT="${SESSION_SEARCH_ROOT:-$REPO_ROOT}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="${1:-$REPO_ROOT/dist}"
OUTPUT_FILE="$OUTPUT_DIR/claude-sessions-$TIMESTAMP.tar.gz"

# Sanitize repo path: replace /, _, and spaces with -
sanitize_path() { printf '%s' "$1" | sed 's|/|-|g; s|_|-|g; s| |-|g'; }
SANITIZED="$(sanitize_path "$SEARCH_ROOT")"
PROJECTS_ROOT="$HOME/.claude/projects"

if [ ! -d "$PROJECTS_ROOT" ]; then
    echo "No Claude projects directory at $PROJECTS_ROOT"
    exit 1
fi

# Collect candidate project dirs: the one whose sanitized name matches SEARCH_ROOT,
# plus any whose name starts with "$SANITIZED-" (candidate likely subpaths).
# Then filter by actual cwd content so unrelated sibling repos with a shared
# prefix don't get packaged.
shopt -s nullglob
candidates=("$PROJECTS_ROOT/$SANITIZED" "$PROJECTS_ROOT/$SANITIZED"-*)
shopt -u nullglob

# Use grep (universally available) instead of rg. The previous version used
# `rg`, which is bundled with Claude Code but is *not* reliably on a
# candidate's PATH when submit.sh shells out. When rg was missing, every cwd
# check silently failed (stderr redirected) and the script reported "No
# Claude sessions found" even when sessions existed.
matching=()
for dir in "${candidates[@]}"; do
    [ -d "$dir" ] || continue
    if grep -r -E -q "\"cwd\":\"$SEARCH_ROOT(/[^\"]*)?\"" "$dir" 2>/dev/null; then
        matching+=("$dir")
    fi
done

if [ ${#matching[@]} -eq 0 ]; then
    echo "No Claude sessions found for $SEARCH_ROOT (checked ${#candidates[@]} candidate dir(s))"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

echo "Packaging Claude sessions for $SEARCH_ROOT (${#matching[@]} project dir(s))..."
rels=()
for d in "${matching[@]}"; do
    rels+=("./$(basename "$d")")
done

tar -czvf "$OUTPUT_FILE" -C "$PROJECTS_ROOT" "${rels[@]}"

echo "Created: $OUTPUT_FILE"
