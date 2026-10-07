import contextlib
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
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
            self["_synthetic_peer_ip"] = peer_ip
            self.peer_ip = peer_ip

    class XRayCore:
        def __init__(self, executable_path=None, assets_path=None):
            self._started = False
            self._active_logs = None
            self._logs_buffer = deque(maxlen=100)
            self.start_count = 0
            self.restart_count = 0
            self.stop_count = 0
            self.emit_started = True

        def get_version(self):
            return "1.0.0"

        @property
        def started(self):
            return self._started

        @contextlib.contextmanager
        def get_logs(self):
            logs = deque(self._logs_buffer, maxlen=100)
            self._active_logs = logs
            try:
                yield logs
            finally:
                self._active_logs = None

        def _emit_started(self):
            if self.emit_started:
                log = "Xray 1.0.0 started"
                self._logs_buffer.append(log)
                if self._active_logs is not None:
                    self._active_logs.append(log)

        def start(self, config):
            if self._started:
                raise RuntimeError("Xray is started already")
            self.start_count += 1
            self._started = True
            self._emit_started()

        def restart(self, config):
            self.restart_count += 1
            self.stop()
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
        self.assertIn("self.lifecycle_generation = 0", first)

    def test_patch_parameters_can_be_updated_without_duplication(self):
        self.patch(timeout=7, grace=60)
        changed = self.patch(timeout=6, grace=90)
        self.assertIn("MNS_START_CONFIRM_SECONDS = 6", changed)
        self.assertIn("MNS_RESTART_GRACE_SECONDS = 90", changed)
        self.assertEqual(changed.count(self.patcher.MARKER), 1)

    def test_unreviewed_upstream_method_change_is_rejected_without_partial_output(self):
        source = self.target.read_text().replace(
            "        self.connected = True\n",
            "        self.connected = True\n        # simulated upstream change\n",
            1,
        )
        self.target.write_text(source)
        original = self.target.read_text()
        with self.assertRaisesRegex(ValueError, "upstream Service.connect changed"):
            self.patcher.patch_file(self.target, timeout=7, grace=60)
        self.assertEqual(self.target.read_text(), original)
        self.assertNotIn(self.patcher.MARKER, self.target.read_text())

    def test_state_anchor_is_bound_to_service_init_not_earlier_object(self):
        source = self.target.read_text()
        prefix = """class EarlierObject:\n    def __init__(self):\n        self.config = None\n\n\n"""
        self.target.write_text(prefix + source)
        patched = self.patch()
        earlier_object = patched.split(self.patcher.MARKER, 1)[0]
        _, service = patched.split("class Service(object):", 1)
        self.assertNotIn("lifecycle_generation", earlier_object)
        self.assertIn("self.lifecycle_generation = 0", service)

    def test_ambiguous_service_state_anchor_is_rejected(self):
        source = self.target.read_text().replace(
            "        self.config = None\n",
            "        self.config = None\n        self.config = None\n",
            1,
        )
        self.target.write_text(source)
        original = self.target.read_text()
        with self.assertRaisesRegex(ValueError, "exactly one Service.__init__ assignment"):
            self.patcher.patch_file(self.target, timeout=7, grace=60)
        self.assertEqual(self.target.read_text(), original)


