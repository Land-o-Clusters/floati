from __future__ import annotations

from tests.test_cli import LAUNCHER

import json
import shutil
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from floati.effects import EffectLedger
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema
from tests.test_effect_controller import _EffectCase
from tests.test_effects import EffectRecordFixture


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EFFECT_STATUS_SCHEMA = REPOSITORY_ROOT / "schemas/v1/effect-status-artifact.schema.json"


def write_effect_rows(root: FloatiRoot, rows: list[dict[str, object]]) -> None:
    path = root.resolve_relative(EffectLedger.relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = [dict(row, tenant_id=root.tenant_id) for row in rows]
    path.write_bytes(b"".join(encode_frame(row) for row in normalized))


def tree_snapshot(path: Path) -> dict[str, tuple[str, bytes]]:
    return {
        child.relative_to(path).as_posix(): (
            "directory" if child.is_dir() else "file",
            b"" if child.is_dir() else child.read_bytes(),
        )
        for child in sorted(path.rglob("*"))
    }


def lifecycle_rows(
    state: str, *, idempotency_key: str = "effect-intent-1"
) -> tuple[EffectRecordFixture, list[dict[str, object]]]:
    fixture = EffectRecordFixture()
    rows = fixture.rows()
    selected = [rows["effect_intent"]]
    if state != "intent":
        selected.append(rows["effect_dispatched"])
    if state == "confirmed":
        selected.extend((rows["effect_acknowledged"], rows["effect_confirmed"]))
    elif state == "failed":
        selected.append(rows["effect_failed"])
    elif state == "unknown":
        selected.append(rows["effect_unknown"])
    for row in selected:
        row["idempotency_key"] = idempotency_key
    return fixture, selected


ATTENTION_OPERATION_IDS = {
    "confirmed": "effect-op-018f0f23abcd71238000000000000001",
    "failed": "effect-op-018f0f23abcd71238000000000000002",
    "unknown": "effect-op-018f0f23abcd71238000000000000003",
    "incomplete": "effect-op-018f0f23abcd71238000000000000004",
}


def attention_effect_rows() -> list[dict[str, object]]:
    rows = []
    for state, lifecycle_state in (
        ("confirmed", "confirmed"),
        ("failed", "failed"),
        ("unknown", "unknown"),
        ("incomplete", "intent"),
    ):
        _, selected = lifecycle_rows(
            lifecycle_state, idempotency_key=f"effect-attention-{state}"
        )
        for row in selected:
            row["operation_id"] = ATTENTION_OPERATION_IDS[state]
        rows.extend(selected)
    return rows


def mixed_effect_rows() -> list[dict[str, object]]:
    _, confirmed = lifecycle_rows("confirmed", idempotency_key="effect-confirmed")
    _, failed = lifecycle_rows("failed", idempotency_key="effect-failed")
    _, unknown = lifecycle_rows("unknown", idempotency_key="effect-unknown")
    proposed_fixture, proposed = lifecycle_rows(
        "confirmed", idempotency_key="effect-proposed"
    )
    proposed.append(proposed_fixture.rows()["compensation_proposed"])
    proposed[-1]["idempotency_key"] = "effect-proposed"

    source_fixture, executed = lifecycle_rows(
        "confirmed", idempotency_key="effect-executed"
    )
    compensation_fixture, compensation = lifecycle_rows(
        "confirmed", idempotency_key="effect-compensation-operation"
    )
    source_rows = source_fixture.rows()
    compensation_rows = compensation_fixture.rows()
    proposal = source_rows["compensation_proposed"]
    proposal["idempotency_key"] = "effect-executed"
    proposal["compensation_operation_id"] = compensation_fixture.binding()["operation_id"]
    proposal["compensation_request_digest"] = compensation_fixture.binding()["request_digest"]
    terminal = source_rows["compensation_executed"]
    terminal["idempotency_key"] = "effect-executed"
    terminal["compensation_proposal_id"] = proposal["id"]
    terminal["compensation_operation_id"] = compensation_fixture.binding()["operation_id"]
    terminal["compensation_terminal_evidence_id"] = compensation_rows["effect_confirmed"]["id"]
    executed.extend((proposal, *compensation, terminal))
    return [*confirmed, *failed, *unknown, *proposed, *executed]


class EffectCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "alpha"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *arguments],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def artifact(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual("", result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        return json.loads(result.stdout)

    def test_effects_lists_deterministic_projection_with_filters(self) -> None:
        first, first_rows = lifecycle_rows("intent")
        second, second_rows = lifecycle_rows("confirmed", idempotency_key="effect-intent-2")
        rows = [*second_rows, *first_rows]
        write_effect_rows(self.root, rows)

        result = self.run_cli("effects", "--root", str(self.home))

        self.assertEqual(0, result.returncode, result.stderr)
        artifact = self.artifact(result)
        validate_json_schema(artifact, EFFECT_STATUS_SCHEMA)
        operations = artifact["evidence"]["operations"]
        self.assertEqual(
            [first.binding()["operation_id"], second.binding()["operation_id"]],
            [operation["operation_id"] for operation in operations],
        )
        filtered = self.run_cli(
            "effects", "--root", str(self.home),
            "--run", second.run_id, "--attempt", second.attempt_id,
        )
        self.assertEqual(0, filtered.returncode, filtered.stderr)
        self.assertEqual(
            [second.binding()["operation_id"]],
            [
                operation["operation_id"]
                for operation in self.artifact(filtered)["evidence"]["operations"]
            ],
        )

    def test_effects_orders_shared_attention_rank_before_operation_id(self) -> None:
        write_effect_rows(self.root, attention_effect_rows())

        result = self.run_cli("effects", "--root", str(self.home))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [
                ATTENTION_OPERATION_IDS["unknown"],
                ATTENTION_OPERATION_IDS["incomplete"],
                ATTENTION_OPERATION_IDS["failed"],
                ATTENTION_OPERATION_IDS["confirmed"],
            ],
            [
                row["operation_id"]
                for row in self.artifact(result)["evidence"]["operations"]
            ],
        )

        earlier_id = "effect-op-018f0f23abcd71238000000000000005"
        later_id = "effect-op-018f0f23abcd71238000000000000009"
        _, earlier = lifecycle_rows("unknown", idempotency_key="effect-tie-earlier")
        _, later = lifecycle_rows("unknown", idempotency_key="effect-tie-later")
        for row in earlier:
            row["operation_id"] = earlier_id
        for row in later:
            row["operation_id"] = later_id
        write_effect_rows(self.root, [*later, *earlier])

        tied = self.run_cli("effects", "--root", str(self.home))

        self.assertEqual(0, tied.returncode, tied.stderr)
        self.assertEqual(
            [earlier_id, later_id],
            [
                row["operation_id"]
                for row in self.artifact(tied)["evidence"]["operations"]
            ],
        )

    def test_effect_show_returns_one_exact_operation_or_no_result(self) -> None:
        fixture, rows = lifecycle_rows("failed")
        write_effect_rows(self.root, rows)
        operation_id = str(fixture.binding()["operation_id"])

        found = self.run_cli(
            "effect", "show", "--root", str(self.home),
            "--operation", operation_id,
        )
        absent = self.run_cli(
            "effect", "show", "--root", str(self.home),
            "--operation", "effect-op-" + uuid7_hex(),
        )

        self.assertEqual(0, found.returncode, found.stderr)
        self.assertEqual(
            [operation_id],
            [row["operation_id"] for row in self.artifact(found)["evidence"]["operations"]],
        )
        self.assertEqual(32, absent.returncode)
        missing = self.artifact(absent)
        self.assertEqual("no_result", missing["status"])
        self.assertEqual([], missing["evidence"]["operations"])
        validate_json_schema(missing, EFFECT_STATUS_SCHEMA)

    def test_read_commands_and_compensation_preview_create_no_files_or_locks(self) -> None:
        operation_id = "effect-op-" + uuid7_hex()
        before = tree_snapshot(self.home)

        listed = self.run_cli("effects", "--root", str(self.home))
        shown = self.run_cli(
            "effect", "show", "--root", str(self.home),
            "--operation", operation_id,
        )
        previewed = self.run_cli(
            "effect", "compensate", "--root", str(self.home),
            "--operation", operation_id, "--preview",
        )

        self.assertEqual(32, listed.returncode)
        self.assertEqual(32, shown.returncode)
        self.assertEqual(20, previewed.returncode)
        self.assertEqual(
            "effect_compensation_plan_unavailable",
            self.artifact(previewed)["evidence"]["code"],
        )
        self.assertEqual(before, tree_snapshot(self.home))
        self.assertFalse(any(path.name.endswith(".lock") for path in self.home.rglob("*")))
        missing = Path(self.temp.name) / "missing-root"
        for arguments in (
            ("effects", "--root", str(missing)),
            (
                "effect", "show", "--root", str(missing),
                "--operation", operation_id,
            ),
            (
                "effect", "compensate", "--root", str(missing),
                "--operation", operation_id, "--preview",
            ),
        ):
            with self.subTest(missing_root=arguments[:2]):
                self.assertEqual(20, self.run_cli(*arguments).returncode)
                self.assertFalse(missing.exists())

    def test_compensation_confirm_unavailable_is_physically_read_only(self) -> None:
        operation_id = "effect-op-" + uuid7_hex()
        digest = "a" * 64
        before = tree_snapshot(self.home)

        confirmed = self.run_cli(
            "effect", "compensate", "--root", str(self.home),
            "--operation", operation_id, "--confirm", digest,
        )

        self.assertEqual(20, confirmed.returncode)
        self.assertEqual(
            "effect_compensation_plan_unavailable",
            self.artifact(confirmed)["evidence"]["code"],
        )
        self.assertEqual(before, tree_snapshot(self.home))
        self.assertFalse(any(path.name.endswith(".lock") for path in self.home.rglob("*")))
        self.assertFalse(self.root.resolve_relative(EffectLedger.relative_path).exists())

        missing = Path(self.temp.name) / "missing-confirm-root"
        missing_result = self.run_cli(
            "effect", "compensate", "--root", str(missing),
            "--operation", operation_id, "--confirm", digest,
        )
        self.assertEqual(20, missing_result.returncode)
        self.assertFalse(missing.exists())

    def test_reconcile_uses_only_frozen_adapter_and_emits_typed_artifact(self) -> None:
        case = _EffectCase(self)
        shutil.copyfile(case.run.policy_path, case.root.path / "FLOATI.toml")
        intent = case.controller.intent(**case.intent_args(
            reconciliation_adapter="none",
            expected_confirmation={
                "kind": "none", "locator": "none", "expected_digest": "0" * 64,
            },
        ))
        case.controller.dispatched(
            intent["operation_id"], dispatch_adapter="git_local",
            dispatch_evidence_digest="d" * 64,
        )
        case.controller.unknown(
            intent["operation_id"], reason_code="confirmation_absent",
            evidence_digest="e" * 64, spend_status="unknown",
        )

        result = self.run_cli(
            "effect", "reconcile", "--root", str(case.root.path),
            "--operation", str(intent["operation_id"]),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        artifact = self.artifact(result)
        validate_json_schema(artifact, EFFECT_STATUS_SCHEMA)
        self.assertEqual("reconciled_unknown", artifact["evidence"]["operations"][0]["state"])
        reconciled = EffectLedger(case.root).records()[-1]
        self.assertEqual("effect_reconciled", reconciled["kind"])
        self.assertEqual("unknown", reconciled["reconciled_outcome"])

    def test_compensate_requires_exactly_one_of_preview_or_confirm(self) -> None:
        base = (
            "effect", "compensate", "--root", str(self.home),
            "--operation", "effect-op-" + uuid7_hex(),
        )
        neither = self.run_cli(*base)
        both = self.run_cli(*base, "--preview", "--confirm", "a" * 64)
        preview = self.run_cli(*base, "--preview")
        confirm = self.run_cli(*base, "--confirm", "a" * 64)

        self.assertEqual(20, neither.returncode)
        self.assertEqual(20, both.returncode)
        self.assertEqual("arguments_invalid", self.artifact(neither)["evidence"]["code"])
        self.assertEqual("arguments_invalid", self.artifact(both)["evidence"]["code"])
        for result in (preview, confirm):
            self.assertEqual(20, result.returncode)
            self.assertEqual(
                "effect_compensation_plan_unavailable",
                self.artifact(result)["evidence"]["code"],
            )

    def test_cli_never_accepts_raw_record_adapter_or_confirmation_override(self) -> None:
        operation_id = "effect-op-" + uuid7_hex()
        commands = (
            ("effects", "--root", str(self.home)),
            ("effect", "show", "--root", str(self.home), "--operation", operation_id),
            ("effect", "reconcile", "--root", str(self.home), "--operation", operation_id),
            ("effect", "compensate", "--root", str(self.home), "--operation", operation_id, "--preview"),
        )
        for command in commands:
            for option in (
                "--record", "--adapter", "--result", "--confirmation",
                "--credentials", "--request-body", "--authority", "--policy",
                "--plan", "--target",
            ):
                with self.subTest(command=command[:2], option=option):
                    result = self.run_cli(*command, option, "hostile")
                    self.assertEqual(20, result.returncode)
                    self.assertEqual(
                        "arguments_invalid", self.artifact(result)["evidence"]["code"]
                    )
        typed_refusals = (
            (("effects", "--root", str(self.home), "--run", "run-invalid"), "run_id_invalid"),
            (("effects", "--root", str(self.home), "--attempt", "attempt-invalid"), "attempt_id_invalid"),
            (("effect", "show", "--root", str(self.home), "--operation", "effect-op-invalid"), "effect_operation_id_invalid"),
            (("effect", "compensate", "--root", str(self.home), "--operation", operation_id, "--confirm", "A" * 64), "plan_digest_invalid"),
        )
        for arguments, code in typed_refusals:
            with self.subTest(typed=code):
                result = self.run_cli(*arguments)
                self.assertEqual(20, result.returncode)
                self.assertEqual(code, self.artifact(result)["evidence"]["code"])

    def test_static_help_covers_every_effect_command_without_import_side_effects(self) -> None:
        topics = (
            ("effects",), ("effect",), ("effect", "show"),
            ("effect", "reconcile"), ("effect", "compensate"),
        )
        before = tree_snapshot(self.home)
        for topic in topics:
            with self.subTest(topic=topic):
                result = self.run_cli(*topic, "--help")
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("NAME\n", result.stdout)
                self.assertIn("SYNOPSIS\n", result.stdout)
                self.assertIn("EXIT STATUS\n", result.stdout)
        self.assertEqual(before, tree_snapshot(self.home))


if __name__ == "__main__":
    unittest.main()
