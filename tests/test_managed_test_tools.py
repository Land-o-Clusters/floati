from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from tests import managed_test_tools
from tests.temp_roots import REAL_TEMP_ROOT


class ManagedTestToolsTests(unittest.TestCase):
    def test_missing_tools_are_reported_only_by_tests_that_invoke_them(self) -> None:
        from tests import test_au1_s0, test_au1_s1, test_au1_s2, test_au1_s3
        from tests import test_minisign_signing

        cases = (
            (test_au1_s0.AU1S0Tests, "test_s0_path_root_verifier_accepts_the_real_fixture", "minisign"),
            (test_au1_s0.AU1S0Tests, "test_s0_path_root_verifier_refuses_escape", None),
            (test_au1_s1.AU1S1Tests, "test_s1_07_signature_tamper_and_trusted_comment_binding_refuse", "minisign"),
            (test_au1_s1.AU1S1Tests, "test_s1_08_undeclared_minisign_is_a_typed_refusal_not_an_observation", None),
            (test_au1_s2.AU1S2Tests, "test_s2_01_wrong_signed_bundle_hash_refuses_without_destination_change", "minisign"),
            (test_au1_s2.AU1S2Tests, "test_s2_05c_git_absence_names_the_fixed_host_candidates", None),
            (test_au1_s3.AU1S3Tests, "test_s3_01_doctor_projects_active_consent_and_signed_check_receipts", "minisign"),
            (test_au1_s3.AU1S3Tests, "test_s3_00_doctor_projects_never_checked_without_network_or_writes", None),
            (test_minisign_signing.MinisignSigningTests, "test_s2_real_minisign_binary_verifies_fixture_and_signed_binding", "minisign"),
            (test_minisign_signing.MinisignSigningTests, "test_s2_absent_declaration_is_a_typed_refusal_with_a_remedy", None),
        )
        which = managed_test_tools.shutil.which

        def without_tools(name, *args, **kwargs):
            if name == "minisign":
                return None
            return which(name, *args, **kwargs)

        with mock.patch.dict(os.environ), mock.patch.object(
            managed_test_tools.shutil, "which", side_effect=without_tools
        ):
            os.environ.pop("FLOATI_TEST_MINISIGN_EXECUTABLE", None)
            for case_type, method, tool in cases:
                with self.subTest(method=method):
                    case = case_type(method)
                    result = case.run()
                    self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
                    expected = [] if tool is None else [(case, f"test_tool_absent: {tool}")]
                    self.assertEqual(expected, result.skipped)

    def test_managed_declaration_is_used_without_path_lookup(self) -> None:
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            executable = Path(temporary) / "tool"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            with mock.patch.dict(
                os.environ,
                {"FLOATI_TEST_TOOL_EXECUTABLE": str(executable), "PATH": ""},
                clear=False,
            ), mock.patch(
                "tests.managed_test_tools.shutil.which",
                side_effect=AssertionError("managed resolution consulted PATH"),
            ):
                self.assertEqual(
                    managed_test_tools.executable(
                        "FLOATI_TEST_TOOL_EXECUTABLE", "tool"
                    ),
                    executable,
                )

    def test_invalid_managed_declaration_refuses_without_path_fallback(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FLOATI_TEST_TOOL_EXECUTABLE": str(
                    Path(REAL_TEMP_ROOT) / "missing-env1-tool"
                )
            },
            clear=False,
        ), mock.patch(
            "tests.managed_test_tools.shutil.which",
            return_value="/usr/bin/true",
        ) as which:
            with self.assertRaises(ProtocolRefusal) as rejected:
                managed_test_tools.executable(
                    "FLOATI_TEST_TOOL_EXECUTABLE", "tool"
                )
        self.assertEqual(rejected.exception.code, "managed_test_tool_invalid")
        which.assert_not_called()


if __name__ == "__main__":
    unittest.main()