class PatchedRuntimeBehaviorTests(unittest.TestCase):
    def setUp(self):
        patcher = load_patcher()
        self.tmpdir = Path(tempfile.mkdtemp())
        self.target = self.tmpdir / "rest_service.py"
        shutil.copy2(FIXTURE, self.target)
        patcher.patch_file(self.target, timeout=7, grace=60)
        self.runtime, self.HTTPException = load_patched_runtime(self.target)
        self.runtime.MNS_START_CONFIRM_SECONDS = 1
        self.service = self.runtime.Service()
        self.request = types.SimpleNamespace(client=types.SimpleNamespace(host="192.0.2.10"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def connect(self):
        return self.service.connect(self.request)["session_id"]

    def run_in_thread(self, func):
        result = {}

        def target():
            try:
                result["value"] = func()
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=target)
        thread.start()
        return thread, result

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

    def test_restart_does_not_accept_stale_started_log(self):
        session_id = self.connect()
        self.service.start(session_id=session_id, config='{"a": 1}')
        self.assertIn("Xray 1.0.0 started", self.service.core._logs_buffer)

        self.service.core.emit_started = False
        started = time.monotonic()
        self.service.restart(session_id=session_id, config='{"a": 2}')
        elapsed = time.monotonic() - started

        self.assertGreater(elapsed, 0.8)
        self.assertEqual(self.service.core.restart_count, 1)
        self.assertTrue(self.service.core.started)

    def test_effective_config_change_from_peer_context_is_not_deduplicated(self):
        session_id = self.connect()
        self.service.start(session_id=session_id, config='{"a": 1}')
        self.service.client_ip = "192.0.2.11"
        self.service.restart(session_id=session_id, config='{"a": 1}')
        self.assertEqual(self.service.core.restart_count, 1)
        self.assertTrue(self.service.core.started)

    def test_new_connect_preserves_takeover_and_stale_disconnect_is_rejected(self):
        old_session = self.connect()
        self.service.start(session_id=old_session, config='{"a": 1}')
        initial_stop_count = self.service.core.stop_count

        self.request.client.host = "192.0.2.11"
        new_session = self.connect()
        self.assertNotEqual(old_session, new_session)
        self.assertFalse(self.service.core.started)
        self.assertEqual(self.service.core.stop_count, initial_stop_count + 1)

        with self.assertRaises(self.HTTPException) as ctx:
            self.service.disconnect(session_id=old_session)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self.service.core.stop_count, initial_stop_count + 1)

        self.service.start(session_id=new_session, config='{"a": 1}')
        self.assertTrue(self.service.core.started)
        self.service.disconnect(session_id=new_session)
        self.assertFalse(self.service.core.started)

    def test_slow_start_does_not_block_connect_and_stale_completion_cannot_commit(self):
        old_session = self.connect()
        self.service.core.emit_started = False
        thread, result = self.run_in_thread(
            lambda: self.service.start(session_id=old_session, config='{"a": 1}')
        )
        time.sleep(0.05)

        self.request.client.host = "192.0.2.11"
        started = time.monotonic()
        new_session = self.connect()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.3)
        self.assertNotEqual(old_session, new_session)

        thread.join(timeout=1.5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(self.service.session_id, new_session)
        self.assertIsNone(self.service.last_config_hash)
        self.assertFalse(self.service.core.started)

    def test_slow_restart_does_not_block_disconnect(self):
        session_id = self.connect()
        self.service.start(session_id=session_id, config='{"a": 1}')
        self.service.core.emit_started = False
        thread, result = self.run_in_thread(
            lambda: self.service.restart(session_id=session_id, config='{"a": 2}')
        )
        time.sleep(0.05)

        started = time.monotonic()
        self.service.disconnect(session_id=session_id)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.3)

        thread.join(timeout=1.5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        self.assertIsNone(self.service.session_id)
        self.assertFalse(self.service.connected)
        self.assertIsNone(self.service.last_config_hash)

    def test_slow_restart_does_not_block_stop(self):
        session_id = self.connect()
        self.service.start(session_id=session_id, config='{"a": 1}')
        self.service.core.emit_started = False
        thread, result = self.run_in_thread(
            lambda: self.service.restart(session_id=session_id, config='{"a": 2}')
        )
        time.sleep(0.05)

        started = time.monotonic()
        self.service.stop(session_id=session_id)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.3)

        thread.join(timeout=1.5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(self.service.session_id, session_id)
        self.assertFalse(self.service.core.started)
        self.assertIsNone(self.service.last_config_hash)


if __name__ == "__main__":
    unittest.main()
