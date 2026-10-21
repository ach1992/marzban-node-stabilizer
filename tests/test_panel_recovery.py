import ast
import copy
import hashlib
import json
import logging
import ssl
import tempfile
import threading
import time
import types
import unittest
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from unittest.mock import Mock

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.poolmanager import PoolManager

from panel_support import ROOT
logging.disable(logging.CRITICAL)

class Config(dict):
    def to_json(self, **kwargs):
        return json.dumps(self, **kwargs)
    def include_db_users(self):
        return copy.deepcopy(self)

class ReadyTimeout(Exception):
    pass

def load_node(variant="candidate", clock=None):
    text = (ROOT / variant / "app/xray/node.py").read_text()
    tree = ast.parse(text)
    wanted = {"string_to_temp_file", "SANIgnoringAdaptor", "NodeAPIError", "ReSTXRayNode"}
    tree.body = [n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in wanted]
    now = clock if clock is not None else [1000.0]
    ns = dict(
        ssl=ssl, tempfile=tempfile, threading=threading, time=types.SimpleNamespace(
            monotonic=lambda:now[0], time=lambda:now[0], sleep=time.sleep),
        requests=requests, HTTPAdapter=HTTPAdapter, PoolManager=PoolManager,
        deepcopy=copy.deepcopy, contextmanager=contextmanager, List=list,
        XRayConfig=Config, XRayAPI=lambda **kwargs:types.SimpleNamespace(_channel=object()),
        grpc=types.SimpleNamespace(
            channel_ready_future=lambda channel:types.SimpleNamespace(result=lambda **kwargs:None),
            FutureTimeoutError=ReadyTimeout),
        logger=logging.getLogger("test.node"),
    )
    exec(compile(tree, str(ROOT/variant/"app/xray/node.py"), "exec"),ns)
    return ns,now

def node_probe(ns):
    node = ns["ReSTXRayNode"].__new__(ns["ReSTXRayNode"])
    node.address="mock-node"
    node.port=62050
    node.api_port=62051
    node._session_id="session-a"
    node._node_cert="test-certificate"
    node._api=None
    node._started=False
    node._connected_cache=None
    node._connected_cache_time=0
    node._started_cache=None
    node._started_cache_time=0
    node._cache_ttl=10
    node._error_cache_ttl=2
    node._state_lock=threading.RLock()
    node._status_lock=threading.Lock()
    node._lifecycle_lock=threading.RLock()
    node._state_generation=0
    node.calls=[]
    node.backend_started=False
    def request(path,timeout,**params):
        node.calls.append((path,params))
        if path in ("/start","/restart"):
            node.backend_started=True
        return {"started":node.backend_started,"session_id":"session-b","version":"test"}
    node.make_request=request
    return node

def load_operations():
    path=ROOT/"candidate/app/xray/operations.py"
    tree=ast.parse(path.read_text())
    tree.body=[n for n in tree.body if not isinstance(n,(ast.Import,ast.ImportFrom))]
    status=types.SimpleNamespace(disabled="disabled",connecting="connecting",connected="connected",error="error")
    dbnode=types.SimpleNamespace(id=1,name="fixture",status="connected",xray_version="v-installed")
    records=[]
    @contextmanager
    def getdb():
        yield types.SimpleNamespace(rollback=lambda:None)
    crud=types.SimpleNamespace(
        get_node_by_id=lambda db,node_id:dbnode if node_id==1 else None,
        update_node_status=lambda db,node,state,message=None,version=None:records.append((state,message,version)),
    )
    xray=types.SimpleNamespace(nodes={}, config=Config(revision="current"))
    ns=dict(
        lru_cache=lru_cache, deepcopy=copy.deepcopy, Lock=threading.Lock, Thread=threading.Thread,
        TYPE_CHECKING=False, logger=logging.getLogger("test.operations"), xray=xray,
        GetDB=getdb,crud=crud,NodeStatus=status,UserResponse=Mock(),
        threaded_function=lambda function:function,
        XRayNode=Mock(), XRayAPI=object,Account=object,XTLSFlows=Mock(),SQLAlchemyError=type("SQLAlchemyError",(Exception,),{}),
    )
    exec(compile(tree,str(path),"exec"),ns)
    return ns,dbnode,records

