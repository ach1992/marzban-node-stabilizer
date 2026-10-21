import importlib.util
import json
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_panel_recovery import Config, load_node, node_probe

class HTTPSFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.cert=Path(cls.temp.name)/"cert.pem"
        cls.key=Path(cls.temp.name)/"key.pem"
        subprocess.run([
            "openssl","req","-x509","-newkey","rsa:2048","-nodes",
            "-keyout",str(cls.key),"-out",str(cls.cert),"-days","1",
            "-subj","/CN=Gozargah","-addext","subjectAltName=DNS:Gozargah,DNS:localhost,IP:127.0.0.1",
        ],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()
    def setUp(self):
        self.state={"session":None,"started":False,"calls":[]}
        state=self.state
        class Handler(BaseHTTPRequestHandler):
            protocol_version="HTTP/1.1"
            def log_message(self,*args): pass
            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError):
                    # Certificate discovery completes TLS without sending HTTP.
                    if getattr(self, "requestline", ""):
                        raise
            def do_POST(self):
                params=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state["calls"].append(self.path)
                status=200
                if self.path=="/connect":
                    state["session"]="session-"+str(len(state["calls"]))
                    result={"session_id":state["session"]}
                elif params.get("session_id")!=state["session"]:
                    status=403;result={"detail":"Session is invalid"}
                elif self.path=="/disconnect":
                    state["session"]=None;state["started"]=False
                    result={}
                elif self.path in ("/start","/restart"):
                    if self.path=="/start" and state["started"]:
                        status=400;result={"detail":"Xray is started already"}
                    else:
                        state["started"]=True
                        state["config"]=json.loads(params["config"])
                        result={}
                else:
                    result={"started":state["started"],"version":"fixture"}
                payload=json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        self.server.daemon_threads=True
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert),str(self.key))
        self.server.socket=context.wrap_socket(self.server.socket,server_side=True)
        self.thread=threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.01),daemon=True)
        self.thread.start()
        self.ns,self.clock=load_node()
        self.ns["ssl"]=ssl
        self.node=self.ns["ReSTXRayNode"](
            "127.0.0.1",self.server.server_port,62051,
            self.key.read_text(),self.cert.read_text())
    def tearDown(self):
        self.node.session.close()
        if self.node._api is not None and hasattr(self.node._api._channel,"close"):
            self.node._api._channel.close()
        for name in ("_certfile","_keyfile","_node_certfile"):
            f=getattr(self.node,name,None)
            if f:f.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
    def test_constructor_retains_existing_150_second_cache_defaults(self):
        self.assertEqual(self.node._cache_ttl,150)
        self.assertEqual(self.node._error_cache_ttl,150)
    def test_real_https_connect_start_restart_disconnect(self):
        self.node.connect()
        self.assertTrue(self.node.connected)
        self.assertFalse(self.node.started)
        self.node.start(Config(revision="A"))
        self.assertTrue(self.node.started)
        self.node.restart(Config(revision="B"))
        self.assertTrue(self.node.started)
        self.assertEqual(self.state["config"]["revision"],"B")
        self.node.disconnect()
        self.assertFalse(self.node.connected)
        self.assertFalse(self.node.started)
        self.assertEqual(self.state["calls"].count("/connect"),1)
    def test_real_https_stale_session_is_detected(self):
        self.node.connect()
        self.state["session"]="different-session"
        self.clock[0]+=self.node._cache_ttl
        self.assertFalse(self.node.connected)
        self.node.disconnect() if self.state["session"]==self.node._session_id else None
    def test_real_https_already_started_falls_back_to_restart(self):
        self.node.connect()
        self.state["started"]=True
        self.node.start(Config(revision="fallback"))
        self.assertTrue(self.node.started)
        self.assertEqual(self.state["config"]["revision"],"fallback")
        self.assertEqual(self.state["calls"].count("/restart"),1)
    def test_real_https_cache_hit_cost(self):
        self.node.connect()
        self.node.start(Config(revision="A"))
        self.clock[0]+=self.node._cache_ttl
        self.assertTrue(self.node.connected)
        self.assertTrue(self.node.started)
        count=len(self.state["calls"])
        began=time.perf_counter()
        for _ in range(1000):
            self.assertTrue(self.node.connected and self.node.started)
        elapsed=time.perf_counter()-began
        self.assertEqual(len(self.state["calls"]),count)
        print(json.dumps({"https_cache_reads":2000,"extra_requests":0,"elapsed_seconds":round(elapsed,6)}))
    @unittest.skipUnless(importlib.util.find_spec("grpc") and importlib.util.find_spec("xray_api"),
                         "Exact Marzban image supplies grpc and xray_api")
    def test_actual_xray_api_tls_channel_readiness(self):
        import grpc
        from xray_api import XRay
        server=grpc.server(ThreadPoolExecutor(max_workers=2))
        credentials=grpc.ssl_server_credentials(((self.key.read_bytes(),self.cert.read_bytes()),))
        port=server.add_secure_port("127.0.0.1:0",credentials)
        server.start()
        try:
            self.ns["grpc"]=grpc
            self.ns["XRayAPI"]=XRay
            self.node.api_port=port
            self.node.connect()
            self.node.start(Config(revision="real-grpc-channel"))
            self.assertTrue(self.node.started)
        finally:
            if self.node._api is not None:self.node._api._channel.close()
            server.stop(0).wait(2)

class WorkloadTests(unittest.TestCase):
    def test_fourteen_nodes_keep_management_reads_cached(self):
        ns,clock=load_node()
        nodes=[node_probe(ns) for _ in range(14)]
        for n in nodes:n.backend_started=True
        began=time.perf_counter()
        for _ in range(1000):
            for node in nodes:
                self.assertTrue(node.connected and node.started)
        elapsed=time.perf_counter()-began
        calls=sum(len(n.calls) for n in nodes)
        self.assertEqual(calls,28)
        print(json.dumps({"simulated_nodes":14,"status_reads":28000,"mock_requests":calls,
                          "elapsed_seconds":round(elapsed,6)}))

if __name__=="__main__":
    unittest.main(verbosity=2)
