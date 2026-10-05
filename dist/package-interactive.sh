#!/bin/bash

# package-interactive.sh — last-resort AI-session packager.
#
# The deterministic packagers (package-all-sessions.sh and friends) only find
# sessions that live in the standard locations AND whose recorded cwd matches
# this repo's path. When a candidate ran their AI tool from an unusual
# directory, used a tool we don't have a dedicated packager for, or bundled a
# web export in a shape our counter doesn't recognize, those scripts come up
# empty and the submission lands in "Manual Review Required" with no AI history
# to score.
#
# This script is the fallback: if a local `claude` CLI is available (we offer
# to install it if not), it drives Claude Code headlessly to *find* the session
# data wherever it actually lives, package it via the existing deterministic
# scripts (or copy it in a recognized format), and verify the result with
# count-session-activity.py — iterating until the capture clears the threshold.
#
# It uses the candidate's OWN Claude auth (login or ANTHROPIC_API_KEY). We never
# ship a key. Normally invoked by submit.sh after a y/n consent prompt, but safe
# to run standalone:  ./dist/package-interactive.sh
#
# Exit codes: always 0 on a "we tried" outcome (so it never aborts submit.sh).
# The real signal is the final session-message count it prints.

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
OUTPUT_DIR="${1:-$REPO_ROOT/session-packages}"
DIST_DIR="$REPO_ROOT/dist"
COUNTER_SCRIPT="${COUNTER_SCRIPT:-$DIST_DIR/count-session-activity.py}"
# Match the floor enforced in package-all-sessions.sh / the reviewer's
# assess_session_quality so "success" here means the same thing downstream.
SESSION_MIN_USER_MESSAGES="${SESSION_MIN_USER_MESSAGES:-15}"

mkdir -p "$OUTPUT_DIR"

# count_sessions: echo the total user-message count currently in OUTPUT_DIR,
# or 0 if we can't run the counter (no python3 / missing script).
count_sessions() {
    if command -v python3 >/dev/null 2>&1 && [ -f "$COUNTER_SCRIPT" ]; then
        local n
        n="$(python3 "$COUNTER_SCRIPT" "$OUTPUT_DIR" 2>/dev/null)"
        n="${n//[^0-9]/}"
        echo "${n:-0}"
    else
        echo 0
    fi
}

BEFORE_COUNT="$(count_sessions)"
if [ "$BEFORE_COUNT" -ge "$SESSION_MIN_USER_MESSAGES" ]; then
    echo "package-interactive.sh: already have $BEFORE_COUNT session message(s); nothing to do."
    exit 0
fi

echo "============================================================"
echo "  AI-assisted session recovery"
echo "============================================================"
echo "The standard packagers found $BEFORE_COUNT session message(s)"
echo "(need >= $SESSION_MIN_USER_MESSAGES). Trying to locate your AI history"
echo "with a local Claude agent..."
echo ""

# --- Ensure a `claude` CLI is available -----------------------------------
# Resolve from PATH first; fall back to common npm-global bin locations that
# may not be on PATH inside a non-login shell.
find_claude() {
    if command -v claude >/dev/null 2>&1; then command -v claude; return 0; fi
    local candidates=(
        "$HOME/.npm-global/bin/claude"
        "/usr/local/bin/claude"
        "/opt/homebrew/bin/claude"
        "$HOME/.local/bin/claude"
    )
    if command -v npm >/dev/null 2>&1; then
        local npm_bin
        npm_bin="$(npm bin -g 2>/dev/null || true)"
        [ -n "$npm_bin" ] && candidates+=("$npm_bin/claude")
    fi
    local c
    for c in "${candidates[@]}"; do
        [ -x "$c" ] && { echo "$c"; return 0; }
    done
    return 1
}

# CLAUDE_CLI is an explicit override (unusual installs, and the test seam): when
# set we use it verbatim if executable and skip autodetect/install, so a test
# can force a specific stub binary or a guaranteed "not found" without npm ever
# running. Unset = normal autodetect + one-time install.
CLAUDE_BIN=""
if [ -n "${CLAUDE_CLI:-}" ]; then
    [ -x "${CLAUDE_CLI}" ] && CLAUDE_BIN="${CLAUDE_CLI}"
else
    CLAUDE_BIN="$(find_claude || true)"
    if [ -z "$CLAUDE_BIN" ]; then
        if command -v npm >/dev/null 2>&1; then
            echo "Claude CLI not found. Installing @anthropic-ai/claude-code (one-time)..."
            if npm install -g @anthropic-ai/claude-code >/dev/null 2>&1; then
                CLAUDE_BIN="$(find_claude || true)"
            fi
        fi
    fi
