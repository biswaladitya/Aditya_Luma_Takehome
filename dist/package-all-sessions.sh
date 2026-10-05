#!/bin/bash

# Package Claude, Codex, Cursor, and Cline sessions into a shared folder at repo root.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
OUTPUT_DIR="${1:-$REPO_ROOT/session-packages}"
# SESSION_SEARCH_ROOT overrides where we look for cwd matches in session
# history. Default: the current repo root. Candidates who ran their AI tools
# from a different directory (e.g. a parent dir or a sibling clone) can set
# this to point at the actual working directory, e.g.:
#   SESSION_SEARCH_ROOT=/path/to/work ./dist/package-all-sessions.sh
SEARCH_ROOT="${SESSION_SEARCH_ROOT:-$REPO_ROOT}"
export SESSION_SEARCH_ROOT="$SEARCH_ROOT"

CLAUDE_SCRIPT="${CLAUDE_SCRIPT:-$REPO_ROOT/dist/package-claude-sessions.sh}"
CODEX_SCRIPT="${CODEX_SCRIPT:-$REPO_ROOT/dist/package-codex-sessions.sh}"
CURSOR_SCRIPT="${CURSOR_SCRIPT:-$REPO_ROOT/dist/package-cursor-sessions.sh}"
CLINE_SCRIPT="${CLINE_SCRIPT:-$REPO_ROOT/dist/package-cline-sessions.sh}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

case "$(uname -s)" in
    Darwin)
        CURSOR_BASE="${CURSOR_BASE:-$HOME/Library/Application Support/Cursor/User}"
        EDITOR_BASE_TEMPLATE="$HOME/Library/Application Support/%s/User"
        ;;
    Linux)
        CURSOR_BASE="${CURSOR_BASE:-$HOME/.config/Cursor/User}"
        EDITOR_BASE_TEMPLATE="$HOME/.config/%s/User"
        ;;
    *)
        CURSOR_BASE=""
        EDITOR_BASE_TEMPLATE=""
        ;;
esac

mkdir -p "$OUTPUT_DIR"

echo "Using output directory: $OUTPUT_DIR"

claude_status=0
codex_status=0
cursor_status=0
cline_status=0
ran_any=0

sanitize_path() { printf '%s' "$1" | sed 's|/|-|g; s|_|-|g; s| |-|g'; }
SANITIZED_SEARCH="$(sanitize_path "$SEARCH_ROOT")"
CLAUDE_ROOT="$HOME/.claude/projects"

# Claude: look for any project dir whose sanitized name matches SEARCH_ROOT or a
# subpath under it (candidate may run Claude from <repo>/subfolder).
claude_found=0
if [ -d "$CLAUDE_ROOT" ]; then
    shopt -s nullglob
    for dir in "$CLAUDE_ROOT/$SANITIZED_SEARCH" "$CLAUDE_ROOT/$SANITIZED_SEARCH"-*; do
        [ -d "$dir" ] || continue
        # Use grep rather than rg here — rg isn't reliably on candidate PATH
        # when submit.sh shells out, and when rg was missing every cwd check
        # silently failed and we reported no sessions found.
        if grep -r -E -q "\"cwd\":\"$SEARCH_ROOT(/[^\"]*)?\"" "$dir" 2>/dev/null; then
            claude_found=1
            break
        fi
    done
    shopt -u nullglob
fi

if [ "$claude_found" -eq 1 ]; then
    ran_any=1
    if [ -x "$CLAUDE_SCRIPT" ]; then
        echo "Running Claude session packager..."
        "$CLAUDE_SCRIPT" "$OUTPUT_DIR" || claude_status=$?
    else
        echo "Claude sessions exist but packager is missing or not executable: $CLAUDE_SCRIPT"
        claude_status=127
    fi
else
    echo "Skipping Claude packaging (no sessions found for this repo)."
fi

# Codex: sessions are JSONL with "cwd" — match SEARCH_ROOT or subpaths.
# grep -E doesn't support \s; substitute [[:space:]]* for portability.
if grep -r -E -q "\"cwd\":[[:space:]]*\"$SEARCH_ROOT(/[^\"]*)?\"" "$CODEX_HOME/sessions" 2>/dev/null; then
    ran_any=1
    if [ -x "$CODEX_SCRIPT" ]; then
        echo "Running Codex session packager..."
        "$CODEX_SCRIPT" "$OUTPUT_DIR" || codex_status=$?
    else
        echo "Codex sessions exist but packager is missing or not executable: $CODEX_SCRIPT"
        codex_status=127
    fi
else
    echo "Skipping Codex packaging (no sessions found for this repo)."
fi

