import ast
import copy
import hashlib
import contextlib
import http.server
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import compose_mount
from patch_panel import patch_source, build_tree, load_spec
from panel_stabilizer import Panel, FILES, ROLE, TRANSACTION_LABEL, PROBE, atomic_write, save_json

FIXTURE = REPO / "tests/fixtures/panel"


class TransformTests(unittest.TestCase):
    def test_pristine_image_and_reviewed_cache_variant(self):
        for source in [FIXTURE/"image/node.py", FIXTURE/"baseline/app/xray/node.py"]:
            result = patch_source(source.read_text(), "app/xray/node.py")
            compile(result, str(source), "exec")
            self.assertIn("_state_generation", result)
            self.assertIn("logger = getLogger", result)

    def test_changed_affected_class_is_refused(self):
        source = (FIXTURE/"image/node.py").read_text().replace("timeout=60", "timeout=61", 1)
        with self.assertRaisesRegex(ValueError, "Unreviewed"):
            patch_source(source, "app/xray/node.py")

    def test_ambiguous_duplicate_class_is_refused(self):
        source = (FIXTURE/"image/node.py").read_text() + "\nclass ReSTXRayNode:\n    pass\n"
        with self.assertRaisesRegex(ValueError, "unambiguous"):
            patch_source(source, "app/xray/node.py")

    def test_unrelated_source_extension_is_retained(self):
        source = (FIXTURE/"image/node.py").read_text() + "\ndef custom_extension():\n    return 42\n"
        result = patch_source(source, "app/xray/node.py")
        self.assertIn("def custom_extension():\n    return 42", result)

    def test_all_inputs_validated_before_any_output(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)/"input"
            output = Path(root)/"output"
            shutil.copytree(FIXTURE/"baseline", source)
            file = source/"main.py"
            file.write_text(file.read_text().replace("workers=1", "workers=2"))
            with self.assertRaisesRegex(ValueError, "Unreviewed"):
                build_tree(source, output)
            self.assertFalse(output.exists())

    def test_build_is_deterministic_and_requires_image_input(self):
        source = (FIXTURE/"image/node.py").read_text()
        first = patch_source(source, "app/xray/node.py")
        self.assertEqual(first, patch_source(source, "app/xray/node.py"))
        with self.assertRaisesRegex(ValueError, "already patched"):
            patch_source(first, "app/xray/node.py")

    def test_generic_compose_target_keeps_node_default(self):
        text = "services:\n  marzban:\n    image: example\n    volumes:\n      - /data:/var/lib/marzban\n"
        for file in FILES:
            text = compose_mount.add_mount(text, "marzban", "/patches/" + file, "/code/" + file)
        self.assertFalse(compose_mount.service_has_mount(text, "marzban"))
        self.assertEqual(compose_mount.mount_source(text, "marzban", "/code/main.py"), "/patches/main.py")
        for file in FILES:
            text = compose_mount.remove_mount(text, "marzban", "/code/" + file)
        self.assertIn("/data:/var/lib/marzban", text)
        self.assertNotIn("/code/", text)


