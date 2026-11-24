"""Draft RED controls: send/exhaustion semantic equality must retain testimony.

Proposed semantic delta: exact changed-path mapping; append entries contain
operation='append' and decoded ordered `frames`; create_lock retains its
operation. Raw hashes/lengths/base64 remain in the producer's original receipt,
not the cross-invocation semantic projection. No timing policy is specified here.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

from floati.codex_wait_contract import CodexWaitReceiptLedger
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT

ROOT = Path(__file__).resolve().parents[1]  # Parent adopts under tests/ before RED.
EVENTS = "/delta/events.jsonl/frames/"
EXHAUSTION = "/delta/receipts~1codex-wait-exhaustion~1scale-worker.jsonl/frames/0/"


def pointer(value, coordinate, replacement=None, *, replace=False):
    parts = [part.replace("~1", "/").replace("~0", "~") for part in coordinate.split("/")[1:]]
    for part in parts[:-1]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    key = int(parts[-1]) if isinstance(value, list) else parts[-1]
    original = value[key]  # Every audit pointer must exist; removal is not normalization.
    if replace:
        value[key] = replacement
    return original


class BusScaleDurableDeltaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-durable-controls-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home, self.workspace = self.base / "scale-fleet", self.base / "workspace"
        self.home.mkdir(mode=0o700)
        from tests.bus_scale_test_profile import banked_send_repository
        repository, self.sha = banked_send_repository(self.base)
        repository.rename(self.workspace)
        self.workspace.chmod(0o700)
        self.root = FloatiRoot.open_direct_home(self.home)
        for node in ("architect", "scale-worker"):
            Registry(self.root).register(node, "worker")
        spec = importlib.util.spec_from_file_location("durable_control_native_producer",
            ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py")
        self.producer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.producer)
        try:
            subject = importlib.import_module("tests.bus_scale_effects")
        except ModuleNotFoundError as error:
            if error.name != "tests.bus_scale_effects":
                raise
            subject = None
        self.normalize = getattr(subject, "normalize_durable_delta", None)
        self.assertTrue(callable(self.normalize), "missing tests.bus_scale_effects.normalize_durable_delta")

    def native(self, case):
        if case == "send":
            from floati.send_prefix import warm
            self.assertIsNotNone(warm(self.root))
            from floati.cut_prefix import warm as warm_cut
            self.assertIsNotNone(warm_cut(self.root))
        self.before = self.producer.raw_files(self.home)
        receipt_input = None
        prefix_input = None
        cut_input = None
        if case == "send":
            from tests.fixtures.bus_scale_census.bus_scale_prefix_effects import capture_prefix_input
            prefix_input = capture_prefix_input(self.home, self.before)
            from tests.fixtures.bus_scale_census.bus_scale_cut_prefix_effects import capture_cut_prefix_input
            cut_input = capture_cut_prefix_input(self.home, self.before)
        if case == "receipt-append":
            from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import capture_receipt_prefix_input
            receipt_input = capture_receipt_prefix_input(self.home, self.before)
        now = datetime.now(timezone.utc)
        begin = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        if case == "send":
            note = "Keep msg-" + "f" * 32 + " timestamp=2000-01-01T00:00:00.000Z home=" + str(self.home)
            evidence = EventLog(self.root).send("scale-worker", "architect", "floati", self.sha,
                "note.md", note, idempotency_key="census-send")
            artifact = dict(artifact_version=0, command="send", status="ok", evidence=dict(evidence))
        else:
            artifact = CodexWaitReceiptLedger(self.root).record_exhaustion(node_id="scale-worker",
                session_digest=hashlib.sha256(b"bus-scale-native-census").hexdigest(),
                waited_seconds=2, idempotency_key="census-receipt")
        now = datetime.now(timezone.utc)
        end = now.replace(microsecond=(now.microsecond // 1000) * 1000) + timedelta(milliseconds=1)
        delta = self.producer.native_delta(case, self.before, self.producer.raw_files(self.home),
            home=self.home, receipt_prefix_input=receipt_input, prefix_input=prefix_input,
            cut_input=cut_input)
        self.context = dict(wall_begin=begin, wall_end=end, child_pid=os.getpid(), home=self.home, workspace=self.workspace)
        if receipt_input is not None:
            self.context.update(receipt_prefix_input=receipt_input, before_raw_files=self.before)
        if prefix_input is not None:
            self.context.update(prefix_input=prefix_input, before_raw_files=self.before)
        if cut_input is not None:
            self.context["cut_input"] = cut_input
        return delta, artifact

    def frames(self, delta, path="events.jsonl"):
        return decode_frames(base64.b64decode(delta[path]["payload_base64"], validate=True))

    def rewrite(self, delta, path, frames):
        payload = b"".join(encode_frame(row) for row in frames)
        row = delta[path]
        row.update(payload_base64=base64.b64encode(payload).decode(),
            after_bytes=row["before_bytes"] + len(payload),
            after_sha256=hashlib.sha256(self.before.get(path, b"") + payload).hexdigest())

    def complete_projection(self, delta, artifact, result, allowed):
        self.assertEqual({"artifact", "delta", "normalizations"}, set(result))
        cache_semantic = {}
        if artifact.get("command") == "send":
            from tests.fixtures.bus_scale_census.bus_scale_wake_hint_effects import verify_wake_hint_effects
            from floati.events import EVENT_KINDS
            hints = verify_wake_hint_effects(self.root, self.before,
                self.producer.raw_files(self.home), delta, ledger_kinds={"events.jsonl": EVENT_KINDS})
            delta = {path: row for path, row in delta.items() if path not in hints}
            cache_semantic.update(hints)
        prefix_input = self.context.get("prefix_input")
        if prefix_input is not None:
            from tests.fixtures.bus_scale_census.bus_scale_prefix_effects import verify_prefix_effect, SUMMARY as SEND_SUMMARY
            proof = verify_prefix_effect(self.home, self.before,
                self.producer.raw_files(self.home), delta, prefix_input)
            delta = {path: row for path, row in delta.items() if path not in proof["changed_files"]}
            cache_semantic[SEND_SUMMARY] = proof["semantic"]
        receipt_input = self.context.get("receipt_prefix_input")
        cut_input = self.context.get("cut_input")
        if cut_input is not None:
            from tests.fixtures.bus_scale_census.bus_scale_cut_prefix_effects import verify_cut_prefix_effect, SUMMARY
            proof = verify_cut_prefix_effect(self.home, self.before,
                self.producer.raw_files(self.home), delta, cut_input)
            delta = {path: row for path, row in delta.items() if path not in proof["changed_files"]}
            cache_semantic[SUMMARY] = proof["semantic"]
        if receipt_input is not None:
            from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import verify_receipt_prefix_effect, SUMMARY
            proof = verify_receipt_prefix_effect(self.home, self.before,
                self.producer.raw_files(self.home), delta, receipt_input)
            # Every coalesced byte is independently proved; the original raw
            # delta remains exact and the semantic projection keeps its fact.
            delta = {path: row for path, row in delta.items() if path not in proof["changed_files"]}
            cache_semantic[SUMMARY] = proof["semantic"]
        self.assertEqual(set(delta) | set(cache_semantic), set(result["delta"]))
        original = {"artifact": copy.deepcopy(artifact), "delta": {
            path: {"operation": row["operation"], **({"frames": self.frames(delta, path)}
                if row["operation"] == "append" else {})} for path, row in delta.items()}}
        original["delta"].update(cache_semantic)
        actual = {"artifact": copy.deepcopy(result["artifact"]), "delta": copy.deepcopy(result["delta"])}
        audits = result["normalizations"]
        self.assertTrue(audits)
        self.assertEqual(len(audits), len({row["pointer"] for row in audits}))
        self.assertEqual(allowed, {row["pointer"] for row in audits})
        for row in audits:
            self.assertEqual({"pointer", "reason"}, set(row))
            self.assertIsInstance(row["reason"], str)
            self.assertTrue(row["reason"].strip())
            source = pointer(original, row["pointer"])
            self.assertNotEqual(source, pointer(actual, row["pointer"]))
            pointer(actual, row["pointer"], source, replace=True)
        # Restoring ONLY the explicit allowed volatile/coordinate pointers must
        # recover the complete original semantic object, including unknown fields.
        self.assertEqual(original, actual)

    def send_allowed(self, artifact):
        allowed = {"/artifact/evidence/message/id", "/artifact/evidence/message/timestamp",
            "/artifact/evidence/recipient_readiness/observed_at",
            EVENTS + "0/id", EVENTS + "0/timestamp", EVENTS + "1/id", EVENTS + "1/envelope_id",
            EVENTS + "1/timestamp", EVENTS + "1/observed_at", EVENTS + "1/readiness/observed_at"}
        for index, fact in enumerate(artifact["evidence"]["recipient_readiness"]["mechanism_evidence"]):
            for source_index, source in enumerate(fact["sources"]):
                self.assertTrue(source["path"].startswith(str(self.home) + "/"))
                tail = "mechanism_evidence/%d/sources/%d/path" % (index, source_index)
                allowed.update({"/artifact/evidence/recipient_readiness/" + tail, EVENTS + "1/readiness/" + tail})
        return allowed

    def test_native_send_retains_complete_ordered_envelope_and_readiness_without_mutating_input(self):
        delta, artifact = self.native("send")
        saved = copy.deepcopy((delta, artifact))
        result = self.normalize("send", delta, artifact, **self.context)
        self.complete_projection(delta, artifact, result, self.send_allowed(artifact))
        frames = result["delta"]["events.jsonl"]["frames"]
        self.assertEqual(["message_envelope", "send_readiness_observation"], [row["kind"] for row in frames])
        self.assertEqual(frames[0], result["artifact"]["evidence"]["message"])
        self.assertEqual(frames[0]["id"], frames[1]["envelope_id"])
        self.assertEqual(frames[1]["readiness"], result["artifact"]["evidence"]["recipient_readiness"])
        self.assertEqual("recipient_not_listening", frames[1]["readiness"]["state"])
        self.assertEqual("all_mechanisms_absent", frames[1]["readiness"]["reason"])
        self.assertEqual(["lease", "stop_hook", "seat_board"], frames[1]["readiness"]["checked_mechanisms"])
        self.assertEqual(saved, (delta, artifact))

    def test_native_codex_send_preserves_unknown_hook_readiness(self):
        # Census seeds register Codex nodes; an absent hook provenance source
        # is unknown, unlike the generic-worker fixture above.
        self.home = self.base / "codex-fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        for node in ("architect", "scale-worker"):
            Registry(self.root).register(node, "codex")
        delta, artifact = self.native("send")
        original = copy.deepcopy((delta, artifact))
        readiness = artifact["evidence"]["recipient_readiness"]
        self.assertEqual("unknown", readiness["state"])
        self.assertEqual("mechanism_evidence_incomplete", readiness["reason"])
        result = self.normalize("send", delta, artifact, **self.context)
        self.complete_projection(delta, artifact, result, self.send_allowed(artifact))
        self.assertEqual("unknown", result["artifact"]["evidence"]["recipient_readiness"]["state"])
        self.assertEqual(original, (delta, artifact))

    def test_native_send_cut_projection_refuses_unexplained_or_omitted_cut_paths(self):
        from tests.fixtures.bus_scale_census.bus_scale_cut_prefix_effects import CACHE, SUMMARY, raw_candidate_delta
        delta, artifact = self.native("send")
        normalized = self.normalize("send", delta, artifact, **self.context)
        self.assertEqual("verified_cut_prefix_append", normalized["delta"][SUMMARY]["operation"])
        cut_paths = {path for path in delta if path.startswith(CACHE + "/")}
        self.assertTrue(cut_paths, "the retained raw delta must include the native cut append")
        for defect in ("omitted", "extra", "lookalike"):
            with self.subTest(defect=defect):
                forged = copy.deepcopy(delta)
                extra = None
                if defect == "omitted":
                    del forged[next(iter(cut_paths))]
                else:
                    relative = (CACHE + "/nodes/" + "f" * 64
                        if defect == "extra" else ".cut-prefix-evil")
                    extra = self.home / relative
                    extra.write_bytes(b"unexplained cut payload")
                    extra.chmod(0o600)
                    forged = raw_candidate_delta(self.before, self.producer.raw_files(self.home))
                try:
                    with self.assertRaises(ValueError):
                        self.normalize("send", forged, artifact, **self.context)
                    if defect != "omitted":
                        with self.assertRaises((ValueError, RuntimeError)):
                            self.producer.native_delta("send", self.before,
                                self.producer.raw_files(self.home), home=self.home,
                                prefix_input=self.context["prefix_input"], cut_input=self.context["cut_input"])
                finally:
                    if extra is not None:
                        extra.unlink()

    def test_native_exhaustion_retains_full_session_waited_outcome_and_exact_single_append(self):
        delta, artifact = self.native("receipt-append")
        saved = copy.deepcopy((delta, artifact))
        result = self.normalize("receipt-append", delta, artifact, **self.context)
        self.complete_projection(delta, artifact, result,
            {"/artifact/id", "/artifact/timestamp", EXHAUSTION + "id", EXHAUSTION + "timestamp"})
        rows = result["delta"]["receipts/codex-wait-exhaustion/scale-worker.jsonl"]["frames"]
        self.assertEqual([result["artifact"]], rows)
        self.assertEqual(("scale-worker", 2, "consent_reopened", "census-receipt"),
            tuple(rows[0][field] for field in ("node_id", "waited_seconds", "outcome", "idempotency_key")))
        self.assertEqual(saved, (delta, artifact))

    def test_send_companion_binding_and_stdout_testimony_must_agree_before_normalizing(self):
        delta, artifact = self.native("send")
        for mismatch in ("envelope_id", "readiness"):
            changed = copy.deepcopy(delta)
            rows = self.frames(changed)
            if mismatch == "envelope_id":
                rows[1]["envelope_id"] = "msg-" + "e" * 32
            else:
                rows[1]["readiness"]["reason"] = "different_native_testimony"
            self.rewrite(changed, "events.jsonl", rows)
            with self.subTest(mismatch=mismatch), self.assertRaises(ValueError):
                self.normalize("send", changed, artifact, **self.context)

    def test_exact_changed_paths_frame_count_and_publication_order_are_not_filtered(self):
        delta, artifact = self.native("send")
        for defect in ("reverse", "extra_frame", "extra_path"):
            changed = copy.deepcopy(delta)
            rows = self.frames(changed)
            if defect == "reverse":
                self.rewrite(changed, "events.jsonl", list(reversed(rows)))
            elif defect == "extra_frame":
                self.rewrite(changed, "events.jsonl", rows + [copy.deepcopy(rows[1])])
            else:
                changed["receipts/unreviewed.jsonl"] = copy.deepcopy(changed["events.jsonl"])
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                self.normalize("send", changed, artifact, **self.context)

    def test_generated_id_and_timestamp_require_validation_not_a_generic_volatile_strip(self):
        delta, artifact = self.native("receipt-append")
        path = "receipts/codex-wait-exhaustion/scale-worker.jsonl"
        for field, bad in (("id", "not-an-exhaustion-id"),
                ("timestamp", (self.context["wall_begin"] - timedelta(days=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z"))):
            changed, output = copy.deepcopy(delta), copy.deepcopy(artifact)
            output[field] = bad
            self.rewrite(changed, path, [output])
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.normalize("receipt-append", changed, output, **self.context)

    def test_unknown_semantic_field_is_preserved_or_refused_never_silently_erased(self):
        delta, artifact = self.native("receipt-append")
        path = "receipts/codex-wait-exhaustion/scale-worker.jsonl"
        extra = {"timestamp": "literal-stable-testimony", "pid": 123, "root": str(self.home)}
        artifact["future_testimony"] = extra
        self.rewrite(delta, path, [artifact])
        original = copy.deepcopy((delta, artifact))
        try:
            result = self.normalize("receipt-append", delta, artifact, **self.context)
        except ValueError:
            self.assertEqual(original, (delta, artifact))
            return  # A closed native schema may explicitly refuse the new field.
        self.assertEqual(extra, result["artifact"]["future_testimony"])
        self.assertEqual(extra, result["delta"][path]["frames"][0]["future_testimony"])
        self.assertFalse(any("future_testimony" in row["pointer"] for row in result["normalizations"]))
        self.assertEqual(original, (delta, artifact))

    def test_foreign_source_coordinate_is_not_hidden_by_home_prefix_substitution(self):
        delta, artifact = self.native("send")
        changed = copy.deepcopy(delta)
        rows = self.frames(changed)
        foreign = str(self.home) + "-other/registry/entries.jsonl"
        artifact["evidence"]["recipient_readiness"]["mechanism_evidence"][0]["sources"][0]["path"] = foreign
        rows[1]["readiness"] = copy.deepcopy(artifact["evidence"]["recipient_readiness"])
        self.rewrite(changed, "events.jsonl", rows)
        with self.assertRaises(ValueError):
            self.normalize("send", changed, artifact, **self.context)

    def test_raw_append_length_and_digest_must_bind_the_decoded_native_payload(self):
        delta, artifact = self.native("receipt-append")
        path = "receipts/codex-wait-exhaustion/scale-worker.jsonl"
        changed = copy.deepcopy(delta)
        changed[path]["after_bytes"] += 1
        with self.assertRaises(ValueError):
            self.normalize("receipt-append", changed, artifact, **self.context)


if __name__ == "__main__":
    unittest.main()
