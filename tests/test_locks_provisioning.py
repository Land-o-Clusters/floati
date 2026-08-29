from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Optional

from floati.errors import ProtocolRefusal
from floati.locks.ledger import LockLedger
from floati.locks.provisioning import ProvisioningController
from floati.root import FloatiRoot


NOW = "2026-08-26T20:00:00.000Z"


class FileHook:
    name = "first"

    def __init__(self, *, rollback_succeeds: bool = True) -> None:
        self.rollback_succeeds = rollback_succeeds
        self.abort_called = False
        self.prepared_root: Optional[Path] = None

    def prepare(self, staging_root: Path) -> dict[str, object]:
        self.prepared_root = staging_root
        (staging_root / "first.prepared").write_text("prepared\n", encoding="utf-8")
        return {"resource": "first.prepared"}

    def abort(self, staging_root: Path) -> None:
        self.abort_called = True
        if self.rollback_succeeds:
            (staging_root / "first.prepared").unlink(missing_ok=True)

    def verify_absent(self, staging_root: Path) -> bool:
        return not (staging_root / "first.prepared").exists()


class FailingHook:
    name = "second"

    @staticmethod
    def prepare(staging_root: Path) -> dict[str, object]:
        if not (staging_root / "first.prepared").exists():
            raise AssertionError("first hook did not prepare before the second")
        raise RuntimeError("second hook failed")

    @staticmethod
    def abort(staging_root: Path) -> None:
        return None

    @staticmethod
    def verify_absent(staging_root: Path) -> bool:
        return True


class PartiallyFailingHook:
    name = "partial"

    def __init__(self) -> None:
        self.abort_called = False

    def prepare(self, staging_root: Path) -> dict[str, object]:
        (staging_root / "partial.prepared").write_text("partial\n", encoding="utf-8")
        raise RuntimeError("failed after allocation")

    def abort(self, staging_root: Path) -> None:
        self.abort_called = True
        (staging_root / "partial.prepared").unlink(missing_ok=True)

    @staticmethod
    def verify_absent(staging_root: Path) -> bool:
        return not (staging_root / "partial.prepared").exists()


class ExternalResourceHook(FileHook):
    name = "external"

    def prepare(self, staging_root: Path) -> dict[str, object]:
        self.prepared_root = staging_root
        return {"resource": "/private/tmp/outside-locks-staging"}


class SymlinkResourceHook(FileHook):
    name = "symlink"

    def __init__(self, outside: Path) -> None:
        super().__init__()
        self.outside = outside

    def prepare(self, staging_root: Path) -> dict[str, object]:
        self.prepared_root = staging_root
        (staging_root / "escape").symlink_to(self.outside)
        return {"resource": "escape"}

    def abort(self, staging_root: Path) -> None:
        self.abort_called = True
        (staging_root / "escape").unlink(missing_ok=True)

    @staticmethod
    def verify_absent(staging_root: Path) -> bool:
        return not (staging_root / "escape").exists()


class IterationFailure:
    def __iter__(self):
        raise RuntimeError("hostile hook collection")


class LocksProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = FloatiRoot.open(self.base, "alpha")
        self.ledger = LockLedger(self.root)
        self.controller = ProvisioningController(self.ledger)

    def test_half_failed_provisioning_unwinds_and_never_yields_a_seat(self) -> None:
        """Catches a prepared resource or seat surviving a later hook failure."""

        first = FileHook()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.controller.provision(
                seat_id="lane-one",
                hooks=(first, FailingHook()),
                now=NOW,
            )
        self.assertEqual("provisioning_prepare_failed", caught.exception.code)
        self.assertTrue(first.abort_called)
        self.assertIsNotNone(first.prepared_root)
        self.assertFalse((first.prepared_root / "first.prepared").exists())
        self.assertEqual({}, self.ledger.snapshot().seats)

    def test_rollback_failure_is_loud_and_still_never_yields_a_seat(self) -> None:
        """Catches rollback uncertainty being reported as successful provisioning."""

        first = FileHook(rollback_succeeds=False)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.controller.provision(
                seat_id="lane-one",
                hooks=(first, FailingHook()),
                now=NOW,
            )
        self.assertEqual("provisioning_rollback_failed", caught.exception.code)
        self.assertTrue(first.abort_called)
        self.assertEqual({}, self.ledger.snapshot().seats)

    def test_hook_that_raises_after_allocation_is_itself_aborted(self) -> None:
        """Catches a partially prepared failing hook escaping the unwind set."""

        partial = PartiallyFailingHook()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.controller.provision(
                seat_id="lane-one",
                hooks=(partial,),
                now=NOW,
            )
        self.assertEqual("provisioning_prepare_failed", caught.exception.code)
        self.assertTrue(partial.abort_called)
        self.assertEqual({}, self.ledger.snapshot().seats)

    def test_success_publishes_resources_before_returning_the_seat(self) -> None:
        """Catches seat testimony preceding complete resource publication."""

        seat = self.controller.provision(
            seat_id="lane-one",
            hooks=(FileHook(),),
            now=NOW,
        )
        self.assertTrue((seat.resource_root / "first.prepared").is_file())
        self.assertEqual(("first",), seat.hook_names)
        projected = self.ledger.snapshot().seats["lane-one"]
        self.assertEqual(seat.manifest_digest, projected.manifest_digest)
        self.assertEqual(("first",), projected.hook_names)

    def test_duplicate_seat_refuses_before_a_second_hook_prepares(self) -> None:
        """Catches duplicate seat identity creating a second resource root."""

        self.controller.provision(seat_id="lane-one", hooks=(FileHook(),), now=NOW)
        second = FileHook()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.controller.provision(seat_id="lane-one", hooks=(second,), now=NOW)
        self.assertEqual("seat_already_provisioned", caught.exception.code)
        self.assertIsNone(second.prepared_root)

    def test_external_resource_claim_refuses_and_appends_no_seat(self) -> None:
        """Catches a hook escaping controller-owned staging through its manifest."""

        with self.assertRaises(ProtocolRefusal) as caught:
            self.controller.provision(
                seat_id="lane-one",
                hooks=(ExternalResourceHook(),),
                now=NOW,
            )
        self.assertEqual("provisioning_manifest_invalid", caught.exception.code)
        self.assertEqual({}, self.ledger.snapshot().seats)

    def test_symlinked_resource_cannot_escape_controller_staging(self) -> None:
        """Catches a relative manifest path resolving to an external resource."""

        outside = self.base / "outside"
        outside.mkdir()
        hook = SymlinkResourceHook(outside)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.controller.provision(
                seat_id="lane-one",
                hooks=(hook,),
                now=NOW,
            )
        self.assertEqual("provisioning_manifest_invalid", caught.exception.code)
        self.assertTrue(hook.abort_called)
        self.assertTrue(outside.is_dir())
        self.assertEqual({}, self.ledger.snapshot().seats)

    def test_hostile_hook_collection_refuses_before_staging(self) -> None:
        """Catches a caller-controlled iterable escaping the input snapshot."""

        with self.assertRaises(ProtocolRefusal) as caught:
            self.controller.provision(seat_id="lane-one", hooks=IterationFailure(), now=NOW)
        self.assertEqual("provisioning_hooks_invalid", caught.exception.code)
        self.assertEqual({}, self.ledger.snapshot().seats)


if __name__ == "__main__":
    unittest.main()
