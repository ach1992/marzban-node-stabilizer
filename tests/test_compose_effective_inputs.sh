#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT

fail() {
  echo "[FAIL] $*" >&2
  exit 1
}

assert_valid_compose_and_rejected() {
  local name="$1"
  local compose_file="$2"
  local before after action rc

  docker compose -f "$compose_file" config -q     || fail "$name fixture is not valid Docker Compose input"

  before="$(sha256sum "$compose_file" | awk '{print $1}')"

  for action in source has add remove; do
    set +e
    if [ "$action" = "add" ]; then
      python3 "$ROOT/lib/compose_mount.py" "$action"         --file "$compose_file"         --service marzban-node         --patch-file /opt/mns/rest_service.py         >"$TMP/$name-$action.out" 2>"$TMP/$name-$action.err"
      rc=$?
    else
      python3 "$ROOT/lib/compose_mount.py" "$action"         --file "$compose_file"         --service marzban-node         >"$TMP/$name-$action.out" 2>"$TMP/$name-$action.err"
      rc=$?
    fi
    set -e

    [ "$rc" -eq 2 ] || {
      cat "$TMP/$name-$action.out" >&2 || true
      cat "$TMP/$name-$action.err" >&2 || true
      fail "$name/$action returned $rc instead of fail-closed rc=2"
    }

    after="$(sha256sum "$compose_file" | awk '{print $1}')"
    [ "$before" = "$after" ]       || fail "$name/$action mutated Compose despite unsupported effective-volume composition"
  done
}

MERGE_FILE="$TMP/merge.yml"
cat > "$MERGE_FILE" <<'EOF_MERGE'
x-node-base: &node_base
  image: alpine:3.20
  volumes:
    - /tmp/foreign.py:/code/rest_service.py:ro
services:
  marzban-node:
    <<: *node_base
    command: ["sleep", "infinity"]
EOF_MERGE
assert_valid_compose_and_rejected yaml-merge "$MERGE_FILE"

EXTENDS_FILE="$TMP/extends.yml"
cat > "$EXTENDS_FILE" <<'EOF_EXTENDS'
services:
  node-base:
    image: alpine:3.20
    volumes:
      - /tmp/foreign.py:/code/rest_service.py:ro
  marzban-node:
    extends:
      service: node-base
    command: ["sleep", "infinity"]
EOF_EXTENDS
assert_valid_compose_and_rejected same-file-extends "$EXTENDS_FILE"

BASE_FILE="$TMP/base-compose.yml"
cat > "$BASE_FILE" <<'EOF_BASE'
services:
  node-base:
    image: alpine:3.20
    volumes:
      - /tmp/foreign.py:/code/rest_service.py:ro
EOF_BASE

EXTERNAL_EXTENDS_FILE="$TMP/external-extends.yml"
cat > "$EXTERNAL_EXTENDS_FILE" <<'EOF_EXTERNAL'
services:
  marzban-node:
    extends:
      file: ./base-compose.yml
      service: node-base
    command: ["sleep", "infinity"]
EOF_EXTERNAL
assert_valid_compose_and_rejected external-file-extends "$EXTERNAL_EXTENDS_FILE"

QUOTED_FILE="$TMP/quoted-volumes.yml"
cat > "$QUOTED_FILE" <<'EOF_QUOTED'
services:
  marzban-node:
    image: alpine:3.20
    "volumes":
      - /tmp/foreign.py:/code/rest_service.py:ro
    command: ["sleep", "infinity"]
EOF_QUOTED
assert_valid_compose_and_rejected quoted-volumes-key "$QUOTED_FILE"

echo "compose effective-input fail-closed tests: OK"
