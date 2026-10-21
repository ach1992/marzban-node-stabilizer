"""Disposable reproduction of panel/Core port collision; synthetic data only."""
import copy,json,os,subprocess,tempfile,time,threading,uuid
from pathlib import Path
from types import SimpleNamespace
import sys
root=Path(tempfile.mkdtemp(prefix="port-repro-"))
os.environ["SQLALCHEMY_DATABASE_URL"]="sqlite:///"+str(root/"panel.sqlite3")
os.environ["XRAY_JSON"]=str(root/"xray.json")
clients=[{"id":str(uuid.UUID(int=i+1)),"email":f"fake-{i}@example.test"} for i in range(2594)]
config={"log":{"loglevel":"warning"},"inbounds":[{"tag":f"FAKE-{i}","listen":"127.0.0.1","port":8443 if i==0 and "--no-collision" not in sys.argv else 18000+i,"protocol":"vless","settings":{"clients":copy.deepcopy(clients),"decryption":"none"}} for i in range(31)],"outbounds":[{"tag":"DIRECT","protocol":"freedom"}]}
(root/"xray.json").write_text(json.dumps({"inbounds":[{"tag":"FAKE","listen":"127.0.0.1","port":8443,"protocol":"vless","settings":{"clients":[],"decryption":"none"}}],"outbounds":[{"tag":"DIRECT","protocol":"freedom"}]}))
r=subprocess.run(["alembic","upgrade","head"],capture_output=True,text=True)
assert r.returncode==0,r.stderr
from app import app,xray
from app.xray.config import XRayConfig
large=XRayConfig(config)
xray.config.include_db_users=lambda:copy.deepcopy(large)
from app.db import crud
from app.models.node import NodeStatus
from app.xray import operations
import uvicorn
nodes={i:SimpleNamespace(id=i,name=f"FAKE-{i}",status=NodeStatus.connected,xray_version="test") for i in range(1,15)}
crud.get_nodes=lambda *a,**kw:list(nodes.values())
crud.get_node_by_id=lambda db,i:nodes.get(i)
def update(db,n,status,message=None,version=None,*a,**kw):n.status=status
crud.update_node_status=update
class Fake:
    connected=True;started=True;usage_coefficient=1
    api=SimpleNamespace(get_sys_stats=lambda **kw:None)
    def start(self,c):pass
    def restart(self,c):pass
    def disconnect(self):pass
    def get_version(self):return "test"
def add(n):
    f=Fake();xray.nodes[n.id]=f;return f
operations.add_node=add
import sys,ssl,urllib.request
servers=[];errors=[]
RealServer=uvicorn.Server
def capture(c):
    server=RealServer(c);servers.append(server);return server
uvicorn.Server=capture
bind_args={"host":"127.0.0.1","port":8443}
use_tls="--tls" in sys.argv
if use_tls:
    r=subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-keyout",str(root/"key.pem"),"-out",str(root/"cert.pem"),"-days","1","-subj","/CN=localhost"],capture_output=True)
    assert r.returncode==0
    bind_args.update(ssl_certfile=str(root/"cert.pem"),ssl_keyfile=str(root/"key.pem"))

def run():
    try:
        if "--reserve" in sys.argv:
            import main
            main.run_server(bind_args)
        else:
            capture(uvicorn.Config(app,**bind_args,log_level="error")).run()
    except BaseException as e:errors.append(type(e).__name__+":"+str(e))
t=threading.Thread(target=run,daemon=True)
start=time.monotonic();t.start()
while time.monotonic()-start<15 and not (servers and servers[0].started) and t.is_alive():time.sleep(.05)
started=bool(servers and servers[0].started)
http_status=None
if started:
    request=urllib.request.Request(("https" if use_tls else "http")+"://127.0.0.1:8443/",headers={"Connection":"close"})
    with urllib.request.urlopen(request,context=ssl._create_unverified_context(),timeout=3) as response:
        http_status=response.status
        response.read()
# Let Xray finish parsing so the Core outcome is measured rather than Popen's initial state.
time.sleep(.3)
print(json.dumps({"candidate":hasattr(operations,"_node_operations"),"reserved":"--reserve" in sys.argv,"tls":use_tls,"panel_started":started,"http_status":http_status,"xray_started":xray.core.started,"elapsed":round(time.monotonic()-start,3),"errors":errors}),flush=True)
if servers:servers[0].should_exit=True
t.join(timeout=8)
assert not t.is_alive(), "Application shutdown did not finish"
if xray.core.started:xray.core.stop()
assert started == ("--reserve" in sys.argv or not hasattr(operations,"_node_operations"))
if started:assert http_status==200

if "--no-collision" in sys.argv:assert not errors
