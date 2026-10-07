import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "lib" / "compose_mount.py"

spec = importlib.util.spec_from_file_location("compose_mount", MODULE_PATH)
compose_mount = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(compose_mount)

BASE = """services:\n  marzban-node:\n    image: gozargah/marzban-node:latest\n    restart: always\n"""

WITH_VOLUMES = """services:\n  marzban-node:\n    image: gozargah/marzban-node:latest\n    volumes:\n      - /var/lib/marzban-node:/var/lib/marzban-node\n    restart: always\n"""

LONG_SYNTAX_CASES = {
    "target_first": """services:
  marzban-node:
    volumes:
      - target: /code/rest_service.py
        source: /tmp/x
        type: bind
""",
    "source_target_type": """services:
  marzban-node:
    volumes:
      - source: /tmp/x
        target: /code/rest_service.py
        type: bind
""",
    "conventional_type_source_target": """services:
  marzban-node:
    volumes:
      - type: bind
        source: /tmp/x
        target: /code/rest_service.py
""",
    "target_inline_comment": """services:
  marzban-node:
    volumes:
      - type: bind
        source: /tmp/x
        target: /code/rest_service.py # exact target
""",
    "different_target": """services:
  marzban-node:
    volumes:
      - target: /code/other.py
        source: /tmp/x
        type: bind
""",
    "flow_mapping": """services:
  marzban-node:
    volumes:
      - {type: bind, source: /tmp/x, target: /code/rest_service.py}
""",
}


