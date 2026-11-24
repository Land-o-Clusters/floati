"""Owned native-effect and raw-bank controls, independent of metric claims."""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from tests.bus_scale_test_profile import seed_counts
import shutil
import tempfile
import unittest

from floati import cli
from floati.codex_wait_contract import CodexWaitReceiptLedger
from floati.events import EventLog
from floati.jsonl import read_records
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location("bus_scale_producer_" + name,
        ROOT / "tests/fixtures/bus_scale_census" / (name + "-2026-10-09.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BusScaleProducerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-producer-controls-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "scale-fleet"
        self.home.mkdir(mode=0o700)
        self.root = FloatiRoot.open_direct_home(self.home)
        from tests.bus_scale_test_profile import banked_send_repository
        repository, self.sha = banked_send_repository(self.base)
        self.workspace = self.base / "workspace"
        repository.rename(self.workspace)
        self.workspace.chmod(0o700)
        for node in ("architect", "scale-worker"):
            Registry(self.root).register(node, "codex")
        self.subject = load("census-bus-scale")

    def test_real_send_artifact_binds_the_nested_exact_appended_envelope(self):
        evidence = EventLog(self.root).send("scale-worker", "architect", "floati", self.sha,
            "note.md", "fixed selected census send", idempotency_key="census-send")
        artifact = dict(artifact_version=0, command="send", status="ok", evidence=evidence)
        witness = self.subject.effect_witness("send", self.home, {"event_rows": []}, json.dumps(artifact), "", {},
                                              workspace=self.workspace)
        self.assertEqual({"kind": "message_envelope_appended", "count": 1}, witness["effect"])

    def test_wait_rejects_a_receipt_for_another_session_even_with_expected_count(self):
        CodexWaitReceiptLedger(self.root).record_exhaustion(node_id="scale-worker",
            session_digest="b" * 64, waited_seconds=2, idempotency_key="foreign-exhaustion")
        before = {"exhaustion_count": 0, "exhaustion_rows": []}
        with self.assertRaisesRegex(RuntimeError, "session|exhaustion|effect"):
            self.subject.effect_witness("wait-start", self.home, before,
                json.dumps({"decision": "block", "reason": "(floati: wait deadline exhausted; end this turn to re-arm)"}),
                "", {"idle": {}})

    def test_send_rejects_extra_native_event_even_with_exact_selected_envelope(self):
        evidence = EventLog(self.root).send("scale-worker", "architect", "floati", self.sha,
            "note.md", "fixed selected census send", idempotency_key="census-send")
        EventLog(self.root).send("scale-worker", "architect", "floati", "a" * 40,
            "docs/evidence/ping.md", "extra selected work", idempotency_key="extra-send")
        artifact = dict(artifact_version=0, command="send", status="ok", evidence=evidence)
        with self.assertRaisesRegex(RuntimeError, "suffix|extra|count|exact"):
            self.subject.effect_witness("send", self.home, {"event_rows": []}, json.dumps(artifact), "", {},
                                       workspace=self.workspace)

    def test_status_ok_without_native_evidence_is_rejected(self):
        with self.assertRaises((RuntimeError, KeyError)):
            self.subject.effect_witness("status", self.home, {}, json.dumps({"status": "ok"}), "", {})

    def test_native_log_arguments_render_real_history_without_replay_animation(self):
        EventLog(self.root).send("scale-worker", "architect", "floati", "a" * 40,
            "docs/evidence/ping.md", "bounded scale fixture", idempotency_key="history")
        entry = load("bus-scale-native-entry")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(entry.arguments("log-history", self.home, self.base))
        self.assertEqual(0, code)
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(1, len(artifact["evidence"]["messages"]))
        self.assertEqual("", stderr.getvalue())

    def test_prepared_non_event_seed_has_constant_native_history_and_delivery(self):
        large = self.base / "large" / "scale-fleet"
        large.parent.mkdir()
        shutil.copytree(self.home, large)
        workspace = self.workspace
        self.subject.prepare({"registry": {"1": self.home, "10": large,
            "ledger_relative": "registry/entries.jsonl"}}, workspace)
        for home in (self.home, large):
            root = FloatiRoot.open_direct_home(home)
            events = EventLog(root).records()
            self.assertEqual(1, len(events))
            self.assertEqual("bounded scale fixture", events[0]["note"])
            self.assertEqual(1, len(read_records(root, "receipts/deliveries/architect.jsonl",
                allowed_kinds={"delivered_unpresented"})))

    def test_collection_hash_covers_the_exact_written_newline(self):
        path = self.base / "collection.json"
        digest = self.subject.write_collection(path, {"child_collected": True})
        self.assertTrue(path.read_bytes().endswith(b"\n"))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_native_delta_rejects_a_rewritten_prefix_and_unexpected_mutation(self):
        cases = (
            ({"events.jsonl": b"old\n"}, {"events.jsonl": b"bad\nnew\n"}),
            ({"registry/entries.jsonl": b"old\n"}, {"registry/entries.jsonl": b"new\n"}),
            ({}, {"state/foreign.json": b"{}\n"}),
        )
        for before, after in cases:
            with self.subTest(after=after):
                with self.assertRaisesRegex(RuntimeError, "prefix|unexpected|changed"):
                    self.subject.native_delta("send", before, after)

    def test_case_seed_dispatch_preserves_old_inputs_and_prepares_new_work_once(self):
        dispatch = getattr(self.subject, "workload_seed", None)
        self.assertTrue(callable(dispatch), "expanded population has no native case seed dispatch")
        entry = load("bus-scale-native-entry")
        old = {"events": {"1": self.home, "10": self.home,
                          "ledger_relative": "events.jsonl"}}
        cache = {}
        pair, workspace = dispatch(old, "events", "send", self.workspace, self.base, entry, cache)
        self.assertIs(pair, old["events"])
        self.assertEqual(self.workspace, workspace)
        expanded, native_workspace = dispatch(old, "events", "sent", self.workspace,
                                               self.base, entry, cache, counts=seed_counts())
        again, same_workspace = dispatch(old, "events", "sent", self.workspace,
                                          self.base, entry, cache, counts=seed_counts())
        self.assertIs(expanded, again, "native preface must be authored only once")
        self.assertEqual(native_workspace, same_workspace)
        self.assertNotEqual(self.workspace, native_workspace,
                            "each new case needs independent Git/auxiliary preparation")
        self.assertEqual(self.sha, entry.workspace_send_sha(native_workspace))
        self.assertTrue(native_workspace.parent.name.startswith("bus-scale-census-owned-"))
        self.assertEqual("sent", expanded["context"]["case"])
        self.assertEqual(seed_counts()[0], (expanded["1"] / expanded["ledger_relative"]).read_bytes().count(b"\n"))
        self.assertEqual(seed_counts()[1], (expanded["10"] / expanded["ledger_relative"]).read_bytes().count(b"\n"))


if __name__ == "__main__":
    unittest.main()
