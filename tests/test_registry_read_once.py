"""Invocation-local registry queries retain registration and lease semantics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from floati.errors import FloatiError, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot


class RegistryReadOnceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        self.registry = Registry(self.root)
        self.registry.register("builder", "Codex")
        self.now = datetime(2026, 10, 25, tzinfo=timezone.utc)

    def _registration(self, state):
        row = {"schema_version": 0, "id": "registry-" + uuid7_hex(),
               "tenant_id": self.root.tenant_id, "timestamp": "2026-10-25T00:00:00.000Z",
               "kind": "registry_entry", "node_id": "builder", "role": "Codex", "state": state}
        append_record(self.root, self.registry.relative_path, row, allowed_kinds=REGISTRY_KINDS)
        return row

    def _lease(self, expires_at):
        row = {"schema_version": 0, "id": "lease-" + uuid7_hex(),
               "tenant_id": self.root.tenant_id, "timestamp": "2026-10-25T00:00:00.000Z",
               "kind": "node_lease", "node_id": "builder",
               "workspace": str(self.root.path / "nodes/builder"),
               "state": "active", "expires_at": expires_at}
        append_record(self.root, self.registry.relative_path, row, allowed_kinds=REGISTRY_KINDS)
        return row

    def test_read_once_uses_latest_registration_and_lease_at_exact_expiry(self):
        """Catches selecting the first row, sorting by time, or making expiry inclusive."""
        self.registry.register("alpha", "Codex")
        self._registration("retired")
        latest = self._registration("active")
        self._lease("2026-10-25T00:00:01.000Z")
        lease = self._lease("2026-10-25T00:00:05.000Z")
        with self.registry.read_once() as registry:
            self.assertEqual(("alpha", "builder"), registry.active_node_ids())
            self.assertEqual(latest, registry.require_active("builder"))
            self.assertEqual(latest, registry._latest("builder"))
            self.assertEqual({"node_lease_id": lease["id"], "state": "active",
                              "expires_at": "2026-10-25T00:00:05.000Z"},
                             registry.node_lease_state("builder", now=self.now + timedelta(seconds=4)))
            self.assertEqual("expired", registry.node_lease_state(
                "builder", now=self.now + timedelta(seconds=5))["state"])
            with self.assertRaises(ProtocolRefusal) as caught:
                registry.require_protocol_lease("builder", now=self.now + timedelta(seconds=5),
                                                act="lane workspace operation")
            self.assertEqual("node_lease_expired", caught.exception.code)
            self.assertEqual("lane workspace operation refused: node builder lease " + lease["id"]
                             + " expired at 2026-10-25T00:00:05.000Z", caught.exception.detail)

    def test_read_once_drops_view_on_scope_exit_and_exception(self):
        """Catches an invocation's parsed rows leaking into a later invocation."""
        with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
            with self.registry.read_once() as registry:
                self.assertEqual(("builder",), registry.active_node_ids())
                raise RuntimeError("fixture interruption")
        latest = self._registration("retired")
        with self.registry.read_once() as registry:
            self.assertEqual((), registry.active_node_ids())
            self.assertEqual(latest, registry._latest("builder"))
            with self.assertRaises(ProtocolRefusal) as caught:
                registry.require_active("builder")
            self.assertEqual("unknown_node", caught.exception.code)
            self.assertEqual("node 'builder' is not active", caught.exception.detail)
        latest = self._registration("active")
        self.assertEqual(latest, self.registry.require_active("builder"))

    def test_read_once_validates_the_complete_registry_before_yielding(self):
        """Catches a selected-node shortcut ignoring malformed unrelated history."""
        self.registry.path.write_bytes(self.registry.path.read_bytes() + b"broken frame\n")
        with self.assertRaises(FloatiError):
            with self.registry.read_once():
                self.fail("malformed registry yielded a query view")


if __name__ == "__main__":
    unittest.main()
