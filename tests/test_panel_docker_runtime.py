#!/usr/bin/env python3
"""Disposable real Panel + stabilized Node validation. Requires root and Compose v2."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from panel_stabilizer import Panel, FILES
import compose_mount

PANEL_IMAGE = os.getenv("MARZBAN_PANEL_IMAGE", "gozargah/marzban@sha256:4c7a449c9cc22f7b08ecc76f21638001987ce7f4a2e3687b84d15e59eed98db5")
NODE_IMAGE = os.getenv("MARZBAN_NODE_IMAGE", "gozargah/marzban-node:latest")


def run(args, *, data=None, timeout=120):
    result = subprocess.run(args, input=data, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} exit={result.returncode}\n{result.stdout[-4000:]}\n{result.stderr[-4000:]}")
    return result.stdout


def wait(check, seconds=90):
    deadline = time.monotonic() + seconds
    error = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception as exc:
            error = str(exc)
        time.sleep(.5)
    raise RuntimeError("Runtime verification timed out: " + str(error))


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run this disposable test as root")
    project = "mns-panel-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix=project+"-") as root:
        root = Path(root)
        certs = root/"certs"
        certs.mkdir()
        data = root/"panel-data"
        data.mkdir()
        compose = root/"docker-compose.yml"
        args = SimpleNamespace(command="apply", compose_file=str(compose), service="marzban", container=None,
                               patch_dir=str(root/"patches"), wait_seconds=120, ready_seconds=45,
                               stable_seconds=1, adopt_reviewed_patch=False)
        password = uuid.uuid4().hex
        (certs/"admin.json").write_text(json.dumps({"username":"test-admin", "password":password}))
        def ssl_command(*items):
            run(["openssl", *items], timeout=30)
        for name in ("client", "node", "ui-ca"):
            ssl_command("req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(certs/(name+".key")), "-out", str(certs/(name+".crt")),
                        "-days", "1", "-subj", "/CN=Gozargah",
                        "-addext", "subjectAltName=DNS:Gozargah,DNS:marzban-node,DNS:localhost")
        ssl_command("req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(certs/"ui.key"),
                    "-out", str(certs/"ui.csr"), "-subj", "/CN=localhost")
        ssl_command("x509", "-req", "-in", str(certs/"ui.csr"), "-CA", str(certs/"ui-ca.crt"),
                    "-CAkey", str(certs/"ui-ca.key"), "-CAcreateserial", "-out", str(certs/"ui.crt"), "-days", "1")
        config = {"log":{"loglevel":"warning"}, "inbounds":[{"tag":"TEST","listen":"0.0.0.0","port":17880,
                  "protocol":"socks","settings":{"auth":"noauth","udp":False}}],
                  "outbounds":[{"tag":"DIRECT","protocol":"freedom"}]}
        (data/"xray.json").write_text(json.dumps(config))
        compose.write_text(f"""name: {project}
services:
  marzban:
    image: {PANEL_IMAGE}
    container_name: {project}-panel
    restart: "no"
    mem_limit: 900m
    environment:
      PYTHONPATH: /code
      SQLALCHEMY_DATABASE_URL: sqlite:////var/lib/marzban/db.sqlite3
      XRAY_JSON: /var/lib/marzban/xray.json
      UVICORN_HOST: 127.0.0.1
      UVICORN_PORT: "8443"
      UVICORN_SSL_CERTFILE: /fixtures/ui.crt
      UVICORN_SSL_KEYFILE: /fixtures/ui.key
      JOB_CORE_HEALTH_CHECK_INTERVAL: "2"
    volumes:
      - "{data}:/var/lib/marzban"
      - "{certs}:/fixtures:ro"
  marzban-node:
    image: {NODE_IMAGE}
    container_name: {project}-node
    restart: "no"
    mem_limit: 700m
    environment:
      SERVICE_PROTOCOL: rest
      SERVICE_HOST: 0.0.0.0
      SERVICE_PORT: "62050"
      XRAY_API_PORT: "62051"
      SSL_CERT_FILE: /fixtures/node.crt
      SSL_KEY_FILE: /fixtures/node.key
      SSL_CLIENT_CERT_FILE: /fixtures/client.crt
    volumes:
      - "{certs}:/fixtures:ro"
