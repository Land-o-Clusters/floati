"""LOCK-1 — a ledger-lock refusal must name WHICH ledger was contended.

`ledger_lock_timeout` used to render `path.name`, so the tenant `events.jsonl`
and `runs/events.jsonl` produced the identical detail

    events.jsonl.lock lock remained contended for 1 second

and a receipt carrying it could not say which of the two was held. The CI
ruling's own Am.1 evidence quoted the wrong one for exactly that reason.

⇒ A REFUSAL THAT CANNOT NAME ITS SUBJECT SENDS THE NEXT READER TO THE WRONG
FILE, and it does it while looking like a complete answer.

The remedy is the ROOT-RELATIVE coordinate — never the absolute host path. A
refusal detail is carried in a receipt and receipts are exported; an absolute
path publishes the host's own coordinates, and the exporter's redactor removes
only the prefixes it enumerates.
"""

from __future__ import annotations

import ast
import fcntl
import pathlib
import tempfile
import unittest
from pathlib import Path

from floati import jsonl
from floati.errors import ProtocolRefusal
from floati.identity_fence import (
    GOVERNED_TEMP_PREFIXES,
    HOME_PREFIX,
)
from floati.jsonl import _lock_beside, _lock_coordinate, append_record
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


TENANT_LEDGER = Path("events.jsonl")
RUN_LEDGER = Path("runs/events.jsonl")
SHIPPED_PACKAGE = pathlib.Path(__file__).parents[1] / "floati"


class LockRefusalCoordinateTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name) / "fleet", "alpha")

    def record(self, record_id: str) -> dict:
        return {
            "schema_version": 0,
            "id": "registry-018f7e9b3c117abc8def0123456789ab",
            "tenant_id": "alpha",
            "timestamp": "2026-07-31T12:00:00.000Z",
            "kind": "registry_entry",
            "node_id": record_id,
            "role": "worker",
            "state": "active",
        }

    def _hold(self, relative: Path) -> Path:
        """Hold the exclusive lock beside one ledger for the whole test.

        `flock` is bound to the OPEN FILE DESCRIPTION, not to the process, so a
        second independent open in this same process contends exactly as
        another process would. That is what makes this deterministic: no
        thread, no sleep, no race to lose.
        """

        ledger = self.root.resolve_relative(relative)
        lock_path, _coordinate = _lock_beside(ledger, relative)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_path

    def _contended_detail(self, relative: Path) -> str:
        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(
                self.root, relative, self.record("n-1"),
                allowed_kinds={"registry_entry"},
            )
        self.assertEqual("ledger_lock_timeout", caught.exception.code)
        return caught.exception.detail

    def test_the_two_events_ledgers_refuse_with_distinguishable_details(self) -> None:
        """Catches a lock refusal that cannot say which ledger was contended."""
        self._hold(TENANT_LEDGER)
        self._hold(RUN_LEDGER)

        tenant_detail = self._contended_detail(TENANT_LEDGER)
        run_detail = self._contended_detail(RUN_LEDGER)

        self.assertNotEqual(tenant_detail, run_detail)
        self.assertTrue(
            tenant_detail.startswith("events.jsonl.lock "), tenant_detail
        )
        self.assertTrue(
            run_detail.startswith("runs/events.jsonl.lock "), run_detail
        )
        # Both still say what they always said about the contention itself;
        # the coordinate is added, and nothing is taken away.
        for detail in (tenant_detail, run_detail):
            self.assertIn("remained contended for 1 second", detail)

    def test_a_lock_refusal_never_carries_an_absolute_host_path(self) -> None:
        """Catches a receipt-bound refusal publishing the host's coordinates."""
        self._hold(RUN_LEDGER)
        detail = self._contended_detail(RUN_LEDGER)

        self.assertFalse(detail.startswith("/"), detail)
        self.assertNotIn(str(self.temp.name), detail)
        self.assertNotIn(HOME_PREFIX, detail)
        for prefix in GOVERNED_TEMP_PREFIXES:
            self.assertNotIn(prefix, detail)

    def test_the_coordinate_refuses_an_absolute_or_escaping_path(self) -> None:
        """Catches the fence moving from construction to export."""
        for bad in ("/etc/passwd", "../outside/events.jsonl", "", "a/../../b"):
            with self.subTest(candidate=bad):
                with self.assertRaises(ValueError):
                    _lock_coordinate(bad)
        self.assertEqual(
            "runs/events.jsonl", _lock_coordinate(Path("runs/events.jsonl"))
        )
        self.assertEqual("events.jsonl", _lock_coordinate("events.jsonl"))

    def test_lock_and_coordinate_are_derived_from_the_same_pair(self) -> None:
        """Catches the locked file and the printed name drifting apart."""
        ledger = self.root.resolve_relative(RUN_LEDGER)
        lock_path, coordinate = _lock_beside(ledger, RUN_LEDGER)

        self.assertEqual("runs/events.jsonl.lock", coordinate)
        self.assertEqual(ledger.with_name("events.jsonl.lock"), lock_path)
        # The coordinate names the lock RELATIVE TO THE ROOT, so joining it
        # back onto the tenant home must land on the very file that is locked.
        self.assertEqual(lock_path, self.root.resolve_relative(coordinate))

    def test_every_shipped_lock_site_names_its_coordinate(self) -> None:
        """Catches a new `_locked_path` call site reintroducing the ambiguity.

        `relative` carries a default so that test doubles which wrap this
        function with their own fixed signature keep working; the default is
        the basename ambiguity itself, so the default cannot be the fence.
        This is the fence, and it is DERIVED rather than written down: every
        shipped module is parsed and every call site is required to pass the
        argument. A hand-kept list of call sites would go stale the first time
        somebody added one — which is the whole history of this row.
        """

        missing = []
        sites = 0
        for module in sorted(SHIPPED_PACKAGE.rglob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                name = (
                    function.attr
                    if isinstance(function, ast.Attribute)
                    else getattr(function, "id", None)
                )
                if name != "_locked_path":
                    continue
                sites += 1
                if not any(word.arg == "relative" for word in node.keywords):
                    missing.append(
                        "%s:%d" % (module.relative_to(SHIPPED_PACKAGE.parent), node.lineno)
                    )

        self.assertEqual([], missing)
        # A count pin beside the derivation: if a refactor collapsed the call
        # sites to none, the emptiness above would pass while witnessing
        # nothing. This does not pin the exact number — that would red on every
        # ordinary edit — only that the sweep still had a population to sweep.
        self.assertGreaterEqual(sites, 30, sites)


if __name__ == "__main__":
    unittest.main()
