"""The Stop-hook bridge — cursor-parity wake for the interactive zcode seat.

The owner's ruled expectation: the turn stops; on arrival the message is
injected into the thread VISIBLE in the desktop; the turn auto-starts
working. The bridge is the mechanism: zcode fires the `Stop` hook when a
turn ends; the bridge queries the seat's bus for mail, and if mail is
present it emits the block-continue decision (`{"decision": "block",
"reason": <prompt>}`) that injects the mail into the thread and restarts
the turn — visibly, in the TUI. With no mail it exits silently and the
turn stops.

The bus-query and decision-emission logic is testable here. The
in-process "continue the turn" semantics on zcode itself is the K3
measurement the owner's hook approval unlocks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids

REPOSITORY_ROOT = Path(__file__).parents[1]
BRIDGE = REPOSITORY_ROOT / "hooks" / "stop-hook-bridge.py"


class StopHookBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "fleet"
        env = dict(
            os.environ,
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=str(REPOSITORY_ROOT),
        )
        subprocess.run(
            [sys.executable, "-m", "floati", "init", "--root", str(self.root)],
            cwd=str(REPOSITORY_ROOT), capture_output=True, text=True,
            check=True, env=env,
        )
        for node in (public_ids.builder("a"), public_ids.builder("b")):
            subprocess.run(
                [sys.executable, "-m", "floati", "register", "--root",
                 str(self.root), node, "--harness", "codex"],
                cwd=str(REPOSITORY_ROOT), capture_output=True, text=True,
                check=True, env=env,
            )
        subprocess.run(
            [sys.executable, "-m", "floati", "send", "--root", str(self.root),
             "--from", public_ids.builder("a"),
             "--to", public_ids.builder("b"), "--repo", "smoke",
             "--sha", "01a0530000007000800000000000000000000000",
             "--doc", "docs/CONFLUENCE-v0.md", "--note", "bridge probe mail"],
            cwd=str(REPOSITORY_ROOT), capture_output=True, text=True,
            check=True, env=env,
        )
        self.env = dict(
            env, HOOK_ROOT=str(self.root), HOOK_NODE=public_ids.builder("b"),
            HOOK_SESSION="builder-b-hook-session",
        )

    def run_bridge(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(BRIDGE)],
            capture_output=True, text=True, timeout=60,
            env=self.env, cwd=str(REPOSITORY_ROOT),
            input=json.dumps({"session_id": "builder-b-hook-session"}),
        )

    def test_bridge_emits_block_continue_with_the_mail(self) -> None:
        self.assertTrue(BRIDGE.is_file(), "the bridge script is missing")
        completed = self.run_bridge()
        self.assertEqual(0, completed.returncode, completed.stderr)
        decision = json.loads(completed.stdout)
        self.assertEqual("block", decision["decision"])
        self.assertIn("[floati]", decision["reason"])
        self.assertIn("bridge probe mail", decision["reason"])
        self.assertIn("builder-b-hook-session", decision["reason"])

    def test_bridge_is_silent_when_no_mail_waits(self) -> None:
        # drain once (the bridge itself presents and injects); the second
        # run over the same presented-and-acked-free mail still injects,
        # so silence is proven on a FRESH root with no mail at all.
        empty_env = dict(self.env)
        empty_env["HOOK_ROOT"] = str(self.base / "empty-fleet")
        subprocess.run(
            [sys.executable, "-m", "floati", "init", "--root",
             str(self.base / "empty-fleet")],
            cwd=str(REPOSITORY_ROOT), capture_output=True, text=True,
            check=True, env=empty_env,
        )
        completed = subprocess.run(
            [sys.executable, str(BRIDGE)],
            capture_output=True, text=True, timeout=60,
            env=empty_env, cwd=str(REPOSITORY_ROOT), input="{}",
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stdout.strip(),
                         "an idle bridge must not inject anything")

    def test_bridge_presents_so_the_injected_mail_is_consumable(self) -> None:
        """The injection must present the mail (delivery receipt) so the
        woken turn can ack after acting — presentation, not duplication."""
        self.run_bridge()
        receipts = list(
            (self.root / "receipts" / "deliveries").glob("builder-b.jsonl"))
        self.assertEqual(1, len(receipts), "no delivery receipt written")
        rows = [json.loads(l) for l in receipts[0].read_text().splitlines() if l]
        self.assertEqual("delivery_receipt", rows[0]["kind"])

    def test_bridge_refuses_loudly_when_repo_is_not_a_servable_checkout(self) -> None:
        """HT-R7 (Am.3): a resolved REPO that cannot serve the seat must say
        so on stderr and exit non-zero. Am.2's installed copy no-oped
        silently - rc=0, empty stdout - because REPO derived to the install
        root; a capability that cannot serve must never lapse quietly."""
        installed = self.base / "install-root" / "hooks"
        installed.mkdir(parents=True)
        script = installed / "stop-hook-bridge.py"
        script.write_bytes(BRIDGE.read_bytes())
        completed = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=60,
            env=self.env, cwd=str(REPOSITORY_ROOT), input="{}",
        )
        self.assertNotEqual(0, completed.returncode,
                            "an unservable REPO must not exit 0 silently")
        self.assertIn("is not a git checkout", completed.stderr)
        self.assertIn("HOOK_REPO", completed.stderr)
        self.assertEqual("", completed.stdout.strip(),
                         "an unservable bridge must not emit a decision")

    def test_bridge_refuses_loudly_when_repo_lacks_the_floati_package(self) -> None:
        """HT-R7 predicate ruling: .git alone is a proxy - an unrelated git
        checkout without the floati package cannot serve the seat, and the
        obscure silent no-op must stay loud."""
        unrelated = self.base / "unrelated-repo"
        (unrelated / ".git").mkdir(parents=True)
        unrelated_env = dict(self.env, HOOK_REPO=str(unrelated))
        completed = subprocess.run(
            [sys.executable, str(BRIDGE)],
            capture_output=True, text=True, timeout=60,
            env=unrelated_env, cwd=str(unrelated), input="{}",
        )
        self.assertNotEqual(0, completed.returncode,
                            "a package-less checkout must not serve silently")
        self.assertIn("floati package", completed.stderr)
        self.assertEqual("", completed.stdout.strip())

    def test_bridge_records_each_firing_in_the_root(self) -> None:
        """WD-R8: the firing is the proof - every dispatch lands a row in the
        seat's own state under the bus root, where doctor can read it."""
        self.run_bridge()

        firings = (
            self.root / "state" / "zcode-hook" / "firings" / "builder-b.jsonl"
        )
        self.assertTrue(firings.is_file(), "no firing evidence written")
        rows = [json.loads(l) for l in firings.read_text().splitlines() if l.strip()]
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]["injected"])
        self.assertIn("timestamp", rows[0])


if __name__ == "__main__":
    unittest.main()
