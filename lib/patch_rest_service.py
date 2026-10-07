#!/usr/bin/env python3
"""Fail-closed source transformer for Marzban Node rest_service.py.

The stabilizer patches the image-provided source instead of maintaining a fork.
Every replacement is bound to the reviewed Service class shape; ambiguous or
changed upstream structure is rejected before a patched file is accepted.
"""

from __future__ import annotations

import argparse
import ast
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


def _service_class(source: str) -> ast.ClassDef:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"upstream source is not valid Python: {exc}") from exc

    services = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Service"
    ]
    if len(services) != 1:
        raise ValueError(
            f"expected exactly one top-level Service class, found {len(services)}"
        )
    return services[0]


def _service_method_span(source: str, method_name: str) -> tuple[int, int, str]:
    service = _service_class(source)
    methods = [
        node
        for node in service.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    ]
    if len(methods) != 1:
        raise ValueError(
            f"expected exactly one Service.{method_name} method, found {len(methods)}"
        )

    lines = source.splitlines(keepends=True)
    service_start = sum(len(line) for line in lines[: service.lineno - 1])
    service_text = "".join(lines[service.lineno - 1 : service.end_lineno])
    pattern = re.compile(
        rf"(?ms)^    def {re.escape(method_name)}\([^\n]*\):\n.*?"
        rf"(?=^    (?:async )?def [A-Za-z_][A-Za-z0-9_]*\(|\Z)"
    )
    matches = list(pattern.finditer(service_text))
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one source span for Service.{method_name}, found {len(matches)}"
        )

    match = matches[0]
    return (
        service_start + match.start(),
        service_start + match.end(),
        match.group(0),
    )


def _replace_method(source: str, method_name: str, replacement: str) -> str:
    start, end, current_block = _service_method_span(source, method_name)
    expected_hash = EXPECTED_METHOD_SHA256[method_name]
    current_hash = hashlib.sha256(current_block.encode("utf-8")).hexdigest()
    if current_hash != expected_hash:
        raise ValueError(
            f"upstream Service.{method_name} changed (sha256={current_hash}); "
            "refusing to replace unreviewed source"
        )

    return source[:start] + replacement.rstrip() + "\n\n" + source[end:]


