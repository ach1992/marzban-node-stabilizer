#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT

FAKE_BIN="$TMP/bin"
INSTALL_ROOT="$TMP/install"
CURL_LOG="$TMP/curl.log"
RESOLVED_SHA="0123456789abcdef0123456789abcdef01234567"
mkdir -p "$FAKE_BIN"

cat > "$FAKE_BIN/id" <<'EOF_ID'
#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "-u" ]; then
  printf '0\n'
  exit 0
fi
exec /usr/bin/id "$@"
EOF_ID
chmod +x "$FAKE_BIN/id"

cat > "$FAKE_BIN/curl" <<'EOF_CURL'
#!/usr/bin/env bash
set -euo pipefail

url=""
out=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o)
      out="$2"
      shift 2
      ;;
    -*)
      shift
      ;;
    *)
      url="$1"
      shift
      ;;
  esac
done

printf '%s\n' "$url" >> "$CURL_LOG"

case "$url" in
  https://api.github.com/repos/*/commits/*)
    printf '{"sha":"%s"}\n' "$RESOLVED_SHA"
    ;;
  */bin/marzban-node-stabilizer)
    [ -n "$out" ]
    cat > "$out" <<'EOF_BIN'
#!/usr/bin/env bash
set -euo pipefail
echo test-cli
EOF_BIN
    ;;
  */lib/patch_rest_service.py|*/lib/compose_mount.py)
    [ -n "$out" ]
    printf 'pass\n' > "$out"
    ;;
  *)
    echo "unexpected curl URL: $url" >&2
    exit 22
    ;;
esac
EOF_CURL
chmod +x "$FAKE_BIN/curl"

export CURL_LOG RESOLVED_SHA

PATH="$FAKE_BIN:$PATH" \
REPO="ach1992/marzban-node-stabilizer" \
REF="feature/test" \
AUTO_APPLY=0 \
INSTALL_PATH="$INSTALL_ROOT/sbin/marzban-node-stabilizer" \
LIB_DIR="$INSTALL_ROOT/lib/marzban-node-stabilizer" \
bash "$ROOT/install.sh" >"$TMP/install.log"

grep -Fxq "https://api.github.com/repos/ach1992/marzban-node-stabilizer/commits/feature%2Ftest" "$CURL_LOG" \
  || { cat "$CURL_LOG" >&2; echo "[FAIL] installer did not resolve the movable ref exactly once" >&2; exit 1; }

for component in \
  bin/marzban-node-stabilizer \
  lib/patch_rest_service.py \
  lib/compose_mount.py
do
  grep -Fxq "https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/$RESOLVED_SHA/$component" "$CURL_LOG" \
    || { cat "$CURL_LOG" >&2; echo "[FAIL] component was not downloaded from resolved immutable SHA: $component" >&2; exit 1; }
done

[ "$(grep -Fc 'https://raw.githubusercontent.com/' "$CURL_LOG")" = "3" ] \
  || { cat "$CURL_LOG" >&2; echo "[FAIL] unexpected raw component request count" >&2; exit 1; }

if grep -Fq 'raw.githubusercontent.com/ach1992/marzban-node-stabilizer/feature/test/' "$CURL_LOG"; then
  cat "$CURL_LOG" >&2
  echo "[FAIL] installer downloaded a component from the movable ref" >&2
  exit 1
fi

[ -x "$INSTALL_ROOT/sbin/marzban-node-stabilizer" ] || { echo "[FAIL] CLI was not installed" >&2; exit 1; }
[ -f "$INSTALL_ROOT/lib/marzban-node-stabilizer/patch_rest_service.py" ] || { echo "[FAIL] patch helper was not installed" >&2; exit 1; }
[ -f "$INSTALL_ROOT/lib/marzban-node-stabilizer/compose_mount.py" ] || { echo "[FAIL] compose helper was not installed" >&2; exit 1; }

grep -Fq "Resolved ach1992/marzban-node-stabilizer@feature/test -> $RESOLVED_SHA" "$TMP/install.log" \
  || { cat "$TMP/install.log" >&2; echo "[FAIL] immutable source identity was not surfaced" >&2; exit 1; }

echo "installer snapshot test: OK"
