#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/frontend"
npm ci
exec npm run dev
