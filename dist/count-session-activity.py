#!/usr/bin/env python3
"""Count user messages across every packaged session artifact.

Invoked by dist/package-all-sessions.sh as a post-flight check. The aggregate
count drives a single user-facing warning when AI evidence is too sparse for
the take-home to be scored. We deliberately don't attribute counts per tool —
the candidate-facing message only needs the total.

Supports every format package-all-sessions.sh produces, plus web-chat data
exports candidates drop in by hand:
  - Claude / Codex tarballs (*.tar.gz) — JSONL inside, one message per line
  - Cline tarballs — api_conversation_history.json inside the tarball
  - Cursor SQLite snapshots (*.vscdb.gz) — bubble rows with type=1 are user
  - Raw api_conversation_history.json files (hand-dropped Cline dumps)
  - Anthropic claude.ai web exports (zip or conversations.json) — JSON list
    of conversations whose chat_messages[].sender == "human"
  - OpenAI ChatGPT exports (zip or conversations.json) — JSON list of
    conversations whose mapping nodes have message.author.role == "user"
  - Canonical agent transcripts (*.md headed `# Claude History:`) — the
    universal format package-interactive.sh transcodes unknown tools into;
    human turns on `**User:**` lines. See dist/SESSION_TRANSCRIPT_FORMAT.md.

Usage:
  python3 count-session-activity.py <output_dir>

Prints a single integer (total user messages) on stdout.
"""
import gzip
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import zipfile


def _is_tool_result_user_msg(content) -> bool:
    """Tool-result envelopes show up under `role:user` / `type:user` but are
    not human turns. Filter them out so we don't inflate the count."""
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
    )


def _count_claude_jsonl(fileobj) -> int:
    n = 0
    text = io.TextIOWrapper(fileobj, encoding="utf-8", errors="replace")
    for line in text:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        t = obj.get("type")
        if t == "user":
            msg = obj.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if _is_tool_result_user_msg(content):
                continue
            n += 1
        elif t == "response_item":
            # Codex rollout format: payload.role == "user"
            payload = obj.get("payload") or {}
            if isinstance(payload, dict) and payload.get("role") == "user":
                n += 1
    return n


def _count_cline_list(data) -> int:
    if not isinstance(data, list):
        return 0
    n = 0
    for m in data:
        if not isinstance(m, dict):
            continue
        if m.get("role") != "user":
            continue
        if _is_tool_result_user_msg(m.get("content")):
            continue
        n += 1
    return n


def _count_tarball(path: str) -> int:
    n = 0
    try:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                if member.name.endswith(".jsonl"):
                    fobj = tar.extractfile(member)
                    if fobj is not None:
                        n += _count_claude_jsonl(fobj)
                elif member.name.endswith("api_conversation_history.json"):
                    fobj = tar.extractfile(member)
                    if fobj is None:
                        continue
                    try:
                        data = json.load(fobj)
                    except Exception:
                        continue
                    n += _count_cline_list(data)
    except (tarfile.TarError, OSError):
        return 0
    return n


def _count_vscdb_gz(path: str) -> int:
    n = 0
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".vscdb", delete=False) as tmp:
            with gzip.open(path, "rb") as f_in:
                tmp.write(f_in.read())
            tmp_path = tmp.name
        conn = sqlite3.connect(tmp_path)
        for (val,) in conn.execute(
            "SELECT cast(value as text) FROM cursorDiskKV "
            "WHERE key LIKE 'bubbleId:%'"
        ):
            try:
                d = json.loads(val)
            except Exception:
                continue
            if isinstance(d, dict) and d.get("type") == 1:
                text = (d.get("text") or "").strip()
                # Empty user bubbles (rare placeholders) shouldn't count.
                if text:
                    n += 1
        conn.close()
    except (OSError, sqlite3.Error):
        return 0
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return n


