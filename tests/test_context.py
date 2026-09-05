from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from floati.admin_registry import RegistryAdminBackend
from floati.errors import ProtocolRefusal
from floati.node_wizard import NodeAddPlan
from floati.role_assignment import RoleAssignmentPlan
from floati.role_templates import load_shipped_role_templates
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema
from tests.schema_validation import SchemaValidationError
from tests.test_node_projections import MutableLedgerSource


E1_HARNESSES = (
    "claude",
    "cline",
    "codex",
    "cursor",
    "grok-build",
    "herdr",
    "opencode",
    "pi",
)
E1_RECEIPT_PATH = "docs/evidence/conformance/E1-cli-help-probe.json"
E1_RECEIPT_SHA256 = (
    "e653035faa98c67c1d7e2407603eecae3932f18bea429b1dfe6061f6e8715f93"
)
E1_SOURCE_COMMIT = "2601b4a9ebc5d9c388c02c4e6b22700097daeb6e"
E1_DATASET_ID = "e1-context-absence-v1"


def _absence_api() -> tuple[Any, Any]:
    try:
        from floati.context_absences import (
            load_shipped_context_absences,
            parse_context_absence_dataset,
        )
    except ModuleNotFoundError as exc:
        raise AssertionError("floati.context_absences is not implemented") from exc
    return load_shipped_context_absences, parse_context_absence_dataset


def _context_api() -> tuple[Any, Any, Any, Any]:
    try:
        from floati.context import (
            ContextStatusProjection,
            ContextTurnoverProjection,
            register_cli,
            render_context_projection,
        )
    except ModuleNotFoundError as exc:
        raise AssertionError("floati.context is not implemented") from exc
    return (
        ContextStatusProjection,
        ContextTurnoverProjection,
        register_cli,
        render_context_projection,
    )


def _dataset_record() -> dict[str, object]:
    return {
        "schema_version": 0,
        "dataset_id": E1_DATASET_ID,
        "source_commit": E1_SOURCE_COMMIT,
        "harnesses": list(E1_HARNESSES),
        "rows": [
            {
                "harness": harness,
                "access_class": "A",
                "state": "not_exposed",
                "receipt_path": E1_RECEIPT_PATH,
                "receipt_sha256": E1_RECEIPT_SHA256,
            }
            for harness in E1_HARNESSES
        ],
    }


class ContextProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(__file__).parents[1]
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        self.templates = load_shipped_role_templates(self.repo / "roles" / "shipped")
        self.source = MutableLedgerSource(self.root, self.templates)
        self.backend = RegistryAdminBackend(self.root)
        registry_record = {
            "schema_version": 0,
            "id": "registry-018f7e9b3c137abc8def0123456789ab",
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-27T23:15:00.000Z",
            "kind": "registry_entry",
            "node_id": "builder-a",
            "role": "Codex",
            "state": "active",
        }
        self.backend.commit_add(
            NodeAddPlan(
                node_id="builder-a",
                harness="Codex",
                lifetime="permanent",
                lease_minutes=None,
                workspace=str(self.root.path / "nodes" / "builder-a"),
                records=(registry_record,),
                boot_command=None,
                teardown_command=None,
            )
        )
        self.backend.commit_role(
            RoleAssignmentPlan(
                node_id="builder-a",
                template_role="builder",
                record=dict(self.source.roles["builder-a"]),
            )
        )

    def _set_builder_harness(self, harness: str) -> None:
        self.source.nodes = [
            dict(node, harness=harness)
            if node["node_id"] == "builder-a"
            else dict(node)
            for node in self.source.nodes
        ]

    def _root_snapshot(self) -> tuple[tuple[object, ...], ...]:
        rows = []
        for path in sorted(
            [self.root.path, *self.root.path.rglob("*")],
            key=lambda item: item.as_posix(),
        ):
            identity = os.lstat(path)
            relative = "." if path == self.root.path else path.relative_to(self.root.path).as_posix()
            payload = path.read_bytes() if path.is_file() and not path.is_symlink() else None
            rows.append(
                (
                    relative,
                    identity.st_mode,
                    identity.st_ino,
                    identity.st_size,
                    identity.st_mtime_ns,
                    payload,
                )
            )
        return tuple(rows)

    def test_context_source_and_not_exposed_rows_cannot_render_measurements(self) -> None:
        """Catches a context surface or absence row acquiring a fabricated numeric whole."""

        source_path = self.repo / "floati" / "context.py"
        self.assertTrue(source_path.is_file(), "floati/context.py is not implemented")
        source = source_path.read_text(encoding="utf-8")
        forbidden = re.compile(
            r"\b(?:percent|percentage|fraction|gauge)\b|[\u2588\u2593\u2592\u2591]",
            re.IGNORECASE,
        )
        self.assertIsNone(forbidden.search(source))
        self.assertNotIn("automatic", source.casefold())

        load_shipped, _ = _absence_api()
        dataset = load_shipped()
        for row in dataset.rows:
            with self.subTest(harness=row.harness):
                for value in row.record.values():
                    self.assertNotIsInstance(value, (int, float, bool))

    def test_absence_without_receipt_path_and_sha_refuses_before_render(self) -> None:
        """Catches rendering a typed absence whose measurement receipt is incomplete."""

        _, parse_dataset = _absence_api()
        for missing in ("receipt_path", "receipt_sha256"):
            with self.subTest(missing=missing):
                record = _dataset_record()
                del record["rows"][0][missing]  # type: ignore[index]
                with self.assertRaises(ProtocolRefusal) as raised:
                    parse_dataset(record)
                self.assertEqual("context_absence_citation_missing", raised.exception.code)

    def test_absence_without_an_access_class_refuses(self) -> None:
        """Catches an external-probe conclusion losing its scope label."""

        _, parse_dataset = _absence_api()
        record = _dataset_record()
        del record["rows"][0]["access_class"]  # type: ignore[index]

        with self.assertRaises(ProtocolRefusal) as raised:
            parse_dataset(record)

        self.assertEqual("context_access_class_missing", raised.exception.code)

    def test_absence_with_a_forged_access_class_refuses(self) -> None:
        """Catches E2 treating future B/C vocabulary as measured testimony."""

        _, parse_dataset = _absence_api()
        for access_class in ("B", "C", "D"):
            with self.subTest(access_class=access_class):
                record = _dataset_record()
                record["rows"][0]["access_class"] = access_class  # type: ignore[index]
                with self.assertRaises(ProtocolRefusal) as raised:
                    parse_dataset(record)
                self.assertEqual("context_absence_dataset_invalid", raised.exception.code)

    def test_unlisted_recorded_harness_refuses_without_fallback(self) -> None:
        """Catches a future harness receiving generic prose unsupported by E1 evidence."""

        ContextStatusProjection, _, _, _ = _context_api()
        self._set_builder_harness("future-cli")

        with self.assertRaises(ProtocolRefusal) as raised:
            ContextStatusProjection(
                self.root, "builder-a", source=self.source
            ).project()

        self.assertEqual("context_harness_unknown", raised.exception.code)

    def test_turnover_recipe_is_physically_read_only_and_ordered(self) -> None:
        """Catches turnover writing state or changing the D3/D5 ritual order."""

        _, ContextTurnoverProjection, _, _ = _context_api()
        before = self._root_snapshot()

        projected = ContextTurnoverProjection(
            self.root, "builder-a", source=self.source
        ).project()

        self.assertEqual(before, self._root_snapshot())
        self.assertTrue(projected["read_only"])
        self.assertEqual(
            ["teardown_projection", "state_flush_receipt", "boot_projection"],
            [step["kind"] for step in projected["steps"]],
        )

    def test_status_is_derived_for_each_of_the_eight_e1_harnesses(self) -> None:
        """Catches a shipped E1 harness being omitted or mapped to uncited output."""

        ContextStatusProjection, _, _, _ = _context_api()
        for harness in E1_HARNESSES:
            with self.subTest(harness=harness):
                self._set_builder_harness(harness.upper() if harness != "grok-build" else harness)
                projected = ContextStatusProjection(
                    self.root, "builder-a", source=self.source
                ).project()
                self.assertEqual("context_status_projection", projected["kind"])
                self.assertEqual(harness, projected["harness"])
                self.assertEqual("not_exposed", projected["remaining_context"]["state"])
                self.assertEqual("A", projected["remaining_context"]["access_class"])
                self.assertEqual(
                    f"remaining context: not exposed to external probes by {harness}",
                    projected["remaining_context"]["message"],
                )
                self.assertEqual(
                    {
                        "path": E1_RECEIPT_PATH,
                        "sha256": E1_RECEIPT_SHA256,
                    },
                    projected["remaining_context"]["receipt"],
                )

    def test_turnover_recipe_spells_exact_existing_verbs_and_live_provenance(self) -> None:
        """Catches a ritual that guesses transport values or drops D1/D2 provenance."""

        _, ContextTurnoverProjection, _, _ = _context_api()
        projected = ContextTurnoverProjection(
            self.root, "builder-a", source=self.source
        ).project()

        self.assertEqual("operator_supplied", projected["inputs"]["source"])
        self.assertEqual(
            {
                "declared_roots": "<DECLARED_ROOTS_FILE>",
                "managed_executable": "<MANAGED_EXECUTABLE>",
                "profile": "<PROFILE>",
                "prior_mtime_ns": "<PRIOR_MTIME_NS>",
            },
            projected["inputs"]["values"],
        )
        root = str(self.root.path)
        self.assertEqual(
            [
                "floati",
                "node",
                "teardown",
                "--root",
                root,
                "--node",
                "builder-a",
                "--declared-roots",
                "<DECLARED_ROOTS_FILE>",
                "--managed-executable",
                "<MANAGED_EXECUTABLE>",
                "--profile",
                "<PROFILE>",
            ],
            projected["steps"][0]["argv"],
        )
        self.assertEqual(["--json"], projected["steps"][0]["optional_argv"])
        self.assertEqual(
            [
                "floati",
                "node",
                "state-flush",
                "--root",
                root,
                "--node",
                "builder-a",
            ],
            projected["steps"][1]["argv"],
        )
        self.assertEqual(
            ["--prior-mtime-ns", "<PRIOR_MTIME_NS>"],
            projected["steps"][1]["optional_argv"],
        )
        expected_boot = list(projected["steps"][0]["argv"])
        expected_boot[2] = "boot"
        self.assertEqual(expected_boot, projected["steps"][2]["argv"])
        self.assertEqual(["--json"], projected["steps"][2]["optional_argv"])
        role = self.source.roles["builder-a"]
        self.assertEqual(
            {
                "role_record_id": role["id"],
                "template_role": "builder",
                "template_version": self.templates["builder"].template_version,
                "template_sha256": self.templates["builder"].digest,
            },
            projected["role_provenance"],
        )

    def test_dataset_is_closed_self_enumerating_and_schema_valid(self) -> None:
        """Catches the dataset list and its evidence rows drifting apart."""

        load_shipped, _ = _absence_api()
        dataset = load_shipped()
        self.assertEqual(E1_HARNESSES, dataset.harnesses)
        self.assertEqual(E1_HARNESSES, tuple(row.harness for row in dataset.rows))
        self.assertEqual(E1_SOURCE_COMMIT, dataset.source_commit)
        self.assertEqual(E1_DATASET_ID, dataset.dataset_id)
        self.assertEqual({"A"}, {row.access_class for row in dataset.rows})
        self.assertEqual(
            set(dataset.harnesses), {row.harness for row in dataset.rows}
        )

        try:
            from floati.context_absences_v0 import DATASET_JSON
        except ModuleNotFoundError as exc:
            raise AssertionError("versioned absence data module is not implemented") from exc
        raw = json.loads(DATASET_JSON)
        schema = self.repo / "schemas" / "v0" / "context-absence-dataset.schema.json"
        validate_json_schema(raw, schema)

        from floati.manifest import _deployable_paths

        deployable = _deployable_paths(self.repo)
        self.assertIn("floati/context_absences_v0.py", deployable)
        self.assertNotIn("floati/context_absences.v0.json", deployable)

    def test_absence_schema_has_the_closed_access_class_vocabulary(self) -> None:
        """Catches the owner-corrected A/B/C vocabulary narrowing or opening."""

        schema = self.repo / "schemas" / "v0" / "context-absence-dataset.schema.json"
        schema_record = json.loads(schema.read_text(encoding="utf-8"))
        self.assertEqual(
            {"enum": ["A", "B", "C"]},
            schema_record["$defs"]["absence"]["properties"]["access_class"],
        )

    def test_json_and_ascii_twins_validate_and_preserve_citations(self) -> None:
        """Catches JSON and human projections disagreeing about evidence or order."""

        ContextStatusProjection, ContextTurnoverProjection, _, render = _context_api()
        turnover = ContextTurnoverProjection(
            self.root, "builder-a", source=self.source
        )
        for harness in E1_HARNESSES:
            with self.subTest(harness=harness):
                self._set_builder_harness(harness)
                status = ContextStatusProjection(
                    self.root, "builder-a", source=self.source
                )
                artifact = status.project()
                self.assertEqual(artifact, json.loads(status.to_json()))
                board = render(artifact)
                board.encode("ascii")
                self.assertNotIn("DRAFT -", board)
                validate_json_schema(
                    artifact,
                    self.repo / "schemas" / "v0" / "context-projection.schema.json",
                )
                self.assertIn(E1_RECEIPT_PATH, board)
                self.assertIn(E1_RECEIPT_SHA256, board)
        for projection in (turnover,):
            artifact = projection.project()
            self.assertEqual(artifact, json.loads(projection.to_json()))
            board = render(artifact)
            board.encode("ascii")
            self.assertNotIn("DRAFT -", board)
            validate_json_schema(
                artifact,
                self.repo / "schemas" / "v0" / "context-projection.schema.json",
            )
        turnover_board = turnover.render()
        self.assertLess(
            turnover_board.index("node teardown"),
            turnover_board.index("node state-flush"),
        )
        self.assertLess(
            turnover_board.index("node state-flush"),
            turnover_board.index("node boot"),
        )

    def test_projection_schema_refuses_measurement_copy_in_status_message(self) -> None:
        """Catches the JSON contract admitting a digit, fraction, or ASCII gauge."""

        ContextStatusProjection, _, _, _ = _context_api()
        schema = self.repo / "schemas" / "v0" / "context-projection.schema.json"
        artifact = ContextStatusProjection(
            self.root, "builder-a", source=self.source
        ).project()
        for suffix in (" 50%", " 1/2", " [####]"):
            with self.subTest(suffix=suffix):
                forged = json.loads(json.dumps(artifact))
                forged["remaining_context"]["message"] += suffix
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(forged, schema)

    def test_constructed_projections_re_read_node_and_role_at_each_call(self) -> None:
        """Catches projection construction capturing stale node or D2 evidence."""

        ContextStatusProjection, ContextTurnoverProjection, _, _ = _context_api()
        status = ContextStatusProjection(self.root, "builder-a", source=self.source)
        turnover = ContextTurnoverProjection(self.root, "builder-a", source=self.source)
        self._set_builder_harness("cline")
        replacement = "registry-role-0193aabbccdd7eef8abc0123456789ab"
        self.source.roles["builder-a"]["id"] = replacement

        self.assertEqual("cline", status.project()["harness"])
        self.assertEqual(replacement, turnover.project()["role_provenance"]["role_record_id"])

    def test_renderer_refuses_a_status_artifact_missing_its_citation(self) -> None:
        """Catches direct rendering bypassing the citation-bound dataset parser."""

        ContextStatusProjection, _, _, render = _context_api()
        artifact = ContextStatusProjection(
            self.root, "builder-a", source=self.source
        ).project()
        del artifact["remaining_context"]["receipt"]["sha256"]

        with self.assertRaises(ProtocolRefusal) as raised:
            render(artifact)

        self.assertEqual("context_absence_citation_missing", raised.exception.code)

    def test_renderer_refuses_a_status_artifact_missing_its_access_class(self) -> None:
        """Catches detached rendering of an unscoped context conclusion."""

        ContextStatusProjection, _, _, render = _context_api()
        artifact = ContextStatusProjection(
            self.root, "builder-a", source=self.source
        ).project()
        del artifact["remaining_context"]["access_class"]

        with self.assertRaises(ProtocolRefusal) as raised:
            render(artifact)

        self.assertEqual("context_output_invalid", raised.exception.code)

    def test_status_renderer_refuses_caller_selected_evidence(self) -> None:
        """Catches detached status rendering replacing the shipped E1 citation."""

        ContextStatusProjection, _, _, render = _context_api()
        artifact = ContextStatusProjection(
            self.root, "builder-a", source=self.source
        ).project()
        mutations = (
            ("source_commit", "0" * 40),
            ("receipt_path", "docs/evidence/invented.json"),
            ("receipt_sha256", "0" * 64),
            ("access_class", "B"),
        )
        for field, replacement in mutations:
            with self.subTest(field=field):
                forged = json.loads(json.dumps(artifact))
                if field == "source_commit":
                    forged["dataset"][field] = replacement
                elif field == "access_class":
                    forged["remaining_context"][field] = replacement
                else:
                    forged["remaining_context"]["receipt"][
                        field.removeprefix("receipt_")
                    ] = replacement
                with self.assertRaises(ProtocolRefusal) as raised:
                    render(forged)
                self.assertEqual("context_output_invalid", raised.exception.code)

    def test_status_projection_has_no_caller_selected_dataset_seam(self) -> None:
        """Catches production status bypassing the one shipped E1 dataset."""

        ContextStatusProjection, _, _, _ = _context_api()
        load_shipped, _ = _absence_api()
        with self.assertRaises(TypeError):
            ContextStatusProjection(
                self.root,
                "builder-a",
                source=self.source,
                dataset=load_shipped(),
            )

    def test_renderer_refuses_a_mutated_turnover_verb_shape(self) -> None:
        """Catches a recipe board printing a command other than the landed D3/D5 shapes."""

        _, ContextTurnoverProjection, _, render = _context_api()
        artifact = ContextTurnoverProjection(
            self.root, "builder-a", source=self.source
        ).project()
        artifact["steps"][1]["argv"][2] = "boot"

        with self.assertRaises(ProtocolRefusal) as raised:
            render(artifact)

        self.assertEqual("context_turnover_shape_invalid", raised.exception.code)

    def test_turnover_renderer_binds_role_to_the_shipped_template(self) -> None:
        """Catches detached turnover rendering invented D1 provenance."""

        _, ContextTurnoverProjection, _, render = _context_api()
        artifact = ContextTurnoverProjection(
            self.root, "builder-a", source=self.source
        ).project()
        for field, replacement in (
            ("template_role", "invented"),
            ("template_version", self.templates["builder"].template_version + 1),
            ("template_sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                forged = json.loads(json.dumps(artifact))
                forged["role_provenance"][field] = replacement
                with self.assertRaises(ProtocolRefusal) as raised:
                    render(forged)
                self.assertEqual("context_output_invalid", raised.exception.code)

    def test_turnover_renderer_refuses_a_well_formed_foreign_d2_record_id(self) -> None:
        """Catches detached turnover nominating another syntactically valid D2 row."""

        _, ContextTurnoverProjection, _, render = _context_api()
        artifact = ContextTurnoverProjection(
            self.root, "builder-a", source=self.source
        ).project()
        artifact["role_provenance"][
            "role_record_id"
        ] = "registry-role-0193aabbccdd7eef8abc0123456789ab"

        with self.assertRaises(ProtocolRefusal) as raised:
            render(artifact)

        self.assertEqual("context_output_invalid", raised.exception.code)

    def test_default_registry_projections_are_physically_read_only(self) -> None:
        """Catches the production adapter writing while it re-reads durable evidence."""

        ContextStatusProjection, ContextTurnoverProjection, _, _ = _context_api()
        before = self._root_snapshot()

        status = ContextStatusProjection(self.root, "builder-a").project()
        turnover = ContextTurnoverProjection(self.root, "builder-a").project()

        self.assertEqual("codex", status["harness"])
        self.assertEqual(
            self.source.roles["builder-a"]["id"],
            turnover["role_provenance"]["role_record_id"],
        )
        self.assertEqual(before, self._root_snapshot())

    def test_turnover_refuses_incomplete_d2_role_records(self) -> None:
        """Catches live turnover trusting malformed D2 metadata or answers."""

        _, ContextTurnoverProjection, _, _ = _context_api()
        mutations = (
            ("timestamp", "not-a-time"),
            ("predecessor_role_record_id", "registry-role-invented"),
            ("answers", {}),
        )
        for field, replacement in mutations:
            with self.subTest(field=field):
                original = self.source.roles["builder-a"][field]
                self.source.roles["builder-a"][field] = replacement
                self.addCleanup(
                    self.source.roles["builder-a"].__setitem__, field, original
                )
                with self.assertRaises(ProtocolRefusal) as raised:
                    ContextTurnoverProjection(
                        self.root, "builder-a", source=self.source
                    ).project()
                self.assertEqual("context_role_invalid", raised.exception.code)
                self.source.roles["builder-a"][field] = original

    def test_turnover_accepts_landed_d2_unicode_answers_without_rendering_them(self) -> None:
        """Catches E2 narrowing D2 answers that never enter the ASCII projection."""

        _, ContextTurnoverProjection, _, _ = _context_api()
        self.source.roles["builder-a"]["answers"]["repo"] = "floati cafe\N{COMBINING ACUTE ACCENT}"
        projection = ContextTurnoverProjection(
            self.root, "builder-a", source=self.source
        )

        artifact = projection.project()

        self.assertNotIn("answers", artifact["role_provenance"])
        projection.to_json().encode("ascii")

    def test_renderer_refuses_shallow_state_path_or_malformed_role_provenance(self) -> None:
        """Catches malformed direct artifacts escaping as crashes or trusted provenance."""

        _, ContextTurnoverProjection, _, render = _context_api()
        for mutation in ("state_file", "role_record_id"):
            with self.subTest(mutation=mutation):
                artifact = ContextTurnoverProjection(
                    self.root, "builder-a", source=self.source
                ).project()
                if mutation == "state_file":
                    artifact["state_file"] = "/STATE.md"
                else:
                    artifact["role_provenance"]["role_record_id"] = "registry-role-invented"

                with self.assertRaises(ProtocolRefusal) as raised:
                    render(artifact)

                self.assertEqual("context_output_invalid", raised.exception.code)


class ContextCliRegistrationTests(unittest.TestCase):
    def test_registration_exposes_only_the_ruled_context_shapes(self) -> None:
        """Catches the dark seam adding hidden configuration or mutation flags."""

        _, _, register_cli, _ = _context_api()
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        register_cli(commands)

        for subcommand in ("status", "turnover"):
            with self.subTest(subcommand=subcommand):
                parsed = parser.parse_args(
                    [
                        "context",
                        subcommand,
                        "--root",
                        "\x2ftmp/fleet",
                        "--as",
                        "builder-a",
                        "--json",
                    ]
                )
                self.assertEqual("context", parsed.command)
                self.assertEqual(subcommand, parsed.context_command)
                self.assertEqual("\x2ftmp/fleet", parsed.root)
                self.assertEqual("builder-a", parsed.actor)
                self.assertTrue(parsed.json)
                self.assertTrue(callable(parsed.handler))

        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "context",
                    "turnover",
                    "--root",
                    "\x2ftmp/fleet",
                    "--as",
                    "builder-a",
                    "--profile",
                    "invented",
                ]
            )
        help_text = parser.format_help()
        self.assertNotIn("DRAFT -", help_text)


if __name__ == "__main__":
    unittest.main()
