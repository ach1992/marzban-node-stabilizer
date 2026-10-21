"""Disposable real-Uvicorn startup failure and reserved-port cleanup regression."""
import json,os,socket,subprocess,tempfile
from pathlib import Path
root=Path(tempfile.mkdtemp(prefix="lifespan-fail-"))
os.environ["SQLALCHEMY_DATABASE_URL"]="sqlite:///"+str(root/"panel.sqlite3")
os.environ["XRAY_JSON"]=str(root/"xray.json")
(root/"xray.json").write_text(json.dumps({"inbounds":[{"tag":"TEST","listen":"127.0.0.1","port":17880,"protocol":"socks","settings":{"auth":"noauth"}}],"outbounds":[{"tag":"DIRECT","protocol":"freedom"}]}))
r=subprocess.run(["alembic","upgrade","head"],capture_output=True,text=True)
assert r.returncode==0
import main
from uvicorn.main import STARTUP_FAILURE
def fail():raise RuntimeError("Expected synthetic startup failure")
main.app.router.on_startup.insert(0,fail)
try:main.run_server({"host":"127.0.0.1","port":18443})
except SystemExit as e:assert e.code==STARTUP_FAILURE
else:raise AssertionError("Startup failure returned success")
with socket.socket() as sock:
    sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    sock.bind(("127.0.0.1",18443))
print(json.dumps({"failed_startup_exit":STARTUP_FAILURE,"reserved_port_released":True}))
