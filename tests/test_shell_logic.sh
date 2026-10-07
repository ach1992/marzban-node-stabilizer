#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../bin/marzban-node-stabilizer
source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null

fail() {
  echo "[FAIL] $*" >&2
  exit 1
}

[ "$STARTUP_WAIT_SECONDS" = "30" ] || fail "default STARTUP_WAIT_SECONDS is not 30 seconds"
TIMEOUT_SECONDS=60
RESTART_GRACE_SECONDS=60
STARTUP_WAIT_SECONDS=70
START_WAIT_SECONDS=15
normalize_settings >/dev/null 2>&1
[ "$TIMEOUT_SECONDS" = "7" ] || fail "TIMEOUT_SECONDS was not clamped to 7"
[ "$STARTUP_WAIT_SECONDS" = "70" ] || fail "STARTUP_WAIT_SECONDS override was not preserved"

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

# Status inspection must preserve the Compose helper's distinction between
# "no target mount" (1) and "unsupported/unsafe layout" (2).
COMPOSE_EDITOR_PATH="$TMP/fake-compose-editor.py"
cat > "$COMPOSE_EDITOR_PATH" <<'PY_COMPOSE_RC'
import sys
sys.exit(2)
PY_COMPOSE_RC
if compose_has_rest_service_mount >/dev/null 2>&1; then
  fail "unsupported Compose helper result was incorrectly treated as success"
else
  rc=$?
fi
[ "$rc" = "2" ] || fail "unsupported Compose helper result was not preserved"

cat > "$COMPOSE_EDITOR_PATH" <<'PY_COMPOSE_NONE'
import sys
sys.exit(1)
PY_COMPOSE_NONE
if compose_has_rest_service_mount >/dev/null 2>&1; then
  fail "missing Compose target mount was incorrectly treated as success"
else
  rc=$?
fi
[ "$rc" = "1" ] || fail "missing Compose target mount did not preserve rc=1"

# Diagnostics must derive effective service/API ports from the running container
# rather than hardcoding Marzban defaults.
docker() {
  if [ "${1:-}" = "container" ] && [ "${2:-}" = "inspect" ]; then
    cat <<'EOF_ENV'
SERVICE_PORT=62060
XRAY_API_PORT=62061
EOF_ENV
    return 0
  fi
  return 1
}
CONTAINER_NAME=marzban-node
[ "$(container_env_value SERVICE_PORT)" = "62060" ] || fail "SERVICE_PORT was not read from container env"
[ "$(container_env_value XRAY_API_PORT)" = "62061" ] || fail "XRAY_API_PORT was not read from container env"
[ "$(effective_container_port SERVICE_PORT 62050)" = "62060" ] || fail "effective SERVICE_PORT ignored container env"
[ "$(effective_container_port XRAY_API_PORT 62051)" = "62061" ] || fail "effective XRAY_API_PORT ignored container env"

docker() {
  if [ "${1:-}" = "container" ] && [ "${2:-}" = "inspect" ]; then
    return 0
  fi
  return 1
}
[ "$(effective_container_port XRAY_API_PORT 62051)" = "62051" ] || fail "XRAY_API_PORT default was not used when unset"

docker() {
  if [ "${1:-}" = "container" ] && [ "${2:-}" = "inspect" ]; then
    printf '%s\n' 'XRAY_API_PORT=not-a-port'
    return 0
  fi
  return 1
}
if effective_container_port XRAY_API_PORT 62051 >/dev/null 2>&1; then
  fail "invalid XRAY_API_PORT was accepted"
fi

docker() {
  if [ "${1:-}" = "container" ] && [ "${2:-}" = "inspect" ]; then
    printf '%s\n' 'XRAY_API_PORT='
    return 0
  fi
  return 1
}
if effective_container_port XRAY_API_PORT 62051 >/dev/null 2>&1; then
  fail "explicitly empty XRAY_API_PORT incorrectly fell back to the default"
fi

docker() {
  if [ "${1:-}" = "container" ] && [ "${2:-}" = "inspect" ]; then
    printf '%s\n' 'SERVICE_PORT='
    return 0
  fi
  return 1
}
if effective_container_port SERVICE_PORT 62050 >/dev/null 2>&1; then
  fail "explicitly empty SERVICE_PORT incorrectly fell back to the default"
fi

docker() {
  if [ "${1:-}" = "container" ] && [ "${2:-}" = "inspect" ]; then
    cat <<'EOF_DUP_EMPTY'
XRAY_API_PORT=62061
XRAY_API_PORT=
EOF_DUP_EMPTY
    return 0
  fi
  return 1
}
if effective_container_port XRAY_API_PORT 62051 >/dev/null 2>&1; then
  fail "last duplicate empty XRAY_API_PORT was not treated as the effective invalid value"
fi

docker() {
  if [ "${1:-}" = "container" ] && [ "${2:-}" = "inspect" ]; then
    cat <<'EOF_DUP_VALID'
XRAY_API_PORT=
XRAY_API_PORT=62062
EOF_DUP_VALID
    return 0
  fi
  return 1
}
[ "$(effective_container_port XRAY_API_PORT 62051)" = "62062" ] \
  || fail "last duplicate valid XRAY_API_PORT was not used"

