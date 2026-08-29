from __future__ import annotations

import unittest
from pathlib import Path
import subprocess


class CopyLedgerTests(unittest.TestCase):
    def test_module_generator_includes_registered_help_entries(self) -> None:
        result = subprocess.run(
            ["python3", "-m", "floati.copy"],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("`help.root`", result.stdout)
        self.assertIn("`help.work.complete`", result.stdout)
        self.assertIn("`tui.header`", result.stdout)
        self.assertIn("`tui.hints`", result.stdout)
        for key in (
            "tui.observed", "tui.stale", "tui.no_nodes", "tui.none",
            "tui.delivery", "tui.ack", "tui.item", "tui.items",
            "tui.holder", "tui.detail", "tui.role", "tui.visible_mail",
            "tui.unknown",
            "tui.work_ready", "tui.work_blocked", "tui.work_claimed",
            "tui.work_done", "tui.worker_receipt",
            "tui.worker_process_cancelled", "tui.worker_authority_expired",
            "tui.effects_header", "tui.effect_confirmed",
            "tui.effect_compensation", "tui.effect_proposed",
            "tui.effect_executed", "tui.effect_unknown",
            "tui.effect_incomplete", "tui.effect_failed",
            "effect.compensation.plan_unavailable",
            "effect.input.operation_id_invalid",
            "effect.input.run_id_invalid",
            "effect.input.attempt_id_invalid",
            "effect.input.plan_digest_invalid",
            "help.effects", "help.effect", "help.effect.show",
            "help.effect.reconcile", "help.effect.compensate",
            "help.wake.daemon", "help.wake.daemon.consent",
            "help.wake.daemon.bind", "help.wake.daemon.install",
            "help.wake.daemon.start", "help.wake.daemon.status",
            "help.wake.daemon.stop", "help.wake.daemon.remove",
            "help.wake.daemon.revoke",
            "help.wake.arm",
            "replay.header", "replay.plain_prefix", "replay.summary",
            "replay.event.claim", "replay.event.turn",
            "replay.event.degraded", "replay.event.denied",
            "replay.event.complete", "replay.source.work",
            "replay.source.worker", "replay.source.refusal",
            "replay.source.denial", "replay.source.event",
            "replay.unit.events", "replay.unit.milliseconds",
        ):
            self.assertIn(f"`{key}`", result.stdout)

    def test_generated_copy_ledger_matches_all_static_help_surfaces(self) -> None:
        from floati.copy import copy_ledger_markdown

        path = Path("docs/COPY-LEDGER.md")
        self.assertTrue(path.is_file(), "visible provisional strings require a tracked copy ledger")
        self.assertEqual(copy_ledger_markdown(), path.read_text(encoding="utf-8"))

    def test_hidden_wake_evaluation_has_no_registered_copy_entry(self) -> None:
        """Catches the internal wake gate entering Fable-owned visible copy."""
        from floati.copy import copy_ledger_markdown
        from floati.helptext import _RAW

        self.assertNotIn("wake-evaluate", _RAW)
        self.assertNotIn("wake-evaluate", copy_ledger_markdown())
        self.assertNotIn("wake daemon serve", _RAW)
        self.assertNotIn("wake daemon serve", copy_ledger_markdown())


if __name__ == "__main__":
    unittest.main()
