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


# LOCK-2. Every function in the shipped tree that takes an advisory lock ITSELF,
# with the reason it does not route through the shared ledger lock. The ledger
# lock is `jsonl._locked_path`; every other entry is a DIFFERENT lock class with
# its own timeout, ordering domain and refusal vocabulary.
#
# This is an allowlist and not a prohibition because 12 of these are legitimate:
# a blanket "only _locked_path may flock" would red on eleven correct call sites
# and teach the next reader to disable the check. What it buys is that a NEW
# lock implementation cannot appear in silence — the author must either route it
# through the shared helper or come here and say why not.
#
# `update_consent.py::_locked` is the entry that is NOT here: it used to be, and
# LOCK-2 routed it through `_locked_path`. Its absence is this row's regression
# guard.
_ADVISORY_LOCK_SITES = {
    ("floati/jsonl.py", "_locked_path"):
        "THE shared ledger lock; the one LOCK-1's coordinate census fences",
    ("floati/bus_epoch.py", "_acquire_epoch_descriptor"):
        "epoch descriptor, held across a roll rather than one transaction",
    ("floati/bus_epoch.py", "epoch_guard"):
        "the epoch guard that _locked_path itself defers to for lock ORDER",
    ("floati/planes.py", "_cas_lock"):
        "outer CAS lock; its own cas_lock_timeout vocabulary and budget",
    ("floati/planes.py", "_existing_cas_read_lock"):
        "non-blocking probe of a CAS lock, never a wait",
    ("floati/cli.py", "_existing_lock_is_held"):
        "non-blocking liveness probe of somebody else's lock file",
    ("floati/fleet_update_receipts.py", "acquire"):
        "update-receipt lease held ACROSS calls, not scoped to one block",
    ("floati/fleet_update_receipts.py", "release"):
        "the release half of that same cross-call lease",
    ("floati/wake_daemon.py", "acquire"):
        "daemon pidfile lease held for the process lifetime",
    ("floati/wake_daemon.py", "release"):
        "the release half of the daemon pidfile lease",
    ("floati/wake_control.py", "_lock"):
        "wake control file, outside every ledger plane",
    ("floati/seat_declaration.py", "__init__"):
        "seat declaration lease taken in a constructor, released on close",
}


class AdvisoryLockImplementationCensusTests(unittest.TestCase):
    """LOCK-2 — a second lock implementation must not appear in silence."""

    maxDiff = None

    @staticmethod
    def _lock_taking_functions() -> set:
        found = set()
        for module in sorted(SHIPPED_PACKAGE.rglob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for call in ast.walk(node):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr in ("flock", "lockf")
                    ):
                        found.add(
                            (
                                module.relative_to(SHIPPED_PACKAGE.parent).as_posix(),
                                node.name,
                            )
                        )
        return found

    def test_every_advisory_lock_implementation_is_declared(self) -> None:
        """Catches a second lock implementation the coordinate fence cannot see."""
        observed = self._lock_taking_functions()
        declared = set(_ADVISORY_LOCK_SITES)

        undeclared = sorted(observed - declared)
        self.assertEqual(
            [], undeclared,
            "a new advisory lock implementation appeared. Route it through "
            "floati.jsonl._locked_path so its refusal carries a root-relative "
            "coordinate and LOCK-1's census can see it, or declare it in "
            "_ADVISORY_LOCK_SITES with the reason it is a different lock class.",
        )
        # Both directions: a declaration whose site is gone is a stale reason,
        # and a stale reason is how an allowlist becomes a place names go to die.
        self.assertEqual([], sorted(declared - observed))

    def test_the_update_consent_ledger_no_longer_locks_for_itself(self) -> None:
        """Catches LOCK-2 being reverted: the second implementation returning."""
        observed = self._lock_taking_functions()

        self.assertNotIn(("floati/update_consent.py", "_locked"), observed)
        self.assertIn(("floati/jsonl.py", "_locked_path"), observed)
        # And the module must not even import the lock primitive any more: a
        # lingering `import fcntl` keeps it READING like a lock implementation.
        source = (SHIPPED_PACKAGE / "update_consent.py").read_text(encoding="utf-8")
        self.assertNotIn("import fcntl", source)


if __name__ == "__main__":
    unittest.main()
