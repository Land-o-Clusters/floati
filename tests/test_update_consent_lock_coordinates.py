"""LOCK-2 — the update-consent ledger's refusals may not carry a host path.

`floati/update_consent.py` carried its OWN `fcntl.flock` loop, so LOCK-1's
coordinate census — which sweeps `_locked_path` call sites — could not see it,
and its refusals interpolated the ABSOLUTE path:

    update_ledger_lock_timeout: update ledger lock remained contended at
                                /<destination>/.floati-install/…jsonl.lock

⇒ A SECOND IMPLEMENTATION OF A FENCED MECHANISM IS A HOLE IN THE FENCE, AND THE
FENCE CANNOT REPORT IT — the census returned "0 missing" while this module
locked, leaked, and was never asked.

**The exposure is traced, not assumed.** `floati/doctor.py` catches BOTH
`IntegrityFailure` and `ProtocolRefusal` from `project_update_findings` and
copies `exc.detail` verbatim into a finding; the doctor artifact is one of
`support_bundle.derive_collectors`' collectors; and the bundle's `_scrub_string`
and `identity_gate` know only the governed home and temp prefixes. A destination
on a shape they have not met survives redaction AND leaves the fence quiet.

So the absence is asserted with the FENCE'S OWN PREDICATE — `identity_gate`,
the exact gate a bundle must pass — and, because that gate is blind to path
shapes it does not enumerate, with a structural check derived from the fixture
rather than from a spelled string.
"""

from __future__ import annotations

import fcntl
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.identity_fence import GOVERNED_TEMP_PREFIXES, HOME_PREFIX
from floati.support_bundle import _scrub_string, identity_gate
from floati.update_consent import (
    CONSENT_LEDGER,
    INSTALL_DIRECTORY,
    UpdateConsentLedger,
    _install_coordinate,
)
from tests.temp_roots import REAL_TEMP_ROOT


EXPECTED_LOCK_COORDINATE = INSTALL_DIRECTORY + "/" + CONSENT_LEDGER + ".lock"


class UpdateConsentLockCoordinateTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / "install"
        (self.destination / INSTALL_DIRECTORY).mkdir(parents=True)
        self.ledger = UpdateConsentLedger(self.destination)

    def _hold_the_lock(self) -> Path:
        """Hold the consent ledger's lock exactly as a second process would.

        `flock` binds to the OPEN FILE DESCRIPTION rather than the process, so a
        second independent open here contends the same way another process does
        — deterministic, with no thread and no sleep.
        """

        lock_path = self.ledger.path.with_name(self.ledger.path.name + ".lock")
        handle = lock_path.open("a+b")
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_path

    def _contended_refusal(self) -> ProtocolRefusal:
        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger._rows()
        return caught.exception

    def test_the_contended_refusal_names_the_destination_relative_coordinate(
        self,
    ) -> None:
        """Catches the update ledger's lock refusal naming a host path."""
        self._hold_the_lock()
        refusal = self._contended_refusal()

        self.assertEqual("ledger_lock_timeout", refusal.code)
        self.assertTrue(
            refusal.detail.startswith(EXPECTED_LOCK_COORDINATE + " "),
            refusal.detail,
        )

    def test_the_contended_refusal_passes_the_support_bundle_fence(self) -> None:
        """Catches a host coordinate reaching an exported support bundle."""
        self._hold_the_lock()
        detail = self._contended_refusal().detail

        # The fence's OWN predicate, not a string spelled here: this is the
        # exact gate every support bundle must pass before it leaves the host.
        identity_gate(detail.encode("utf-8"))
        # And nothing for the scrubber to remove, which is the stronger claim:
        # `identity_gate` passing could also mean "the redactor already ran".
        self.assertEqual(detail, _scrub_string(detail))

    def test_the_refusal_carries_no_absolute_path_on_any_platform(self) -> None:
        """Controls the fence: it is blind to shapes it does not enumerate.

        `identity_gate` knows the governed home and temp prefixes only. A CI
        runner whose temporary root is neither — `/home/runner/work/_temp` is
        under no governed prefix — would let an absolute path through the test
        above and score a pass for the wrong reason. So the absence is also
        asserted structurally, against paths derived from THIS fixture.
        """

        self._hold_the_lock()
        detail = self._contended_refusal().detail

        self.assertNotIn(str(self.destination), detail)
        self.assertNotIn(str(self.ledger.path), detail)
        self.assertNotIn(str(REAL_TEMP_ROOT), detail)
        # No component of the detail may begin at the filesystem root.
        self.assertFalse(
            any(word.startswith("/") for word in detail.split()), detail
        )

    def test_the_governed_prefixes_would_have_caught_the_old_detail(self) -> None:
        """Controls the instrument: prove the gate fires on the old shape.

        A gate that never fires cannot witness anything. This builds the detail
        the module used to emit and requires the fence to REFUSE it, so the
        passing assertions above mean the leak is gone rather than the gate
        being asleep on this host.
        """

        lock_path = self._hold_the_lock()
        old_shape = f"update ledger lock remained contended at {lock_path}"

        governed = [p for p in (HOME_PREFIX, *GOVERNED_TEMP_PREFIXES)
                    if str(lock_path).startswith(p)]
        if not governed:
            # On a host whose temp root is ungoverned the fence genuinely
            # cannot see this, which is the very reason the structural test
            # above exists. Assert THAT, rather than skipping the question.
            self.assertTrue(str(lock_path).startswith("/"), lock_path)
            self.assertNotEqual(old_shape, _scrub_string(old_shape) or None)
            return
        with self.assertRaises(ProtocolRefusal) as caught:
            identity_gate(old_shape.encode("utf-8"))
        self.assertEqual("snapshot_identity_fence_failed", caught.exception.code)

    def test_the_coordinate_is_relative_by_construction(self) -> None:
        """Catches the coordinate being derived by subtraction instead."""
        coordinate = _install_coordinate(self.ledger.path)

        self.assertEqual(INSTALL_DIRECTORY + "/" + CONSENT_LEDGER, coordinate)
        self.assertFalse(coordinate.startswith("/"))
        # It must name the very file, relative to the destination.
        self.assertEqual(self.ledger.path, self.destination / coordinate)
        # Even handed an absolute path it cannot return one: the constant and
        # the bare basename are joined, so there is no leaking branch.
        self.assertFalse(_install_coordinate(Path("/etc/passwd")).startswith("/"))


if __name__ == "__main__":
    unittest.main()
