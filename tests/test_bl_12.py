"""BL-12: one turnover verb, documented in one table.

The ruling pins the shape: `/boardbus` stays THE per-thread command and, on a
contested claim, prints the ruled line naming the claimant thread and its
last-bound time; `/takeover` is the operator's word and runs the gateway's
`takeover` operation (`seat board … --take-over`, never added silently); the
four-row table ships in `docs/AGENT-OPERATIONS.md` and in the `/boardbus`
prompt's own text.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from floati.codex_hook_trust import observe_codex_waiter_hooks
from tests import test_bl_11, test_seat_board
from tests.test_bl_11 import BANNER, fixed_transport_interpreter
from tests.private_artifacts import require_private_artifact

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

CONTESTED_LINE_ANCHOR = "Another thread still holds this seat"
TABLE_ROWS = (
    "first time on a seat",
    "a new thread on the same seat, the old thread CLOSED",
    "a new thread while the old thread is STILL OPEN",
    "the Stop hook untrusted",
)


class TurnoverOperationTests(test_seat_board._SeatBoardFixture, unittest.TestCase):
    def test_the_gateway_derives_seat_board_take_over_for_the_takeover_operation(self):
        require_private_artifact(self, "tools/codex/codex-fleet-bus.py")
        gateway = load_gateway()
        profile = {
            "identity": "builder-a",
            "bus_root": Path("\x2fprivate/tmp/fleet"),
            "workspace_root": Path("\x2fprivate/tmp/workspace"),
            "allowed_operations": ["takeover"],
        }
        _, operation, values = gateway._parse_arguments(
            ["gateway", "fixture", "takeover"], {"fixture": profile}
        )
        self.assertEqual("takeover", operation)
        with mock.patch.dict(
            gateway.os.environ, {"CODEX_THREAD_ID": "session-takeover"}, clear=True
        ):
            module, argv = gateway._derived_invocation(
                operation, values, profile, Path("\x2fprivate/tmp/staged")
            )
        self.assertEqual("floati", module)
        self.assertEqual(("seat", "board"), tuple(argv[:2]))
        self.assertIn("--take-over", argv)


class TurnoverStopHookGuardTests(test_bl_11.StopHookBoardGuardTests):
    """BL-11's law holds every path that can board, the turnover verb included.

    A boarding path is any gateway operation whose derived client invocation
    is ``seat board``. The fence derives the boarding set from the gateway's
    own operation surface instead of pinning the operations the ruling
    named, so a future boarding operation is held by the same instrument.
    The verb's trust guard is not an expression anyone maintains: an
    operation boards, or it does not, and the guard measures the same Stop
    hook for both. The trusted control proves the guard blocks nothing a
    granted trust should board.
    """

    BOARD_SESSION = "019f7000-0000-7000-8000-00000000000a"

    def _dispatch(self, operation: str, allowed: list):
        """Drive one operation through the real dispatch; return the exit
        status, the printed artifact, and every client invocation reached."""

        gateway = self.gateway
        profile = {
            "transport": "fixture",
            "identity": "worker",
            "workspace_root": self.workspace,
            "git_common_dir": self.workspace / ".git",
            "bus_root": self.bus_root,
            "allowed_operations": allowed,
        }
        transport = {
            "source_sha": "0" * 40,
            "install_root": self.base / "installed",
            "interpreter": fixed_transport_interpreter(),
        }
        calls: list = []

        def run_client(interpreter, private_root, module, arguments):
            calls.append((module, tuple(arguments)))
            return 0

        repo = Path(__file__).resolve().parents[1]

        def stage_real_reader(entries, root):
            shutil.copytree(repo / "floati", Path(root) / "floati", dirs_exist_ok=True)

        output = io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(gateway, "_validate_wrapper_invocation"))
        stack.enter_context(mock.patch.object(gateway, "_has_git_redirection", return_value=False))
        stack.enter_context(
            mock.patch.object(
                gateway,
                "load_registry",
                return_value=({"fixture": transport}, {"fixture": profile}),
            )
        )
        stack.enter_context(
            mock.patch.object(
                gateway,
                "repository_identity",
                return_value=(self.workspace, self.workspace / ".git", self.workspace / ".git"),
            )
        )
        stack.enter_context(mock.patch.object(gateway, "_is_primary_or_linked", return_value=True))
        stack.enter_context(
            mock.patch.object(gateway, "_validate_profile_workspace", return_value=self.workspace)
        )
        stack.enter_context(mock.patch.object(gateway, "_reader_currency_observation", return_value=None))
        stack.enter_context(mock.patch.object(gateway, "_host_path", side_effect=self.host_path))
        stack.enter_context(mock.patch.object(gateway, "_manifest_entries", return_value=[]))
        stack.enter_context(mock.patch.object(gateway, "_stage", side_effect=stage_real_reader))
        stack.enter_context(mock.patch.object(gateway, "_run_client", side_effect=run_client))
        with stack:
            environment = dict(os.environ, CODEX_THREAD_ID=self.BOARD_SESSION)
            with mock.patch.dict(os.environ, environment, clear=False):
                with contextlib.redirect_stdout(output):
                    status = gateway.main(["gateway", "fixture", operation])
        return status, output.getvalue(), calls

    def _grant_trust(self) -> None:
        rows = observe_codex_waiter_hooks(self.hooks_path)
        key = rows[0]["hook_trust_key"]
        state_key = json.dumps(key, ensure_ascii=False)
        (self.codex / "config.toml").write_text(
            f"[hooks.state.{state_key}]\nenabled = true\n"
            f'trusted_hash = "sha256:{rows[0]["hook_trust_current_hash"]}"\n',
            encoding="utf-8",
        )

    def test_takeover_stops_before_seat_board_when_stop_hook_is_untrusted(self):
        status, output, calls = self._dispatch("takeover", ["attach", "board", "takeover"])
        self.assertEqual(
            [],
            [argv for _, argv in calls if argv[:2] == ("seat", "board")],
            "the takeover path boarded past the untrusted-Stop-hook guard;"
            " no board may proceed while the hook is not armed",
        )
        self.assertEqual(20, status)
        artifact = json.loads(output)
        self.assertEqual("refused", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual("stop_hook_untrusted", evidence["code"])
        self.assertEqual(BANNER, evidence["banner"])
        self.assertEqual(str(self.hooks_path) + ":stop:3:0", evidence["hook_trust_key"])

    def test_takeover_proceeds_when_the_stop_hook_is_trusted(self):
        self._grant_trust()
        status, output, calls = self._dispatch("takeover", ["attach", "board", "takeover"])
        self.assertEqual(0, status)
        self.assertNotIn("stop_hook_untrusted", output)
        boarded = [(module, argv) for module, argv in calls if argv[:2] == ("seat", "board")]
        self.assertEqual(1, len(boarded))
        module, argv = boarded[0]
        self.assertEqual("floati", module)
        self.assertIn("--take-over", argv)

    def test_every_derived_boarding_operation_stops_when_stop_hook_is_untrusted(self):
        gateway = self.gateway
        allowed = sorted(gateway.SUPPORTED_OPERATIONS)
        boarded: list = []
        refused: list = []
        for operation in allowed:
            try:
                status, output, calls = self._dispatch(operation, allowed)
            except gateway.ApprovalRequired:
                # Not invocable as constructed: it derives no boarding run
                # here, and its argv never names seat board.
                continue
            seat_board = [argv for _, argv in calls if argv[:2] == ("seat", "board")]
            if seat_board:
                boarded.append((operation, seat_board))
                continue
            if status == 20 and output.strip():
                artifact = json.loads(output)
                if artifact.get("evidence", {}).get("code") == "stop_hook_untrusted":
                    refused.append(operation)
        self.assertEqual(
            [],
            boarded,
            "an operation that derives a seat board invocation ran past the"
            " untrusted-Stop-hook guard; the law holds every boarding path,"
            " not the ones a list happens to name",
        )
        self.assertTrue(
            {"board", "takeover"} <= set(refused),
            f"the guard refused {refused}; every derived boarding member must"
            " meet the same Stop-hook measurement",
        )


class ContestedClaimEvidenceTests(test_seat_board._SeatBoardFixture, unittest.TestCase):
    def test_the_contested_refusal_names_the_claimant_and_its_last_bound_time(self):
        from floati.errors import ProtocolRefusal

        prior = self.arm_session("prior-session")
        with self.assertRaises(ProtocolRefusal) as caught:
            self.board()
        self.assertEqual("seat_board_claim_contested", caught.exception.code)
        # The refusal carries the measured facts the ruled line renders: the
        # claimant thread and its last-bound time.
        self.assertIn("prior-session", caught.exception.detail)
        self.assertIn(prior["timestamp"], caught.exception.detail)


class TurnoverCopyGateTests(unittest.TestCase):
    def test_boardbus_prompt_carries_the_contested_line_and_the_four_row_table(self):
        require_private_artifact(self, "tools/codex/boardbus.md")
        text = (REPOSITORY_ROOT / "tools" / "codex" / "boardbus.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(CONTESTED_LINE_ANCHOR, text)
        for row in TABLE_ROWS:
            self.assertIn(row, text)
        self.assertIn("/takeover", text)

    def test_agent_operations_manual_carries_the_four_row_table(self):
        text = (REPOSITORY_ROOT / "docs" / "AGENT-OPERATIONS.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(CONTESTED_LINE_ANCHOR, text)
        for row in TABLE_ROWS:
            self.assertIn(row, text)
        self.assertIn("/takeover", text)

    def test_the_takeover_prompt_asset_exists_and_is_pinned(self):
        require_private_artifact(
            self, "tools/codex/takeover.md", "tools/codex/takeover.sha256.json"
        )
        asset = REPOSITORY_ROOT / "tools" / "codex" / "takeover.md"
        self.assertTrue(asset.is_file(), "the /takeover prompt asset is missing")
        text = asset.read_text(encoding="utf-8")
        self.assertIn("takeover", text)
        self.assertIn("--take-over", text)
        digest_record = REPOSITORY_ROOT / "tools" / "codex" / "takeover.sha256.json"
        self.assertTrue(digest_record.is_file(), "the takeover digest record is missing")
        import hashlib

        record = json.loads(digest_record.read_text(encoding="utf-8"))
        self.assertEqual(
            hashlib.sha256(asset.read_bytes()).hexdigest(), record["sha256"]
        )

    def test_the_installer_ships_the_takeover_asset(self):
        require_private_artifact(self, "tools/codex/install_codex_gateway.py")
        source = (REPOSITORY_ROOT / "tools" / "codex" / "install_codex_gateway.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("takeover.md", source)


def load_gateway():
    import importlib.util

    vendored = REPOSITORY_ROOT / "tools" / "codex" / "codex-fleet-bus.py"
    spec = importlib.util.spec_from_file_location("codex_fleet_bus", vendored)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
