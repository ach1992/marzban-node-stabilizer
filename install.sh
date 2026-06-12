#!/usr/bin/env bash
set -euo pipefail

REPO="${REPO:-ach1992/marzban-node-stabilizer}"
BRANCH="${BRANCH:-main}"
INSTALL_PATH="${INSTALL_PATH:-/usr/local/sbin/marzban-node-stabilizer}"
RAW_BASE="https://raw.githubusercontent.com/${REPO}/${BRANCH}"

AUTO_APPLY="${AUTO_APPLY:-1}"

info() {
  echo "[INFO] $*"
}

warn() {
  echo "[WARN] $*" >&2
}

error() {
  echo "[ERROR] $*" >&2
  exit 1
}

if [ "$(id -u)" -ne 0 ]; then
  error "Run installer as root. Example: curl -fsSL ... | sudo bash"
fi

if [ ! -f /etc/os-release ]; then
  error "Cannot detect OS. This installer supports Debian/Ubuntu only."
fi

. /etc/os-release

OS_MATCH=" ${ID:-} ${ID_LIKE:-} "

case "$OS_MATCH" in
  *" debian "*|*" ubuntu "*)
    ;;
  *)
    error "Unsupported OS: ${PRETTY_NAME:-unknown}. This script supports Debian/Ubuntu only."
    ;;
esac

if ! command -v curl >/dev/null 2>&1; then
  info "curl not found. Installing curl..."
  apt-get update
  apt-get install -y curl ca-certificates
fi

if ! command -v python3 >/dev/null 2>&1; then
  info "python3 not found. Installing python3..."
  apt-get update
  apt-get install -y python3
fi

info "Installing marzban-node-stabilizer to ${INSTALL_PATH}"

curl -fsSL "${RAW_BASE}/bin/marzban-node-stabilizer" -o "${INSTALL_PATH}"
chmod +x "${INSTALL_PATH}"

info "Installed successfully."

if [ "$AUTO_APPLY" = "1" ]; then
  echo
  info "Running patch automatically..."
  echo

  "${INSTALL_PATH}" apply

  echo
  info "All done."
else
  echo
  info "Auto apply disabled."
  echo
  echo "Run manually with:"
  echo "  sudo marzban-node-stabilizer apply"
fi
