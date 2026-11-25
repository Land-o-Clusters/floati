"""Execute the native plugin transport with a real, private Python receiver.

Changing the native event, cwd, fixed Python argv, or pipe ownership breaks
these properties. Ledger authentication is covered by the desktop observer tests.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.managed_test_tools import require_node

BRIDGE = Path(__file__).resolve().parents[1] / "scripts/floati-opencode-observer.mjs"


class OpenCodeHostBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bank = tempfile.TemporaryDirectory(prefix="opencode-bridge-")
        self.addCleanup(self.bank.cleanup)
        self.home = Path(self.bank.name).resolve()
        self.workspace = self.home / "native workspace"
        self.workspace.mkdir()
        self.root = self.home / "explicit bus root"
        self.root.mkdir()
        self.observer = self.home / "observer.py"
        self.record = self.observer.with_suffix(".jsonl")
        self.observer.write_text(
            "import json, os, pathlib, sys\n"
            "payload = json.load(sys.stdin)\n"
            "with pathlib.Path(__file__).with_suffix('.jsonl').open('a') as out:\n"
            "    out.write(json.dumps({'argv': sys.argv, 'cwd': os.getcwd(), "
            "'payload': payload, 'bytecode': sys.dont_write_bytecode}) + '\\n')\n"
            "print('{\\\"permissionDecision\\\":\\\"allow\\\"}')\n",
            encoding="utf-8")

    def event(self, kind="permission.asked"):
        properties = {"sessionID": "ses_native_123"}
        if kind == "permission.asked":
            properties.update(id="per_native_123", permission="bash",
                patterns=["command is data; $(never-execute)"], metadata={}, always=[])
        return {"id": "evt_native_123", "type": kind, "properties": properties}

    def run_bridge(self, events, *, root=None, observer=None, context=None, concurrent=False, dispose=False):
        self.assertTrue(BRIDGE.is_file(), "repository-owned OpenCode event bridge is absent")
        node = require_node(self)
        config = {"root": str(self.root) if root is None else root,
            "observerPath": str(self.observer) if observer is None else observer}
        context = context or {"directory": str(self.workspace), "worktree": str(self.workspace)}
        if dispose:
            dispatch = (
                "if (typeof hooks.dispose !== 'function') throw Error('native lifecycle drain absent');\n"
                "const pending = " + json.dumps(events) + ".map(event => hooks.event({event}));\n"
                "await hooks.dispose();\n"
                "if (readFileSync(" + json.dumps(str(self.record)) + ", 'utf8').trim().split('\\n').length !== "
                + str(len(events)) + ") throw Error('dispose returned before receiver completion');\n"
                "await hooks.event({event: " + json.dumps(events[0]) + "});\n"
                "await Promise.all(pending);\n")
        else:
            dispatch = (
                "await Promise.all(" + json.dumps(events) + ".map(async (event) => {\n"
                if concurrent else "for (const event of " + json.dumps(events) + ") {\n")
            dispatch += (
                "  if (await hooks.event({event}) !== undefined) throw Error('host decision returned');\n"
                + ("}));\n" if concurrent else "}\n"))
        script = (
            "import {createFloatiHostObserver} from " + json.dumps(BRIDGE.as_uri()) + ";\n"
            "import {readFileSync} from 'node:fs';\n"
            "const hooks = await createFloatiHostObserver(" + json.dumps(config) + ")(" + json.dumps(context) + ");\n"
            "if (Object.keys(hooks).some(key => !['event', 'dispose'].includes(key))) throw Error('decision hook exposed');\n"
            + dispatch)
        env = dict(os.environ, HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1")
        # Keep generated code and events out of execve's bounded argv strings.
        return subprocess.run([node, "--input-type=module"], input=script,
            cwd=self.workspace, env=env, capture_output=True, text=True, timeout=12)

    def records(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def test_native_permission_and_idle_are_exact_input_without_stdout_decisions(self):
        events = [self.event(), self.event("session.idle")]
        completed = self.run_bridge(events)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        rows = self.records()
        self.assertEqual(len(rows), 2)
        for row, event in zip(rows, events):
            self.assertEqual(row["argv"], [str(self.observer), "--root", str(self.root), "--harness", "opencode"])
            self.assertEqual(row["cwd"], str(self.workspace))
            self.assertEqual(row["bytecode"], True)
            self.assertEqual(row["payload"], {"cwd": str(self.workspace), "worktree": str(self.workspace), "event": event})

    def test_unrelated_events_never_launch_observer(self):
        completed = self.run_bridge([self.event("permission.replied"), self.event("message.updated")])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(self.records(), [])

    def test_fire_and_forget_native_events_finish_in_callback_order(self):
        self.observer.write_text(
            "import json, pathlib, sys, time\n"
            "payload = json.load(sys.stdin)\n"
            "if payload['event']['type'] == 'permission.asked':\n"
            "    time.sleep(0.6)\n"
            "with pathlib.Path(__file__).with_suffix('.jsonl').open('a') as out:\n"
            "    out.write(json.dumps(payload['event']) + '\\n')\n")
        completed = self.run_bridge([self.event(), self.event("session.idle")], concurrent=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual([row["type"] for row in self.records()], ["permission.asked", "session.idle"],
            "native fire-and-forget callbacks must publish in their original invocation order")

    def test_native_dispose_drains_pending_callbacks_and_rejects_later_events(self):
        self.observer.write_text(
            "import json, pathlib, sys, time\n"
            "payload = json.load(sys.stdin)\n"
            "time.sleep(0.2)\n"
            "with pathlib.Path(__file__).with_suffix('.jsonl').open('a') as out:\n"
            "    out.write(json.dumps(payload['event']) + '\\n')\n")
        completed = self.run_bridge([self.event(), self.event("session.idle")], dispose=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual([row["type"] for row in self.records()], ["permission.asked", "session.idle"],
            "dispose must drain the callback queue and stop any later child launch")

    def test_large_event_reaches_bridge_without_oversized_exec_arguments(self):
        # Linux MAX_ARG_STRLEN is 32 pages, including the terminating NUL.
        # Apply that rule with the local page size even on macOS, so the
        # transport regression fails here before a Linux execve would fail.
        max_arg_bytes = 32 * os.sysconf("SC_PAGESIZE")
        event = self.event()
        event["properties"]["metadata"] = {"large": "x" * max_arg_bytes}
        run = subprocess.run

        def bounded_run(argv, **kwargs):
            for index, argument in enumerate(argv):
                size = len(os.fsencode(argument)) + 1
                self.assertLessEqual(size, max_arg_bytes,
                    f"Node argv[{index}] exceeds Linux's 32-page single-argument bound")
            return run(argv, **kwargs)

        with patch.object(subprocess, "run", side_effect=bounded_run):
            completed = self.run_bridge([event])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertTrue(completed.stderr, "oversized input must emit its refusal artifact")
        self.assertEqual(json.loads(completed.stderr), {
            "status": "no_result", "source": "desktop_host", "harness": "opencode",
            "code": "opencode_observer_payload_oversized"})
        self.assertEqual(self.records(), [])

    def test_root_must_be_an_explicit_absolute_directory(self):
        completed = self.run_bridge([self.event()], root="relative-root")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.records(), [])

    def test_observer_symlink_is_not_executed(self):
        link = self.home / "linked-observer.py"
        link.symlink_to(self.observer)
        completed = self.run_bridge([self.event()], observer=str(link))
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.records(), [])

    def test_observer_nonzero_exit_cannot_change_host_outcome(self):
        self.observer.write_text("import sys\nprint('deny')\nsys.exit(20)\n")
        completed = self.run_bridge([self.event()])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_receiver_failure_releases_the_next_native_callback(self):
        self.observer.write_text(
            "import json, pathlib, sys\n"
            "payload = json.load(sys.stdin)\n"
            "if payload['event']['type'] == 'permission.asked':\n"
            "    sys.exit(20)\n"
            "with pathlib.Path(__file__).with_suffix('.jsonl').open('a') as out:\n"
            "    out.write(json.dumps(payload['event']) + '\\n')\n")
        completed = self.run_bridge([self.event(), self.event("session.idle")], concurrent=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual([row["type"] for row in self.records()], ["session.idle"])

    def test_timeout_waits_for_owned_receiver_cleanup(self):
        marker = self.home / "terminated"
        self.observer.write_text(
            "import pathlib, signal, sys, time\n"
            "def finish(*args):\n"
            "    pathlib.Path(" + repr(str(marker)) + ").write_text('closed')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, finish)\n"
            "sys.stdin.read()\n"
            "time.sleep(30)\n")
        start = time.monotonic()
        completed = self.run_bridge([self.event()])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(marker.read_text(), "closed")
        self.assertLess(time.monotonic() - start, 10)