docker() {
  if [ "${1:-}" = "container" ] && [ "${2:-}" = "inspect" ]; then
    return 2
  fi
  return 1
}
if effective_container_port XRAY_API_PORT 62051 >/dev/null 2>&1; then
  fail "container inspect failure incorrectly fell back to the default port"
fi

# Stabilizer mount ownership requires path + marker + metadata/hash identity.
PATCH_DIR="$TMP/patches"
PATCH_FILE="$PATCH_DIR/rest_service.py"
METADATA_FILE="$PATCH_DIR/metadata.env"
mkdir -p "$PATCH_DIR"
printf '# marzban-node-stabilizer: lifecycle-hardening-v2\npass\n' > "$PATCH_FILE"
PATCH_SHA="$(sha256sum "$PATCH_FILE" | awk '{print $1}')"
cat > "$METADATA_FILE" <<EOF_METADATA
PATCH_FORMAT_VERSION=2
PATCH_SHA256=$PATCH_SHA
EOF_METADATA
verify_stabilizer_mount_identity "$PATCH_FILE" || fail "valid Stabilizer mount ownership was not recognized"
printf 'PATCH_FORMAT_VERSION=2\nPATCH_SHA256=deadbeef\n' > "$METADATA_FILE"
if verify_stabilizer_mount_identity "$PATCH_FILE"; then
  fail "invalid Stabilizer metadata/hash was accepted as ownership proof"
fi

# restore must refuse a foreign target mount before mutating Compose.
FOREIGN_COMPOSE="$TMP/foreign-compose.yml"
printf 'services:\n  marzban-node:\n    image: example\n' > "$FOREIGN_COMPOSE"
FOREIGN_BEFORE="$(sha256sum "$FOREIGN_COMPOSE" | awk '{print $1}')"
if (
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  require_root() { :; }
  check_os() { :; }
  normalize_settings() { :; }
  check_requirements() { :; }
  check_compose_editor() { :; }
  acquire_lock() { :; }
  COMPOSE_FILE="$FOREIGN_COMPOSE"
  SERVICE_NAME=marzban-node
  PATCH_FILE="$TMP/owned-rest-service.py"
  compose_mount_source() { printf '%s\n' "$TMP/foreign-rest-service.py"; }
  restore_patch
) >"$TMP/foreign-restore.log" 2>&1; then
  fail "restore unexpectedly accepted a foreign target mount"
fi
FOREIGN_AFTER="$(sha256sum "$FOREIGN_COMPOSE" | awk '{print $1}')"
[ "$FOREIGN_BEFORE" = "$FOREIGN_AFTER" ] || fail "foreign restore refusal mutated Compose"
grep -Fq 'foreign mount source' "$TMP/foreign-restore.log" || fail "foreign restore refusal was not explicit"

# A rollback recreate failure must be observable, never swallowed.
if (
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  LAST_COMPOSE_BACKUP="$TMP/recovery-backup.yml"
  compose_validate() { return 0; }
  compose_up() { return 1; }
  wait_for_container() { return 0; }
  recover_service_after_compose_restore
) >"$TMP/recovery-failure.log" 2>&1; then
  fail "rollback recovery unexpectedly succeeded when service recreate failed"
fi
grep -Fq 'Rollback recovery failed: service recreate failed' "$TMP/recovery-failure.log" \
  || fail "rollback recreate failure was not reported explicitly"

# A rollback that recreates onto a different image may not keep an old owned patch mounted.
ROLLBACK_STALE_MARKER="$TMP/rollback-stale-disabled"
if (
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  PATCH_FILE="$TMP/owned-rest-service.py"
  compose_mount_source() { printf '%s\n' "$PATCH_FILE"; }
  verify_stabilizer_mount_identity() { return 0; }
  metadata_value() {
    [ "$1" = "IMAGE_ID" ] && printf '%s\n' "sha256:old"
  }
  container_image_id() { printf '%s\n' "sha256:new"; }
  disable_stale_stabilizer_mount_after_incompatibility() {
    : > "$ROLLBACK_STALE_MARKER"
    return 0
  }
  verify_recovered_patch_image_identity
) >"$TMP/rollback-image-mismatch.log" 2>&1; then
  fail "rollback image mismatch was incorrectly accepted"
fi
[ -f "$ROLLBACK_STALE_MARKER" ] || fail "rollback image mismatch did not disable the stale mount"
grep -Fq 'restored patch belongs to sha256:old' "$TMP/rollback-image-mismatch.log" \
  || fail "rollback image mismatch was not reported explicitly"

