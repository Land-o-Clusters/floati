from __future__ import annotations

import hashlib
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.errors import ProtocolRefusal


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _request(request_id: str = "approval-request-1"):
    from floati.tui_approval import ApprovalOption, ApprovalPanelRequest

    return ApprovalPanelRequest(
        request_id=request_id,
        record={
            "kind": "effect_intent",
            "id": "effect-intent-1",
            "effect_type": "git_remote_ref_update",
            "target": "refs/heads/main",
            "request_digest": "a" * 64,
        },
        options=(
            ApprovalOption("refuse-once", "Refuse once", "request"),
            ApprovalOption("allow-once", "Allow once", "request"),
            ApprovalOption(
                "allow-session", "Allow for this session", "session"
            ),
        ),
    )


class RegattaR4ApprovalPanelTests(unittest.TestCase):
    def test_fresh_panel_always_preselects_refuse_once(self) -> None:
        """Catches a prior broad grant becoming a sticky dangerous default."""
        from floati.tui_approval import ApprovalPanelController

        controller = ApprovalPanelController(session_id="session-a")
        controller.enqueue(_request())
        self.assertEqual("refuse-once", controller.focused_option_id)

        controller.handle_key("3")
        broad = controller.handle_key("ENTER")
        self.assertEqual("allow-session", broad.receipt["option_id"])
        controller.enqueue(_request("approval-request-2"))

        self.assertEqual("refuse-once", controller.focused_option_id)

    def test_escape_is_one_key_but_grants_require_focus_then_enter(self) -> None:
        """Catches digits resolving or refusal becoming slower than a grant."""
        from floati.tui_approval import ApprovalPanelController

        controller = ApprovalPanelController(session_id="session-a")
        controller.enqueue(_request())
        controller.handle_key("3")
        self.assertEqual("allow-session", controller.focused_option_id)
        self.assertIsNone(controller.handle_key("3").receipt)
        self.assertEqual([], controller.resolved_receipts)

        grant = controller.handle_key("ENTER")
        self.assertEqual("allow-session", grant.receipt["option_id"])

        controller.enqueue(_request("approval-request-2"))
        controller.handle_key("2")
        refusal = controller.handle_key("ESC")
        self.assertEqual("refuse-once", refusal.receipt["option_id"])

    def test_mouse_requires_same_narrow_target_twice_and_never_commits_broad(self) -> None:
        """Catches single-click or pointer-only broad approval."""
        from floati.tui_approval import ApprovalPanelController

        controller = ApprovalPanelController(session_id="session-a")
        controller.enqueue(_request())
        first = controller.handle_mouse("allow-once", now_ms=100)
        self.assertEqual("focused", first.kind)
        self.assertIsNone(first.receipt)
        narrow = controller.handle_mouse("allow-once", now_ms=350)
        self.assertEqual("allow-once", narrow.receipt["option_id"])

        controller.enqueue(_request("approval-request-2"))
        controller.handle_mouse("allow-session", now_ms=500)
        broad = controller.handle_mouse("allow-session", now_ms=650)
        self.assertEqual("refused", broad.kind)
        self.assertEqual("approval_mouse_broad_forbidden", broad.note_code)
        self.assertIsNone(broad.receipt)
        self.assertEqual("approval-request-2", controller.active_request_id)

    def test_receipt_binds_rendered_record_and_stale_panel_refuses(self) -> None:
        """Catches approval of record bytes other than those the human saw."""
        from floati.tui_approval import ApprovalPanelController

        appended = []
        controller = ApprovalPanelController(
            session_id="session-a",
            composer_text="draft command",
            receipt_sink=appended.append,
        )
        request = _request()
        controller.enqueue(request)
        frame = controller.render(width=72, color_tier="mono")
        receipt = controller.handle_key("ESC").receipt
        self.assertEqual(
            hashlib.sha256(frame.record_body.encode("utf-8")).hexdigest(),
            frame.record_digest,
        )
        self.assertEqual(frame.record_digest, receipt["rendered_record_digest"])
        self.assertEqual("session-a", receipt["acting_session"])
        self.assertEqual([receipt], appended)

        stale = _request("approval-request-2")
        controller.enqueue(stale)
        controller.render(width=72, color_tier="mono")
        stale.record["target"] = "refs/heads/release"
        with self.assertRaises(ProtocolRefusal) as caught:
            controller.handle_key("ESC")
        self.assertEqual("approval_panel_stale", caught.exception.code)
        self.assertEqual("approval-request-2", controller.active_request_id)

    def test_fifo_never_replaces_or_refocuses_and_restores_composer(self) -> None:
        """Catches concurrent arrival replacing the record under decision."""
        from floati.tui_approval import ApprovalPanelController

        controller = ApprovalPanelController(
            session_id="session-a", composer_text="draft command"
        )
        controller.enqueue(_request())
        controller.handle_key("2")
        first_frame = controller.render(width=72, color_tier="mono")
        controller.enqueue(_request("approval-request-2"))

        self.assertEqual("approval-request-1", controller.active_request_id)
        self.assertEqual("allow-once", controller.focused_option_id)
        self.assertEqual(first_frame, controller.render(width=72, color_tier="mono"))
        self.assertEqual("", controller.composer_text)

        controller.handle_key("ENTER")
        self.assertEqual("approval-request-2", controller.active_request_id)
        self.assertEqual("refuse-once", controller.focused_option_id)
        controller.handle_key("ESC")
        self.assertEqual("draft command", controller.composer_text)

    def test_environment_and_flag_sweep_cannot_resolve_panel(self) -> None:
        """Catches any non-human auto-approval seam entering shipping code."""
        from floati.tui_approval import ApprovalPanelController

        env_names = (
            "FLOATI_AUTO_APPROVE",
            "FLOATI_APPROVE",
            "CI",
            "YES",
            "AUTO_APPROVE",
        )
        for env_name in env_names:
            with self.subTest(env_name=env_name), patch.dict(
                os.environ, {env_name: "1"}, clear=False
            ):
                controller = ApprovalPanelController(session_id="session-a")
                controller.enqueue(_request())
                with self.assertRaises(ProtocolRefusal) as caught:
                    controller.resolve_non_human(source=env_name)
                self.assertEqual(
                    "approval_human_act_required", caught.exception.code
                )
                self.assertEqual([], controller.resolved_receipts)
                self.assertEqual("approval-request-1", controller.active_request_id)

        controller = ApprovalPanelController(session_id="session-a")
        controller.enqueue(_request())
        with self.assertRaises(ProtocolRefusal) as caught:
            controller.resolve_non_human(source="--yes")
        self.assertEqual("approval_human_act_required", caught.exception.code)

    def test_color_monochrome_and_no_color_capture_the_complete_panel(self) -> None:
        """Catches color carrying focus meaning or a degraded tier losing controls."""
        from floati.demo import capture_approval_panel

        capture_root = Path("docs/evidence/captures")
        color_path = capture_root / "regatta-r4-color.txt"
        mono_path = capture_root / "regatta-r4-monochrome.txt"
        color = capture_approval_panel(color=True)
        mono = capture_approval_panel(color=False)

        self.assertEqual(color, color_path.read_text(encoding="utf-8"))
        self.assertEqual(mono, mono_path.read_text(encoding="utf-8"))
        self.assertEqual(ANSI.sub("", color), mono)
        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            self.assertEqual(mono, capture_approval_panel())
        for signal in (
            "APPROVAL REQUIRED",
            "effect_intent",
            "refs/heads/main",
            "> 1  Refuse once",
            "2  Allow once",
            "3  Allow for this session",
            "Esc refuse",
            "Enter commit",
        ):
            self.assertIn(signal, mono)


if __name__ == "__main__":
    unittest.main()
