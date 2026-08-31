from __future__ import annotations

import unittest
from pathlib import Path
from tests.private_artifacts import require_private_artifact


class HM3IContractTests(unittest.TestCase):
    def test_charter_amendment_is_mirrored_with_exact_authority(self) -> None:
        design = Path("docs/DESIGN.md").read_text()
        spec = Path("docs/SPEC-DRAFT.md").read_text()
        for text in (design, spec):
            self.assertIn("bounded local run graph", text.lower())
            self.assertIn("a111202b228d34c2b371bcc5e2c4798206474439", text)
            self.assertIn(
                "no model-authored graph mutation without a durable plan_amendment",
                text,
            )
            self.assertIn("never the reasoning framework", text)

    def test_item_ten_and_item_eleven_docs_state_local_coordinates_without_publication_claims(self) -> None:
        require_private_artifact(self, 'docs/PUBLICATION-CHECKLIST.md')

        spec = Path("docs/SPEC-DRAFT.md").read_text()
        checklist = Path("docs/PUBLICATION-CHECKLIST.md").read_text()
        for literal in (
            "runs/events.jsonl",
            "FLOATI.toml",
            "plan_admission",
            "attempt_harness_session_bound",
            "supervisor_orphaned",
            "exact candidate",
            "C7.1 candidate read bundle",
            "bundle/c7.1/schema-catalog.json",
            "approvals: \"excluded-c7.1\"",
            "conflicting_binding",
            "auxiliary_sources",
            "reprojects those exact captured bytes",
        ):
            self.assertIn(literal, spec)
        self.assertIn("does not publish, install,", spec)
        self.assertIn("deploy, or activate", spec)
        self.assertIn("HM3I_BRIEF.md", checklist)
        self.assertIn("exact-candidate local evidence", checklist)
        self.assertIn("does not publish, install, deploy, activate", checklist)
        self.assertIn("completed Items 1–11", checklist)
        self.assertIn("post-HM3I C7.2 sibling package", checklist)
        self.assertIn("segment_id", checklist)
        self.assertIn("segment_kind", checklist)
        self.assertNotIn("post-Item-11, prepublication C7.2 debt", checklist)
        self.assertNotIn("`relation`", checklist)

    def test_item_nine_decision_boundary_is_bounded_in_the_item_ten_draft(self) -> None:
        """The Item 10 draft names Item 9's actual durable boundary without extending Item 11."""
        spec = Path("docs/SPEC-DRAFT.md").read_text()
        for literal in (
            "repositories/<repository-coordinate>/decisions.jsonl",
            "closed durable taxonomy",
            "injected read-only repository/SHA/path resolver",
            "`operator` or `architect` authority",
            "legacy or unavailable binding proof fails closed",
            "entire validated record except that digest field itself",
            "accepted-frame physical order",
        ):
            self.assertIn(literal, spec)


if __name__ == "__main__":
    unittest.main()