# Simulate restore primary recreate failure followed by rollback recreate failure.
RESTORE_COMPOSE="$TMP/restore-compose.yml"
printf 'services:\n  marzban-node:\n    image: previous\n    volumes:\n      - /tmp/owned.py:/code/rest_service.py:ro\n' > "$RESTORE_COMPOSE"
RESTORE_ORIGINAL="$(cat "$RESTORE_COMPOSE")"
if (
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  require_root() { :; }
  check_os() { :; }
  normalize_settings() { :; }
  check_requirements() { :; }
  check_compose_editor() { :; }
  acquire_lock() { :; }
  COMPOSE_FILE="$RESTORE_COMPOSE"
  SERVICE_NAME=marzban-node
  PATCH_FILE=/tmp/owned.py
  compose_mount_source() { printf '%s\n' "$PATCH_FILE"; }
  verify_stabilizer_mount_identity() { return 0; }
  remove_bind_mount() { printf 'services:\n  marzban-node:\n    image: restored\n' > "$COMPOSE_FILE"; }
  compose_validate() { return 0; }
  compose_up() { return 1; }
  wait_for_container() { return 0; }
  restore_patch
) >"$TMP/restore-rollback-failure.log" 2>&1; then
  fail "restore unexpectedly succeeded when primary and rollback recreates failed"
fi
[ "$(cat "$RESTORE_COMPOSE")" = "$RESTORE_ORIGINAL" ] \
  || fail "restore rollback failure did not put the previous Compose file back on disk"
grep -Fq 'rollback runtime recovery FAILED' "$TMP/restore-rollback-failure.log" \
  || fail "restore did not surface rollback runtime recovery failure"


# Runtime process detection must not depend on pgrep. The wrapper preserves
# present / absent / unknown rather than collapsing Docker/inspection errors.
(
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  CONTAINER_NAME=marzban-node
  MOCK_PROCESS_RC=0
  docker() {
    [ "${1:-}" = "exec" ] || return 125
    return "$MOCK_PROCESS_RC"
  }
  xray_process_state
  MOCK_PROCESS_RC=1
  if xray_process_state; then exit 11; else [ "$?" -eq 1 ] || exit 12; fi
  MOCK_PROCESS_RC=125
  if xray_process_state; then exit 13; else [ "$?" -eq 2 ] || exit 14; fi
) || fail "Xray process-state return semantics are incorrect"

# A detected Xray process must end the post-apply observation immediately.
(
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  STARTUP_WAIT_SECONDS=30
  container_is_running() { return 0; }
  xray_process_state() { return 0; }
  sleep() { exit 91; }
  wait_for_xray_or_timeout >/dev/null
) || fail "post-apply Xray wait did not exit immediately when Xray was detected"

# Unknown process inspection is not equivalent to Xray absence and must not
# burn the entire observation timeout retrying an inspection that cannot work.
if (
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  STARTUP_WAIT_SECONDS=30
  container_is_running() { return 0; }
  xray_process_state() { return 2; }
  sleep() { exit 92; }
  wait_for_xray_or_timeout
); then
  fail "unknown Xray inspection unexpectedly succeeded"
else
  rc=$?
fi
[ "$rc" -eq 2 ] || fail "unknown Xray inspection did not preserve rc=2 without waiting"

# Listener detection parses Linux /proc socket tables and therefore does not
# require ss inside the container.
(
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  CONTAINER_NAME=marzban-node
  docker() {
    [ "${1:-}" = "exec" ] || return 125
    cat <<'EOF_PROC_TCP'
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:F263 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1
EOF_PROC_TCP
    return 0
  }
  container_tcp_listener_state 62051
  if container_tcp_listener_state 62052; then exit 21; else [ "$?" -eq 1 ] || exit 22; fi
) || fail "dependency-free /proc listener detection is incorrect"

(
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  CONTAINER_NAME=marzban-node
  docker() { return 125; }
  if container_tcp_listener_state 62051; then exit 31; else [ "$?" -eq 2 ] || exit 32; fi
) || fail "listener inspection failure was not preserved as unknown"

# diagnose must distinguish unknown process inspection from a confirmed absence.
DIAG_UNKNOWN="$(
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  container_exists() { return 0; }
  container_is_running() { return 0; }
  xray_process_state() { return 2; }
  diagnose_runtime_health
)"
printf '%s\n' "$DIAG_UNKNOWN" | grep -Fq 'readiness-unknown: container is running but the Xray process state could not be inspected' \
  || fail "diagnose did not report unknown process inspection correctly"
if printf '%s\n' "$DIAG_UNKNOWN" | grep -Fq 'xray-unavailable'; then
  fail "diagnose converted unknown process inspection into xray-unavailable"
fi

DIAG_READY="$(
  # shellcheck source=../bin/marzban-node-stabilizer
  source "$ROOT/bin/marzban-node-stabilizer" help >/dev/null
  container_exists() { return 0; }
  container_is_running() { return 0; }
  xray_process_state() { return 0; }
  effective_container_port() { printf '%s\n' '62051'; }
  container_tcp_listener_state() { return 0; }
  diagnose_runtime_health
)"
printf '%s\n' "$DIAG_READY" | grep -Fq 'basic-ready: Xray is running and the effective API listener (62051) is present' \
  || fail "diagnose did not report dependency-free basic readiness"

echo "shell logic tests: OK"
