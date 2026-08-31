from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.jsonl import read_records
from floati.registry import Registry
from floati.root import FloatiRoot

try:
    from floati.verification import DeliveryVerifier, parse_unittest_measurement
except ModuleNotFoundError:
    DeliveryVerifier = None
    parse_unittest_measurement = None


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class VerifyReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = FloatiRoot.open(self.base / "fleet", "alpha")
        self.registry = Registry(self.root)
        for node in ("sender", "recipient", "verifier"):
            self.registry.register(node, "Codex")
        self.events = EventLog(self.root, self.registry)
        self.repository = self.base / "subject"
        self._git("init", "-b", "main", str(self.repository), cwd=self.base)
        self._git("config", "user.name", "Floati Test", cwd=self.repository)
        self._git("config", "user.email", "floati@example.invalid", cwd=self.repository)
        (self.repository / "tests").mkdir()
        (self.repository / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.repository / "tests" / "test_sample.py").write_text(
            "import unittest\n\n"
            "class SampleTests(unittest.TestCase):\n"
            "    def test_truth(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (self.repository / "artifact.txt").write_text("landed\n", encoding="utf-8")
        self._git("add", ".", cwd=self.repository)
        self._git("commit", "-m", "subject fixture", cwd=self.repository)
        self.sha = self._git("rev-parse", "HEAD", cwd=self.repository).stdout.strip()
        self._git(
            "update-ref", "refs/remotes/origin/main", self.sha, cwd=self.repository
        )

    def _git(
        self, *arguments: str, cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["/usr/bin/git", *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed

    def _claim_payload(
        self,
        *,
        sha: str | None = None,
        bank: object = None,
        ran: int = 1,
        result: str = "OK",
        artifact_sha256: str | None = None,
        deadline_seconds: int = 10,
    ) -> dict[str, object]:
        if artifact_sha256 is None:
            artifact_sha256 = hashlib.sha256(b"landed\n").hexdigest()
        return {
            "kind": "delivery_claim",
            "schema_version": 0,
            "sha": self.sha if sha is None else sha,
            "repo_path": str(self.repository),
            "bank": ["tests.test_sample"] if bank is None else bank,
            "declared": {"ran": ran, "result": result},
            "artifacts": [
                {"path": "artifact.txt", "sha256": artifact_sha256}
            ],
            "deadline_seconds": deadline_seconds,
        }

    def _send_claim(self, payload: dict[str, object] | None = None) -> dict[str, object]:
        self.assertIn("claim", inspect.signature(self.events.send).parameters)
        selected = self._claim_payload() if payload is None else payload
        return self.events.send(
            "sender",
            "recipient",
            "floati",
            str(selected["sha"]),
            "docs/evidence/v1.md",
            "V1 delivery",
            idempotency_key="v1-claim",
            claim=selected,
        )

    def _verifier(self):
        self.assertIsNotNone(
            DeliveryVerifier,
            "floati.verification must own the hermetic verification run",
        )
        return DeliveryVerifier(self.root)

    def test_v1a_claim_appends_atomically_and_replays_under_send_idempotency(self) -> None:
        first = self._send_claim()
        replay = self._send_claim()

        self.assertEqual(first, replay)
        self.assertEqual({"message", "claim", "recipient_readiness"}, set(first))
        self.assertEqual(first["message"]["id"], first["claim"]["note_ref"])
        self.assertEqual("delivery-claim-", first["claim"]["id"][:15])
        rows = self.events.event_records()
        self.assertEqual(
            ["message_envelope", "delivery_claim"],
            [row["kind"] for row in rows],
        )

    def test_v1a_claim_is_part_of_the_idempotency_payload(self) -> None:
        self._send_claim()
        changed = self._claim_payload(ran=2)

        with self.assertRaises(ProtocolRefusal) as caught:
            self._send_claim(changed)

        self.assertEqual("idempotency_conflict", caught.exception.code)
        self.assertEqual(2, len(self.events.event_records()))

    def test_v1a_malformed_claim_refuses_before_any_event_mutation(self) -> None:
        malformed = self._claim_payload()
        malformed["repo_path"] = "relative/repository"

        with self.assertRaises(ProtocolRefusal) as caught:
            self._send_claim(malformed)

        self.assertEqual("claim_repo_path_invalid", caught.exception.code)
        self.assertFalse(self.events.path.exists())

    def test_v1b_parser_uses_direct_process_status_even_when_text_says_ok(self) -> None:
        self.assertIsNotNone(parse_unittest_measurement)

        measurement = parse_unittest_measurement(
            b"Ran 1 test in 0.001s\n\nOK\n", returncode=1
        )

        self.assertEqual("FAILED", measurement["result"])
        self.assertEqual(1, measurement["returncode"])

    def test_v1b_parser_finds_anchors_in_untruncated_output(self) -> None:
        self.assertIsNotNone(parse_unittest_measurement)
        output = b"x" * 1_100_000 + b"\nRan 7 tests in 1.000s\n\nOK\n"

        measurement = parse_unittest_measurement(output, returncode=0)

        self.assertEqual(7, measurement["ran"])
        self.assertEqual("OK", measurement["result"])
        self.assertEqual(hashlib.sha256(output).hexdigest(), measurement["output_sha256"])

    def test_v1b_parser_refuses_doubled_ran_anchors(self) -> None:
        self.assertIsNotNone(parse_unittest_measurement)
        output = (
            b"Ran 1 test in 0.001s\n\nOK\n"
            b"nested noise\nRan 1 test in 0.001s\n\nOK\n"
        )

        with self.assertRaises(ProtocolRefusal) as caught:
            parse_unittest_measurement(output, returncode=0)

        self.assertEqual("test_output_unparseable", caught.exception.code)

    def test_v1b_exact_remote_banked_sha_runs_in_fresh_fleet_scratch(self) -> None:
        sent = self._send_claim()

        receipt = self._verifier().verify("verifier", sent["claim"]["id"])

        self.assertEqual("verified_match", receipt["outcome"])
        self.assertEqual(
            [
                "PYTHONDONTWRITEBYTECODE=1",
                "python3",
                "-m",
                "unittest",
                "tests.test_sample",
            ],
            receipt["runner_argv"],
        )
        self.assertEqual("claimed_bank_only", receipt["unchecked_scope"])
        self.assertTrue(receipt["scratch"]["created"])
        self.assertTrue(receipt["scratch"]["destroyed"])
        self.assertFalse(Path(receipt["scratch"]["path"]).exists())
        self.assertEqual([], self._git("worktree", "list", "--porcelain", cwd=self.repository).stdout.split(str(self.root.path / "scratch"))[1:])

    def test_v1b_unbanked_sha_refuses_without_fetch_and_receipts_unrunnable(self) -> None:
        (self.repository / "unbanked.txt").write_text("local only\n", encoding="utf-8")
        self._git("add", "unbanked.txt", cwd=self.repository)
        self._git("commit", "-m", "unbanked", cwd=self.repository)
        unbanked = self._git("rev-parse", "HEAD", cwd=self.repository).stdout.strip()
        sent = self._send_claim(self._claim_payload(sha=unbanked))

        receipt = self._verifier().verify("verifier", sent["claim"]["id"])

        self.assertEqual("verification_unrunnable", receipt["outcome"])
        self.assertEqual("sha_unbanked", receipt["reason_code"])
        self.assertIn("git fetch", receipt["remedy"])
        self.assertFalse((self.root.path / "scratch").exists())

    def test_v1b_absent_sha_refuses_before_bank_or_scratch(self) -> None:
        absent = "0" * 40
        sent = self._send_claim(self._claim_payload(sha=absent))

        receipt = self._verifier().verify("verifier", sent["claim"]["id"])

        self.assertEqual("verification_unrunnable", receipt["outcome"])
        self.assertEqual("sha_absent", receipt["reason_code"])
        self.assertIn("git fetch", receipt["remedy"])
        self.assertFalse(receipt["scratch"]["created"])
        self.assertFalse((self.root.path / "scratch").exists())

    def test_v1b_worktree_creation_failure_is_typed_checkout_failure(self) -> None:
        sent = self._send_claim()
        original_run = subprocess.run

        def fail_worktree_add(argv, *args, **kwargs):
            if "worktree" in argv and "add" in argv:
                return subprocess.CompletedProcess(argv, 128, b"", b"blocked")
            return original_run(argv, *args, **kwargs)

        with mock.patch(
            "floati.verification.subprocess.run", side_effect=fail_worktree_add
        ):
            receipt = self._verifier().verify("verifier", sent["claim"]["id"])

        self.assertEqual("verification_unrunnable", receipt["outcome"])
        self.assertEqual("checkout_failure", receipt["reason_code"])
        self.assertFalse(receipt["scratch"]["created"])
        self.assertFalse(Path(receipt["scratch"]["path"]).exists())

    def test_v1b_unresolved_bank_is_a_typed_claim_refusal_not_a_tree_failure(self) -> None:
        sent = self._send_claim(
            self._claim_payload(bank=["tests.test_does_not_exist"])
        )

        receipt = self._verifier().verify("verifier", sent["claim"]["id"])

        self.assertEqual("verification_unrunnable", receipt["outcome"])
        self.assertEqual("bank_module_unresolved", receipt["reason_code"])
        self.assertTrue(receipt["scratch"]["destroyed"])

    def test_v1b_deadline_overrun_is_unrunnable_and_cleans_the_worktree(self) -> None:
        (self.repository / "tests" / "test_sample.py").write_text(
            "import time\nimport unittest\n\n"
            "class SampleTests(unittest.TestCase):\n"
            "    def test_slow(self):\n"
            "        time.sleep(2)\n",
            encoding="utf-8",
        )
        self._git("add", "tests/test_sample.py", cwd=self.repository)
        self._git("commit", "-m", "slow bank", cwd=self.repository)
        slow_sha = self._git("rev-parse", "HEAD", cwd=self.repository).stdout.strip()
        self._git(
            "update-ref", "refs/remotes/origin/main", slow_sha, cwd=self.repository
        )
        sent = self._send_claim(
            self._claim_payload(sha=slow_sha, deadline_seconds=1)
        )

        receipt = self._verifier().verify("verifier", sent["claim"]["id"])

        self.assertEqual("verification_unrunnable", receipt["outcome"])
        self.assertEqual("deadline_exceeded", receipt["reason_code"])
        self.assertTrue(receipt["scratch"]["destroyed"])
        self.assertFalse(Path(receipt["scratch"]["path"]).exists())

    def test_v1b_dirty_scratch_is_retained_and_never_removed(self) -> None:
        (self.repository / "tests" / "test_sample.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "import unittest\n\n"
            "class SampleTests(unittest.TestCase):\n"
            "    def test_dirties_checkout(self):\n"
            "        os.chmod(Path.cwd(), 0o700)\n"
            "        Path('unexpected.txt').write_text('retain me')\n",
            encoding="utf-8",
        )
        self._git("add", "tests/test_sample.py", cwd=self.repository)
        self._git("commit", "-m", "dirty bank", cwd=self.repository)
        dirty_sha = self._git("rev-parse", "HEAD", cwd=self.repository).stdout.strip()
        self._git(
            "update-ref", "refs/remotes/origin/main", dirty_sha, cwd=self.repository
        )
        sent = self._send_claim(self._claim_payload(sha=dirty_sha))

        receipt = self._verifier().verify("verifier", sent["claim"]["id"])

        self.assertEqual("verification_unrunnable", receipt["outcome"])
        self.assertEqual("checkout_dirty", receipt["reason_code"])
        self.assertTrue(receipt["scratch"]["created"])
        self.assertFalse(receipt["scratch"]["destroyed"])
        self.assertTrue(Path(receipt["scratch"]["path"]).exists())

    def test_v1c_mismatch_carries_claim_and_measurement_side_by_side(self) -> None:
        sent = self._send_claim(
            self._claim_payload(ran=4, artifact_sha256="0" * 64)
        )

        receipt = self._verifier().verify("verifier", sent["claim"]["id"])

        self.assertEqual("verified_mismatch", receipt["outcome"])
        self.assertEqual(sent["claim"], receipt["claim"])
        self.assertEqual(1, receipt["measurement"]["declared"]["ran"])
        self.assertEqual(
            hashlib.sha256(b"landed\n").hexdigest(),
            receipt["measurement"]["artifacts"][0]["sha256"],
        )

    def test_v1c_every_outcome_is_appended_as_typed_verification_evidence(self) -> None:
        sent = self._send_claim()
        receipt = self._verifier().verify("verifier", sent["claim"]["id"])

        rows = read_records(
            self.root,
            "receipts/verifications.jsonl",
            allowed_kinds={"verification_receipt"},
        )

        self.assertEqual([receipt], rows)
        self.assertEqual("verification_receipt", rows[0]["kind"])
        self.assertEqual(str(self.repository), rows[0]["repo_path"])
        self.assertRegex(rows[0]["python_version"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(rows[0]["wall_time_seconds"], r"^\d+\.\d{6}$")

    def test_cli_claim_file_and_verify_forms_emit_one_typed_artifact(self) -> None:
        claim_path = self.base / "claim.json"
        claim_path.write_text(json.dumps(self._claim_payload()), encoding="utf-8")
        send = subprocess.run(
            [
                "python3", "-m", "floati", "send",
                "--root", str(self.root.tenant_home),
                "--from", "sender", "--to", "recipient",
                "--repo", "floati", "--sha", self.sha,
                "--doc", "docs/evidence/v1.md", "--note", "V1 delivery",
                "--idempotency-key", "cli-v1", "--claim", str(claim_path),
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, send.returncode, send.stderr)
        sent = json.loads(send.stdout)
        claim_id = sent["evidence"]["claim"]["id"]

        verified = subprocess.run(
            [
                "python3", "-m", "floati", "verify",
                "--root", str(self.root.tenant_home),
                "--as", "verifier", "--claim", claim_id, "--json",
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, verified.returncode, verified.stderr)
        self.assertEqual(1, len(verified.stdout.splitlines()))
        artifact = json.loads(verified.stdout)
        self.assertEqual("verify", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual("verified_match", artifact["evidence"]["outcome"])

    def test_cli_duplicate_claim_keys_are_typed_malformed_without_event_append(self) -> None:
        payload = self._claim_payload()
        encoded = json.dumps(payload)
        duplicated = encoded.replace(
            '"sha": "' + self.sha + '"',
            '"sha": "' + self.sha + '", "sha": "' + self.sha + '"',
            1,
        )
        claim_path = self.base / "duplicate-claim.json"
        claim_path.write_text(duplicated, encoding="utf-8")

        completed = subprocess.run(
            [
                "python3", "-m", "floati", "send",
                "--root", str(self.root.tenant_home),
                "--from", "sender", "--to", "recipient",
                "--repo", "floati", "--sha", self.sha,
                "--doc", "docs/evidence/v1.md", "--note", "V1 delivery",
                "--claim", str(claim_path),
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(20, completed.returncode)
        artifact = json.loads(completed.stderr)
        self.assertEqual("claim_malformed", artifact["evidence"]["code"])
        self.assertFalse(self.events.path.exists())

    def test_help_routes_render_restamped_send_claim_and_verify_copy(self) -> None:
        """Catches DRAFT copy escaping onto a live help surface post-restamp."""
        for arguments, needles in (
            (("send", "--help"), ("--claim PATH",)),
            (("verify", "--help"), ("floati verify",)),
        ):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    ["python3", "-m", "floati", *arguments],
                    cwd=REPOSITORY_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                for needle in needles:
                    self.assertIn(needle, completed.stdout)
                self.assertNotIn("DRAFT -", completed.stdout)


if __name__ == "__main__":
    unittest.main()
