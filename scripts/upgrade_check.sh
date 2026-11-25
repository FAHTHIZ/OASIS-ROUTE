#!/usr/bin/env bash
# Verify the upgrade path from a previous release with real processes and real data.
#
#   scripts/upgrade_check.sh [<previous-git-ref>]
#
# 1. Checks out the previous release in a temporary worktree and runs it (`oasis-route dev`: SQLite, two
#    mock providers, API and embedded worker) against a fresh state directory.
# 2. Makes an inference request with the Oasis Key it issued, so the database holds a user, project, key,
#    two vault-encrypted provider connections, learned quota pools, usage and routing history.
# 3. Stops it and starts the current code on the same state directory (migrations run on start).
# 4. Asserts the schema is current, the old Oasis Key still authenticates, the old encrypted connections
#    still serve a request, the earlier usage is still there, and the background worker is running.
set -euo pipefail

PREV_REF="${1:-${OASIS_UPGRADE_FROM:-a775cea8655c}}"   # a775cea8655c = last 0.1.0 commit on main
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
STATE="$WORK/state"
PORT="${OASIS_UPGRADE_PORT:-18080}"
MOCK_PORT="${OASIS_UPGRADE_MOCK_PORT:-19100}"
BASE="http://127.0.0.1:$PORT"
PID=""

cleanup() {
  [ -n "$PID" ] && kill "$PID" 2>/dev/null && wait "$PID" 2>/dev/null || true
  git -C "$ROOT" worktree remove --force "$WORK/prev" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# Stable secrets across both versions, as in a real deployment.
export OASIS_ROUTE_MASTER_KEY="$(python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())')"
export OASIS_ROUTE_API_KEY_PEPPER="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
export OASIS_ROUTE_WORKER_TICK_S=1
unset UV_NATIVE_TLS 2>/dev/null || true

start() {  # $1 = project dir, $2 = log file
  (cd "$1" && exec uv run --frozen oasis-route dev --state-dir "$STATE" --port "$PORT" --mock-port "$MOCK_PORT") \
    >"$2" 2>&1 &
  PID=$!
  for _ in $(seq 1 120); do
    if curl -fsS "$BASE/readyz" >/dev/null 2>&1 && [ -s "$STATE/admin.key" ]; then return 0; fi
    if ! kill -0 "$PID" 2>/dev/null; then cat "$2"; echo "server exited" >&2; return 1; fi
    sleep 1
  done
  cat "$2"; echo "server did not become ready" >&2; return 1
}

stop() { kill "$PID"; wait "$PID" 2>/dev/null || true; PID=""; }

chat() {
  curl -fsS "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
    -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"$1\"}]}"
}

echo "== previous release: $PREV_REF"
git -C "$ROOT" worktree add --detach "$WORK/prev" "$PREV_REF" >/dev/null
(cd "$WORK/prev" && uv sync --frozen -q)
start "$WORK/prev" "$WORK/prev.log"
PROJECT="$(sed -n 1p "$STATE/admin.key")"
KEY="$(sed -n 2p "$STATE/admin.key")"
OLD_REV="$(curl -fsS "$BASE/readyz" | python3 -c 'import json,sys;print(json.load(sys.stdin)["schema_revision"])')"
chat "before upgrade" | python3 -c 'import json,sys;d=json.load(sys.stdin);assert d["choices"][0]["message"]["content"]'
OLD_USAGE="$(curl -fsS "$BASE/api/v1/projects/$PROJECT/usage" -H "Authorization: Bearer $KEY" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
stop
echo "   schema $OLD_REV, $OLD_USAGE usage record(s) written"

echo "== current code"
start "$ROOT" "$WORK/new.log"
python3 - "$BASE" "$PROJECT" "$KEY" "$OLD_REV" "$OLD_USAGE" <<'PY'
import json, sys, time, urllib.request

base, project, key, old_rev, old_usage = sys.argv[1:6]
H = {"Authorization": f"Bearer {key}", "content-type": "application/json"}

def get(path):
    with urllib.request.urlopen(urllib.request.Request(base + path, headers=H)) as r:
        return json.load(r)

def post(path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=H, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.load(r)

ready = get("/readyz")
heads = sorted(p.split("_")[0] for p in __import__("os").listdir("src/oasis_route/db/migrations/versions") if p[0].isdigit())
assert ready["status"] == "ready" and ready["schema_revision"] == heads[-1], ready
assert ready["schema_revision"] >= old_rev
me = get("/api/v1/me")
assert me["project"]["id"] == project, me
conns = get(f"/api/v1/projects/{project}/connections")
assert len(conns) >= 2 and all(c["status"] == "active" for c in conns), conns
out = post("/v1/chat/completions", {"model": "auto", "messages": [{"role": "user", "content": "after upgrade"}]})
assert out["choices"][0]["message"]["content"] and out["oasis_route"]["provider"], out
usage = get(f"/api/v1/projects/{project}/usage")
assert len(usage) >= int(old_usage) + 1, usage
for _ in range(30):
    if get("/readyz").get("worker") == "running":
        break
    time.sleep(1)
else:
    raise SystemExit("worker not running after upgrade")
print(f"   upgraded {old_rev} -> {ready['schema_revision']}: key, encrypted connections, history and worker OK")
PY
stop
echo "upgrade check passed"
