#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_IMAGE="${MARZBAN_NODE_IMAGE:-gozargah/marzban-node:latest}"
TMP="$(mktemp -d)"
PROJECT="mns-runtime-$$"
CONTAINER_NAME="mns-runtime-node-$$"
COMPOSE_FILE="$TMP/docker-compose.yml"
PATCH_DIR="$TMP/patches"
BAD_IMAGE="mns-runtime-incompatible:$$"
UPSTREAM_CID=""

cleanup() {
  docker compose -p "$PROJECT" -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true
  if [ -n "$UPSTREAM_CID" ]; then
    docker rm -f "$UPSTREAM_CID" >/dev/null 2>&1 || true
  fi
  docker image rm -f "$BAD_IMAGE" >/dev/null 2>&1 || true
  rm -rf -- "$TMP"
}
trap cleanup EXIT INT TERM

fail() {
  echo "[FAIL] $*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || fail "run this test as root"
command -v docker >/dev/null 2>&1 || fail "docker is required"
docker compose version >/dev/null

docker pull "$UPSTREAM_IMAGE" >/dev/null
UPSTREAM_CID="$(docker create "$UPSTREAM_IMAGE")"
docker cp "$UPSTREAM_CID:/code/rest_service.py" "$TMP/rest_service.upstream.py"
docker rm -f "$UPSTREAM_CID" >/dev/null
UPSTREAM_CID=""

cp "$TMP/rest_service.upstream.py" "$TMP/rest_service.compat.py"
python3 "$ROOT/lib/patch_rest_service.py" \
  --file "$TMP/rest_service.compat.py" \
  --timeout 7 \
  --grace 60 >/dev/null
python3 -m py_compile "$TMP/rest_service.compat.py"

grep -Fq 'marzban-node-stabilizer: lifecycle-hardening-v2' "$TMP/rest_service.compat.py" \
  || fail "current upstream image source was not patched"

cat > "$COMPOSE_FILE" <<EOF_COMPOSE
services:
  marzban-node:
    image: $UPSTREAM_IMAGE
    container_name: $CONTAINER_NAME
    command: ["sh", "-c", "sleep infinity"]
EOF_COMPOSE

docker compose -p "$PROJECT" -f "$COMPOSE_FILE" up -d marzban-node >/dev/null

run_cli() {
  env \
    CONTAINER_NAME="$CONTAINER_NAME" \
    SERVICE_NAME=marzban-node \
    COMPOSE_FILE="$COMPOSE_FILE" \
    PATCH_DIR="$PATCH_DIR" \
    STARTUP_WAIT_SECONDS=0 \
    START_WAIT_SECONDS=20 \
    "$ROOT/bin/marzban-node-stabilizer" "$@"
}

run_cli apply >/dev/null
MOUNT_SOURCE="$(python3 "$ROOT/lib/compose_mount.py" source --file "$COMPOSE_FILE" --service marzban-node)"
[ "$MOUNT_SOURCE" = "$PATCH_DIR/rest_service.py" ] || fail "apply did not install the expected bind mount"
grep -Fq 'marzban-node-stabilizer: lifecycle-hardening-v2' "$PATCH_DIR/rest_service.py" \
  || fail "host patch marker missing after apply"
docker exec "$CONTAINER_NAME" grep -Fq 'marzban-node-stabilizer: lifecycle-hardening-v2' /code/rest_service.py \
  || fail "container is not using the patched rest_service.py"

run_cli apply >/dev/null
COUNT="$(grep -Fc '/code/rest_service.py' "$COMPOSE_FILE")"
[ "$COUNT" = "1" ] || fail "re-apply produced duplicate rest_service.py mounts"

run_cli restore >/dev/null
MOUNT_SOURCE="$(python3 "$ROOT/lib/compose_mount.py" source --file "$COMPOSE_FILE" --service marzban-node)"
[ -z "$MOUNT_SOURCE" ] || fail "restore left the stabilizer bind mount active"
if docker exec "$CONTAINER_NAME" grep -Fq 'marzban-node-stabilizer: lifecycle-hardening-v2' /code/rest_service.py; then
  fail "restore left patched rest_service.py active"
fi

# Re-apply a valid patch, then switch the underlying image to an intentionally
# incompatible source while keeping the old mount active. A subsequent apply
# must fail and remove the stale stabilizer mount instead of masking new image
# source indefinitely.
run_cli apply >/dev/null
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
cat > "$TMP/bad-image/Dockerfile" <<EOF_DOCKER
FROM $UPSTREAM_IMAGE
COPY rest_service.py /code/rest_service.py
EOF_DOCKER
docker build -q -t "$BAD_IMAGE" "$TMP/bad-image" >/dev/null
python3 - "$COMPOSE_FILE" "$BAD_IMAGE" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
image = sys.argv[2]
text = path.read_text()
lines = text.splitlines()
out = []
replaced = False
for line in lines:
    if not replaced and line.strip().startswith("image:"):
        indent = line[: len(line) - len(line.lstrip())]
        out.append(f"{indent}image: {image}")
        replaced = True
    else:
        out.append(line)
if not replaced:
    raise SystemExit("compose image line not found")
path.write_text("\n".join(out) + "\n")
PY

docker compose -p "$PROJECT" -f "$COMPOSE_FILE" up -d --force-recreate marzban-node >/dev/null
if run_cli apply >"$TMP/incompatible.log" 2>&1; then
  cat "$TMP/incompatible.log" >&2
  fail "incompatible upstream source unexpectedly applied"
fi
MOUNT_SOURCE="$(python3 "$ROOT/lib/compose_mount.py" source --file "$COMPOSE_FILE" --service marzban-node)"
[ -z "$MOUNT_SOURCE" ] || fail "incompatible upstream source left stale stabilizer mount active"
if docker exec "$CONTAINER_NAME" grep -Fq 'marzban-node-stabilizer: lifecycle-hardening-v2' /code/rest_service.py; then
  fail "stale patched source still masks incompatible image source"
fi
docker exec "$CONTAINER_NAME" grep -Fq 'simulated incompatible upstream lifecycle change' /code/rest_service.py \
  || fail "container did not recover to image-provided incompatible source"

printf 'docker runtime validation: OK (%s)\n' "$UPSTREAM_IMAGE"