def _count_raw_json(path: str) -> int:
    try:
        with open(path, "r", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return 0
    return _count_cline_list(data)


def _count_loose_jsonl(path: str) -> int:
    """Count user messages in a raw .jsonl file (extracted Claude/Codex
    transcript, not yet inside a tarball)."""
    try:
        with open(path, "rb") as f:
            return _count_claude_jsonl(f)
    except OSError:
        return 0


def _count_claude_web_conversations(convos) -> int:
    """Count human turns in a parsed Anthropic claude.ai conversations.json."""
    if not isinstance(convos, list):
        return 0
    n = 0
    for c in convos:
        if not isinstance(c, dict):
            continue
        for m in c.get("chat_messages") or []:
            if not isinstance(m, dict) or m.get("sender") != "human":
                continue
            # Skip empty / pure-tool-result turns so the counter matches
            # what export_claude_web_history.py renders to markdown.
            content = m.get("content")
            text = m.get("text", "")
            if isinstance(content, list):
                has_text = any(
                    isinstance(b, dict) and b.get("type") == "text"
                    and (b.get("text") or "").strip()
                    for b in content
                )
                if not has_text and not (text or "").strip():
                    continue
            elif not (text or "").strip():
                continue
            n += 1
    return n


def _count_chatgpt_conversations(convos) -> int:
    """Count human turns in a parsed OpenAI ChatGPT conversations.json."""
    if not isinstance(convos, list):
        return 0
    n = 0
    for c in convos:
        if not isinstance(c, dict):
            continue
        mapping = c.get("mapping") or {}
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            msg = node.get("message") or {}
            author = msg.get("author") or {}
            if author.get("role") != "user":
                continue
            meta = msg.get("metadata") or {}
            if meta.get("is_user_system_message"):
                continue
            content = msg.get("content") or {}
            parts = content.get("parts") or []
            joined = "".join(p for p in parts if isinstance(p, str))
            if not joined.strip() and not (content.get("text") or "").strip():
                continue
            n += 1
    return n


def _load_conversations_from_zip_or_file(path: str):
    """Return parsed conversations.json (list) or None on any failure."""
    try:
        if path.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    if os.path.basename(name) != "conversations.json":
                        continue
                    with zf.open(name) as f:
                        return json.load(f)
            return None
        if os.path.basename(path) == "conversations.json":
            with open(path, "r", errors="replace") as f:
                return json.load(f)
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError):
        return None
    return None


def _count_web_export(path: str) -> int:
    """Count human turns in a web-chat export.

    Auto-detects claude.ai vs ChatGPT by shape — claude.ai has
    `chat_messages` on each conversation, ChatGPT has `mapping`.
    Returns 0 if the file isn't a recognized web export (so it's safe to
    call this on any zip in the repo).
    """
    convos = _load_conversations_from_zip_or_file(path)
    if not isinstance(convos, list) or not convos:
        return 0
    first = convos[0] if isinstance(convos[0], dict) else {}
    if "chat_messages" in first:
        return _count_claude_web_conversations(convos)
    if "mapping" in first:
        return _count_chatgpt_conversations(convos)
    # Some claude.ai exports lead with an empty conversation. Try the
    # next non-empty one.
    for c in convos:
        if not isinstance(c, dict):
            continue
        if "chat_messages" in c:
            return _count_claude_web_conversations(convos)
        if "mapping" in c:
            return _count_chatgpt_conversations(convos)
    return 0


# The "Claude Exporter" browser extension saves each claude.ai conversation
# as a standalone markdown file beginning with this heading, with human turns
# marked "## 👤 User". Candidates submit a zip or folder of these instead of
# the official conversations.json export. See export_claude_markdown_history.py.
_MD_SIGNATURE = "# Claude Conversation Log"
_MD_USER_TURN = "## \U0001f464"  # "## 👤"

