"""Run only inside a disposable pinned Marzban container; uses no production data."""
import asyncio
import inspect
import json
import os
import sys
import subprocess
import tempfile
import time
from pathlib import Path

root=Path(tempfile.mkdtemp(prefix="panel-recovery-smoke-"))
os.environ["SQLALCHEMY_DATABASE_URL"]="sqlite:///"+str(root/"panel.sqlite3")
os.environ["XRAY_JSON"]=str(root/"xray.json")
(root/"xray.json").write_text(json.dumps({
    "log":{"loglevel":"warning"},
    "inbounds":[{"tag":"SMOKE","listen":"127.0.0.1","port":17880,
                 "protocol":"socks","settings":{"auth":"noauth","udp":False}}],
    "outbounds":[{"tag":"DIRECT","protocol":"freedom"}],
}))
migration=subprocess.run(["alembic","upgrade","head"],capture_output=True,text=True)
if migration.returncode:
    raise RuntimeError(migration.stdout + migration.stderr)
from app import app,xray
from app.xray import operations
from app.xray.node import ReSTXRayNode
import importlib
from app.routers import core as core_router

variant=sys.argv[1]
expected=variant=="candidate"
assert ("health_check" in inspect.signature(operations.restart_node).parameters)==expected
assert hasattr(ReSTXRayNode,"_cached_status")==expected
assert hasattr(core_router,"_core_config_lock")==expected

async def main():
    await app.router.startup()
    try:
        assert xray.core.started, "Actual Xray core did not start"
        import grpc
        grpc.channel_ready_future(xray.api._channel).result(timeout=10)
        stats=xray.api.get_sys_stats(timeout=3)
        assert stats is not None
        payload=json.loads((root/"xray.json").read_text())
        payload["inbounds"][0]["port"]=17881
        core_router.modify_core_config(payload)
        deadline=time.monotonic()+10
        while True:
            try:
                xray.api.get_sys_stats(timeout=1)
                break
            except Exception:
                if time.monotonic()>=deadline:raise
                time.sleep(.05)
        importlib.import_module("app.jobs.0_xray_core").core_health_check()
        assert xray.core.started
        print(json.dumps({"variant":variant,"application_startup":"OK",
                          "actual_xray_api":"OK","core_config_update":"OK","health_check":"OK"}))
    finally:
        await app.router.shutdown()
    assert not xray.core.started
    print(json.dumps({"variant":variant,"application_shutdown":"OK"}))

asyncio.run(main())