# Cursor: workspace.json folder must be SEARCH_ROOT or a subpath under it.
cursor_found=0
if [ -n "$CURSOR_BASE" ] && [ -d "$CURSOR_BASE/workspaceStorage" ]; then
    for ws in "$CURSOR_BASE/workspaceStorage"/*/workspace.json; do
        [ -f "$ws" ] || continue
        folder="$(grep -o '"folder":"[^"]*"' "$ws" 2>/dev/null | head -1 | sed 's/"folder":"//; s/"$//; s|^file://||')" || true
        if [ "$folder" = "$SEARCH_ROOT" ] || [[ "$folder" == "$SEARCH_ROOT"/* ]]; then
            cursor_found=1
            break
        fi
    done
fi
# Also count global state as a reason to run
if [ -n "$CURSOR_BASE" ] && [ -f "$CURSOR_BASE/globalStorage/state.vscdb" ]; then
    cursor_found=1
fi

if [ "$cursor_found" -eq 1 ]; then
    ran_any=1
    if [ -x "$CURSOR_SCRIPT" ]; then
        echo "Running Cursor session packager..."
        "$CURSOR_SCRIPT" "$OUTPUT_DIR" || cursor_status=$?
    else
        echo "Cursor sessions exist but packager is missing or not executable: $CURSOR_SCRIPT"
        cursor_status=127
    fi
else
    echo "Skipping Cursor packaging (no sessions found for this repo)."
fi

# Cline: per-task folders under <editor>/User/globalStorage/saoudrizwan.claude-dev/tasks/
cline_found=0
if [ -n "$EDITOR_BASE_TEMPLATE" ]; then
    for editor in Code "Code - Insiders" Cursor Windsurf VSCodium; do
        base="$(printf "$EDITOR_BASE_TEMPLATE" "$editor")"
        tasks_dir="$base/globalStorage/saoudrizwan.claude-dev/tasks"
        [ -d "$tasks_dir" ] || continue
        for task_dir in "$tasks_dir"/*/; do
            hist="$task_dir/api_conversation_history.json"
            [ -f "$hist" ] || continue
            if grep -qF "Current Working Directory ($SEARCH_ROOT)" "$hist" 2>/dev/null \
               || grep -qF "Current Working Directory ($SEARCH_ROOT/" "$hist" 2>/dev/null; then
                cline_found=1
                break 2
            fi
        done
    done
fi

if [ "$cline_found" -eq 1 ]; then
    ran_any=1
    if [ -x "$CLINE_SCRIPT" ]; then
        echo "Running Cline session packager..."
        "$CLINE_SCRIPT" "$OUTPUT_DIR" || cline_status=$?
    else
        echo "Cline sessions exist but packager is missing or not executable: $CLINE_SCRIPT"
        cline_status=127
    fi
else
    echo "Skipping Cline packaging (no sessions found for this repo)."
fi

if [ "$ran_any" -eq 0 ]; then
    # Loud, actionable warning — earlier versions said "Nothing to package"
    # and we only noticed the problem days later when a review came in with
    # RECOMMENDATION=Manual Review Required and no AI history. If a candidate
    # sees this block, they have a chance to fix it before submitting.
    cat >&2 <<EOF

============================================================
⚠️  No AI session history found for:
    $SEARCH_ROOT

Searched these locations (nothing matched the path above):
  Claude   ~/.claude/projects/
  Codex    $CODEX_HOME/sessions/
  Cursor   ${CURSOR_BASE:-<not detected on this platform>}/workspaceStorage/
  Cline    <editor>/User/globalStorage/saoudrizwan.claude-dev/tasks/

If you used an AI tool from a DIFFERENT directory (for example a parent
of this repo, or a separate clone), re-run the packager with that path:

  SESSION_SEARCH_ROOT=/path/to/where/you/worked ./dist/package-all-sessions.sh

Or invoke a single packager directly, e.g.:

  SESSION_SEARCH_ROOT=/path/to/where/you/worked ./dist/package-claude-sessions.sh

Submission scoring weights AI usage, so submitting without sessions will
typically lower your score. If you genuinely did not use AI for this
challenge, you can ignore this message.
============================================================
EOF
    exit 0
fi

if [ "$claude_status" -ne 0 ] || [ "$codex_status" -ne 0 ] || [ "$cursor_status" -ne 0 ] || [ "$cline_status" -ne 0 ]; then
    echo "One or more packagers failed."
    [ "$claude_status" -ne 0 ] && echo "Claude packager failed with status $claude_status."
    [ "$codex_status" -ne 0 ] && echo "Codex packager failed with status $codex_status."
    [ "$cursor_status" -ne 0 ] && echo "Cursor packager failed with status $cursor_status."
    [ "$cline_status" -ne 0 ] && echo "Cline packager failed with status $cline_status."
    exit 1
fi

# Post-flight: aggregate user-message count across every packaged artifact.
# We need session logs to evaluate the take-home; below this floor the
# auto-reviewer will fall back to Manual Review Required and the submission
# stalls in a queue. Threshold is intentionally generous — a real working
# session (even a brief one) clears it easily; only capture failures and
# essentially-no-AI submissions fall below.
SESSION_MIN_USER_MESSAGES=15
COUNTER_SCRIPT="${COUNTER_SCRIPT:-$REPO_ROOT/dist/count-session-activity.py}"

if command -v python3 &>/dev/null && [ -f "$COUNTER_SCRIPT" ]; then
    if total_user_msgs="$(python3 "$COUNTER_SCRIPT" "$OUTPUT_DIR" 2>/dev/null)"; then
        # Strip any stray whitespace; default to 0 if empty/non-numeric.
        total_user_msgs="${total_user_msgs//[^0-9]/}"
        : "${total_user_msgs:=0}"

        if [ "$total_user_msgs" -lt "$SESSION_MIN_USER_MESSAGES" ]; then
            cat >&2 <<EOF

============================================================
!!  We only detected $total_user_msgs AI session message(s) in this
!!  submission. We need session logs to evaluate your take-home.
!!
!!  If you used an AI tool from a different directory, re-run with:
!!    SESSION_SEARCH_ROOT=/path/to/where/you/worked ./dist/package-all-sessions.sh
!!
!!  If you used a web-based AI (ChatGPT, Claude.ai, etc.), export
!!  those conversations and include them in your repo before
!!  submitting.
============================================================
EOF
        fi
    fi
fi

echo "Done. Archives are in: $OUTPUT_DIR"
