from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.role_templates import (
    SHIPPED_ROLE_NAMES,
    load_role_template,
    load_shipped_role_templates,
    parse_role_template,
)
from tests.schema_validation import validate_json_schema


def template_payload(role: str = "architect") -> dict[str, object]:
    return {
        "schema_version": 0,
        "template_version": 1,
        "role": role,
        "duties": ["Keep the fleet aligned with the owner mandate."],
        "decision_rights": ["Rule scoped implementation choices."],
        "stops": ["Stop when owner-tier authority is required."],
        "fences": ["Never cross a declared repository boundary."],
        "cadence": "envelope-per-row",
        "questions": [
            {"key": "repo", "ask": "Which repository does this seat own?"},
            {
                "key": "reports_to",
                "ask": "Which node does it report to?",
                "default": "<architect>",
            },
        ],
    }


class RoleTemplateTests(unittest.TestCase):
    def test_stale_fleet_map_field_refuses_template_construction(self) -> None:
        """Catches a future boot projection reading cached topology from a template."""
        payload = template_payload()
        payload["fleet_map"] = {"architect": "retired-node"}

        with self.assertRaises(ProtocolRefusal) as raised:
            parse_role_template(payload)

        self.assertEqual("role_template_invalid", raised.exception.code)

    def test_six_shipped_archetypes_are_typed_schema_valid_and_unstamped(self) -> None:
        """Catches a shipped role bypassing the typed contract or copy gate."""
        library = load_shipped_role_templates(Path("roles/shipped"))

        self.assertEqual(
            ("architect", "builder", "github-manager", "researcher", "reviewer", "sre"),
            tuple(library),
        )
        self.assertEqual(SHIPPED_ROLE_NAMES, tuple(library))
        for name, template in library.items():
            with self.subTest(role=name):
                path = Path("roles/shipped") / f"{name}.json"
                raw = json.loads(path.read_text(encoding="utf-8"))
                validate_json_schema(raw, Path("schemas/v0/role-template.schema.json"))
                self.assertEqual(raw, template.record)
                self.assertEqual(64, len(template.digest))
                copy_lines = [
                    *template.duties,
                    *template.decision_rights,
                    *template.stops,
                    *template.fences,
                    *(question.ask for question in template.questions),
                ]
                self.assertTrue(all(line and not line.startswith("DRAFT - ") for line in copy_lines))

    def test_shipped_loader_reads_exact_roster_and_ignores_unlisted_files(self) -> None:
        """Catches directory scanning turning an arbitrary file into a shipped role."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in SHIPPED_ROLE_NAMES:
                (root / f"{name}.json").write_text(
                    json.dumps(template_payload(name)), encoding="utf-8"
                )
            (root / "unlisted.json").write_text("not-json", encoding="utf-8")

            library = load_shipped_role_templates(root)

        self.assertEqual(
            ("architect", "builder", "github-manager", "researcher", "reviewer", "sre"),
            tuple(library),
        )

    def test_duplicate_question_keys_refuse_ambiguous_interviews(self) -> None:
        """Catches one answer being silently reused for two declared questions."""
        payload = template_payload()
        payload["questions"] = [
            {"key": "repo", "ask": "First repository question."},
            {"key": "repo", "ask": "Second repository question."},
        ]

        with self.assertRaises(ProtocolRefusal) as raised:
            parse_role_template(payload)

        self.assertEqual("role_template_invalid", raised.exception.code)

    def test_terminal_unsafe_template_copy_refuses(self) -> None:
        """Catches control characters reaching generated prompts through a template."""
        payload = template_payload()
        payload["duties"] = ["unsafe\x1bcopy"]

        with self.assertRaises(ProtocolRefusal) as raised:
            parse_role_template(payload)

        self.assertEqual("role_template_invalid", raised.exception.code)

    def test_digest_is_canonical_and_binds_template_version(self) -> None:
        """Catches role records binding byte order instead of typed template content."""
        payload = template_payload()
        reordered = {key: payload[key] for key in reversed(tuple(payload))}
        newer = dict(payload)
        newer["template_version"] = 2

        first = parse_role_template(payload)
        second = parse_role_template(reordered)
        third = parse_role_template(newer)

        self.assertEqual(first.digest, second.digest)
        self.assertNotEqual(first.digest, third.digest)

    def test_symlink_template_refuses_before_read(self) -> None:
        """Catches an explicit template path being redirected after selection."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text(json.dumps(template_payload()), encoding="utf-8")
            link = root / "role.json"
            link.symlink_to(target)

            with self.assertRaises(ProtocolRefusal) as raised:
                load_role_template(link)

        self.assertEqual("role_template_path_invalid", raised.exception.code)


MEASURED_ACK_SLA_MINUTES = 45
# MEASURED 2026-09-04 on the live fleet ledger's acknowledgment receipts: 601
# latencies across six active nodes, median 173s, p90 2630s (43.83min), max
# ~45h (a dark seat). Declared norm = p90 rounded up to the nearest quarter
# hour = 45. Re-measurement amends this number; it is never invented.


class AckSlaDeclarationTests(unittest.TestCase):
    """ACK-1-F1: the role template declares the measured acknowledgment SLA."""

    def test_shipped_templates_declare_the_measured_ack_sla(self) -> None:
        library = load_shipped_role_templates(Path("roles/shipped"))

        self.assertEqual(6, len(library))
        for name, template in library.items():
            with self.subTest(role=name):
                self.assertEqual(2, template.template_version)
                self.assertEqual(
                    MEASURED_ACK_SLA_MINUTES, template.ack_sla_minutes
                )

    def test_ack_sla_minutes_is_optional_and_round_trips(self) -> None:
        payload = template_payload()
        payload["ack_sla_minutes"] = MEASURED_ACK_SLA_MINUTES

        declared = parse_role_template(payload)
        undeclared = parse_role_template(template_payload())

        self.assertEqual(MEASURED_ACK_SLA_MINUTES, declared.ack_sla_minutes)
        self.assertIn("ack_sla_minutes", declared.record)
        self.assertIsNone(undeclared.ack_sla_minutes)
        self.assertNotIn("ack_sla_minutes", undeclared.record)

    def test_ack_sla_minutes_changes_the_canonical_digest(self) -> None:
        declared = parse_role_template(
            {**template_payload(), "ack_sla_minutes": MEASURED_ACK_SLA_MINUTES}
        )
        undeclared = parse_role_template(template_payload())

        self.assertNotEqual(declared.digest, undeclared.digest)

    def test_ack_sla_minutes_type_and_range_refuse(self) -> None:
        for bad in ("45", 0, -5, True, 1_000_001, 4.5):
            with self.subTest(value=bad):
                payload = template_payload()
                payload["ack_sla_minutes"] = bad

                with self.assertRaises(ProtocolRefusal) as raised:
                    parse_role_template(payload)

                self.assertEqual("role_template_invalid", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
