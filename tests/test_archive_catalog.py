"""Native commit authority and exact immutable chunk verification foundation."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from floati import bus_epoch, jsonl
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.framing import encode_frame
from floati.events import EventLog
from floati.records import validate_record
from tests import test_epoch_history as native_fixture
from tests.schema_validation import SchemaValidationError, validate_json_schema


KIND = "epoch_history_commit"
DOMAIN = b"floati-epoch-history-generation-v1\0"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode()


def generation_digest(row):
    return hashlib.sha256(DOMAIN + canonical({key: value for key, value in row.items()
        if key not in {"id", "timestamp", "kind", "schema_version", "generation"}})).hexdigest()


class ArchiveCatalogTests(unittest.TestCase):
    def setUp(self):
        native_fixture.EpochHistoryTests.setUp(self)
        self.chunks = []
        for domain, entries in (
            ("catalog", [{"receipt": receipt} for receipt in self.receipts]),
            ("sources", [{"archive_path": str(path), "witness": [
                [name, plane, list(bus_epoch._selected_member_identity(status))]
                for name, plane, status in bus_epoch._epoch_archive_snapshot(path)]}
                for path in self.paths]),
        ):
            payload = encode_frame({"schema_version": 1, "tenant_id": self.root.tenant_id,
                                    "domain": domain, "ordinal": 0, "entries": entries})
            digest = hashlib.sha256(payload).hexdigest()
            path = self.root.path / "state/epoch-history/v1/chunks" / (digest + ".jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            self.chunks.append((path, payload, {"domain": domain, "ordinal": 0,
                                "byte_length": len(payload), "sha256": digest}))
        events = (self.root.path / "events.jsonl").read_bytes()
        source_payload = self.chunks[1][1]
        self.record = {
            "schema_version": 1, "kind": KIND,
            "id": "epoch-history-commit-00000000000070008000000000000001",
            "tenant_id": self.root.tenant_id, "timestamp": self.receipts[0]["timestamp"],
            "actor": "actor", "trigger": "ack", "verifier_version": 1,
            "policy": {"schema_version": 1, "keep_forever": False},
            "live_anchor": {"byte_length": len(events), "sha256": hashlib.sha256(events).hexdigest()},
            "previous_generation": None,
            "source_commitment": {"chunk_count": 1,
                "sha256": hashlib.sha256(b"floati-epoch-history-sources-v1\0" + source_payload).hexdigest()},
            "chunks": [entry for _path, _payload, entry in self.chunks],
        }
        self.record["generation"] = generation_digest(self.record)
        self.ledger = self.root.path / "receipts/epoch-history-commits.jsonl"

    def install_fixture_commit(self, row=None):
        # Deliberate scratch native-record fixture, not a production issuer seam.
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_bytes(encode_frame(self.record if row is None else row))

    def reader(self):
        spec = importlib.util.find_spec("floati.archive_catalog")
        self.assertIsNotNone(spec, "native committed-history reader is absent")
        from floati.archive_catalog import read_committed_generation
        return read_committed_generation(self.root)

    def test_new_native_commit_family_accepts_exact_closed_fixture(self):
        try:
            actual = validate_record(self.record, self.root.tenant_id, {KIND}, integrity=False)
        except ProtocolRefusal as failure:
            self.fail("native history commit was refused: " + failure.code)
        self.assertEqual(actual, self.record)

    def test_committed_chunks_load_cold_and_return_exact_bytes(self):
        self.install_fixture_commit()
        generation = self.reader()
        self.assertEqual(generation.commit, self.record)
        for _path, payload, row in self.chunks:
            self.assertEqual(generation.read_chunk(row["domain"], row["ordinal"]), payload)
        self.assertIsNone(generation.read_chunk("survivor", 0))

    def test_a_pointer_without_native_commit_has_no_authority(self):
        (self.root.path / "state/epoch-history/v1/current.json").write_bytes(b"forged pointer")
        self.assertIsNone(self.reader())

    def test_missing_or_changed_chunk_refuses_instead_of_recovering_from_a_pointer(self):
        self.install_fixture_commit()
        path, raw, _row = self.chunks[0]
        for payload in (None, raw + b" "):
            with self.subTest(payload=payload):
                if payload is None:
                    path.unlink()
                else:
                    path.write_bytes(payload)
                with self.assertRaises(IntegrityFailure):
                    self.reader()

    def test_chunk_replacement_invalidates_old_handle_even_with_identical_bytes(self):
        self.install_fixture_commit()
        generation = self.reader()
        path, raw, row = self.chunks[0]
        replacement = self.base / "replacement-chunk"
        replacement.write_bytes(raw)
        replacement.replace(path)
        with self.assertRaises(IntegrityFailure):
            generation.read_chunk(row["domain"], row["ordinal"])
        self.assertEqual(self.reader().read_chunk(row["domain"], 0), raw)

    def test_generation_and_predecessor_are_complete_native_commitments(self):
        for field, value in (("generation", "a" * 64), ("previous_generation", "b" * 64)):
            row = deepcopy(self.record)
            row[field] = value
            if field != "generation":
                row["generation"] = generation_digest(row)
            self.install_fixture_commit(row)
            with self.subTest(field=field), self.assertRaises(IntegrityFailure):
                self.reader()

    def test_generic_and_lower_ledger_writers_cannot_issue_history_authority(self):
        before = self.ledger.exists()
        writers = (
            lambda: jsonl.append_record(self.root, "receipts/epoch-history-commits.jsonl",
                                        self.record, allowed_kinds={KIND}),
            lambda: jsonl.transact(self.root, "receipts/epoch-history-commits.jsonl",
                                   lambda _rows: (None, self.record), allowed_kinds={KIND}),
            lambda: jsonl._append_frame(self.ledger, encode_frame(self.record)),
        )
        for writer in writers:
            with self.subTest(writer=writer), self.assertRaises(ProtocolRefusal) as caught:
                writer()
            self.assertEqual(caught.exception.code, "epoch_history_controller_only")
        self.assertEqual(self.ledger.exists(), before)

    def test_native_manual_roll_preserves_the_unselected_commit_ledger(self):
        self.install_fixture_commit()
        before = self.ledger.read_bytes()
        bus_epoch.roll_bus_epoch(self.root, actor="actor", idempotency_key="after-history-commit")
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.reader().commit, self.record)

    def test_read_bound_refuses_without_changing_any_history(self):
        self.install_fixture_commit()
        before = self.ledger.read_bytes()
        spec = importlib.util.find_spec("floati.archive_catalog")
        self.assertIsNotNone(spec, "native committed-history reader is absent")
        from floati import archive_catalog
        with mock.patch.object(archive_catalog, "MAX_CHUNK_BYTES", 1):
            with self.assertRaises(IntegrityFailure):
                self.reader()
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_new_schema_and_runtime_refuse_open_or_unbounded_commit_shapes(self):
        schema = Path(__file__).resolve().parents[1] / "schemas/v1/epoch-history-commit.schema.json"
        validate_json_schema(self.record, schema)
        cases = []
        for field, value in (("schema_version", 0), ("verifier_version", True),
                             ("trigger", []), ("trigger", "manual"),
                             ("previous_generation", "../foreign"),
                             ("generation", "A" * 64), ("chunks", [])):
            row = deepcopy(self.record)
            row[field] = value
            cases.append(row)
        for field in ("policy", "live_anchor", "source_commitment"):
            row = deepcopy(self.record)
            row[field]["accepted_without_native_check"] = True
            cases.append(row)
        for field, value in (("ordinal", True), ("ordinal", -1),
                             ("domain", "../sources"), ("byte_length", 8388609),
                             ("sha256", "short")):
            row = deepcopy(self.record)
            row["chunks"][0][field] = value
            cases.append(row)
        for row in cases:
            with self.subTest(row=row):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(row, self.root.tenant_id, {KIND}, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(row, schema)

    def test_rehashed_but_incomplete_or_conflicting_chunk_collections_refuse(self):
        for change in ("gap", "duplicate", "reordered", "missing-source", "source-count"):
            row = deepcopy(self.record)
            if change == "gap":
                row["chunks"][0]["ordinal"] = 1
            elif change == "duplicate":
                row["chunks"].insert(1, deepcopy(row["chunks"][0]))
            elif change == "reordered":
                row["chunks"].reverse()
            elif change == "missing-source":
                row["chunks"][1]["domain"] = "survivor"
            else:
                row["source_commitment"]["chunk_count"] = 2
            row["generation"] = generation_digest(row)
            with self.subTest(change=change), self.assertRaises(ProtocolRefusal):
                validate_record(row, self.root.tenant_id, {KIND}, integrity=False)

    def test_commit_chain_extends_but_old_handles_cannot_rebase_their_witness(self):
        self.install_fixture_commit()
        old = self.reader()
        later = deepcopy(self.record)
        later["id"] = later["id"][:-1] + "2"
        later["previous_generation"] = self.record["generation"]
        later["generation"] = generation_digest(later)
        self.ledger.write_bytes(encode_frame(self.record) + encode_frame(later))
        with self.assertRaises(IntegrityFailure):
            old.commit
        self.assertEqual(self.reader().commit, later)

    def test_chunk_alias_and_hardlink_never_borrow_verified_generation(self):
        self.install_fixture_commit()
        path, raw, _row = self.chunks[0]
        foreign = self.base / "foreign-chunk"
        foreign.write_bytes(raw)
        path.unlink()
        path.symlink_to(foreign)
        with self.assertRaises(IntegrityFailure):
            self.reader()
        self.assertEqual(foreign.read_bytes(), raw)
        path.unlink()
        os.link(foreign, path)
        with self.assertRaises(IntegrityFailure):
            self.reader()
        self.assertEqual(foreign.read_bytes(), raw)

    def test_changed_source_collection_digest_refuses_even_with_rehashed_generation(self):
        row = deepcopy(self.record)
        row["source_commitment"]["sha256"] = "f" * 64
        row["generation"] = generation_digest(row)
        self.install_fixture_commit(row)
        with self.assertRaises(IntegrityFailure):
            self.reader()

    def test_native_automatic_roll_preserves_unselected_commit_authority(self):
        self.install_fixture_commit()
        before = self.ledger.read_bytes()
        EventLog(self.root).send("actor", "recipient", "floati", "a" * 40,
                                "AGENTS.md", "automatic fixture", idempotency_key="auto-after-commit")
        def planner(root, payloads):
            return payloads, {"trigger": "ack", "policy": {"max_bytes": 1,
                "max_age_days": 7, "source": "operator_declared"},
                "observed_bytes": next(len(raw) for name, _plane, raw in payloads if name == "events.jsonl"),
                "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "epoch_started_at": None, "due_reasons": ["bytes"]}
        bus_epoch.roll_automatic_epoch(self.root, actor="actor", planner=planner)
        from floati.framing import decode_frames
        self.assertIn("automatic", decode_frames((self.root.path / "events.jsonl").read_bytes())[0])
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.reader().commit, self.record)

    def test_lower_writer_refuses_a_history_ledger_alias_before_touching_target(self):
        foreign = self.base / "foreign-ledger.jsonl"
        foreign.write_bytes(b"foreign sentinel\n")
        self.ledger.symlink_to(foreign)
        with self.assertRaises(ProtocolRefusal) as caught:
            jsonl._append_frame(self.ledger, encode_frame(self.record))
        self.assertEqual(caught.exception.code, "epoch_history_controller_only")
        self.assertEqual(foreign.read_bytes(), b"foreign sentinel\n")

    def test_predecessor_only_chunk_change_invalidates_current_generation(self):
        later = deepcopy(self.record)
        later["id"] = later["id"][:-1] + "2"
        later["previous_generation"] = self.record["generation"]
        changed = self.chunks[0][1].replace(b'"history-1"', b'"history-2"')
        digest = hashlib.sha256(changed).hexdigest()
        path = self.root.path / "state/epoch-history/v1/chunks" / (digest + ".jsonl")
        path.write_bytes(changed)
        later["chunks"][0] = dict(later["chunks"][0], sha256=digest, byte_length=len(changed))
        later["generation"] = generation_digest(later)
        self.ledger.write_bytes(encode_frame(self.record) + encode_frame(later))
        generation = self.reader()
        prior_path, prior_bytes, _descriptor = self.chunks[0]
        replacement = self.base / "prior-replacement"
        replacement.write_bytes(prior_bytes)
        replacement.replace(prior_path)
        with self.assertRaises(IntegrityFailure):
            generation.commit

    def test_hardlink_alias_cannot_append_native_history_authority(self):
        later = deepcopy(self.record)
        later["id"] = later["id"][:-1] + "2"
        later["previous_generation"] = self.record["generation"]
        later["generation"] = generation_digest(later)
        alias = self.ledger.parent / "alias.jsonl"
        writers = (
            lambda: jsonl.append_record(self.root, "receipts/alias.jsonl", later, allowed_kinds={KIND}),
            lambda: jsonl._append_frame(alias, encode_frame(later)),
        )
        for writer in writers:
            self.install_fixture_commit()
            before = self.ledger.read_bytes()
            os.link(self.ledger, alias)
            with self.subTest(writer=writer):
                try:
                    with self.assertRaises(ProtocolRefusal) as caught:
                        writer()
                    self.assertEqual(caught.exception.code, "epoch_history_controller_only")
                finally:
                    alias.unlink()
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_schema_and_runtime_share_the_exact_live_anchor_integer_boundary(self):
        schema = Path(__file__).resolve().parents[1] / "schemas/v1/epoch-history-commit.schema.json"
        for size in (2**63 - 1, 2**63):
            row = deepcopy(self.record)
            row["live_anchor"]["byte_length"] = size
            row["generation"] = generation_digest(row)
            if size == 2**63 - 1:
                validate_json_schema(row, schema)
                validate_record(row, self.root.tenant_id, {KIND}, integrity=False)
            else:
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(row, schema)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(row, self.root.tenant_id, {KIND}, integrity=False)

    def test_schema_never_admits_a_final_newline_in_native_identity_fields(self):
        schema = Path(__file__).resolve().parents[1] / "schemas/v1/epoch-history-commit.schema.json"
        for field in ("actor", "tenant_id", "id", "generation"):
            row = deepcopy(self.record)
            row[field] += "\n"
            if field != "generation":
                row["generation"] = generation_digest(row)
            with self.subTest(field=field):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(row, self.root.tenant_id, {KIND}, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(row, schema)

    def test_schema_integer_semantics_match_runtime_for_integral_json_numbers(self):
        schema = Path(__file__).resolve().parents[1] / "schemas/v1/epoch-history-commit.schema.json"
        for field in ("verifier_version", "policy-version", "anchor-length", "chunk-ordinal", "chunk-length", "source-count"):
            row = deepcopy(self.record)
            if field == "policy-version":
                row["policy"]["schema_version"] = 1.0
            elif field == "anchor-length":
                row["live_anchor"]["byte_length"] = float(row["live_anchor"]["byte_length"])
            elif field == "chunk-ordinal":
                row["chunks"][0]["ordinal"] = 0.0
            elif field == "chunk-length":
                row["chunks"][0]["byte_length"] = float(row["chunks"][0]["byte_length"])
            elif field == "source-count":
                row["source_commitment"]["chunk_count"] = 1.0
            else:
                row[field] = 1.0
            row["generation"] = generation_digest(row)
            with self.subTest(field=field):
                validate_json_schema(row, schema)
                try:
                    actual = validate_record(row, self.root.tenant_id, {KIND}, integrity=False)
                except ProtocolRefusal as failure:
                    self.fail("schema accepted an integral JSON number that runtime refused: " + failure.code)
                self.assertEqual(actual, row)

    def test_integral_float_chunk_lengths_read_without_rewriting_native_wire(self):
        schema = Path(__file__).resolve().parents[1] / "schemas/v1/epoch-history-commit.schema.json"
        row = deepcopy(self.record)
        for descriptor in row["chunks"]:
            descriptor["byte_length"] = float(descriptor["byte_length"])
        row["generation"] = generation_digest(row)
        validate_json_schema(row, schema)
        self.assertEqual(row, validate_record(row, self.root.tenant_id, {KIND}, integrity=False))
        self.install_fixture_commit(row)
        wire = self.ledger.read_bytes()
        self.assertIn(('"byte_length":' + str(row["chunks"][0]["byte_length"])).encode(), wire)
        try:
            generation = self.reader()
            self.assertEqual(encode_frame(row), encode_frame(generation.commit))
            for _path, payload, descriptor in self.chunks:
                self.assertEqual(payload, generation.read_chunk(descriptor["domain"], descriptor["ordinal"]))
        except IntegrityFailure as failure:
            self.fail("schema/native accepted chunk length was refused by byte reader: " + failure.code)
        self.assertEqual(wire, self.ledger.read_bytes())


if __name__ == "__main__":
    unittest.main()
