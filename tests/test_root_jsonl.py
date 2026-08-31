from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

try:
    from floati.errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
    from floati.root import FloatiRoot
except ModuleNotFoundError:
    DurabilityFailure = None
    IntegrityFailure = None
    ProtocolRefusal = None
    FloatiRoot = None

try:
    from floati.jsonl import append_record, read_records
except ModuleNotFoundError:
    append_record = None
    read_records = None


class RootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(FloatiRoot, "floati.root must implement the explicit-root contract")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_missing_or_relative_root_refuses(self) -> None:
        with self.assertRaises(ProtocolRefusal) as missing:
            FloatiRoot.open(None, "alpha")
        self.assertEqual("root_required", missing.exception.code)
        with self.assertRaises(ProtocolRefusal) as relative:
            FloatiRoot.open(Path("relative"), "alpha")
        self.assertEqual("root_not_absolute", relative.exception.code)

    def test_missing_or_invalid_tenant_refuses(self) -> None:
        for tenant in (None, "", ".", "..", "Alpha", "a/b", "-alpha", "alpha-"):
            with self.subTest(tenant=tenant):
                with self.assertRaises(ProtocolRefusal) as caught:
                    FloatiRoot.open(self.base, tenant)
                self.assertEqual("tenant_invalid", caught.exception.code)

    def test_valid_root_creates_only_selected_tenant(self) -> None:
        root = FloatiRoot.open(self.base, "alpha-1")
        self.assertEqual(self.base.resolve(), root.path)
        self.assertEqual(self.base.resolve() / "tenants" / "alpha-1", root.tenant_home)
        self.assertTrue(root.tenant_home.is_dir())
        self.assertEqual([Path("alpha-1")], [p.relative_to(self.base / "tenants") for p in (self.base / "tenants").iterdir()])

    def test_cross_tenant_observation_requires_explicit_capability(self) -> None:
        root = FloatiRoot.open(self.base, "alpha")
        FloatiRoot.open(self.base, "bravo")
        denied = root.grant_observation("charlie")
        with self.assertRaises(ProtocolRefusal) as caught:
            root.observe_tenant(denied, "bravo")
        self.assertEqual("observation_not_granted", caught.exception.code)
        granted = root.grant_observation("bravo")
        observation = root.observe_tenant(granted, "bravo")
        self.assertEqual("bravo", observation.tenant_id)
        self.assertFalse(hasattr(observation, "tenant_home"), "read capability must not disclose a writable path")

    def test_root_and_observation_capabilities_are_not_forgeable(self) -> None:
        with self.assertRaises(TypeError):
            FloatiRoot(self.base, "alpha", self.base / "tenants" / "alpha")
        root = FloatiRoot.open(self.base, "alpha")
        with self.assertRaises(TypeError):
            type(root.grant_observation("alpha"))(frozenset(("alpha",)))

    def test_relative_resolution_rejects_traversal_and_symlink_escape(self) -> None:
        root = FloatiRoot.open(self.base, "alpha")
        FloatiRoot.open(self.base, "bravo")
        for relative in (Path("../bravo/events.jsonl"), Path("\x2ftmp/outside.jsonl"), Path("a/../../bravo/x")):
            with self.subTest(relative=relative):
                with self.assertRaises(ProtocolRefusal) as caught:
                    root.resolve_relative(relative)
                self.assertEqual("path_not_contained", caught.exception.code)
        link = root.tenant_home / "escape"
        link.symlink_to(self.base / "tenants" / "bravo", target_is_directory=True)
        with self.assertRaises(ProtocolRefusal) as caught:
            root.resolve_relative("escape/events.jsonl")
        self.assertEqual("path_not_contained", caught.exception.code)


class JsonlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(append_record, "floati.jsonl must implement durable JSONL")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = FloatiRoot.open(self.base, "alpha")
        self.path = Path("events.jsonl")

    def record(self, record_id: str = "r-1", tenant_id: str = "alpha") -> dict:
        return {
            "schema_version": 0,
            "id": "registry-018f7e9b3c117abc8def0123456789ab" if record_id == "r-1" else "registry-018f7e9b3c127abc8def0123456789ab",
            "tenant_id": tenant_id,
            "timestamp": "2026-07-31T12:00:00.000Z",
            "kind": "registry_entry",
            "node_id": record_id,
            "role": "worker",
            "state": "active",
        }

    def test_append_writes_one_complete_line_and_round_trips(self) -> None:
        append_record(self.root, self.path, self.record(), allowed_kinds={"registry_entry"})
        data = self.root.resolve_relative(self.path).read_bytes()
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(1, data.count(b"\n"))
        self.assertEqual([self.record()], read_records(self.root, self.path, allowed_kinds={"registry_entry"}))

    def test_multiple_records_preserve_order(self) -> None:
        append_record(self.root, self.path, self.record("r-1"), allowed_kinds={"registry_entry"})
        append_record(self.root, self.path, self.record("r-2"), allowed_kinds={"registry_entry"})
        self.assertEqual(["r-1", "r-2"], [item["node_id"] for item in read_records(self.root, self.path, allowed_kinds={"registry_entry"})])

    def test_oversized_record_refuses_without_mutation(self) -> None:
        append_record(self.root, self.path, self.record(), allowed_kinds={"registry_entry"})
        absolute = self.root.resolve_relative(self.path)
        before = absolute.read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(self.root, self.path, self.record("r-2"), allowed_kinds={"registry_entry"}, max_bytes=10)
        self.assertEqual("record_too_large", caught.exception.code)
        self.assertEqual(before, absolute.read_bytes())

    def test_incomplete_or_malformed_input_refuses(self) -> None:
        absolute = self.root.resolve_relative(self.path)
        absolute.write_bytes(b'{"id":"r-1","tenant_id":"alpha"}')
        with self.assertRaises(IntegrityFailure) as incomplete:
            read_records(self.root, self.path, allowed_kinds={"registry_entry"})
        self.assertEqual("incomplete_jsonl_line", incomplete.exception.code)
        absolute.write_bytes(b"{bad}\n")
        with self.assertRaises(IntegrityFailure) as malformed:
            read_records(self.root, self.path, allowed_kinds={"registry_entry"})
        self.assertEqual("malformed_json", malformed.exception.code)

    def test_non_object_and_duplicate_ids_refuse(self) -> None:
        absolute = self.root.resolve_relative(self.path)
        absolute.write_text("[]\n", encoding="utf-8")
        with self.assertRaises(IntegrityFailure) as non_object:
            read_records(self.root, self.path, allowed_kinds={"registry_entry"})
        self.assertEqual("record_not_object", non_object.exception.code)
        row = json.dumps(self.record(), separators=(",", ":"))
        absolute.write_text(f"{row}\n{row}\n", encoding="utf-8")
        with self.assertRaises(IntegrityFailure) as duplicate:
            read_records(self.root, self.path, allowed_kinds={"registry_entry"})
        self.assertEqual("duplicate_record_id", duplicate.exception.code)

    def test_wrong_tenant_refuses_before_append(self) -> None:
        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(self.root, self.path, self.record(tenant_id="bravo"), allowed_kinds={"registry_entry"})
        self.assertEqual("tenant_mismatch", caught.exception.code)
        self.assertFalse(self.root.resolve_relative(self.path).exists())

    def test_observation_can_read_but_cannot_be_used_for_append(self) -> None:
        other = FloatiRoot.open(self.base, "bravo")
        append_record(other, "events.jsonl", {**self.record(), "tenant_id": "bravo"}, allowed_kinds={"registry_entry"})
        observation = self.root.observe_tenant(self.root.grant_observation("bravo"), "bravo")
        before = sorted(path.relative_to(other.tenant_home) for path in other.tenant_home.rglob("*"))
        records = read_records(observation, "events.jsonl", allowed_kinds={"registry_entry"})
        after = sorted(path.relative_to(other.tenant_home) for path in other.tenant_home.rglob("*"))
        self.assertEqual("bravo", records[0]["tenant_id"])
        self.assertEqual(before, after, "observation must not create lock or metadata files")
        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(observation, "events.jsonl", records[0], allowed_kinds={"registry_entry"})
        self.assertEqual("write_root_required", caught.exception.code)

    def test_short_write_restores_previous_complete_ledger(self) -> None:
        append_record(self.root, self.path, self.record(), allowed_kinds={"registry_entry"})
        absolute = self.root.resolve_relative(self.path)
        before = absolute.read_bytes()
        real_write = __import__("os").write

        def short_write(descriptor: int, data: bytes) -> int:
            real_write(descriptor, data[: len(data) // 2])
            return len(data) // 2

        with mock.patch("floati.jsonl.os.write", side_effect=short_write):
            with self.assertRaises(DurabilityFailure) as caught:
                append_record(self.root, self.path, self.record("r-2"), allowed_kinds={"registry_entry"})
        self.assertEqual("short_write", caught.exception.code)
        self.assertEqual(before, absolute.read_bytes())
        self.assertEqual(1, len(read_records(self.root, self.path, allowed_kinds={"registry_entry"})))

    def test_total_ledger_record_bound_is_enforced_on_write_and_read(self) -> None:
        with mock.patch("floati.jsonl.MAX_LEDGER_RECORDS", 1):
            append_record(self.root, self.path, self.record(), allowed_kinds={"registry_entry"})
            with self.assertRaises(ProtocolRefusal) as write_failure:
                append_record(self.root, self.path, self.record("r-2"), allowed_kinds={"registry_entry"})
            self.assertEqual("ledger_record_limit", write_failure.exception.code)
        with mock.patch("floati.jsonl.MAX_LEDGER_RECORDS", 0):
            with self.assertRaises(IntegrityFailure) as read_failure:
                read_records(self.root, self.path, allowed_kinds={"registry_entry"})
            self.assertEqual("ledger_record_limit", read_failure.exception.code)


if __name__ == "__main__":
    unittest.main()
