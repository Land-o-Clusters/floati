"""Opt-in native projection faults; excluded from ordinary test discovery."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from tests import test_no_listener_fence as fences
from tests.export_inventory import POLICY_RELATIVE


class SliceDSensitivityTests(unittest.TestCase):
    def test_one_native_projection_rejects_independent_faults(self):
        source = fences.REPOSITORY_ROOT / "tests/test_no_listener_fence.py"
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        private_path = "scripts/prepare_public_export.py"
        self.assertEqual([], list(fences.classify_inventory(
            [private_path], root=fences.REPOSITORY_ROOT)))
        prefix = "tests.test_no_listener_fence.WholeProductNoListenerFenceTests."
        file_pin = prefix + "test_built_argv_egress_is_enumerated_not_blind"
        site_pin = prefix + "test_built_argv_egress_sites_are_pinned"
        network = prefix + "test_network_capable_imports_and_subprocesses_are_fully_counted"
        listener = prefix + "test_bind_and_listen_calls_exist_only_in_the_ruled_af_unix_supervisor"
        receipts = []

        def execute(projection, selectors):
            completed = fences._run_projected_fences(projection, selectors)
            receipts.append({"selectors": list(selectors), "returncode": completed.returncode,
                             "stdout": completed.stdout, "stderr": completed.stderr})
            return completed

        def pin_mutation(original, method, value):
            text = original.decode("utf-8")
            function = next(node for node in ast.walk(ast.parse(text))
                            if isinstance(node, ast.FunctionDef) and node.name == method)
            assignment = next(node for node in function.body if isinstance(node, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == "pinned"
                                      for target in node.targets))
            lines = text.splitlines(keepends=True)
            self.assertTrue(lines[assignment.lineno - 1].strip().endswith("{"))
            lines.insert(assignment.lineno, "            " + repr(value) + ",\n")
            return "".join(lines).encode("utf-8")

        with fences._adapted_fence_projection() as projection:
            self.assertFalse((projection / POLICY_RELATIVE).exists())
            self.assertFalse((projection / private_path).exists())
            pristine = execute(projection, fences.PROJECTED_FENCE_IDS)
            fences._assert_projected_fences(self, pristine)
            test_file = projection / "tests/test_no_listener_fence.py"
            original = test_file.read_bytes()
            for label, selector, value in (
                ("private-file-pin", file_pin, private_path),
                ("private-site-pin", site_pin, (private_path, "0000000000000000")),
            ):
                with self.subTest(fault=label):
                    try:
                        test_file.write_bytes(pin_mutation(original, selector.rsplit(".", 1)[1], value))
                        completed = execute(projection, (selector,))
                        self.assertEqual(1, completed.returncode, completed.stderr)
                        self.assertIn("FAIL: " + selector.rsplit(".", 1)[1], completed.stderr)
                        self.assertIn(private_path, completed.stderr)
                        self.assertIn("FAILED (failures=1)", completed.stderr)
                    finally:
                        test_file.write_bytes(original)
                        self.assertEqual(original, test_file.read_bytes())

            planted = projection / "floati/slice_d_unexpected.py"
            self.assertFalse(planted.exists())
            for label, selector, content in (
                ("unexpected-network-import", network, "import urllib.request\n"),
                ("unexpected-listener", listener, "def listen(value):\n    value.listen(1)\n"),
            ):
                with self.subTest(fault=label):
                    try:
                        planted.write_text(content, encoding="utf-8")
                        completed = execute(projection, (selector,))
                        self.assertEqual(1, completed.returncode, completed.stderr)
                        self.assertIn("FAIL: " + selector.rsplit(".", 1)[1], completed.stderr)
                        self.assertIn("floati/slice_d_unexpected.py", completed.stderr)
                        self.assertIn("FAILED (failures=1)", completed.stderr)
                    finally:
                        planted.unlink()
                        self.assertFalse(planted.exists())

            built_file = projection / "scripts/capture-demo-assets.py"
            built_original = built_file.read_bytes()
            try:
                built_file.write_bytes(built_original + b'\ndef slice_d_extra_site(target):\n    subprocess.run(f"git fetch {target}")\n')
                completed = execute(projection, (file_pin, site_pin))
                self.assertEqual(1, completed.returncode, completed.stderr)
                self.assertIn("FAIL: " + site_pin.rsplit(".", 1)[1], completed.stderr)
                self.assertNotIn("FAIL: " + file_pin.rsplit(".", 1)[1], completed.stderr)
                self.assertIn("scripts/capture-demo-assets.py", completed.stderr)
                self.assertIn("FAILED (failures=1)", completed.stderr)
            finally:
                built_file.write_bytes(built_original)
                self.assertEqual(built_original, built_file.read_bytes())

            missing = (*fences.PROJECTED_FENCE_IDS[:-1], prefix + "test_slice_d_missing")
            completed = execute(projection, missing)
            self.assertEqual(1, completed.returncode, completed.stderr)
            self.assertIn("test_slice_d_missing", completed.stderr)
            self.assertIn("FAILED (errors=1)", completed.stderr)
            with self.assertRaisesRegex(AssertionError, "projected product fences failed"):
                fences._assert_projected_fences(self, completed)

            completed = execute(projection, fences.PROJECTED_FENCE_IDS[:-1])
            self.assertEqual(0, completed.returncode, completed.stderr)
            with self.assertRaisesRegex(AssertionError, "projected fence selection differs"):
                fences._assert_projected_fences(self, completed)

            try:
                needle = b"    def test_mcp_module_imports_only_local_code_and_non_network_stdlib(self) -> None:"
                self.assertEqual(1, original.count(needle))
                test_file.write_bytes(original.replace(needle,
                    b'    @unittest.skip("slice-d controlled skip")\n' + needle))
                completed = execute(projection, fences.PROJECTED_FENCE_IDS)
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertIn("OK (skipped=1)", completed.stderr)
                with self.assertRaisesRegex(AssertionError, "skipped"):
                    fences._assert_projected_fences(self, completed)
            finally:
                test_file.write_bytes(original)
                self.assertEqual(original, test_file.read_bytes())

            restored = execute(projection, fences.PROJECTED_FENCE_IDS)
            fences._assert_projected_fences(self, restored)
        self.assertFalse(projection.exists())
        self.assertEqual(source_hash, hashlib.sha256(source.read_bytes()).hexdigest())
        print("projection sensitivity", json.dumps({
            "one_owned_adapted_tree": str(projection), "tree_removed": True,
            "restored_source_sha256": source_hash, "native_children": receipts}, sort_keys=True))

    def test_strict_adaptation_refusal_cannot_start_projection_child(self):
        from tests import export_inventory
        destinations = []
        adapted_paths = []
        materialise = fences.materialise_adapted_tree
        adapt_factory = export_inventory._adapted_bytes_source
        def observe(destination, **kwargs):
            destinations.append(destination)
            return materialise(destination, **kwargs)
        def failing_adapter(*arguments, **kwargs):
            adapt = adapt_factory(*arguments, **kwargs)
            def refuse(relative, data):
                adapt(relative, data)
                adapted_paths.append(relative)
                raise RuntimeError("slice-d controlled strict adaptation refusal")
            return refuse
        case = fences.WholeProductNoListenerFenceTests(
            "test_exported_product_fences_run_in_a_real_adapted_projection")
        result = unittest.TestResult()
        with mock.patch.object(fences, "materialise_adapted_tree", observe),\
                mock.patch.object(export_inventory, "_adapted_bytes_source", failing_adapter),\
                mock.patch.object(fences, "_run_projected_fences") as child:
            case.run(result)
        self.assertEqual(1, len(result.errors), result.errors)
        self.assertEqual([], result.failures)
        self.assertEqual([], result.skipped)
        self.assertIn("slice-d controlled strict adaptation refusal", result.errors[0][1])
        child.assert_not_called()
        self.assertEqual(1, len(destinations))
        self.assertEqual(1, len(adapted_paths))
        self.assertFalse(destinations[0].exists())
        print("projection adaptation refusal", json.dumps({
            "child_calls": 0, "failed_preparation_removed": True,
            "adapted_path_before_refusal": adapted_paths[0],
            "scope": "real strict materialiser, controlled refusal after first native adaptation"}, sort_keys=True))
