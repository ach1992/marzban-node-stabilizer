#!/usr/bin/env python3
"""Fail-closed source transformer for Marzban Node rest_service.py.

The stabilizer intentionally patches the image-provided source instead of keeping a
long-lived fork. This transformer is strict: when the expected upstream method
shape changes, it exits without producing a partially patched file.
"""

from __future__ import annotations

import argparse
import hashlib
import py_compile
import re
from pathlib import Path

MARKER = "# marzban-node-stabilizer: lifecycle-hardening-v2"

EXPECTED_METHOD_SHA256 = {
    "connect": "e75009ea71bf5d4871c0070f47e2de7d9832e6b82b55b06ae8cf34e6ef0719fe",
    "disconnect": "361a7d56cf23ed4b930ea07b4e93d86d4a99ebcdebe6abb89a28b3bcc221d421",
    "start": "caac79a143b1e13ccf2b757c036ecbc1609f424970c400a33f0c44104a21aa84",
    "stop": "fd2a15da953cf7def829b15515019fc3634a538ae4d0a50dc26135c9f8a5faa7",
    "restart": "5b4e8a613fcb8d69c6a6ea2479370e7c03a808b18d73ecf9eee68e330893cdf9",
}


def _replace_method(source: str, method_name: str, replacement: str) -> str:
    pattern = re.compile(
        rf"(?ms)^    def {re.escape(method_name)}\([^\n]*\):\n.*?(?=^    (?:async )?def [A-Za-z_][A-Za-z0-9_]*\(|^service = Service\(\))"
    )
    match = pattern.search(source)
    if not match:
        raise ValueError(f"expected Service.{method_name} method was not found")

    current_block = match.group(0)
    expected_hash = EXPECTED_METHOD_SHA256[method_name]
    current_hash = hashlib.sha256(current_block.encode("utf-8")).hexdigest()
    if current_hash != expected_hash:
        raise ValueError(
            f"upstream Service.{method_name} changed (sha256={current_hash}); "
            "refusing to replace unreviewed source"
        )

    return source[: match.start()] + replacement.rstrip() + "\n\n" + source[match.end() :]


def _update_existing_patch(source: str, timeout: int, grace: int) -> str:
    source, timeout_count = re.subn(
        r"^MNS_START_CONFIRM_SECONDS[ \t]*=[ \t]*\d+[ \t]*$",
        f"MNS_START_CONFIRM_SECONDS = {timeout}",
        source,
        count=1,
        flags=re.MULTILINE,
    )
    source, grace_count = re.subn(
        r"^MNS_RESTART_GRACE_SECONDS[ \t]*=[ \t]*\d+[ \t]*$",
        f"MNS_RESTART_GRACE_SECONDS = {grace}",
        source,
        count=1,
        flags=re.MULTILINE,
    )
    if timeout_count != 1 or grace_count != 1:
        raise ValueError("existing stabilizer patch marker is present but constants are missing")
    return source


