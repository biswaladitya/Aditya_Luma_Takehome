#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
project_root="$PWD"
log_dir="$project_root/.run"
mkdir -p "$log_dir"

listener_pids() {
  lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

stop_project_server() {
  local port="$1" expected="$2" pid command
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$command" == *"$expected"* ]]; then
      echo "Stopping project server on port $port (PID $pid)"
      kill "$pid"
    fi
  done < <(listener_pids "$port")
}

stop_project_server 5173 "$project_root/frontend/node_modules/.bin/vite"
stop_project_server 8000 "$project_root/.venv/bin/uvicorn backend.app:app"

for attempt in {1..40}; do
  if [ -z "$(listener_pids 5173)" ] && [ -z "$(listener_pids 8000)" ]; then
    break
  fi
  sleep 0.25
done

if [ -n "$(listener_pids 5173)" ] || [ -n "$(listener_pids 8000)" ]; then
  echo "A server is still using port 5173 or 8000. Stop it manually, then retry." >&2
  exit 1
fi

"$project_root/start-backend.sh" > "$log_dir/backend.log" 2>&1 &
backend_pid=$!
trap 'kill "$backend_pid" 2>/dev/null || true' EXIT
echo "Backend starting (log: .run/backend.log)"
echo "Frontend starting. Keep this terminal open; press Ctrl+C to stop both."
echo "Open http://localhost:5173 once both servers are ready."
"$project_root/start-frontend.sh"
