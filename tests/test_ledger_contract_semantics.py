"""A ledger freeze follows accepted records, rather than unrelated runtime work."""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import tempfile
import unittest
from pathlib import Path


class LedgerContractSemanticControls(unittest.TestCase):
    def setUp(self):
        self.tool = importlib.import_module("scripts.ledger_contract")

    def assert_guard_property(self, change, *, passes, extra_sources=None):
        """Exercise the same property before and after a synthetic first cut."""
        for phase in ("preparation", "cut"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                (root / "floati").mkdir()
                (root / "schemas/v0").mkdir(parents=True)
                (root / "bundle/c7.2").mkdir(parents=True)
                (root / "bundle/c7.2/bundle-index.json").write_text(
                    '{"schema_version":"c7.2-candidate"}\n',
                )
                (root / "floati/records.py").write_text(
                    '_SPECS = {"sample": ("sample-", {"kind", "value"})}\n'
                    'def validate(row):\n    return row["kind"] in _SPECS\n',
                )
                (root / "floati/cli.py").write_text(
                    'def main():\n    print("The operation completed.")\n',
                )
                schema_path = root / "schemas/v0/sample.schema.json"
                schema_path.write_text(json.dumps({
                    "type": "object", "required": ["kind", "value"],
                    "properties": {
                        "kind": {"const": "sample"},
                        "value": {"enum": ["old", "other"]},
                    },
                }) + "\n")
                for relative, text in (extra_sources or {}).items():
                    (root / relative).write_text(text)
                if phase == "preparation":
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(0, self.tool.main(["--root", str(root), "prepare"]))
                else:
                    self.tool.cut(root, "c7.3", self.tool.inventory(root))
                before = self.contract_bytes(root)
                self.assertEqual("prepared" if phase == "preparation" else "unchanged",
                                 self.tool.check(root)["classification"])
                change(root)
                if passes:
                    self.assertEqual("prepared" if phase == "preparation" else "unchanged",
                                     self.tool.check(root)["classification"])
                else:
                    with self.assertRaises(self.tool.ContractRefusal) as caught:
                        self.tool.check(root)
                    self.assertEqual(
                        "ledger_contract_preparation_drift" if phase == "preparation"
                        else "ledger_contract_recut_required", caught.exception.code,
                    )
                self.assertEqual(before, self.contract_bytes(root),
                                 "A check must not regenerate pins or mutate a frozen bundle")

    def contract_bytes(self, root):
        paths = list((root / "bundle").rglob("*"))
        preparation = root / self.tool.PREPARATION
        if preparation.exists():
            paths.append(preparation)
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in paths if path.is_file()}

    def test_comments_and_whitespace_do_not_change_the_ledger_contract(self):
        def change(root):
            path = root / "floati/records.py"
            path.write_text("# Runtime maintenance.\n\n" + path.read_text().replace(
                'return row["kind"] in _SPECS', 'return  row["kind"]  in  _SPECS',
            ))
        self.assert_guard_property(change, passes=True)

    def test_a_helper_with_no_kind_or_validator_needs_no_regeneration(self):
        self.assert_guard_property(lambda root: (root / "floati/helper.py").write_text(
            'def elapsed(start, end):\n    return end - start\n',
        ), passes=True)

    def test_a_sibling_helper_in_a_writer_module_needs_no_regeneration(self):
        def change(root):
            path = root / "floati/writer.py"
            path.write_text(path.read_text() +
                            '\ndef elapsed(start, end):\n    return end - start\n')
        self.assert_guard_property(change, passes=True, extra_sources={
            "floati/writer.py":
                'def write():\n    return {"kind": "sample", "value": "old"}\n',
        })

    def test_cli_output_text_needs_no_regeneration(self):
        self.assert_guard_property(lambda root: (root / "floati/cli.py").write_text(
            'def main():\n    print("Completed successfully.")\n',
        ), passes=True)

    def test_a_new_literal_kind_requires_ratification_or_recut(self):
        self.assert_guard_property(lambda root: (root / "floati/new_writer.py").write_text(
            'def write():\n    return {"kind": "new_literal_record", "value": 1}\n',
        ), passes=False)

    def test_removing_a_kind_requires_recut(self):
        self.assert_guard_property(lambda root: (root / "floati/records.py").write_text(
            '_SPECS = {}\n'
            'def validate(row):\n    return row["kind"] in _SPECS\n',
        ), passes=False)

    def test_a_new_f_string_kind_cannot_evade_the_guard(self):
        self.assert_guard_property(lambda root: (root / "floati/new_writer.py").write_text(
            'PREFIX = "new"\n'
            'def write(suffix):\n    return {"kind": f"{PREFIX}_{suffix}", "value": 1}\n',
        ), passes=False)

    def test_a_new_concatenated_kind_cannot_evade_the_guard(self):
        self.assert_guard_property(lambda root: (root / "floati/new_writer.py").write_text(
            'PREFIX = "new_"\n'
            'def write(suffix):\n    return {"kind": PREFIX + suffix, "value": 1}\n',
        ), passes=False)

    def test_changing_a_computed_kind_helper_is_contract_drift(self):
        self.assert_guard_property(lambda root: (root / "floati/writer.py").write_text(
            'def record_name():\n    return "new_computed_record"\n'
            'def write():\n    return {"kind": record_name(), "value": 1}\n',
        ), passes=False, extra_sources={
            "floati/writer.py":
                'def record_name():\n    return "sample"\n'
                'def write():\n    return {"kind": record_name(), "value": 1}\n',
        })

    def test_changing_an_imported_computed_kind_helper_is_contract_drift(self):
        self.assert_guard_property(lambda root: (root / "floati/names.py").write_text(
            'def record_name():\n    return "new_imported_record"\n',
        ), passes=False, extra_sources={
            "floati/names.py": 'def record_name():\n    return "sample"\n',
            "floati/writer.py":
                'from .names import record_name\n'
                'def write():\n    return {"kind": record_name(), "value": 1}\n',
        })

    def test_a_new_caller_of_a_generic_writer_cannot_introduce_an_unpinned_kind(self):
        self.assert_guard_property(lambda root: (root / "floati/caller.py").write_text(
            'from .writer import emit\n'
            'def publish(suffix):\n    return emit("new_" + suffix)\n',
        ), passes=False, extra_sources={
            "floati/writer.py":
                'def emit(name):\n    return {"kind": name, "value": 1}\n',
        })

    def test_a_local_assignment_feeding_a_generic_writer_is_contract_drift(self):
        self.assert_guard_property(lambda root: (root / "floati/caller.py").write_text(
            'from .writer import emit\n'
            'def publish(suffix):\n'
            '    name = "new_" + suffix\n'
            '    return emit(name)\n',
        ), passes=False, extra_sources={
            "floati/writer.py":
                'def emit(name):\n    return {"kind": name, "value": 1}\n',
            "floati/caller.py":
                'from .writer import emit\n'
                'def publish(suffix):\n'
                '    name = "sample"\n'
                '    return emit(name)\n',
        })

    def test_aliases_and_reexports_do_not_hide_computed_writer_arguments(self):
        self.assert_guard_property(lambda root: (root / "floati/caller.py").write_text(
            'from .bridge import publish as deliver\n'
            'def perform(suffix):\n    return deliver("new_" + suffix)\n',
        ), passes=False, extra_sources={
            "floati/writer.py":
                'def emit(name):\n    return {"kind": name, "value": 1}\n',
            "floati/bridge.py": 'from .writer import emit as publish\n',
            "floati/caller.py":
                'from .bridge import publish as deliver\n'
                'def perform(suffix):\n    return deliver("sample")\n',
        })

    def test_redirecting_an_import_between_selected_validators_is_contract_drift(self):
        self.assert_guard_property(lambda root: (root / "floati/consumer.py").write_text(
            'from .helpers import validate_new as accept\n'
            'def validate(row):\n    return accept(row)\n',
        ), passes=False, extra_sources={
            "floati/helpers.py":
                'def validate_old(row):\n    return row["value"] == "old"\n'
                'def validate_new(row):\n    return row["value"] in {"old", "new"}\n',
            "floati/consumer.py":
                'from .helpers import validate_old as accept\n'
                'def validate(row):\n    return accept(row)\n',
        })

    def test_a_kindless_journal_new_event_value_is_contract_drift(self):
        def add_dynamic_writer(root):
            before = self.tool.inventory(root)
            path = root / "floati/records.py"
            path.write_text(path.read_text() + '\ndef write(name):\n    return {"event": name}\n')
            self.assertNotEqual(before["sources"], self.tool.inventory(root)["sources"])

        for case, change, extra_sources in (
            ("new-event-value", lambda root: (root / "floati/journal.py").write_text(
                '_EVENTS = {"opened", "closed"}\n'
                'def validate(row):\n    return row["event"] in _EVENTS\n',
            ), {"floati/journal.py":
                '_EVENTS = {"opened"}\n'
                'def validate(row):\n    return row["event"] in _EVENTS\n'}),
            ("dynamic-event-writer", add_dynamic_writer, None),
        ):
            with self.subTest(case=case):
                self.assert_guard_property(change, passes=False, extra_sources=extra_sources)

    def test_tightening_a_schema_requires_recut(self):
        def change(root):
            path = root / "schemas/v0/sample.schema.json"
            schema = json.loads(path.read_text())
            schema["properties"]["value"]["enum"] = ["old"]
            path.write_text(json.dumps(schema) + "\n")
        self.assert_guard_property(change, passes=False)

    def test_a_module_level_computed_writer_argument_is_contract_drift(self):
        self.assert_guard_property(lambda root: (root / "floati/writer.py").write_text(
            'def emit(name):\n    return {"kind": name}\n'
            'emit("new_" + "computed_record")\n',
        ), passes=False, extra_sources={
            "floati/writer.py": 'def emit(name):\n    return {"kind": name}\n',
        })

    def test_an_unrelated_sibling_method_needs_no_regeneration(self):
        self.assert_guard_property(lambda root: (root / "floati/writer.py").write_text(
            'class Writer:\n'
            '    def write(self):\n        return {"kind": "sample"}\n'
            '    def elapsed(self, start, end):\n        return end - start\n',
        ), passes=True, extra_sources={
            "floati/writer.py":
                'class Writer:\n'
                '    def write(self):\n        return {"kind": "sample"}\n',
        })

    def test_a_module_qualified_computed_kind_helper_is_contract_drift(self):
        self.assert_guard_property(lambda root: (root / "floati/names.py").write_text(
            'def record_name():\n    return "new_computed_record"\n',
        ), passes=False, extra_sources={
            "floati/names.py": 'def record_name():\n    return "sample"\n',
            "floati/writer.py":
                'import floati.names\n'
                'def write():\n    return {"kind": floati.names.record_name()}\n',
        })

    def test_keyword_construction_and_update_cannot_hide_new_computed_kinds(self):
        for body in ('return dict(kind=f"new_{suffix}")',
                     'row.update(kind="new_" + suffix)\n    return row'):
            with self.subTest(body=body):
                self.assert_guard_property(lambda root: (root / "floati/writer.py").write_text(
                    'def write(row, suffix):\n    ' + body + '\n',
                ), passes=False)


if __name__ == "__main__":
    unittest.main()
