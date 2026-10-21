#!/usr/bin/env python3
"""Recoverable Panel-role deployment; no periodic service or watchdog."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

import compose_mount
from patch_panel import build_tree, load_spec

ROLE = "marzban-panel-stabilizer"
FILES = tuple(load_spec()["files"])
PROBE = r'''
import http.client,json,os,socket,ssl,urllib.request
from pathlib import Path
from sqlalchemy import create_engine,text
from config import SQLALCHEMY_DATABASE_URL,UVICORN_HOST,UVICORN_PORT,UVICORN_UDS,UVICORN_SSL_CERTFILE,UVICORN_SSL_KEYFILE
tls=bool(UVICORN_SSL_CERTFILE and UVICORN_SSL_KEYFILE)
ready=False
try:
 if UVICORN_UDS:
  c=http.client.HTTPConnection("localhost",timeout=3)
  sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);sock.settimeout(3);sock.connect(UVICORN_UDS)
  c.sock=ssl._create_unverified_context().wrap_socket(sock,server_hostname="localhost") if tls else sock
  c.request("GET","/",headers={"Connection":"close"});r=c.getresponse();ready=200<=r.status<400;c.close()
 else:
  host=UVICORN_HOST
  if host in ("0.0.0.0",""):host="127.0.0.1"
  if host=="::":host="[::1]"
  if ":" in host and not host.startswith("["):host="["+host+"]"
  req=urllib.request.Request(("https" if tls else "http")+"://"+host+":"+str(UVICORN_PORT)+"/",headers={"Connection":"close"})
  with urllib.request.urlopen(req,context=ssl._create_unverified_context(),timeout=3) as r:ready=200<=r.status<400
except Exception:pass
with create_engine(SQLALCHEMY_DATABASE_URL).connect() as c:
 nodes=[dict(r._mapping) for r in c.execute(text("SELECT id,name,status,xray_version FROM nodes ORDER BY id"))]
 users=c.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
print(json.dumps({"http_ready":ready,"nodes":nodes,"users_count":users}))
'''


def emit(event, **details):
    print(json.dumps({"role": ROLE, "event": event,
                      "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **details}), flush=True)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    previous_owner = (path.stat().st_uid, path.stat().st_gid) if path.exists() else None
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
        if previous_owner is not None and os.geteuid() == 0:
            os.chown(name, *previous_owner)
        os.replace(name, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_json(path, value):
    atomic_write(path, json.dumps(value, indent=2) + "\n")


class RuntimeIdentityError(RuntimeError):
    pass


class Panel:
    def __init__(self, args):
        self.args = args
        self.compose = Path(args.compose_file).resolve()
        self.service = args.service
        self.root = Path(args.patch_dir).resolve()
        self.state_file = self.root / "state.json"
        self.journal = self.root / "transaction.json"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.service):
            raise ValueError("Unsupported Compose service name")
        self.docker = ["docker", "compose"]
        self.run(self.docker + ["version"], timeout=10)

    def run(self, command, timeout=30):
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            # Full diagnostic output stays readable to the invoking operator, mode 0600.
            fd, log_name = tempfile.mkstemp(prefix=ROLE + "-error-", suffix=".log")
            with os.fdopen(fd, "w") as handle:
                handle.write(result.stderr + "\n" + result.stdout)
            raise RuntimeError(f"{command[0]} failed (exit={result.returncode}); details={log_name}")
        return result.stdout

    def compose_run(self, file, *args, extra=None, timeout=30):
        command = self.docker + ["--project-directory", str(self.compose.parent), "-f", str(file)]
        if extra:
            command += ["-f", str(extra)]
        return self.run(command + list(args), timeout=timeout)

    def model(self, file):
        return json.loads(self.compose_run(file, "config", "--format", "json"))

    def inspect(self):
        if self.args.container:
            identity = self.args.container
        else:
            identities = self.compose_run(self.compose, "ps", "-a", "-q", self.service).split()
            if len(identities) != 1:
                raise RuntimeError("Expected exactly one existing Panel container for selected service")
            identity = identities[0]
        value = json.loads(self.run(["docker", "inspect", identity]))[0]
        labels = value["Config"].get("Labels") or {}
        model = self.model(self.compose)
        if labels.get("com.docker.compose.service") != self.service or labels.get("com.docker.compose.project") != model["name"]:
            raise RuntimeError("Container does not belong to the selected Compose project/service")
        return value

    def probe(self, container):
        return json.loads(self.run(["docker", "exec", container["Id"], "python", "-B", "-c", PROBE], timeout=15))

    def runtime_hashes(self, container):
        command = "import hashlib,json;from pathlib import Path;paths=" + repr(FILES) + ";print(json.dumps({p:hashlib.sha256((Path('/code')/p).read_bytes()).hexdigest() for p in paths}))"
        return json.loads(self.run(["docker", "exec", container["Id"], "python", "-B", "-c", command]))

    def mounts(self, text):
        result = {}
        for file in FILES:
            mounts = compose_mount._target_mounts(text.splitlines(), self.service, "/code/" + file)
            result[file] = mounts[0] if mounts else None
        return result

    def state(self):
        if self.state_file.exists():
            state = json.loads(self.state_file.read_text())
            if state.get("role") != ROLE or state.get("compose") != str(self.compose) or state.get("service") != self.service:
                raise RuntimeError("Patch state belongs to a different installation")
            return state
        return None

    def require_owned(self, text, state):
        mounts = self.mounts(text)
        if not state:
            if any(mounts.values()):
                raise RuntimeError("Foreign Panel overrides found; no changes made. Only --adopt-reviewed-patch can adopt the exact reviewed manual bundle")
            return
        for file, mount in mounts.items():
            item = state["files"][file]
            if mount is None or mount.source != item["host"] or mount.options != "ro":
                raise RuntimeError("Foreign or changed Panel mount: " + file)
            if not Path(item["host"]).is_file() or sha(item["host"]) != item["patched_sha256"]:
                raise RuntimeError("Modified managed Panel source: " + file)

    def original_mounts(self, text):
        result = {}
        for file, mount in self.mounts(text).items():
            result[file] = {"source": mount.source, "options": mount.options} if mount else None
        return result

    def edit(self, text, replacements):
        for file, value in replacements.items():
            target = "/code/" + file
            if value is None:
                text = compose_mount.remove_mount(text, self.service, target)
            else:
                text = compose_mount.add_mount(text, self.service, value["source"], target, value["options"])
        return text

    def verify_model_delta(self, before, after):
        def unrelated(value):
            value = copy.deepcopy(value)
            service = value["services"][self.service]
            service["volumes"] = [m for m in service.get("volumes", []) if m.get("target") not in {"/code/" + f for f in FILES}]
            return value
        if unrelated(before) != unrelated(after):
            raise RuntimeError("Compose changes exceed the five Panel file mounts")

    def extract(self, image, destination):
        temp = self.run(["docker", "create", "--network", "none", image]).strip()
        try:
            for file in FILES:
                dest = destination / file
                dest.parent.mkdir(parents=True, exist_ok=True)
                self.run(["docker", "cp", temp + ":/code/" + file, str(dest)])
                if dest.is_symlink() or not dest.is_file():
                    raise RuntimeError("Image source is not a regular file: " + file)
        finally:
            self.run(["docker", "rm", temp])

    def recreate(self, image, transaction_dir):
        # Pin this transaction to the exact already-installed image, even if its local tag moves.
        override = transaction_dir / "image-pin.json"
        save_json(override, {"services": {self.service: {"image": image}}})
        emit("recreate_begin", service=self.service, image=image)
        self.compose_run(self.compose, "up", "-d", "--no-deps", "--force-recreate",
                         "--pull", "never", "--no-build", self.service, extra=override, timeout=120)

    def wait_healthy(self, baseline, image, expected=None, stable_seconds=0):
        start = time.monotonic()
        deadline = start + self.args.wait_seconds
        ready_deadline = min(deadline, start + self.args.ready_seconds)
        ready_seen = False
        stable = None
        wanted = {n["id"] for n in baseline["nodes"] if n["status"] == "connected"}
        last_error = None
        last_missing = sorted(wanted)
        while time.monotonic() < deadline:
            try:
                container = self.inspect()
                if container["Image"] != image:
                    raise RuntimeIdentityError("Recreated image identity mismatch")
                if not container["State"]["Running"] or container["RestartCount"]:
                    raise RuntimeIdentityError("Panel is stopped or crash-restarting")
                probe = self.probe(container)
                ready_seen = ready_seen or probe["http_ready"]
                last_missing = sorted(wanted - {n["id"] for n in probe["nodes"] if n["status"] == "connected"})
                if probe["http_ready"] and not last_missing:
                    if expected is not None and self.runtime_hashes(container) != expected:
                        raise RuntimeIdentityError("Loaded Panel source hashes do not match the staged bundle")
                    if stable is None:
                        stable = time.monotonic()
                    if time.monotonic() - stable >= stable_seconds:
                        return probe
                else:
                    stable = None
            except RuntimeIdentityError:
                raise
            except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                last_error = str(exc)
                stable = None
            if not ready_seen and time.monotonic() >= ready_deadline:
                raise RuntimeError(f"Panel HTTP startup failed; last_error={last_error}")
            time.sleep(2)
        raise RuntimeError(f"Panel recovery timed out; missing_baseline_nodes={last_missing}; last_error={last_error}")

    def check_pending(self):
        if self.journal.exists():
            journal = json.loads(self.journal.read_text())
            if journal["phase"] not in ("complete", "recovered"):
                raise RuntimeError("Unfinished transaction; inspect status then run recover before another mutation")

    def begin(self, action, original, state, container, baseline):
        transaction_dir = self.root / "transactions" / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
        transaction_dir.mkdir(parents=True, mode=0o700)
        atomic_write(transaction_dir / "compose.before", original)
        transaction = {"action": action, "phase": "prepared", "directory": str(transaction_dir),
                       "before_sha256": hashlib.sha256(original.encode()).hexdigest(),
                       "previous_state": state, "image": container["Image"], "baseline": baseline,
                       "container_before": container["Id"], "baseline_runtime_hashes": self.runtime_hashes(container), "recreate_attempted": False}
        save_json(self.journal, transaction)
        return transaction

    def publish(self, transaction, text, state):
        path = Path(transaction["directory"])
        atomic_write(path / "compose.after", text)
        transaction["after_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        transaction["next_state"] = state
        transaction["phase"] = "publishing"
        save_json(self.journal, transaction)
        atomic_write(self.compose, text, stat.S_IMODE(self.compose.stat().st_mode))
        transaction["recreate_attempted"] = True
        transaction["phase"] = "recreating"
        save_json(self.journal, transaction)
        self.recreate(transaction["image"], path)
        expected = {f: value["patched_sha256"] for f, value in state["files"].items()} if state else transaction["expected_restored_hashes"]
        result = self.wait_healthy(transaction["baseline"], transaction["image"], expected, self.args.stable_seconds)
        if state:
            save_json(self.state_file, state)
        elif self.state_file.exists():
            self.state_file.unlink()
        transaction["phase"] = "complete"
        transaction["result"] = result
        save_json(self.journal, transaction)
        emit("complete", action=transaction["action"], transaction=str(path), panel_ready=result["http_ready"],
             nodes=result["nodes"], users_count=result["users_count"])

    def recover(self):
        transaction = json.loads(self.journal.read_text())
        if transaction["phase"] in ("complete", "recovered"):
            emit("no_pending_recovery")
            return
        path = Path(transaction["directory"])
        if sha(self.compose) not in {transaction["before_sha256"], transaction.get("after_sha256")}:
            raise RuntimeError("Compose changed outside this transaction; refusing recovery overwrite")
        before = path / "compose.before"
        if sha(before) != transaction["before_sha256"]:
            raise RuntimeError("Recovery backup integrity mismatch")
        # Preserve a state file changed by another operator.
        current_state = self.state()
        if current_state not in (transaction["previous_state"], transaction.get("next_state")):
            raise RuntimeError("Panel state changed outside this transaction")
        self.model(before)
        atomic_write(self.compose, before.read_text(), stat.S_IMODE(self.compose.stat().st_mode))
        previous = transaction["previous_state"]
        if transaction["recreate_attempted"]:
            self.recreate(transaction["image"], path)
            self.wait_healthy(transaction["baseline"], transaction["image"], transaction["baseline_runtime_hashes"])
        if previous:
            save_json(self.state_file, previous)
        elif self.state_file.exists():
            self.state_file.unlink()
        transaction["phase"] = "recovered"
        save_json(self.journal, transaction)
        emit("recovered", service_recreated=transaction["recreate_attempted"], backup=str(path))

    def apply(self):
        self.check_pending()
        original = self.compose.read_text()
        state = self.state()
        container = self.inspect()
        if not container["State"]["Running"]:
            raise RuntimeError("Panel role requires an existing running Compose service for baseline/recovery verification")
        baseline = self.probe(container)
        original_mounts = state["original_mounts"] if state else self.original_mounts(original)
        if self.args.adopt_reviewed_patch:
            if state:
                raise RuntimeError("Already managed; use apply without --adopt-reviewed-patch")
            known = load_spec()["manual_patch_sha256"]
            mounts = self.mounts(original)
            if not all(mounts.values()) or self.runtime_hashes(container) != known:
                raise RuntimeError("Manual bundle is not the exact reviewed five-file patch")
            for file, mount in mounts.items():
                host = Path(mount.source)
                if not host.is_absolute() or host.is_symlink() or not host.is_file() or sha(host) != known[file]:
                    raise RuntimeError("Unverified manual source: " + file)
        else:
            self.require_owned(original, state)
        with tempfile.TemporaryDirectory(prefix="panel-source-") as temp:
            temp = Path(temp)
            self.extract(container["Image"], temp / "image")
            hashes = build_tree(temp / "image", temp / "patched")
            if state and state["image"] == container["Image"] and hashes == {f: v["patched_sha256"] for f, v in state["files"].items()}:
                self.require_owned(original, state)
                if self.runtime_hashes(container) == hashes and baseline["http_ready"]:
                    emit("already_current", recreated=False)
                    return
            if self.args.command == "check":
                emit("compatible", image=container["Image"], would_recreate=True, adoption=self.args.adopt_reviewed_patch)
                return
            transaction = self.begin("apply", original, state, container, baseline)
            dest = Path(transaction["directory"]) / "files"
            shutil.copytree(temp / "patched", dest)
            files = {f: {"host": str(dest / f), "source_sha256": sha(temp / "image" / f), "patched_sha256": hashes[f]} for f in FILES}
            next_state = {"role": ROLE, "compose": str(self.compose), "service": self.service,
                          "image": container["Image"], "files": files, "original_mounts": original_mounts}
            after = self.edit(original, {f: {"source": v["host"], "options": "ro"} for f, v in files.items()})
            staged = Path(transaction["directory"]) / "compose.staged"
            atomic_write(staged, after)
            self.verify_model_delta(self.model(self.compose), self.model(staged))
            if sha(self.compose) != transaction["before_sha256"] or self.inspect()["Id"] != container["Id"]:
                raise RuntimeError("Installation changed during staging; refusing publication")
            try:
                self.publish(transaction, after, next_state)
            except BaseException:
                self.recover()
                raise

    def restore(self):
        self.check_pending()
        state = self.state()
        if not state:
            emit("not_managed", recreated=False)
            return
        original = self.compose.read_text()
        self.require_owned(original, state)
        container = self.inspect()
        baseline = self.probe(container)
        with tempfile.TemporaryDirectory(prefix="panel-restore-source-") as temp:
            self.extract(container["Image"], Path(temp))
            expected = {}
            for file, mount in state["original_mounts"].items():
                if mount:
                    source = Path(mount["source"])
                    if not source.is_absolute():
                        source = self.compose.parent / source
                    if source.is_symlink() or not source.is_file():
                        raise RuntimeError("Original foreign override is missing or not regular: " + file)
                    expected[file] = sha(source)
                else:
                    expected[file] = sha(Path(temp) / file)
        transaction = self.begin("restore", original, state, container, baseline)
        transaction["expected_restored_hashes"] = expected
        save_json(self.journal, transaction)
        after = self.edit(original, state["original_mounts"])
        staged = Path(transaction["directory"]) / "compose.staged"
        atomic_write(staged, after)
        self.verify_model_delta(self.model(self.compose), self.model(staged))
        try:
            self.publish(transaction, after, None)
        except BaseException:
            self.recover()
            raise

    def status(self):
        container = self.inspect()
        state = self.state()
        result = {"managed": state is not None, "container": container["Id"], "image": container["Image"],
                  "running": container["State"]["Running"], "restart_count": container["RestartCount"]}
        if state:
            result["image_drift"] = state["image"] != container["Image"]
            try:
                self.require_owned(self.compose.read_text(), state)
                result["owned_files_valid"] = True
                result["runtime_matches"] = self.runtime_hashes(container) == {f: v["patched_sha256"] for f, v in state["files"].items()}
            except Exception as exc:
                result["owned_files_valid"] = False
                result["ownership_error"] = str(exc)
        if container["State"]["Running"]:
            try:
                result["health"] = self.probe(container)
            except Exception as exc:
                result["health_error"] = str(exc)
        if self.journal.exists():
            transaction = json.loads(self.journal.read_text())
            result["last_transaction"] = {key: transaction[key] for key in ("action", "phase", "directory")}
        emit("status", **result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "apply", "status", "diagnose", "restore", "recover"))
    parser.add_argument("--compose-file", default=os.getenv("PANEL_COMPOSE_FILE", os.getenv("COMPOSE_FILE", "/opt/marzban/docker-compose.yml")))
    parser.add_argument("--service", default=os.getenv("PANEL_SERVICE_NAME", os.getenv("SERVICE_NAME", "marzban")))
    parser.add_argument("--container", default=os.getenv("PANEL_CONTAINER_NAME", os.getenv("CONTAINER_NAME")))
    parser.add_argument("--patch-dir", default=os.getenv("PANEL_PATCH_DIR", "/opt/marzban-panel-patches"))
    parser.add_argument("--wait-seconds", type=float, default=float(os.getenv("PANEL_WAIT_SECONDS", "180")))
    parser.add_argument("--ready-seconds", type=float, default=float(os.getenv("PANEL_READY_SECONDS", "45")))
    parser.add_argument("--stable-seconds", type=float, default=float(os.getenv("PANEL_STABLE_SECONDS", "30")))
    parser.add_argument("--adopt-reviewed-patch", action="store_true", help="Explicitly adopt only the exact previously reviewed manual five-file bundle")
    args = parser.parse_args()
    if args.wait_seconds <= 0 or args.stable_seconds < 0 or args.wait_seconds <= args.stable_seconds:
        parser.error("wait-seconds must exceed stable-seconds and both must be finite nonnegative durations")
    import math
    if not math.isfinite(args.wait_seconds) or not math.isfinite(args.stable_seconds) or not math.isfinite(args.ready_seconds) or args.ready_seconds <= 0:
        parser.error("durations must be finite")
    if args.adopt_reviewed_patch and args.command not in ("check", "apply"):
        parser.error("--adopt-reviewed-patch is only valid for check/apply")
    mutating = args.command in ("apply", "restore", "recover")
    if mutating and os.geteuid() != 0:
        parser.error("Run apply/restore/recover as root")
    if Path(args.patch_dir).is_symlink():
        raise RuntimeError("Panel patch directory must not be a symlink")
    panel = Panel(args)
    if mutating:
        panel.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if panel.root.stat().st_uid != 0 or stat.S_IMODE(panel.root.stat().st_mode) & 0o077:
            raise RuntimeError("Panel patch directory must be private to root (mode 0700)")
        with open(str(panel.compose) + ".stabilizer.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            getattr(panel, args.command)()
    elif args.command == "check":
        panel.apply()
    else:
        panel.status()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit("error", error_type=type(exc).__name__, reason=str(exc))
        raise SystemExit(1)
