"""Legacy cross-ledger evidence must degrade one node, never the whole fleet."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile


# Run the exact archived reader in an isolated child before importing any
# current product or fixture modules. This is acceptance proof, not a mocked
# old status implementation or a second import of current Floati.
if len(sys.argv) > 1 and sys.argv[1] == "--old-status":
    from datetime import datetime

    old_source, home, stamp = sys.argv[2:5]
    sys.path.insert(0, old_source)
    import floati
    from floati.projection import FleetProjection
    from floati.root import FloatiRoot

    root = FloatiRoot.open_direct_home(Path(home), create=False)
    instant = datetime.fromisoformat(stamp)
    artifact = FleetProjection(root).status_artifact(instant)
    proof = {"source": str(Path(floati.__file__).resolve().parents[1]), "artifact": artifact}
    if len(sys.argv) > 5 and sys.argv[5] == "warm-board":
        from floati.tui import model_from_root
        proof["board_nodes"] = [dict(row) for row in model_from_root(root, instant).nodes]
    proof["cache_files"] = [str(path) for path in (root.tenant_home / ".floati-snapshots/v0").glob("*.json")]
    print(json.dumps(proof, sort_keys=True))
    raise SystemExit(0)


import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import tests  # Pin HOME before current product imports.

from floati import fixture_ids as ids
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.message_consumption import MessageConsumption
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.tui import model_from_root
from floati.tui_render import render_plain_dump
from tests import public_projection
from tests import test_seat_board as board_fixture
from tests.public_projection import require_harbor_history
from tests.schema_validation import SchemaValidationError, _CallCache, _validate


REPOSITORY = Path(__file__).resolve().parents[1]
OLD_SHA = "da6573b0dedecb31d28dc640c7b4b26b41e405ce"
CACHE_CANDIDATE_SHA = "3b10dda91c146e64239200b0df19a81841aa7e59"
REASONS = ("ack_evidence_invalid", "delivery_evidence_invalid")


def unknown(code):
    return {"state": "unknown", "reason_code": code,
            "delivered": None, "acknowledged": None, "consumed": None}


def ledger_bytes(root):
    """Capture durable truth; legitimate snapshot-cache/lock files are separate."""
    return {path.relative_to(root.tenant_home).as_posix(): path.read_bytes()
            for path in root.tenant_home.rglob("*.jsonl") if path.is_file()}


class CurrentReaderAvailabilityTests(unittest.TestCase):
    def test_missing_history_fails_in_harbor_and_skips_in_public_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            subprocess.run(["/usr/bin/git", "init", "--quiet", str(repository)], check=True)
            marker = repository / "history-fixture-marker"
            marker.touch()
            with mock.patch(__name__ + ".REPOSITORY", repository), mock.patch.object(
                public_projection, "HARBOR_POLICY_RELATIVE", marker.name
            ):
                name = "test_orphan_plain_ack_without_any_envelopes_is_unknown"
                for projected in (False, True):
                    with self.subTest(projected=projected):
                        if projected:
                            marker.unlink()
                        result = unittest.TestResult()
                        unittest.TestSuite([LegacyConsumptionReaderTests(name)]).run(result)
                        if projected:
                            self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
                            self.assertEqual(1, len(result.skipped))
                            reason = result.skipped[0][1]
                            self.assertTrue(reason.startswith("public_shallow_history:"), reason)
                            self.assertIn(OLD_SHA, reason)
                        else:
                            self.assertEqual([], result.skipped)
                            self.assertEqual(1, len(result.failures), result.errors)
                            self.assertIn(OLD_SHA, result.failures[0][1])

    def test_current_schema_control_runs_without_historical_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "tests" / Path(__file__).name
            module.parent.mkdir()
            module.write_bytes(Path(__file__).read_bytes())
            result = subprocess.run(
                [sys.executable, "-B", "-c",
                 "import runpy, sys, unittest\n"
                 "namespace = runpy.run_path(sys.argv[1])\n"
                 "case = namespace['LegacyConsumptionReaderTests'](sys.argv[2])\n"
                 "result = unittest.TextTestRunner().run(unittest.TestSuite([case]))\n"
                 "sys.exit(not (result.wasSuccessful() and not result.skipped))",
                 str(module), "test_progress_field_schemas_refuse_partial_or_unsafe_unknown"],
                cwd=REPOSITORY, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class LegacyConsumptionReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_temporary = tempfile.TemporaryDirectory(prefix="floati-old-reader-")
        cls.addClassCleanup(cls.old_temporary.cleanup)
        cls.old_base = Path(cls.old_temporary.name)
        cls.old_source = None
        cls.old_candidate_source = None
        cls.old_home = cls.old_base / "home"
        cls.old_temp = cls.old_base / "temp"
        cls.old_home.mkdir()
        cls.old_temp.mkdir()

    @classmethod
    def _extract_reader(cls, sha, label):
        source = cls.old_base / label
        result = subprocess.run(
            ["/usr/bin/git", "archive", "--format=tar", sha], cwd=REPOSITORY,
            capture_output=True, check=False,
        )
        if result.returncode != 0:
            raise AssertionError("exact old reader unavailable: " + result.stderr.decode())
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                relative = Path(member.name)
                if relative.is_absolute() or ".." in relative.parts or member.issym() or member.islnk():
                    raise AssertionError("old committed archive contains a nonordinary source member")
            source.mkdir()
            archive.extractall(source)
        return source

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="floati-legacy-consumption-")
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        for node in ("sender", "affected", "healthy"):
            Registry(self.root).register(node, "codex")
        self.events = EventLog(self.root)
        self.cursor = SparseCursor(self.root)
        self.now = datetime.now(timezone.utc)

    def _native_consumed_message(self, node):
        message = self.events.send(
            "sender", node, "floati", "a" * 40, "docs/dispatch.md", node,
            idempotency_key="dispatch-" + uuid7_hex(),
        )
        self.events.present(node)
        self.cursor.ack(node, [str(message["id"])], acting_session_id="legacy-session")
        MessageConsumption(self.root).consume(node, str(message["id"]), "legacy-session")
        return message

    def _old_status_accepts(self):
        cls = type(self)
        if cls.old_source is None:
            cls.old_source = cls._extract_reader(OLD_SHA, "source")
        source = cls.old_source
        before = ledger_bytes(self.root)
        environment = dict(os.environ)
        environment.update(HOME=str(self.old_home), TMPDIR=str(self.old_temp),
                           PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", str(Path(__file__).resolve()),
             "--old-status", str(source), str(self.root.path), self.now.isoformat()],
            cwd=source, env=environment, capture_output=True, text=True,
            check=False, timeout=30,
        )
        self.assertEqual(0, completed.returncode,
                         "old reader " + OLD_SHA + " did not accept this exact fixture: " + completed.stderr)
        self.assertEqual("", completed.stderr)
        proof = json.loads(completed.stdout)
        self.assertEqual(str(source.resolve()), proof["source"])
        self.assertEqual({"sender", "affected", "healthy"},
                         {row["node_id"] for row in proof["artifact"]["nodes"]})
        self.assertEqual(before, ledger_bytes(self.root))
        print(json.dumps({"legacy_reader_sha": OLD_SHA, "old_status_exit": completed.returncode,
                          "fixture_root": str(self.root.path), "ledger_bytes_unchanged": True}, sort_keys=True))

    def _legacy_fixture(self, code):
        affected_message = self._native_consumed_message("affected")
        self._native_consumed_message("healthy")
        # These schema-valid v0 receipts represent historical cross-ledger
        # gaps the prior status reader counted without validating references.
        # Native consumption is already valid before this older row appears.
        receipt = {
            "schema_version": 0, "tenant_id": self.root.tenant_id,
            "timestamp": self.now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "recipient": "affected", "item_ids": ["msg-" + uuid7_hex()],
        }
        if code == "ack_evidence_invalid":
            receipt.update(id="ack-" + uuid7_hex(), kind="ack_receipt")
            path = "receipts/acks/affected.jsonl"
        else:
            receipt.update(id="delivery-" + uuid7_hex(), kind="delivery_receipt", presentation_count=2)
            path = "receipts/deliveries/affected.jsonl"
        allowed = ({"ack_receipt"} if code == "ack_evidence_invalid" else
                   {"delivery_receipt", "delivered_unpresented", "wake_hold_receipt"})
        append_record(self.root, path, receipt, allowed_kinds=allowed)
        self._old_status_accepts()
        # Authoring stays strict. Read degradation cannot turn a historical
        # gap into eligibility for a new ACTED receipt.
        with self.assertRaises(IntegrityFailure) as strict:
            self.cursor.state_for("affected", self.events.records())
        self.assertEqual(code, strict.exception.code)
        before = ledger_bytes(self.root)
        with self.assertRaises(IntegrityFailure) as authoring:
            MessageConsumption(self.root).consume(
                "affected", str(affected_message["id"]), "successor-session")
        self.assertEqual(code, authoring.exception.code)
        self.assertEqual(before, ledger_bytes(self.root))
        return receipt

    @staticmethod
    def _row(artifact, node):
        return next(row for row in artifact["nodes"] if row["node_id"] == node)

    def _validate_progress_value(self, version, progress):
        # Field-schema compatibility only. The older full status schemas
        # intentionally do not describe every field in today's status body.
        path = (Path("schemas") / version / "fleet-status-artifact.schema.json").resolve()
        document = json.loads(path.read_text())
        schema = document["$defs"]["node"]["properties"]["message_progress"]
        _validate(progress, schema, document, path, "$.message_progress", _CallCache())

    def _validate_progress_field_schemas(self, evidence):
        for version in ("v0", "v1"):
            for node in evidence["nodes"]:
                self._validate_progress_value(version, node["message_progress"])

    def test_progress_field_schemas_refuse_partial_or_unsafe_unknown(self):
        for version in ("v0", "v1"):
            self._validate_progress_value(version, unknown("ack_evidence_invalid"))
            for changes in ({"delivered": 0}, {"reason_code": "ack_evidence_invalid\n"},
                            {"reason_code": "9invalid"}, {"reason_code": "a" * 129},
                            {"state": "known"}, {"extra": True}):
                with self.subTest(version=version, changes=changes):
                    with self.assertRaises(SchemaValidationError):
                        self._validate_progress_value(version, dict(unknown("ack_evidence_invalid"), **changes))

    def _orphan_plane_fixture(self, code, *, worker_session_id=None):
        receipt = {
            "schema_version": 0, "tenant_id": self.root.tenant_id,
            "timestamp": self.now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "recipient": "affected", "item_ids": ["msg-" + uuid7_hex()],
        }
        if code == "ack_evidence_invalid":
            receipt.update(id="ack-" + uuid7_hex(), kind="ack_receipt")
            relative = self.cursor._relative_path_for("affected", worker_session_id=worker_session_id)
            allowed = {"ack_receipt"}
        else:
            receipt.update(id="delivery-" + uuid7_hex(), kind="delivery_receipt", presentation_count=1)
            relative = self.cursor._delivery_relative_path_for("affected", worker_session_id=worker_session_id)
            allowed = {"delivery_receipt", "delivered_unpresented", "wake_hold_receipt"}
        append_record(self.root, relative, receipt, allowed_kinds=allowed)
        self._old_status_accepts()
        with self.assertRaises(IntegrityFailure) as strict:
            self.cursor.state_for("affected", self.events.records(), worker_session_id=worker_session_id)
        self.assertEqual(code, strict.exception.code)

    def _warm_exact_candidate(self, *, board=False):
        cls = type(self)
        if cls.old_candidate_source is None:
            cls.old_candidate_source = cls._extract_reader(CACHE_CANDIDATE_SHA, "cache-candidate")
        before = ledger_bytes(self.root)
        environment = dict(os.environ)
        environment.update(HOME=str(self.old_home), TMPDIR=str(self.old_temp),
                           PYTHONDONTWRITEBYTECODE="1")
        argv = ["/usr/bin/python3", "-I", "-B", str(Path(__file__).resolve()),
                "--old-status", str(cls.old_candidate_source), str(self.root.path), self.now.isoformat()]
        if board:
            argv.append("warm-board")
        completed = subprocess.run(argv, cwd=cls.old_candidate_source, env=environment,
                                   capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        proof = json.loads(completed.stdout)
        self.assertEqual(str(cls.old_candidate_source.resolve()), proof["source"])
        numeric_zero = {"delivered": 0, "acknowledged": 0, "consumed": 0}
        self.assertEqual(numeric_zero, self._row(proof["artifact"], "affected")["message_progress"])
        self.assertTrue(any(Path(path).name.startswith("status-") for path in proof["cache_files"]))
        if board:
            self.assertEqual(numeric_zero, self._row({"nodes": proof["board_nodes"]}, "affected")["message_progress"])
            self.assertTrue(any(Path(path).name.startswith("board-") for path in proof["cache_files"]))
        self.assertEqual(before, ledger_bytes(self.root))
        print(json.dumps({"cache_candidate_sha": CACHE_CANDIDATE_SHA,
                          "candidate_status_progress": numeric_zero, "candidate_board_warmed": board,
                          "observation_time": self.now.isoformat(), "ledger_bytes_unchanged": True}, sort_keys=True))

    def test_exact_old_candidate_status_cache_cannot_reuse_numeric_zero_for_orphan_ack(self):
        require_harbor_history(self, REPOSITORY, OLD_SHA, CACHE_CANDIDATE_SHA)
        code = "ack_evidence_invalid"
        self._orphan_plane_fixture(code)
        cold = FleetProjection(self.root).snapshot(self.now)
        self.assertEqual(unknown(code), self._row(cold, "affected")["message_progress"])
        self.assertEqual({"delivered": 0, "acknowledged": 0, "consumed": 0},
                         self._row(cold, "healthy")["message_progress"])
        self._warm_exact_candidate()
        before = ledger_bytes(self.root)
        try:
            warm = FleetProjection(self.root).status_artifact(self.now)
            self.assertEqual(unknown(code), self._row(warm, "affected")["message_progress"])
        finally:
            self.assertEqual(before, ledger_bytes(self.root))

    def test_exact_old_candidate_harbor_cache_cannot_reuse_numeric_zero_for_orphan_worker(self):
        require_harbor_history(self, REPOSITORY, OLD_SHA, CACHE_CANDIDATE_SHA)
        code = "delivery_evidence_invalid"
        self._orphan_plane_fixture(code, worker_session_id="orphan-worker")
        cold = FleetProjection(self.root).snapshot(self.now)
        self.assertEqual(unknown(code), self._row(cold, "affected")["message_progress"])
        self.assertEqual({"delivered": 0, "acknowledged": 0, "consumed": 0},
                         self._row(cold, "healthy")["message_progress"])
        self._warm_exact_candidate(board=True)
        before = ledger_bytes(self.root)
        try:
            model = model_from_root(self.root, self.now)
            affected = next(row for row in model.nodes if row["node_id"] == "affected")
            self.assertEqual(unknown(code), affected["message_progress"])
        finally:
            self.assertEqual(before, ledger_bytes(self.root))

    def _assert_orphan_observation_unknown(self, code):
        before = ledger_bytes(self.root)
        self.assertEqual(unknown(code), MessageConsumption(self.root).progress("affected"))
        for sealed in (False, True):
            self.assertEqual(unknown(code), MessageConsumption(self.root).progress(
                "affected", item_ids=[], sealed=sealed))
        artifact = FleetProjection(self.root).status_artifact(self.now)
        self.assertEqual(unknown(code), self._row(artifact, "affected")["message_progress"])
        self.assertEqual({"delivered": 0, "acknowledged": 0, "consumed": 0},
                         self._row(artifact, "healthy")["message_progress"])
        self.assertEqual(before, ledger_bytes(self.root))

    def test_orphan_plain_ack_without_any_envelopes_is_unknown(self):
        require_harbor_history(self, REPOSITORY, OLD_SHA)
        self._orphan_plane_fixture("ack_evidence_invalid")
        self.assertEqual([], self.events.records())
        self._assert_orphan_observation_unknown("ack_evidence_invalid")

    def test_orphan_plain_delivery_without_any_envelopes_is_unknown(self):
        require_harbor_history(self, REPOSITORY, OLD_SHA)
        self._orphan_plane_fixture("delivery_evidence_invalid")
        self.assertEqual([], self.events.records())
        self._assert_orphan_observation_unknown("delivery_evidence_invalid")

    def test_orphan_worker_plane_without_any_envelopes_is_unknown(self):
        require_harbor_history(self, REPOSITORY, OLD_SHA)
        for code in REASONS:
            with self.subTest(reason=code):
                if code != REASONS[0]:
                    self.setUp()
                self._orphan_plane_fixture(code, worker_session_id="orphan-worker")
                self.assertEqual([], self.events.records())
                self._assert_orphan_observation_unknown(code)

    def test_orphan_worker_plane_beside_a_valid_plain_envelope_is_unknown(self):
        require_harbor_history(self, REPOSITORY, OLD_SHA)
        for code in REASONS:
            with self.subTest(reason=code):
                if code != REASONS[0]:
                    self.setUp()
                message = self._native_consumed_message("affected")
                self._orphan_plane_fixture(code, worker_session_id="orphan-worker")
                self.assertEqual([message["id"]], [row["id"] for row in self.events.records()])
                self._assert_orphan_observation_unknown(code)

    def test_legacy_ack_gap_degrades_only_affected_status_node(self):
        require_harbor_history(self, REPOSITORY, OLD_SHA)
        self._legacy_fixture("ack_evidence_invalid")
        before = ledger_bytes(self.root)
        artifact = FleetProjection(self.root).status_artifact(self.now)
        self.assertEqual(unknown("ack_evidence_invalid"), self._row(artifact, "affected")["message_progress"])
        self.assertEqual({"delivered": 1, "acknowledged": 1, "consumed": 1},
                         self._row(artifact, "healthy")["message_progress"])
        self._validate_progress_field_schemas(artifact)
        self.assertEqual(before, ledger_bytes(self.root))

    def test_legacy_delivery_gap_degrades_only_affected_status_node(self):
        require_harbor_history(self, REPOSITORY, OLD_SHA)
        self._legacy_fixture("delivery_evidence_invalid")
        before = ledger_bytes(self.root)
        artifact = FleetProjection(self.root).status_artifact(self.now)
        self.assertEqual(unknown("delivery_evidence_invalid"), self._row(artifact, "affected")["message_progress"])
        self.assertEqual({"delivered": 1, "acknowledged": 1, "consumed": 1},
                         self._row(artifact, "healthy")["message_progress"])
        self._validate_progress_field_schemas(artifact)
        self.assertEqual(before, ledger_bytes(self.root))

    def test_receipts_preserve_legacy_rows_and_name_unknown_consumption(self):
        require_harbor_history(self, REPOSITORY, OLD_SHA)
        for code in REASONS:
            with self.subTest(reason=code):
                # A fresh root avoids combining two independent refusal causes.
                if code != REASONS[0]:
                    self.setUp()
                legacy = self._legacy_fixture(code)
                before = ledger_bytes(self.root)
                history = FleetProjection(self.root).receipts("affected")
                self.assertEqual(unknown(code), history["message_progress"])
                self.assertEqual([], history["consumptions"])
                key = "acks" if code == "ack_evidence_invalid" else "deliveries"
                self.assertIn(legacy, history[key])
                self.assertEqual(1, len(FleetProjection(self.root).receipts("healthy")["consumptions"]))
                self.assertEqual(before, ledger_bytes(self.root))

    def test_harbor_renders_unknown_reason_and_healthy_neighbor(self):
        require_harbor_history(self, REPOSITORY, OLD_SHA)
        for code in REASONS:
            with self.subTest(reason=code):
                if code != REASONS[0]:
                    self.setUp()
                self._legacy_fixture(code)
                before = ledger_bytes(self.root)
                model = model_from_root(self.root, self.now)
                affected = next(row for row in model.nodes if row["node_id"] == "affected")
                self.assertEqual(unknown(code), affected["message_progress"])
                text = render_plain_dump(model, width=200)
                self.assertIn("UNKNOWN", text)
                self.assertIn(code, text)
                self.assertIn("D/A/C 1/1/1", text)
                self.assertEqual(before, ledger_bytes(self.root))


class BoardingProgressFailureTests(unittest.TestCase):
    write_map = board_fixture.SeatBoardTests.write_map
    send = board_fixture.SeatBoardTests.send
    arm_session = board_fixture.SeatBoardTests.arm_session
    board = board_fixture.SeatBoardTests.board

    def setUp(self):
        board_fixture.SeatBoardTests.setUp(self)

    def test_post_ack_progress_reader_failure_cannot_poison_completed_board(self):
        message = self.send("post-ack-reader-failure")
        with mock.patch.object(
            MessageConsumption, "progress",
            side_effect=IntegrityFailure("ack_evidence_invalid", "legacy cross-ledger gap"),
        ):
            result = self.board()
        self.assertEqual("completed", result["step"])
        self.assertEqual([message["id"]], result["item_ids"])
        self.assertEqual(unknown("ack_evidence_invalid"), result["message_progress"])
        self.assertIn(message["id"], SparseCursor(self.root).acked_ids(ids.builder("floati")))
        before = ledger_bytes(self.root)
        self.assertEqual(result, self.board())
        self.assertEqual(before, ledger_bytes(self.root))


if __name__ == "__main__":
    unittest.main()
