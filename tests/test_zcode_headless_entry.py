"""F2-SEAT-BIND-1: the packaged glm entry boots with a scratch HOME.

The fake CLI implements the measured provider discovery contract, never a
provider call. Its entry and bundled data occupy ZCode 3.16.3's real layout.
"""

from __future__ import annotations

from tests.readiness import wait_until

import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from floati.adapters.zcode import ZcodeAdapter, require_zcode_terminal_runtime
from floati.errors import ProtocolRefusal
from floati.cli import main
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import ZcodeResumeWakeAdapter
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from tests.temp_roots import REAL_TEMP_ROOT


FAKE_CLI = '''import json, os, sys
from pathlib import Path
entry = Path(__file__)
override = os.environ.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE")
candidates = ([Path(override)] if override else [
    entry.parent / "provider/zcode-builtin.json",
    entry.parent / "../../../../../config/provider/zcode-builtin.json",
])
config = next((path for path in candidates if path.is_file()), None)
if config is None:
    print("无法定位 CLI ZCode Built-in Provider Config：" +
          ", ".join(map(str, candidates)), file=sys.stderr)
    sys.exit(1)
assert json.loads(config.read_text()) == {"fixture": True}
session = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else "new-session"
payload = {"sessionId": session, "response": "fixture booted",
                  "home": os.environ["HOME"], "config": str(config),
                  "personal": os.environ.get("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE")}
print(json.dumps(payload))
# The detached wake has no stdout pipe; this fixture-only witness observes
# its launch environment. Publish atomically so the parent sees whole JSON.
if "--resume" in sys.argv:
    staging = Path.cwd() / "launch.tmp"
    staging.write_text(json.dumps(payload))
    staging.replace(Path.cwd() / "launch.json")
'''


class ZcodeHeadlessEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.resources = self.base / "ZCode.app/Contents/Resources"
        self.entry = self.resources / "glm/zcode.cjs"
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text(FAKE_CLI, encoding="utf-8")
        self.entry.chmod(0o700)
        self.config = self.resources / "config/provider/zcode-builtin.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text('{"fixture": true}\n', encoding="utf-8")
        self.node = self.base / "fake-node"
        self.node.write_text('#!/bin/sh\nexec /usr/bin/python3 -B "$@"\n')
        self.node.chmod(0o700)
        self.home = self.base / "home"
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for key in ("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"):
            os.environ.pop(key, None)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(self.root).register("builder-a", "zcode")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.coordinate = DaemonCoordinate(self.root, "builder-a", "zcode")
        self.initial = self.snapshot()

    def snapshot(self):
        return {str(path.relative_to(self.resources)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.resources.rglob("*") if path.is_file()}

    def adapter(self):
        return ZcodeResumeWakeAdapter(self.coordinate, node_executable=self.node,
                                      entry_executable=self.entry)

    def bind(self, *, probe=True):
        arguments = ["wake", "daemon", "bind", "--root", str(self.root.path),
                     "--as", "builder-a", "--harness", "zcode",
                     "--session", "fixture-session", "--workspace", str(self.workspace),
                     "--executable", str(self.entry), "--binding-epoch", "1",
                     "--zcode-node-executable", str(self.node),
                     "--zcode-entry-executable", str(self.entry)]
        if probe:
            arguments.append("--yes")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), mock.patch("sys.stdin", io.StringIO("")):
            status = main(arguments)
        return status, json.loads(stdout.getvalue() or stderr.getvalue())

    def test_worker_boots_the_declared_glm_entry_without_changing_the_app(self):
        parent = self.base / "floati-work"
        with mock.patch("floati.adapters.codex_live._WORKSPACE_PARENT", parent),\
                mock.patch("floati.adapters.headless_template._WORKSPACE_PARENT", parent):
            adapter = ZcodeAdapter((str(self.node), str(self.entry)))
            item = {"id": "fixture-work", "workspace": str(parent / "fixture-work"), "title": "boot"}
            handle = adapter.spawn(item, deadline_seconds=10)
            self.addCleanup(adapter.cancel)
            result = adapter.drive(handle, deadline_seconds=10)
        self.assertEqual(str(self.config), result["payload"]["config"])
        self.assertEqual(str(self.home), result["payload"]["home"])
        self.assertEqual(self.initial, self.snapshot())
        self.assertEqual((str(self.node), str(self.entry)), tuple(adapter.command))

    def test_bind_probe_boots_with_declared_entry_and_scratch_home(self):
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                status, artifact = self.bind()
                self.assertEqual(0, status, artifact)
                self.assertEqual("resume_proven", artifact["evidence"]["resume_state"])
        binding = AdapterBindingStore(self.root).read(self.coordinate)
        self.assertEqual(str(self.entry), binding["executable"])
        self.assertEqual(self.initial["glm/zcode.cjs"], binding["executable_digest"])
        self.assertEqual(self.initial, self.snapshot())

    def test_missing_provider_config_names_the_cause_and_preserves_stderr(self):
        self.config.unlink()
        status, artifact = self.bind()
        self.assertEqual(20, status, artifact)
        self.assertEqual("wake_bind_provider_config_missing", artifact["evidence"]["code"])
        self.assertIn("ZCode Built-in Provider Config", artifact["evidence"]["detail"])
        self.assertNotIn("large rollout", artifact["evidence"]["detail"])
        self.assertFalse(AdapterBindingStore(self.root).path(self.coordinate).exists())

    def test_missing_entry_dependency_names_version_and_piece_before_binding(self):
        (self.resources.parent / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleShortVersionString": "3.17.1",
        }))
        self.entry.write_text(
            "import sys\n"
            "print(\"Error: Cannot find package '@zcode/tui' imported from entry\", file=sys.stderr)\n"
            "sys.exit(1)\n", encoding="utf-8")
        status, artifact = self.bind()
        self.assertEqual(20, status, artifact)
        self.assertEqual("zcode_entrypoint_unavailable", artifact["evidence"]["code"])
        self.assertIn("3.17.1", artifact["evidence"]["detail"])
        self.assertIn("@zcode/tui", artifact["evidence"]["detail"])
        self.assertFalse(AdapterBindingStore(self.root).path(self.coordinate).exists())

    def test_interactive_resolution_distinguishes_absence_from_unknown_and_never_loads_preloads(self):
        self.entry.write_text('async function tui() { return import ( "@zcode/tui" ); }\n')
        cases = [(0, '{"code":null}', None),
                 (0, '{"code":"ERR_PACKAGE_PATH_NOT_EXPORTED"}', 'zcode_entrypoint_unverified'),
                 (0, 'not-json', 'zcode_entrypoint_unverified'),
                 (1, '', 'zcode_entrypoint_unverified')]
        for status, output, expected in cases:
            with self.subTest(status=status, output=output), mock.patch.dict(os.environ, {
                    'NODE_OPTIONS': '--require=/foreign/preload.js', 'NODE_PATH': '/foreign/modules'}),\
                    mock.patch('subprocess.run', return_value=
                        subprocess.CompletedProcess([], status, output, '')) as resolver:
                if expected is None:
                    require_zcode_terminal_runtime((str(self.node), str(self.entry)))
                else:
                    with self.assertRaises(ProtocolRefusal) as caught:
                        require_zcode_terminal_runtime((str(self.node), str(self.entry)))
                    self.assertEqual(expected, caught.exception.code)
                environment = resolver.call_args.kwargs['env']
                self.assertNotIn('NODE_OPTIONS', environment)
                self.assertNotIn('NODE_PATH', environment)
        with mock.patch('subprocess.run', side_effect=subprocess.TimeoutExpired('resolver', 15)):
            with self.assertRaises(ProtocolRefusal) as caught:
                require_zcode_terminal_runtime((str(self.node), str(self.entry)))
        self.assertEqual('zcode_entrypoint_unverified', caught.exception.code)

    def test_bound_display_names_zcode_even_without_a_probe(self):
        status, artifact = self.bind(probe=False)
        self.assertEqual(0, status, artifact)
        self.assertEqual("exact zcode session binding recorded.", artifact["evidence"]["display"])

    def test_detached_wake_boots_the_same_packaged_entry_under_scratch_home(self):
        status, artifact = self.bind(probe=False)
        self.assertEqual(0, status, artifact)
        binding = AdapterBindingStore(self.root).read(self.coordinate)
        result = self.adapter().request_wake(binding, "wake fixture", 10)
        self.assertEqual("woke", result.outcome)
        witness = self.workspace / "launch.json"
        wait_until(witness.exists, "detached CLI launch witness")
        payload = json.loads(witness.read_text())
        self.assertEqual("fixture-session", payload["sessionId"])
        self.assertEqual(str(self.home), payload["home"])
        self.assertEqual(str(self.config), payload["config"])
        self.assertEqual(self.initial, self.snapshot())

    def test_custom_glm_declaration_keeps_supported_provider_environment(self):
        custom = self.base / "tools/glm/zcode.cjs"
        custom.parent.mkdir(parents=True)
        custom.write_text(FAKE_CLI, encoding="utf-8")
        custom.chmod(0o700)
        personal = str(self.home / "personal.json")
        with mock.patch.dict(os.environ, {
            "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": str(self.config),
            "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": personal,
        }):
            result = ZcodeResumeWakeAdapter(
                self.coordinate, node_executable=self.node, entry_executable=custom,
            ).probe_resume(custom, self.workspace, "custom-session", "probe", 10)
        self.assertEqual("woke", result.outcome, result)
        payload = json.loads((self.workspace / "launch.json").read_text())
        self.assertEqual(personal, payload["personal"])
        self.assertEqual(str(self.home), payload["home"])

    def test_packaged_entry_uses_its_own_config_and_keeps_personal_coordinates(self):
        personal = str(self.home / "personal.json")
        foreign = str(self.base / "foreign-config.json")
        with mock.patch.dict(os.environ, {
            "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": foreign,
            "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": personal,
        }):
            result = self.adapter().probe_resume(
                self.entry, self.workspace, "fixture-session", "probe", 10,
            )
            self.assertEqual(foreign, os.environ["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"])
        self.assertEqual("woke", result.outcome, result)
        payload = json.loads((self.workspace / "launch.json").read_text())
        self.assertEqual(str(self.config), payload["config"])
        self.assertEqual(personal, payload["personal"])


if __name__ == "__main__":
    unittest.main()