class ProbeTests(unittest.TestCase):
    def probe(self, host, port=8000, tls=False, uds=None, debug=False):
        config = ModuleType("config")
        values = {"DEBUG": debug, "SQLALCHEMY_DATABASE_URL": "sqlite://", "UVICORN_HOST": host,
                  "UVICORN_PORT": port, "UVICORN_UDS": uds,
                  "UVICORN_SSL_CERTFILE": "cert" if tls else None,
                  "UVICORN_SSL_KEYFILE": "key" if tls else None}
        for key, value in values.items():
            setattr(config, key, value)
        sqlalchemy = ModuleType("sqlalchemy")
        sqlalchemy.create_engine = MagicMock()
        connection = sqlalchemy.create_engine.return_value.connect.return_value.__enter__.return_value
        connection.execute.side_effect = [[], SimpleNamespace(scalar_one=lambda: 0)]
        sqlalchemy.text = lambda value: value
        output = io.StringIO()
        with patch.dict(sys.modules, {"config": config, "sqlalchemy": sqlalchemy}), contextlib.redirect_stdout(output):
            exec(compile(PROBE, "panel-readiness-probe", "exec"), {})
        return json.loads(output.getvalue())

    def test_no_tls_public_and_wildcard_hosts_reach_actual_local_listener(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
            def log_message(self, *_):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for host in ("8.8.8.8", "0.0.0.0"):
                with self.subTest(host=host), patch.dict("os.environ", {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""}):
                    self.assertTrue(self.probe(host, server.server_port)["http_ready"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_private_interface_hostname_and_tls_endpoints(self):
        cases = [("10.0.0.7", False, "10.0.0.7", "http://10.0.0.7:8443/"),
                 ("panel.internal", False, "192.168.1.7", "http://panel.internal:8443/"),
                 ("8.8.8.8", True, "8.8.8.8", "https://8.8.8.8:8443/"),
                 ("0.0.0.0", True, "0.0.0.0", "https://127.0.0.1:8443/")]
        for host, tls, resolved, expected in cases:
            with self.subTest(host=host, tls=tls), patch("socket.gethostbyname", return_value=resolved), patch("urllib.request.build_opener") as build:
                build.return_value.open.return_value.__enter__.return_value.status = 200
                self.assertTrue(self.probe(host, port=8443, tls=tls)["http_ready"])
                self.assertEqual(build.return_value.open.call_args.args[0].full_url, expected)

    def test_unix_socket_and_tls_transport_are_retained(self):
        for tls in (False, True):
            with self.subTest(tls=tls), patch("socket.socket") as socket_factory, patch("ssl._create_unverified_context") as context, patch("http.client.HTTPConnection") as connection:
                connection.return_value.getresponse.return_value.status = 200
                self.assertTrue(self.probe("8.8.8.8", tls=tls, uds="/tmp/panel.sock")["http_ready"])
                sock = socket_factory.return_value
                sock.connect.assert_called_once_with("/tmp/panel.sock")
                if tls:
                    context.return_value.wrap_socket.assert_called_once_with(sock, server_hostname="localhost")
                else:
                    context.return_value.wrap_socket.assert_not_called()

    def test_debug_overrides_uds_and_uses_upstream_default_port(self):
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value.status = 200
            self.assertTrue(self.probe("8.8.8.8", port=8443, uds="/tmp/ignored.sock", debug=True)["http_ready"])
            self.assertEqual(build.return_value.open.call_args.args[0].full_url, "http://127.0.0.1:8000/")


class FakePanel(Panel):
    """Exercise the real transaction/filesystem logic; replace only Docker boundaries."""
    def __init__(self, root):
        self.args = SimpleNamespace(adopt_reviewed_patch=False, command="apply", container=None,
                                    wait_seconds=1, stable_seconds=0)
        self.compose = root/"docker-compose.yml"
        self.compose.write_text("services:\n  marzban:\n    image: panel-fixture\n    volumes:\n      - /data:/var/lib/marzban\n      - /subscription.py:/code/app/routers/subscription.py\n")
        self.service = "marzban"
        self.root = root/"patches"
        self.root.mkdir(mode=0o700)
        self.state_file = self.root/"state.json"
        self.journal = self.root/"transaction.json"
        self.count = 0
        self.image = "sha256:fixture"
        self.fail_wait = False
        self.labels = {}
        self.pristine = {f:(FIXTURE/"baseline"/f).read_text() for f in FILES}
        self.pristine["app/xray/node.py"] = (FIXTURE/"image/node.py").read_text()

    def inspect(self):
        return {"Id": "container-" + str(self.count), "Image": self.image,
                "State": {"Running": True}, "RestartCount": 0, "Config": {"Labels": self.labels}}

    def probe(self, container):
        return {"http_ready": True, "nodes": [{"id": 1, "name": "fixture", "status": "connected"}], "users_count": 2595}

    def model(self, file):
        text = Path(file).read_text()
        mounts = compose_mount._short_volume_items(text.splitlines(), self.service)
        return {"name": "fixture", "services": {"marzban": {"image": "panel-fixture", "volumes": [
            {"source": m.source, "target": m.target, "read_only": m.options == "ro"} for m in mounts]}}}

    def runtime_hashes(self, container):
        result = {}
        for file, mount in self.mounts(self.compose.read_text()).items():
            data = Path(mount.source).read_bytes() if mount else self.pristine[file].encode()
            result[file] = hashlib.sha256(data).hexdigest()
        return result

    def extract(self, image, destination):
        for file, content in self.pristine.items():
            p = destination/file
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

    def recreate(self, image, transaction_dir):
        self.count += 1
        # Every referenced source already exists and compiles before this sole activation.
        for file, mount in self.mounts(self.compose.read_text()).items():
            if mount:
                compile(Path(mount.source).read_text(), file, "exec")
        self.image = image
        self.labels = {TRANSACTION_LABEL: transaction_dir.name}

    def wait_healthy(self, baseline, image, expected=None, stable_seconds=0):
        if self.fail_wait:
            self.fail_wait = False
            raise RuntimeError("synthetic readiness failure")
        if expected is not None and self.runtime_hashes(self.inspect()) != expected:
            raise AssertionError("Loaded source mismatch")
        return self.probe(self.inspect())


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.panel = FakePanel(self.root)

    def test_one_apply_then_idempotent_reapply_and_restore(self):
        before = self.panel.compose.read_text()
        self.panel.apply()
        self.assertEqual(self.panel.count, 1)
        self.assertEqual(len(self.panel.state()["files"]), 5)
        self.assertIn("/subscription.py:/code/app/routers/subscription.py", self.panel.compose.read_text())
        self.panel.apply()
        self.assertEqual(self.panel.count, 1)
        self.panel.restore()
        self.assertEqual(self.panel.count, 2)
        self.assertIsNone(self.panel.state())
        self.assertEqual(self.panel.compose.read_text(), before)

    def test_foreign_override_refused_before_transaction(self):
        text = compose_mount.add_mount(self.panel.compose.read_text(), "marzban", "/foreign.py", "/code/main.py")
        self.panel.compose.write_text(text)
        with self.assertRaisesRegex(RuntimeError, "Foreign"):
            self.panel.apply()
        self.assertEqual(self.panel.count, 0)
        self.assertFalse(self.panel.journal.exists())
        self.assertEqual(self.panel.compose.read_text(), text)

    def test_exact_manual_adoption_and_restore_preceding_topology(self):
        originals = {}
        text = self.panel.compose.read_text()
        for file in FILES:
            dest = self.root/"manual"/file
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(FIXTURE/"manual"/file, dest)
            originals[file] = dest.read_bytes()
            text = compose_mount.add_mount(text, "marzban", str(dest), "/code/" + file, None)
        self.panel.compose.write_text(text)
        mounts_before = self.panel.original_mounts(text)
        self.panel.args.adopt_reviewed_patch = True
        self.panel.apply()
        self.assertEqual(self.panel.count, 1)
        self.panel.args.adopt_reviewed_patch = False
        self.panel.restore()
        self.assertEqual(self.panel.original_mounts(self.panel.compose.read_text()), mounts_before)
        for file in FILES:
            self.assertEqual((self.root/"manual"/file).read_bytes(), originals[file])

    def test_failed_activation_rolls_back_with_verified_runtime(self):
        before = self.panel.compose.read_text()
        self.panel.fail_wait = True
        with self.assertRaisesRegex(RuntimeError, "synthetic readiness"):
            self.panel.apply()
        self.assertEqual(self.panel.count, 2)
        self.assertEqual(self.panel.compose.read_text(), before)
        self.assertIsNone(self.panel.state())
        self.assertEqual(json.loads(self.panel.journal.read_text())["phase"], "recovered")

    def test_interrupted_prepublication_recovery_does_not_recreate(self):
        before = self.panel.compose.read_text()
        self.panel.begin("apply", before, None, self.panel.inspect(), self.panel.probe(None))
        with self.assertRaisesRegex(RuntimeError, "Unfinished"):
            self.panel.apply()
        self.panel.recover()
        self.assertEqual(self.panel.count, 0)
        self.assertEqual(self.panel.compose.read_text(), before)

    def test_recovery_refuses_unrelated_compose_edit(self):
        self.panel.begin("apply", self.panel.compose.read_text(), None, self.panel.inspect(), self.panel.probe(None))
        self.panel.compose.write_text(self.panel.compose.read_text() + "# operator change\n")
        with self.assertRaisesRegex(RuntimeError, "outside"):
            self.panel.recover()
        self.assertEqual(self.panel.count, 0)

    def test_recovery_refuses_external_runtime_before_writing(self):
        for image in ("sha256:fixture", "sha256:new-operator-image"):
            with self.subTest(image=image):
                before = self.panel.compose.read_text()
                transaction = self.panel.begin("apply", before, None, self.panel.inspect(), self.panel.probe(None))
                transaction["recreate_attempted"] = True
                save_json(self.panel.journal, transaction)
                self.panel.count += 1
                self.panel.image = image
                count = self.panel.count
                with self.assertRaisesRegex(RuntimeError, "runtime changed outside"):
                    self.panel.recover()
                self.assertEqual(self.panel.count, count)
                self.assertEqual(self.panel.compose.read_text(), before)
                self.assertIsNone(self.panel.state())
                self.assertEqual(self.panel.image, image)
                self.assertEqual(json.loads(self.panel.journal.read_text())["phase"], "prepared")
                self.panel.image = "sha256:fixture"

    def test_recovery_accepts_own_interrupted_replacement(self):
        before = self.panel.compose.read_text()
        transaction = self.panel.begin("apply", before, None, self.panel.inspect(), self.panel.probe(None))
        transaction["recreate_attempted"] = True
        save_json(self.panel.journal, transaction)
        self.panel.recreate(transaction["image"], Path(transaction["directory"]))
        self.panel.recover()
        self.assertEqual(self.panel.count, 2)
        self.assertEqual(json.loads(self.panel.journal.read_text())["phase"], "recovered")

    def test_recovery_refuses_changed_preceding_override_source(self):
        source = self.root/"old-main.py"
        source.write_text(self.panel.pristine["main.py"])
        before = compose_mount.add_mount(self.panel.compose.read_text(), "marzban", str(source), "/code/main.py")
        self.panel.compose.write_text(before)
        self.panel.begin("apply", before, None, self.panel.inspect(), self.panel.probe(None))
        source.write_text(source.read_text() + "# operator edit\n")
        with self.assertRaisesRegex(RuntimeError, "Original Panel source changed"):
            self.panel.recover()
        self.assertEqual(self.panel.count, 0)
        self.assertEqual(self.panel.compose.read_text(), before)

    def test_changed_managed_file_refuses_apply_and_restore(self):
        self.panel.apply()
        file = Path(self.panel.state()["files"]["main.py"]["host"])
        file.write_text(file.read_text() + "# operator edit\n")
        for command in (self.panel.apply, self.panel.restore):
            with self.assertRaisesRegex(RuntimeError, "Modified"):
                command()
        self.assertEqual(self.panel.count, 1)

    def test_publication_rejects_external_compose_edit(self):
        before = self.panel.compose.read_text()
        transaction = self.panel.begin("apply", before, None, self.panel.inspect(), self.panel.probe(None))
        modified = before + "# concurrent operator edit\n"
        self.panel.compose.write_text(modified)
        with self.assertRaisesRegex(RuntimeError, "changed during staging"):
            self.panel.publish(transaction, before, None)
        self.assertEqual(self.panel.compose.read_text(), modified)
        self.assertEqual(self.panel.count, 0)

    def test_recovery_failure_reports_both_causes(self):
        with patch.object(self.panel, "recover", side_effect=RuntimeError("secondary recovery")):
            with self.assertRaisesRegex(RuntimeError, "primary operation.*secondary recovery"):
                self.panel.recover_after_failure(RuntimeError("primary operation"))

    def test_status_does_not_recreate(self):
        self.panel.status()
        self.assertEqual(self.panel.count, 0)


if __name__ == "__main__":
    unittest.main()
