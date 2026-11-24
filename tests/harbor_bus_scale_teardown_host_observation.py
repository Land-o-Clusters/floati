"""Ruled host-observation projection, using a retained genuine native base.

The native activity observer runs unchanged. Only AFTER its owned child has
completed and been collected do these controls plant coherent private copies
of receipt frames, the report envelope and stdout. Those copies are synthetic
test data, never claims about observed PIDs or native execution. Existing
native completed-receipt validation and helper.normalize remain authoritative.
No fixture or child is constructed merely by importing this module.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from tests.bus_scale_test_profile import StableSourceFixture

from floati import send_prefix
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EVENT_KINDS
from floati.framing import decode_frames, encode_frame
from floati.root import FloatiRoot
from floati.seat_teardown import completed_teardown_for_session
from tests.bus_scale_test_profile import banked_send_repository
from tests.bus_scale_case_seeds import inventory
from tests.bus_scale_test_profile import full_scale_enabled, seed_counts
from tests.harbor_bus_scale_expanded_observation import ADAPTER, ENTRY, load
from tests.test_bus_scale_seat_teardown_boundary import ACTOR, SESSION, SOURCE, _inventory, _json
from tests.temp_roots import REAL_TEMP_ROOT, _PLATFORM_TEMP_ROOT, _worker_temp_root


CASE = "seat-teardown"
RECEIPTS = "receipts/seat-teardown/scale-worker.jsonl"
HELPER = SOURCE / "tests/fixtures/bus_scale_census/bus-scale-routine-teardown-2026-10-15.py"
CENSUS = SOURCE / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py"
SOURCES = (Path(__file__).resolve(), HELPER, ADAPTER, ENTRY, CENSUS,
           SOURCE / "tests/bus_scale_case_seeds.py", SOURCE / "floati/seat_teardown.py",
           SOURCE / "floati/seat_teardown_activity.py", SOURCE / "floati/records.py")


def sources():
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in SOURCES}


def retained_root():
    # Preserve platform-level retention for ordinary suite workers, while an
    # explicit owner-run bank can keep every artifact inside its owned root.
    override = os.environ.get("FLOATI_TEST_RETAINED_ROOT")
    return (_PLATFORM_TEMP_ROOT if override is None
        else _worker_temp_root(override, _PLATFORM_TEMP_ROOT))


class TeardownHostObservationTests(StableSourceFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._shared_native = None
        cls._shared_receipts = []
        cls.addClassCleanup(cls._close_shared_native)

    @classmethod
    def _close_shared_native(cls):
        fixture = cls._shared_native
        cls._shared_native = None
        if fixture is None:
            return
        fixture.doCleanups()
        for receipt in cls._shared_receipts:
            record = json.loads(receipt.read_bytes())
            record["scratch_removed"] = not fixture.base.exists()
            record["shared_fixture_cleanup_pending"] = False
            _json(receipt, record)

    def setUp(self):
        if full_scale_enabled():
            return self._build_native_fixture()
        cls = type(self)
        if cls._shared_native is None:
            fixture = cls()
            try:
                fixture._build_native_fixture()
            except BaseException:
                fixture.doCleanups()
                raise
            cls._shared_native = fixture
        fixture = cls._shared_native
        # variant() already advances each synthetic suffix at this original
        # inode, using the genuine pre-child prefix and current native stat.
        # Sharing the base across methods adds no copied-root authority.
        self.assertEqual(fixture.prefix_input["ledger_identity"][:4],
                         send_prefix._identity(fixture.home / "events.jsonl")[:4])
        for name in ("base", "safe_to_remove", "entry", "adapter", "helper", "owner",
                     "workspace", "home", "begin", "end", "pid", "genuine"):
            setattr(self, name, getattr(fixture, name))
        for name in ("pair", "context", "before", "prefix_input", "artifact", "detail", "completed"):
            setattr(self, name, copy.deepcopy(getattr(fixture, name)))
        self.retained = Path(tempfile.mkdtemp(prefix="bus-scale-teardown-host-observation-",
                                             dir=retained_root())).resolve()
        self.record = copy.deepcopy(fixture.record)
        self.record.update(test=self.id(), variants=[], shared_genuine_base=str(fixture.retained))
        self._shared_fixture_user = True
        self.addCleanup(self.retain_and_remove)

    def _build_native_fixture(self):
        self.base = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-host-observation-",
                                         dir=REAL_TEMP_ROOT)).resolve()
        self.retained = Path(tempfile.mkdtemp(prefix="bus-scale-teardown-host-observation-",
                                             dir=retained_root())).resolve()
        self.safe_to_remove = True
        self.record = {"test": self.id(), "performance_evidence": False,
                       "native_activity_unmodified": full_scale_enabled(), "source_before": sources(),
                       "transport_profile": "full_native" if full_scale_enabled() else "bounded_in_process",
                       "original_coordinate": str(self.base), "variants": []}
        self.addCleanup(self.retain_and_remove)
        source_workspace, _sha = banked_send_repository(self.base)
        self.entry = load(ENTRY, "host_observation_native_entry")
        self.adapter = load(ADAPTER, "host_observation_adapter")
        self.helper = load(HELPER, "host_observation_helper")
        self.owner = load(SOURCE / "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py",
                          "host_observation_native_owner")
        census = load(CENSUS, "host_observation_census")
        self.pair, self.workspace = census.workload_seed({}, "events", CASE, source_workspace,
                                                       self.base, self.entry, {}, counts=seed_counts())
        self.home, self.context = self.pair["prepared_home"], self.pair["context"]
        shutil.rmtree(self.home)
        shutil.copytree(self.pair["1"], self.home, copy_function=shutil.copy2)
        self.assertIsNotNone(send_prefix.warm(FloatiRoot.open_direct_home(self.home)))
        self.before = inventory(self.home)
        from tests.fixtures.bus_scale_census.bus_scale_prefix_effects import capture_prefix_input
        self.prefix_input = capture_prefix_input(self.home, self.before)
        self.assertEqual(seed_counts()[0], self.before["events.jsonl"].count(b"\n"))
        _json(self.home.parent / "case-context.json", self.context)
        _json(self.retained / "context.json", self.context)
        _json(self.retained / "native-before-raw.json",
              {path: base64.b64encode(raw).decode() for path, raw in self.before.items()})
        _json(self.retained / "native-prefix-input.json", self.prefix_input)
        output, error, detail = (self.home.parent / name for name in
                                 ("host-base-stdout.txt", "host-base-stderr.txt", "host-base-detail.json"))
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("GIT_", "FLOATI_", "PYTHON", "LD_", "DYLD_"))}
        environment.update(PATH="/usr/bin:/bin", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
        self.begin = datetime.now(timezone.utc)
        with output.open("xb") as stdout, error.open("xb") as stderr:
            child = self.owner.launch_owned_child(
                ["/usr/bin/python3", "-B", str(ENTRY), "cpu", CASE, str(self.home),
                 str(self.workspace), str(detail), "owned-census-v1"],
                cwd=self.workspace, env=environment, stdout=stdout, stderr=stderr)
            self.safe_to_remove = False
            collected = self.owner.collect_owned_child(child, deadline_seconds=120, collection_seconds=10)
            self.safe_to_remove = collected.get("child_collected") is True
        self.end = datetime.now(timezone.utc)
        self.pid = child.pid
        self.record.update(native_collection=collected, native_pid=self.pid,
                           wall_begin=self.begin.isoformat(), wall_end=self.end.isoformat(),
                           stdout_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                           stderr_sha256=hashlib.sha256(error.read_bytes()).hexdigest())
        for path, name in ((output, "native-stdout.txt"), (error, "native-stderr.txt"),
                           (detail, "native-detail.json")):
            if path.exists():
                shutil.copy2(path, self.retained / name)
        self.assertEqual({"status": "complete", "exit_code": 0, "child_collected": True,
                          "interrupted": False}, collected, "genuine native base must complete first")
        self.artifact = json.loads(output.read_bytes())
        self.detail = json.loads(detail.read_bytes())
        self.assertEqual("ok", self.artifact["status"])
        self.assertEqual("completed", self.artifact["evidence"]["step"])
        self.completed = inventory(self.home)
        self.assertEqual(self.artifact["evidence"], completed_teardown_for_session(
            FloatiRoot.open_direct_home(self.home), ACTOR, SESSION))
        witness = self.helper.witness(CASE, self.home, self.before, self.artifact, self.context, self.detail)
        self.assertEqual({"exit_code": 0, "status": "ok", "effect": {
            "kind": "seat_teardown_completed", "count": 1}}, witness)
        self.assertEqual(self.completed, inventory(self.home), "native read proof must not change the base")
        self.genuine = self.base / "genuine-completed-root"
        shutil.copytree(self.home, self.genuine, copy_function=shutil.copy2)
        _json(self.retained / "native-witness.json", witness)
        self.record["native_base_completed_verified"] = True
        self.record["baseline_unnamed_rows"] = copy.deepcopy(self.artifact["evidence"]["activity"]["native_unknown"])

    def retain_and_remove(self):
        self.record["source_after"] = sources()
        try:
            for path in SOURCES:
                target = self.retained / "source" / path.relative_to(SOURCE)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            if self.safe_to_remove:
                shutil.copytree(self.base, self.retained / "fixture", copy_function=shutil.copy2)
                self.record["fixture_inventory"] = _inventory(self.retained / "fixture")
            else:
                self.record["original_coordinate_preserved_for_uncollected_child"] = str(self.base)
        finally:
            if self.safe_to_remove and not getattr(self, "_shared_fixture_user", False):
                shutil.rmtree(self.base)
            self.record["scratch_removed"] = not self.base.exists()
            if getattr(self, "_shared_fixture_user", False):
                self.record["shared_fixture_cleanup_pending"] = True
                type(self)._shared_receipts.append(self.retained / "result.json")
            _json(self.retained / "result.json", self.record)
            print("teardown host-observation controls retained: " + str(self.retained), flush=True)

    def variant(self, label, change=None, artifact_change=None):
        """Plant at the original bound root; saved copies are archives only."""
        # Keep the collected event inode: the immediate pre-child certificate
        # binds it. Restore the pre-child cache, then let its native append
        # producer derive the exact cache for this synthetic report variant.
        # Rebuilding a copied cache would replace its certified generation.
        restored = {path: raw for path, raw in self.completed.items()
                    if not path.startswith(".send-prefix/")}
        restored.update({path: raw for path, raw in self.before.items()
                         if path.startswith(".send-prefix/")})
        current = inventory(self.home)
        for relative in set(current) - set(restored):
            self.assertTrue(relative.startswith(".send-prefix/nodes/"),
                            "a synthetic variant introduced an unrelated file")
            (self.home / relative).unlink()
        for relative, raw in restored.items():
            if current.get(relative) != raw:
                path = self.home / relative
                path.write_bytes(raw)
                path.chmod(0o600)
        rows = decode_frames(self.completed[RECEIPTS])
        self.assertEqual(["started", "paused", "reported", "completed"], [row["step"] for row in rows])
        if change is not None:
            for row in rows:
                change(row)
            (self.home / RECEIPTS).write_bytes(b"".join(encode_frame(row) for row in rows))
            events = decode_frames(self.completed["events.jsonl"])
            report = next(row for row in events if row["id"] == self.artifact["evidence"]["envelope_id"])
            report["note"] = rows[-1]["report_note"]
            (self.home / "events.jsonl").write_bytes(b"".join(encode_frame(row) for row in events))
        event_path = self.home / "events.jsonl"
        event_raw = event_path.read_bytes()
        self.assertTrue(event_raw.startswith(self.before["events.jsonl"]))
        self.assertEqual(self.prefix_input["ledger_identity"][:4],
                         send_prefix._identity(event_path)[:4])
        batch = decode_frames(event_raw[len(self.before["events.jsonl"]):])
        prefix = send_prefix.Prefix(FloatiRoot.open_direct_home(self.home), EVENT_KINDS)
        prefix.payload = copy.deepcopy(json.loads(
            self.before[".send-prefix/checkpoint.json"])["payload"])
        prefix.appended(batch, tuple(encode_frame(row) for row in batch),
                        send_prefix._identity(event_path))
        artifact = copy.deepcopy(self.artifact)
        artifact["evidence"] = copy.deepcopy(rows[-1])
        if artifact_change is not None:
            artifact_change(artifact)
        bank = self.retained / "variants" / label
        bank.mkdir(mode=0o700, parents=True)
        provenance = {"label": label, "synthetic_post_collection_variant": change is not None or artifact_change is not None,
                      "native_execution": False, "genuine_base_stdout_sha256": self.record["stdout_sha256"],
                      "actual_bound_home": str(self.home), "copied_root_is_archive_only": True,
                      "variant_cache_producer": "floati.send_prefix.Prefix.appended",
                      "prechild_event_identity_and_generation_retained": True}
        _json(bank / "provenance.json", provenance)
        _json(bank / "artifact.json", artifact)
        shutil.copytree(self.home, bank / "raw-root", copy_function=shutil.copy2)
        after = inventory(self.home)
        delta = self.adapter.native_delta(self.before, after, home=self.home,
                                          prefix_input=self.prefix_input)
        _json(bank / "native-delta.json", delta)
        _json(bank / "before-raw.json", {path: base64.b64encode(raw).decode() for path, raw in self.before.items()})
        self.record["variants"].append(provenance)
        return artifact, delta, bank, after

    def normalized(self, artifact, delta, bank, after):
        original = copy.deepcopy(artifact)
        try:
            result = self.helper.normalize(CASE, delta, artifact, self.context,
                home=self.home, workspace=self.workspace, before_raw_files=self.before,
                wall_begin=self.begin, wall_end=self.end, child_pid=self.pid,
                prefix_input=self.prefix_input)
        except (IntegrityFailure, ProtocolRefusal, self.helper.Unresolved) as error:
            _json(bank / "refusal.json", {"type": type(error).__name__, "code": error.code, "detail": str(error)})
            raise
        else:
            _json(bank / "normalized.json", result)
            return result
        finally:
            self.assertEqual(original, artifact, "normalization mutated the raw supplied artifact")
            self.assertEqual(after, inventory(self.home), "normalization rewrote planted raw testimony")
            self.assertEqual(self.record["source_before"], sources(), "instrument source changed during control")

    def append_unknown(self, row, *, named=False):
        unknown = row["activity"]["native_unknown"]
        count = len(unknown)
        pids = {item["pid"] for item in unknown}
        pid = 2147483647
        while pid in pids:
            pid -= 1
        unknown.append({"pid": pid, "reason": "cwd_unresolvable",
                        "receipt_ids": [self.context["claim_id"]] if named else []})
        unknown.sort(key=lambda item: item["pid"])
        old = "; unnamed native UNKNOWN " + str(count) + " (reasons in receipt);"
        new = "; unnamed native UNKNOWN " + str(count + 1) + " (reasons in receipt);"
        self.assertEqual(1, row["report_note"].count(old), "derived native count token must bind the base list")
        row["report_note"] = row["report_note"].replace(old, new, 1)

    def assert_explicit_host_exclusion(self, projected, raw_artifact, raw_delta):
        """Exclusion must be visible; fabricated quiet/count zero is not it."""
        audits = projected["normalizations"]
        pointers = {row["pointer"] for row in audits}
        note_paths = [("/artifact/evidence/report_note", projected["artifact"]["evidence"]["report_note"])]
        unknown_paths = []
        if raw_artifact["evidence"]["activity"]["native_unknown"]:
            unknown_paths.append("/artifact/evidence/activity/native_unknown")
        receipt_key = RECEIPTS.replace("~", "~0").replace("/", "~1")
        raw_receipts = decode_frames(base64.b64decode(raw_delta[RECEIPTS]["payload_base64"]))
        for ordinal, raw in enumerate(raw_receipts):
            prefix = "/delta/" + receipt_key + "/frames/" + str(ordinal)
            note_paths.append((prefix + "/report_note", projected["delta"][RECEIPTS]["frames"][ordinal]["report_note"]))
            if raw["activity"]["native_unknown"]:
                unknown_paths.append(prefix + "/activity/native_unknown")
        raw_events = decode_frames(base64.b64decode(raw_delta["events.jsonl"]["payload_base64"]))
        for ordinal, raw in enumerate(raw_events):
            if raw["id"] == raw_artifact["evidence"]["envelope_id"]:
                note_paths.append(("/delta/events.jsonl/frames/" + str(ordinal) + "/note",
                                   projected["delta"]["events.jsonl"]["frames"][ordinal]["note"]))
        for pointer, note in note_paths:
            tokens = re.findall(r"; unnamed native UNKNOWN (.*?) \(reasons in receipt\);", note)
            self.assertEqual(1, len(tokens), "projected prose must retain one explicit host-observation count field")
            token = tokens[0].strip()
            self.assertTrue(token and any(character.isalpha() for character in token),
                            "reviewed host count exclusion needs an explicit nonnumeric token, not a fabricated native count")
            self.assertIsNone(re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", token),
                              "native count zero or another number cannot stand for an excluded host observation")
            self.assertIn(pointer, pointers, "normalization audit omitted an affected native report/count path")
        for pointer in unknown_paths:
            self.assertTrue(any(value == pointer or value.startswith(pointer + "/") for value in pointers),
                            "normalization audit omitted the affected unnamed native_unknown path: " + pointer)

    def test_only_unnamed_host_rows_and_exact_derived_count_leave_cross_arm_projection_equal(self):
        base_artifact, base_delta, base_bank, base_after = self.variant("native-base")
        baseline = self.normalized(base_artifact, base_delta, base_bank, base_after)
        artifact, delta, bank, after = self.variant("planted-unnamed", self.append_unknown)
        # This coherent planted variant must pass the existing native witness;
        # acceptance of the new comparison rule is the sole intended RED.
        self.assertEqual(artifact["evidence"], completed_teardown_for_session(
            FloatiRoot.open_direct_home(self.home), ACTOR, SESSION))
        self.assertNotEqual(base_artifact["evidence"]["activity"]["native_unknown"],
                            artifact["evidence"]["activity"]["native_unknown"])
        self.assertNotEqual(base_artifact["evidence"]["report_note"], artifact["evidence"]["report_note"])
        projected = self.normalized(artifact, delta, bank, after)
        self.assertEqual({key: baseline[key] for key in ("artifact", "delta")},
                         {key: projected[key] for key in ("artifact", "delta")},
                         "reviewed unnamed host rows/count alone must not invalidate cross-arm native equivalence")
        self.assert_explicit_host_exclusion(baseline, base_artifact, base_delta)
        self.assert_explicit_host_exclusion(projected, artifact, delta)

    def test_named_unknown_refuses_native_completed_witness_before_projection(self):
        artifact, delta, bank, after = self.variant("planted-named", lambda row: self.append_unknown(row, named=True))
        with self.assertRaises((IntegrityFailure, ProtocolRefusal, self.helper.Unresolved)):
            self.normalized(artifact, delta, bank, after)
        self.assertFalse((bank / "normalized.json").exists(), "named UNKNOWN cannot become successful projected work")

    def test_unrelated_report_prose_verdict_jobs_and_uid_scope_are_not_host_noise(self):
        for kind in ("report-prose", "verdict", "native-jobs", "uid-scope"):
            with self.subTest(kind=kind):
                def change(row):
                    if kind == "report-prose":
                        row["report_note"] += "; UNREVIEWED FACT MUST REMAIN"
                    elif kind == "native-jobs":
                        row["activity"]["native_jobs"] = [{"pid": self.pid, "cwd": str(self.workspace)}]
                    elif kind == "uid-scope":
                        old_uid = row["activity"]["native_scope"]["uid"]
                        new_uid = (os.getuid() + 1) % 4294967296
                        row["activity"]["native_scope"]["uid"] = new_uid
                        old_token = "; native scope invoking uid " + str(old_uid) + ";"
                        new_token = "; native scope invoking uid " + str(new_uid) + ";"
                        self.assertEqual(1, row["report_note"].count(old_token))
                        row["report_note"] = row["report_note"].replace(old_token, new_token, 1)
                def artifact_change(value):
                    if kind == "verdict":
                        value["status"] = "refused"
                artifact, delta, bank, after = self.variant("planted-" + kind,
                    change if kind != "verdict" else None, artifact_change if kind == "verdict" else None)
                if kind == "uid-scope":
                    self.assertEqual(artifact["evidence"], completed_teardown_for_session(
                        FloatiRoot.open_direct_home(self.home), ACTOR, SESSION),
                        "coherent historical UID/prose must pass native replay before the invoking-UID guard")
                    with self.assertRaises(self.helper.Unresolved) as refusal:
                        self.normalized(artifact, delta, bank, after)
                    self.assertIn("scope", str(refusal.exception), "helper must reject the changed invoking-UID scope")
                else:
                    with self.assertRaises((IntegrityFailure, ProtocolRefusal, self.helper.Unresolved)):
                        self.normalized(artifact, delta, bank, after)
                self.assertFalse((bank / "normalized.json").exists(), "unreviewed native fact was projected away")


if __name__ == "__main__":
    unittest.main()
