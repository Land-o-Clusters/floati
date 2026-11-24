"""Retention decisions over genuine native rolls and scratch policy files."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from floati import archive_retention_policy as policy, bus_epoch
from floati.errors import FloatiError, ProtocolRefusal
from floati.root import FloatiRoot
from tests import test_epoch_carry as fixture


class ArchiveRetentionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.EpochCarryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.path = self.root.tenant_home / "state/archive-policy.json"

    def write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value))

    def snapshot(self):
        return {path.relative_to(self.root.tenant_home).as_posix():
                path.read_bytes() if path.is_file() else None
                for path in self.root.tenant_home.rglob("*")}

    def roll(self, automatic=False):
        self.fixture.send("retention-native-" + str(automatic))
        result = (self.fixture.automatic_roll() if automatic else
                  bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="retention-manual"))
        return result["receipt"]

    def moment(self, receipt):
        return datetime.fromisoformat(receipt["timestamp"].replace("Z", "+00:00"))

    def refused(self, call, code="archive_policy_invalid"):
        caught = None
        try:
            call()
        except Exception as error:
            caught = error
        self.assertIsInstance(caught, ProtocolRefusal)
        self.assertEqual(code, caught.code)

    def test_absent_policy_defaults_without_initializing_any_files(self):
        before = self.snapshot()
        result = policy.read_archive_policy(self.root)
        self.assertEqual({"schema_version": 1, "keep_forever": False}, result)
        result["keep_forever"] = True
        self.assertEqual({"schema_version": 1, "keep_forever": False}, policy.read_archive_policy(self.root))
        self.assertEqual(before, self.snapshot())

    def test_absent_state_directory_defaults_without_initialization(self):
        root = FloatiRoot.open_direct_home(self.fixture.base / "empty-home", create=True)
        before = sorted(path.relative_to(root.path).as_posix() for path in root.path.rglob("*"))
        self.assertEqual({"schema_version": 1, "keep_forever": False}, policy.read_archive_policy(root))
        self.assertEqual(before, sorted(path.relative_to(root.path).as_posix() for path in root.path.rglob("*")))

    def test_policy_boolean_values_are_exact_and_read_only(self):
        for value in (True, False, True):
            self.write({"schema_version": 1, "keep_forever": value})
            before = self.snapshot()
            self.assertEqual({"schema_version": 1, "keep_forever": value}, policy.read_archive_policy(self.root))
            self.assertEqual(before, self.snapshot())

    def test_policy_closed_shape_and_native_numeric_version(self):
        self.write({"schema_version": 1.0, "keep_forever": True})
        self.assertEqual({"schema_version": 1, "keep_forever": True}, policy.read_archive_policy(self.root))
        invalid = [None, [], {}, {"schema_version": 0, "keep_forever": True},
            {"schema_version": True, "keep_forever": True},
            {"schema_version": "1", "keep_forever": True},
            {"schema_version": 1.5, "keep_forever": True},
            {"schema_version": 1, "keep_forever": 1},
            {"schema_version": 1, "keep_forever": "false"},
            {"schema_version": 1, "keep_forever": False, "age_days": 1}]
        for value in invalid:
            with self.subTest(value=value):
                self.write(value)
                self.refused(lambda: policy.read_archive_policy(self.root))

    def test_policy_duplicate_fields_nonfinite_utf8_and_json_refuse(self):
        samples = [b'{"schema_version":1,"keep_forever":true,"keep_forever":false}',
                   b'{"schema_version":NaN,"keep_forever":false}', b'\xff', b'[] trailing']
        for raw in samples:
            with self.subTest(raw=raw):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(raw)
                self.refused(lambda: policy.read_archive_policy(self.root))

    def test_policy_symlink_and_hardlink_are_refused(self):
        target = self.fixture.base / "foreign-policy.json"
        target.write_text('{"schema_version":1,"keep_forever":true}')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.symlink_to(target)
        self.refused(lambda: policy.read_archive_policy(self.root))
        self.path.unlink()
        os.link(target, self.path)
        self.refused(lambda: policy.read_archive_policy(self.root))

    def test_nonregular_policy_and_symlinked_parent_refuse(self):
        self.path.mkdir(parents=True)
        self.refused(lambda: policy.read_archive_policy(self.root))
        self.path.rmdir()
        state = self.path.parent
        moved = self.fixture.base / "foreign-state"
        state.rename(moved)
        state.symlink_to(moved, target_is_directory=True)
        self.refused(lambda: policy.read_archive_policy(self.root))

    def test_reader_owns_shared_epoch_and_uses_identity_size_plus_one(self):
        self.write({"schema_version": 1, "keep_forever": True})
        expected = self.path.stat()
        requests = []
        native = os.read
        def observed(fd, count):
            if os.fstat(fd).st_ino == expected.st_ino:
                bus_epoch._require_owned_epoch_guard(self.root)
                requests.append(count)
            return native(fd, count)
        with mock.patch.object(os, "read", observed):
            self.assertTrue(policy.read_archive_policy(self.root)["keep_forever"])
        self.assertTrue(requests, "policy bytes must pass the bounded descriptor reader")
        self.assertLessEqual(max(requests), expected.st_size + 1)

    def test_size_overflow_refuses_before_any_policy_read(self):
        self.write({"schema_version": 1, "keep_forever": True})
        with mock.patch.object(policy, "MAX_POLICY_BYTES", 8), mock.patch.object(
                os, "read", side_effect=AssertionError("oversized policy was read")):
            self.refused(lambda: policy.read_archive_policy(self.root))

    def test_substituted_file_refuses_before_reading_replacement(self):
        self.write({"schema_version": 1, "keep_forever": False})
        native = os.open
        replaced = []
        def substitute(path, flags, *args, **kwargs):
            if str(path) == "archive-policy.json" and not replaced:
                self.path.rename(self.fixture.base / "old-policy")
                self.write({"schema_version": 1, "keep_forever": True})
                replaced.append(True)
            return native(path, flags, *args, **kwargs)
        with mock.patch.object(os, "open", substitute):
            self.refused(lambda: policy.read_archive_policy(self.root))

    def test_substituted_parent_descriptor_is_not_read(self):
        self.write({"schema_version": 1, "keep_forever": False})
        foreign = self.fixture.base / "foreign-policy-parent"
        foreign.mkdir()
        (foreign / "archive-policy.json").write_text('{"schema_version":1,"keep_forever":true}')
        fd = os.open(foreign, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        native = os.open
        def substitute(path, flags, *args, **kwargs):
            return os.dup(fd) if str(path) == "state" else native(path, flags, *args, **kwargs)
        with mock.patch.object(os, "open", substitute):
            self.refused(lambda: policy.read_archive_policy(self.root))

    def test_changed_or_grown_policy_refuses_with_bounded_read(self):
        for change in ("append", "chmod"):
            with self.subTest(change=change):
                self.write({"schema_version": 1, "keep_forever": False})
                expected = self.path.stat()
                native = os.read
                changed, requests, observed_bytes = [], [], []
                def mutate(fd, count):
                    if os.fstat(fd).st_ino == expected.st_ino:
                        requests.append(count)
                        if not changed:
                            if change == "append":
                                with self.path.open("ab") as stream:
                                    stream.write(b" " * 5000)
                            else:
                                self.path.chmod(0o600 if expected.st_mode & 0o077 else 0o644)
                            changed.append(True)
                    block = native(fd, count)
                    if os.fstat(fd).st_ino == expected.st_ino:
                        observed_bytes.append(len(block))
                    return block
                with mock.patch.object(os, "read", mutate):
                    self.refused(lambda: policy.read_archive_policy(self.root))
                self.assertTrue(requests)
                self.assertLessEqual(max(requests), expected.st_size + 1)
                self.assertLessEqual(sum(observed_bytes), expected.st_size + 1)

    def test_native_manual_and_automatic_exact_thirty_day_boundary(self):
        for automatic in (False, True):
            receipt = self.roll(automatic)
            before = deepcopy(receipt)
            stamp = self.moment(receipt)
            for age, expired in ((29 * 86400, False), (30 * 86400 - 0.000001, False),
                                 (30 * 86400, True), (31 * 86400, True)):
                with self.subTest(automatic=automatic, age=age):
                    self.assertEqual({"retained": not expired, "expired": expired,
                        "keep_forever": False, "age_seconds": age},
                        policy.retention_decision(receipt, {"schema_version": 1, "keep_forever": False},
                                                  stamp + timedelta(seconds=age)))
            self.assertEqual(before, receipt)

    def test_keep_forever_toggle_changes_only_decision_data_and_ignores_mtime(self):
        receipt = self.roll()
        observed = self.moment(receipt) + timedelta(days=40)
        archive = Path(receipt["archive_path"])
        os.utime(archive, (0, 0))
        before = self.snapshot()
        for keep, expired in ((True, False), (False, True), (True, False)):
            result = policy.retention_decision(receipt, {"schema_version": 1, "keep_forever": keep}, observed)
            self.assertEqual({"retained": not expired, "expired": expired,
                "keep_forever": keep, "age_seconds": 3456000}, result)
            self.assertTrue(archive.exists())
        self.assertEqual(before, self.snapshot())

    def test_future_roll_refuses_even_with_keep_forever(self):
        receipt = self.roll()
        for keep in (False, True):
            self.refused(lambda: policy.retention_decision(receipt,
                {"schema_version": 1, "keep_forever": keep}, self.moment(receipt) - timedelta(microseconds=1)),
                "archive_retention_clock_invalid")

    def test_observation_must_be_explicit_aware_utc_datetime(self):
        receipt = self.roll()
        for observed in (None, receipt["timestamp"], datetime.now(),
                         datetime.now(timezone(timedelta(hours=1)))):
            self.refused(lambda: policy.retention_decision(receipt,
                {"schema_version": 1, "keep_forever": False}, observed), "archive_retention_clock_invalid")

    def test_decision_is_pure_validates_native_receipt_and_preserves_numeric_version(self):
        receipt = self.roll()
        receipt["schema_version"] = 1.0
        before = deepcopy(receipt)
        with mock.patch("builtins.open", side_effect=AssertionError("pure decision read filesystem")),\
                mock.patch.object(Path, "stat", side_effect=AssertionError("pure decision stat")):
            result = policy.retention_decision(receipt, {"schema_version": 1, "keep_forever": False},
                                              self.moment(receipt) + timedelta(days=30))
        self.assertTrue(result["expired"])
        self.assertEqual(before, receipt)
        for version in (0, True, "1"):
            hostile = {**receipt, "schema_version": version}
            self.refused(lambda: policy.retention_decision(hostile,
                {"schema_version": 1, "keep_forever": False}, self.moment(receipt)), "schema_version_invalid")

    def test_decision_refuses_invalid_policy_and_native_timestamp(self):
        receipt = self.roll()
        self.refused(lambda: policy.retention_decision(receipt, {"schema_version": 1, "keep_forever": 1},
                                                      self.moment(receipt)))
        hostile = {**receipt, "timestamp": "2026-02-30T00:00:00Z"}
        self.refused(lambda: policy.retention_decision(hostile,
            {"schema_version": 1, "keep_forever": False}, self.moment(receipt)), "timestamp_invalid")