def _inject_service_state(source: str) -> str:
    service = _service_class(source)
    initializers = [
        node
        for node in service.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    ]
    if len(initializers) != 1:
        raise ValueError(
            f"expected exactly one Service.__init__, found {len(initializers)}"
        )
    initializer = initializers[0]

    config_assignments: list[ast.Assign] = []
    for node in ast.walk(initializer):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (
            isinstance(target, ast.Attribute)
            and target.attr == "config"
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and isinstance(node.value, ast.Constant)
            and node.value.value is None
        ):
            continue
        config_assignments.append(node)

    if len(config_assignments) != 1:
        raise ValueError(
            "expected exactly one Service.__init__ assignment 'self.config = None', "
            f"found {len(config_assignments)}"
        )

    assignment = config_assignments[0]
    lines = source.splitlines(keepends=True)
    index = assignment.lineno - 1
    if lines[index] != "        self.config = None\n":
        raise ValueError(
            "reviewed Service.__init__ state anchor changed; refusing ambiguous injection"
        )

    state_lines = (
        "        self.last_start_ts = 0.0\n"
        "        self.last_config_hash = None\n"
        "        self.lifecycle_generation = 0\n"
        "        self.lifecycle_lock = threading.RLock()\n"
    )
    lines.insert(index + 1, state_lines)
    return "".join(lines)


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
    compile(source, "<patched-rest-service>", "exec")
    service = _service_class(source)
    initializer = next(
        (
            node
            for node in service.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        ),
        None,
    )
    if initializer is None:
        raise ValueError("existing stabilizer patch has no Service.__init__")
    attrs = {
        node.targets[0].attr
        for node in ast.walk(initializer)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "self"
    }
    required = {"last_start_ts", "last_config_hash", "lifecycle_generation", "lifecycle_lock"}
    if not required.issubset(attrs):
        raise ValueError("existing stabilizer patch marker is present but lifecycle state is incomplete")
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
        '        self.router.add_api_route("/connect", self.connect, methods=["POST"])\n',
        '        self.router.add_api_route("/disconnect", self.disconnect, methods=["POST"])\n',
    )
    for fragment in required_fragments:
        if fragment not in source:
            raise ValueError(f"upstream source is incompatible; missing expected fragment: {fragment!r}")

    for name in EXPECTED_METHOD_SHA256:
        _, _, block = _service_method_span(source, name)
        current_hash = hashlib.sha256(block.encode("utf-8")).hexdigest()
        if current_hash != EXPECTED_METHOD_SHA256[name]:
            raise ValueError(
                f"upstream Service.{name} changed (sha256={current_hash}); "
                "refusing to replace unreviewed source"
            )

    source = source.replace(
        "import asyncio\n",
        f"{MARKER}\nimport asyncio\nimport hashlib\nimport threading\n",
        1,
    )

    helper_block = f'''
MNS_START_CONFIRM_SECONDS = {timeout}
MNS_RESTART_GRACE_SECONDS = {grace}


def _mns_config_fingerprint(config_value) -> str:
    if isinstance(config_value, str):
        config_value = json.loads(config_value)
    canonical = json.dumps(config_value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _mns_operation_is_current(service, generation, session_id):
    with service.lifecycle_lock:
        return (
            service.lifecycle_generation == generation
            and service.session_id == session_id
        )


def _mns_wait_for_core_start(logs, core_version, service, generation, session_id):
    deadline = time.monotonic() + MNS_START_CONFIRM_SECONDS
    last_log = ""
    while time.monotonic() < deadline:
        if not _mns_operation_is_current(service, generation, session_id):
            return last_log, False
        while logs:
            log = logs.popleft()
            if log:
                last_log = log
            if f"Xray {{core_version}} started" in log:
                return last_log, True
        time.sleep(0.1)
    return last_log, _mns_operation_is_current(service, generation, session_id)
'''

    app_anchor = "app = FastAPI()\n"
    if source.count(app_anchor) != 1:
        raise ValueError("upstream source is incompatible; FastAPI app anchor changed")
    source = source.replace(app_anchor, app_anchor + helper_block, 1)
    source = _inject_service_state(source)

    connect = '''    def connect(self, request: Request):
        with self.lifecycle_lock:
            client_ip = request.client.host
            self.lifecycle_generation += 1

            if self.connected:
                logger.warning(
                    f'New connection from {client_ip}, Core control access was taken away from previous client.')
                if self.core.started:
                    try:
                        self.core.stop()
                    except RuntimeError:
                        pass

            self.last_start_ts = 0.0
            self.last_config_hash = None
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
            self.lifecycle_generation += 1

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
        try:
            with self.core.get_logs() as logs:
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

                    self.lifecycle_generation += 1
                    operation_generation = self.lifecycle_generation
                    self.core.start(xray_config)
                    self.last_start_ts = time.monotonic()
                    self.last_config_hash = config_hash

                last_log, still_current = _mns_wait_for_core_start(
                    logs,
                    self.core_version,
                    self,
                    operation_generation,
                    session_id,
                )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"Failed to start core: {exc}")
            with self.lifecycle_lock:
                if 'operation_generation' in locals() and (
                    self.lifecycle_generation == operation_generation
                    and self.session_id == session_id
                ):
                    self.last_start_ts = 0.0
                    self.last_config_hash = None
            raise HTTPException(
                status_code=503,
                detail=str(exc)
            )

        with self.lifecycle_lock:
            if not still_current or (
                self.lifecycle_generation != operation_generation
                or self.session_id != session_id
            ):
                logger.warning("Start completion was superseded by a newer lifecycle transition.")
                return self.response()

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
            self.lifecycle_generation += 1

            try:
                self.core.stop()
            except RuntimeError:
                pass

            self.last_start_ts = 0.0
            self.last_config_hash = None
            return self.response()'''

    restart = '''    def restart(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True)):
        try:
            with self.core.get_logs() as logs:
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

                    self.lifecycle_generation += 1
                    operation_generation = self.lifecycle_generation
                    self.core.restart(xray_config)
                    self.last_start_ts = time.monotonic()
                    self.last_config_hash = config_hash

                last_log, still_current = _mns_wait_for_core_start(
                    logs,
                    self.core_version,
                    self,
                    operation_generation,
                    session_id,
                )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"Failed to restart core: {exc}")
            with self.lifecycle_lock:
                if 'operation_generation' in locals() and (
                    self.lifecycle_generation == operation_generation
                    and self.session_id == session_id
                ):
                    self.last_start_ts = 0.0
                    self.last_config_hash = None
            raise HTTPException(
                status_code=503,
                detail=str(exc)
            )

        with self.lifecycle_lock:
            if not still_current or (
                self.lifecycle_generation != operation_generation
                or self.session_id != session_id
            ):
                logger.warning("Restart completion was superseded by a newer lifecycle transition.")
                return self.response()

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

    compile(source, "<patched-rest-service>", "exec")
    return source


def patch_file(path: Path, timeout: int, grace: int) -> None:
    original = path.read_text()
    patched = patch_source(original, timeout=timeout, grace=grace)
    staged = path.with_name(f".{path.name}.mns.tmp")
    staged.write_text(patched)
    try:
        py_compile.compile(str(staged), doraise=True)
        staged.replace(path)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


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
