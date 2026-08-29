from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.graph import HarborGraph
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema

try:
    from floati.bridge import BRIDGE_KINDS, LocalBridgeV0
except (ImportError, ModuleNotFoundError):
    BRIDGE_KINDS = set()
    LocalBridgeV0 = None


NOW = datetime(2026, 8, 1, 15, 0, 0, tzinfo=timezone.utc)


class LocalBridgeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.left = FloatiRoot.open_direct_home(base / "alpha", create=True)
        self.right = FloatiRoot.open_direct_home(base / "beta", create=True)
        Registry(self.left).register("alice", "Codex")
        Registry(self.right).register("bob", "Claude")

    def bridge(self, left=None, right=None):
        self.assertIsNotNone(LocalBridgeV0, "local bridge dark implementation must exist")
        return LocalBridgeV0(self.left if left is None else left, self.right if right is None else right)

    def records(self, root, relative, kinds):
        return read_records_snapshot(root, relative, allowed_kinds=kinds)

    def consent_both(self, bridge):
        left = bridge.consent(self.left, self.right, "alice", now=NOW)
        right = bridge.consent(self.right, self.left, "bob", now=NOW)
        return left, right

    def test_two_root_round_trip_is_advisory_and_never_consumption(self) -> None:
        bridge = self.bridge()
        left_consent, right_consent = self.consent_both(bridge)
        bridge.establish(now=NOW)

        outbound = bridge.forward(
            self.left, "alice", "bob", "slipway", "a" * 40,
            "docs/evidence/alpha.md", "alpha to beta", now=NOW,
        )
        inbound = bridge.forward(
            self.right, "bob", "alice", "slipway", "b" * 40,
            "docs/evidence/beta.md", "beta to alpha", now=NOW,
        )

        self.assertEqual("advisory_not_consumption", outbound["stamp"])
        self.assertEqual("advisory_not_consumption", inbound["stamp"])
        self.assertEqual(left_consent["id"], outbound["source_consent_id"])
        self.assertEqual(right_consent["id"], outbound["destination_consent_id"])
        for root in (self.left, self.right):
            forwards = self.records(root, "bridges/forwards.jsonl", {"bridge_forward"})
            self.assertEqual(2, len(forwards))
            self.assertEqual({"inbound", "outbound"}, {row["direction"] for row in forwards})
            self.assertTrue(all(row["stamp"] == "advisory_not_consumption" for row in forwards))
            self.assertFalse(root.resolve_relative("events.jsonl").exists())
            self.assertFalse(root.resolve_relative("work/items.jsonl").exists())

        left_stubs = HarborGraph(self.left).artifact()["bridge_stubs"]
        right_stubs = HarborGraph(self.right).artifact()["bridge_stubs"]
        self.assertEqual("beta", left_stubs[0]["peer_tenant_id"])
        self.assertEqual("alpha", right_stubs[0]["peer_tenant_id"])

    def test_missing_or_revoked_consent_refuses_and_records_both_sides(self) -> None:
        bridge = self.bridge()
        bridge.consent(self.left, self.right, "alice", now=NOW)
        with self.assertRaises(ProtocolRefusal) as missing:
            bridge.establish(now=NOW)
        self.assertEqual("bridge_consent_missing", missing.exception.code)
        for root in (self.left, self.right):
            denials = self.records(root, "bridges/denials.jsonl", {"bridge_denial"})
            self.assertEqual("bridge_consent_missing", denials[-1]["reason_code"])

        bridge = self.bridge()
        self.consent_both(bridge)
        bridge.establish(now=NOW)
        bridge.revoke(self.right, self.left, "bob", now=NOW)
        with self.assertRaises(ProtocolRefusal) as revoked:
            bridge.forward(
                self.left, "alice", "bob", "slipway", "a" * 40,
                "docs/evidence/refused.md", "must refuse", now=NOW,
            )
        self.assertEqual("bridge_consent_revoked", revoked.exception.code)
        for root in (self.left, self.right):
            self.assertEqual(
                "bridge_consent_revoked",
                self.records(root, "bridges/denials.jsonl", {"bridge_denial"})[-1]["reason_code"],
            )

    def test_same_root_and_remote_transport_fail_closed_with_denials(self) -> None:
        same = self.bridge(self.left, self.left)
        with self.assertRaises(ProtocolRefusal) as same_root:
            same.establish(now=NOW)
        self.assertEqual("bridge_same_root", same_root.exception.code)
        self.assertEqual(
            1,
            len(self.records(self.left, "bridges/denials.jsonl", {"bridge_denial"})),
        )

        bridge = self.bridge()
        self.consent_both(bridge)
        bridge.establish(now=NOW)
        with self.assertRaises(ProtocolRefusal) as remote:
            bridge.forward(
                self.left, "alice", "bob", "slipway", "a" * 40,
                "docs/evidence/refused.md", "must refuse", now=NOW,
                transport="https",
            )
        self.assertEqual("bridge_transport_forbidden", remote.exception.code)
        for root in (self.left, self.right):
            self.assertEqual(
                "bridge_transport_forbidden",
                self.records(root, "bridges/denials.jsonl", {"bridge_denial"})[-1]["reason_code"],
            )

    def test_malformed_actor_refusal_still_writes_denials_to_both_roots(self) -> None:
        bridge = self.bridge()
        self.consent_both(bridge)
        bridge.establish(now=NOW)

        with self.assertRaises(ProtocolRefusal) as malformed:
            bridge.forward(
                self.left, "NOT VALID", "bob", "slipway", "a" * 40,
                "docs/evidence/refused.md", "must refuse", now=NOW,
            )

        self.assertEqual("bridge_sender_inactive", malformed.exception.code)
        for root in (self.left, self.right):
            self.assertEqual(
                "bridge_sender_inactive",
                self.records(root, "bridges/denials.jsonl", {"bridge_denial"})[-1]["reason_code"],
            )

    def test_bridge_fixtures_validate_against_exact_contracts(self) -> None:
        names = (
            ("bridge-consent-alpha.json", "bridge-consent-receipt.schema.json"),
            ("bridge-record.json", "bridge-record.schema.json"),
            ("bridge-forward.json", "bridge-forward-receipt.schema.json"),
            ("bridge-denial.json", "bridge-denial-receipt.schema.json"),
        )
        for name, schema_name in names:
            fixture = json.loads((Path("tests/fixtures/bridge/v0") / name).read_text(encoding="utf-8"))
            schema = Path("schemas/v0") / schema_name
            validate_json_schema(fixture, schema)


if __name__ == "__main__":
    unittest.main()
