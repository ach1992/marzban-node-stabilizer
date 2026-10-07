#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_IMAGE="${MARZBAN_NODE_IMAGE:-gozargah/marzban-node:latest}"
TMP="$(mktemp -d)"
PROJECT="mns-runtime-$$"
CONTAINER_NAME="mns-runtime-node-$$"
COMPOSE_FILE="$TMP/docker-compose.yml"
PATCH_DIR="$TMP/patches"
COMPAT_IMAGE="mns-runtime-compatible:$$"
BAD_IMAGE="mns-runtime-incompatible:$$"
UPSTREAM_CID=""
REST_PORT=""

cleanup() {
  COMPOSE_PROJECT_NAME="$PROJECT" docker compose -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true
  if [ -n "$UPSTREAM_CID" ]; then
    docker rm -f "$UPSTREAM_CID" >/dev/null 2>&1 || true
  fi
  docker image rm -f "$COMPAT_IMAGE" "$BAD_IMAGE" >/dev/null 2>&1 || true
  rm -rf -- "$TMP"
}
trap cleanup EXIT INT TERM

fail() {
  echo "[FAIL] $*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || fail "run this test as root"
command -v docker >/dev/null 2>&1 || fail "docker is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v openssl >/dev/null 2>&1 || fail "openssl is required"
docker compose version >/dev/null

REST_PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"

mkdir -p "$TMP/certs"
openssl req -x509 -newkey rsa:2048 -nodes   -keyout "$TMP/certs/ca.key" -out "$TMP/certs/ca.crt"   -subj "/CN=MNS-Test-CA" -days 1 >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes   -keyout "$TMP/certs/server.key" -out "$TMP/certs/server.csr"   -subj "/CN=localhost" >/dev/null 2>&1
openssl x509 -req -in "$TMP/certs/server.csr"   -CA "$TMP/certs/ca.crt" -CAkey "$TMP/certs/ca.key" -CAcreateserial   -out "$TMP/certs/server.crt" -days 1 >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes   -keyout "$TMP/certs/client.key" -out "$TMP/certs/client.csr"   -subj "/CN=mns-test-client" >/dev/null 2>&1
openssl x509 -req -in "$TMP/certs/client.csr"   -CA "$TMP/certs/ca.crt" -CAkey "$TMP/certs/ca.key" -CAcreateserial   -out "$TMP/certs/client.crt" -days 1 >/dev/null 2>&1

cat > "$TMP/fake-xray" <<'EOF_XRAY'
#!/bin/sh
if [ "${1:-}" = "version" ]; then
  echo "Xray 1.0.0"
  exit 0
fi
if [ "${1:-}" = "run" ]; then
  /bin/sleep 3.8
  echo "Xray 1.0.0 started"
  while :; do /bin/sleep 60; done
fi
exit 1
EOF_XRAY
chmod 0755 "$TMP/fake-xray"

docker pull "$UPSTREAM_IMAGE" >/dev/null
UPSTREAM_CID="$(docker create "$UPSTREAM_IMAGE")"
docker cp "$UPSTREAM_CID:/code/rest_service.py" "$TMP/rest_service.upstream.py"
docker rm -f "$UPSTREAM_CID" >/dev/null
UPSTREAM_CID=""

cp "$TMP/rest_service.upstream.py" "$TMP/rest_service.compat.py"
python3 "$ROOT/lib/patch_rest_service.py"   --file "$TMP/rest_service.compat.py"   --timeout 4   --grace 60 >/dev/null
python3 -m py_compile "$TMP/rest_service.compat.py"

grep -Fq 'marzban-node-stabilizer: lifecycle-hardening-v2' "$TMP/rest_service.compat.py"   || fail "current upstream image source was not patched"

cat > "$COMPOSE_FILE" <<EOF_COMPOSE
services:
  marzban-node:
    image: $UPSTREAM_IMAGE
    container_name: $CONTAINER_NAME
    environment:
      SERVICE_PROTOCOL: rest
      SERVICE_HOST: 0.0.0.0
      SERVICE_PORT: "62050"
      SSL_CERT_FILE: /test-certs/server.crt
      SSL_KEY_FILE: /test-certs/server.key
      SSL_CLIENT_CERT_FILE: /test-certs/ca.crt
      XRAY_EXECUTABLE_PATH: /test-bin/fake-xray
    volumes:
      - "$TMP/certs:/test-certs:ro"
      - "$TMP/fake-xray:/test-bin/fake-xray:ro"
    ports:
      - "127.0.0.1:$REST_PORT:62050"
EOF_COMPOSE

compose() {
  COMPOSE_PROJECT_NAME="$PROJECT" docker compose -f "$COMPOSE_FILE" "$@"
}

run_cli() {
  env     COMPOSE_PROJECT_NAME="$PROJECT"     CONTAINER_NAME="$CONTAINER_NAME"     SERVICE_NAME=marzban-node     COMPOSE_FILE="$COMPOSE_FILE"     PATCH_DIR="$PATCH_DIR"     TIMEOUT_SECONDS=4     STARTUP_WAIT_SECONDS=0     START_WAIT_SECONDS=20     "$ROOT/bin/marzban-node-stabilizer" "$@"
}

api_request() {
  local path="$1"
  local payload="$2"
  local body_file="$3"
  local code_file="$4"
  local max_time="${5:-12}"
  curl -skS     --cert "$TMP/certs/client.crt"     --key "$TMP/certs/client.key"     --connect-timeout 2     --max-time "$max_time"     -H 'Content-Type: application/json'     -d "$payload"     -o "$body_file"     -w '%{http_code}'     "https://127.0.0.1:$REST_PORT$path" > "$code_file"
}

wait_for_rest() {
  local code
  for _ in $(seq 1 60); do
    code="$(curl -sk       --cert "$TMP/certs/client.crt"       --key "$TMP/certs/client.key"       --connect-timeout 1       --max-time 2       -H 'Content-Type: application/json'       -d '{}'       -o /dev/null       -w '%{http_code}'       "https://127.0.0.1:$REST_PORT/" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then
      return 0
    fi
    sleep 0.25
  done
  docker logs "$CONTAINER_NAME" >&2 || true
  return 1
}

connect_session() {
  local body="$TMP/connect.$RANDOM.json"
  local code="$TMP/connect.$RANDOM.code"
  api_request "/connect" '{}' "$body" "$code" 4
  [ "$(cat "$code")" = "200" ] || {
    cat "$body" >&2
    fail "connect returned HTTP $(cat "$code")"
  }
  python3 - "$body" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1]))["session_id"])