fi

if [ -z "$CLAUDE_BIN" ]; then
    cat >&2 <<EOF

Could not find or install the Claude CLI, so automatic recovery isn't possible.

To recover your AI history manually, either:
  * Install Claude Code (https://docs.claude.com/claude-code) and re-run
    ./dist/package-interactive.sh, OR
  * Point the standard packager at the directory where you actually ran your
    AI tool:
        SESSION_SEARCH_ROOT=/path/to/where/you/worked ./dist/package-all-sessions.sh
  * If you used a web AI (ChatGPT, Claude.ai), transcribe ONLY the
    conversations about this challenge into ./session-packages/ as .md files
    in the format described in ./dist/SESSION_TRANSCRIPT_FORMAT.md (starts
    with "# Claude History: ...", one "**User:**" line per message you sent).
    Plain-text or other .md shapes are not counted and score as no AI history.
    Do NOT copy a full-history export (conversations.json / export .zip) there:
    it contains ALL your personal chats, and everything in ./session-packages/
    is uploaded with your submission.

Continuing submission without recovered sessions.
EOF
    exit 0
fi

echo "Using Claude CLI: $CLAUDE_BIN"
echo ""

# --- Build the agent instructions -----------------------------------------
# The agent reads from $HOME (session stores live outside the repo) and writes
# only into ./session-packages/. We prefer it to re-run the deterministic
# packagers with the right SESSION_SEARCH_ROOT so output format stays identical
# to the normal path; copying raw files is the fallback.
read -r -d '' PROMPT <<EOF
You are helping a take-home candidate package their AI coding-assistant session
history so the reviewer can see how they used AI on this challenge. The
deterministic packagers found only ${BEFORE_COUNT} user message(s); we need at
least ${SESSION_MIN_USER_MESSAGES}.

Repo root: ${REPO_ROOT}
Output directory (package everything here): ${OUTPUT_DIR}
Packaging scripts available: ${DIST_DIR}/package-all-sessions.sh and the
per-tool scripts package-claude-sessions.sh, package-codex-sessions.sh,
package-cursor-sessions.sh, package-cline-sessions.sh (each accepts an output
dir argument and honors the SESSION_SEARCH_ROOT env var).
Verifier: python3 ${COUNTER_SCRIPT} ${OUTPUT_DIR}  (prints the total
user-message count across recognized bundles in the output dir).

Goal: get the verifier count to >= ${SESSION_MIN_USER_MESSAGES}, using only
sessions that relate to THIS challenge/repo.

Do this:
1. Figure out which AI tool(s) the candidate used for this repo. Look in the
   usual stores, e.g.:
     - Claude Code:  ~/.claude/projects/   (JSONL per project; "cwd" field)
     - Codex:        ~/.codex/sessions/    (JSONL; "cwd" field)
     - Cursor:       ~/Library/Application Support/Cursor/User/ (macOS) or
                     ~/.config/Cursor/User/ (Linux)  (workspaceStorage, state.vscdb)
     - Cline:        <editor>/User/globalStorage/saoudrizwan.claude-dev/tasks/
     - Web exports the candidate may have downloaded into the repo or ~/Downloads
       (ChatGPT/Claude.ai exports: conversations.json, *.zip, or *.md).
2. Determine the real working directory the candidate used. It may NOT equal the
   repo root (they may have run the AI from a parent dir, a sibling clone, or a
   subfolder). Match on the session's recorded cwd / workspace folder, and on
   content that references this challenge.
3. Prefer running the existing deterministic packagers with the correct path,
   e.g.:  SESSION_SEARCH_ROOT=<the real dir> ${DIST_DIR}/package-all-sessions.sh ${OUTPUT_DIR}
   Run them once per relevant directory if work spanned several.
4. If a relevant session is in a location or tool the scripts don't cover, copy
   the raw session data into ${OUTPUT_DIR} in a format the verifier recognizes:
   a tarball (*.tar.gz) of the session directory, the raw *.jsonl file(s), or a
   Cline api_conversation_history.json. Do not rename files in ways that hide
   their format. Web-chat exports are the one exception: never copy them raw —
   see step 5.
5. UNIVERSAL FALLBACK — for tools we have NO deterministic packager for (e.g.
   GitHub Copilot in VS Code, Windsurf, Aider, Gemini CLI) and for ALL web-chat
   exports (ChatGPT / Claude.ai), do NOT copy the raw file into ${OUTPUT_DIR}.
   A full-history export (conversations.json or an export .zip) contains EVERY
   conversation the candidate has ever had with that assistant, and everything
   in ${OUTPUT_DIR} is uploaded with the submission — copying it wholesale
   would ship the candidate's entire personal chat history to the reviewer.
   Instead TRANSCODE only the challenge-related session(s) into the canonical
   transcript format and
   write it to ${OUTPUT_DIR} as transcript-<tool>.md. The exact spec is in
   ${DIST_DIR}/SESSION_TRANSCRIPT_FORMAT.md — read it. In short: a markdown
   file beginning with the line "# Claude History: <repo>", one
   "## Session: <date> (source: <tool>)" line, every human turn on its own
   "**User:**" line (verbatim), every assistant turn on a "**Claude:**" line.
   Transcribe the candidate's ACTUAL messages from the tool's own store/export
   (read its JSON/db/log) — do NOT invent content. This is the general way to
   make ANY tool's session scorable.
6. After each attempt, re-run the verifier and keep going until the count is
   >= ${SESSION_MIN_USER_MESSAGES} or you have exhausted plausible sources.

Rules:
- Only include sessions tied to this challenge/repo. Do NOT bundle unrelated
  personal or other-project conversations.
- NEVER place a full-history web export (conversations.json / a ChatGPT or
  Claude.ai export .zip) in ${OUTPUT_DIR}, even though the verifier would count
  it — extract only the challenge-related conversations per step 5.
- Do NOT modify the candidate's source code or anything outside ${OUTPUT_DIR}.
- Do NOT fabricate session content — transcribe what actually happened. On
  "**User:**" lines preserve the user's real wording; assistant text may be
  summarized.
- Be concise. End with one line stating the final verifier count and which
  directories/tools you packaged.
EOF

# --- Run Claude headlessly --------------------------------------------------
# bypassPermissions + --add-dir "$HOME": this is the candidate's own machine,
# own Claude account, and they consented in submit.sh. Wrap in `timeout` when
# available so a stuck agent can't hang the submission indefinitely.
CLAUDE_ARGS=(--print "$PROMPT" --permission-mode bypassPermissions --add-dir "$HOME")

# errexit is intentionally off (set -uo pipefail only): a nonzero agent exit is
# handled below, never aborts the script, so submit.sh always proceeds.
if command -v timeout >/dev/null 2>&1; then
    timeout 900 "$CLAUDE_BIN" "${CLAUDE_ARGS[@]}"
    CLAUDE_RC=$?
else
    "$CLAUDE_BIN" "${CLAUDE_ARGS[@]}"
    CLAUDE_RC=$?
fi

echo ""
if [ "$CLAUDE_RC" -ne 0 ]; then
    cat >&2 <<EOF
The Claude agent exited with status $CLAUDE_RC. The most common cause is that
Claude isn't logged in on this machine. Authenticate and re-run, either:
    claude   (then complete the login), OR
    export ANTHROPIC_API_KEY=sk-...   then re-run ./dist/package-interactive.sh
EOF
fi

# Make recovered packages visible to the submission tarball.
git -C "$REPO_ROOT" add session-packages/ >/dev/null 2>&1 || true

AFTER_COUNT="$(count_sessions)"
echo "------------------------------------------------------------"
echo "Session message count: $BEFORE_COUNT -> $AFTER_COUNT (need >= $SESSION_MIN_USER_MESSAGES)"
if [ "$AFTER_COUNT" -lt "$SESSION_MIN_USER_MESSAGES" ]; then
    cat >&2 <<EOF
Still below the threshold. If you used an AI tool from a directory the agent
didn't find, run it directly:
    SESSION_SEARCH_ROOT=/path/to/where/you/worked ./dist/package-all-sessions.sh
If you used a web AI, transcribe only the conversations about this challenge
into ./session-packages/ as .md files in the format described in
./dist/SESSION_TRANSCRIPT_FORMAT.md ("# Claude History: ..." header, one
"**User:**" line per message you sent) — other shapes are not counted. Do NOT
copy a full-history export (conversations.json / export .zip) there; it
contains all your personal chats and everything in that folder is uploaded
with your submission.
You can submit anyway, but submissions without AI history typically score lower.
EOF
fi
echo "============================================================"
exit 0
