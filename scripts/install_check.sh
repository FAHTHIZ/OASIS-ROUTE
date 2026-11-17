#!/usr/bin/env bash
# Verify the documented pip install path from a freshly built wheel, outside the development environment.
#
#   scripts/install_check.sh
#
# Builds the sdist and wheel, installs the wheel with plain pip into a new virtualenv (no uv, no source
# tree on the path), then: migrate, bootstrap, serve with the embedded worker, and check readiness, the
# console, the OpenAPI schema and an authenticated management call.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
PORT="${OASIS_INSTALL_PORT:-18090}"
PID=""
cleanup() { [ -n "$PID" ] && kill "$PID" 2>/dev/null; wait 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT
unset UV_NATIVE_TLS 2>/dev/null || true

(cd "$ROOT" && uv build -q --out-dir "$WORK/dist")
ls "$WORK/dist"/*.whl "$WORK/dist"/*.tar.gz >/dev/null

python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q "$WORK"/dist/*.whl
cd "$WORK"   # make sure the installed package, not the source tree, is imported

# Production requires PostgreSQL: pass OASIS_INSTALL_DATABASE_URL (CI does). Locally, SQLite in development mode.
if [ -n "${OASIS_INSTALL_DATABASE_URL:-}" ]; then
  export OASIS_ROUTE_ENVIRONMENT=production OASIS_ROUTE_DATABASE_URL="$OASIS_INSTALL_DATABASE_URL"
else
  export OASIS_ROUTE_ENVIRONMENT=development OASIS_ROUTE_DATABASE_URL="sqlite+aiosqlite:///$WORK/oasis.db"
  export OASIS_ROUTE_ENABLE_MOCK_PROVIDERS=false
fi
export OASIS_ROUTE_MASTER_KEY="$(python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())')"
export OASIS_ROUTE_API_KEY_PEPPER="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
BIN="$WORK/venv/bin/oasis-route"

"$BIN" --help >/dev/null
"$BIN" migrate up
KEY="$("$BIN" bootstrap --email admin@example.com 2>/dev/null | sed -n 's/^OASIS_API_KEY=//p')"
test -n "$KEY"

"$BIN" serve --port "$PORT" >"$WORK/serve.log" 2>&1 &
PID=$!
for _ in $(seq 1 60); do curl -fsS "http://127.0.0.1:$PORT/readyz" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$PORT/readyz" | grep -q '"status":"ready"' || { cat "$WORK/serve.log"; exit 1; }
curl -fsS "http://127.0.0.1:$PORT/console/" | grep -qi '<html' || { echo "console not packaged"; exit 1; }
curl -fsS "http://127.0.0.1:$PORT/openapi.json" | grep -q '/v1/chat/completions'
curl -fsS "http://127.0.0.1:$PORT/api/v1/me" -H "Authorization: Bearer $KEY" | grep -q '"role":"admin"'
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/readyz" | grep -q '"worker":"running"' && break; sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/readyz" | grep -q '"worker":"running"' || { cat "$WORK/serve.log"; exit 1; }
echo "install check passed: $(ls "$WORK"/dist/*.whl | xargs -n1 basename)"