def wait_for(predicate,seconds=3):
    deadline=time.monotonic()+seconds
    while not predicate():
        if time.monotonic()>=deadline:
            raise AssertionError("Timed out waiting for worker")
        time.sleep(.002)

class CacheTests(unittest.TestCase):
    def setUp(self):
        self.ns,self.clock=load_node()
        self.node=node_probe(self.ns)
    def test_positive_status_is_cached_for_ten_seconds(self):
        self.node.backend_started=True
        self.assertTrue(self.node.started)
        self.clock[0]+=9
        self.assertTrue(self.node.started)
        self.assertEqual(len(self.node.calls),1)
        self.clock[0]+=1
        self.assertTrue(self.node.started)
        self.assertEqual(len(self.node.calls),2)
    def test_negative_status_expires_after_two_seconds(self):
        self.assertFalse(self.node.started)
        self.node.backend_started=True
        self.clock[0]+=1
        self.assertFalse(self.node.started)
        self.clock[0]+=1
        self.assertTrue(self.node.started)
    def test_zero_ttl_is_supported(self):
        self.node._error_cache_ttl=0
        self.assertFalse(self.node.started)
        self.node.backend_started=True
        self.assertTrue(self.node.started)
    def test_clock_uses_monotonic_not_wall_time(self):
        self.node.backend_started=True
        self.assertTrue(self.node.started)
        self.ns["time"].time=lambda:-100000
        self.clock[0]+=10
        self.node.backend_started=False
        self.assertFalse(self.node.started)
    def test_expiry_starts_after_network_response(self):
        request=self.node.make_request
        def delayed(*args,**kwargs):
            self.clock[0]+=25
            return request(*args,**kwargs)
        self.node.make_request=delayed
        self.assertFalse(self.node.started)
        self.assertEqual(self.node._started_cache_time,self.clock[0])
        self.node.started
        self.assertEqual(len(self.node.calls),1)
    def test_successful_start_replaces_cached_false_immediately(self):
        self.assertFalse(self.node.started)
        self.node.start(Config(inbounds=[]))
        self.assertTrue(self.node.started)
        self.assertTrue(self.node.connected)
        self.assertEqual([p for p,k in self.node.calls],["/","/ping","/start"])
    def test_successful_restart_replaces_cached_false(self):
        self.assertFalse(self.node.started)
        self.node.restart(Config(inbounds=[]))
        self.assertTrue(self.node.started)
    def test_failed_start_invalidates_cached_true(self):
        self.node._update_status_cache(True,True)
        def fail(*args,**kwargs):
            raise self.ns["NodeAPIError"](500,"test failure")
        self.node.make_request=fail
        with self.assertRaises(self.ns["NodeAPIError"]):
            self.node.start(Config(inbounds=[]))
        self.assertFalse(self.node.started)
        self.assertIsNone(self.node._api)
    def test_readiness_timeout_is_not_reported_as_success(self):
        def timeout(**kwargs):
            raise ReadyTimeout()
        self.ns["grpc"].channel_ready_future=lambda channel:types.SimpleNamespace(result=timeout)
        with self.assertRaises(ConnectionError):
            self.node.start(Config(inbounds=[]))
        self.assertIsNone(self.node._started_cache)
        self.assertFalse(self.node._started)
        self.assertIsNone(self.node._api)
    def test_disconnect_clears_state_even_when_request_fails(self):
        self.node._update_status_cache(True,True)
        self.node.make_request=Mock(side_effect=self.ns["NodeAPIError"](403,"stale session"))
        with self.assertRaises(self.ns["NodeAPIError"]):
            self.node.disconnect()
        self.assertIsNone(self.node._session_id)
        self.assertFalse(self.node.connected)
        self.assertFalse(self.node.started)
        self.assertIsNone(self.node._api)
    def test_already_started_fallback_does_not_deadlock(self):
        request=self.node.make_request
        def already(path,*args,**kwargs):
            if path=="/start":
                raise self.ns["NodeAPIError"](400,"Xray is started already")
            return request(path,*args,**kwargs)
        self.node.make_request=already
        thread=threading.Thread(target=self.node.start,args=(Config(inbounds=[]),),daemon=True)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.node.started)
        self.assertIn("/restart",[p for p,k in self.node.calls])
    def test_stale_failed_probe_cannot_overwrite_new_success(self):
        entered=threading.Event()
        release=threading.Event()
        result=[]
        def late(*args,**kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            raise self.ns["NodeAPIError"](403,"old session")
        self.node.make_request=late
        t=threading.Thread(target=lambda:result.append(self.node.started))
        t.start()
        self.assertTrue(entered.wait(2))
        self.node._session_id="new-session"
        self.node._update_status_cache(True,True)
        release.set()
        t.join(2)
        self.assertEqual(result,[True])
        self.assertTrue(self.node._started_cache)
    def test_concurrent_cache_misses_issue_one_probe(self):
        entered=threading.Event()
        release=threading.Event()
        request=self.node.make_request
        def delayed(*args,**kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return request(*args,**kwargs)
        self.node.make_request=delayed
        threads=[threading.Thread(target=lambda:self.node.started) for _ in range(20)]
        for t in threads:t.start()
        self.assertTrue(entered.wait(2))
        release.set()
        for t in threads:t.join(2)
        self.assertEqual(len(self.node.calls),1)
    def test_tls_preparation_does_not_mutate_shared_config(self):
        with tempfile.TemporaryDirectory() as d:
            cert=Path(d)/"cert"; cert.write_text("certificate-line\n")
            key=Path(d)/"key"; key.write_text("key-line\n")
            config=Config(inbounds=[{"streamSettings":{"tlsSettings":{"certificates":[{
                "certificateFile":str(cert),"keyFile":str(key)}]}}}])
            before=copy.deepcopy(config)
            self.node.start(config)
            self.assertEqual(config,before)
            sent=json.loads(next(k["config"] for p,k in self.node.calls if p=="/start"))
            prepared=sent["inbounds"][0]["streamSettings"]["tlsSettings"]["certificates"][0]
            self.assertEqual(prepared,{"certificate":["certificate-line"],"key":["key-line"]})

    def test_health_recovery_updates_150_second_failed_status_without_waiting(self):
        operations,dbnode,records=load_operations()
        self.node._cache_ttl=150
        self.node._error_cache_ttl=150
        operations["xray"].nodes[1]=self.node
        operations["xray"].core=types.SimpleNamespace(started=True)
        operations["xray"].operations=types.SimpleNamespace(
            restart_node=operations["restart_node"],connect_node=operations["connect_node"])
        self.assertFalse(self.node.started)
        health_tree=ast.parse((ROOT/"candidate/app/jobs/0_xray_core.py").read_text())
        health_tree.body=[n for n in health_tree.body if isinstance(n,ast.FunctionDef) and n.name=="core_health_check"]
        env={"xray":operations["xray"],"xray_exc":types.SimpleNamespace(XrayError=RuntimeError)}
        exec(compile(health_tree,"<health-check>","exec"),env)
        env["core_health_check"]()
        wait_for(lambda:not operations["_node_operations"])
        self.assertTrue(self.node.started)
        self.assertEqual(self.node._error_cache_ttl,150)
        self.assertEqual([path for path,params in self.node.calls].count("/restart"),1)

    def test_original_failure_is_reproduced(self):
        ns,clock=load_node("baseline")
        node=node_probe(ns)
        node._cache_ttl=150
        self.assertFalse(node.started)
        node.start(Config(inbounds=[]))
        clock[0]+=1
        self.assertFalse(node.started)
        clock[0]+=150
        self.assertTrue(node.started)

class OperationTests(unittest.TestCase):
    def setUp(self):
        self.ns,self.dbnode,self.records=load_operations()
    def wait_idle(self):
        wait_for(lambda:not self.ns["_node_operations"])
    def test_health_ticks_do_not_queue_restarts_during_active_operation(self):
        entered=threading.Event(); release=threading.Event(); calls=[]
        def perform(*args):
            calls.append(args)
            entered.set()
            self.assertTrue(release.wait(2))
        self.ns["_perform_node_operation"]=perform
        self.ns["restart_node"](1,Config(revision="A"))
        self.assertTrue(entered.wait(2))
        for _ in range(100):
            self.ns["restart_node"](1,Config(revision="A"),health_check=True)
            self.ns["connect_node"](1,Config(revision="A"),health_check=True)
        release.set(); self.wait_idle()
        self.assertEqual(len(calls),1)
    def test_latest_explicit_config_is_preserved(self):
        entered=threading.Event();release=threading.Event();seen=[]
        def perform(node_id,action,config):
            seen.append(config["revision"])
            if len(seen)==1:
                entered.set();self.assertTrue(release.wait(2))
        self.ns["_perform_node_operation"]=perform
        self.ns["connect_node"](1,Config(revision="A"))
        self.assertTrue(entered.wait(2))
        self.ns["restart_node"](1,Config(revision="B"))
        c=Config(revision="C")
        self.ns["restart_node"](1,c)
        c["revision"]="mutated-after-request"
        self.ns["restart_node"](1,Config(revision="old-health-snapshot"),health_check=True)
        release.set();self.wait_idle()
        self.assertEqual(seen,["A","C"])
    def test_different_nodes_operate_independently(self):
        entered=[threading.Event(),threading.Event()];release=threading.Event()
        def perform(node_id,*args):
            entered[node_id-1].set();self.assertTrue(release.wait(2))
        self.ns["_perform_node_operation"]=perform
        self.ns["restart_node"](1)
        self.ns["restart_node"](2)
        try:
            self.assertTrue(entered[0].wait(2));self.assertTrue(entered[1].wait(2))
        finally:release.set()
        self.wait_idle()
    def test_connect_and_restart_share_one_inflight_guard(self):
        gate=threading.Barrier(21);guard=threading.Lock();counts=[0,0]
        def perform(*args):
            with guard:
                counts[0]+=1;counts[1]=max(counts)
            time.sleep(.03)
            with guard:counts[0]-=1
        self.ns["_perform_node_operation"]=perform
        def request(i):
            gate.wait()
            self.ns["connect_node" if i%2 else "restart_node"](1,health_check=True)
        ts=[threading.Thread(target=request,args=(i,)) for i in range(20)]
        for t in ts:t.start()
        gate.wait()
        for t in ts:t.join(2)
        self.wait_idle()
        self.assertEqual(counts[1],1)
    def test_worker_failure_releases_guard_for_future_recovery(self):
        self.ns["_perform_node_operation"]=Mock(side_effect=RuntimeError("fixture"))
        self.ns["restart_node"](1)
        self.wait_idle()
        self.ns["restart_node"](1)
        self.wait_idle()
        self.assertEqual(self.ns["_perform_node_operation"].call_count,2)
    def test_thread_start_failure_releases_guard(self):
        self.ns["Thread"]=lambda **kwargs:types.SimpleNamespace(start=Mock(side_effect=RuntimeError("fixture")))
        with self.assertRaises(RuntimeError):self.ns["restart_node"](1)
        self.assertEqual(self.ns["_node_operations"],{})
    def fake_node(self,connected=True):
        return types.SimpleNamespace(connected=connected,start=Mock(),restart=Mock(),get_version=Mock(return_value="v1"),disconnect=Mock())
    def test_disconnected_restart_uses_connect_in_same_worker(self):
        old=self.fake_node(False);new=self.fake_node(True)
        self.ns["xray"].nodes[1]=old
        self.ns["add_node"]=lambda dbnode:self.ns["xray"].nodes.setdefault(1,new)
        def replace(dbnode):
            self.ns["xray"].nodes[1]=new
            return new
        self.ns["add_node"]=replace
        self.ns["restart_node"](1,Config(revision="A"))
        self.wait_idle()
        new.start.assert_called_once()
        new.restart.assert_not_called()
        self.assertEqual(self.records[-1][0],"connected")
    def test_old_operation_cannot_disconnect_replacement_node(self):
        old=self.fake_node();new=self.fake_node()
        self.ns["xray"].nodes[1]=old
        def fail(config):
            self.ns["xray"].nodes[1]=new
            raise RuntimeError("old operation failed")
        old.restart=fail
        self.ns["restart_node"](1)
        self.wait_idle()
        old.disconnect.assert_not_called()
        new.disconnect.assert_not_called()
        self.assertEqual(self.records,[])
    def test_restart_success_clears_previous_error_status(self):
        node=self.fake_node();self.ns["xray"].nodes[1]=node
        self.dbnode.status="error"
        self.ns["restart_node"](1)
        self.wait_idle()
        node.restart.assert_called_once()
        self.assertEqual(self.records[-1],("connected",None,"v-installed"))
    def test_missing_and_disabled_nodes_are_not_started(self):
        node=self.fake_node();self.ns["xray"].nodes[1]=node
        self.dbnode.status="disabled"
        self.ns["restart_node"](1);self.ns["restart_node"](999)
        self.wait_idle()
        node.restart.assert_not_called()
    def test_none_config_is_loaded_from_current_panel_state(self):
        node=self.fake_node();self.ns["xray"].nodes[1]=node
        self.ns["restart_node"](1)
        self.wait_idle()
        self.assertEqual(node.restart.call_args.args[0]["revision"],"current")

class PreservationTests(unittest.TestCase):
    def test_unrelated_source_functions_are_unchanged(self):
        changed={"app/xray/node.py":{"__init__","connected","started","connect","disconnect","start","restart"},
                 "app/xray/operations.py":{"connect_node","restart_node"},
                 "app/routers/core.py":{"restart_core","modify_core_config"}}
        for path,allowed in changed.items():
            a=ast.parse((ROOT/"baseline"/path).read_text())
            b=ast.parse((ROOT/"candidate"/path).read_text())
            if path.endswith("node.py"):
                a=next(n for n in a.body if isinstance(n,ast.ClassDef) and n.name=="ReSTXRayNode")
                b=next(n for n in b.body if isinstance(n,ast.ClassDef) and n.name=="ReSTXRayNode")
            am={n.name:ast.dump(n) for n in a.body if isinstance(n,(ast.ClassDef,ast.FunctionDef))}
            bm={n.name:ast.dump(n) for n in b.body if isinstance(n,(ast.ClassDef,ast.FunctionDef))}
            for name in am.keys()-allowed:
                self.assertEqual(am[name],bm[name],(path,name))
    def test_only_health_calls_get_health_flag(self):
        s=(ROOT/"candidate/app/jobs/0_xray_core.py").read_text()
        before,after=s.split('@app.on_event("startup")',1)
        self.assertEqual(before.count("health_check=True"),2)
        self.assertNotIn("health_check=True",after)


def load_core_router(operations, directory):
    path=ROOT/"candidate/app/routers/core.py"
    tree=ast.parse(path.read_text())
    selected=[]
    for n in tree.body:
        if isinstance(n,ast.FunctionDef) and n.name in ("restart_core","modify_core_config"):
            n.decorator_list=[]
            selected.append(n)
    tree.body=selected
    def build(payload,api_port):
        config=Config(payload)
        config.api_port=api_port
        return config
    xray=operations["xray"]
    xray.config.api_port=10001
    xray.operations=types.SimpleNamespace(
        restart_node=operations["restart_node"],connect_node=operations["connect_node"],
        get_node_ids=operations["get_node_ids"])
    xray.core=types.SimpleNamespace(restart=Mock())
    xray.hosts=types.SimpleNamespace(update=Mock())
    ns=dict(xray=xray,XRayConfig=build,XRAY_JSON=str(Path(directory)/"core.json"),
            Admin=types.SimpleNamespace(check_sudo_admin=object()),
            Depends=lambda value:None,HTTPException=RuntimeError,
            _core_config_lock=threading.Lock(),json=json)
    exec(compile(tree,str(path),"exec"),ns)
    return ns

class CoreDispatchTests(unittest.TestCase):
    def test_core_change_reaches_admitted_node_absent_during_constructor(self):
        for replacing in (False,True):
            with self.subTest(replacing=replacing):
                operations,dbnode,records=load_operations()
                dbnode.address="fixture";dbnode.port=62050;dbnode.api_port=62051
                dbnode.usage_coefficient=1
                entered=threading.Event();release=threading.Event();seen=[]
                node=types.SimpleNamespace(
                    connected=True,start=lambda config:seen.append(config["revision"]),
                    restart=lambda config:seen.append(config["revision"]),
                    get_version=lambda:"v1",disconnect=Mock())
                def construct(**kwargs):
                    entered.set()
                    release.wait(2)
                    return node
                operations["get_tls"]=lambda:{"key":"fixture","certificate":"fixture"}
                operations["XRayNode"]=construct
                if replacing:
                    operations["xray"].nodes[1]=types.SimpleNamespace(connected=False,disconnect=Mock())
                with tempfile.TemporaryDirectory() as directory:
                    core=load_core_router(operations,directory)
                    operations["connect_node"](1,Config(revision="A"))
                    self.assertTrue(entered.wait(2))
                    try:
                        self.assertNotIn(1,operations["xray"].nodes)
                        self.assertIn(1,operations["get_node_ids"]())
                        core["modify_core_config"]({"revision":"B"})
                    finally:release.set()
                    wait_for(lambda:not operations["_node_operations"])
                    self.assertEqual(seen,["A","B"])
                    self.assertIs(operations["xray"].nodes[1],node)
                    self.assertEqual(records[-1][0],"connected")

    def test_config_reaches_node_while_old_connect_is_inflight_and_disconnected(self):
        operations,dbnode,records=load_operations()
        operations["xray"].nodes[1]=types.SimpleNamespace(connected=False)
        entered=threading.Event();release=threading.Event();seen=[];active=[0,0]
        def perform(node_id,action,config):
            active[0]+=1;active[1]=max(active)
            seen.append(config["revision"])
            if len(seen)==1:
                entered.set()
                release.wait(2)
            active[0]-=1
        operations["_perform_node_operation"]=perform
        with tempfile.TemporaryDirectory() as directory:
            core=load_core_router(operations,directory)
            operations["connect_node"](1,Config(revision="A"))
            self.assertTrue(entered.wait(2))
            try:
                core["modify_core_config"]({"revision":"B"})
                self.assertFalse(operations["xray"].nodes[1].connected)
            finally:release.set()
            wait_for(lambda:not operations["_node_operations"])
            self.assertEqual(seen,["A","B"])
            self.assertEqual(active[1],1)
    def test_already_pending_newer_explicit_config_is_preserved(self):
        operations,dbnode,records=load_operations()
        operations["xray"].nodes[1]=types.SimpleNamespace(connected=False)
        entered=threading.Event();release=threading.Event();seen=[]
        def perform(node_id,action,config):
            seen.append(config["revision"])
            if len(seen)==1:
                entered.set();release.wait(2)
        operations["_perform_node_operation"]=perform
        with tempfile.TemporaryDirectory() as directory:
            core=load_core_router(operations,directory)
            operations["connect_node"](1,Config(revision="A"))
            self.assertTrue(entered.wait(2))
            try:
                core["modify_core_config"]({"revision":"B"})
                core["modify_core_config"]({"revision":"C"})
                operations["restart_node"](1,Config(revision="old-health"),health_check=True)
            finally:release.set()
            wait_for(lambda:not operations["_node_operations"])
            self.assertEqual(seen,["A","C"])
    def test_restart_core_also_dispatches_disconnected_nodes(self):
        operations,dbnode,records=load_operations()
        operations["xray"].nodes[1]=types.SimpleNamespace(connected=False)
        calls=[]
        operations["restart_node"]=lambda node_id,config:calls.append((node_id,config))
        with tempfile.TemporaryDirectory() as directory:
            core=load_core_router(operations,directory)
            core["restart_core"]()
        self.assertEqual([node_id for node_id,config in calls],[1])
        self.assertEqual(calls[0][1]["revision"],"current")
    def test_concurrent_core_changes_cannot_enqueue_older_snapshot_after_newer_one(self):
        operations,dbnode,records=load_operations()
        operations["xray"].nodes[1]=types.SimpleNamespace(connected=False)
        calls=[];entered=threading.Event();release=threading.Event();b_started=threading.Event()
        errors=[]
        operations["restart_node"]=lambda node_id,config:calls.append(config["revision"])
        with tempfile.TemporaryDirectory() as directory:
            core=load_core_router(operations,directory)
            def restart(config):
                if config["revision"]=="A":
                    entered.set();release.wait(2)
            operations["xray"].core.restart=restart
            def change(revision):
                try:
                    if revision=="B":b_started.set()
                    core["modify_core_config"]({"revision":revision})
                except Exception as exc:errors.append(exc)
            a=threading.Thread(target=change,args=("A",))
            b=threading.Thread(target=change,args=("B",))
            a.start()
            self.assertTrue(entered.wait(2))
            b.start();self.assertTrue(b_started.wait(2))
            time.sleep(.03)
            self.assertEqual(operations["xray"].config["revision"],"A")
            release.set()
            a.join(2);b.join(2)
            self.assertFalse(a.is_alive() or b.is_alive())
            self.assertEqual(errors,[])
            self.assertEqual(calls,["A","B"])
            self.assertEqual(operations["xray"].config["revision"],"B")

if __name__=="__main__":
    unittest.main(verbosity=2)
