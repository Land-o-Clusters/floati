"""LEDGER-1 (a): the supervisor-managed daemon logs are BOUNDED.

launchd/systemd append the daemon's stderr to
``state/wake-daemon/logs/<digest>.stderr.log`` forever — measured on the
live root: 7.1 MB of one refusal line per cycle from a circuit-open
daemon, never rotated. The daemon's serve loop maintains its own logs:
copytruncate rotation past a size threshold, a bounded number of
retained rotations, and one typed receipt per rotation under the root's
``receipts/`` plane. A log failure never wedges the turn.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT

_DIGEST = "18fa5abb06ddfd9624265fa02776867ccb451f9ff7c4a3cfca5fb642a74d36e8"
_REFUSAL_LINE = (
    "wake daemon cycle refused: circuit open "
    "wake_daemon_budget_exhausted backoff 80\n"
)


class WakeDaemonLogRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)
        self.logs_dir = self.root_path / "state" / "wake-daemon" / "logs"
        self.logs_dir.mkdir(parents=True)
        self.stderr_log = self.logs_dir / f"{_DIGEST}.stderr.log"

    def _maintain(self, **kwargs):
        from floati.wake_daemon import maintain_supervisor_logs

        return maintain_supervisor_logs(self.root, _DIGEST, **kwargs)

    def test_oversized_stderr_log_rotates_with_a_receipt(self) -> None:
        """RED: nothing bounds the supervisor log today."""

        self.stderr_log.write_text(_REFUSAL_LINE * 100, encoding="utf-8")
        oversized = self.stderr_log.stat().st_size

        receipts = self._maintain(max_bytes=1024, retain=2)

        self.assertEqual(1, len(receipts), receipts)
        receipt = receipts[0]
        self.assertEqual(0, receipt["schema_version"])
        self.assertEqual("wake_daemon_log_rotation", receipt["kind"])
        self.assertEqual(_DIGEST, receipt["coordinate_digest"])
        self.assertEqual("stderr", receipt["stream"])
        self.assertGreaterEqual(receipt["rotated_bytes"], oversized - len(_REFUSAL_LINE))
        self.assertLessEqual(
            self.stderr_log.stat().st_size, len(_REFUSAL_LINE),
            "the active log must be truncated at rotation",
        )
        rotated = self.root_path / receipt["rotated_to"]
        self.assertTrue(rotated.is_file(), receipt["rotated_to"])
        self.assertEqual(
            _REFUSAL_LINE * 100,
            rotated.read_text(encoding="utf-8"),
            "the rotated file carries the exact pre-rotation bytes",
        )
        receipt_file = self.root_path / receipt["receipt_path"]
        self.assertTrue(receipt_file.is_file(), receipt["receipt_path"])
        on_disk = json.loads(receipt_file.read_text(encoding="utf-8"))
        self.assertEqual(receipt, on_disk)

    def test_refusing_daemon_cycles_stay_bounded(self) -> None:
        """The row's RED, behaviorally: a daemon refusing every cycle
        must not grow its logs without limit."""

        max_bytes, retain, cycles = 1024, 2, 50
        seen_receipts: list[dict] = []
        for _ in range(cycles):
            with self.stderr_log.open("a", encoding="utf-8") as log:
                log.write(_REFUSAL_LINE * 8)
            seen_receipts.extend(
                self._maintain(max_bytes=max_bytes, retain=retain)
            )

        every_log = sorted(self.logs_dir.glob(f"{_DIGEST}.*.log"))
        total = sum(path.stat().st_size for path in every_log)
        self.assertLess(
            total,
            max_bytes * (retain + 2),
            f"the log estate grew without limit: {every_log}",
        )
        rotated_stderr = list(self.logs_dir.glob(f"{_DIGEST}.stderr.*.log"))
        self.assertLessEqual(
            len(rotated_stderr), retain, "only the retained rotations survive"
        )
        self.assertGreaterEqual(
            len(seen_receipts), 1, "the cycles crossed the threshold at least once"
        )
        for receipt in seen_receipts:
            self.assertTrue(
                (self.root_path / receipt["receipt_path"]).is_file()
            )

    def test_under_threshold_log_is_untouched(self) -> None:
        self.stderr_log.write_text(_REFUSAL_LINE, encoding="utf-8")

        receipts = self._maintain(max_bytes=1024, retain=2)

        self.assertEqual([], receipts)
        self.assertEqual(
            _REFUSAL_LINE, self.stderr_log.read_text(encoding="utf-8")
        )
        self.assertEqual(
            [], sorted(self.logs_dir.glob(f"{_DIGEST}.stderr.*.log"))
        )


if __name__ == "__main__":
    unittest.main()


class RotationTakeoverTests(unittest.TestCase):
    """LEDGER-1 (a) Am.1: rename+dup2, receipt BEFORE takeover.

    Copytruncate loses lines: every O_APPEND write inside the read ->
    truncate window exists in NEITHER file (measured: a 228-line span).
    The daemon IS the writer of its own fd 1/2 — rotation renames the
    live file (the launchd-inherited descriptor follows the inode),
    opens a fresh live file, and dup2's it onto the daemon's own
    descriptor. The receipt is written BEFORE the takeover: a rotation
    without its receipt must be impossible.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)
        self.logs_dir = self.root_path / "state" / "wake-daemon" / "logs"
        self.logs_dir.mkdir(parents=True)
        self.stderr_log = self.logs_dir / (
            "18fa5abb06ddfd9624265fa02776867ccb451f9ff7c4a3cfca5fb642a74d36e8"
            ".stderr.log"
        )

    def _digest(self) -> str:
        return self.stderr_log.name[: -len(".stderr.log")]

    def _maintain(self, **kwargs):
        from floati.wake_daemon import maintain_supervisor_logs

        return maintain_supervisor_logs(self.root, self._digest(), **kwargs)

    def test_hammer_writer_loses_zero_bytes_across_rotations(self) -> None:
        """RED: a hammer O_APPEND writer across one rotation lost a
        contiguous span to the read -> truncate window."""

        import os
        import threading

        # A 16 MB live file makes the read -> truncate window tens of
        # milliseconds wide: a hammering O_APPEND writer loses a
        # measurable span inside it under copytruncate, and NOTHING
        # under rename (the descriptor follows the inode).
        seed = b"0123456789" * 10 + b"\n"
        with self.stderr_log.open("wb") as log:
            for _ in range(160_000):
                log.write(seed)
        hammer_lines = 200_000
        written = {"count": 0}
        stop = threading.Event()

        def hammer() -> None:
            descriptor = os.open(self.stderr_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                while not stop.is_set() and written["count"] < hammer_lines:
                    os.write(descriptor, seed)
                    written["count"] += 1
            finally:
                os.close(descriptor)

        worker = threading.Thread(target=hammer)
        worker.start()
        try:
            self._maintain(max_bytes=1024 * 1024, retain=3)
        finally:
            stop.set()
            worker.join()

        produced = sum(
            path.stat().st_size
            for path in self.logs_dir.glob(self.stderr_log.name.replace(".stderr.log", "*.log"))
        )
        expected = (160_000 + min(written["count"], hammer_lines)) * 101
        self.assertEqual(
            expected, produced,
            f"lost {expected - produced} bytes across the rotations",
        )

    def test_unwritable_receipts_dir_aborts_without_touching_the_log(self) -> None:
        """RED: the old order truncated FIRST and wrote the receipt after
        — an unwritable receipts plane cost a rotation with no receipt."""

        import os

        from floati.errors import ProtocolRefusal

        self.stderr_log.write_text("line\n" * 500, encoding="utf-8")
        before = self.stderr_log.read_bytes()
        receipts_dir = self.root_path / "receipts"
        receipts_dir.mkdir()
        receipts_dir.chmod(0o500)

        try:
            with self.assertRaises(ProtocolRefusal) as raised:
                self._maintain(max_bytes=256, retain=2)
            self.assertEqual(
                "wake_daemon_log_receipt_unwritable", raised.exception.code
            )
        finally:
            receipts_dir.chmod(0o700)

        self.assertEqual(before, self.stderr_log.read_bytes())
        self.assertEqual([], list(self.logs_dir.glob("*.1*.log")))


class SameSecondRotationTests(unittest.TestCase):
    """LEDGER-1 (a) Am.2: no rotation may destroy a rotated file.

    The Am.1 rotated filename used a SECOND-resolution stamp, so two
    rotations of one stream inside one wall-clock second collided and
    os.replace silently overwrote the first rotation — the loss moved
    from the live file into the rotated set, and earlier receipts named
    bytes that no longer exist. Am.2: the rotated name carries the same
    MILLISECOND stamp the receipt uses plus a monotonic per-stream
    counter, an existing target refuses instead of overwriting, and
    every receipt resolves to bytes whose digest matches.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)
        self.logs_dir = self.root_path / "state" / "wake-daemon" / "logs"
        self.logs_dir.mkdir(parents=True)
        self.stderr_log = self.logs_dir / (
            "18fa5abb06ddfd9624265fa02776867ccb451f9ff7c4a3cfca5fb642a74d36e8"
            ".stderr.log"
        )

    def _digest(self) -> str:
        return self.stderr_log.name[: -len(".stderr.log")]

    def _maintain(self, **kwargs):
        from floati.wake_daemon import maintain_supervisor_logs

        return maintain_supervisor_logs(self.root, self._digest(), **kwargs)

    def test_four_same_second_rotations_keep_four_true_receipts(self) -> None:
        """RED: same-second rotations collapsed into one surviving file
        whose bytes matched only the last receipt."""

        import hashlib

        receipts: list[dict] = []
        payloads: list[bytes] = []
        for index in range(4):
            payload = (b"%d-" % index) + b"x" * 2048
            payloads.append(payload)
            with self.stderr_log.open("ab") as log:
                log.write(payload)
            receipts.extend(self._maintain(max_bytes=1024, retain=64))

        self.assertEqual(4, len(receipts), receipts)
        rotated_files = sorted(
            self.logs_dir.glob(
                self.stderr_log.name.replace(".stderr.log", ".stderr.*.log")
            )
        )
        self.assertEqual(
            4, len(rotated_files),
            f"same-second rotations collapsed: {rotated_files}",
        )
        for receipt, payload in zip(receipts, payloads):
            rotated = self.root_path / receipt["rotated_to"]
            self.assertTrue(rotated.is_file(), receipt["rotated_to"])
            self.assertEqual(receipt["rotated_bytes"], len(payload))
            self.assertEqual(
                receipt["rotated_sha256"],
                hashlib.sha256(rotated.read_bytes()).hexdigest(),
                "every receipt must resolve to bytes its digest matches",
            )
            self.assertNotEqual(
                hashlib.sha256(payload).hexdigest(),
                hashlib.sha256(b"").hexdigest(),
            )

    def test_existing_rotation_target_refuses_instead_of_overwriting(self) -> None:
        from floati.errors import ProtocolRefusal
        from floati import wake_daemon

        pinned_stamp = "2026-09-06T01:02:03.004Z"
        self.stderr_log.write_bytes(b"a" * 2048)
        wake_daemon._ROTATION_COUNTER.clear()
        self._maintain(max_bytes=1024, retain=64, stamp=pinned_stamp)
        wake_daemon._ROTATION_COUNTER.clear()

        with self.stderr_log.open("ab") as log:
            log.write(b"b" * 2048)
        # Same pinned stamp + a reset counter recomputes the SAME target
        # name; the fence must refuse rather than overwrite it.
        with self.assertRaises(ProtocolRefusal) as raised:
            self._maintain(max_bytes=1024, retain=64, stamp=pinned_stamp)
        self.assertEqual("wake_daemon_log_rotation_exists", raised.exception.code)
        self.assertIsNotNone(raised.exception.remedy)

        rotations = list(
            self.logs_dir.glob(
                self.stderr_log.name.replace(".stderr.log", ".stderr.*.log")
            )
        )
        self.assertEqual(1, len(rotations), "no file was overwritten")
        self.assertTrue(all(path.stat().st_size == 2048 for path in rotations))


class RotationPruneReceiptTests(unittest.TestCase):
    """LEDGER-1 (a) Am.3: retention never leaves a dangling name.

    The Am.2 read's ONE finding: _prune_rotations unlinked the rotated
    files the receipts still name — after retain=2 prunes, rotation
    receipts carry rotated_to and rotated_sha256 for bytes that are gone,
    and the prune writes nothing. Each prune now writes one typed receipt
    (kind wake_daemon_log_prune) naming the pruned file, the digest of
    the bytes actually destroyed, and the retaining rule — written BEFORE
    the unlink, so a prune without its receipt must be impossible.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)
        self.logs_dir = self.root_path / "state" / "wake-daemon" / "logs"
        self.logs_dir.mkdir(parents=True)
        self.stderr_log = self.logs_dir / (
            "18fa5abb06ddfd9624265fa02776867ccb451f9ff7c4a3cfca5fb642a74d36e8"
            ".stderr.log"
        )

    def _digest(self) -> str:
        return self.stderr_log.name[: -len(".stderr.log")]

    def _maintain(self, **kwargs):
        from floati.wake_daemon import maintain_supervisor_logs

        return maintain_supervisor_logs(self.root, self._digest(), **kwargs)

    def _receipts(self, plane: str) -> list:
        directory = self.root_path / "receipts" / plane / self._digest()
        if not directory.is_dir():
            return []
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))
        ]

    def test_every_pruned_rotation_is_recorded(self) -> None:
        """RED: prunes were silent — six rotations with retain=2 leave
        four rotation receipts naming bytes that no longer exist."""

        for index in range(6):
            with self.stderr_log.open("ab") as log:
                log.write(b"%d-" % index + b"x" * 2048)
            self._maintain(max_bytes=1024, retain=2)

        rotations = self._receipts("wake-daemon-log-rotation")
        prunes = self._receipts("wake-daemon-log-prune")
        self.assertEqual(6, len(rotations), rotations)
        self.assertEqual(4, len(prunes), prunes)
        pruned_by_path = {row["pruned_path"]: row for row in prunes}
        dangling = [
            row for row in rotations
            if not (self.root_path / row["rotated_to"]).is_file()
        ]
        self.assertEqual(4, len(dangling), "the fixture pruned nothing")
        for row in dangling:
            prune = pruned_by_path.get(row["rotated_to"])
            self.assertIsNotNone(
                prune,
                f"retention unlinked {row['rotated_to']} without a receipt",
            )
            assert prune is not None
            self.assertEqual("wake_daemon_log_prune", prune["kind"])
            self.assertEqual(0, prune["schema_version"])
            self.assertEqual(self._digest(), prune["coordinate_digest"])
            self.assertEqual("stderr", prune["stream"])
            self.assertEqual(
                row["rotated_sha256"], prune["pruned_sha256"],
                "the prune receipt must name the bytes it destroyed",
            )
            self.assertEqual(row["rotated_bytes"], prune["pruned_bytes"])
            self.assertEqual(
                {"kind": "retain_newest", "retain": 2}, prune["retaining_rule"]
            )
            self.assertEqual(row["id"], prune["rotated_receipt"])
            self.assertTrue(prune["rotated_receipt_path"])
        for prune in prunes:
            self.assertFalse(
                (self.root_path / prune["pruned_path"]).exists(),
                "a prune receipt names bytes that still exist",
            )
            self.assertTrue(
                (self.root_path / prune["receipt_path"]).is_file()
            )

    def test_an_unwritable_prune_receipt_leaves_the_file(self) -> None:
        """Fail closed: the receipt is written BEFORE the unlink, so a
        broken receipts plane leaves the rotated file in place instead of
        destroying bytes without testimony."""

        import os  # noqa: F401  (chmod contract mirrors the rotation test)

        from floati.errors import ProtocolRefusal

        for index in range(3):
            with self.stderr_log.open("ab") as log:
                log.write(b"%d-" % index + b"x" * 2048)
            self._maintain(
                max_bytes=1024, retain=2,
                stamp=f"2026-09-06T01:02:03.00{index}Z",
            )

        stale = min(
            self.logs_dir.glob(
                self.stderr_log.name.replace(".stderr.log", ".stderr.*.log")
            ),
            key=lambda path: path.name,
        )
        self.assertTrue(stale.is_file(), "the fixture must hold a prunable file")
        prune_plane = (
            self.root_path / "receipts" / "wake-daemon-log-prune" / self._digest()
        )
        prune_plane.mkdir(parents=True, exist_ok=True)
        prune_plane.chmod(0o500)
        try:
            with self.stderr_log.open("ab") as log:
                log.write(b"y" * 2048)
            with self.assertRaises(ProtocolRefusal) as raised:
                self._maintain(
                    max_bytes=1024, retain=2,
                    stamp="2026-09-06T01:02:03.009Z",
                )
            self.assertEqual(
                "wake_daemon_log_prune_receipt_unwritable", raised.exception.code
            )
            self.assertIsNotNone(raised.exception.remedy)
        finally:
            prune_plane.chmod(0o700)
        self.assertTrue(
            stale.is_file(), "an unreceipted prune must not unlink"
        )


