#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../bin/marzban-node-stabilizer
source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null

fail() {
  echo "[FAIL] $*" >&2
  exit 1
}

TIMEOUT_SECONDS=60
RESTART_GRACE_SECONDS=60
STARTUP_WAIT_SECONDS=70
START_WAIT_SECONDS=15
normalize_settings >/dev/null 2>&1
[ "$TIMEOUT_SECONDS" = "8" ] || fail "TIMEOUT_SECONDS was not clamped to 8"

TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
mkdir -p "$TMP/compose-dir"
COMPOSE_FILE="$TMP/compose-dir/custom-compose.yml"
printf 'services:\n  marzban-node:\n    image: example\n' > "$COMPOSE_FILE"
DOCKER_LOG="$TMP/docker.log"

docker() {
  printf '%s\n' "$*" >> "$DOCKER_LOG"
  return 0
}

compose_exec config -q
expected="compose -f custom-compose.yml config -q"
grep -Fxq "$expected" "$DOCKER_LOG" || fail "compose_exec did not honor custom COMPOSE_FILE with -f"

COMPOSE_CALLS="$TMP/compose-calls.log"
compose_exec() {
  printf '%s\n' "$*" >> "$COMPOSE_CALLS"
  return 0
}
SERVICE_NAME=marzban-node
compose_up force
grep -Fxq "up -d --force-recreate marzban-node" "$COMPOSE_CALLS" || fail "compose_up did not stay scoped to the configured service"

created=0
container_exists() {
  [ "$created" = "1" ]
}
compose_exec() {
  printf '%s\n' "$*" >> "$COMPOSE_CALLS"
  if [ "${1:-}" = "create" ]; then
    created=1
  fi
  return 0
}
ensure_container_exists
[ "$created" = "1" ] || fail "ensure_container_exists did not create the service container"
grep -Fxq "create marzban-node" "$COMPOSE_CALLS" || fail "missing service-scoped compose create"

LOCK_FILE="$TMP/stabilizer.lock"
READY_FILE="$TMP/ready"
(
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  LOCK_FILE="$LOCK_FILE"
  acquire_lock
  : > "$READY_FILE"
  sleep 2
) &
holder_pid=$!

for _ in $(seq 1 20); do
  [ -f "$READY_FILE" ] && break
  sleep 0.05
done
[ -f "$READY_FILE" ] || fail "lock holder did not start"

if (
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  LOCK_FILE="$LOCK_FILE"
  acquire_lock
) >/dev/null 2>&1; then
  fail "second lock acquisition unexpectedly succeeded"
fi

wait "$holder_pid"
echo "shell logic tests: OK"
