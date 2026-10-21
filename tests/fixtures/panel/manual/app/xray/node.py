import socket
import re
import ssl
import tempfile
import threading
import time
from collections import deque
from copy import deepcopy
from contextlib import contextmanager
from typing import List

import grpc
import requests
import rpyc
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.poolmanager import PoolManager
from websocket import (
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
    create_connection,
)

from app.xray.config import XRayConfig
from xray_api import XRay as XRayAPI
from logging import getLogger

logger = getLogger("uvicorn.error")

def string_to_temp_file(content: str):
    file = tempfile.NamedTemporaryFile(mode="w+t")
    file.write(content)
    file.flush()
    return file


class SANIgnoringAdaptor(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = PoolManager(
            num_pools=connections, maxsize=maxsize, block=block, assert_hostname=False
        )


class NodeAPIError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class ReSTXRayNode:
    def __init__(
        self,
        address: str,
        port: int,
        api_port: int,
        ssl_key: str,
        ssl_cert: str,
        usage_coefficient: float = 1,
    ):
        self.address = address
        self.port = port
        self.api_port = api_port
        self.ssl_key = ssl_key
        self.ssl_cert = ssl_cert
        self.usage_coefficient = usage_coefficient

        self._keyfile = string_to_temp_file(ssl_key)
        self._certfile = string_to_temp_file(ssl_cert)

        self.session = requests.Session()
        self.session.mount("https://", SANIgnoringAdaptor())
        self.session.cert = (self._certfile.name, self._keyfile.name)

        self._session_id = None
        self._rest_api_url = f"https://{self.address.strip('/')}:{self.port}"

        self._ssl_context = ssl.create_default_context()
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = ssl.CERT_NONE
        self._ssl_context.load_cert_chain(
            certfile=self.session.cert[0], keyfile=self.session.cert[1]
        )
        self._logs_ws_url = f"wss://{self.address.strip('/')}:{self.port}/logs"
        self._logs_queues = []
        self._logs_bg_thread = threading.Thread(target=self._bg_fetch_logs, daemon=True)

        self._api = None
        self._started = False

        # Elapsed-time caches must not depend on wall-clock adjustments.
        self._connected_cache = None
        self._connected_cache_time = 0
        self._started_cache = None
        self._started_cache_time = 0
        self._cache_ttl = 150
        self._error_cache_ttl = 150
        self._state_lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._state_generation = 0

    def _prepare_config(self, config: XRayConfig):
        for inbound in config.get("inbounds", []):
            streamSettings = inbound.get("streamSettings") or {}
            tlsSettings = streamSettings.get("tlsSettings") or {}
            certificates = tlsSettings.get("certificates") or []
            for certificate in certificates:
                if certificate.get("certificateFile"):
                    with open(certificate["certificateFile"]) as file:
                        certificate["certificate"] = [
                            line.strip() for line in file.readlines()
                        ]
                        del certificate["certificateFile"]

                if certificate.get("keyFile"):
                    with open(certificate["keyFile"]) as file:
                        certificate["key"] = [line.strip() for line in file.readlines()]
                        del certificate["keyFile"]

        return config

    def make_request(self, path: str, timeout: int, **params):
        try:
            res = self.session.post(
                self._rest_api_url + path,
                timeout=timeout,
                json={"session_id": self._session_id, **params},
            )
            data = res.json()
        except Exception as e:
            exc = NodeAPIError(0, str(e))
            raise exc

        if res.status_code == 200:
            return data
        else:
            exc = NodeAPIError(res.status_code, data["detail"])
            raise exc

    def _update_status_cache(self, connected=None, started=None):
        with self._state_lock:
            self._state_generation += 1
            now = time.monotonic()
            self._connected_cache = connected
            self._connected_cache_time = now
            self._started_cache = started
            self._started_cache_time = now

    def _cached_status(self, field):
        # Coalesce concurrent expired-cache reads for this node.
        with self._status_lock:
            with self._state_lock:
                if not self._session_id:
                    return False
                value = getattr(self, "_" + field + "_cache")
                checked = getattr(self, "_" + field + "_cache_time")
                ttl = self._cache_ttl if value else self._error_cache_ttl
                if value is not None and time.monotonic() - checked < ttl:
                    return value
                generation = self._state_generation
                session_id = self._session_id

            try:
                if field == "connected":
                    self.make_request("/ping", timeout=30)
                    value = True
                else:
                    value = bool(self.make_request("/", timeout=30).get("started", False))
            except NodeAPIError as exc:
                value = False
                logger.debug("Node %s %s probe failed: status=%s detail=%s",
                             self.address, field, exc.status_code, exc.detail)

            with self._state_lock:
                if (generation != self._state_generation or
                        session_id != self._session_id):
                    # An older probe cannot overwrite a completed lifecycle transition.
                    return getattr(self, "_" + field + "_cache") is True
                previous = getattr(self, "_" + field + "_cache")
                setattr(self, "_" + field + "_cache", value)
                setattr(self, "_" + field + "_cache_time", time.monotonic())
            if previous != value:
                logger.info("Node %s %s status: %s", self.address, field, value)
            return value

    @property
    def connected(self):
        return self._cached_status("connected")

    @property
    def started(self):
        return self._cached_status("started")

    @property
    def api(self):
        if not self._session_id:
            raise ConnectionError("Node is not connected")

        if not self._api:
            if self._started is True:
                self._api = XRayAPI(
                    address=self.address,
                    port=self.api_port,
                    ssl_cert=self._node_cert.encode(),
                    ssl_target_name="Gozargah",
                )
            else:
                raise ConnectionError("Node is not started")

        return self._api

    def connect(self):
        with self._lifecycle_lock:
            self._update_status_cache()
            self._started = False
            self._api = None
            try:
                self._node_cert = ssl.get_server_certificate((self.address, self.port))
                self._node_certfile = string_to_temp_file(self._node_cert)
                self.session.verify = self._node_certfile.name
                res = self.make_request("/connect", timeout=30)
                self._session_id = res["session_id"]
                self._update_status_cache(connected=True)
            except Exception:
                self._session_id = None
                self._update_status_cache(connected=False, started=False)
                raise

    def disconnect(self):
        with self._lifecycle_lock:
            try:
                if self._session_id:
                    self.make_request("/disconnect", timeout=30)
            finally:
                self._session_id = None
                self._started = False
                self._api = None
                self._update_status_cache(connected=False, started=False)

    def get_version(self):
        res = self.make_request("/", timeout=30)
        return res.get("core_version")

    def start(self, config: XRayConfig):
        with self._lifecycle_lock:
            if not self.connected:
                self.connect()
            self._update_status_cache(connected=True)
            try:
                config = self._prepare_config(deepcopy(config))
                try:
                    res = self.make_request("/start", timeout=60, config=config.to_json())
                except NodeAPIError as exc:
                    if exc.detail == "Xray is started already":
                        return self.restart(config)
                    raise
                self._wait_for_api()
                self._update_status_cache(connected=True, started=True)
                return res
            except Exception:
                self._started = False
                self._api = None
                self._update_status_cache()
                raise

    def _wait_for_api(self):
        self._started = True
        self._api = XRayAPI(
            address=self.address,
            port=self.api_port,
            ssl_cert=self._node_cert.encode(),
            ssl_target_name="Gozargah",
        )
        try:
            grpc.channel_ready_future(self._api._channel).result(timeout=35)
        except grpc.FutureTimeoutError:
            raise ConnectionError("Failed to connect to node's API")

    def stop(self):
        if not self.connected:
            self.connect()

        self.make_request("/stop", timeout=35)
        self._api = None
        self._started = False

    def restart(self, config: XRayConfig):
        with self._lifecycle_lock:
            if not self.connected:
                self.connect()
            self._update_status_cache(connected=True)
            try:
                config = self._prepare_config(deepcopy(config))
                res = self.make_request("/restart", timeout=60, config=config.to_json())
                self._wait_for_api()
                self._update_status_cache(connected=True, started=True)
                return res
            except Exception:
                self._started = False
                self._api = None
                self._update_status_cache()
                raise

    def _bg_fetch_logs(self):
        while self._logs_queues:
            try:
                websocket_url = (
                    f"{self._logs_ws_url}?session_id={self._session_id}&interval=0.7"
                )
                self._ssl_context.load_verify_locations(self.session.verify)
                ws = create_connection(
                    websocket_url, sslopt={"context": self._ssl_context}, timeout=2
                )
                while self._logs_queues:
                    try:
                        logs = ws.recv()
                        for buf in self._logs_queues:
                            buf.append(logs)
                    except WebSocketConnectionClosedException:
                        break
                    except WebSocketTimeoutException:
                        pass
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(2)

    @contextmanager
    def get_logs(self):
        try:
            buf = deque(maxlen=100)
            self._logs_queues.append(buf)

            if not self._logs_bg_thread.is_alive():
                try:
                    self._logs_bg_thread.start()
                except RuntimeError:
                    self._logs_bg_thread = threading.Thread(
                        target=self._bg_fetch_logs, daemon=True
                    )
                    self._logs_bg_thread.start()

            yield buf

        finally:
            try:
                self._logs_queues.remove(buf)
            except ValueError:
                pass
            del buf


class RPyCXRayNode:
    def __init__(
        self,
        address: str,
        port: int,
        api_port: int,
        ssl_key: str,
        ssl_cert: str,
        usage_coefficient: float = 1,
    ):
        class Service(rpyc.Service):
            def __init__(
                self,
                on_start_funcs: List[callable] = [],
                on_stop_funcs: List[callable] = [],
            ):
                self.on_start_funcs = on_start_funcs
                self.on_stop_funcs = on_stop_funcs

            def exposed_on_start(self):
                for func in self.on_start_funcs:
                    threading.Thread(target=func).start()

            def exposed_on_stop(self):
                for func in self.on_stop_funcs:
                    threading.Thread(target=func).start()

            def add_startup_func(self, func):
                self.on_start_funcs.append(func)

            def add_shutdown_func(self, func):
                self.on_stop_funcs.append(func)

            def on_connect(self, conn):
                pass

            def on_disconnect(self, conn):
                pass

        self.address = address
        self.port = port
        self.api_port = api_port
        self.ssl_key = ssl_key
        self.ssl_cert = ssl_cert
        self.usage_coefficient = usage_coefficient

        self.started = False

        self._keyfile = string_to_temp_file(ssl_key)
        self._certfile = string_to_temp_file(ssl_cert)

        self._service = Service()
        self._api = None

    def disconnect(self):
        try:
            self.connection.close()
            del self.connection
        except AttributeError:
            pass

    def connect(self):
        self.disconnect()

        tries = 0
        while True:
            tries += 1
            self._node_cert = ssl.get_server_certificate((self.address, self.port))
            self._node_certfile = string_to_temp_file(self._node_cert)
            conn = rpyc.ssl_connect(
                self.address,
                self.port,
                service=self._service,
                keyfile=self._keyfile.name,
                certfile=self._certfile.name,
                ca_certs=self._node_certfile.name,
                keepalive=True,
            )
            try:
                conn.ping()
                self.connection = conn
                break
            except EOFError as exc:
                if tries <= 3:
                    continue
                raise exc

    @property
    def connected(self):
        try:
            self.connection.ping()
            return not self.connection.closed
        except (AttributeError, EOFError, TimeoutError):
            self.disconnect()
            return False

    @property
    def remote(self):
        if not self.connected:
            self.connect()
        return self.connection.root

    @property
    def api(self):
        if not self.connected:
            raise ConnectionError("Node is not connected")

        if not self.started:
            raise ConnectionError("Node is not started")

        return self._api

    def get_version(self):
        return self.remote.fetch_xray_version()

    def _prepare_config(self, config: XRayConfig):
        for inbound in config.get("inbounds", []):
            streamSettings = inbound.get("streamSettings") or {}
            tlsSettings = streamSettings.get("tlsSettings") or {}
            certificates = tlsSettings.get("certificates") or []
            for certificate in certificates:
                if certificate.get("certificateFile"):
                    with open(certificate["certificateFile"]) as file:
                        certificate["certificate"] = [
                            line.strip() for line in file.readlines()
                        ]
                        del certificate["certificateFile"]

                if certificate.get("keyFile"):
                    with open(certificate["keyFile"]) as file:
                        certificate["key"] = [line.strip() for line in file.readlines()]
                        del certificate["keyFile"]

        return config

    def start(self, config: XRayConfig):
        config = self._prepare_config(config)
        json_config = config.to_json()
        self.remote.start(json_config)
        self.started = True

        # connect to API
        self._api = XRayAPI(
            address=self.address,
            port=self.api_port,
            ssl_cert=self._node_cert.encode(),
            ssl_target_name="Gozargah",
        )
        try:
            grpc.channel_ready_future(self._api._channel).result(timeout=5)
        except grpc.FutureTimeoutError:
            start_time = time.time()
            end_time = start_time + 3  # check logs for 3 seconds
            last_log = ""
            with self.get_logs() as logs:
                while time.time() < end_time:
                    if logs:
                        last_log = logs[-1].strip().split("\n")[-1]
                    time.sleep(0.1)

            self.disconnect()

            if re.search(r"[Ff]ailed", last_log):
                raise RuntimeError(last_log)

            raise ConnectionError("Failed to connect to node's API")

    def stop(self):
        self.remote.stop()
        self.started = False
        self._api = None

    def restart(self, config: XRayConfig):
        self.started = False
        config = self._prepare_config(config)
        json_config = config.to_json()
        self.remote.restart(json_config)
        self.started = True

    @contextmanager
    def get_logs(self):
        if not self.connected:
            raise ConnectionError("Node is not connected")

        try:
            self.__curr_logs
        except AttributeError:
            self.__curr_logs = 0

        try:
            buf = deque(maxlen=100)

            if self.__curr_logs <= 0:
                self.__curr_logs = 1
                self.__bgsrv = rpyc.BgServingThread(self.connection)
            else:
                if not self.__bgsrv._active:
                    self.__bgsrv = rpyc.BgServingThread(self.connection)
                self.__curr_logs += 1

            logs = self.remote.fetch_logs(buf.append)
            yield buf

        finally:
            if self.__curr_logs <= 1:
                self.__curr_logs = 0
                self.__bgsrv.stop()
            else:
                if not self.__bgsrv._active:
                    self.__bgsrv = rpyc.BgServingThread(self.connection)
                self.__curr_logs -= 1

            if logs:
                logs.stop()

    def on_start(self, func: callable):
        self._service.add_startup_func(func)
        return func

    def on_stop(self, func: callable):
        self._service.add_shutdown_func(func)
        return func


class XRayNode:
    def __new__(
        self,
        address: str,
        port: int,
        api_port: int,
        ssl_key: str,
        ssl_cert: str,
        usage_coefficient: float = 1,
    ):
        # trying to detect what's the server of node
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect((address, port))
            s.send(b"HEAD / HTTP/1.0\r\n\r\n")
            s.recv(1024)
            s.close()
            # it might be uvicorn
            return ReSTXRayNode(
                address=address,
                port=port,
                api_port=api_port,
                ssl_key=ssl_key,
                ssl_cert=ssl_cert,
                usage_coefficient=usage_coefficient,
            )
        except Exception:
            # if might be rpyc
            return RPyCXRayNode(
                address=address,
                port=port,
                api_port=api_port,
                ssl_key=ssl_key,
                ssl_cert=ssl_cert,
                usage_coefficient=usage_coefficient,
            )
