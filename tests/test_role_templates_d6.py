from __future__ import annotations

import json
import unittest
from pathlib import Path

from floati.role_templates import SHIPPED_ROLE_NAMES, load_shipped_role_templates
from tests.schema_validation import validate_json_schema


EXPECTED_ROLES = {
    "github-manager": {
        "duties": [
            "Keep the named repository's pull requests, issues, and labels current and truthful.",
            "Relay review verdicts and merge states onto the bus with their receipts.",
        ],
        "decision_rights": [
            "Choose triage order, labels, and reviewer routing inside the named repository.",
            "Close conversations that are resolved by a merged change or a recorded ruling.",
        ],
        "stops": [
            "Stop before any merge, force operation, branch deletion, or permission change the owner did not name."
        ],
        "fences": [
            "Never merge without a named approval from outside this seat.",
            "Never rewrite published history or touch CI credentials and secrets.",
        ],
        "questions": [
            {"key": "repo", "ask": "Which repository does this manager tend?"},
            {"key": "never_touch", "ask": "What must this manager never modify?"},
            {"key": "reports_to", "ask": "Which node receives this manager's reports?"},
        ],
    },
    "reviewer": {
        "duties": [
            "Review assigned rows independently against their written contract and evidence.",
            "Return a verdict that names what was checked, what was not, and why.",
        ],
        "decision_rights": [
            "Accept, reject, or bound a row's evidence within the written contract.",
            "Demand a re-round when evidence does not support its claim.",
        ],
        "stops": [
            "Stop when a verdict would require authority or context the contract does not grant."
        ],
        "fences": [
            "Never review work this seat produced and call the review independent.",
            "Never soften a red finding to keep a queue moving.",
        ],
        "questions": [
            {"key": "repo", "ask": "Which repository does this reviewer serve?"},
            {"key": "never_touch", "ask": "What must this reviewer never modify?"},
            {"key": "reports_to", "ask": "Which node receives this reviewer's verdicts?"},
        ],
    },
    "researcher": {
        "duties": [
            "Answer assigned questions with findings tied to sources that actually state them.",
            "Separate what was measured from what was inferred in every report.",
        ],
        "decision_rights": [
            "Choose sources, search paths, and depth inside the assigned question.",
            "Declare a question unanswerable when the evidence does not exist.",
        ],
        "stops": [
            "Stop before acting on a finding; research reports, it does not execute."
        ],
        "fences": [
            "Never present an uncited claim as a finding.",
            "Never run code or commands obtained from a researched source.",
        ],
        "questions": [
            {"key": "repo", "ask": "Which repository or corpus does this researcher work from?"},
            {"key": "never_touch", "ask": "What must this researcher never modify?"},
            {"key": "reports_to", "ask": "Which node receives this researcher's findings?"},
        ],
    },
}

EXPECTED_GUIDE = """A role template is a typed contract, not a personality. To author one:

1. **Fields.** `duties` (what the seat does), `decision_rights` (what it may
   decide alone), `stops` (when it must wait), `fences` (what it must never
   do), `cadence`, `questions` (what the operator must answer before the role
   binds). Every list entry is one sentence, plain ASCII, no trailing stamp.
2. **Voice.** Duties start with a verb. Fences start with "Never". Stops start
   with "Stop". One claim per sentence; a sentence an operator could misread
   is a defect.
3. **Questions.** Ask only what the template cannot know: the territory
   (`repo`), the hard boundary (`never_touch`), the reporting line
   (`reports_to`). Answers must exactly match declared questions — extra or
   missing answers refuse.
4. **Versioning.** Shipped templates are immutable at a version; any content
   change bumps `template_version` and re-digests. A role record binds the
   version and digest it was assigned under.
5. **Tests.** Add the file to the shipped-roster pin (the loader reads an
   exact roster, never a directory scan), validate against
   `role-template.schema.json`, and keep the no-DRAFT copy fence green.
"""


class D6RoleTemplateCopyTests(unittest.TestCase):
    def test_exact_shipped_roster_includes_the_three_d6_archetypes(self) -> None:
        expected = (
            "architect",
            "builder",
            "github-manager",
            "researcher",
            "reviewer",
            "sre",
        )

        self.assertEqual(expected, SHIPPED_ROLE_NAMES)
        self.assertEqual(expected, tuple(load_shipped_role_templates("roles/shipped")))

    def test_d6_archetypes_match_the_voice_passed_copy_and_schema(self) -> None:
        schema = Path("schemas/v0/role-template.schema.json")
        for role, copy in EXPECTED_ROLES.items():
            with self.subTest(role=role):
                path = Path("roles/shipped") / f"{role}.json"
                self.assertTrue(path.is_file(), f"missing shipped D6 role: {role}")
                record = json.loads(path.read_text(encoding="utf-8"))
                expected = {
                    "schema_version": 0,
                    "template_version": 2,
                    "role": role,
                    **copy,
                    "cadence": "envelope-per-row",
                    "ack_sla_minutes": 45,
                }
                self.assertEqual(expected, record)
                validate_json_schema(record, schema)
                self.assertNotIn("DRAFT -", json.dumps(record, ensure_ascii=True))

    def test_role_authoring_guide_is_the_verbatim_copy_pack_section(self) -> None:
        path = Path("docs/design/ROLE-AUTHORING.md")
        self.assertTrue(path.is_file(), "missing D6 authoring guide")
        self.assertEqual(
            EXPECTED_GUIDE,
            path.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
