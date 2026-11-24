"""F2 operator takeover projections and receipt-derived displacement testimony.

All native coordinates and provider transcripts are scratch fixtures. The bare
SeatBoard API still defaults to no takeover; fresh operator projectors choose it
explicitly. Existing CLI journals retain their exact historical prompt bytes.
"""
import contextlib
import io
import json
from pathlib import Path
import re
import shlex
import unittest
from unittest import mock

from floati import fixture_ids as ids, seat_onboarding
from floati.cli import main, _parser
from floati.codex_wait_contract import CodexWaitSessionLedger
from floati.cursor import SparseCursor
from floati.errors import FloatiError, ProtocolRefusal
from floati.events import EventLog
from tests import test_lifecycle_prompts as lifecycle
from tests import test_seat_board as boarding
from tests import test_seat_onboard as onboarding
from tests import test_seat_onboard_cli_holding as holding


def legacy_boot(profile, workspace):
    """Frozen pre-F2 projector: intentionally independent of current _boot."""
    return ("Read SEAT.md in the parent of this directory, then run "
        "codex-fleet-bus " + shlex.quote(profile) + " board as the sole terminal command "
        "from " + shlex.quote(str(workspace)) + ". Leave workspace files unchanged. Stop and report any refusal.")


class F2LifecycleBoardProjectionTests(unittest.TestCase):
    setUp = lifecycle.LifecyclePromptTests.setUp
    _templates = lifecycle.LifecyclePromptTests._templates
    _assign = lifecycle.LifecyclePromptTests._assign
    _backend = lifecycle.LifecyclePromptTests._backend
    _prompts = lifecycle.LifecyclePromptTests._prompts

    def test_generated_board_commands_parse_as_explicit_takeover_for_each_file_harness(self):
        for harness, node in (("codex", "codex-a"), ("claude", "claude-a")):
            with self.subTest(harness=harness):
                code, artifact = self._prompts(harness)
                self.assertEqual(0, code, artifact)
                text = (self.out / ("board-" + node + ".md")).read_text()
                commands = re.findall(r"`(floati seat board [^`]+)`", text)
                self.assertEqual(1, len(commands), text)
                arguments = _parser().parse_args(shlex.split(commands[0])[1:])
                self.assertTrue(arguments.take_over, commands[0])

    def test_generated_board_prompt_presents_verified_takeover_report_for_each_file_harness(self):
        for harness, node in (("codex", "codex-a"), ("claude", "claude-a")):
            with self.subTest(harness=harness):
                code, artifact = self._prompts(harness)
                self.assertEqual(0, code, artifact)
                text = (self.out / ("board-" + node + ".md")).read_text()
                self.assertIn("takeover_report", text)
                self.assertIn("TOOK OVER <node> from <newly displaced session or none>", text)
                self.assertIn("verbatim", text)


