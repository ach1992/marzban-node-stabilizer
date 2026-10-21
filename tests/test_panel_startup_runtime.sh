#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
IMAGE="${MARZBAN_PANEL_IMAGE:-gozargah/marzban@sha256:4c7a449c9cc22f7b08ecc76f21638001987ce7f4a2e3687b84d15e59eed98db5}"
python3 "$ROOT/lib/patch_panel.py" --source "$ROOT/tests/fixtures/panel/baseline" --output "$TMP/candidate" >/dev/null
mounts=(-e PYTHONPATH=/code -v "$ROOT:/work:ro")
files=(app/xray/node.py app/xray/operations.py app/jobs/0_xray_core.py app/routers/core.py main.py)
for file in "${files[@]}"; do
  mounts+=(-v "$TMP/candidate/$file:/code/$file:ro")
done
run_case() {
  docker run --rm --network none --memory 1500m --label mns.panel-startup=disposable \
    --entrypoint python "${mounts[@]}" "$IMAGE" "/work/tests/$1" "${@:2}"
}
run_case runtime_panel_startup_failure.py
run_case runtime_panel_port_startup.py --reserve --tls
run_case runtime_panel_port_startup.py --reserve --tls --no-collision
run_case runtime_panel_smoke.py candidate
echo "Panel generated-source startup, TLS, Core and failure validation: OK"
