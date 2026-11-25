"""Every derived headless profile must complete through the real Worker runtime."""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import shutil
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import floati.adapters
from floati import fixture_ids
from floati.adapters.headless_template import HarnessProfile, HeadlessProfileAdapter
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerReceipts, WorkerRunner
from tests.temp_roots import REAL_TEMP_ROOT


NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
NET_HEADLESS_FIXTURES = Path(__file__).parent / "fixtures" / "net-headless"


def headless_adapter_types():
    """Import the package, then derive membership from class ancestry."""
    classes = set()
    for entry in pkgutil.walk_packages(
        floati.adapters.__path__, floati.adapters.__name__ + ".",
    ):
        module = importlib.import_module(entry.name)
        for _, candidate in inspect.getmembers(module, inspect.isclass):
            if (candidate is not HeadlessProfileAdapter
                    and HeadlessProfileAdapter in candidate.__mro__):
                classes.add(candidate)
    return sorted(classes, key=lambda cls: (cls.__module__, cls.__qualname__))


class HeadlessWorkerProtocolTests(unittest.TestCase):
    def assert_completes(self, adapter_cls, replay=None):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary)
            # The shell carries bytes produced by Python; it does not quote JSON.
            harness = directory / "harness.py"
            harness.write_bytes(
                b"from pathlib import Path\n"
                b"import sys\n"
                b"Path('answer.txt').write_bytes(b'worker answer\\n')\n"
                b"sys.stdout.buffer.write(b'{\"status\":\"ok\"}\\n')\n"
            )
            profile = HarnessProfile(
                name=adapter_cls.name,
                command=("/usr/bin/python3", "-B", str(harness)),
            )
            if replay is not None:
                harness.write_text(
                    "from pathlib import Path\nimport sys\n"
                    f"if sys.argv[1:] != {replay['argv']!r}:\n"
                    "    sys.exit(2)\n"
                    "Path('answer.txt').write_bytes(b'worker answer\\n')\n"
                    f"sys.stdout.buffer.write({replay['stdout'].encode()!r})\n"
                    f"sys.stderr.buffer.write({replay['stderr'].encode()!r})\n"
                    f"sys.exit({replay['exit_code']!r})\n"
                )
                profile = replace(adapter_cls._default_profile(), command=profile.command)
            root = FloatiRoot.open_direct_home(directory / "fleet", create=True)
            node = fixture_ids.builder("a")
            Registry(root).register(node, "Codex")
            work = WorkLog(root)
            item = work.add("write answer", node, [], provision_workspace=True, now=NOW)
            workspace = Path(item["workspace"])
            self.addCleanup(shutil.rmtree, workspace, True)
            AuthorityGrantStore(root).claim("work-claims", node, 60, 60, NOW)
            runner = WorkerRunner(root, {adapter_cls.name: adapter_cls(profile)})

            result = runner.run(node, adapter_cls.name, now=NOW)

            if replay is not None and replay["exit_code"] != 0:
                self.assertEqual("degrade", result["transition"])
                self.assertEqual("adapter_error", result["outcome_code"])
                self.assertNotEqual("completed", work.show(item["id"])[0]["state"])
                self.assertFalse(result.get("artifact_bindings"))
                self.assertEqual([], runner.last_process_audit["alive_after_cleanup"])
                return

            self.assertEqual("complete", result["transition"], result)
            self.assertIsNone(result["outcome_code"])
            bindings = result["artifact_bindings"]
            self.assertEqual(1, len(bindings))
            binding = bindings[0]
            self.assertEqual({"repo", "sha", "doc"}, set(binding))
            self.assertEqual("local/" + item["id"], binding["repo"])
            self.assertEqual("answer.txt", binding["doc"])
            self.assertRegex(binding["sha"], r"^[0-9a-f]{40}$")
            committed = subprocess.run(
                ["/usr/bin/git", "show", binding["sha"] + ":answer.txt"],
                cwd=workspace, capture_output=True, check=True,
            )
            self.assertEqual(b"worker answer\n", committed.stdout)
            completed = work.show(item["id"])[0]
            self.assertEqual("completed", completed["state"])
            self.assertEqual(bindings, completed["artifact_bindings"])
            receipts = WorkerReceipts(root).records()
            self.assertEqual(
                ["claim", "spawn", "drive", "bind_artifact", "complete"],
                [record["transition"] for record in receipts],
            )
            self.assertEqual(bindings, receipts[-2]["artifact_bindings"])
            self.assertEqual([], runner.last_process_audit["alive_after_cleanup"])

    def test_recorded_headless_turns_preserve_worker_receipt_outcomes(self):
        adapters = {cls.name: cls for cls in headless_adapter_types()}
        for name in ("gemini", "copilot"):
            with self.subTest(adapter=name):
                self.assertIn(name, adapters)
                adapter_cls = adapters[name]
                self.assertFalse(adapter_cls.availability()["surface_verified"])
                replays = json.loads((NET_HEADLESS_FIXTURES / f"{name}.json").read_text())
                for replay in replays:
                    with self.subTest(source=replay["source"]):
                        self.assert_completes(adapter_cls, replay)

    def test_every_derived_adapter_completes_and_binds_its_artifact(self):
        population = headless_adapter_types()
        self.assertTrue(population, "an empty census must not pass")
        for adapter_cls in population:
            with self.subTest(adapter=adapter_cls.__name__):
                self.assert_completes(adapter_cls)

    def test_new_subclass_is_discovered_and_runs_without_a_roster_edit(self):
        module = importlib.import_module("floati.adapters.headless_template")

        class FutureHeadlessAdapter(HeadlessProfileAdapter):
            name = "future-fixture"

        before = set(headless_adapter_types())
        # Simulate a newly shipped class visible in an adapter module.
        module.FutureHeadlessAdapter = FutureHeadlessAdapter
        try:
            additions = set(headless_adapter_types()) - before
            self.assertEqual({FutureHeadlessAdapter}, additions)
            for adapter_cls in additions:
                with self.subTest(adapter=adapter_cls.__name__):
                    self.assert_completes(adapter_cls)
        finally:
            del module.FutureHeadlessAdapter

    def test_background_writer_is_quiescent_before_artifact_commit(self):
        base = headless_adapter_types()[0]

        class BoundaryAdapter(base):
            def _finalize(self, handle):
                evidence = handle.filesystem_workspace / ".floati"
                (evidence / "finalize-started").touch()
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    if (evidence / "mutated").exists():
                        break
                    time.sleep(0.01)
                return super()._finalize(handle)

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary)
            harness = directory / "harness.py"
            descendant = (
                "from pathlib import Path\nimport time\n"
                "while not Path('.floati/finalize-started').exists():\n"
                "    time.sleep(0.01)\n"
                "Path('answer.txt').write_bytes(b'late mutation\\n')\n"
                "Path('.floati/mutated').touch()\n"
            )
            harness.write_text(
                "from pathlib import Path\nimport subprocess, sys\n"
                "Path('answer.txt').write_bytes(b'worker answer\\n')\n"
                "subprocess.Popen(['/usr/bin/python3', '-B', '-c', "
                + repr(descendant) + "], stdin=subprocess.DEVNULL, "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "sys.stdout.buffer.write(b'{}\\n')\n",
                encoding="utf-8",
            )
            parent = directory / "work"
            item = {"id": "work-boundary", "workspace": str(parent / "work-boundary")}
            profile = HarnessProfile(
                name=base.name, command=("/usr/bin/python3", "-B", str(harness)),
            )
            with mock.patch("floati.adapters.headless_template._WORKSPACE_PARENT", parent),\
                 mock.patch("floati.adapters.codex_live._WORKSPACE_PARENT", parent):
                adapter = BoundaryAdapter(profile)
                try:
                    handle = adapter.spawn(item, deadline_seconds=5)
                    bindings = adapter.drive(handle, item, deadline_seconds=5)
                    committed = subprocess.run(
                        ["/usr/bin/git", "show", bindings[0]["sha"] + ":answer.txt"],
                        cwd=item["workspace"], capture_output=True, check=True,
                    )
                    self.assertEqual(b"worker answer\n", committed.stdout)
                    self.assertFalse((Path(item["workspace"]) / ".floati/mutated").exists())
                finally:
                    adapter.cancel()


if __name__ == "__main__":
    unittest.main()