class PruneBlockMarkerTests(unittest.TestCase):
    """LEDGER-1a-F1: an unhealable prune condition holds quietly.

    Measured by the read: with the prune plane unwritable the SAME
    refusal re-raised every cycle (3 of 3) - the LEDGER-1 stderr-wall
    shape in miniature. The block is recorded once (the one refusal plus
    a state-plane marker naming code, digest, stream and pending count);
    every further blocked cycle holds quietly (rotation still receipted,
    prunable files intact), and a successful prune proves the plane
    writable again and clears the marker.
    """

    def setUp(self) -> None:
        import os  # noqa: F401

        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)
        self.logs_dir = self.root_path / "state" / "wake-daemon" / "logs"
        self.logs_dir.mkdir(parents=True)
        self.stderr_log = self.logs_dir / (
            "18fa5abb06ddfd9624265fa02776867ccb451f9ff7c4a3cfca5fb642a74d36e8"
            ".stderr.log"
        )
        from floati import wake_daemon

        wake_daemon._ROTATION_COUNTER.clear()
        # The in-process held flag lands with F1 Am.2; a tree without it
        # has nothing to clear.
        getattr(wake_daemon, "_PRUNE_BLOCK_HELD", set()).clear()

    def _digest(self) -> str:
        return self.stderr_log.name[: -len(".stderr.log")]

    def _maintain(self, stamp: str):
        from floati.wake_daemon import maintain_supervisor_logs

        with self.stderr_log.open("ab") as log:
            log.write(b"x" * 2048)
        return maintain_supervisor_logs(
            self.root, self._digest(), max_bytes=1024, retain=2, stamp=stamp
        )

    def _marker(self) -> Path:
        return (
            self.root_path / "state" / "wake-daemon" / "log-prune-blocked"
            / f"{self._digest()}.stderr.json"
        )

    def _prune_plane(self) -> Path:
        return (
            self.root_path / "receipts" / "wake-daemon-log-prune"
            / self._digest()
        )

    def _marker_pending(self) -> int:
        import json as _json

        return int(
            _json.loads(self._marker().read_text(encoding="utf-8"))[
                "pending_prunes"
            ]
        )

    def test_the_block_is_recorded_once_then_held_quietly(self) -> None:
        from floati.errors import ProtocolRefusal

        # Two rotations, nothing prunable yet; then the prune plane breaks.
        self._maintain("2026-09-06T01:02:03.001Z")
        self._maintain("2026-09-06T01:02:03.002Z")
        self._prune_plane().mkdir(parents=True, exist_ok=True)
        self._prune_plane().chmod(0o500)
        held = None
        try:
            # The first blocked prune RAISES - the refusal is recorded
            # once, beside a state-plane block marker.
            with self.assertRaises(ProtocolRefusal) as raised:
                self._maintain("2026-09-06T01:02:03.003Z")
            self.assertEqual(
                "wake_daemon_log_prune_receipt_unwritable",
                raised.exception.code,
            )
            self.assertTrue(self._marker().is_file(), "no block marker")
            recorded = json.loads(self._marker().read_text(encoding="utf-8"))
            self.assertEqual(
                "wake_daemon_log_prune_receipt_unwritable", recorded["code"]
            )
            self.assertEqual(self._digest(), recorded["coordinate_digest"])
            self.assertEqual("stderr", recorded["stream"])
            self.assertGreaterEqual(recorded["pending_prunes"], 1)

            held = min(
                self.logs_dir.glob(
                    self.stderr_log.name.replace(".stderr.log", ".stderr.*.log")
                ),
                key=lambda path: path.name,
            )

            # The next two blocked cycles HOLD QUIETLY: no refusal, the
            # rotation is still receipted, the held file survives.
            for index in (4, 5):
                receipts = self._maintain(
                    f"2026-09-06T01:02:03.00{index}Z"
                )
                self.assertEqual(1, len(receipts), receipts)
            self.assertTrue(
                held.is_file(), "a quietly held prune must not unlink"
            )
            self.assertTrue(self._marker().is_file())
        finally:
            self._prune_plane().chmod(0o700)

        # The condition clears: the next prune succeeds and receipt, and
        # the marker is gone.
        self._maintain("2026-09-06T01:02:03.006Z")
        self.assertFalse(self._marker().exists(), "the marker never cleared")
        self.assertTrue(
            not held.is_file() if held else True,
            "the pruned file must be gone once the plane healed",
        )
        prunes = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self._prune_plane().glob("*.json"))
        ]
        self.assertGreaterEqual(len(prunes), 1, "no prune receipt after healing")

    def test_pending_prunes_refreshes_every_blocked_cycle(self) -> None:
        """LEDGER-1a-F1 Am.1 FINDING 1: the marker was written once and its
        pending_prunes froze at the first blocked cycle's count while the
        true backlog kept growing - a stale number is a number floati did
        not measure. Every blocked cycle refreshes it."""

        from floati.errors import ProtocolRefusal

        self._maintain("2026-09-06T01:02:03.001Z")
        self._maintain("2026-09-06T01:02:03.002Z")
        self._prune_plane().mkdir(parents=True, exist_ok=True)
        self._prune_plane().chmod(0o500)
        try:
            with self.assertRaises(ProtocolRefusal):
                self._maintain("2026-09-06T01:02:03.003Z")
            self.assertEqual(1, self._marker_pending())

            # Each further blocked cycle: backlog grows by one, and the
            # marker must say so.
            for cycle, backlog in ((4, 2), (5, 3), (6, 4)):
                receipts = self._maintain(f"2026-09-06T01:02:03.00{cycle}Z")
                self.assertEqual(1, len(receipts), receipts)
                self.assertEqual(backlog, self._marker_pending())
        finally:
            self._prune_plane().chmod(0o700)

    def test_an_unwritable_marker_plane_is_typed(self) -> None:
        """LEDGER-1a-F1 Am.1 FINDING 2: when the marker directory is
        unwritable too, _record_prune_block raised a RAW PermissionError
        every cycle - no code, no remedy, no marker. The marker-plane
        failure is its own typed refusal, with the prunable file intact."""

        from floati.errors import ProtocolRefusal

        self._maintain("2026-09-06T01:02:03.001Z")
        self._maintain("2026-09-06T01:02:03.002Z")
        self._prune_plane().mkdir(parents=True, exist_ok=True)
        self._prune_plane().chmod(0o500)
        marker_dir = (
            self.root_path / "state" / "wake-daemon" / "log-prune-blocked"
        )
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_dir.chmod(0o500)
        try:
            with self.assertRaises(ProtocolRefusal) as raised:
                self._maintain("2026-09-06T01:02:03.003Z")
            self.assertEqual(
                "wake_daemon_log_prune_marker_unwritable",
                raised.exception.code,
            )
            self.assertIsNotNone(raised.exception.remedy)
        finally:
            self._prune_plane().chmod(0o700)
            marker_dir.chmod(0o700)
        held = min(
            self.logs_dir.glob(
                self.stderr_log.name.replace(".stderr.log", ".stderr.*.log")
            ),
            key=lambda path: path.name,
        )
        self.assertTrue(held.is_file(), "the prunable file must survive")

    def test_a_blocked_marker_plane_raises_once_then_holds_quietly(self) -> None:
        """LEDGER-1a-F1 Am.2 RED: with the marker plane unwritable the
        typed wake_daemon_log_prune_marker_unwritable re-raised
        IDENTICALLY every cycle - the marker can never be written, so the
        already-recorded check is always false, and the Am.1 test
        exercised cycle 1 only. The once-flag lives in process memory per
        (digest, stream): the typed refusal raises once, further blocked
        cycles hold quietly with the prunable file intact, and a changed
        condition (the marker plane healing) re-arms the flag."""

        from floati import wake_daemon
        from floati.errors import ProtocolRefusal

        self._maintain("2026-09-06T01:02:03.001Z")
        self._maintain("2026-09-06T01:02:03.002Z")
        self._prune_plane().mkdir(parents=True, exist_ok=True)
        self._prune_plane().chmod(0o500)
        marker_dir = (
            self.root_path / "state" / "wake-daemon" / "log-prune-blocked"
        )
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_dir.chmod(0o500)
        try:
            # Cycle 1 on the blocked marker plane: the typed refusal raises
            # once.
            with self.assertRaises(ProtocolRefusal) as raised:
                self._maintain("2026-09-06T01:02:03.003Z")
            self.assertEqual(
                "wake_daemon_log_prune_marker_unwritable",
                raised.exception.code,
            )
            # Cycles 2-4 hold QUIETLY: no raise at all.
            for index in (4, 5, 6):
                receipts = self._maintain(
                    f"2026-09-06T01:02:03.00{index}Z"
                )
                self.assertEqual(1, len(receipts), receipts)
        finally:
            marker_dir.chmod(0o700)
            self._prune_plane().chmod(0o700)

        # The condition changed: the marker plane healed, so the next
        # blocked prune is RECORDED (the receipt-plane refusal raises
        # once) and then holds quietly through the marker.
        self._prune_plane().chmod(0o500)
        try:
            with self.assertRaises(ProtocolRefusal) as raised:
                self._maintain("2026-09-06T01:02:03.007Z")
            self.assertEqual(
                "wake_daemon_log_prune_receipt_unwritable",
                raised.exception.code,
            )
            self.assertTrue(self._marker().is_file())
            receipts = self._maintain("2026-09-06T01:02:03.008Z")
            self.assertEqual(1, len(receipts), receipts)
        finally:
            self._prune_plane().chmod(0o700)
        self.assertEqual(
            set(), wake_daemon._PRUNE_BLOCK_HELD,
            "the in-process flag must clear when the condition changes",
        )
