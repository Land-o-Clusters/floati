from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.errors import ProtocolRefusal, UNNAMED_REMEDY
from floati.node_wizard import NodeWizard
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_node_wizard import RecordingBackend


class SurveyOfferWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.output = io.StringIO()
        self.backend = RecordingBackend(self.output)
        ids = iter(("a" * 32, "b" * 32, "c" * 32, "d" * 32))
        self.wizard = NodeWizard(
            self.root,
            self.backend,
            id_factory=lambda: next(ids),
            now=lambda: datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc),
        )

    def _plant_undeclared_sibling(self) -> Path:
        sibling = self.base / "other-bus"
        sibling.mkdir()
        (sibling / "events.jsonl").write_bytes(b"")
        return sibling.resolve()

    def test_plain_wizard_prompts_mention_survey_when_undeclared_bus_in_scope(self) -> None:
        """Catches the node-add wizard skipping the survey offer beside an undeclared bus."""
        sibling = self._plant_undeclared_sibling()
        plain_input = io.StringIO("alpha\nCodex\npermanent\n\nyes\nyes\n")

        result = self.wizard.add_plain(plain_input, self.output)
        rendered = self.output.getvalue().lower()

        self.assertIn("survey", rendered)
        self.assertIn("adopt", rendered)
        self.assertIn(str(sibling), rendered)
        self.assertIn("survey", result)
        self.assertTrue(result["survey"]["foreign_buses"])
        self.assertIsNotNone(self.backend.added)

    def test_plain_wizard_refuses_before_commit_when_adopt_is_declined(self) -> None:
        """Catches node add committing beside an undeclared bus without adopt consent."""
        sibling = self._plant_undeclared_sibling()
        before = (sibling / "events.jsonl").read_bytes()
        plain_input = io.StringIO("alpha\nCodex\npermanent\n\nyes\nno\n")

        with self.assertRaises(ProtocolRefusal) as raised:
            self.wizard.add_plain(plain_input, self.output)

        self.assertEqual("wizard_undeclared_bus_not_adopted", raised.exception.code)
        self.assertIsNone(self.backend.added)
        self.assertEqual(before, (sibling / "events.jsonl").read_bytes())

    def test_plan_flags_run_the_same_survey_and_adopt_without_prompts(self) -> None:
        """Catches a plan object that cannot carry survey and adopt as keys."""
        sibling = self._plant_undeclared_sibling()

        result = self.wizard.add_from_plan(
            {
                "node": "alpha",
                "harness": "Codex",
                "lifetime": "permanent",
                "survey": True,
                "adopt": True,
            },
            self.output,
        )
        rendered = self.output.getvalue()

        self.assertNotIn("run read-only survey?", rendered)
        self.assertNotIn("adopt and continue", rendered)
        self.assertIn("survey", result)
        self.assertEqual(
            str(sibling),
            result["survey"]["foreign_buses"][0]["root"],
        )
        self.assertTrue(result["adopted"])
        self.assertIsNotNone(self.backend.added)

    def test_plan_adopt_false_refuses_before_commit(self) -> None:
        """Catches a plan with adopt false still committing beside an undeclared sibling."""
        sibling = self._plant_undeclared_sibling()
        before = (sibling / "events.jsonl").read_bytes()

        with self.assertRaises(ProtocolRefusal) as raised:
            self.wizard.add_from_plan(
                {
                    "node": "alpha",
                    "harness": "Codex",
                    "lifetime": "permanent",
                    "survey": True,
                    "adopt": False,
                },
                self.output,
            )

        self.assertEqual("wizard_undeclared_bus_not_adopted", raised.exception.code)
        self.assertIsNone(self.backend.added)
        self.assertEqual(before, (sibling / "events.jsonl").read_bytes())

    def test_plan_adopt_not_boolean_names_a_remedy(self) -> None:
        """Catches adopt-not-boolean serving the kind:none placeholder."""

        with self.assertRaises(ProtocolRefusal) as raised:
            self.wizard.add_from_plan(
                {
                    "node": "alpha",
                    "harness": "Codex",
                    "lifetime": "permanent",
                    "adopt": 1,
                },
                self.output,
            )

        self.assertEqual("wizard_input_invalid", raised.exception.code)
        self.assertNotEqual(UNNAMED_REMEDY, raised.exception.remedy)
        self.assertIn("adopt", raised.exception.remedy)
        self.assertIn("boolean", raised.exception.remedy)
        self.assertIsNone(self.backend.added)

    def test_nested_bus_outside_parent_scope_does_not_trigger_survey_offer(self) -> None:
        """Catches a home-style walk offering survey for a bus that is not in scope."""
        nested = self.base / "photos" / "hidden-bus"
        nested.mkdir(parents=True)
        (nested / "events.jsonl").write_bytes(b"")
        plain_input = io.StringIO("alpha\nCodex\npermanent\n\n")

        self.wizard.add_plain(plain_input, self.output)

        self.assertNotIn("survey", self.output.getvalue().lower())
        self.assertIsNotNone(self.backend.added)

    def test_nothing_in_scope_is_typed_absence_none_in_scope(self) -> None:
        """Catches a silent {} when the parent has no undeclared bus."""
        plain_input = io.StringIO("alpha\nCodex\npermanent\n\n")

        result = self.wizard.add_plain(plain_input, self.output)

        self.assertEqual([], result["undeclared_in_scope"])
        self.assertEqual("not_offered", result["survey"])
        self.assertEqual("none_in_scope", result["reason"])
        self.assertIsNotNone(self.backend.added)

    def test_nested_bus_is_typed_absence_nested_out_of_scope(self) -> None:
        """Catches a silent {} when a bus sits nested outside parent scope."""
        nested = self.base / "photos" / "hidden-bus"
        nested.mkdir(parents=True)
        (nested / "events.jsonl").write_bytes(b"")
        plain_input = io.StringIO("alpha\nCodex\npermanent\n\n")

        result = self.wizard.add_plain(plain_input, self.output)

        self.assertEqual([], result["undeclared_in_scope"])
        self.assertEqual("not_offered", result["survey"])
        self.assertEqual("nested_out_of_scope", result["reason"])
        self.assertNotIn("survey", self.output.getvalue().lower())
        self.assertIsNotNone(self.backend.added)


if __name__ == "__main__":
    unittest.main()
