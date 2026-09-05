from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from tests.temp_roots import REAL_TEMP_ROOT

try:
    from floati.adapters.acp import ACPAdapter, ACPRefusal, MAXIMUM_ACP_LINE_BYTES, probe_reference_harness
except (ImportError, ModuleNotFoundError):
    ACPAdapter = None
    ACPRefusal = ValueError
    MAXIMUM_ACP_LINE_BYTES = None
    probe_reference_harness = None


FIXTURES = Path("tests/fixtures/acp")


class ACPAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(ACPAdapter, "ACP v0 fixture codec must exist")
        self.adapter = ACPAdapter()

    def test_initialize_request_and_response_round_trip_semantically(self) -> None:
        for name, category in (
            ("initialize-request.json", "request"),
            ("initialize-response.json", "response"),
        ):
            with self.subTest(name=name):
                raw = (FIXTURES / name).read_bytes()
                message = self.adapter.decode_line(raw)
                self.assertEqual(category, message.category)
                self.assertEqual(json.loads(raw), json.loads(self.adapter.encode_line(message)))

    def test_duplicate_keys_and_unruled_methods_refuse(self) -> None:
        with self.assertRaises(ACPRefusal) as duplicate:
            self.adapter.decode_line(b'{"jsonrpc":"2.0","id":1,"id":2,"method":"initialize"}')
        self.assertEqual("acp_duplicate_key", duplicate.exception.code)

        with self.assertRaises(ACPRefusal) as method:
            self.adapter.decode_line(b'{"jsonrpc":"2.0","id":1,"method":"fs/write","params":{}}')
        self.assertEqual("acp_method_unruled", method.exception.code)

    def test_lines_are_bounded_and_extensions_are_quarantined(self) -> None:
        self.assertEqual(1_048_576, MAXIMUM_ACP_LINE_BYTES)
        message = self.adapter.decode_line(
            b'{"jsonrpc":"2.0","method":"session/update","params":{},"future":{"x":1}}'
        )
        self.assertEqual({"future": {"x": 1}}, message.quarantined)
        with self.assertRaises(ACPRefusal) as oversized:
            self.adapter.decode_line(b" " * MAXIMUM_ACP_LINE_BYTES + b"{}")
        self.assertEqual("acp_line_too_large", oversized.exception.code)

    def test_reference_probe_reports_honest_absence_without_launching(self) -> None:
        self.assertIsNotNone(probe_reference_harness, "ACP harness probe must exist")
        result = probe_reference_harness()
        self.assertEqual(
            {"status": "reference_harness_absent", "executable": None, "command": None},
            result,
        )

    def test_undeclared_probe_ignores_a_path_decoy(self) -> None:
        decoy_dir = Path(tempfile.mkdtemp(prefix="acp-which-", dir=REAL_TEMP_ROOT))
        self.addCleanup(lambda: shutil.rmtree(decoy_dir, ignore_errors=True))
        decoy = decoy_dir / "claude-code-acp"
        decoy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        decoy.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(decoy_dir)}):
            result = probe_reference_harness()
        self.assertEqual(
            {"status": "reference_harness_absent", "executable": None, "command": None},
            result,
        )

    def test_declared_probe_uses_the_house_explicit_executable_fence(self) -> None:
        directory = Path(
            tempfile.mkdtemp(prefix="acp-declared-", dir=REAL_TEMP_ROOT)
        ).resolve(strict=True)
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        tool = directory / "claude-code-acp"
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)
        link = directory / "claude-code-acp-link"
        link.symlink_to(tool)

        result = probe_reference_harness(executable=tool)
        self.assertEqual("reference_harness_present_unlaunched", result["status"])
        self.assertEqual(str(tool), result["executable"])
        self.assertEqual(["claude-code-acp"], result["command"])

        with self.assertRaises(ProtocolRefusal) as symlink:
            probe_reference_harness(executable=link)
        self.assertEqual("acp_reference_executable_invalid", symlink.exception.code)

        other = directory / "installer"
        other.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        other.chmod(0o755)
        with self.assertRaises(ProtocolRefusal) as unrecognized:
            probe_reference_harness(executable=other)
        self.assertEqual(
            "acp_reference_executable_unrecognized", unrecognized.exception.code
        )


if __name__ == "__main__":
    unittest.main()
