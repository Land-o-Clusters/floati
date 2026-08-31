from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.foreign_bus_survey import ForeignBusSurvey
from floati.root import FloatiRoot


class ForeignBusSurveyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.ours = FloatiRoot.open_direct_home(self.base / "ours", create=True)
        (self.ours.path / "nodes" / public_ids.builder('a')).mkdir(parents=True)
        self.foreign = self.base / ".other-bus"
        self.foreign.mkdir()
        (self.foreign / "events.jsonl").write_bytes(b"")
        self.generic = self.base / "generic-agent-root"
        self.generic.mkdir()
        (self.generic / "ledger.jsonl").write_bytes(b"opaque foreign bytes\n")
        self.not_a_bus = self.base / "photos"
        self.not_a_bus.mkdir()
        (self.not_a_bus / "image.txt").write_text("not a bus\n", encoding="utf-8")
        self.outside = self.base / "outside-search"
        self.outside.mkdir()
        hidden = self.outside / "hidden-bus"
        hidden.mkdir()
        (hidden / "events.jsonl").write_bytes(b"")
        self.declared = self.base / "declared-roots.json"
        self.declared.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "roots": [{
                        "bus_id": "ours",
                        "root": str(self.ours.path),
                        "architect_node": public_ids.builder('a'),
                        "downstream": [],
                    }],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.hooks = self.base / "hooks.json"
        self.hooks.write_text(
            json.dumps(
                {
                    "hooks": [{
                        "event": "Stop",
                        "command": str(self.foreign / "toolkit" / "waiter.py"),
                    }]
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.targets = self.base / "targets.json"
        self.targets.write_text(
            json.dumps({str(self.ours.path): public_ids.builder('a')}, sort_keys=True),
            encoding="utf-8",
        )

    def survey(self, *search_paths: Path) -> ForeignBusSurvey:
        return ForeignBusSurvey(
            self.declared,
            search_paths=search_paths,
            hooks_path=self.hooks,
            targets_paths=[self.targets],
        )

    def test_survey_reports_only_bus_shapes_inside_explicit_and_declared_parent_paths(self) -> None:
        """Catches home-wide recursion or arbitrary-directory false positives."""
        artifact = self.survey().run()

        self.assertEqual(
            [str(self.foreign.resolve()), str(self.generic.resolve())],
            [entry["root"] for entry in artifact["foreign_buses"]],
        )
        serialized = json.dumps(artifact)
        self.assertNotIn(str(self.not_a_bus), serialized)
        self.assertNotIn(str(self.outside / "hidden-bus"), serialized)
        self.assertNotIn(str(self.ours.path), [entry["root"] for entry in artifact["foreign_buses"]])

    def test_survey_classifies_apparent_schema_without_reading_foreign_ledgers(self) -> None:
        """Catches schema claims derived from consuming untrusted foreign records."""
        artifact = self.survey().run()
        schemas = {entry["root"]: entry["apparent_schema"] for entry in artifact["foreign_buses"]}

        self.assertEqual("floati-direct-v0-shaped", schemas[str(self.foreign.resolve())])
        self.assertEqual("agent-bus-jsonl-shaped", schemas[str(self.generic.resolve())])
        self.assertEqual(b"opaque foreign bytes\n", (self.generic / "ledger.jsonl").read_bytes())

    def test_survey_correlates_foreign_waiter_with_our_declared_workspace_read_only(self) -> None:
        """Catches missing cross-bus hook warning or a write to foreign registration state."""
        artifact = self.survey().run()
        foreign = next(
            entry for entry in artifact["foreign_buses"]
            if entry["root"] == str(self.foreign.resolve())
        )

        self.assertEqual(
            [{
                "hooks_file": str(self.hooks.resolve()),
                "targets_file": str(self.targets.resolve()),
                "workspace": str(self.ours.path),
            }],
            foreign["foreign_waiter_bindings"],
        )

    def test_survey_is_byte_for_byte_read_only_across_all_inputs(self) -> None:
        """Catches survey creating locks, receipts, acknowledgements, or registrations."""
        before = self._snapshot()

        self.survey(self.base).run()

        self.assertEqual(before, self._snapshot())

    def test_relative_search_hook_or_target_paths_refuse_without_scanning(self) -> None:
        """Catches ambient current-directory or home fallback behavior."""
        cases = (
            {"search_paths": [Path("relative")]},
            {"hooks_path": Path("hooks.json")},
            {"targets_paths": [Path("targets.json")]},
        )
        for index, overrides in enumerate(cases):
            with self.subTest(case=index):
                arguments = {
                    "search_paths": [],
                    "hooks_path": self.hooks,
                    "targets_paths": [self.targets],
                    **overrides,
                }
                with self.assertRaises(ProtocolRefusal):
                    ForeignBusSurvey(self.declared, **arguments).run()

    def _snapshot(self) -> dict[str, tuple[str, bytes]]:
        return {
            str(path.relative_to(self.base)): (
                "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file",
                b"" if path.is_symlink() or path.is_dir() else path.read_bytes(),
            )
            for path in self.base.rglob("*")
        }


if __name__ == "__main__":
    unittest.main()
