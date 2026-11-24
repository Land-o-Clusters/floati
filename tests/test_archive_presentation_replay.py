"""Cold presentation checks built from native prepare/flush transactions."""
from copy import deepcopy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from floati import bus_epoch, archive_presentation_replay, jsonl
from floati.archive_presentation_replay import replay_presentation_history
from floati.archive_projection_replay import replay_projection_pool
from floati.archive_survivor_pool import encode_survivor_source_pool
from floati.archive_survivor_sources import collect_survivor_sources
from floati.cursor import SparseCursor
from floati.cursor_presentations import CursorPresentationLedger
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.framing import FrameError, decode_frames, encode_frame
from tests import test_epoch_carry as carry_fixture


class ArchivePresentationReplayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = carry_fixture.EpochCarryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def inputs(self):
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            plan = collect_survivor_sources(self.root)
        entry = plan["sources"]["presentation"]["entries"][0]
        consents = {group["consent"]["id"]: group["consent"] for group in entry["groups"]}
        claims = {group["claim"]["id"]: group["claim"] for group in entry["groups"] if group["claim"] is not None}
        return encode_survivor_source_pool(plan), {
            "tenant_id": self.root.tenant_id, "node_id": "seat-a", "witness": entry["witness"],
            "physical_bytes": (self.root.path / CursorPresentationLedger.relative("seat-a")).read_bytes(),
            "consents": list(consents.values()), "claims": list(claims.values())}

    def replay(self, chunks, inputs):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("cold payload read")),\
                patch.object(Path, "write_bytes", side_effect=AssertionError("cold write")):
            return replay_presentation_history(chunks, **inputs)

    def prepare(self, *, flushed=True):
        self.message = self.fixture.send("presentation")
        return self.fixture.presentation(flushed=flushed)

    def rewrite_rows(self, inputs, change):
        rows = decode_frames(inputs["physical_bytes"])
        for row in rows:
            change(row)
        raw = b"".join(encode_frame(row) for row in rows)
        witness = inputs["witness"]
        reserved = witness["reserved_record"]
        if reserved is not None:
            change(reserved)
            prefix = raw[:-len(encode_frame(rows[-1]))]
        else:
            prefix = raw
        witness["before_bytes"] = len(prefix)
        witness["before_digest"] = hashlib.sha256(prefix).hexdigest()
        inputs["physical_bytes"] = raw

    def test_flushed_then_acked_history_replays_after_two_automatic_rolls(self):
        _binding, consent, _classification, prepared = self.prepare()
        SparseCursor(self.root).ack("seat-a", [self.message["id"]], acting_session_id="conversation-a")
        self.fixture.automatic_roll()
        self.fixture.automatic_roll()
        chunks, inputs = self.inputs()
        expected = decode_frames(inputs["physical_bytes"])
        result = self.replay(chunks, inputs)
        self.assertEqual(expected, result["records"])
        self.assertEqual(1, len(result["groups"]))
        group = result["groups"][0]
        self.assertEqual(prepared, group["records"][0])
        self.assertEqual(consent, group["consent"])
        self.assertEqual(prepared["output_digest"], group["records"][-1]["output_digest"])
        self.assertEqual(prepared["item_ids"], group["eligible_ids"])
        self.assertEqual(expected[-1]["delivery_receipt_id"], group["following_delivery"]["id"])
        self.assertFalse(result["uncertain"])


    def test_missing_physical_prepare_cannot_be_hidden_by_effective_group_data(self):
        self.prepare()
        chunks, inputs = self.inputs()
        inputs["physical_bytes"] = b""
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            self.replay(chunks, inputs)

    def test_terminal_immutable_digest_change_refuses_even_with_consistent_witness(self):
        self.prepare()
        chunks, original = self.inputs()
        replay_projection_pool(chunks, self.root.tenant_id)
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            plan = collect_survivor_sources(self.root)
        source = plan["sources"]["presentation"]["entries"][0]["groups"][0]["source"]
        for case in ("terminal digest", "all digests", "consent", "claim", "missing claim", "earlier cut", "following delivery"):
            with self.subTest(case=case):
                inputs = deepcopy(original)
                if case in ("terminal digest", "all digests"):
                    self.rewrite_rows(inputs, lambda row: row.update(output_digest="f" * 64)
                                      if case == "all digests" or row["phase"] == "flushed" else None)
                elif case in ("consent", "claim"):
                    inputs[case + "s"][0]["workspace"] += "-different"
                elif case == "missing claim":
                    self.assertTrue(inputs["claims"])
                    inputs["claims"] = []
                elif case == "earlier cut":
                    def borrow(row):
                        row["event_prefix_digest"] = source[1][0]
                        row["delivery_prefix_digest"] = source[3][-1]
                    self.rewrite_rows(inputs, borrow)
                else:
                    def change(row):
                        if row["phase"] == "flushed":
                            row["delivery_receipt_id"] = "delivery-" + "0" * 12 + "7" + "0" * 3 + "8" + "0" * 15
                    self.rewrite_rows(inputs, change)
                expected = IntegrityFailure if case == "all digests" else (IntegrityFailure, ProtocolRefusal)
                with self.assertRaises(expected):
                    self.replay(chunks, inputs)


    def test_unappended_terminal_witness_must_match_its_original_pool_frame(self):
        self.fixture.send("reserved-terminal-identity")
        native = CursorPresentationLedger._append_reserved
        def fail_terminal(plane, node, raw, row):
            if row["phase"] == "flushed":
                raise OSError("terminal append fault")
            return native(plane, node, raw, row)
        with patch.object(CursorPresentationLedger, "_append_reserved", fail_terminal):
            with self.assertRaises(OSError):
                self.fixture.presentation(flushed=True)
        chunks, inputs = self.inputs()
        self.assertEqual("flushed", inputs["witness"]["reserved_record"]["phase"])
        inputs["witness"]["reserved_record"]["output_digest"] = "f" * 64
        with self.assertRaises(IntegrityFailure):
            self.replay(chunks, inputs)


    def test_virtual_prepare_effective_limit_is_checked_before_decoding(self):
        self.fixture.send("bounded-reserved-prepare")
        with patch.object(CursorPresentationLedger, "_append_reserved", side_effect=OSError("prepare append fault")):
            with self.assertRaises(OSError):
                self.fixture.presentation()
        chunks, inputs = self.inputs()
        self.assertEqual(b"", inputs["physical_bytes"])
        with patch.object(archive_presentation_replay, "MAX_LEDGER_BYTES", 0),\
                patch.object(jsonl, "decode_frames",
                             side_effect=AssertionError("effective bytes decoded before bound")):
            with self.assertRaises(IntegrityFailure) as refused:
                self.replay(chunks, inputs)
        self.assertEqual("ledger_too_large", refused.exception.code)

    def test_raw_whitespace_frame_limit_cannot_borrow_small_canonical_length(self):
        self.prepare()
        chunks, inputs = self.inputs()
        lines = inputs["physical_bytes"].splitlines(keepends=True)
        lines[0] = b" " * archive_presentation_replay.MAX_RECORD_BYTES + lines[0]
        inputs["physical_bytes"] = b"".join(lines)
        prefix = b"".join(lines[:-1])
        inputs["witness"]["before_bytes"] = len(prefix)
        inputs["witness"]["before_digest"] = hashlib.sha256(prefix).hexdigest()
        with self.assertRaises(IntegrityFailure) as refused:
            self.replay(chunks, inputs)
        self.assertEqual("record_too_large", refused.exception.code)

    def test_native_framing_errors_remain_typed_integrity_refusals(self):
        self.prepare()
        chunks, original = self.inputs()
        for raw, code in ((b"{", "incomplete_jsonl_line"), (b"\n", "blank_jsonl_line"),
                          (b'{"kind":1,"kind":2}\n', "duplicate_json_key")):
            with self.subTest(code=code):
                inputs = deepcopy(original)
                inputs["physical_bytes"] = raw
                inputs["witness"].update(before_bytes=len(raw),
                    before_digest=hashlib.sha256(raw).hexdigest(), reserved_record=None)
                with self.assertRaises((IntegrityFailure, FrameError)) as refused:
                    self.replay(chunks, inputs)
                self.assertIsInstance(refused.exception, IntegrityFailure)
                self.assertEqual(code, refused.exception.code)

    def test_empty_history_validates_node_before_pool_work(self):
        self.prepare()
        chunks, original = self.inputs()
        for node in ("../foreign", []):
            with self.subTest(node=node):
                inputs = deepcopy(original)
                inputs.update(node_id=node, physical_bytes=b"", consents=[], claims=[])
                inputs["witness"].update(node_id=node, before_bytes=0,
                    before_digest=hashlib.sha256(b"").hexdigest(), reserved_record=None)
                with self.assertRaises((ProtocolRefusal, TypeError)) as refused:
                    self.replay(chunks, inputs)
                self.assertIsInstance(refused.exception, ProtocolRefusal)
                self.assertEqual("node_invalid", refused.exception.code)

if __name__ == "__main__":
    unittest.main()