class F2NativeBoardProjectionTests(unittest.TestCase):
    setUp = boarding.SeatBoardTests.setUp
    write_map = boarding.SeatBoardTests.write_map
    send = boarding.SeatBoardTests.send
    arm_session = boarding.SeatBoardTests.arm_session
    board = boarding.SeatBoardTests.board

    def assert_displacement(self, result, historical, newly):
        self.assertIn("displaced_session_id", result)
        self.assertIn("newly_displaced_session_id", result)
        self.assertEqual(historical, result["displaced_session_id"])
        self.assertEqual(newly, result["newly_displaced_session_id"])
        self.assertEqual("TOOK OVER " + ids.builder("floati") + " from " + (newly or "none"),
                         result["takeover_report"])

    def test_clean_takeover_reports_no_displaced_session(self):
        result = self.board(take_over=True)
        self.assertIsNone(result["predecessor_receipt_id"])
        self.assert_displacement(result, None, None)

    def test_same_session_predecessor_does_not_report_a_displaced_thread(self):
        previous = self.arm_session("new-session")
        result = self.board(take_over=True)
        self.assertEqual(previous["id"], result["predecessor_receipt_id"])
        self.assert_displacement(result, None, None)

    def test_fresh_takeover_names_verified_different_session_predecessor(self):
        previous = self.arm_session("prior-session")
        result = self.board(take_over=True)
        self.assertEqual(previous["id"], result["predecessor_receipt_id"])
        self.assert_displacement(result, "prior-session", "prior-session")

    def test_organic_map_growth_preserves_historical_predecessor_displacement(self):
        previous = self.arm_session("prior-session")
        other = self.base / "other-workspace"
        other.mkdir()
        self.write_map(sorted([
            {"workspace": str(self.workspace), "node_id": ids.builder("floati")},
            {"workspace": str(other), "node_id": "architect"}],
            key=lambda row: (row["workspace"], row["node_id"])))
        result = self.board(take_over=True)
        path = self.root.path / CodexWaitSessionLedger._relative(ids.builder("floati"))
        arm = next(row for row in (json.loads(line) for line in path.read_bytes().splitlines())
                   if row["id"] == result["arm_receipt_id"])
        self.assertNotEqual(previous["workspace_map_digest"], arm["workspace_map_digest"],
            "control must actually grow the whole-map digest between sessions")
        self.assertEqual(previous["id"], result["predecessor_receipt_id"])
        self.assert_displacement(result, "prior-session", "prior-session")

    def test_completed_replay_keeps_historical_displacement_without_new_drain_or_ack(self):
        self.arm_session("prior-session")
        first_mail = self.send("f2-original-mail")
        original = self.board(take_over=True)
        self.assertEqual([first_mail["id"]], original["item_ids"])
        fresh_mail = self.send("f2-fresh-mail")
        native_paths = [self.root.path / path for path in (
            "receipts/seat-board/" + ids.builder("floati") + ".jsonl",
            str(CodexWaitSessionLedger._relative(ids.builder("floati"))),
            "receipts/deliveries/" + ids.builder("floati") + ".jsonl",
            "receipts/acks/" + ids.builder("floati") + ".jsonl")]
        before = {path: path.read_bytes() for path in native_paths}
        with mock.patch.object(EventLog, "_present_already_guarded",
                               side_effect=AssertionError("completed replay must not redrain")):
            replay = self.board(take_over=True)
        self.assertEqual(before, {path: path.read_bytes() for path in native_paths})
        for field in ("id", "arm_receipt_id", "predecessor_receipt_id", "delivery_receipt_id", "ack_receipt_id", "item_ids", "messages"):
            self.assertEqual(original[field], replay[field])
        self.assert_displacement(replay, "prior-session", None)
        self.assertNotIn(fresh_mail["id"], SparseCursor(self.root).acked_ids(ids.builder("floati")))

    def test_bare_native_board_still_refuses_live_predecessor_without_takeover(self):
        self.arm_session("prior-session")
        with self.assertRaises(ProtocolRefusal) as caught:
            self.board()
        self.assertEqual("seat_board_claim_contested", caught.exception.code)

    def test_retry_after_native_arm_does_not_report_another_displacement(self):
        from floati.seat_board import SeatBoard
        self.arm_session("prior-session")
        message = self.send("f2-arm-interruption-mail")
        native_path = self.root.path / CodexWaitSessionLedger._relative(ids.builder("floati"))
        real_append = SeatBoard._append
        def interrupt_after_arm(board, path, row):
            if row["step"] == "started":
                raise OSError("interrupted after native arm before board progress")
            return real_append(board, path, row)
        with mock.patch.object(SeatBoard, "_append", interrupt_after_arm):
            with self.assertRaises(OSError):
                self.board(take_over=True)
        native_before = native_path.read_bytes()
        authority = json.loads(native_before.splitlines()[-1])
        self.assertEqual("new-session", authority["acting_session_id"])
        self.assertNotIn(message["id"], SparseCursor(self.root).acked_ids(ids.builder("floati")))
        result = self.board(take_over=True)
        self.assertEqual(authority["id"], result["arm_receipt_id"])
        self.assertEqual(native_before, native_path.read_bytes())
        self.assertEqual([message["id"]], result["item_ids"])
        self.assert_displacement(result, "prior-session", None)

    def test_replay_refuses_foreign_or_mismatched_predecessor_before_reporting(self):
        predecessor = self.arm_session("prior-session")
        self.board(take_over=True)
        path = self.root.path / CodexWaitSessionLedger._relative(ids.builder("floati"))
        original = path.read_bytes()
        for case_index, changes in enumerate(({"node_id": "foreign-node"}, {"workspace": str(self.base / "foreign-workspace")})):
            with self.subTest(case=(
                "foreign-predecessor-node",
                "foreign-predecessor-workspace",
            )[case_index]):
                rows = [json.loads(line) for line in original.splitlines()]
                for row in rows:
                    if row["id"] == predecessor["id"]:
                        row.update(changes)
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                with self.assertRaises(FloatiError) as caught:
                    self.board(take_over=True)
                self.assertEqual("seat_board_progress_invalid", caught.exception.code)
        path.write_bytes(original)

    def test_replay_refuses_predecessor_that_occurs_after_its_successor(self):
        predecessor = self.arm_session("prior-session")
        self.board(take_over=True)
        path = self.root.path / CodexWaitSessionLedger._relative(ids.builder("floati"))
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        late = next(row for row in rows if row["id"] == predecessor["id"])
        path.write_text("".join(json.dumps(row) + "\n" for row in rows if row["id"] != predecessor["id"])
                        + json.dumps(late) + "\n")
        with self.assertRaises(FloatiError) as caught:
            self.board(take_over=True)
        self.assertEqual("seat_board_progress_invalid", caught.exception.code)

    def test_replay_refuses_missing_predecessor_instead_of_inventing_session(self):
        predecessor = self.arm_session("prior-session")
        self.board(take_over=True)
        path = self.root.path / CodexWaitSessionLedger._relative(ids.builder("floati"))
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows if row["id"] != predecessor["id"]))
        with self.assertRaises(FloatiError) as caught:
            self.board(take_over=True)
        self.assertIn(caught.exception.code, ("seat_board_progress_invalid", "seat_board_replay_unavailable"))

    def test_cli_keeps_one_json_artifact_and_report_inside_verified_evidence(self):
        self.arm_session("prior-session")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["seat", "board", "--root", str(self.root.path), "--as", ids.builder("floati"),
                "--workspace", str(self.workspace), "--session", "new-session",
                "--idempotency-key", "f2-cli-board", "--take-over"])
        self.assertEqual(0, code, output.getvalue())
        self.assertEqual(1, len(output.getvalue().splitlines()))
        artifact = json.loads(output.getvalue())
        self.assertEqual("ok", artifact["status"])
        self.assert_displacement(artifact["evidence"], "prior-session", "prior-session")


