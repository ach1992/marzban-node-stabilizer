#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-ach1992/marzban-node-stabilizer}"
REF="${REF:-${BRANCH:-main}}"
INSTALL_PATH="${INSTALL_PATH:-/usr/local/sbin/marzban-node-stabilizer}"
LIB_DIR="${LIB_DIR:-/usr/local/lib/marzban-node-stabilizer}"
MODE="${MODE:-node}"
case "$MODE" in
  node) AUTO_APPLY="${AUTO_APPLY:-1}" ;;
  panel|both) AUTO_APPLY="${AUTO_APPLY:-0}" ;;
  *) echo "[ERROR] MODE must be node, panel or both." >&2; exit 2 ;;
esac
if [ "$MODE" = "both" ] && [ "$AUTO_APPLY" != "0" ]; then
  echo "[ERROR] Install both with AUTO_APPLY=0, then apply each role explicitly." >&2
  exit 2
fi
PANEL_INSTALL_PATH="${PANEL_INSTALL_PATH:-$(dirname -- "$INSTALL_PATH")/marzban-panel-stabilizer}"
STABILIZER_INSTALL_PATH="${STABILIZER_INSTALL_PATH:-$(dirname -- "$INSTALL_PATH")/marzban-stabilizer}"

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
components=(bin/marzban-stabilizer lib/compose_mount.py)
if [ "$MODE" = "node" ] || [ "$MODE" = "both" ]; then
  components+=(bin/marzban-node-stabilizer lib/patch_rest_service.py)
fi
if [ "$MODE" = "panel" ] || [ "$MODE" = "both" ]; then
  components+=(bin/marzban-panel-stabilizer lib/panel_stabilizer.py lib/patch_panel.py lib/panel/patch-spec.json lib/panel/LICENSE.Marzban)
fi
for component in "${components[@]}"; do
  mkdir -p -- "$TMP_DIR/$(dirname -- "$component")"
  curl -fsSL "${RAW_BASE}/${component}" -o "$TMP_DIR/$component"
done

info "Validating downloaded components..."
for component in "${components[@]}"; do
  case "$component" in
    bin/*) bash -n "$TMP_DIR/$component" ;;
    *.py) python3 -m py_compile "$TMP_DIR/$component" ;;
    *.json) python3 -m json.tool "$TMP_DIR/$component" >/dev/null ;;
  esac
done

install -d -m 0755 -- "$LIB_DIR"
for component in "${components[@]}"; do
  mode=0644
  case "$component" in
    bin/marzban-node-stabilizer) destination="$INSTALL_PATH"; mode=0755 ;;
    bin/marzban-panel-stabilizer) destination="$PANEL_INSTALL_PATH"; mode=0755 ;;
    bin/marzban-stabilizer) destination="$STABILIZER_INSTALL_PATH"; mode=0755 ;;
    lib/*) destination="$LIB_DIR/${component#lib/}" ;;
  esac
  install -d -m 0755 -- "$(dirname -- "$destination")"
  install -m "$mode" -- "$TMP_DIR/$component" "$destination.tmp.$$"
  mv -f -- "$destination.tmp.$$" "$destination"
done

info "Installed $MODE role(s) from $RESOLVED_REF."
if [ "$AUTO_APPLY" = "1" ]; then
  info "Applying the selected role; its Compose service will be recreated."
  if [ "$MODE" = "node" ]; then
    "$INSTALL_PATH" apply
  else
    "$PANEL_INSTALL_PATH" apply
  fi
else
  info "No service changes were applied."
  if [ "$MODE" = "both" ]; then
    echo "Inspect each role: marzban-stabilizer node status / marzban-stabilizer panel status"
    echo "Apply node and panel separately; each has independent settings and recovery."
  else
    echo "Run the selected role explicitly: marzban-stabilizer $MODE status"
  fi
fi
