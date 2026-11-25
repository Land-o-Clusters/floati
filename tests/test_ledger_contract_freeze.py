"""The ledger cut must cover the code, and ratification must preserve old records."""
from __future__ import annotations

import copy
import contextlib
import hashlib
import importlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.ledger_contract_candidate import copy_live_contract_inputs
from tests.temp_roots import REAL_TEMP_ROOT


ROOT = Path(__file__).resolve().parents[1]


class LedgerContractFreezeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.find_spec("scripts.ledger_contract")
        self.assertIsNotNone(
            spec, "Every ledger reader and writer needs a versioned, checked cut contract",
        )
        self.tool = importlib.import_module("scripts.ledger_contract")
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "floati").mkdir()
        (self.root / "schemas/v0").mkdir(parents=True)
        (self.root / "bundle/c7.2").mkdir(parents=True)
        (self.root / "bundle/c7.2/bundle-index.json").write_text(
            '{"schema_version":"c7.2-candidate"}\n',
        )
        (self.root / "floati/records.py").write_text(
            '_SPECS = {"sample": ("sample-", {"kind", "value"})}\n'
            'def validate(row):\n    return row["kind"] in _SPECS\n',
        )
        self.schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object", "required": ["kind", "value"],
            "additionalProperties": False,
            "properties": {"kind": {"const": "sample"}, "value": {"enum": ["old"]}},
        }
        self.schema_path = self.root / "schemas/v0/sample.schema.json"
        self.write_schema(self.schema)

    def write_schema(self, schema):
        self.schema_path.write_text(json.dumps(schema, sort_keys=True) + "\n")

    def cut(self):
        return self.tool.cut(self.root, "c7.3", self.tool.inventory(self.root))

    def cut_payload(self, version, name="schemas/v0/sample.schema.json"):
        document = self.tool.load_contract(self.root, version)
        return self.root / document["schema_index"][name]["storage"]

    def assert_refused(self, code, call):
        with self.assertRaises(self.tool.ContractRefusal) as caught:
            call()
        self.assertEqual(code, caught.exception.code)

    def test_inventory_derives_kinds_and_schema_members_from_code(self):
        inventory = self.tool.inventory(self.root)
        self.assertIn("sample", inventory["kinds"])
        self.assertIn("floati/records.py", inventory["sources"])
        self.assertIn("schemas/v0/sample.schema.json", inventory["schemas"])
        (self.root / "floati/other.py").write_text(
            'NEW_KIND = "future_record"\n'
            'def write():\n    return {"kind": NEW_KIND}\n',
        )
        changed = self.tool.inventory(self.root)
        self.assertIn("future_record", changed["kinds"])
        self.assertIn("floati/other.py", changed["sources"])

    def test_inventory_includes_schema_ref_targets_and_legacy_versions(self):
        (self.root / "schemas/v1").mkdir()
        (self.root / "schemas/v1/common.schema.json").write_text('{"type":"string"}\n')
        schema = copy.deepcopy(self.schema)
        schema["properties"]["value"] = {"$ref": "../v1/common.schema.json"}
        self.write_schema(schema)
        members = self.tool.inventory(self.root)["schemas"]
        self.assertEqual(2, len(members))
        self.assertIn("schemas/v1/common.schema.json", members)

    def test_cut_refuses_any_unpinned_member_before_creating_bundle(self):
        for section in ("sources", "schemas", "kinds"):
            with self.subTest(section=section):
                pins = self.tool.inventory(self.root)
                pins[section].pop(next(iter(pins[section])))
                self.assert_refused("ledger_contract_unpinned_member", lambda: self.tool.cut(
                    self.root, "c7.3", pins,
                ))
                self.assertFalse((self.root / "bundle/c7.3").exists())

    def test_cut_refuses_stale_pins(self):
        pins = self.tool.inventory(self.root)
        self.schema_path.write_text(self.schema_path.read_text() + "\n")
        self.assert_refused("ledger_contract_unpinned_member", lambda: self.tool.cut(
            self.root, "c7.3", pins,
        ))

    def command_artifact(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = self.tool.main(["--root", str(self.root), *arguments])
        return exit_code, json.loads(output.getvalue())

    def test_drift_refusals_name_changed_schema_and_added_source(self):
        """An operator can act on preparation, cut and stale-pin refusals."""
        pins = self.tool.inventory(self.root)
        pins_path = self.root / "candidate-pins.json"
        pins_path.write_text(json.dumps(pins))
        self.assertEqual(0, self.command_artifact("prepare")[0])
        self.schema_path.write_text(self.schema_path.read_text() + "\n")
        (self.root / "floati/extra.py").write_text(
            'def validate_extra(row):\n    return isinstance(row, dict)\n',
        )
        expected = {
            "kinds": {"added": [], "removed": [], "changed": []},
            "schemas": {"added": [], "removed": [],
                        "changed": ["schemas/v0/sample.schema.json"]},
            "sources": {"added": ["floati/extra.py"], "removed": [], "changed": []},
        }
        for command, code in ((["check"], "preparation_drift"),
                              (["cut", "--version", "c7.3", "--pins", str(pins_path)],
                               "unpinned_member")):
            with self.subTest(command=command):
                exit_code, artifact = self.command_artifact(*command)
                self.assertEqual(20, exit_code)
                evidence = artifact["evidence"]
                self.assertEqual("ledger_contract_" + code, evidence["code"])
                self.assert_member_diff(expected, evidence["drift"])
        # Establish the same baseline as a cut, then reproduce the drift.
        (self.root / "floati/extra.py").unlink()
        self.write_schema(self.schema)
        self.cut()
        self.schema_path.write_text(self.schema_path.read_text() + "\n")
        (self.root / "floati/extra.py").write_text(
            'def validate_extra(row):\n    return isinstance(row, dict)\n',
        )
        exit_code, artifact = self.command_artifact("check")
        self.assertEqual(20, exit_code)
        self.assertEqual("ledger_contract_recut_required", artifact["evidence"]["code"])
        self.assert_member_diff(expected, artifact["evidence"]["drift"])

    def assert_member_diff(self, expected, drift):
        self.assertEqual(50, drift["limit"])
        for section, changes in expected.items():
            for change, members in changes.items():
                self.assertEqual(members, drift[section][change])
                self.assertEqual(len(members), drift[section]["counts"][change])

    def test_drift_lists_are_sorted_bounded_and_count_every_member(self):
        """Large drift retains exact totals for all three sections and categories."""
        pins, current = {}, {}
        for section in ("kinds", "schemas", "sources"):
            pins[section], current[section] = {}, {}
            for number in reversed(range(53)):
                pins[section]["removed-%02d" % number] = "unused"
                current[section]["added-%02d" % number] = "new"
                pins[section]["changed-%02d" % number] = "old"
                current[section]["changed-%02d" % number] = "new"
        pins_path = self.root / "candidate-pins.json"
        pins_path.write_text(json.dumps(pins))
        with mock.patch.object(self.tool, "inventory", return_value=current):
            exit_code, artifact = self.command_artifact(
                "cut", "--version", "c7.3", "--pins", str(pins_path),
            )
        self.assertEqual(20, exit_code)
        drift = artifact["evidence"]["drift"]
        self.assertEqual(50, drift["limit"])
        for section in ("kinds", "schemas", "sources"):
            for change in ("added", "removed", "changed"):
                self.assertEqual([change + "-%02d" % number for number in range(50)],
                                 drift[section][change])
                self.assertEqual(53, drift[section]["counts"][change])

    def test_malformed_candidate_pins_keep_the_typed_member_refusal(self):
        pins_path = self.root / "candidate-pins.json"
        for pins in (None, [], {"schemas": [], "sources": None}):
            with self.subTest(pins=pins):
                pins_path.write_text(json.dumps(pins))
                exit_code, artifact = self.command_artifact(
                    "cut", "--version", "c7.3", "--pins", str(pins_path),
                )
                self.assertEqual(20, exit_code)
                self.assertEqual("ledger_contract_unpinned_member", artifact["evidence"]["code"])
                self.assertEqual(["schemas/v0/sample.schema.json"],
                                 artifact["evidence"]["drift"]["schemas"]["added"])

    def test_schema_copy_race_names_the_observed_schema_even_if_it_changes_back(self):
        pins = self.tool.inventory(self.root)
        original = Path.read_bytes

        def racing_read(path):
            data = original(path)
            if path == self.schema_path:
                return data + b"\n"
            return data

        # Freeze the discovery snapshot, then race only the subsequent copy read.
        pins_path = self.root / "candidate-pins.json"
        pins_path.write_text(json.dumps(pins))
        with mock.patch.object(self.tool, "inventory", return_value=pins),\
                mock.patch.object(Path, "read_bytes", racing_read):
            exit_code, artifact = self.command_artifact(
                "cut", "--version", "c7.3", "--pins", str(pins_path),
            )
        self.assertEqual(20, exit_code)
        self.assertEqual("ledger_contract_unpinned_member", artifact["evidence"]["code"])
        self.assertEqual(["schemas/v0/sample.schema.json"],
                         artifact["evidence"]["drift"]["schemas"]["changed"])
        self.assertEqual(pins, self.tool.inventory(self.root))
        self.assertFalse((self.root / "bundle/c7.3").exists())

    def test_cut_is_self_contained_pinned_and_never_overwrites(self):
        notes = [{"row": "EXIT-1", "decision": "Ratify the shipped exit behavior.",
                  "basis": "Freeze what ships; a later recut can change it."}]
        notes_path = self.root / "reviewed-notes.json"
        notes_path.write_text(json.dumps(notes))
        self.assertEqual(0, self.command_artifact(
            "prepare", "--ratification-notes", str(notes_path),
        )[0])
        self.assertEqual(0, self.command_artifact("check")[0])
        # The deferred cut command needs no additional arguments. The notes
        # must outlive both their input file and the provisional preparation.
        notes_path.unlink()
        self.cut()
        bundle = self.root / "bundle/c7.3"
        contract = json.loads((bundle / "ledger-contract.json").read_text())
        self.assertEqual(notes, contract["ratification_notes"])
        self.assertEqual("unchanged", self.tool.check(self.root)["classification"])
        self.assertEqual(self.tool.inventory(self.root), contract["members"])
        snapshot = self.cut_payload("c7.3")
        self.assertEqual(self.schema_path.read_bytes(), snapshot.read_bytes())
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                         contract["members"]["schemas"]["schemas/v0/sample.schema.json"])
        self.assert_refused("ledger_contract_version_exists", self.cut)

    def test_cut_requires_the_next_version_and_safe_paths(self):
        for version in ("c7.2", "c7.4", "../escape", "c7.03"):
            with self.subTest(version=version):
                self.assert_refused("ledger_contract_version_invalid", lambda: self.tool.cut(
                    self.root, version, self.tool.inventory(self.root),
                ))

    def test_symlinked_members_are_refused(self):
        (self.root / "schemas/v0/alias.schema.json").symlink_to(self.schema_path)
        self.assert_refused("ledger_contract_path_invalid", lambda: self.tool.inventory(self.root))

    def test_preparation_refuses_a_symlinked_output_parent(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "tests").symlink_to(outside, target_is_directory=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.tool.main(["--root", str(self.root), "prepare"])
        self.assertEqual(20, result)
        self.assertEqual("ledger_contract_path_invalid", json.loads(output.getvalue())["evidence"]["code"])
        self.assertEqual([], list(outside.iterdir()))

    def test_schema_reference_outside_member_set_is_refused(self):
        self.write_schema({"$ref": "../../outside.json"})
        self.assert_refused("ledger_contract_schema_reference_invalid", lambda: self.tool.inventory(self.root))

    def test_new_kind_without_ratification_or_recut_fails_guard(self):
        self.cut()
        path = self.root / "floati/records.py"
        path.write_text(path.read_text() + '\nNEW_KINDS = {"unratified_kind"}\n')
        self.assert_refused("ledger_contract_recut_required", lambda: self.tool.check(self.root))
        self.tool.cut(self.root, "c7.4", self.tool.inventory(self.root))
        self.assertEqual("unchanged", self.tool.check(self.root)["classification"])

    def test_missing_old_schema_or_source_requires_recut(self):
        self.cut()
        self.schema_path.unlink()
        self.assertEqual("recut", self.tool.assess(self.root, "c7.3", {})["classification"])

    def test_recut_keeps_earlier_published_cuts_under_the_guard(self):
        self.cut()
        snapshot = self.cut_payload("c7.3")
        self.tool.cut(self.root, "c7.4", self.tool.inventory(self.root))
        snapshot.write_text("{}\n")
        self.assert_refused("ledger_contract_bundle_invalid", lambda: self.tool.check(self.root))

    def test_unchanged_schemas_are_readable_without_another_payload_copy(self):
        twin = self.schema_path.with_name("twin.schema.json")
        twin.write_bytes(self.schema_path.read_bytes())
        self.cut()
        self.assertEqual(self.cut_payload("c7.3"), self.cut_payload("c7.3", "schemas/v0/twin.schema.json"))
        self.tool.cut(self.root, "c7.4", self.tool.inventory(self.root))
        added = {
            path.relative_to(self.root / "bundle/c7.4").as_posix()
            for path in (self.root / "bundle/c7.4").rglob("*") if path.is_file()
        }
        self.assertEqual({"ledger-contract.json", "ledger-contract.sha256"}, added)
        self.assertEqual("unchanged", self.tool.check(self.root)["classification"])

    def test_legacy_cut_is_verified_and_reused_without_changing_its_files(self):
        pins = self.tool.inventory(self.root)
        version = "c7.3"
        legacy = self.root / "bundle" / version
        schema_name = "schemas/v0/sample.schema.json"
        payload = legacy / schema_name
        payload.parent.mkdir(parents=True)
        payload.write_bytes(self.schema_path.read_bytes())
        self.tool.save_contract(legacy, {
            "format_version": 1, "kind": "ledger_contract", "version": version,
            "predecessor": "c7.2", "source_pin": self.tool.SOURCE_PIN,
            "predecessor_contract_sha256": None, "members": pins, "ratifications": [],
        })
        (self.root / self.tool.PREPARATION).parent.mkdir(parents=True)
        self.tool.write_json(self.root / self.tool.PREPARATION, {"state": "cut", "version": version})
        self.tool.write_json(self.root / self.tool.CURRENT, {
            "version": version, "sha256": hashlib.sha256(
                (legacy / "ledger-contract.json").read_bytes(),
            ).hexdigest(),
        })
        before = {name: content for name, content in self.bundle_bytes().items()
                  if name.startswith("c7.3/")}
        self.tool.cut(self.root, "c7.4", pins)
        self.assertEqual(payload, self.cut_payload("c7.4"))
        self.assertEqual(before, {name: content for name, content in self.bundle_bytes().items()
                                  if name.startswith("c7.3/")})
        payload.write_text("{}\n")
        self.assert_refused("ledger_contract_bundle_invalid", lambda: self.tool.check(self.root))

    def test_index_cannot_redirect_to_unapproved_storage_with_identical_bytes(self):
        self.cut()
        version = "c7.3"
        cut = self.root / "bundle" / version
        name = "schemas/v0/sample.schema.json"
        document = self.tool.load_contract(self.root, version)
        storage = document["schema_index"][name]["storage"]
        wrong_paths = ("outside.json", storage.replace("c7.3/", "c7.4/"),
                       f"bundle/{version}/{name}")
        for wrong in wrong_paths:
            with self.subTest(storage=wrong):
                path = self.root / wrong
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.schema_path.read_bytes())
                document["schema_index"][name]["storage"] = wrong
                self.tool.save_contract(cut, document)
                self.assert_refused("ledger_contract_bundle_invalid",
                                    lambda: self.tool.load_contract(self.root, version))

    def test_semantic_code_change_requires_recut_even_with_schema_fixture(self):
        self.cut()
        path = self.root / "floati/records.py"
        path.write_text(path.read_text().replace('in _SPECS', 'not in _SPECS'))
        result = self.tool.assess(self.root, "c7.3", {})
        self.assertEqual("recut", result["classification"])

    def loosen(self):
        self.cut()
        schema = copy.deepcopy(self.schema)
        schema["properties"]["value"]["enum"].append("new")
        self.write_schema(schema)
        return {"schemas/v0/sample.schema.json": [{"kind": "sample", "value": "old"}]}

    def test_enum_loosening_with_preserved_record_can_be_ratified(self):
        fixtures = self.loosen()
        self.assertEqual("ratify", self.tool.assess(self.root, "c7.3", fixtures)["classification"])
        self.tool.ratify(self.root, "c7.3", fixtures)
        self.assertEqual("unchanged", self.tool.check(self.root)["classification"])

    def test_loosening_without_a_fixture_requires_recut(self):
        self.loosen()
        self.assertEqual("recut", self.tool.assess(self.root, "c7.3", {})["classification"])

    def test_fixture_must_have_been_valid_under_the_pinned_schema(self):
        self.loosen()
        fixture = {"schemas/v0/sample.schema.json": [{"kind": "sample", "value": "new"}]}
        self.assertEqual("recut", self.tool.assess(self.root, "c7.3", fixture)["classification"])

    def test_one_preserved_fixture_does_not_prove_tightening_is_loosening(self):
        self.schema["properties"]["value"]["enum"].append("other")
        self.write_schema(self.schema)
        self.cut()
        schema = copy.deepcopy(self.schema)
        schema["properties"]["value"]["enum"] = ["old"]
        self.write_schema(schema)
        fixtures = {"schemas/v0/sample.schema.json": [{"kind": "sample", "value": "old"}]}
        self.assertEqual("recut", self.tool.assess(self.root, "c7.3", fixtures)["classification"])
        self.assert_refused("ledger_contract_recut_required", lambda: self.tool.ratify(self.root, "c7.3", fixtures))

    def test_added_optional_constraint_on_previously_open_property_is_tightening(self):
        old = {"type": "object"}
        new = {"type": "object", "properties": {"x": {"type": "string"}}}
        self.assertFalse(self.tool.is_loosening(old, new))

    def test_required_removal_and_closed_optional_property_addition_are_loosenings(self):
        new = copy.deepcopy(self.schema)
        new["required"].remove("value")
        new["properties"]["extra"] = {"type": "integer"}
        self.assertTrue(self.tool.is_loosening(self.schema, new))

    def test_changed_pattern_or_unknown_keyword_is_not_guessed_from_examples(self):
        self.assertFalse(self.tool.is_loosening({"pattern": "^x$"}, {"pattern": "x"}))
        self.assertFalse(self.tool.is_loosening({"x-domain": 1}, {"x-domain": 2}))

    def test_pattern_properties_interaction_cannot_masquerade_as_optional_addition(self):
        old = {"type": "object", "additionalProperties": False,
               "patternProperties": {"^x$": {"type": "integer"}}}
        new = copy.deepcopy(old)
        new["properties"] = {"x": {"type": "string"}}
        self.assertFalse(self.tool.is_loosening(old, new))

    def test_tampered_frozen_schema_is_not_a_ratification_baseline(self):
        self.cut()
        path = self.cut_payload("c7.3")
        path.write_text("{}\n")
        self.assert_refused("ledger_contract_bundle_invalid", lambda: self.tool.assess(self.root, "c7.3", {}))

    def bundle_bytes(self):
        return {path.relative_to(self.root / "bundle").as_posix(): path.read_bytes()
                for path in (self.root / "bundle").rglob("*") if path.is_file()}

    def test_ratification_binds_the_exact_assessed_schema(self):
        fixtures = self.loosen()
        previous = self.bundle_bytes()
        assess = self.tool.assess

        def concurrent_change(*args):
            result = assess(*args)
            self.write_schema({"type": "null"})
            return result

        with mock.patch.object(self.tool, "assess", side_effect=concurrent_change):
            with self.assertRaises(self.tool.ContractRefusal) as caught:
                self.tool.ratify(self.root, "c7.3", fixtures)
        self.assertEqual("ledger_contract_unpinned_member", caught.exception.code)
        self.assertEqual(["schemas/v0/sample.schema.json"],
                         caught.exception.evidence["drift"]["schemas"]["changed"])
        self.assertEqual(previous, self.bundle_bytes())

    def test_failed_ratification_staging_preserves_every_bundle_byte(self):
        fixtures = self.loosen()
        previous = self.bundle_bytes()
        with mock.patch.object(self.tool, "save_contract", side_effect=OSError("write failed")):
            self.assert_refused("ledger_contract_publication_failed", lambda: self.tool.ratify(
                self.root, "c7.3", fixtures,
            ))
        self.assertEqual(previous, self.bundle_bytes())

    def test_failed_publication_rolls_back_bundle_and_current_pointer(self):
        fixtures = self.loosen()
        previous = self.bundle_bytes()
        replace = Path.replace

        def cannot_publish(path, target):
            if Path(target) == self.root / self.tool.CURRENT:
                raise OSError("pointer publication failed")
            return replace(path, target)

        with mock.patch.object(Path, "replace", cannot_publish):
            self.assert_refused("ledger_contract_publication_failed", lambda: self.tool.ratify(
                self.root, "c7.3", fixtures,
            ))
        self.assertEqual(previous, self.bundle_bytes())

    def test_missing_cut_index_never_falls_back_to_preparation(self):
        for case in ("missing-index", "missing-pointer", "erased-cut-and-pointer"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
                root = Path(temporary) / "root"
                shutil.copytree(self.root, root)
                self.tool.cut(root, "c7.3", self.tool.inventory(root))
                if case == "missing-index":
                    (root / "bundle/c7.3/ledger-contract.json").unlink()
                else:
                    (root / self.tool.CURRENT).unlink()
                    if case == "erased-cut-and-pointer":
                        shutil.rmtree(root / "bundle/c7.3")
                self.assert_refused("ledger_contract_bundle_invalid", lambda: self.tool.check(root))

    def test_ratification_audit_is_verified_on_every_guard(self):
        fixtures = self.loosen()
        self.tool.ratify(self.root, "c7.3", fixtures)
        path = self.root / "bundle/c7.3/ratifications/1/fixtures.json"
        path.write_text("{}\n")
        self.assert_refused("ledger_contract_bundle_invalid", lambda: self.tool.check(self.root))

    def test_symlinked_audit_parent_cannot_write_outside_the_bundle(self):
        fixtures = self.loosen()
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "bundle/c7.3/ratifications").symlink_to(outside, target_is_directory=True)
        self.assert_refused("ledger_contract_path_invalid", lambda: self.tool.ratify(
            self.root, "c7.3", fixtures,
        ))
        self.assertEqual([], list(outside.iterdir()))

    def test_multiple_ratifications_preserve_the_full_audit_chain(self):
        fixtures = self.loosen()
        path = self.cut_payload("c7.3")
        self.tool.ratify(self.root, "c7.3", fixtures)
        schema = json.loads(self.schema_path.read_text())
        schema["required"].remove("value")
        self.write_schema(schema)
        self.tool.ratify(self.root, "c7.3", fixtures)
        self.assertEqual("unchanged", self.tool.check(self.root)["classification"])
        path.write_text("{}\n")
        self.assert_refused("ledger_contract_bundle_invalid", lambda: self.tool.check(self.root))

    def test_broadened_negative_reference_dependency_requires_recut(self):
        (self.root / "schemas/v0/inverted.schema.json").write_text(
            '{"not":{"$ref":"sample.schema.json"}}\n',
        )
        fixtures = self.loosen()
        self.assertEqual("recut", self.tool.assess(self.root, "c7.3", fixtures)["classification"])

    def test_live_tree_is_covered_by_preparation_pins_or_latest_cut(self):
        status_command = ["/usr/bin/git", "status", "--porcelain", "--untracked-files=all"]
        before = subprocess.check_output(status_command, cwd=ROOT, text=True)
        inventory = self.tool.inventory

        def observe_inventory(root):
            # Observe during the real check, while its lock is still held.
            self.assertEqual(before, subprocess.check_output(status_command, cwd=ROOT, text=True),
                             "contract self-test wrote into the running checkout")
            return inventory(root)

        # Copy today's candidate bytes, including uncommitted edits. Checking
        # HEAD instead would silently weaken this live contract guard.
        candidate = copy_live_contract_inputs(self.tool, self.root / "live-copy")
        with mock.patch.object(self.tool, "inventory", side_effect=observe_inventory):
            result = self.tool.check(candidate)
        self.assertIn(result["classification"], {"prepared", "unchanged"})

    def test_real_ask_latency_change_preserves_prepared_and_cut_contract(self):
        """The exact e1bc5b02 first-parent ask diff changes no ledger contract."""
        # Include real import/caller dependencies: an isolated ask module can
        # hide accidental reverse-closure back into perform_ask.
        shutil.copytree(ROOT / "floati", self.root / "floati", dirs_exist_ok=True)
        path = self.root / "floati/ask.py"
        before = (ROOT / "tests/fixtures/ledger-contract-ask-before.txt").read_text()
        patch = ROOT / "tests/fixtures/ledger-contract-ask-latency.patch"
        path.write_text(before)
        members = self.tool.inventory(self.root)
        applied = subprocess.run(["/usr/bin/git", "apply", str(patch)], cwd=self.root,
                                 capture_output=True, text=True)
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertIn('timing["question_sent_ns"]', path.read_text())
        self.assertEqual(members, self.tool.inventory(self.root))


if __name__ == "__main__":
    unittest.main()