class ComposeMountTests(unittest.TestCase):
    def assert_layout_rejected_everywhere(self, text):
        pure_operations = (
            lambda: compose_mount.mount_source(text, "marzban-node"),
            lambda: compose_mount.service_has_mount(text, "marzban-node"),
            lambda: compose_mount.add_mount(text, "marzban-node", "/opt/mns/rest_service.py"),
            lambda: compose_mount.remove_mount(text, "marzban-node"),
        )
        for operation in pure_operations:
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    operation()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compose.yml"
            for action in ("source", "has", "add", "remove"):
                with self.subTest(cli_action=action):
                    path.write_text(text)
                    args = [
                        sys.executable,
                        str(MODULE_PATH),
                        action,
                        "--file",
                        str(path),
                        "--service",
                        "marzban-node",
                    ]
                    if action == "add":
                        args.extend(["--patch-file", "/opt/mns/rest_service.py"])

                    result = subprocess.run(
                        args,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(path.read_text(), text)

    def test_adds_volumes_section_when_missing(self):
        out = compose_mount.add_mount(BASE, "marzban-node", "/opt/marzban-node-patches/rest_service.py")
        self.assertIn("    volumes:\n", out)
        self.assertIn('      - "/opt/marzban-node-patches/rest_service.py:/code/rest_service.py:ro"\n', out)

    def test_add_is_idempotent(self):
        once = compose_mount.add_mount(WITH_VOLUMES, "marzban-node", "/opt/marzban-node-patches/rest_service.py")
        twice = compose_mount.add_mount(once, "marzban-node", "/opt/marzban-node-patches/rest_service.py")
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(":/code/rest_service.py:ro"), 1)

    def test_remove_preserves_other_volumes(self):
        mounted = compose_mount.add_mount(WITH_VOLUMES, "marzban-node", "/opt/marzban-node-patches/rest_service.py")
        restored = compose_mount.remove_mount(mounted, "marzban-node")
        self.assertNotIn(":/code/rest_service.py:ro", restored)
        self.assertIn("/var/lib/marzban-node:/var/lib/marzban-node", restored)

    def test_remove_drops_empty_volumes_section(self):
        mounted = compose_mount.add_mount(BASE, "marzban-node", "/opt/marzban-node-patches/rest_service.py")
        restored = compose_mount.remove_mount(mounted, "marzban-node")
        self.assertEqual(restored, BASE)

    def test_missing_service_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Compose service"):
            compose_mount.add_mount(BASE, "other", "/opt/x.py")

    def test_all_mapping_style_long_syntax_is_rejected_before_mutation(self):
        for name, text in LONG_SYNTAX_CASES.items():
            with self.subTest(layout=name):
                self.assert_layout_rejected_everywhere(text)

    def test_inline_or_aliased_volumes_value_is_rejected_before_mutation(self):
        cases = {
            "empty_flow": """services:
  marzban-node:
    image: example
    volumes: []
""",
            "flow_sequence": """services:
  marzban-node:
    image: example
    volumes: [/tmp/a:/data]
""",
            "alias": """services:
  marzban-node:
    image: example
    volumes: *shared_volumes
""",
            "anchored_block": """services:
  marzban-node:
    image: example
    volumes: &shared_volumes
      - /tmp/a:/data
""",
        }
        for name, text in cases.items():
            with self.subTest(layout=name):
                self.assert_layout_rejected_everywhere(text)

    def test_interpolated_short_syntax_is_rejected_before_mutation(self):
        text = """services:
  marzban-node:
    volumes:
      - ${REST_SOURCE:-/tmp/rest_service.py}:/code/rest_service.py:ro
"""
        self.assert_layout_rejected_everywhere(text)

    def test_anonymous_exact_target_is_rejected_before_mutation(self):
        text = """services:
  marzban-node:
    volumes:
      - /code/rest_service.py
"""
        self.assert_layout_rejected_everywhere(text)

    def test_inline_comment_on_supported_short_syntax_is_classified_safely(self):
        text = """services:
  marzban-node:
    volumes: # ordinary comment
      - "/tmp/x:/code/rest_service.py:ro" # target mount
"""
        self.assertTrue(compose_mount.service_has_mount(text, "marzban-node"))
        self.assertEqual(compose_mount.mount_source(text, "marzban-node"), "/tmp/x")

    def test_other_service_target_mount_is_preserved(self):
        text = """services:
  marzban-node:
    image: example/node
  helper:
    image: example/helper
    volumes:
      - /tmp/helper.py:/code/rest_service.py:ro
"""
        mounted = compose_mount.add_mount(text, "marzban-node", "/opt/mns/rest_service.py")
        self.assertIn("/tmp/helper.py:/code/rest_service.py:ro", mounted)
        restored = compose_mount.remove_mount(mounted, "marzban-node")
        self.assertIn("/tmp/helper.py:/code/rest_service.py:ro", restored)

    def test_has_is_scoped_to_selected_service(self):
        text = """services:
  marzban-node:
    image: example/node
  helper:
    image: example/helper
    volumes:
      - /tmp/helper.py:/code/rest_service.py:ro
"""
        self.assertFalse(compose_mount.service_has_mount(text, "marzban-node"))
        self.assertTrue(compose_mount.service_has_mount(text, "helper"))

    def test_mount_source_returns_selected_service_source(self):
        mounted = compose_mount.add_mount(
            WITH_VOLUMES,
            "marzban-node",
            "/opt/marzban-node-patches/rest_service.py",
        )
        self.assertEqual(
            compose_mount.mount_source(mounted, "marzban-node"),
            "/opt/marzban-node-patches/rest_service.py",
        )

    def test_multiple_exact_target_mounts_are_rejected(self):
        text = """services:
  marzban-node:
    volumes:
      - /tmp/a.py:/code/rest_service.py:ro
      - /tmp/b.py:/code/rest_service.py:ro
"""
        with self.assertRaisesRegex(ValueError, "multiple exact rest_service.py mounts"):
            compose_mount.mount_source(text, "marzban-node")

    def test_non_volume_occurrences_are_never_treated_as_mounts(self):
        text = """services:
  marzban-node:
    image: example/node
    environment:
      - NOTE=/code/rest_service.py
    command:
      - sh
      - -c
      - echo /code/rest_service.py
    healthcheck:
      test:
        - CMD-SHELL
        - test -f /code/rest_service.py
    # /code/rest_service.py is mentioned here too
"""
        self.assertFalse(compose_mount.service_has_mount(text, "marzban-node"))
        self.assertIsNone(compose_mount.mount_source(text, "marzban-node"))
        mounted = compose_mount.add_mount(text, "marzban-node", "/opt/mns/rest_service.py")
        restored = compose_mount.remove_mount(mounted, "marzban-node")
        self.assertEqual(restored, text)

    def test_similar_target_is_not_treated_as_exact_target(self):
        text = """services:
  marzban-node:
    volumes:
      - /tmp/backup.py:/code/rest_service.py.backup:ro
"""
        self.assertFalse(compose_mount.service_has_mount(text, "marzban-node"))
        mounted = compose_mount.add_mount(text, "marzban-node", "/opt/mns/rest_service.py")
        self.assertIn("/tmp/backup.py:/code/rest_service.py.backup:ro", mounted)
        restored = compose_mount.remove_mount(mounted, "marzban-node")
        self.assertEqual(restored, text)

    def test_colon_in_short_syntax_source_does_not_hide_exact_target(self):
        text = """services:
  marzban-node:
    volumes:
      - /tmp/source:with-colon:/code/rest_service.py:ro
"""
        self.assertTrue(compose_mount.service_has_mount(text, "marzban-node"))
        self.assertEqual(
            compose_mount.mount_source(text, "marzban-node"),
            "/tmp/source:with-colon",
        )

    def test_unsafe_patch_path_is_rejected(self):
        for path in (
            "relative.py",
            "/tmp/a:b.py",
            "/tmp/$HOME.py",
            '/tmp/a"b.py',
            "/tmp/a'b.py",
            "/tmp/a#b.py",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    compose_mount.add_mount(BASE, "marzban-node", path)

    def test_atomic_write_replaces_complete_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compose.yml"
            path.write_text(BASE)
            compose_mount.atomic_write(path, WITH_VOLUMES)
            self.assertEqual(path.read_text(), WITH_VOLUMES)


if __name__ == "__main__":
    unittest.main()
