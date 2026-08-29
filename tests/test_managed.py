from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema

try:
    from floati.managed import ManagedSessions
except (ImportError, ModuleNotFoundError):
    ManagedSessions = None


NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 1, 12, 1, 0, tzinfo=timezone.utc)


class ManagedSessionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(self.temp.name) / "alpha", create=True)
        Registry(self.root).register("observer-manager", "Observer")
        self.lease = AuthorityGrantStore(self.root).claim(
            "managed-mode", "observer-manager", 300, 240, NOW
        )

    def manager(self):
        self.assertIsNotNone(ManagedSessions, "managed-session dark implementation must exist")
        return ManagedSessions(self.root)

    def test_versioned_adoption_and_release_fixtures_validate(self) -> None:
        for fixture_name, schema_name in (
            ("session-adoption.json", "session-adoption-record.schema.json"),
            ("session-release.json", "session-release-record.schema.json"),
        ):
            fixture = Path("tests/fixtures/managed/v0") / fixture_name
            schema = Path("schemas/v0") / schema_name
            validate_json_schema(json.loads(fixture.read_text(encoding="utf-8")), schema)

    def test_adoption_and_release_project_exact_managed_state(self) -> None:
        sessions = self.manager()
        adoption = sessions.adopt(
            "hm-session-1",
            "observer-manager",
            "managed-mode",
            int(self.lease["epoch"]),
            str(self.lease["expires_at"]),
            now=NOW,
        )

        self.assertEqual("MANAGED", adoption["mode"])
        self.assertEqual("adopted", sessions.project()["hm-session-1"]["state"])
        release = sessions.release(
            "hm-session-1",
            "observer-manager",
            int(self.lease["epoch"]),
            now=LATER,
        )

        projected = sessions.project()["hm-session-1"]
        self.assertEqual("released", projected["state"])
        self.assertEqual(adoption["id"], release["adoption_id"])
        self.assertEqual("observer-manager", projected["manager_node_id"])
        self.assertEqual("managed-mode", projected["lease_subject"])

    def test_adoption_refuses_wrong_or_expired_lease_without_mutation(self) -> None:
        sessions = self.manager()
        with self.assertRaises(ProtocolRefusal) as wrong_epoch:
            sessions.adopt(
                "hm-session-1", "observer-manager", "managed-mode", 9,
                str(self.lease["expires_at"]), now=NOW,
            )
        self.assertEqual("managed_lease_mismatch", wrong_epoch.exception.code)

        with self.assertRaises(ProtocolRefusal) as expired:
            sessions.adopt(
                "hm-session-1", "observer-manager", "managed-mode",
                int(self.lease["epoch"]), str(self.lease["expires_at"]),
                now=datetime(2026, 8, 1, 12, 6, 0, tzinfo=timezone.utc),
            )
        self.assertEqual("managed_lease_expired", expired.exception.code)
        self.assertEqual({}, sessions.project())

    def test_duplicate_adoption_and_stale_release_refuse(self) -> None:
        sessions = self.manager()
        sessions.adopt(
            "hm-session-1", "observer-manager", "managed-mode",
            int(self.lease["epoch"]), str(self.lease["expires_at"]), now=NOW,
        )
        with self.assertRaises(ProtocolRefusal) as duplicate:
            sessions.adopt(
                "hm-session-1", "observer-manager", "managed-mode",
                int(self.lease["epoch"]), str(self.lease["expires_at"]), now=NOW,
            )
        self.assertEqual("managed_session_already_adopted", duplicate.exception.code)

        with self.assertRaises(ProtocolRefusal) as stale:
            sessions.release("hm-session-1", "observer-manager", 99, now=LATER)
        self.assertEqual("managed_release_mismatch", stale.exception.code)
        self.assertEqual("adopted", sessions.project()["hm-session-1"]["state"])

    def test_malformed_persisted_session_record_is_integrity_failure(self) -> None:
        path = self.root.resolve_relative("managed/sessions.jsonl")
        path.parent.mkdir(parents=True)
        path.write_text('{"kind":"session_adoption"}\n', encoding="utf-8")

        with self.assertRaises(IntegrityFailure):
            self.manager().project()


if __name__ == "__main__":
    unittest.main()
