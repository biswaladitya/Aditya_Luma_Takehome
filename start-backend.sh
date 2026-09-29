#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
uv sync
exec uv run uvicorn backend.app:app --reload --port 8000
