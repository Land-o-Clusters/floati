"""Role template write evidence and frozen role assignment compatibility."""
import json
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.records import validate_record
from tests.schema_validation import SchemaValidationError, validate_json_schema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/v1/role-template-write-record.schema.json"
KIND = "role_template_write_receipt"
UUID = "00000000000070008000000000000001"
OLD_BYTES = b'{"schema_version":0,"id":"registry-role-00000000000070008000000000000001","tenant_id":"fleet","timestamp":"2026-08-27T23:15:00.000Z","kind":"registry_role_record","node_id":"alpha","template_role":"builder","template_version":2,"template_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","answers":{"repo":"floati"},"state":"active","predecessor_role_record_id":null}'


def receipt(**changes):
    row = dict(schema_version=1, id="role-template-write-" + UUID,
               tenant_id="fleet", timestamp="2026-09-05T12:00:00.000Z", kind=KIND,
               operation="new", role="custom-role", path="roles/custom/custom-role.json",
               state="prepared", request_sha256="a" * 64, before_sha256=None,
               after_sha256="b" * 64, template_sha256="c" * 64,
               idempotency_key="write-1", predecessor_receipt_id=None)
    row.update(changes)
    return row


class FrozenRoleAssignmentTests(unittest.TestCase):
    def test_original_bytes_validate_without_shape_or_version_migration(self):
        row = json.loads(OLD_BYTES)
        self.assertEqual(row, validate_record(row, "fleet", {"registry_role_record"}, integrity=False))
        validate_json_schema(row, ROOT / "schemas/v0/registry-role-record.schema.json")
        self.assertEqual(OLD_BYTES, json.dumps(row, separators=(",", ":")).encode())


class RoleTemplateWriteRecordTests(unittest.TestCase):
    def test_operations_states_and_nullable_prior_digest(self):
        for operation in ("new", "import", "edit"):
            for state in ("prepared", "applied"):
                for before in (("d" * 64,) if operation == "edit" else (None,)):
                    with self.subTest(operation=operation, state=state, before=before):
                        row = receipt(operation=operation, state=state, before_sha256=before,
                                      predecessor_receipt_id=None if state == "prepared" else "role-template-write-" + UUID)
                        self.assertEqual(row, validate_record(row, "fleet", {KIND}, integrity=False))
                        validate_json_schema(row, SCHEMA)

    def test_runtime_and_schema_reject_invalid_evidence(self):
        changes = [dict(schema_version=0), dict(schema_version=True), dict(role="../bad"),
                   dict(path="roles/custom/../custom-role.json"), dict(path="/roles/custom/custom-role.json"),
                   dict(operation="delete"), dict(state="done"), dict(request_sha256="A" * 64),
                   dict(before_sha256="a" * 63), dict(after_sha256=None), dict(template_sha256="no"),
                   dict(idempotency_key=""), dict(idempotency_key="x" * 129), dict(idempotency_key="bad\nkey"),
                   dict(predecessor_receipt_id="role-template-write-" + UUID),
                   dict(state="applied", predecessor_receipt_id=None),
                   dict(state="applied", predecessor_receipt_id="registry-role-" + UUID),
                   dict(source_path="private.json"), dict(template={"role": "custom-role"}),
                   dict(id="role-template-write-" + "0" * 32)]
        for change in changes:
            row = receipt(**change)
            with self.subTest(change=change):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(row, "fleet", {KIND}, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(row, SCHEMA)

    def test_operation_requires_the_matching_prior_file_evidence(self):
        for operation in ("new", "import", "edit"):
            for state in ("prepared", "applied"):
                row = receipt(operation=operation, state=state,
                              before_sha256=None if operation == "edit" else "d" * 64,
                              predecessor_receipt_id=None if state == "prepared" else "role-template-write-" + UUID)
                with self.subTest(operation=operation, state=state):
                    with self.assertRaises(ProtocolRefusal):
                        validate_record(row, "fleet", {KIND}, integrity=False)
                    with self.assertRaises(SchemaValidationError):
                        validate_json_schema(row, SCHEMA)

    def test_path_is_bound_to_the_declared_role(self):
        row = receipt(path="roles/custom/another-role.json")
        with self.assertRaises(ProtocolRefusal):
            validate_record(row, "fleet", {KIND}, integrity=False)


if __name__ == "__main__":
    unittest.main()
