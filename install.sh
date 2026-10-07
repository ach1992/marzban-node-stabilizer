#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-ach1992/marzban-node-stabilizer}"
REF="${REF:-${BRANCH:-main}}"
INSTALL_PATH="${INSTALL_PATH:-/usr/local/sbin/marzban-node-stabilizer}"
LIB_DIR="${LIB_DIR:-/usr/local/lib/marzban-node-stabilizer}"
AUTO_APPLY="${AUTO_APPLY:-1}"

TMP_DIR=""
RESOLVED_REF=""
RAW_BASE=""

info() {
  echo "[INFO] $*"
}

error() {
  echo "[ERROR] $*" >&2
  exit 1
}

cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf -- "$TMP_DIR"
  fi
}
trap cleanup EXIT

if [ "$(id -u)" -ne 0 ]; then
  error "Run installer as root. Example: curl -fsSL ... | sudo bash"
fi

if [ ! -f /etc/os-release ]; then
  error "Cannot detect OS. This installer supports Debian/Ubuntu only."
fi

. /etc/os-release
OS_MATCH=" ${ID:-} ${ID_LIKE:-} "
case "$OS_MATCH" in
  *" debian "*|*" ubuntu "*) ;;
  *) error "Unsupported OS: ${PRETTY_NAME:-unknown}. This script supports Debian/Ubuntu only." ;;
esac

need_apt=0
command -v curl >/dev/null 2>&1 || need_apt=1
command -v python3 >/dev/null 2>&1 || need_apt=1
command -v flock >/dev/null 2>&1 || need_apt=1

if [ "$need_apt" = "1" ]; then
  info "Installing required base packages..."
  apt-get update
  apt-get install -y curl ca-certificates python3 util-linux
fi

if [[ ! "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  error "REPO must use owner/name form with GitHub-safe characters."
fi
[ -n "$REF" ] || error "REF must not be empty."

if [[ "$REF" =~ ^[0-9a-fA-F]{40}$ ]]; then
  RESOLVED_REF="${REF,,}"
else
  ENCODED_REF="$(python3 - "$REF" <<'PY'
import sys
from urllib.parse import quote
print(quote(sys.argv[1], safe=""))
PY
)"
  RESOLVED_REF="$(
    curl -fsSL "https://api.github.com/repos/${REPO}/commits/${ENCODED_REF}" \
      | python3 -c 'import json,re,sys; data=json.load(sys.stdin); sha=data.get("sha", ""); sys.exit(0) if re.fullmatch(r"[0-9a-fA-F]{40}", sha) and not print(sha.lower()) else sys.exit(1)'
  )" || error "Could not resolve ${REPO}@${REF} to an immutable commit."
fi

[[ "$RESOLVED_REF" =~ ^[0-9a-f]{40}$ ]] \
  || error "Resolved GitHub commit identity is invalid: ${RESOLVED_REF:-empty}"

RAW_BASE="https://raw.githubusercontent.com/${REPO}/${RESOLVED_REF}"
TMP_DIR="$(mktemp -d)"

info "Resolved ${REPO}@${REF} -> ${RESOLVED_REF}"
info "Downloading stabilizer components from immutable commit ${RESOLVED_REF}"
curl -fsSL "${RAW_BASE}/bin/marzban-node-stabilizer" -o "$TMP_DIR/marzban-node-stabilizer"
curl -fsSL "${RAW_BASE}/lib/patch_rest_service.py" -o "$TMP_DIR/patch_rest_service.py"
curl -fsSL "${RAW_BASE}/lib/compose_mount.py" -o "$TMP_DIR/compose_mount.py"

info "Validating downloaded components..."
bash -n "$TMP_DIR/marzban-node-stabilizer"
python3 -m py_compile "$TMP_DIR/patch_rest_service.py" "$TMP_DIR/compose_mount.py"

install -d -m 0755 -- "$(dirname -- "$INSTALL_PATH")" "$LIB_DIR"
install -m 0755 -- "$TMP_DIR/marzban-node-stabilizer" "$INSTALL_PATH.tmp.$$"
mv -f -- "$INSTALL_PATH.tmp.$$" "$INSTALL_PATH"
install -m 0644 -- "$TMP_DIR/patch_rest_service.py" "$LIB_DIR/patch_rest_service.py.tmp.$$"
mv -f -- "$LIB_DIR/patch_rest_service.py.tmp.$$" "$LIB_DIR/patch_rest_service.py"
install -m 0644 -- "$TMP_DIR/compose_mount.py" "$LIB_DIR/compose_mount.py.tmp.$$"
mv -f -- "$LIB_DIR/compose_mount.py.tmp.$$" "$LIB_DIR/compose_mount.py"

info "Installed successfully."

if [ "$AUTO_APPLY" = "1" ]; then
  echo
  info "Running patch automatically..."
  echo
  "$INSTALL_PATH" apply
  echo
  info "All done."
else
  echo
  info "Auto apply disabled."
  echo "Run manually with:"
  echo "  sudo marzban-node-stabilizer apply"
fi
