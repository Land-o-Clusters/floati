"""Am.17: a real contended inbox names one typed retry, never retries itself."""

from __future__ import annotations

import tests  # Pin HOME before product imports or fixture children.

import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal, UNNAMED_REMEDY
from floati.events import EventLog
from floati.mcp import McpServer
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.readiness import wait_readable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# Retain the native lock until the invocation returns, including slow CLI
# imports. Cleanup releases that same child even on assertion failure. The
# child has its own bounded fallback and never needs a process signal.
HOLDER_CODE = """
import fcntl, select, sys
with open(sys.argv[1], 'a+b') as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    print('locked', flush=True)
    select.select([sys.stdin], [], [], 120)
"""


class LedgerLockTransientRemedyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        registry = Registry(self.root)
        registry.register("sender", "Codex")
        registry.register("recipient", "Codex")
        EventLog(self.root).send(
            "sender", "recipient", "floati", "a" * 40,
            "docs/evidence/control.md", "control envelope",
        )

    def _release_holder(self, holder: subprocess.Popen) -> None:
        _output, errors = holder.communicate(input="\n", timeout=10)
        self.assertEqual(0, holder.returncode, errors)

    def test_real_inbox_lock_timeout_names_transient_retry_once(self) -> None:
        """Catch a lock timeout being rendered as an unactionable permanent stop."""

        events = self.root.resolve_relative("events.jsonl")
        lock = events.with_name("events.jsonl.lock")
        before = events.read_bytes()
        lock_before = lock.read_bytes()
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER_CODE, str(lock)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=self.base,
        )
        self.addCleanup(self._release_holder, holder)
        wait_readable(holder.stdout, "scratch holder must signal lock ownership")
        self.assertEqual("locked\n", holder.stdout.readline())
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(REPOSITORY_ROOT), environment.get("PYTHONPATH")) if part
        )
        completed = subprocess.run(
            [sys.executable, "-m", "floati", "inbox", "--root", str(self.root.path),
             "--as", "recipient", "--peek"],
            cwd=self.base, env=environment, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(20, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        self.assertEqual(1, len(completed.stdout.splitlines()))
        artifact = json.loads(completed.stdout)
        self.assertEqual("refused", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual("ledger_lock_timeout", evidence["code"])
        self.assertEqual(
            "events.jsonl.lock lock remained contended for 1 second", evidence["detail"]
        )
        self.assertEqual({
            "kind": "transient", "action": "retry_once", "delay_seconds": 1,
            "holder": "unknown",
        }, evidence["remedy"])
        self.assertEqual(before, events.read_bytes())
        self.assertEqual(lock_before, lock.read_bytes(), "ordinary lock bytes stay opaque")
        self.assertFalse(self.root.resolve_relative("receipts/deliveries/recipient.jsonl").exists())

    def test_binder_accepts_closed_transient_shape_without_aliasing_caller(self) -> None:
        """Catch typed delay/holder evidence being discarded or aliased."""

        supplied = {
            "kind": "transient", "action": "retry_once", "delay_seconds": 1,
            "holder": "fixture-holder:7",
        }
        refusal = ProtocolRefusal("ledger_lock_timeout", "contended", remedy=supplied)
        self.assertEqual({
            "kind": "transient", "action": "retry_once", "delay_seconds": 1,
            "holder": "fixture-holder:7",
        }, refusal.remedy)
        supplied["delay_seconds"] = 0
        self.assertEqual(1, refusal.remedy["delay_seconds"])
        boundary = dict(supplied, delay_seconds=3600, holder="a" * 128)
        self.assertEqual(boundary, ProtocolRefusal(
            "ledger_lock_timeout", "contended", remedy=boundary
        ).remedy)

    def test_mcp_keeps_the_typed_retry_evidence(self) -> None:
        remedy = {
            "kind": "transient", "action": "retry_once", "delay_seconds": 1,
            "holder": "unknown",
        }
        server = McpServer(str(self.root.path), "recipient", "scratch-session")
        reshaped = server._reshape_refusal({
            "status": "refused",
            "evidence": {
                "code": "ledger_lock_timeout", "detail": "events.jsonl.lock contended",
                "remedy": ProtocolRefusal("ledger_lock_timeout", "contended", remedy).remedy,
            },
        }, ("inbox",))
        self.assertEqual(remedy, reshaped["evidence"]["remedy"])
        self.assertIs(type(reshaped["evidence"]["remedy"]["delay_seconds"]), int)

    def test_malformed_transient_shapes_and_policy_codes_stay_unnamed(self) -> None:
        """Catch an arbitrary dictionary or policy refusal being labeled transient."""

        valid = {
            "kind": "transient", "action": "retry_once", "delay_seconds": 1,
            "holder": "unknown",
        }
        malformed = [
            {}, {key: value for key, value in valid.items() if key != "holder"},
            dict(valid, extra="unreviewed"), dict(valid, kind="other"),
            dict(valid, action="retry_forever"),
        ]
        malformed.extend(dict(valid, delay_seconds=value) for value in (
            True, False, 0, -1, 3601, 1.0, "1", None,
        ))
        malformed.extend(dict(valid, holder=value) for value in (
            "", "/private/path", "bad\nline", "Uppercase", "é", "a" * 129, 3, {},
        ))
        for remedy in malformed:
            with self.subTest(remedy=remedy):
                refusal = ProtocolRefusal("ledger_lock_timeout", "contended", remedy=remedy)
                self.assertEqual(UNNAMED_REMEDY, refusal.remedy)
        refusal = ProtocolRefusal("workspace_identity_mismatch", "policy", remedy=valid)
        self.assertEqual(UNNAMED_REMEDY, refusal.remedy)

    def test_existing_text_and_explicit_none_remedies_stay_unchanged(self) -> None:
        self.assertEqual("correct --root", ProtocolRefusal(
            "workspace_identity_mismatch", "policy", remedy="correct --root"
        ).remedy)
        self.assertEqual({"kind": "none", "why": "operator review required"}, ProtocolRefusal(
            "workspace_identity_mismatch", "policy",
            remedy={"kind": "none", "why": "operator review required"},
        ).remedy)
