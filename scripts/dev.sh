#!/usr/bin/env bash
# Run the FastAPI backend and the Next.js console together for local development.
#
#   ./scripts/dev.sh              # API on :8000 (from .env), console on :3000
#   ./scripts/dev.sh --reload     # API auto-reloads on code changes (restarts drop a live delegate)
#
# Ctrl+C stops both.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "No virtualenv at .venv/. Create it first:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
if [[ ! -d web/node_modules ]]; then
  echo "Frontend dependencies missing. Run:  (cd web && npm install)" >&2
  exit 1
fi

API_ARGS=()
[[ "${1:-}" == "--reload" ]] && API_ARGS+=(--reload)

cleanup() {
  trap - INT TERM EXIT
  echo
  echo "stopping..."
  kill 0 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "DELEG8 API     -> http://${DELEG8_HOST:-127.0.0.1}:${DELEG8_PORT:-8000}/docs"
echo "DELEG8 console -> http://localhost:3000"
echo

.venv/bin/python -m src.main serve "${API_ARGS[@]}" 2>&1 | sed -u 's/^/[api] /' &
(cd web && npm run dev 2>&1 | sed -u 's/^/[web] /') &
wait
