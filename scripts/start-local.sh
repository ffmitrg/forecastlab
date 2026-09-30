#!/usr/bin/env bash
# Runs only this checkout. Bind loopback: the demo has no user authentication.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PORT="${PORT:-8765}"
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
  echo "PORT must be an integer from 1024 to 65535" >&2; exit 2
fi
if command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
elif [[ -x "$ROOT/../tooling/bin/uv" ]]; then
  UV="$ROOT/../tooling/bin/uv"
else
  echo "uv is required. Install it in your chosen environment, then rerun this script." >&2; exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "Node.js and npm are required; see README.md." >&2; exit 1
fi
"$UV" sync --locked
if [[ ! -f frontend/dist/index.html ]] || [[ "${REBUILD:-0}" == 1 ]]; then
  (cd frontend && npm ci && npm run build)
fi
printf 'ForecastLab: http://127.0.0.1:%s/\n' "$PORT"
printf 'No API keys are needed for the fixed teaching demo. Ctrl+C stops the server.\n'
exec "$UV" run uvicorn app.api:app --app-dir backend --host 127.0.0.1 --port "$PORT"
