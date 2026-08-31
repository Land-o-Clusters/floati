from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.root import FloatiRoot
from tests.schema_validation import SchemaValidationError, validate_json_schema

try:
    from floati.gateway import GatewayConfig, LocalGatewayV0
except (ImportError, ModuleNotFoundError):
    GatewayConfig = None
    LocalGatewayV0 = None


NOW = datetime(2026, 8, 1, 15, 0, 0, tzinfo=timezone.utc)
FIXTURES = Path("tests/fixtures/gateway/v0")
SCHEMAS = Path("schemas/v0")


class GatewayContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(GatewayConfig, "gateway config contract must exist")
        self.assertIsNotNone(LocalGatewayV0, "local gateway dark implementation must exist")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.workspace_root = self.base / "workspaces"
        self.workspace_root.mkdir()
        self.config_path = self.base / "gateway.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "kind": "local_gateway_config",
                    "transport": "stdio",
                    "network": "disabled",
                    "workspace_root": str(self.workspace_root),
                    "approval_mode": "forward_fail_closed",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def gateway(self):
        return LocalGatewayV0(self.root, GatewayConfig.load(self.config_path))

    def test_versioned_fixtures_validate_and_reject_contract_mutations(self) -> None:
        pairs = (
            ("config.json", "local-gateway-config.schema.json"),
            ("session-ingress.json", "gateway-session-ingress-record.schema.json"),
            ("capability-declaration.json", "gateway-capability-declaration-record.schema.json"),
            ("approval-forward.json", "gateway-approval-forward-record.schema.json"),
        )
        for fixture_name, schema_name in pairs:
            with self.subTest(fixture=fixture_name):
                value = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
                validate_json_schema(value, SCHEMAS / schema_name)
                value["unexpected"] = True
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(value, SCHEMAS / schema_name)

        invalid_config = json.loads((FIXTURES / "config.json").read_text())
        invalid_config["network"] = "enabled"
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(invalid_config, SCHEMAS / "local-gateway-config.schema.json")

    def test_dark_round_trip_is_local_sorted_and_fail_closed(self) -> None:
        gateway = self.gateway()
        workspace = self.workspace_root / "session-a"
        workspace.mkdir()

        ingress = gateway.ingress(
            "session-019fbb00000070008000000000000011",
            public_ids.builder('a'),
            workspace,
            now=NOW,
        )
        declaration = gateway.declare(
            ingress["session_id"],
            ["workspace.write", "approval.forward", "workspace.write"],
            now=NOW,
        )
        approval = gateway.forward_approval(
            ingress["session_id"],
            "approval-request-019fbb00000070008000000000000012",
            "workspace.write",
            ["PROOF.txt"],
            now=NOW,
        )

        self.assertEqual("stdio", ingress["transport"])
        self.assertEqual(
            ["approval.forward", "workspace.write"], declaration["capabilities"]
        )
        self.assertEqual("forwarded_unresolved", approval["state"])
        self.assertEqual(
            ["gateway_session_ingress", "gateway_capability_declaration", "gateway_approval_forward"],
            [row["kind"] for row in gateway.records()],
        )
        for forbidden in (
            "events.jsonl",
            "work/items.jsonl",
            "leases/authority.jsonl",
        ):
            self.assertFalse(self.root.resolve_relative(forbidden).exists(), forbidden)

    def test_ingress_requires_lexically_confined_workspace(self) -> None:
        gateway = self.gateway()
        outside = self.base / "outside"
        outside.mkdir()

        with self.assertRaisesRegex(ProtocolRefusal, "gateway_workspace_outside_root"):
            gateway.ingress(
                "session-019fbb00000070008000000000000013",
                public_ids.builder('a'),
                outside,
                now=NOW,
            )
        self.assertEqual([], gateway.records())

    def test_declaration_and_approval_require_durable_session_capability_chain(self) -> None:
        gateway = self.gateway()
        session_id = "session-019fbb00000070008000000000000014"
        with self.assertRaisesRegex(ProtocolRefusal, "gateway_session_missing"):
            gateway.declare(session_id, ["approval.forward"], now=NOW)

        workspace = self.workspace_root / "session-chain"
        workspace.mkdir()
        gateway.ingress(session_id, public_ids.builder('a'), workspace, now=NOW)
        gateway.declare(session_id, ["approval.forward"], now=NOW)
        with self.assertRaisesRegex(ProtocolRefusal, "gateway_capability_missing"):
            gateway.forward_approval(
                session_id,
                "approval-request-019fbb00000070008000000000000015",
                "workspace.write",
                ["PROOF.txt"],
                now=NOW,
            )

    def test_config_rejects_remote_relative_symlink_and_unknown_shapes(self) -> None:
        for field, value in (
            ("transport", "http"),
            ("network", "enabled"),
            ("workspace_root", "relative/work"),
        ):
            raw = json.loads(self.config_path.read_text())
            raw[field] = value
            candidate = self.base / f"{field}.json"
            candidate.write_text(json.dumps(raw), encoding="utf-8")
            with self.subTest(field=field), self.assertRaisesRegex(
                ProtocolRefusal, "gateway_config_malformed"
            ):
                GatewayConfig.load(candidate)

        raw = json.loads(self.config_path.read_text())
        raw["extra"] = True
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(ProtocolRefusal, "gateway_config_malformed"):
            GatewayConfig.load(self.config_path)

        target = self.base / "target.json"
        target.write_text((FIXTURES / "config.json").read_text(), encoding="utf-8")
        alias = self.base / "alias.json"
        alias.symlink_to(target)
        with self.assertRaisesRegex(ProtocolRefusal, "gateway_config_identity_invalid"):
            GatewayConfig.load(alias)

    def test_malformed_persisted_gateway_record_is_integrity_failure(self) -> None:
        path = self.root.resolve_relative("gateway/events.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"kind":"gateway_session_ingress"}\n', encoding="utf-8")

        with self.assertRaises(IntegrityFailure):
            self.gateway().records()

    def test_unhashable_capability_and_scope_members_refuse_without_append(self) -> None:
        gateway = self.gateway()
        workspace = self.workspace_root / "session-malformed"
        workspace.mkdir()
        session_id = "session-019fbb00000070008000000000000016"
        gateway.ingress(session_id, public_ids.builder('a'), workspace, now=NOW)

        with self.assertRaisesRegex(ProtocolRefusal, "gateway_capabilities_invalid"):
            gateway.declare(session_id, ["approval.forward", []], now=NOW)
        gateway.declare(
            session_id, ["approval.forward", "workspace.write"], now=NOW
        )
        with self.assertRaisesRegex(ProtocolRefusal, "gateway_scope_invalid"):
            gateway.forward_approval(
                session_id,
                "approval-request-019fbb00000070008000000000000017",
                "workspace.write",
                ["PROOF.txt", []],
                now=NOW,
            )
        self.assertEqual(
            ["gateway_session_ingress", "gateway_capability_declaration"],
            [row["kind"] for row in gateway.records()],
        )


if __name__ == "__main__":
    unittest.main()
