import contextlib
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import time
import types
import unittest
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCHER_PATH = ROOT / "lib" / "patch_rest_service.py"
FIXTURE = ROOT / "tests" / "fixtures" / "rest_service.py"


def load_patcher():
    spec = importlib.util.spec_from_file_location("mns_patcher", PATCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def configure_fixture_hashes(patcher, path: Path):
    source = path.read_text()
    for name in ("connect", "disconnect", "start", "stop", "restart"):
        pattern = re.compile(
            rf"(?ms)^    def {re.escape(name)}\([^\n]*\):\n.*?(?=^    (?:async )?def [A-Za-z_][A-Za-z0-9_]*\(|^service = Service\(\))"
        )
        match = pattern.search(source)
        if not match:
            raise AssertionError(f"fixture method missing: {name}")
        patcher.EXPECTED_METHOD_SHA256[name] = hashlib.sha256(
            match.group(0).encode("utf-8")
        ).hexdigest()


def install_runtime_stubs():
    fastapi = types.ModuleType("fastapi")

    class APIRouter:
        def add_api_route(self, *args, **kwargs):
            pass

        def add_websocket_route(self, *args, **kwargs):
            pass

    def Body(*args, **kwargs):
        return None

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            super().__init__(str(detail))
            self.status_code = status_code
            self.detail = detail

    class FastAPI:
        def exception_handler(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

        def include_router(self, *args, **kwargs):
            pass

    class Request:
        pass

    class WebSocket:
        pass

    fastapi.APIRouter = APIRouter
    fastapi.Body = Body
    fastapi.FastAPI = FastAPI
    fastapi.HTTPException = HTTPException
    fastapi.Request = Request
    fastapi.WebSocket = WebSocket
    fastapi.status = types.SimpleNamespace(HTTP_422_UNPROCESSABLE_ENTITY=422)

    encoders = types.ModuleType("fastapi.encoders")
    encoders.jsonable_encoder = lambda value: value

    exceptions = types.ModuleType("fastapi.exceptions")

    class RequestValidationError(Exception):
        pass

    exceptions.RequestValidationError = RequestValidationError

    responses = types.ModuleType("fastapi.responses")

    class JSONResponse:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    responses.JSONResponse = JSONResponse

    starlette = types.ModuleType("starlette")
    websockets = types.ModuleType("starlette.websockets")

    class WebSocketDisconnect(Exception):
        pass

    websockets.WebSocketDisconnect = WebSocketDisconnect

    config = types.ModuleType("config")
    config.XRAY_ASSETS_PATH = "/tmp/assets"
    config.XRAY_EXECUTABLE_PATH = "/usr/bin/xray"

    logger_module = types.ModuleType("logger")

    class Logger:
        def __init__(self):
            self.messages = []

        def info(self, message):
            self.messages.append(("info", message))

        def warning(self, message):
            self.messages.append(("warning", message))

        def error(self, message):
            self.messages.append(("error", message))

    logger_module.logger = Logger()

    xray = types.ModuleType("xray")

    class XRayConfig(dict):
        def __init__(self, config_text, peer_ip):
            super().__init__(json.loads(config_text))
            # Model the upstream node-side transformation whose effective output
            # depends on the current Panel peer address.
            self["_synthetic_peer_ip"] = peer_ip
            self.peer_ip = peer_ip

    class XRayCore:
        def __init__(self, executable_path=None, assets_path=None):
            self._started = False
            self._active_logs = None
            self.start_count = 0
            self.restart_count = 0
            self.stop_count = 0

        def get_version(self):
            return "1.0.0"

        @property
        def started(self):
            return self._started

        @contextlib.contextmanager
        def get_logs(self):
            logs = deque()
            self._active_logs = logs
            try:
                yield logs
            finally:
                self._active_logs = None

        def _emit_started(self):
            if self._active_logs is not None:
                self._active_logs.append("Xray 1.0.0 started")

        def start(self, config):
            if self._started:
                raise RuntimeError("Xray is started already")
            self.start_count += 1
            self._started = True
            self._emit_started()

        def restart(self, config):
            self.restart_count += 1
            self._started = True
            self._emit_started()

        def stop(self):
            self.stop_count += 1
            self._started = False

    xray.XRayConfig = XRayConfig
    xray.XRayCore = XRayCore

    modules = {
        "fastapi": fastapi,
        "fastapi.encoders": encoders,
        "fastapi.exceptions": exceptions,
        "fastapi.responses": responses,
        "starlette": starlette,
        "starlette.websockets": websockets,
        "config": config,
        "logger": logger_module,
        "xray": xray,
    }
    sys.modules.update(modules)
    return HTTPException


def load_patched_runtime(path: Path):
    http_exception = install_runtime_stubs()
    name = f"patched_rest_service_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, http_exception


class PatchTransformTests(unittest.TestCase):
    def setUp(self):
        self.patcher = load_patcher()
        self.tmpdir = Path(tempfile.mkdtemp())
        self.target = self.tmpdir / "rest_service.py"
        shutil.copy2(FIXTURE, self.target)
        configure_fixture_hashes(self.patcher, self.target)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def patch(self, timeout=7, grace=60):
        self.patcher.patch_file(self.target, timeout=timeout, grace=grace)
        return self.target.read_text()

    def test_patch_is_idempotent_and_compilable(self):
        first = self.patch()
        second = self.patch()
        self.assertEqual(first, second)
        self.assertEqual(first.count(self.patcher.MARKER), 1)
        self.assertIn("time.monotonic()", first)
        self.assertIn("threading.RLock()", first)

    def test_patch_parameters_can_be_updated_without_duplication(self):
        self.patch(timeout=7, grace=60)
        changed = self.patch(timeout=6, grace=90)
        self.assertIn("MNS_START_CONFIRM_SECONDS = 6", changed)
        self.assertIn("MNS_RESTART_GRACE_SECONDS = 90", changed)
        self.assertEqual(changed.count(self.patcher.MARKER), 1)

    def test_unreviewed_upstream_method_change_is_rejected(self):
        source = self.target.read_text().replace(
            "        self.connected = True\n",
            "        self.connected = True\n        # simulated upstream change\n",
            1,
        )
        self.target.write_text(source)
        with self.assertRaisesRegex(ValueError, "upstream Service.connect changed"):
            self.patcher.patch_file(self.target, timeout=7, grace=60)
        self.assertNotIn(self.patcher.MARKER, self.target.read_text())


class PatchedRuntimeBehaviorTests(unittest.TestCase):
    def setUp(self):
        patcher = load_patcher()
        self.tmpdir = Path(tempfile.mkdtemp())
        self.target = self.tmpdir / "rest_service.py"
        shutil.copy2(FIXTURE, self.target)
        configure_fixture_hashes(patcher, self.target)
        patcher.patch_file(self.target, timeout=7, grace=60)
        self.runtime, self.HTTPException = load_patched_runtime(self.target)
        self.service = self.runtime.Service()
        self.request = types.SimpleNamespace(client=types.SimpleNamespace(host="192.0.2.10"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def connect(self):
        return self.service.connect(self.request)["session_id"]

    def test_started_log_exits_wait_early(self):
        session_id = self.connect()
        started = time.monotonic()
        self.service.start(session_id=session_id, config='{"inbounds": []}')
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertTrue(self.service.core.started)

    def test_same_effective_config_restart_is_deduplicated(self):
        session_id = self.connect()
        self.service.start(session_id=session_id, config='{"b": 2, "a": 1}')
        self.service.restart(session_id=session_id, config='{"a":1,"b":2}')
        self.assertEqual(self.service.core.restart_count, 0)

    def test_changed_config_restart_is_not_dropped(self):
        session_id = self.connect()
        self.service.start(session_id=session_id, config='{"a": 1}')
        self.service.restart(session_id=session_id, config='{"a": 2}')
        self.assertEqual(self.service.core.restart_count, 1)
        self.assertTrue(self.service.core.started)

    def test_effective_config_change_from_new_peer_is_not_deduplicated(self):
        first_session = self.connect()
        self.service.start(session_id=first_session, config='{"a": 1}')

        self.request.client.host = "192.0.2.11"
        second_session = self.connect()
        self.service.restart(session_id=second_session, config='{"a": 1}')

        self.assertEqual(self.service.core.restart_count, 1)
        self.assertTrue(self.service.core.started)

    def test_new_connect_does_not_stop_running_core_and_stale_disconnect_is_rejected(self):
        old_session = self.connect()
        self.service.start(session_id=old_session, config='{"a": 1}')
        self.assertTrue(self.service.core.started)
        initial_stop_count = self.service.core.stop_count

        new_session = self.connect()
        self.assertNotEqual(old_session, new_session)
        self.assertTrue(self.service.core.started)
        self.assertEqual(self.service.core.stop_count, initial_stop_count)

        with self.assertRaises(self.HTTPException) as ctx:
            self.service.disconnect(session_id=old_session)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertTrue(self.service.core.started)
        self.assertEqual(self.service.core.stop_count, initial_stop_count)

        self.service.disconnect(session_id=new_session)
        self.assertFalse(self.service.core.started)
        self.assertEqual(self.service.core.stop_count, initial_stop_count + 1)


if __name__ == "__main__":
    unittest.main()
