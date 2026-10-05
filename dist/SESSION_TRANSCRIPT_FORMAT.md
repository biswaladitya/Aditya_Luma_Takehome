# Canonical session-transcript format

This is the **universal** format for capturing an AI coding-assistant session
when no dedicated packager exists for the tool you used (e.g. GitHub Copilot in
VS Code, Windsurf, Aider, Gemini CLI, or a web chat exported in an unusual
shape). The deterministic packagers (`package-all-sessions.sh` and friends)
handle Claude Code, Codex, Cursor, and Cline directly; everything else can be
transcribed into the format below and dropped into `session-packages/`.

`package-interactive.sh` drives a local Claude agent to produce this
automatically as a last resort. You can also write it by hand.

## Why this exact shape

It is the same markdown every `export_*.py` already emits and the behavior
reviewer already reads. The submission-time counter
(`dist/count-session-activity.py`), the reviewer's scoring gate, and the
reviewer's ingestion (`export_transcript_history.py`) all recognize files in
this format — so a transcript that clears the counter is the same file the
reviewer scores. No other hand-writable format satisfies all three.

## The format

A UTF-8 markdown file (any name ending in `.md`, placed in
`session-packages/`) that:

1. **Begins with** an H1 header line: `# Claude History: <repo-or-tool-name>`
   (the literal text `# Claude History` is the signature — it must appear in
   the first ~500 bytes).
2. Has one or more session blocks, each introduced by a line:
   `## Session: <YYYY-MM-DD HH:MM> (source: <tool-name>)`
3. Represents every **human turn** as a line beginning with `**User:**`
   followed by the user's verbatim message.
4. Represents every **assistant turn** as a line beginning with `**Claude:**`
   (tool calls may be summarized, e.g. `**Claude:** → Bash (x3), Edit`).

Only `**User:**` lines are counted as user messages, so each real human turn
must be its own `**User:**` line. The capture must contain at least **15**
user turns to clear the scoring gate.

## Example

```markdown
# Claude History: my-take-home

## Session: 2026-06-17 14:58 (source: copilot-vscode)

**User:** I want to build an app that books appointments by phone. Let's plan
the architecture before writing code.

**Claude:** Here's a design with a realtime voice model plus an extraction
model. → Read, Bash

**User:** Use Azure OpenAI only — I have $100 of credits and gpt-realtime.

**Claude:** Updated the plan to target the Azure v1 Foundry endpoint.
```

## Rules

- Only include sessions tied to **this** challenge/repo.
- Do **not** fabricate content — transcribe what actually happened.
- Preserve the user's wording on `**User:**` lines; assistant text may be
  summarized/truncated.
