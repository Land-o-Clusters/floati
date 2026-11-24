"""Scratch-only capability retirement without rewriting historical mail."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.codex_wait_contract import WORKSPACE_MAP_RELATIVE, consent_ledger_relative
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.waiter_consent import arm_waiter_consent
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from tests.temp_roots import REAL_TEMP_ROOT


class SeatWaiterLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "scratch-fleet", create=True)
        Registry(self.root).register("seat-a", "Codex")
        Registry(self.root).register("seat-b", "Codex")
        self.workspace = self.base / "home-a"
        self.other = self.base / "home-b"
        self.workspace.mkdir()
        self.other.mkdir()
        self.map_path = self.root.path / WORKSPACE_MAP_RELATIVE

    def arm(self, node="seat-a", workspace=None):
        return arm_waiter_consent(self.root.path, workspace or self.workspace, node, "Codex", 30, 25)

    def files(self):
        return {str(p.relative_to(self.root.path)): p.read_bytes()
                for p in self.root.path.rglob("*") if p.is_file()}

    def binding(self):
        executable = self.base / "codex-bin"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        coordinate = DaemonCoordinate(self.root, "seat-a", "codex")
        store = AdapterBindingStore(self.root)
        store.write(coordinate, session_id="session-a", workspace=self.workspace,
                    executable=executable, adapter_version="1", adapter_digest="a" * 64,
                    binding_epoch=1)
        return executable, coordinate, store.path(coordinate)

    def test_prepare_waiter_creates_native_consent_and_exact_mapping(self):
        from floati.seat_waiter_lifecycle import prepare_waiter
        result = prepare_waiter(self.root, "seat-a", self.workspace, 30, 25)
        self.assertEqual("armed", result["state"])
        self.assertEqual([{"node_id": "seat-a", "workspace": str(self.workspace)}],
                         json.loads(self.map_path.read_bytes())["mappings"])

    def test_retire_disarms_native_receipt_preserving_other_mapping_and_history(self):
        from floati.seat_waiter_lifecycle import retire_waiter
        self.arm()
        self.arm("seat-b", self.other)
        ledger = self.root.path / consent_ledger_relative("seat-a")
        before = ledger.read_bytes()
        result = retire_waiter(self.root, "seat-a", self.workspace, "remove-a")
        self.assertEqual("disarmed", result["state"])
        self.assertTrue(ledger.read_bytes().startswith(before))
        rows = read_records_snapshot(self.root, consent_ledger_relative("seat-a"),
                                     allowed_kinds={"codex_wait_consent_receipt"})
        self.assertEqual(["armed", "disarmed"], [row["state"] for row in rows])
        self.assertEqual([{"node_id": "seat-b", "workspace": str(self.other)}],
                         json.loads(self.map_path.read_bytes())["mappings"])
        replay = retire_waiter(self.root, "seat-a", self.workspace, "remove-a")
        self.assertEqual(result["consent_receipt_id"], replay["consent_receipt_id"])
        self.assertEqual(2, len(read_records_snapshot(self.root, consent_ledger_relative("seat-a"),
                         allowed_kinds={"codex_wait_consent_receipt"})))

    def test_retire_removes_new_empty_map_and_restores_preexisting_empty_bytes(self):
        from floati.seat_waiter_lifecycle import retire_waiter
        self.arm()
        retire_waiter(self.root, "seat-a", self.workspace, "remove-first")
        self.assertFalse(self.map_path.exists())
        previous = json.dumps({"schema_version": 0, "tenant_id": "scratch-fleet", "mappings": []},
                              indent=2).encode() + b"\n"
        self.map_path.write_bytes(previous)
        self.arm()
        retire_waiter(self.root, "seat-a", self.workspace, "remove-next", previous_map=previous)
        self.assertEqual(previous, self.map_path.read_bytes())

    def test_foreign_mapping_and_symlink_map_refuse_without_mutation(self):
        from floati.seat_waiter_lifecycle import preflight_retire_waiter, retire_waiter
        self.arm("seat-b", self.workspace)
        before = self.files()
        for action in (preflight_retire_waiter, lambda r, n, w: retire_waiter(r, n, w, "foreign")):
            with self.assertRaises(ProtocolRefusal) as caught:
                action(self.root, "seat-a", self.workspace)
            self.assertEqual("seat_waiter_lifecycle_invalid", caught.exception.code)
            self.assertTrue(caught.exception.remedy)
            self.assertEqual(before, self.files())
        saved = self.map_path.with_name("saved.json")
        self.map_path.rename(saved)
        self.map_path.symlink_to(saved)
        with self.assertRaises(ProtocolRefusal):
            preflight_retire_waiter(self.root, "seat-b", self.workspace)
        self.assertEqual(before[str(WORKSPACE_MAP_RELATIVE)], saved.read_bytes())

    def test_stale_adapter_archives_exact_bytes_without_live_executable(self):
        from floati.seat_waiter_lifecycle import retire_stale_adapter
        executable, coordinate, path = self.binding()
        before = path.read_bytes()
        executable.unlink()
        result = retire_stale_adapter(self.root, "seat-a", "stale-a")
        self.assertEqual("retired", result["state"])
        self.assertEqual("executable_missing", result["reason"])
        self.assertFalse(path.exists())
        archive = Path(result["archive_path"])
        self.assertTrue(archive.is_relative_to(self.root.path / "state/seat-onboarding-history"))
        self.assertEqual(before, archive.read_bytes())
        replay = retire_stale_adapter(self.root, "seat-a", "stale-a")
        self.assertEqual(result["archive_path"], replay["archive_path"])

    def test_intact_adapter_is_unchanged(self):
        from floati.seat_waiter_lifecycle import retire_stale_adapter
        _, _, path = self.binding()
        before = self.files()
        result = retire_stale_adapter(self.root, "seat-a", "keep-a")
        self.assertEqual("unchanged", result["state"])
        self.assertTrue(path.exists())
        self.assertEqual(before, self.files())

    def test_foreign_and_corrupt_adapter_refuse_before_archive_or_removal(self):
        from floati.seat_waiter_lifecycle import retire_stale_adapter
        executable, _, path = self.binding()
        raw = json.loads(path.read_bytes())
        executable.unlink()
        for updates in ({"node_id": "seat-b"}, {"coordinate_digest": "b" * 64},
                        {"executable_digest": "not-a-digest"}, {"extra": "foreign"},
                        {"resume_state": []}):
            with self.subTest(updates=updates):
                path.write_text(json.dumps(dict(raw, **updates)))
                before = self.files()
                with self.assertRaises(ProtocolRefusal) as caught:
                    retire_stale_adapter(self.root, "seat-a", "bad-a")
                self.assertEqual("seat_waiter_lifecycle_invalid", caught.exception.code)
                self.assertTrue(caught.exception.remedy)
                self.assertEqual(before, self.files())

    def test_runtime_evidence_prevents_stale_adapter_removal(self):
        from floati.seat_waiter_lifecycle import retire_stale_adapter
        executable, coordinate, _ = self.binding()
        executable.unlink()
        runtime = self.root.path / "state/wake-daemon/runtime" / (coordinate.digest + ".json")
        runtime.parent.mkdir()
        runtime.write_text("{}")
        before = self.files()
        with self.assertRaises(ProtocolRefusal):
            retire_stale_adapter(self.root, "seat-a", "runtime-a")
        self.assertEqual(before, self.files())

    def test_prior_life_mail_is_read_only_and_retired_sender_does_not_hide_it(self):
        from floati.seat_waiter_lifecycle import prior_life_mail
        events = EventLog(self.root)
        first = events.send("seat-b", "seat-a", "floati", "a" * 40, "README.md", "first")
        second = events.send("seat-b", "seat-a", "floati", "a" * 40, "README.md", "second")
        messages, _, _ = events.drain("seat-a", acting_session_id="session-a", limit=1)
        self.assertEqual(first["id"], messages[0]["id"])
        Registry(self.root).retire("seat-b")
        # Historical copied ledgers need not retain their original lock files.
        for lock in self.root.path.rglob("*.lock"):
            lock.unlink()
        before = self.files()
        self.assertEqual([second["id"]], prior_life_mail(self.root, "seat-a"))
        self.assertEqual(before, self.files())

    def test_archive_is_private(self):
        from floati.seat_waiter_lifecycle import retire_stale_adapter
        executable, _, path = self.binding()
        executable.unlink()
        result = retire_stale_adapter(self.root, "seat-a", "private-a")
        archive = Path(result["archive_path"])
        self.assertEqual(0o600, archive.stat().st_mode & 0o777)
        self.assertEqual(0o700, archive.parent.parent.stat().st_mode & 0o777)

    def test_symlinked_adapter_ancestor_refuses_without_mutation(self):
        from floati.seat_waiter_lifecycle import retire_stale_adapter
        executable, _, path = self.binding()
        executable.unlink()
        foreign = self.base / "foreign-adapters"
        path.parent.rename(foreign)
        path.parent.symlink_to(foreign)
        before = {str(p): p.read_bytes() for p in foreign.iterdir()}
        with self.assertRaises(ProtocolRefusal):
            retire_stale_adapter(self.root, "seat-a", "symlink-a")
        self.assertEqual(before, {str(p): p.read_bytes() for p in foreign.iterdir()})
        self.assertFalse((self.root.path / "state/seat-onboarding-history").exists())

    def test_adapter_replacement_at_removal_boundary_is_preserved(self):
        from floati.seat_waiter_lifecycle import retire_stale_adapter
        executable, _, path = self.binding()
        executable.unlink()
        original_read = Path.read_bytes
        reads = 0
        def replace_after_read(candidate):
            nonlocal reads
            encoded = original_read(candidate)
            if candidate == path:
                reads += 1
                if reads == 2:
                    candidate.write_bytes(b"replacement binding belongs to another writer\n")
            return encoded
        with mock.patch.object(Path, "read_bytes", replace_after_read):
            with self.assertRaises(ProtocolRefusal):
                retire_stale_adapter(self.root, "seat-a", "race-a")
        self.assertEqual(b"replacement binding belongs to another writer\n", path.read_bytes())

    def test_rehome_removes_all_old_node_mappings_even_when_home_is_missing(self):
        from floati.seat_waiter_lifecycle import preflight_rehome, rehome_waiter
        second_home = self.base / "another-old-home"
        second_home.mkdir()
        self.arm()
        self.arm("seat-a", second_home)
        self.arm("seat-b", self.other)
        self.workspace.rmdir()
        second_home.rmdir()
        before = self.files()
        preflight_rehome(self.root, "seat-a")
        self.assertEqual(before, self.files())
        result = rehome_waiter(self.root, "seat-a", "rehome-a")
        self.assertEqual(sorted([str(self.workspace), str(second_home)]), result["old_workspaces"])
        self.assertEqual([{"node_id": "seat-b", "workspace": str(self.other)}],
                         json.loads(self.map_path.read_bytes())["mappings"])
        rows = read_records_snapshot(self.root, consent_ledger_relative("seat-a"),
                                     allowed_kinds={"codex_wait_consent_receipt"})
        self.assertEqual(["armed", "armed", "disarmed", "disarmed"], [row["state"] for row in rows])
        self.assertFalse(self.workspace.exists())
        self.assertFalse(second_home.exists())

    def test_rehome_refuses_intact_adapter_before_any_capability_changes(self):
        from floati.seat_waiter_lifecycle import preflight_rehome, rehome_waiter
        self.arm()
        self.binding()
        before = self.files()
        for action in (preflight_rehome, lambda root, node: rehome_waiter(root, node, "live-a")):
            with self.assertRaises(ProtocolRefusal) as caught:
                action(self.root, "seat-a")
            self.assertEqual("seat_waiter_lifecycle_invalid", caught.exception.code)
            self.assertEqual(before, self.files())

    def test_rehome_archives_stale_binding_and_leaves_events_unchanged(self):
        from floati.seat_waiter_lifecycle import rehome_waiter
        self.arm()
        events = EventLog(self.root)
        events.send("seat-b", "seat-a", "floati", "a" * 40, "README.md", "historical")
        event_bytes = events.path.read_bytes()
        executable, _, binding = self.binding()
        executable.unlink()
        self.workspace.rmdir()
        result = rehome_waiter(self.root, "seat-a", "stale-rehome-a")
        self.assertEqual("retired", result["adapter"]["state"])
        self.assertTrue(Path(result["adapter"]["archive_path"]).exists())
        self.assertFalse(binding.exists())
        self.assertFalse(self.map_path.exists())
        self.assertEqual(event_bytes, events.path.read_bytes())

    def test_rehome_corrupt_consent_refuses_before_stale_adapter_archival(self):
        from floati.seat_waiter_lifecycle import rehome_waiter
        self.arm()
        executable, _, _ = self.binding()
        executable.unlink()
        ledger = self.root.path / consent_ledger_relative("seat-a")
        ledger.write_bytes(b"corrupt consent\n")
        before = self.files()
        with self.assertRaises(ProtocolRefusal):
            rehome_waiter(self.root, "seat-a", "bad-consent-a")
        self.assertEqual(before, self.files())

    def test_same_home_readd_rearms_after_native_disarm_with_stable_replay(self):
        from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant
        from floati.seat_waiter_lifecycle import prepare_waiter, retire_waiter
        first = prepare_waiter(self.root, "seat-a", self.workspace, 30, 25)
        retire_waiter(self.root, "seat-a", self.workspace, "same-home-remove")
        rearmed = prepare_waiter(self.root, "seat-a", self.workspace, 30, 25)
        participant = resolve_participant(self.root.path, self.workspace)
        current = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        self.assertNotEqual(first["consent_receipt_id"], current["id"])
        self.assertEqual(current["id"], rearmed["consent_receipt_id"])
        replay = prepare_waiter(self.root, "seat-a", self.workspace, 30, 25)
        self.assertEqual(current["id"], replay["consent_receipt_id"])

    def test_corrupt_archive_staging_refuses_before_publish_or_binding_removal(self):
        from floati import seat_waiter_lifecycle as lifecycle
        executable, _, binding = self.binding()
        before = binding.read_bytes()
        executable.unlink()
        native_write = lifecycle.os.write
        def corrupt_write(descriptor, encoded):
            return native_write(descriptor, b"!" + encoded[1:])
        with mock.patch.object(lifecycle.os, "write", corrupt_write):
            with self.assertRaises(ProtocolRefusal):
                lifecycle.retire_stale_adapter(self.root, "seat-a", "corrupt-stage")
        self.assertEqual(before, binding.read_bytes())
        self.assertEqual([], list(self.root.path.joinpath("state/seat-onboarding-history").rglob("*.json")))
        self.assertEqual([], list(self.root.path.joinpath("state/seat-onboarding-history").rglob(".seat-archive-*")))

    def test_zero_archive_write_never_publishes_partial_bytes(self):
        from floati import seat_waiter_lifecycle as lifecycle
        executable, _, binding = self.binding()
        before = binding.read_bytes()
        executable.unlink()
        with mock.patch.object(lifecycle.os, "write", return_value=0):
            with self.assertRaises(ProtocolRefusal):
                lifecycle.retire_stale_adapter(self.root, "seat-a", "zero-stage")
        self.assertEqual(before, binding.read_bytes())
        self.assertEqual([], list(self.root.path.joinpath("state/seat-onboarding-history").rglob("*.json")))

    def test_foreign_archive_racing_publication_preserves_bytes_and_mode(self):
        from floati import seat_waiter_lifecycle as lifecycle
        executable, _, binding = self.binding()
        before = binding.read_bytes()
        executable.unlink()
        native_publish = lifecycle._rename_generation_noreplace
        archives = []
        def race_archive(source, target):
            target.write_bytes(b"foreign archive data\n")
            target.chmod(0o640)
            archives.append(target)
            return native_publish(source, target)
        with mock.patch.object(lifecycle, "_rename_generation_noreplace", race_archive):
            with self.assertRaises(ProtocolRefusal):
                lifecycle.retire_stale_adapter(self.root, "seat-a", "raced-archive")
        self.assertEqual(before, binding.read_bytes())
        self.assertEqual(1, len(archives))
        self.assertEqual(b"foreign archive data\n", archives[0].read_bytes())
        self.assertEqual(0o640, archives[0].stat().st_mode & 0o777)

    def test_published_archive_readback_failure_preserves_source_binding(self):
        from floati import seat_waiter_lifecycle as lifecycle
        executable, _, binding = self.binding()
        before = binding.read_bytes()
        executable.unlink()
        native_publish = lifecycle._rename_generation_noreplace
        archives = []
        def publish_then_replace(source, target):
            native_publish(source, target)
            target.write_bytes(b"replaced after archive publication\n")
            archives.append(target)
        with mock.patch.object(lifecycle, "_rename_generation_noreplace", publish_then_replace):
            with self.assertRaises(ProtocolRefusal):
                lifecycle.retire_stale_adapter(self.root, "seat-a", "archive-readback")
        self.assertEqual(before, binding.read_bytes())
        self.assertEqual(b"replaced after archive publication\n", archives[0].read_bytes())
        self.assertEqual([], list(archives[0].parent.glob(".retiring-*")))

    def test_matching_preexisting_archive_with_foreign_mode_is_not_chmodded_or_used(self):
        import hashlib
        from floati.seat_waiter_lifecycle import retire_stale_adapter
        executable, _, binding = self.binding()
        before = binding.read_bytes()
        executable.unlink()
        archive = self.root.path / "state/seat-onboarding-history/seat-a" / (
            "codex-" + hashlib.sha256(b"foreign-mode").hexdigest() + ".json")
        archive.parent.mkdir(parents=True)
        archive.write_bytes(before)
        archive.chmod(0o640)
        with self.assertRaises(ProtocolRefusal):
            retire_stale_adapter(self.root, "seat-a", "foreign-mode")
        self.assertEqual(before, binding.read_bytes())
        self.assertEqual(before, archive.read_bytes())
        self.assertEqual(0o640, archive.stat().st_mode & 0o777)

    def test_archive_directory_fsync_failure_keeps_binding_and_retry_rechecks_durability(self):
        import stat
        from floati import seat_waiter_lifecycle as lifecycle
        executable, _, binding = self.binding()
        before = binding.read_bytes()
        executable.unlink()
        native_fsync = lifecycle.os.fsync
        def fail_directory_sync(descriptor):
            if stat.S_ISDIR(lifecycle.os.fstat(descriptor).st_mode):
                raise OSError("injected archive directory fsync failure")
            return native_fsync(descriptor)
        with mock.patch.object(lifecycle.os, "fsync", fail_directory_sync):
            with self.assertRaises(OSError):
                lifecycle.retire_stale_adapter(self.root, "seat-a", "directory-fsync")
        self.assertEqual(before, binding.read_bytes())
        archives = list(self.root.path.joinpath("state/seat-onboarding-history").rglob("*.json"))
        self.assertEqual(1, len(archives))
        self.assertEqual(before, archives[0].read_bytes())
        synchronized = []
        def record_fsync(descriptor):
            synchronized.append("directory" if stat.S_ISDIR(
                lifecycle.os.fstat(descriptor).st_mode) else "file")
            return native_fsync(descriptor)
        native_replace = lifecycle.os.replace
        def require_durable_archive(source, target):
            self.assertEqual(["file", "directory"], synchronized)
            self.assertEqual(before, archives[0].read_bytes())
            return native_replace(source, target)
        with mock.patch.object(lifecycle.os, "fsync", record_fsync), mock.patch.object(
                lifecycle.os, "replace", require_durable_archive):
            result = lifecycle.retire_stale_adapter(self.root, "seat-a", "directory-fsync")
        self.assertEqual("retired", result["state"])
        self.assertFalse(binding.exists())
        self.assertEqual(before, Path(result["archive_path"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
