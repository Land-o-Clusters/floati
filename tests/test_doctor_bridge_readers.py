"""BRIDGE-1: doctor names every bound zcode seat's bridge checkout.

The measured deafness (2026-09-06 00:12Z) had a third cause doctor could
not see: the seat's Stop-hook bridge ran `python3 -m floati` from a stale
seat checkout whose reader died on newer ledger vocabulary. Doctor must
list, per bound zcode seat, the registration it read, the floati commit
the checkout its HOOK_REPO (or bridge-script location) runs, and — beside
`older_readers` — flag a bridge whose reader predates the ledger's newest
kind. A registration that cannot be named is a typed finding, never
silence.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.doctor import Doctor, parse_bridge_command
from floati.events import EventLog
from floati.framing import encode_frame
from floati.records import READER_VERSION
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import adapter_contract_digest
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from tests.temp_roots import REAL_TEMP_ROOT

REPOSITORY_ROOT = Path(__file__).parents[1]

FUTURE_KIND = "future_bridge_probe_kind"
FUTURE_ID = "future-bridge-01a0745f000070008000000000000000"


class DoctorBridgeReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.node = public_ids.builder("a")
        Registry(self.root).register(self.node, "worker")
        self.workspace = self.base / "workspace"
        self.register_bridge_command()

    def register_bridge_command(self) -> None:
        """Write the registration a real bound zcode seat carries."""

        command = (
            "/bin/sh -c 'HOOK_ROOT=" + str(self.root.path)
            + " HOOK_NODE=" + self.node
            + " HOOK_SESSION=" + self.node + "-hook-session"
            + " HOOK_REPO=" + str(REPOSITORY_ROOT)
            + " exec /usr/bin/python3 "
            + str(self.workspace / "hooks" / "stop-hook-bridge.py") + "'"
        )
        settings = self.workspace / ".zcode"
        settings.mkdir(parents=True, exist_ok=True)
        (settings / "settings.json").write_text(
            json.dumps({
                "hooks": {"Stop": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": command,
                               "timeout": 30}],
                }]},
            }, indent=2) + "\n",
            encoding="utf-8",
        )

    def bind_zcode_seat(self) -> None:
        executable = self.base / "zcode-cli"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        AdapterBindingStore(self.root).write(
            DaemonCoordinate(self.root, self.node, "zcode"),
            session_id=self.node + "-hook-session",
            workspace=self.workspace,
            executable=executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("zcode"),
            binding_epoch=1,
        )

    def run_doctor(self) -> tuple[dict, int]:
        artifact, return_code = Doctor(
            Path.cwd(), str(self.root.path), ref="HEAD"
        ).artifact()
        return artifact, return_code

    def findings(self, code: str) -> list[dict]:
        artifact, _return_code = self.run_doctor()
        return [row for row in artifact["findings"] if row["code"] == code]

    def test_doctor_names_the_commit_each_bridge_runs(self) -> None:
        """The finding carries the checkout, its full commit, and the
        reader version, read from the same sources the bridge runs."""

        self.bind_zcode_seat()
        expected = subprocess.run(
            ["/usr/bin/git", "-C", str(REPOSITORY_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        rows = self.findings("bridge_reader_current")
        self.assertEqual(1, len(rows), rows)
        observation = rows[0]["bridge"]
        self.assertEqual(str(REPOSITORY_ROOT), observation["repo"])
        self.assertEqual(expected, observation["commit"])
        self.assertEqual(READER_VERSION, observation["reader_schema_version"])
        self.assertTrue(observation["hook_repo_pinned"])

    def test_doctor_flags_a_bridge_older_than_the_ledgers_newest_kind(
        self,
    ) -> None:
        """An unknown-kind event above the bridge reader's vocabulary is
        the measured deafness; the flag lands beside `older_readers`."""

        self.bind_zcode_seat()
        events = self.root.resolve_relative("events.jsonl")
        with events.open("ab") as handle:
            handle.write(encode_frame({
                "schema_version": 2,
                "id": FUTURE_ID,
                "tenant_id": self.root.tenant_id,
                "timestamp": "2026-09-06T01:00:00.000Z",
                "kind": FUTURE_KIND,
                "payload": {"newer": True},
            }))
        rows = self.findings("bridge_older_reader")
        self.assertEqual(1, len(rows), rows)
        self.assertEqual("warning", rows[0]["severity"])
        self.assertIn("remediation", rows[0])
        self.assertEqual(
            [{"reader_schema_version": READER_VERSION,
              "ledger_newest_kind": FUTURE_KIND}],
            rows[0]["older_readers"],
        )
        observation = rows[0]["bridge"]
        self.assertEqual(str(REPOSITORY_ROOT), observation["repo"])
        self.assertEqual(READER_VERSION, observation["reader_schema_version"])

    def test_a_binding_without_a_readable_registration_is_typed(self) -> None:
        """A bound seat with no readable Stop registration cannot be
        audited; that absence is a warning naming the remedy."""

        self.bind_zcode_seat()
        registration = self.workspace / ".zcode"
        if registration.is_dir():
            import shutil

            shutil.rmtree(registration)
        rows = self.findings("bridge_registration_unnameable")
        self.assertEqual(1, len(rows), rows)
        self.assertEqual("warning", rows[0]["severity"])
        self.assertIn(self.node, rows[0]["subject"])

    def test_no_zcode_binding_is_a_typed_absence(self) -> None:
        rows = self.findings("bridge_registrations_absent")
        self.assertEqual(1, len(rows), rows)
        self.assertEqual("ok", rows[0]["severity"])


class ParseBridgeCommandTests(unittest.TestCase):
    def test_sh_c_wrapped_command_with_pin(self) -> None:
        command = (
            "/bin/sh -c 'HOOK_ROOT=\x2ftmp/fleet HOOK_NODE=seat"
            " HOOK_SESSION=sess HOOK_REPO=\x2ftmp/repo"
            " exec /usr/bin/python3 \x2ftmp/repo/hooks/stop-hook-bridge.py'"
        )
        parsed = parse_bridge_command(command)
        self.assertEqual("\x2ftmp/repo/hooks/stop-hook-bridge.py", parsed["script"])
        self.assertEqual("\x2ftmp/repo", parsed["hook_repo"])

    def test_flat_command_without_pin(self) -> None:
        command = "/usr/bin/python3 /seat/hooks/stop-hook-bridge.py"
        parsed = parse_bridge_command(command)
        self.assertEqual("/seat/hooks/stop-hook-bridge.py", parsed["script"])
        self.assertIsNone(parsed["hook_repo"])

    def test_a_command_without_the_bridge_is_none(self) -> None:
        self.assertIsNone(
            parse_bridge_command("/usr/bin/python3 /tools/other.py"))


if __name__ == "__main__":
    unittest.main()
