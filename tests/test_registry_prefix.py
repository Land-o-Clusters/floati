"""Selected registry truth is writer-advanced; expiry remains a live projection."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from floati import jsonl, records
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from tests.bus_scale_test_profile import full_scale_enabled, StableSourceFixture


class RegistryPrefixTests(StableSourceFixture, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.registry = Registry(self.root)
        self.registration = self.registry.register("builder", "Codex")
        self.path = self.root.resolve_relative("registry/entries.jsonl")
        self.bank = self.root.tenant_home / ".registry-prefix"

    def warm(self):
        with self.selected() as view:
            self.assertEqual(self.registration, view.require_active("builder"))
        self.assertTrue((self.bank / "checkpoint.json").is_file(),
            "selected registry reads need the ruled disposable KeyPrefix")

    @contextmanager
    def no_history_reads(self):
        native_open = Path.open
        def opening(path, mode="r", *args, **kwargs):
            if path == self.path and mode == "rb":
                self.fail("warm selected lookup replayed registry history")
            return native_open(path, mode, *args, **kwargs)
        with patch.object(Path, "open", opening):
            yield

    def selected(self, node="builder"):
        self.assertIn("node_id", inspect.signature(self.registry.read_once).parameters,
            "lane invocation needs a selected registry view without changing full read_once")
        return self.registry.read_once(node_id=node)

    def lease(self, expires_at):
        return {"schema_version": 0, "id": "lease-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": "2026-10-25T00:00:00.000Z",
            "kind": "node_lease", "node_id": "builder",
            "workspace": str(self.root.path / "nodes/builder"),
            "state": "active", "expires_at": expires_at}

    def test_registration_only_prelude_does_not_create_a_disposable_bank(self):
        self.assertFalse(self.bank.exists())
        self.registry.register("other", "Codex")
        self.assertFalse(self.bank.exists(), "missing banks are rebuilt by selected readers")

    def test_roster_keeps_active_latest_harness_and_nullable_role(self):
        from floati.registry_prefix import RegistryPrefix
        self.registry.register("other", "Claude")
        self.warm()
        prefix = RegistryPrefix(self.root)
        prefix.load()
        self.assertEqual([["builder", "Codex", None, None], ["other", "Claude", None, None]],
            prefix.get("roster", "active"))
        self.registry.retire("other")
        prefix = RegistryPrefix(self.root)
        prefix.load()
        self.assertEqual([["builder", "Codex", None, None]], prefix.get("roster", "active"))

    def test_warm_selected_io_is_independent_of_unrelated_registry_history(self):
        self.warm()
        for count in ((16, 160) if full_scale_enabled() else (2, 4)):
            for index in range(count):
                node = "history-" + str(index)
                if node not in self.registry.active_node_ids():
                    self.registry.register(node, "Codex")
            with self.no_history_reads(), Registry(self.root).read_once(node_id="builder") as view:
                self.assertEqual(self.registration, view.require_active("builder"))
                self.assertEqual("not_leased", view.node_lease_state("builder")["state"])

    def test_selected_invocation_keeps_latest_rows_and_computes_expiry_at_each_boundary(self):
        self.warm()
        now = datetime(2026, 10, 25, tzinfo=timezone.utc)
        first = self.lease("2026-10-25T00:00:01.000Z")
        last = self.lease("2026-10-25T00:00:05.000Z")
        jsonl.transact_records(self.root, "registry/entries.jsonl",
            lambda _rows: (None, [first, last]), allowed_kinds=REGISTRY_KINDS)
        with self.no_history_reads(), self.selected() as view:
            self.assertEqual(self.registration, view.require_active("builder"))
            self.assertEqual(last["id"], view.node_lease_state("builder", now=now)["node_lease_id"])
            self.assertEqual("active", view.node_lease_state("builder", now=now + timedelta(seconds=4))["state"])
            with self.assertRaises(ProtocolRefusal) as expired:
                view.require_protocol_lease("builder", now=now + timedelta(seconds=5), act="lane")
            self.assertEqual("node_lease_expired", expired.exception.code)

    def test_single_and_batch_registry_transactions_advance_under_the_registry_lock(self):
        import fcntl
        self.warm()
        before = json.loads((self.bank / "checkpoint.json").read_bytes())["payload"]
        self.registry.register("other", "Codex")
        lease = self.lease("2099-10-25T00:00:05.000Z")
        jsonl.transact_records(self.root, "registry/entries.jsonl",
            lambda _rows: (lease, [lease]), allowed_kinds=REGISTRY_KINDS)
        after = json.loads((self.bank / "checkpoint.json").read_bytes())["payload"]
        self.assertEqual(before["count"] + 2, after["count"])
        self.assertEqual(self.path.stat().st_size, after["offset"])
        self.assertNotEqual(before["head"], after["head"])
        from floati.registry_prefix import RegistryPrefix
        native = RegistryPrefix.appended
        observations = []
        def appended(prefix, *args, **kwargs):
            lock_path = self.path.with_name(self.path.name + ".lock")
            with lock_path.open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    observations.append("held")
                else:
                    observations.append("free")
                    fcntl.flock(lock, fcntl.LOCK_UN)
            return native(prefix, *args, **kwargs)
        with patch.object(RegistryPrefix, "appended", appended):
            self.registry.register("third", "Codex")
        self.assertEqual(["held"], observations)
        with self.no_history_reads(), self.selected() as view:
            self.assertEqual(lease["id"], view.node_lease_state("builder")["node_lease_id"])

    def test_retirement_replaces_the_selected_registration_and_preserves_native_refusal(self):
        self.warm()
        retired = self.registry.retire("builder")
        with self.selected() as view:
            self.assertEqual(retired, view._latest("builder"))
            with self.assertRaises(ProtocolRefusal) as caught:
                view.require_active("builder")
        self.assertEqual("unknown_node", caught.exception.code)
        self.assertEqual("node 'builder' is not active", caught.exception.detail)

    def test_missing_corrupt_and_foreign_banks_recover_through_native_validation(self):
        self.warm()
        foreign = self.base / "foreign-bank"
        other = FloatiRoot.open_direct_home(self.base / "foreign-fleet", create=True)
        Registry(other).register("builder", "Codex")
        with Registry(other).read_once(node_id="builder") as view:
            view.require_active("builder")
        shutil.copytree(other.tenant_home / ".registry-prefix", foreign)
        for mutation in ("missing", "corrupt", "foreign"):
            with self.subTest(mutation=mutation):
                shutil.rmtree(self.bank)
                if mutation == "corrupt":
                    self.bank.mkdir(mode=0o700)
                    (self.bank / "checkpoint.json").write_bytes(b"corrupt\n")
                elif mutation == "foreign":
                    shutil.copytree(foreign, self.bank)
                self.warm()
                with self.no_history_reads(), self.selected() as view:
                    view.require_active("builder")

    def test_reader_revision_upgrade_rebuilds_the_same_native_registry(self):
        self.warm()
        before = json.loads((self.bank / "checkpoint.json").read_bytes())["payload"]["binding"]["revision"]
        upgraded = records.READER_VERSION + "-upgrade-control"
        with patch.object(records, "READER_VERSION", upgraded):
            self.warm()
            after = json.loads((self.bank / "checkpoint.json").read_bytes())["payload"]["binding"]["revision"]
        self.assertNotEqual(before, after)
        self.assertIn("source", after)
        self.assertEqual(upgraded, after["reader_version"])

    def test_changed_malformed_unrelated_history_never_uses_a_stale_positive(self):
        self.warm()
        self.path.write_bytes(self.path.read_bytes() + b"broken unrelated frame\n")
        with self.assertRaises(IntegrityFailure) as native:
            self.registry.known_records()
        with self.assertRaises(IntegrityFailure) as indexed:
            with self.selected() as view:
                view.require_active("builder")
        self.assertEqual(native.exception.code, indexed.exception.code)

    def test_well_formed_future_kinds_keep_the_native_compatible_fallback(self):
        from floati.framing import encode_frame
        self.warm()
        future = {"schema_version": 2, "id": "future-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": "2026-10-25T00:00:00.000Z",
            "kind": "future_registry_kind", "payload": {"newer": True}}
        with self.path.open("ab") as stream:
            stream.write(encode_frame(future))
        before = self.path.read_bytes()
        with self.selected() as view:
            self.assertEqual(self.registration, view.require_active("builder"))
        self.assertEqual("future_registry_kind", self.registry.skip_receipt()["kind"])
        self.assertEqual(before, self.path.read_bytes())

    def test_selected_absence_cannot_hide_a_missing_sealed_positive_entry(self):
        self.warm()
        from floati.registry_prefix import RegistryPrefix
        from floati.send_prefix import _key
        prefix = RegistryPrefix(self.root)
        (prefix.nodes / prefix.filename(_key("registration", "builder"))).unlink()
        with self.selected() as view:
            self.assertEqual(self.registration, view.require_active("builder"))
        with self.no_history_reads(), self.selected() as view:
            view.require_active("builder")

    def test_a_superseded_valid_lease_entry_cannot_be_replayed_into_the_current_bank(self):
        self.warm()
        from floati.registry_prefix import RegistryPrefix
        from floati.send_prefix import _key
        first = self.lease("2098-10-25T00:00:00.000Z")
        latest = self.lease("2099-10-25T00:00:00.000Z")
        for row in (first, latest):
            jsonl.transact_records(self.root, "registry/entries.jsonl",
                lambda _prior: (row, [row]), allowed_kinds=REGISTRY_KINDS)
            prefix = RegistryPrefix(self.root)
            selected = prefix.nodes / prefix.filename(_key("lease", "builder"))
            if row is first:
                old_entry = selected.read_bytes()
        selected.write_bytes(old_entry)
        with self.selected() as view:
            self.assertEqual(latest["id"], view.node_lease_state("builder")["node_lease_id"])
        with self.no_history_reads(), self.selected() as view:
            self.assertEqual(latest["id"], view.node_lease_state("builder")["node_lease_id"])