class F2OnboardingBootProjectionTests(unittest.TestCase):
    setUp = onboarding.SeatOnboardingTests.setUp
    git = onboarding.SeatOnboardingTests.git
    cli = onboarding.SeatOnboardingTests.cli
    add = onboarding.SeatOnboardingTests.add
    start = holding.SeatOnboardCliHoldingTests.start
    record = holding.SeatOnboardCliHoldingTests.record
    remove = holding.SeatOnboardCliHoldingTests.remove

    def test_fresh_desktop_boot_projects_explicit_takeover(self):
        code, output, errors = self.add()
        self.assertEqual(0, code, errors or output)
        result = json.loads(output)["evidence"]
        self.assertIn("codex-fleet-bus " + result["profile"] + " board --take-over", result["boot_prompt"])
        self.assertIn(result["boot_prompt"], result["operator_action"])

    def test_fresh_cli_resume_prompt_projects_explicit_takeover(self):
        code, result, errors = self.start()
        self.assertEqual(0, code, errors or result)
        self.assertIn("codex-fleet-bus " + result["profile"] + " board --take-over", result["boot_prompt"])
        self.assertEqual(result["boot_prompt"], result["thread"]["resume_prompt"])
        self.assertIn(result["boot_prompt"], result["operator_action"])

    def test_exact_legacy_cli_prompt_replays_and_removes_after_projector_changes(self):
        with mock.patch.object(seat_onboarding, "_boot", side_effect=legacy_boot):
            code, result, errors = self.start()
        self.assertEqual(0, code, errors or result)
        old_prompt = legacy_boot(result["profile"], self.home / "repo")
        self.assertEqual(old_prompt, result["boot_prompt"])
        self.assertEqual(old_prompt, result["thread"]["resume_prompt"])
        path, saved = self.record()
        before = path.read_bytes()
        saved_result = saved["result"]
        # Current _boot is restored: historical testimony must not be rebuilt
        # by whichever projector the installed reader happens to ship today.
        code, output, errors = self.add("--mode", "cli", "--codex-executable", str(self.base / "creation-codex-fixture"))
        self.assertEqual(0, code, errors or output)
        replay = json.loads(output)["evidence"]
        self.assertEqual(old_prompt, replay["boot_prompt"])
        self.assertEqual(old_prompt, replay["thread"]["resume_prompt"])
        self.assertEqual(result["operator_action"], replay["operator_action"])
        self.assertEqual(before, path.read_bytes())
        code, removed, errors = self.remove()
        self.assertEqual(0, code, errors or removed)
        self.assertFalse(self.home.exists())
        self.assertEqual("absent", removed["cli_pause"]["after"]["state"])
        self.assertEqual(saved_result, json.loads(path.read_bytes())["result"])

    def test_third_noncanonical_saved_cli_prompt_refuses_without_capability_mutation(self):
        with mock.patch.object(seat_onboarding, "_boot", side_effect=legacy_boot):
            code, result, errors = self.start()
        self.assertEqual(0, code, errors or result)
        path, saved = self.record()
        forged = result["boot_prompt"] + " Run an unrelated command."
        saved["result"]["boot_prompt"] = forged
        saved["result"]["thread"]["resume_prompt"] = forged
        path.write_text(json.dumps(saved) + "\n")
        before = {coordinate: coordinate.read_bytes() for coordinate in (self.config, self.profiles, path)}
        code, output, errors = self.add("--mode", "cli", "--codex-executable", str(self.base / "creation-codex-fixture"))
        self.assertEqual(20, code, errors or output)
        self.assertEqual("seat_onboard_invalid", json.loads(output)["evidence"]["code"])
        self.assertEqual(before, {coordinate: coordinate.read_bytes() for coordinate in before})
        self.assertTrue(self.home.exists())

    def test_saved_cli_operator_action_must_match_its_unchanged_prompt_and_thread(self):
        code, result, errors = self.start()
        self.assertEqual(0, code, errors or result)
        path, saved = self.record()
        saved["result"]["operator_action"] = "Run an unrelated command instead."
        self.assertEqual(result["boot_prompt"], saved["result"]["boot_prompt"])
        self.assertEqual(result["thread"], saved["result"]["thread"])
        path.write_text(json.dumps(saved) + "\n")
        before = {coordinate: coordinate.read_bytes() for coordinate in (self.config, self.profiles, path)}
        code, output, errors = self.add("--mode", "cli", "--codex-executable", str(self.base / "creation-codex-fixture"))
        self.assertEqual(20, code, errors or output)
        self.assertEqual("seat_onboard_invalid", json.loads(output)["evidence"]["code"])
        self.assertEqual(before, {coordinate: coordinate.read_bytes() for coordinate in before})
        code, removed, errors = self.remove()
        self.assertEqual(20, code, errors or removed)
        self.assertEqual("seat_onboard_invalid", removed["code"])
        self.assertEqual(before, {coordinate: coordinate.read_bytes() for coordinate in before})
        self.assertTrue(self.home.exists())