# Canonical "agent-transcribed session" format. This is the same markdown
# shape every export_*.py emits (and the dimension agents read): a file
# headed `# Claude History: <repo>` with human turns on lines beginning
# `**User:**`. It is the universal target produced by
# package-interactive.sh's last-resort transcoder when a candidate used an
# AI tool we have no dedicated packager/exporter for (e.g. VS Code Copilot,
# Windsurf, Aider). Recognizing it here — and, because
# history_utils.assess_session_quality delegates to this counter, in the
# reviewer's gate too — keeps the counter, the gate, and
# export_transcript_history.py (the scorer's ingestion) in lockstep on one
# format. See dist/SESSION_TRANSCRIPT_FORMAT.md.
_TRANSCRIPT_SIGNATURE = "# Claude History"
_TRANSCRIPT_USER_TURN = "**User:**"


def _count_markdown_log_text(text: str) -> int:
    """Count human turns in one conversation-export markdown file.

    Recognizes two signed formats and returns 0 for everything else (README,
    docs, design notes), so it's safe to call on any .md in the repo:
      * Claude Exporter browser extension — `# Claude Conversation Log`
        header, human turns on `## 👤 ...` lines.
      * Canonical agent transcript — `# Claude History:` header, human turns
        on `**User:**` lines (export_*.py output shape).
    """
    head = text[:512]
    if _MD_SIGNATURE in head:
        return sum(
            1 for line in text.splitlines() if line.startswith(_MD_USER_TURN)
        )
    if _TRANSCRIPT_SIGNATURE in head:
        return sum(
            1 for line in text.splitlines()
            if line.startswith(_TRANSCRIPT_USER_TURN)
        )
    return 0


def _count_markdown_export(path: str) -> int:
    """Count human turns in a markdown conversation export (zip, dir, or .md).

    Auto-skips anything that isn't a Claude Exporter-style log, so it's safe
    to call on any zip/markdown file in the repo. Returns 0 on failure.
    """
    total = 0
    try:
        if path.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    if name.endswith("/") or not name.endswith(".md"):
                        continue
                    with zf.open(name) as f:
                        text = f.read().decode("utf-8", errors="replace")
                    total += _count_markdown_log_text(text)
            return total
        if path.endswith(".md"):
            with open(path, "r", errors="replace") as f:
                return _count_markdown_log_text(f.read())
    except (OSError, zipfile.BadZipFile, UnicodeError):
        return 0
    return 0


def main():
    if len(sys.argv) < 2:
        print("Usage: count-session-activity.py <output_dir>", file=sys.stderr)
        sys.exit(2)
    output_dir = sys.argv[1]
    if not os.path.isdir(output_dir):
        print(0)
        return

    # Walk one level deep — candidates occasionally drop raw JSONL files
    # into a `sessions/` subdirectory of session-packages/ instead of
    # tarballing them (e.g. Zachary Braasch's submission). Without the
    # walk we'd miss the entire bundle and report 0.
    total = 0
    for dirpath, dirnames, filenames in os.walk(output_dir):
        # Don't descend further than one level beneath output_dir — anything
        # deeper is unlikely to be a packaged artifact and risks scanning
        # vendor / build dirs if a candidate accidentally bundled them.
        rel = os.path.relpath(dirpath, output_dir)
        if rel != "." and os.sep in rel:
            dirnames[:] = []
            continue
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.isfile(path):
                continue
            if name.endswith(".tar.gz") or name.endswith(".tgz"):
                total += _count_tarball(path)
            elif name.endswith(".vscdb.gz"):
                total += _count_vscdb_gz(path)
            elif name.endswith("api_conversation_history.json"):
                total += _count_raw_json(path)
            elif name.endswith(".jsonl"):
                total += _count_loose_jsonl(path)
            elif name.endswith(".zip") or name == "conversations.json":
                n = _count_web_export(path)
                # A zip with no conversations.json may still be a markdown
                # conversation export (the "Claude Exporter" extension format).
                if n == 0 and name.endswith(".zip"):
                    n = _count_markdown_export(path)
                total += n
            elif name.endswith(".md"):
                total += _count_markdown_export(path)
    print(total)


if __name__ == "__main__":
    main()