networks:
  default:
    internal: true
""")
        def dc(*items, **kw):
            return run(["docker","compose","--project-directory",str(root),"-f",str(compose),*items], **kw)
        def cid(service):
            return dc("ps","-a","-q",service).strip()
        def node_cli(command):
            env = os.environ.copy()
            env.update(COMPOSE_FILE=str(compose), SERVICE_NAME="marzban-node",
                       CONTAINER_NAME=project+"-node", PATCH_DIR=str(root/"node-patches"),
                       LOCK_FILE=str(root/"node.lock"), STARTUP_WAIT_SECONDS="0", START_WAIT_SECONDS="25")
            result = subprocess.run([str(ROOT/"bin/marzban-node-stabilizer"),command],env=env,
                                    capture_output=True,text=True,timeout=180)
            if result.returncode:
                raise RuntimeError(f"Node {command} failed\n"+result.stdout[-5000:]+result.stderr[-3000:])
        try:
            # Seed isolated SQLite using normal migrations and the actual Admin model.
            seed = r'''
import json
from pathlib import Path
from sqlalchemy import create_engine,text
from config import SQLALCHEMY_DATABASE_URL
from app.db import GetDB,crud
from app.models.admin import AdminCreate
credentials=json.loads(Path("/fixtures/admin.json").read_text())
with GetDB() as db:
 crud.create_admin(db, AdminCreate(username=credentials["username"],password=credentials["password"],is_sudo=True))
e=create_engine(SQLALCHEMY_DATABASE_URL)
with e.begin() as c:
 c.execute(text("DELETE FROM tls"))
 c.execute(text("INSERT INTO tls (id,key,certificate) VALUES (1,:key,:certificate)"),
           {"key":Path("/fixtures/client.key").read_text(),"certificate":Path("/fixtures/client.crt").read_text()})
 c.execute(text("INSERT INTO nodes (id,name,address,port,api_port,status,uplink,downlink,usage_coefficient) VALUES (1,'test-node','marzban-node',62050,62051,'connecting',0,0,1)"))
'''
            seed_file = certs/"seed.py"
            seed_file.write_text(seed)
            dc("run","--rm","--no-deps","--entrypoint","bash","marzban","-c",
               "alembic upgrade head && python /fixtures/seed.py",timeout=90)
            dc("up","-d","--no-deps","marzban-node")
            node_cli("apply")
            stable_node = cid("marzban-node")
            dc("up","-d","--no-deps","marzban")
            panel = Panel(args)
            panel.root.mkdir(mode=0o700)
            def connected():
                probe = panel.probe(panel.inspect())
                return probe["http_ready"] and any(n["id"]==1 and n["status"]=="connected" for n in probe["nodes"])
            wait(connected)
            original = compose.read_text()
            old_panel = cid("marzban")
            panel.apply()
            active_panel = cid("marzban")
            assert active_panel != old_panel
            assert cid("marzban-node") == stable_node, "Panel apply recreated its Node dependency"
            panel.apply()
            assert cid("marzban") == active_panel, "Identical apply recreated Panel again"

            # Exercise actual authenticated Core changes while Node operations can still be pending.
            client = r'''
import json,ssl,time,urllib.parse,urllib.request
from pathlib import Path
ctx=ssl._create_unverified_context()
base="https://127.0.0.1:8443"
credentials=json.loads(Path("/fixtures/admin.json").read_text())
req=urllib.request.Request(base+"/api/admin/token",data=urllib.parse.urlencode(credentials).encode())
with urllib.request.urlopen(req,context=ctx,timeout=8) as r:token=json.load(r)["access_token"]
headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"}
def request(path,method="GET",body=None):
 req=urllib.request.Request(base+path,headers=headers,method=method,data=json.dumps(body).encode() if body is not None else None)
 with urllib.request.urlopen(req,context=ctx,timeout=15) as r:return json.load(r)
config=request("/api/core/config")
for port in (17882,17883):
 next(i for i in config["inbounds"] if i["tag"]=="TEST")["port"]=port
 request("/api/core/config","PUT",config)
print("authenticated Core updates accepted")
'''
            run(["docker","exec",project+"-panel","python","-B","-c",client])
            port_check = r'''
import socket,sys
def open_port(port):
 try:
  with socket.create_connection(("127.0.0.1",port),timeout=1):return True
 except OSError:return False
sys.exit(0 if open_port(17883) and not open_port(17882) and not open_port(17880) else 1)
'''
            def new_config_active():
                result = subprocess.run(["docker","exec",project+"-node","python","-c",port_check],capture_output=True,text=True)
                return result.returncode == 0
            wait(new_config_active)
            wait(connected)
            # A real SOCKS handshake and HTTP response traverse the updated Node Core.
            run(["docker","exec","-d",project+"-node","python","-m","http.server","18777","--bind","127.0.0.1"])
            traffic = r'''
import socket,time
time.sleep(.5)
s=socket.create_connection(("marzban-node",17883),timeout=5);s.sendall(b"\x05\x01\x00");assert s.recv(2)==b"\x05\x00"
s.sendall(b"\x05\x01\x00\x01\x7f\x00\x00\x01"+(18777).to_bytes(2,"big"))
reply=s.recv(10);assert reply[1]==0,reply
s.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n");assert b"200 OK" in s.recv(1024);s.close()
print("real SOCKS/HTTP traffic passed")
'''
            run(["docker","exec",project+"-panel","python","-c",traffic])
            panel.restore()
            assert compose.read_text() == original
            wait(connected)

            # Fail after real activation; the real transaction must recreate and verify the old topology.
            panel = Panel(args)
            actual_wait = panel.wait_healthy
            calls = [0]
            def fail_once(*a, **kw):
                calls[0] += 1
                if calls[0] == 1:
                    raise RuntimeError("synthetic post-activation failure")
                return actual_wait(*a, **kw)
            panel.wait_healthy = fail_once
            try:
                panel.apply()
            except RuntimeError as exc:
                assert "synthetic post-activation failure" in str(exc)
            else:
                raise AssertionError("Injected activation failure was not reported")
            assert compose.read_text() == original
            assert json.loads(panel.journal.read_text())["phase"] == "recovered"
            wait(connected)

            # Explicit adoption must restore the exact original foreign mount topology.
            manual = root/"manual"
            shutil.copytree(ROOT/"tests/fixtures/panel/manual",manual)
            text = compose.read_text()
            for file in FILES:
                text = compose_mount.add_mount(text,"marzban",str(manual/file),"/code/"+file)
            compose.write_text(text)
            dc("up","-d","--no-deps","--force-recreate","marzban")
            wait(connected)
            panel = Panel(args)
            prior = panel.original_mounts(compose.read_text())
            args.adopt_reviewed_patch = True
            panel.apply()
            args.adopt_reviewed_patch = False
            panel.restore()
            assert panel.original_mounts(compose.read_text()) == prior
            wait(connected)
            # Node remains independently restorable.
            node_cli("restore")
            wait(connected,seconds=120)
            print(json.dumps({"result":"PASS","panel_single_apply":True,"idempotent_apply":True,
                              "node_not_recreated_by_panel":True,"actual_authenticated_config_updates":True,
                              "real_socks_http_traffic":True,"verified_activation_rollback":True,
                              "manual_adoption_restore":True,"independent_node_restore":True}),flush=True)
        except BaseException:
            for name in (project+"-panel",project+"-node"):
                output = subprocess.run(["docker","logs","--tail","30",name],capture_output=True,text=True)
                print("DIAGNOSTIC",name,output.stdout[-3000:],output.stderr[-3000:],flush=True)
            raise
        finally:
            dc("down","--remove-orphans","-v",timeout=90)


if __name__ == "__main__":
    main()
