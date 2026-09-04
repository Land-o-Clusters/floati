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
