#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
FAKE_BIN="$TMP/bin"
RESOLVED_SHA="0123456789abcdef0123456789abcdef01234567"
CURL_LOG="$TMP/curl.log"
SOURCE_ROOT="$ROOT"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/id" <<'EOF_ID'
#!/usr/bin/env bash
if [ "${1:-}" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi
EOF_ID
cat > "$FAKE_BIN/curl" <<'EOF_CURL'
#!/usr/bin/env bash
set -euo pipefail
url=""; out=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
printf '%s\n' "$url" >> "$CURL_LOG"
case "$url" in
  https://api.github.com/repos/*/commits/*)
    printf '{"sha":"%s"}\n' "$RESOLVED_SHA" ;;
  https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/"$RESOLVED_SHA"/*)
    component="${url#https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/$RESOLVED_SHA/}"
    [ -n "$out" ]
    cp -- "$SOURCE_ROOT/$component" "$out" ;;
  *) echo "Unexpected URL $url" >&2; exit 22 ;;
esac
EOF_CURL
chmod +x "$FAKE_BIN/id" "$FAKE_BIN/curl"
export CURL_LOG RESOLVED_SHA SOURCE_ROOT
for role in node panel both; do
  : > "$CURL_LOG"
  install_root="$TMP/$role"
  PATH="$FAKE_BIN:$PATH" REPO=ach1992/marzban-node-stabilizer REF=feature/test \
    MODE="$role" AUTO_APPLY=0 INSTALL_PATH="$install_root/sbin/marzban-node-stabilizer" \
    LIB_DIR="$install_root/lib/marzban-node-stabilizer" bash "$ROOT/install.sh" > "$TMP/$role.log"
  grep -Fxq "https://api.github.com/repos/ach1992/marzban-node-stabilizer/commits/feature%2Ftest" "$CURL_LOG"
  [ "$(grep -c 'api.github.com/' "$CURL_LOG")" = 1 ]
  if grep 'raw.githubusercontent.com' "$CURL_LOG" | grep -v "/$RESOLVED_SHA/"; then
    echo "[FAIL] Mixed installer snapshot" >&2; exit 1
  fi
  [ -x "$install_root/sbin/marzban-stabilizer" ]
  [ -f "$install_root/lib/marzban-node-stabilizer/compose_mount.py" ]
  case "$role" in
    node) expected=4 ;;
    panel) expected=7 ;;
    both) expected=9 ;;
  esac
  [ "$(grep -c 'raw.githubusercontent.com' "$CURL_LOG")" = "$expected" ]
  for selected in node panel; do
    binary="$install_root/sbin/marzban-$selected-stabilizer"
    if [ "$role" = "$selected" ] || [ "$role" = both ]; then
      [ -x "$binary" ]
      INSTALLED_LIB_DIR="$install_root/lib/marzban-node-stabilizer" "$install_root/sbin/marzban-stabilizer" "$selected" --help > "$TMP/help.log"
    else
      [ ! -e "$binary" ]
    fi
  done
  grep -Fq "No service changes were applied." "$TMP/$role.log"
done

if PATH="$FAKE_BIN:$PATH" MODE=invalid AUTO_APPLY=0 bash "$ROOT/install.sh" > "$TMP/invalid.log" 2>&1; then
  echo "[FAIL] Invalid role accepted" >&2; exit 1
fi
if PATH="$FAKE_BIN:$PATH" MODE=both AUTO_APPLY=1 bash "$ROOT/install.sh" > "$TMP/invalid.log" 2>&1; then
  echo "[FAIL] Both roles auto-applied" >&2; exit 1
fi
echo "installer snapshot and independent role selection: OK"
