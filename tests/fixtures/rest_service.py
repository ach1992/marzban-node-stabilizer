"""Synthetic Marzban-node-like source used only to test the transformer mechanics."""

import asyncio
import json
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, FastAPI, HTTPException, Request, WebSocket
from config import XRAY_ASSETS_PATH, XRAY_EXECUTABLE_PATH
from logger import logger
from xray import XRayConfig, XRayCore

app = FastAPI()


class Service(object):
    def __init__(self):
        self.router = APIRouter()
        self.connected = False
        self.client_ip = None
        self.session_id = None
        self.core = XRayCore(executable_path=XRAY_EXECUTABLE_PATH, assets_path=XRAY_ASSETS_PATH)
        self.core_version = self.core.get_version()
        self.config = None

        self.router.add_api_route("/", self.base, methods=["POST"])
        self.router.add_api_route("/ping", self.ping, methods=["POST"])
        self.router.add_api_route("/connect", self.connect, methods=["POST"])
        self.router.add_api_route("/disconnect", self.disconnect, methods=["POST"])
        self.router.add_api_route("/start", self.start, methods=["POST"])
        self.router.add_api_route("/stop", self.stop, methods=["POST"])
        self.router.add_api_route("/restart", self.restart, methods=["POST"])
        self.router.add_websocket_route("/logs", self.logs)

    def match_session_id(self, session_id: UUID):
        if session_id != self.session_id:
            raise HTTPException(status_code=403, detail="Session ID mismatch.")
        return True

    def response(self, **kwargs):
        return {"connected": self.connected, "started": self.core.started, "core_version": self.core_version, **kwargs}

    def base(self):
        return self.response()

    def connect(self, request: Request):
        self.session_id = uuid4()
        self.client_ip = request.client.host
        if self.connected and self.core.started:
            self.core.stop()
        self.connected = True
        return self.response(session_id=self.session_id)

    def disconnect(self):
        self.session_id = None
        self.client_ip = None
        self.connected = False
        if self.core.started:
            self.core.stop()
        return self.response()

    def ping(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)
        return {}

    def start(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True)):
        self.match_session_id(session_id)
        config = XRayConfig(config, self.client_ip)
        with self.core.get_logs() as logs:
            self.core.start(config)
            end_time = time.time() + 3
            last_log = ""
            while time.time() < end_time:
                while logs:
                    last_log = logs.popleft()
                    if f"Xray {self.core_version} started" in last_log:
                        break
                time.sleep(0.1)
        if not self.core.started:
            raise HTTPException(status_code=503, detail=last_log)
        return self.response()

    def stop(self, session_id: UUID = Body(embed=True)):
        self.match_session_id(session_id)
        self.core.stop()
        return self.response()

    def restart(self, session_id: UUID = Body(embed=True), config: str = Body(embed=True)):
        self.match_session_id(session_id)
        config = XRayConfig(config, self.client_ip)
        with self.core.get_logs() as logs:
            self.core.restart(config)
            end_time = time.time() + 3
            last_log = ""
            while time.time() < end_time:
                while logs:
                    last_log = logs.popleft()
                    if f"Xray {self.core_version} started" in last_log:
                        break
                time.sleep(0.1)
        if not self.core.started:
            raise HTTPException(status_code=503, detail=last_log)
        return self.response()

    async def logs(self, websocket: WebSocket):
        await asyncio.sleep(0)


service = Service()
app.include_router(service.router)
