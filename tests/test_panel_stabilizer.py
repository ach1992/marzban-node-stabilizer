import ast
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import compose_mount
from patch_panel import patch_source, build_tree, load_spec
from panel_stabilizer import Panel, FILES, ROLE, atomic_write, save_json

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
        self.pristine = {f:(FIXTURE/"baseline"/f).read_text() for f in FILES}
        self.pristine["app/xray/node.py"] = (FIXTURE/"image/node.py").read_text()

    def inspect(self):
        return {"Id": "container-" + str(self.count), "Image": self.image,
                "State": {"Running": True}, "RestartCount": 0}

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

    def test_changed_managed_file_refuses_apply_and_restore(self):
        self.panel.apply()
        file = Path(self.panel.state()["files"]["main.py"]["host"])
        file.write_text(file.read_text() + "# operator edit\n")
        for command in (self.panel.apply, self.panel.restore):
            with self.assertRaisesRegex(RuntimeError, "Modified"):
                command()
        self.assertEqual(self.panel.count, 1)

    def test_status_does_not_recreate(self):
        self.panel.status()
        self.assertEqual(self.panel.count, 0)


if __name__ == "__main__":
    unittest.main()
