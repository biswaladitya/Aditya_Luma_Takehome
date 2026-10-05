#!/bin/bash

# Package Cline (saoudrizwan.claude-dev) sessions for the current repo into a
# tar.gz archive.
#
# Cline stores per-task folders under the VS Code / Cursor / Windsurf extension
# global storage directory:
#   <editor-base>/User/globalStorage/saoudrizwan.claude-dev/tasks/<taskId>/
# Each task folder contains api_conversation_history.json and ui_messages.json.
#
# Cline embeds the current working directory in environment_details blocks as
# "Current Working Directory (/path/to/cwd)". We use that to filter which task
# folders belong to the repo being packaged.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
# SESSION_SEARCH_ROOT overrides where we look for cwd matches — defaults to
# the current repo root. Candidates who used Cline from a different dir can
# set this to point at the actual working directory.
SEARCH_ROOT="${SESSION_SEARCH_ROOT:-$REPO_ROOT}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUTPUT_DIR="${1:-$REPO_ROOT/dist}"
OUTPUT_FILE="$OUTPUT_DIR/cline-sessions-$TIMESTAMP.tar.gz"

# Collect candidate "<editor base>/User" dirs across editors and platforms.
# Tests/CI can override the search paths by setting CLINE_EDITOR_BASES to a
# colon-separated list of "<editor base>/User" dirs.
editor_bases=()
if [ -n "${CLINE_EDITOR_BASES:-}" ]; then
    IFS=':' read -r -a editor_bases <<< "$CLINE_EDITOR_BASES"
else
    case "$(uname -s)" in
        Darwin)
            for editor in Code "Code - Insiders" Cursor Windsurf VSCodium; do
                dir="$HOME/Library/Application Support/$editor/User"
                [ -d "$dir" ] && editor_bases+=("$dir")
            done
            ;;
        Linux)
            for editor in Code "Code - Insiders" Cursor Windsurf VSCodium; do
                dir="$HOME/.config/$editor/User"
                [ -d "$dir" ] && editor_bases+=("$dir")
            done
            ;;
    esac
fi

if [ "${#editor_bases[@]}" -eq 0 ]; then
    echo "No VS Code-family editor data dirs found; skipping Cline packaging."
    exit 0
fi

# For each candidate task dir, check api_conversation_history.json for a
# Current Working Directory marker under REPO_ROOT.
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
STAGING="$WORK_DIR/cline-sessions"
mkdir -p "$STAGING"

matched=0
for base in "${editor_bases[@]}"; do
    tasks_dir="$base/globalStorage/saoudrizwan.claude-dev/tasks"
    [ -d "$tasks_dir" ] || continue

    editor_label="$(basename "$(dirname "$base")")"
    editor_slug="$(printf '%s' "$editor_label" | tr ' /' '--')"

    for task_dir in "$tasks_dir"/*/; do
        [ -d "$task_dir" ] || continue
        hist="$task_dir/api_conversation_history.json"
        [ -f "$hist" ] || continue

        # Look for "Current Working Directory (SEARCH_ROOT)" or a subpath under it.
        if grep -qF "Current Working Directory ($SEARCH_ROOT)" "$hist" 2>/dev/null \
           || grep -qF "Current Working Directory ($SEARCH_ROOT/" "$hist" 2>/dev/null; then
            task_id="$(basename "$task_dir")"
            dest="$STAGING/$editor_slug/$task_id"
            mkdir -p "$dest"
            # Copy session JSONs (skip large caches / images if present).
            for f in api_conversation_history.json ui_messages.json; do
                [ -f "$task_dir/$f" ] && cp "$task_dir/$f" "$dest/$f"
            done
            matched=$((matched + 1))
            echo "  matched cline task: $editor_label/$task_id"
        fi
    done
done

if [ "$matched" -eq 0 ]; then
    echo "No Cline sessions found for $SEARCH_ROOT"
    exit 0
fi

mkdir -p "$OUTPUT_DIR"

echo "Packaging $matched Cline task(s) for $SEARCH_ROOT..."
tar -czf "$OUTPUT_FILE" -C "$WORK_DIR" cline-sessions

echo "Created: $OUTPUT_FILE"
