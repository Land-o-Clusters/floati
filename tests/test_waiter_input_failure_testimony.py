"""Private pre-read failure testimony; public JSONL contract stays unchanged.

The public baseline serializes every return value or exception class/code/
detail/string/cause, parameterizing only this fixture's scratch ledger path.
Race injections delegate the original decoder with the exact original bytes,
then arrange a lawful replacement before its native failure reaches a caller.
They do not replace evaluation, authority, validation, clocks or content.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from floati import jsonl
from floati.errors import DurabilityFailure, IntegrityFailure
from floati.framing import encode_frame
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


KINDS = {"registry_entry"}
DOMAIN = "waiter-input-failure-testimony-v1"
# Captured against jsonl.py SHA256
# 0063c890718b3cbd246cf28713ea48cb35fb2e6e83adef98d6e5bcebe2a333ff.
PUBLIC_BASELINE_SHA256 = "cb71a622d431614faef530059778b638e32586a409fcb1f9ca24040eb67a719f"


def record(second=False):
    return {
        "schema_version": 0,
        "id": "registry-018f7e9b3c127abc8def0123456789ab" if second else
              "registry-018f7e9b3c117abc8def0123456789ab",
        "tenant_id": "alpha", "timestamp": "2026-07-31T12:00:00.000Z",
        "kind": "registry_entry", "node_id": "r-2" if second else "r-1",
        "role": "worker", "state": "active",
    }


def measured(path):
    status = path.stat()
    return (status.st_dev, status.st_ino, status.st_mode, status.st_size,
            status.st_mtime_ns, status.st_ctime_ns, status.st_nlink)


def samples(second=False):
    valid = record(second)
    foreign = dict(valid, tenant_id="foreign")
    return {
        "empty": b"", "valid": encode_frame(valid),
        "incomplete": b"{", "blank": b"\n", "invalid_utf8": b"\xff\n",
        "malformed_json": b"{]\n", "invalid_record": encode_frame({}),
        "foreign_tenant": encode_frame(foreign),
        "duplicate_id": encode_frame(record()),
        "noncanonical": (json.dumps(valid, sort_keys=True) + "\n").encode(),
    }


class WaiterInputFailureTestimonyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open(Path(self.temporary.name), "alpha")
        self.path = self.root.resolve_relative("input.jsonl")

    def read(self, mode, cursor=None):
        if mode == "snapshot":
            return jsonl.read_records_snapshot(self.root, "input.jsonl", allowed_kinds=KINDS)
        return jsonl.read_records_with_prefix_digests(
            self.root, "input.jsonl", allowed_kinds=KINDS, domain=DOMAIN,
            cursor=cursor,
        )

    def ready(self, mode, payload):
        if mode != "incremental":
            self.path.write_bytes(payload)
            return None
        self.path.write_bytes(encode_frame(record()))
        cursor = jsonl.VerifiedLedgerCursor()
        self.read(mode, cursor)
        with self.path.open("ab") as stream:
            stream.write(payload)
        return cursor

    def public_response(self, mode, cursor):
        try:
            result = self.read(mode, cursor)
        except Exception as failure:
            chain = []
            seen = set()
            while failure is not None and id(failure) not in seen:
                seen.add(id(failure))
                chain.append({
                    "class": type(failure).__module__ + "." + type(failure).__name__,
                    "code": getattr(failure, "code", None),
                    "detail": getattr(failure, "detail", None), "str": str(failure),
                    "cause_explicit": failure.__cause__ is not None,
                    "suppress_context": failure.__suppress_context__,
                })
                failure = failure.__cause__ or failure.__context__
            return {"exception_chain": chain}
        return {"result": result}

    def public_transcript(self, *, save_inputs=None):
        transcript = {}
        for mode in ("snapshot", "full", "incremental"):
            for name, payload in samples(second=mode == "incremental").items():
                cursor = self.ready(mode, payload)
                if save_inputs is not None:
                    save_inputs[mode + "-" + name] = self.path.read_bytes()
                transcript[mode + "/" + name] = self.public_response(mode, cursor)
        return json.dumps(transcript, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode()

    def public_baseline(self):
        # A complete owned-path template: no public field is dropped, and no
        # arbitrary path, diagnostic or payload is generalized.
        return self.public_transcript().replace(str(self.path).encode(), b"$INPUT")

    def test_public_results_are_byte_identical_to_prechange_baseline(self):
        transcript = self.public_baseline()
        digest = hashlib.sha256(transcript).hexdigest()
        self.assertEqual(PUBLIC_BASELINE_SHA256, digest,
                         "complete public transcript baseline SHA256=" + digest)

    def assert_testimony(self, failure, before):
        self.assertEqual((self.path, before), getattr(failure, "waiter_input_witness", None),
                         "native failure must retain measured input metadata from BEFORE read")

    def test_snapshot_native_failures_keep_measured_private_witness(self):
        for name in ("incomplete", "blank", "invalid_utf8", "malformed_json",
                     "invalid_record", "foreign_tenant"):
            with self.subTest(case=name):
                self.path.write_bytes(samples()[name])
                before = measured(self.path)
                with self.assertRaises(IntegrityFailure) as observed:
                    self.read("snapshot")
                self.assert_testimony(observed.exception, before)

    def test_full_native_failures_keep_measured_private_witness(self):
        for name in ("incomplete", "blank", "invalid_utf8", "malformed_json",
                     "invalid_record", "foreign_tenant", "noncanonical"):
            with self.subTest(case=name):
                self.path.write_bytes(samples()[name])
                before = measured(self.path)
                with self.assertRaises(IntegrityFailure) as observed:
                    self.read("full")
                self.assert_testimony(observed.exception, before)

    def test_incremental_native_failures_keep_measured_private_witness(self):
        for name in ("incomplete", "blank", "invalid_utf8", "malformed_json",
                     "invalid_record", "foreign_tenant", "duplicate_id", "noncanonical"):
            with self.subTest(case=name):
                cursor = self.ready("incremental", samples(second=True)[name])
                before = measured(self.path)
                with self.assertRaises(IntegrityFailure) as observed:
                    self.read("incremental", cursor)
                self.assert_testimony(observed.exception, before)

    def race(self, mode):
        cursor = self.ready(mode, b"{")
        before = measured(self.path)
        healthy = encode_frame(record())
        if mode == "incremental":
            healthy += encode_frame(record(True))
        original_decode = (jsonl.decode_frames if mode == "incremental" else
                           jsonl._decode_path_records)
        replacement = self.path.with_suffix(".replacement")
        replacement.write_bytes(healthy)
        injected = []

        def repair_then_decode(*args, **kwargs):
            if not injected:
                os.replace(replacement, self.path)
                injected.append(True)
            return original_decode(*args, **kwargs)

        target = ("floati.jsonl.decode_frames" if mode == "incremental" else
                  "floati.jsonl._decode_path_records")
        with mock.patch(target, side_effect=repair_then_decode):
            with self.assertRaises(IntegrityFailure) as observed:
                self.read(mode, cursor)
        self.assertTrue(injected, "must exercise a real post-read/pre-failure repair")
        self.assertEqual("incomplete_jsonl_line", observed.exception.code)
        self.assertNotEqual(before, measured(self.path), "owned replacement must change identity")
        self.assert_testimony(observed.exception, before)
        rows, _prefixes = self.read("full")
        self.assertEqual([record(), record(True)] if mode == "incremental" else [record()], rows)

    def test_snapshot_repair_before_failure_retains_original_read_identity(self):
        self.race("snapshot")

    def test_full_repair_before_failure_retains_original_read_identity(self):
        self.race("full")

    def test_incremental_repair_before_failure_retains_original_read_identity(self):
        self.race("incremental")

    def test_snapshot_open_failure_does_not_manufacture_measured_testimony(self):
        self.path.write_bytes(b"{")
        original_open = Path.open

        def denied(path, *args, **kwargs):
            if path == self.path:
                raise PermissionError(13, "owned fixture denied", str(path))
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", denied):
            with self.assertRaises(DurabilityFailure) as observed:
                self.read("snapshot")
        self.assertFalse(hasattr(observed.exception, "waiter_input_witness"))


def equivalence_bank(existing=None):
    """Explicit diagnostic mode; preserve same-path raw public bytes privately.

    This is JSONL return/error equivalence, not CLI stdout or performance
    evidence. The durable unittest path-template fingerprint is separate.
    Every case resets this exact owned fixture path to the same banked bytes.
    """
    prefix = "floati-integrity-equivalence-20261011-"
    if existing is None:
        bank = Path(tempfile.mkdtemp(prefix=prefix, dir=REAL_TEMP_ROOT))
    else:
        bank = Path(existing)
        if (not bank.is_absolute() or bank.parent != Path(REAL_TEMP_ROOT)
                or not bank.name.startswith(prefix) or bank.is_symlink()
                or bank.stat().st_uid != os.getuid() or not bank.is_dir()):
            raise ValueError("equivalence bank must be an owned ordinary bounded temporary directory")
    fixture = bank / "fixture"
    if fixture.is_symlink():
        raise ValueError("equivalence fixture must be ordinary")
    case = WaiterInputFailureTestimonyTests()
    case.root = FloatiRoot.open(fixture, "alpha")
    case.path = case.root.resolve_relative("input.jsonl")
    source_sha = hashlib.sha256(Path(jsonl.__file__).read_bytes()).hexdigest()
    inputs = {}
    raw = case.public_transcript(save_inputs=inputs)
    raw_sha = hashlib.sha256(raw).hexdigest()
    normalized_sha = hashlib.sha256(raw.replace(str(case.path).encode(), b"$INPUT")).hexdigest()
    if normalized_sha != PUBLIC_BASELINE_SHA256:
        # Retain a failed public-equivalence transcript as exact private data.
        (bank / ("unexpected-" + source_sha + ".json")).write_bytes(raw)
        raise AssertionError("public path-template fingerprint changed")
    if existing is None:
        (bank / "inputs").mkdir(mode=0o700)
        for name, payload in inputs.items():
            (bank / "inputs" / (name + ".bin")).write_bytes(payload)
        (bank / "public.before.json").write_bytes(raw)
        receipt = {
            "kind": "waiter_input_failure_public_equivalence_baseline",
            "bank": str(bank), "fixture_input": str(case.path), "cases": len(inputs),
            "jsonl_source_sha256": source_sha, "raw_public_sha256": raw_sha,
            "path_template_sha256": normalized_sha,
            "normalization_in_raw_public_proof": False,
            "inputs": {name: hashlib.sha256(payload).hexdigest()
                       for name, payload in sorted(inputs.items())},
        }
        (bank / "receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
        return {"status": "baseline_banked", "receipt": str(bank / "receipt.json"),
                "cases": len(inputs), "raw_public_sha256": raw_sha,
                "path_template_sha256": normalized_sha, "jsonl_source_sha256": source_sha}
    receipt = json.loads((bank / "receipt.json").read_text())
    if receipt["bank"] != str(bank) or receipt["fixture_input"] != str(case.path):
        raise AssertionError("baseline fixture path binding changed")
    for name, payload in inputs.items():
        expected = (bank / "inputs" / (name + ".bin")).read_bytes()
        if expected != payload or hashlib.sha256(expected).hexdigest() != receipt["inputs"][name]:
            raise AssertionError("exact baseline fixture input changed")
    before = (bank / "public.before.json").read_bytes()
    if hashlib.sha256(before).hexdigest() != receipt["raw_public_sha256"]:
        raise AssertionError("baseline raw transcript changed")
    with (bank / ("public.after-" + source_sha + ".json")).open("xb") as stream:
        stream.write(raw)
    result = {"status": "byte_identical" if raw == before else "different",
              "bank": str(bank), "cases": len(inputs), "same_fixture_path": True,
              "exact_input_bytes_reset": True, "raw_public_sha256": raw_sha,
              "baseline_jsonl_source_sha256": receipt["jsonl_source_sha256"],
              "jsonl_source_sha256": source_sha, "normalization": False}
    (bank / ("comparison-" + source_sha + ".json")).write_text(json.dumps(result, sort_keys=True) + "\n")
    if raw != before:
        raise AssertionError("same-path raw public transcript differs")
    return result


if __name__ == "__main__":
    if sys.argv[1:] == ["--bank-baseline"]:
        print(json.dumps(equivalence_bank(), sort_keys=True))
    elif len(sys.argv) == 3 and sys.argv[1] == "--compare-bank":
        print(json.dumps(equivalence_bank(sys.argv[2]), sort_keys=True))
    else:
        unittest.main()