def patch_source(source: str, timeout: int, grace: int) -> str:
    if not 1 <= timeout <= 30:
        raise ValueError("timeout must be between 1 and 30 seconds")
    if not 0 <= grace <= 3600:
        raise ValueError("restart grace must be between 0 and 3600 seconds")

    if MARKER in source:
        return _update_existing_patch(source, timeout, grace)

    required_fragments = (
        "import asyncio\n",
        "import json\n",
        "import time\n",
        "class Service(object):\n",
        "        self.config = None\n",
        '        self.router.add_api_route("/connect", self.connect, methods=["POST"])\n',
        '        self.router.add_api_route("/disconnect", self.disconnect, methods=["POST"])\n',
    )
    for fragment in required_fragments:
        if fragment not in source:
            raise ValueError(f"upstream source is incompatible; missing expected fragment: {fragment!r}")

    source = source.replace(
        "import asyncio\n",
        f"{MARKER}\nimport asyncio\nimport hashlib\nimport threading\n",
        1,
    )

    helper_block = f'''\nMNS_START_CONFIRM_SECONDS = {timeout}\nMNS_RESTART_GRACE_SECONDS = {grace}\n\n\ndef _mns_config_fingerprint(config_value) -> str:\n    if isinstance(config_value, str):\n        config_value = json.loads(config_value)\n    canonical = json.dumps(config_value, sort_keys=True, separators=(\",\", \":\"))\n    return hashlib.sha256(canonical.encode(\"utf-8\")).hexdigest()\n\n\ndef _mns_wait_for_core_start(logs, core_version):\n    deadline = time.monotonic() + MNS_START_CONFIRM_SECONDS\n    last_log = \"\"\n    while time.monotonic() < deadline:\n        while logs:\n            log = logs.popleft()\n            if log:\n                last_log = log\n            if f\"Xray {{core_version}} started\" in log:\n                return last_log\n        time.sleep(0.1)\n    return last_log\n'''

    app_anchor = "app = FastAPI()\n"
    if source.count(app_anchor) != 1:
        raise ValueError("upstream source is incompatible; FastAPI app anchor changed")
    source = source.replace(app_anchor, app_anchor + helper_block, 1)

    state_anchor = "        self.config = None\n"
    state_replacement = (
        "        self.config = None\n"
        "        self.last_start_ts = 0.0\n"
        "        self.last_config_hash = None\n"
        "        self.lifecycle_lock = threading.RLock()\n"
    )
    source = source.replace(state_anchor, state_replacement, 1)

    connect = '''    def connect(self, request: Request):
        with self.lifecycle_lock:
            client_ip = request.client.host
            if self.connected:
                logger.warning(
                    f'New connection from {client_ip}, Core control access was taken away from previous client.')

            self.session_id = uuid4()
            self.client_ip = client_ip
            self.connected = True
            logger.info(f'{self.client_ip} connected, Session ID = "{self.session_id}".')

            return self.response(
                session_id=self.session_id
            )'''

    disconnect = '''    def disconnect(self, session_id: UUID = Body(embed=True)):
        with self.lifecycle_lock:
            self.match_session_id(session_id)

            if self.connected:
                logger.info(f'{self.client_ip} disconnected, Session ID = "{self.session_id}".')

            self.session_id = None
            self.client_ip = None
            self.connected = False

            if self.core.started:
                try:
                    self.core.stop()
                except RuntimeError:
                    pass

            self.last_start_ts = 0.0
            self.last_config_hash = None
            return self.response()'''

    start = '''    def start(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True)):
        with self.lifecycle_lock:
            self.match_session_id(session_id)

            try:
                xray_config = XRayConfig(config, self.client_ip)
                config_hash = _mns_config_fingerprint(xray_config)
            except json.decoder.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "config": f'Failed to decode config: {exc}'
                    }
                )

            if self.core.started and self.last_config_hash == config_hash:
                logger.info("Start request matches the active Xray config; keeping the current core.")
                return self.response()

            with self.core.get_logs() as logs:
                try:
                    self.core.start(xray_config)
                    self.last_start_ts = time.monotonic()
                    last_log = _mns_wait_for_core_start(logs, self.core_version)
                except Exception as exc:
                    logger.error(f"Failed to start core: {exc}")
                    raise HTTPException(
                        status_code=503,
                        detail=str(exc)
                    )

            if not self.core.started:
                self.last_start_ts = 0.0
                self.last_config_hash = None
                raise HTTPException(
                    status_code=503,
                    detail=last_log
                )

            self.last_config_hash = config_hash
            return self.response()'''

    stop = '''    def stop(self, session_id: UUID = Body(embed=True)):
        with self.lifecycle_lock:
            self.match_session_id(session_id)

            try:
                self.core.stop()
            except RuntimeError:
                pass

            self.last_start_ts = 0.0
            self.last_config_hash = None
            return self.response()'''

    restart = '''    def restart(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True)):
        with self.lifecycle_lock:
            self.match_session_id(session_id)

            try:
                xray_config = XRayConfig(config, self.client_ip)
                config_hash = _mns_config_fingerprint(xray_config)
            except json.decoder.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "config": f'Failed to decode config: {exc}'
                    }
                )

            if (
                self.core.started
                and self.last_config_hash == config_hash
                and time.monotonic() - self.last_start_ts < MNS_RESTART_GRACE_SECONDS
            ):
                logger.warning("Ignoring duplicate restart during Xray startup grace period.")
                return self.response()

            try:
                with self.core.get_logs() as logs:
                    self.core.restart(xray_config)
                    self.last_start_ts = time.monotonic()
                    last_log = _mns_wait_for_core_start(logs, self.core_version)
            except Exception as exc:
                logger.error(f"Failed to restart core: {exc}")
                raise HTTPException(
                    status_code=503,
                    detail=str(exc)
                )

            if not self.core.started:
                self.last_start_ts = 0.0
                self.last_config_hash = None
                raise HTTPException(
                    status_code=503,
                    detail=last_log
                )

            self.last_config_hash = config_hash
            return self.response()'''

    for name, block in (
        ("connect", connect),
        ("disconnect", disconnect),
        ("start", start),
        ("stop", stop),
        ("restart", restart),
    ):
        source = _replace_method(source, name, block)

    return source


def patch_file(path: Path, timeout: int, grace: int) -> None:
    original = path.read_text()
    patched = patch_source(original, timeout=timeout, grace=grace)
    path.write_text(patched)
    py_compile.compile(str(path), doraise=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--timeout", required=True, type=int)
    parser.add_argument("--grace", required=True, type=int)
    args = parser.parse_args()

    try:
        patch_file(args.file, timeout=args.timeout, grace=args.grace)
    except Exception as exc:
        raise SystemExit(f"[ERROR] Could not safely patch {args.file}: {exc}") from exc

    print(f"[OK] patched: {args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
