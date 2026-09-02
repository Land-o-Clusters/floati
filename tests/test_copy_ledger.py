from __future__ import annotations

from floati import fixture_ids as public_ids

import ast
import unittest
import tempfile
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
            "tui.unknown", "tui.violation_prefix",
            "tui.event.root_unavailable", "tui.event.source_unsupported",
            "tui.event.watch_unavailable",
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
            "help.quota", "help.quota.collect", "help.quota.show",
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

    def test_door_copy_is_draft_stamped_and_registered_once(self) -> None:
        """Catches new onboarding-door copy escaping the generated review ledger."""
        from floati import tui_doors
        from floati.copy import copy_ledger_markdown

        ledger = copy_ledger_markdown()
        expected = {
            "tui.door.hints": "DRAFT - arrows/digits choose · Enter continue · Esc back",
            "tui.door.node_prompt": "DRAFT - Node id",
            "tui.door.harness_prompt": "DRAFT - Harness",
            "tui.door.lease_prompt": "DRAFT - Lease minutes",
            "tui.door.text_input_prefix": "DRAFT - > ",
            "tui.door.lifetime_title": "DRAFT - Choose node lifetime",
            "tui.door.preview_title": "DRAFT - Review exact ledger records",
            "tui.door.permanent_label": "DRAFT - Permanent",
            "tui.door.permanent_detail": "DRAFT - No automatic expiry.",
            "tui.door.temporary_label": "DRAFT - Temporary",
            "tui.door.temporary_detail": "DRAFT - Requires a bounded lease.",
            "tui.door.commit_label": "DRAFT - Commit these records",
            "tui.door.commit_detail": "DRAFT - Append the previewed records once.",
            "tui.door.back_label": "DRAFT - Back",
            "tui.door.back_detail": "DRAFT - Revise the preceding answer.",
            "tui.door.interactive_terminal_required": "DRAFT - onboarding doors require interactive stdin and stderr",
            "tui.door.viewport_too_small": "DRAFT - the onboarding frame does not fit the measured terminal viewport",
            "tui.door.input_closed": "DRAFT - onboarding input ended before confirmation",
            "tui.door.controller_invalid": "DRAFT - onboarding requires one door controller",
            "tui.door.solo_fully_flagged_remedy": "DRAFT - floati init --root ROOT --solo NODE --harness HARNESS",
            "tui.door.node_add_fully_flagged_remedy": "DRAFT - floati node add --root ROOT --node NODE --harness HARNESS --lifetime permanent|temporary [--lease-minutes N]",
            "tui.door.option_invalid": "DRAFT - door options require ids, labels, and details",
            "tui.door.frame_option_invalid": "DRAFT - door frame requires unique options and one focused option",
            "tui.door.width_invalid": "DRAFT - onboarding doors require at least 40 terminal columns",
            "tui.door.color_tier_invalid": "DRAFT - door color tier must be 256, 16, or mono",
            "tui.door.title_invalid": "DRAFT - door title must be non-empty text",
            "tui.door.body_invalid": "DRAFT - door body rows must be text",
            "tui.door.text_invalid": "DRAFT - this onboarding text step requires non-empty text",
            "tui.door.preview_invalid": "DRAFT - exact preview can be attached once at the preview step",
            "tui.door.key_invalid": "DRAFT - door keys must be text",
            "tui.door.solo_text_invalid": "DRAFT - solo onboarding requires explicit node and harness text",
            "tui.door.output_invalid": "DRAFT - node add onboarding requires a preview output",
            "tui.door.preview_required": "DRAFT - node add requires the preview decision step",
            "tui.door.terminal_io_failed": "DRAFT - the onboarding terminal could not complete its I/O lifecycle",
            "tui.door.cancelled": "DRAFT - onboarding was cancelled before commit",
            "tui.door.solo_flags_conflict": "DRAFT - no-value --solo does not compose with harness or governance flags",
            "tui.door.harness_requires_solo": "DRAFT - --harness requires --solo",
            "tui.door.node_add_shape_invalid": "DRAFT - node add requires either no flags or the complete flagged shape",
            "tui.door.tide_fields_incomplete": "DRAFT - optional tide step requires metric, threshold, and action together",
            "tui.door.node_add_commit_failed_prefix": "DRAFT - node add commit: ",
            "tui.door.solo_config_preview": "DRAFT - solo configuration exact bytes:",
            "tui.door.solo_registry_preview": "DRAFT - registry entry exact values:",
            "tui.door.solo_authority_preview": "DRAFT - authority grant exact values:",
        }

        self.assertEqual(set(expected), set(tui_doors.TUI_DOOR_COPY))
        for key, value in expected.items():
            self.assertEqual(value, tui_doors.TUI_DOOR_COPY[key])
            self.assertTrue(value.startswith("DRAFT -"), key)
            self.assertIn(f"`{key}`", ledger)
            self.assertIn(value.replace("|", "\\|"), ledger)

    def test_tui_3_refusal_prompt_detail_and_remedy_copy_has_no_raw_literals(self) -> None:
        """Catches new visible door copy bypassing TUI_DOOR_COPY at product call sites."""

        def raw_literals(node: ast.AST) -> list[str]:
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "TUI_DOOR_COPY"
            ):
                return []
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return [node.value]
            values: list[str] = []
            for child in ast.iter_child_nodes(node):
                values.extend(raw_literals(child))
            return values

        targets = {
            Path("floati/tui_doors.py"): None,
            Path("floati/cli.py"): {"_init"},
            Path("floati/admin_cli.py"): {"_node_add"},
        }
        findings: list[str] = []
        for path, selected_functions in targets.items():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            roots: list[ast.AST]
            if selected_functions is None:
                roots = [tree]
            else:
                roots = [
                    node
                    for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in selected_functions
                ]
            for root in roots:
                for node in ast.walk(root):
                    if isinstance(node, ast.Call):
                        name = (
                            node.func.id
                            if isinstance(node.func, ast.Name)
                            else node.func.attr
                            if isinstance(node.func, ast.Attribute)
                            else ""
                        )
                        if name in {"ProtocolRefusal", "DurabilityFailure"}:
                            for argument in node.args[1:]:
                                for value in raw_literals(argument):
                                    findings.append(f"{path}:{node.lineno}:{value}")
                    if isinstance(node, (ast.Assign, ast.AnnAssign)):
                        names = []
                        if isinstance(node, ast.Assign):
                            names = [
                                target.id
                                for target in node.targets
                                if isinstance(target, ast.Name)
                            ]
                            value_node = node.value
                        else:
                            names = [node.target.id] if isinstance(node.target, ast.Name) else []
                            value_node = node.value
                        if any(
                            name.endswith(("_PROMPT", "_TITLE", "_LABEL", "_DETAIL", "_REMEDY"))
                            for name in names
                        ) and value_node is not None:
                            for value in raw_literals(value_node):
                                findings.append(f"{path}:{node.lineno}:{value}")

        self.assertEqual([], findings)

    def test_copy_examples_use_role_neutral_fictional_nodes(self) -> None:
        """Projecting a seat-shaped example into an invalid two-word argv is rejected."""

        from floati.copy import copy_ledger_markdown
        from scripts.public_name_fence import scan_tree

        ledger = copy_ledger_markdown()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "docs" / "COPY-LEDGER.md"
            path.parent.mkdir(parents=True)
            path.write_text(ledger, encoding="utf-8")
            findings = [
                finding
                for finding in scan_tree(Path(temporary))
                if finding["code"] == "seat_name"
            ]
            self.assertEqual([], findings)
        self.assertIn(public_ids.builder("a"), ledger)

    def test_hidden_wake_evaluation_has_no_registered_copy_entry(self) -> None:
        'Catches the internal wake gate entering reviewer-owned visible copy.'
        from floati.copy import copy_ledger_markdown
        from floati.helptext import _RAW

        self.assertNotIn("wake-evaluate", _RAW)
        self.assertNotIn("wake-evaluate", copy_ledger_markdown())
        self.assertNotIn("wake daemon serve", _RAW)
        self.assertNotIn("wake daemon serve", copy_ledger_markdown())


if __name__ == "__main__":
    unittest.main()