PY
}

session_payload() {
  python3 - "$1" <<'PY'
import json
import sys
print(json.dumps({"session_id": sys.argv[1]}))
PY
}

lifecycle_payload() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
print(json.dumps({"session_id": sys.argv[1], "config": sys.argv[2]}))
PY
}

monotonic_ms() {
  python3 - <<'PY'
import time
print(time.monotonic_ns() // 1_000_000)
PY
}

assert_fast_request() {
  local path="$1"
  local payload="$2"
  local budget_ms="$3"
  local label="$4"
  local body="$TMP/${label}.body"
  local code="$TMP/${label}.code"
  local before after elapsed
  before="$(monotonic_ms)"
  api_request "$path" "$payload" "$body" "$code" 5
  after="$(monotonic_ms)"
  elapsed=$((after - before))
  [ "$(cat "$code")" = "200" ] || {
    cat "$body" >&2
    fail "$label returned HTTP $(cat "$code")"
  }
  [ "$elapsed" -lt "$budget_ms" ] || fail "$label took ${elapsed}ms (budget ${budget_ms}ms)"
}

set_compose_image() {
  python3 - "$COMPOSE_FILE" "$1" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
image = sys.argv[2]
lines = path.read_text().splitlines()
out = []
changed = False
for line in lines:
    if not changed and line.strip().startswith("image:"):
        indent = line[: len(line) - len(line.lstrip())]
        out.append(f"{indent}image: {image}")
        changed = True
    else:
        out.append(line)
if not changed:
    raise SystemExit("compose image line not found")
path.write_text("\n".join(out) + "\n")
PY
}

compose up -d marzban-node >/dev/null
wait_for_rest || fail "real Marzban-node REST service did not become ready"

# Simulate an interrupted prior apply: Compose already contains the owned mount,
# but the host patch was never installed and the running container was never recreated.
mkdir -p "$PATCH_DIR"
python3 "$ROOT/lib/compose_mount.py" add   --file "$COMPOSE_FILE"   --service marzban-node   --patch-file "$PATCH_DIR/rest_service.py" >/dev/null
rm -f "$PATCH_DIR/rest_service.py" "$PATCH_DIR/rest_service.py.original_backup" "$PATCH_DIR/metadata.env"
run_cli apply >/dev/null
wait_for_rest || fail "REST service did not recover after interrupted-state apply"

MOUNT_SOURCE="$(python3 "$ROOT/lib/compose_mount.py" source --file "$COMPOSE_FILE" --service marzban-node)"
[ "$MOUNT_SOURCE" = "$PATCH_DIR/rest_service.py" ] || fail "apply did not install the expected bind mount"
grep -Fq 'marzban-node-stabilizer: lifecycle-hardening-v2' "$PATCH_DIR/rest_service.py"   || fail "host patch marker missing after apply"
docker exec "$CONTAINER_NAME" grep -Fq 'marzban-node-stabilizer: lifecycle-hardening-v2' /code/rest_service.py   || fail "container is not using the patched rest_service.py"

# Minimal-image diagnostics must not require pgrep/ss. Create a harmless process
# whose Linux comm name is exactly xray, then exercise the real /proc-based
# process/listener inspection against the running upstream container.
docker cp "$CONTAINER_NAME:/bin/sleep" "$TMP/xray" >/dev/null
docker cp "$TMP/xray" "$CONTAINER_NAME:/tmp/xray" >/dev/null
XRAY_PROBE_PID="$(docker exec "$CONTAINER_NAME" sh -c '/tmp/xray 30 >/dev/null 2>&1 & echo $!')"
sleep 0.2
STATUS_PROBE="$TMP/minimal-status.log"
run_cli status >"$STATUS_PROBE" 2>&1
grep -Eq 'PID=[0-9]+ NAME=xray' "$STATUS_PROBE" \
  || { cat "$STATUS_PROBE" >&2; fail "status did not detect Xray through /proc"; }
grep -Fq 'SERVICE_PORT 62050: listening' "$STATUS_PROBE" \
  || { cat "$STATUS_PROBE" >&2; fail "status did not detect the REST listener through /proc/net/tcp*"; }
if grep -Fq 'pgrep: not found' "$STATUS_PROBE" || grep -Fq "'ss' is unavailable" "$STATUS_PROBE"; then
  cat "$STATUS_PROBE" >&2
  fail "minimal-image status still depends on pgrep/ss"
fi

before="$(monotonic_ms)"
(
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  CONTAINER_NAME="$CONTAINER_NAME"
  STARTUP_WAIT_SECONDS=5
  wait_for_xray_or_timeout >/dev/null
)
after="$(monotonic_ms)"
[ $((after - before)) -lt 2000 ] || fail "dependency-free Xray observation did not exit promptly"

docker exec "$CONTAINER_NAME" sh -c "kill $XRAY_PROBE_PID" >/dev/null 2>&1 || true
rm -f "$TMP/xray"

run_cli apply >/dev/null
wait_for_rest || fail "REST service did not recover after idempotent re-apply"
COUNT="$(grep -Fc '/code/rest_service.py' "$COMPOSE_FILE")"
[ "$COUNT" = "1" ] || fail "re-apply produced duplicate rest_service.py mounts"

CONFIG_A='{"log":{"loglevel":"warning"},"inbounds":[],"outbounds":[{"protocol":"freedom","tag":"direct"}]}'
CONFIG_B='{"log":{"loglevel":"warning"},"inbounds":[],"outbounds":[{"protocol":"freedom","tag":"direct-b"}]}'

# Actual REST concurrency: connect must not wait behind start readiness.
SESSION_1="$(connect_session)"
START_PAYLOAD="$(lifecycle_payload "$SESSION_1" "$CONFIG_A")"
api_request "/start" "$START_PAYLOAD" "$TMP/start-connect.body" "$TMP/start-connect.code" 10 &
START_PID=$!
sleep 3.2
before="$(monotonic_ms)"
SESSION_2="$(connect_session)"
after="$(monotonic_ms)"
[ $((after - before)) -lt 2500 ] || fail "connect blocked behind start readiness"
wait "$START_PID"
[ "$(cat "$TMP/start-connect.code")" = "200" ] || {
  cat "$TMP/start-connect.body" >&2
  fail "superseded start did not complete without a caller-error path"
}
api_request "/" '{}' "$TMP/state-after-connect.body" "$TMP/state-after-connect.code" 4
[ "$(cat "$TMP/state-after-connect.code")" = "200" ] || fail "could not read state after connect superseded start"
python3 - "$TMP/state-after-connect.body" <<'PY2'
import json, sys
body = json.load(open(sys.argv[1]))
assert body["connected"] is True, body
assert body["started"] is False, body
PY2

# Disconnect must also remain inside its 3-second upstream caller budget.
START_PAYLOAD="$(lifecycle_payload "$SESSION_2" "$CONFIG_A")"
api_request "/start" "$START_PAYLOAD" "$TMP/start-disconnect.body" "$TMP/start-disconnect.code" 10 &
START_PID=$!
sleep 3.2
assert_fast_request "/disconnect" "$(session_payload "$SESSION_2")" 2500 "disconnect-during-start"
wait "$START_PID"
[ "$(cat "$TMP/start-disconnect.code")" = "200" ] || fail "superseded start did not complete without a caller-error path"
api_request "/" '{}' "$TMP/state-after-disconnect.body" "$TMP/state-after-disconnect.code" 4
[ "$(cat "$TMP/state-after-disconnect.code")" = "200" ] || fail "could not read state after disconnect superseded start"
python3 - "$TMP/state-after-disconnect.body" <<'PY2'
import json, sys
body = json.load(open(sys.argv[1]))
assert body["connected"] is False, body
assert body["started"] is False, body
PY2

# Establish a running core, then prove stop is not held behind restart readiness.
SESSION_3="$(connect_session)"
START_PAYLOAD="$(lifecycle_payload "$SESSION_3" "$CONFIG_A")"
api_request "/start" "$START_PAYLOAD" "$TMP/start-normal.body" "$TMP/start-normal.code" 10
[ "$(cat "$TMP/start-normal.code")" = "200" ] || {
  cat "$TMP/start-normal.body" >&2
  fail "normal patched start did not succeed"
}
RESTART_PAYLOAD="$(lifecycle_payload "$SESSION_3" "$CONFIG_B")"
api_request "/restart" "$RESTART_PAYLOAD" "$TMP/restart-stop.body" "$TMP/restart-stop.code" 10 &
RESTART_PID=$!
sleep 3.2
assert_fast_request "/stop" "$(session_payload "$SESSION_3")" 3500 "stop-during-restart"
wait "$RESTART_PID"
[ "$(cat "$TMP/restart-stop.code")" = "200" ] || fail "superseded restart did not complete without a caller-error path"
api_request "/" '{}' "$TMP/state-after-stop.body" "$TMP/state-after-stop.code" 4
[ "$(cat "$TMP/state-after-stop.code")" = "200" ] || fail "could not read state after stop superseded restart"
python3 - "$TMP/state-after-stop.body" <<'PY2'
import json, sys
body = json.load(open(sys.argv[1]))
assert body["connected"] is True, body
assert body["started"] is False, body
PY2

# Positive restore: owned mount is removed and image-provided source becomes visible.
run_cli restore >/dev/null
wait_for_rest || fail "REST service did not recover after restore"
MOUNT_SOURCE="$(python3 "$ROOT/lib/compose_mount.py" source --file "$COMPOSE_FILE" --service marzban-node)"
[ -z "$MOUNT_SOURCE" ] || fail "restore left the stabilizer bind mount active"
if docker exec "$CONTAINER_NAME" grep -Fq 'marzban-node-stabilizer: lifecycle-hardening-v2' /code/rest_service.py; then
  fail "restore left patched rest_service.py active"
fi

# Restore must refuse a foreign mount without mutating Compose or recreating the service.
cp "$TMP/rest_service.upstream.py" "$TMP/foreign-rest_service.py"
python3 - "$COMPOSE_FILE" "$TMP/foreign-rest_service.py" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
source = sys.argv[2]
text = path.read_text()
needle = "    volumes:\n"
if needle not in text:
    raise SystemExit("volumes section not found")
path.write_text(text.replace(needle, needle + f'      - "{source}:/code/rest_service.py:ro"\n', 1))
PY
cp "$COMPOSE_FILE" "$TMP/compose.foreign.before"
if run_cli restore >"$TMP/foreign-restore.log" 2>&1; then
  fail "restore unexpectedly removed a foreign rest_service.py mount"
fi
cmp -s "$COMPOSE_FILE" "$TMP/compose.foreign.before" || fail "foreign-mount restore mutated Compose"
grep -Fq "foreign mount source" "$TMP/foreign-restore.log"   || fail "foreign-mount restore refusal was not explicit"
python3 - "$COMPOSE_FILE" "$TMP/foreign-rest_service.py" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
source = sys.argv[2]
line = f'      - "{source}:/code/rest_service.py:ro"\n'
text = path.read_text()
if line not in text:
    raise SystemExit("foreign mount line missing")
path.write_text(text.replace(line, "", 1))
PY

# Re-apply on image A, then change Compose to compatible image B without
# recreating first. Apply must detect that recreation moved to B, rebase the
# patch onto B, and finish with metadata bound to B.
run_cli apply >/dev/null
mkdir -p "$TMP/compatible-image"
cat > "$TMP/compatible-image/Dockerfile" <<EOF_COMPAT
FROM $UPSTREAM_IMAGE
RUN printf 'compatible-image-b\n' > /mns-compatible-image
EOF_COMPAT
docker build -q -t "$COMPAT_IMAGE" "$TMP/compatible-image" >/dev/null
set_compose_image "$COMPAT_IMAGE"
run_cli apply >/dev/null
wait_for_rest || fail "REST service did not recover after compatible image transition"
ACTUAL_IMAGE="$(docker container inspect -f '{{.Image}}' "$CONTAINER_NAME")"
APPLIED_IMAGE="$(awk -F= '$1 == "IMAGE_ID" {print $2; exit}' "$PATCH_DIR/metadata.env")"
[ "$ACTUAL_IMAGE" = "$APPLIED_IMAGE" ] || fail "patch metadata was not rebased to the recreated compatible image"

# Change Compose to incompatible image C while the container is still B.
# Apply must never report success with B-derived source masking C; it must
# disable the stale mount and leave the service on C's image-provided source.
mkdir -p "$TMP/bad-image"
cp "$TMP/rest_service.upstream.py" "$TMP/bad-image/rest_service.py"
python3 - "$TMP/bad-image/rest_service.py" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
needle = "        self.connected = True\n"
if needle not in text:
    raise SystemExit("upstream connect fixture changed")
path.write_text(text.replace(needle, needle + "        # simulated incompatible upstream lifecycle change\n", 1))
PY
cat > "$TMP/bad-image/Dockerfile" <<EOF_BAD
FROM $UPSTREAM_IMAGE
COPY rest_service.py /code/rest_service.py
EOF_BAD
docker build -q -t "$BAD_IMAGE" "$TMP/bad-image" >/dev/null
set_compose_image "$BAD_IMAGE"
if run_cli apply >"$TMP/incompatible.log" 2>&1; then
  cat "$TMP/incompatible.log" >&2
  fail "incompatible image transition unexpectedly applied"
fi
wait_for_rest || fail "service did not recover on incompatible image-provided source"
MOUNT_SOURCE="$(python3 "$ROOT/lib/compose_mount.py" source --file "$COMPOSE_FILE" --service marzban-node)"
[ -z "$MOUNT_SOURCE" ] || fail "incompatible image transition left stale stabilizer mount active"
ACTUAL_IMAGE="$(docker container inspect -f '{{.Image}}' "$CONTAINER_NAME")"
BAD_IMAGE_ID="$(docker image inspect -f '{{.Id}}' "$BAD_IMAGE")"
[ "$ACTUAL_IMAGE" = "$BAD_IMAGE_ID" ] || fail "container did not remain on the incompatible target image after fail-closed recovery"
if docker exec "$CONTAINER_NAME" grep -Fq 'marzban-node-stabilizer: lifecycle-hardening-v2' /code/rest_service.py; then
  fail "old patched source still masks incompatible image source"
fi
docker exec "$CONTAINER_NAME" grep -Fq 'simulated incompatible upstream lifecycle change' /code/rest_service.py   || fail "container did not expose the incompatible image-provided source"

printf 'docker runtime validation: OK (%s)\n' "$UPSTREAM_IMAGE"
