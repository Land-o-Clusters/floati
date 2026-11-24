from __future__ import annotations

from floati import fixture_ids as public_ids

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.banked_sha_fixture import banked_send_repository
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPOSITORY_ROOT / "scripts" / "floati"
SHA = "a" * 40


def _scratch_sequencer_ready(ready_descriptor: int, home: str) -> int:
    """Observe the real CLI service boundary in an owned scratch child."""
    from floati.cli import main
    from floati.sequencer import SequencerService

    native_serve = SequencerService.serve_forever
    pending_descriptor = [ready_descriptor]

    def observed_serve(service, stop_event):
        # CLI construction has entered the native writer lease, opened its
        # socket, and installed its actual SIGTERM handler before this call.
        os.write(ready_descriptor, b"1")
        os.close(ready_descriptor)
        pending_descriptor.clear()
        return native_serve(service, stop_event)

    try:
        with mock.patch.object(SequencerService, "serve_forever", observed_serve):
            return main(["sequencer", "serve", "--root", home, "--as", "sequencer-a"])
    finally:
        if pending_descriptor:
            os.close(pending_descriptor.pop())


def root_entries(root: Path) -> dict[Path, tuple[str, bytes]]:
    return {
        path.relative_to(root): (
            "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file",
            b"" if path.is_symlink() or path.is_dir() else path.read_bytes(),
        )
        for path in root.rglob("*")
    }


class SlipCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "demo-fleet"

    def run_cli(
        self,
        *args: str,
        cwd: Path = REPOSITORY_ROOT,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )

    def artifact(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual("", result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        self.assertTrue(result.stdout.endswith("\n"))
        artifact = json.loads(result.stdout)
        self.assertEqual(
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            result.stdout,
        )
        return artifact

    def initialize(self) -> None:
        result = self.run_cli("init", "--root", str(self.home))
        self.assertEqual(0, result.returncode, result.stderr)

    def register(self, node: str, harness: str = "Codex") -> dict[str, object]:
        result = self.run_cli(
            "register", "--root", str(self.home), node, "--harness", harness
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return self.artifact(result)

    def send(self) -> dict[str, object]:
        # SEND-SHA-RESOLVES-1: sent from a repository that holds the sha.
        self.send_repo, self.send_sha = banked_send_repository(Path(self.temp.name))
        result = self.run_cli(
            "send",
            "--root", str(self.home),
            "--from", "sender",
            "--to", "recipient",
            "--repo", "floati",
            "--sha", self.send_sha,
            "--doc", "docs/evidence/checkpoint.md",
            "--note", "HM-0.5 delivered",
            cwd=self.send_repo,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return self.artifact(result)

    def test_init_emits_one_compact_json_artifact_and_is_idempotent(self) -> None:
        first = self.run_cli("init", "--root", str(self.home))
        second = self.run_cli("init", "--root", str(self.home))

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        artifact = self.artifact(first)
        self.assertEqual(0, artifact["artifact_version"])
        self.assertEqual("init", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(str(self.home.resolve()), artifact["evidence"]["root"])
        self.assertEqual("demo-fleet", artifact["evidence"]["tenant_id"])
        self.assertTrue(self.home.is_dir())

    def test_init_refuses_a_legacy_positional_root_even_when_environment_is_set(self) -> None:
        """Catches init selecting a positional root ahead of the ruled environment root."""
        environment = Path(self.temp.name) / "environment"
        legacy = Path(self.temp.name) / "legacy"
        result = subprocess.run(
            [str(LAUNCHER), "init", str(legacy)],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "FLOATI_BUS_ROOT": str(environment)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(20, result.returncode, result.stderr)
        artifact = self.artifact(result)
        self.assertEqual("init", artifact["command"])
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("arguments_invalid", artifact["evidence"]["code"])
        self.assertFalse(legacy.exists())
        self.assertFalse(environment.exists())

    def test_init_rejects_invalid_solo_inputs_before_creating_a_root(self) -> None:
        """TD1: a lexical solo refusal cannot create a new direct home."""
        cases = (
            ("hostile-node", ("--solo=--hostile-node",), "node_invalid"),
            ("empty-harness", ("--solo=valid-node", "--harness="), "role_invalid"),
        )
        for suffix, arguments, code in cases:
            with self.subTest(case=suffix):
                root = Path(self.temp.name) / f"new-fleet-{suffix}"
                result = self.run_cli("init", "--root", str(root), *arguments)

                self.assertEqual(20, result.returncode, result.stderr)
                artifact = self.artifact(result)
                self.assertEqual("init", artifact["command"])
                self.assertEqual("refused", artifact["status"])
                self.assertEqual(code, artifact["evidence"]["code"])
                self.assertFalse(root.exists())

    def test_init_rejects_terminal_unsafe_harness_before_creating_a_root(self) -> None:
        """TD1: durable-role controls and Bidi code points cannot create the solo home."""
        cases = (
            ("control", "bad\x1brole"),
            ("bidi", "bad\u202erole"),
        )
        for suffix, harness in cases:
            with self.subTest(case=suffix):
                root = Path(self.temp.name) / f"new-fleet-harness-{suffix}"
                result = self.run_cli(
                    "init",
                    "--root",
                    str(root),
                    "--solo=valid-node",
                    f"--harness={harness}",
                )

                self.assertEqual(20, result.returncode, result.stderr)
                artifact = self.artifact(result)
                self.assertEqual("init", artifact["command"])
                self.assertEqual("refused", artifact["status"])
                self.assertEqual("role_invalid", artifact["evidence"]["code"])
                self.assertFalse(root.exists())

    def test_register_stores_harness_as_role(self) -> None:
        self.initialize()
        artifact = self.register("sender", "Codex")

        self.assertEqual("register", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual("sender", artifact["evidence"]["node_id"])
        self.assertEqual("Codex", artifact["evidence"]["role"])

    def test_send_inbox_ack_and_log_round_trip_exact_notification(self) -> None:
        self.initialize()
        self.register("sender")
        self.register("recipient")

        sent = self.send()
        receipt = sent["evidence"]
        message = receipt["message"]
        self.assertEqual("send", sent["command"])
        self.assertEqual("floati", message["repo"])
        self.assertEqual(self.send_sha, message["sha"])
        self.assertEqual("docs/evidence/checkpoint.md", message["doc"])
        self.assertEqual("HM-0.5 delivered", message["note"])
        self.assertNotIn("body", message)
        self.assertNotIn("wake_cause", message)
        # Registration alone supplies no recipient-bound hook provenance.
        readiness = receipt["recipient_readiness"]
        self.assertEqual("unknown", readiness["state"])
        self.assertEqual("mechanism_evidence_incomplete", readiness["reason"])
        hook = next(fact for fact in readiness["mechanism_evidence"]
                    if fact["mechanism"] == "stop_hook")
        self.assertEqual("unknown", hook["state"])

        inbox_result = self.run_cli(
            "inbox", "--root", str(self.home), "--as", "recipient", "--peek"
        )
        self.assertEqual(0, inbox_result.returncode, inbox_result.stderr)
        inbox = self.artifact(inbox_result)
        self.assertEqual("inbox", inbox["command"])
        shown = inbox["evidence"]["messages"]
        self.assertEqual([message], [{key: row[key] for key in message} for row in shown])
        self.assertEqual([message["id"]], inbox["evidence"]["receipt"]["item_ids"])

        ack_result = self.run_cli(
            "ack",
            "--root", str(self.home),
            "--as", "recipient",
            "--session", "cli-session",
            "--id", message["id"],
        )
        self.assertEqual(0, ack_result.returncode, ack_result.stderr)
        ack = self.artifact(ack_result)
        self.assertEqual("ack", ack["command"])
        self.assertEqual([message["id"]], ack["evidence"]["item_ids"])

        log_result = self.run_cli("log", "--root", str(self.home))
        self.assertEqual(0, log_result.returncode, log_result.stderr)
        log = self.artifact(log_result)
        self.assertEqual("log", log["command"])
        self.assertEqual([message], log["evidence"]["messages"])

    def test_sequencer_help_exposes_only_status_serve_and_direct_management(self) -> None:
        """Catches a public raw append verb or an undocumented managed-mode lifecycle."""
        result = self.run_cli("sequencer", "--help")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("status", result.stdout)
        self.assertIn("serve", result.stdout)
        self.assertIn("direct", result.stdout)
        self.assertNotIn("append", result.stdout)

    def test_sequencer_status_is_read_only_and_direct_leaves_closed_epoch(self) -> None:
        """Catches status creating lock evidence or direct mode leaving managed ownership open."""
        self.initialize()
        before = sorted(path.relative_to(self.home) for path in self.home.rglob("*"))
        status = self.run_cli("sequencer", "status", "--root", str(self.home))
        self.assertEqual(0, status.returncode, status.stderr)
        observed = self.artifact(status)
        self.assertEqual("direct", observed["evidence"]["mode"])
        self.assertEqual(before, sorted(path.relative_to(self.home) for path in self.home.rglob("*")))

        direct = self.run_cli("sequencer", "direct", "--root", str(self.home), "--as", "operator-a")
        self.assertEqual(0, direct.returncode, direct.stderr)
        evidence = self.artifact(direct)["evidence"]
        self.assertEqual("direct", evidence["mode"])
        self.assertFalse(evidence["managed_epoch_open"])

    def test_send_reply_to_and_idempotency_key_are_echoed(self) -> None:
        self.initialize()
        self.register("sender")
        self.register("recipient")
        first = self.send()["evidence"]["message"]

        # SEND-SHA-RESOLVES-1: the reply is sent from the same repository
        # that holds the sha the first envelope carried.
        result = self.run_cli(
            "send",
            "--root", str(self.home),
            "--from", "recipient",
            "--to", "sender",
            "--repo", "floati",
            "--sha", self.send_sha,
            "--doc", "docs/evidence/reply.md",
            "--note", "reply",
            "--reply-to", first["id"],
            "--idempotency-key", "reply-1",
            cwd=self.send_repo,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        reply = self.artifact(result)["evidence"]["message"]
        self.assertEqual(first["id"], reply["reply_to"])
        self.assertEqual("reply-1", reply["idempotency_key"])

    def test_empty_inbox_is_intentional_silence_exit_31_without_receipt(self) -> None:
        self.initialize()
        self.register("recipient")

        result = self.run_cli(
            "inbox", "--root", str(self.home), "--as", "recipient", "--peek"
        )

        self.assertEqual(31, result.returncode)
        artifact = self.artifact(result)
        self.assertEqual("intentional_silence", artifact["status"])
        self.assertEqual([], artifact["evidence"]["messages"])
        self.assertIsNone(artifact["evidence"]["receipt"])
        self.assertEqual(
            {
                "root": str(self.home.resolve()),
                "tenant": self.home.name,
                "root_source": "explicit",
            },
            artifact["evidence"]["scope"],
        )
        self.assertFalse((self.home / "receipts" / "deliveries" / "recipient.jsonl").exists())

    def test_empty_log_is_no_result_exit_32(self) -> None:
        self.initialize()

        result = self.run_cli("log", "--root", str(self.home))

        self.assertEqual(32, result.returncode)
        artifact = self.artifact(result)
        self.assertEqual("no_result", artifact["status"])
        self.assertEqual({"messages": []}, artifact["evidence"])

    def test_every_non_init_command_without_a_root_cannot_speak_with_exit_22(self) -> None:
        cases = (
            ("register", "sender", "--harness", "Codex"),
            ("send", "--from", "sender", "--to", "recipient", "--repo", "floati",
             "--sha", SHA, "--doc", "docs/evidence/checkpoint.md", "--note", "notice"),
            ("inbox", "--as", "recipient"),
            ("ack", "--as", "recipient", "--session", "cli-session", "--id", "msg-" + "0" * 32),
            ("log",),
            ("grant", "--as", "architect-a", "--holder", public_ids.builder('a'), "--subject", "work-claims", "--epoch", "1"),
            ("grant", "revoke", "--as", "architect-a", "--holder", public_ids.builder('a'), "--subject", "work-claims", "--epoch", "1"),
        )
        for args in cases:
            with self.subTest(command=args[0]):
                result = self.run_cli(*args)
                self.assertEqual(22, result.returncode)
                artifact = self.artifact(result)
                self.assertEqual(args[0], artifact["command"])
                self.assertEqual("cannot_speak", artifact["status"])
                self.assertEqual("cannot_speak", artifact["evidence"]["code"])

    def test_lifecycle_artifacts_need_no_fleet_root_and_json_remedies_parse(self) -> None:
        """Catches lifecycle output inheriting a fleet-root or unusable JSON remedy."""

        from floati.cli import _parser, main

        parser = _parser()
        cases = (
            ("install", "--source", "/source", "--destination", "/destination", "--json"),
            ("update", "--source", "/source", "--destination", "/destination", "--json"),
            ("uninstall", "--destination", "/destination", "--dry-run", "--json"),
        )
        for arguments in cases:
            with self.subTest(command=arguments[0]):
                parsed = parser.parse_args(arguments)
                self.assertEqual(arguments[0], parsed.command)
                self.assertTrue(parsed.json)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(
            "floati.cli.DeploymentWriter.run",
            return_value={"operation": "install", "status": "installed"},
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(list(cases[0]))

        self.assertEqual(0, status)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual("ok", json.loads(stdout.getvalue())["status"])

    def test_protocol_refusal_is_one_stdout_artifact_with_exit_20(self) -> None:
        self.initialize()
        self.register("sender")
        registry_lock = self.home / "registry" / "entries.jsonl.lock"
        registry_lock.unlink()
        self.assertFalse(registry_lock.exists())
        before = root_entries(self.home)

        # SEND-SHA-RESOLVES-1: sent from a repository that holds the sha,
        # so the recipient refusal — not the absent-sha fence — answers.
        repository, sha = banked_send_repository(Path(self.temp.name))
        result = self.run_cli(
            "send", "--root", str(self.home), "--from", "sender", "--to", "unknown",
            "--repo", "floati", "--sha", sha, "--doc", "docs/evidence/checkpoint.md",
            "--note", "notice",
            cwd=repository,
        )

        self.assertEqual(20, result.returncode)
        artifact = self.artifact(result)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("recipient_unregistered", artifact["evidence"]["code"])
        self.assertEqual(
            "message refused: recipient 'unknown' is not registered; registered nodes: sender",
            artifact["evidence"]["detail"],
        )
        self.assertEqual(before, root_entries(self.home))

    def test_init_refuses_existing_file_without_mutation_or_traceback(self) -> None:
        original = b"not a direct home\n"
        self.home.write_bytes(original)

        result = self.run_cli("init", "--root", str(self.home))

        self.assertEqual(20, result.returncode)
        artifact = self.artifact(result)
        self.assertEqual("init", artifact["command"])
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("direct_home_not_directory", artifact["evidence"]["code"])
        self.assertEqual(original, self.home.read_bytes())

    def test_malformed_durable_evidence_is_integrity_failure_exit_33(self) -> None:
        self.initialize()
        (self.home / "events.jsonl").write_text("not-json\n", encoding="utf-8")

        result = self.run_cli("log", "--root", str(self.home))

        self.assertEqual(33, result.returncode)
        artifact = self.artifact(result)
        self.assertEqual("malformed_evidence", artifact["status"])
        self.assertEqual("malformed_json", artifact["evidence"]["code"])
        self.assertIsInstance(artifact["evidence"]["detail"], str)

    def test_direct_main_returns_exit_and_routes_artifact_streams(self) -> None:
        from floati.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["init", "--root", str(self.home)])
        self.assertEqual(0, exit_code)
        self.assertEqual("init", json.loads(stdout.getvalue())["command"])
        self.assertEqual("", stderr.getvalue())

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["register", "sender", "--harness", "Codex"])
        self.assertEqual(22, exit_code)
        self.assertEqual("cannot_speak", json.loads(stdout.getvalue())["evidence"]["code"])
        self.assertEqual("", stderr.getvalue())

    def test_every_command_has_static_man_page_quality_help(self) -> None:
        from floati.cli import main

        commands = (
            (), ("init",), ("register",), ("retire",), ("send",), ("verify",),
            ("journal",), ("journal", "checkpoint"), ("journal", "verify"),
            ("signature",), ("signature", "sign"), ("signature", "verify"),
            ("inbox",), ("ack",),
            ("log",), ("status",), ("watch",), ("receipts",), ("supervise",),
            ("board",), ("orchestrate",), ("plan",), ("snapshot",),
            ("grant",), ("grant", "revoke"),
            ("node",), ("node", "spawn"), ("node", "retire"),
            ("mcp",), ("mcp", "serve"),
            ("work",), ("work", "add"), ("work", "claim"),
            ("work", "complete"), ("work", "show"),
            ("install",), ("update",),
        )
        for command in commands:
            with self.subTest(command=command):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main([*command, "--help"])
                result = subprocess.CompletedProcess(
                    [*command, "--help"], code, stdout.getvalue(), stderr.getvalue(),
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("", result.stderr)
                self.assertIn("NAME\n", result.stdout)
                self.assertIn("SYNOPSIS\n", result.stdout)
                self.assertIn("DESCRIPTION\n", result.stdout)
                self.assertIn("EXIT STATUS\n", result.stdout)
                self.assertIn("EXAMPLES\n", result.stdout)
                # R-4: grant revoke previously rendered its parent's valid
                # page. Generic sections alone cannot establish exact routing.
                if command in (
                    ("grant", "revoke"),
                    ("journal", "checkpoint"), ("journal", "verify"),
                    ("signature", "sign"), ("signature", "verify"),
                    ("work", "add"), ("work", "claim"),
                    ("work", "complete"), ("work", "show"),
                ):
                    self.assertIn("floati " + " ".join(command) + " - ", result.stdout)
                if command == ("init",):
                    self.assertIn("--solo", result.stdout)
                    self.assertIn("--harness", result.stdout)
                if command in (("work", "add"), ("work", "claim")):
                    self.assertIn("solo", result.stdout.lower())
                if command in (("install",), ("update",)):
                    self.assertIn("--committed-tree", result.stdout)
                    self.assertIn("origin/main", result.stdout)
                    self.assertIn("never-prune-foreign", result.stdout)
                if command == ("log",):
                    self.assertIn("--replay", result.stdout)
                    self.assertIn("--speed", result.stdout)
                    self.assertIn("--plain", result.stdout)
                    self.assertIn("ledger", result.stdout.lower())
                if command == ("status",):
                    self.assertIn("--json", result.stdout)
                    self.assertIn("version-zero", result.stdout)
                if command == ("plan",):
                    self.assertIn("--plan", result.stdout)
                    self.assertIn("--policy", result.stdout)
                    self.assertIn("--explain", result.stdout)
                    self.assertIn("read-only", result.stdout)
                    self.assertIn("needs_operator", result.stdout)

                # Retain real launcher coverage at both routing depths; the
                # exhaustive table above uses a fresh main invocation per row.
                if command in ((), ("grant", "revoke")):
                    native = self.run_cli(*command, "--help")
                    self.assertEqual(0, native.returncode, native.stderr)
                    self.assertEqual("", native.stderr)
                    self.assertEqual(result.stdout, native.stdout)

    def test_direct_main_emits_help_on_stdout_without_artifact_wrapper(self) -> None:
        from floati.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["--help"])

        self.assertEqual(0, exit_code)
        self.assertIn("floati - inspect and operate an explicit fleet root", stdout.getvalue())
        self.assertIn("board", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_machine_artifacts_always_use_stdout_and_success_bytes_stay_fixed(self) -> None:
        """STREAM-1: status is artifact data, never a stream selector."""

        from floati.cli import _emit

        cases = (
            (
                "status",
                "ok",
                {"root": "/fleet"},
                0,
                0,
                '{"artifact_version":0,"command":"status","evidence":{"root":"/fleet"},"schema_version":0,"status":"ok"}\n',
            ),
            ("register", "refused", {"code": "example_refusal"}, 20, None, None),
            ("doctor", "degraded", {"code": "example_degraded"}, 35, None, None),
        )
        for command, status, evidence, exit_code, schema_version, exact_bytes in cases:
            with self.subTest(status=status):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    _emit(
                        command,
                        status,
                        evidence,
                        exit_code,
                        schema_version=schema_version,
                    )

                self.assertEqual("", stderr.getvalue())
                artifact = json.loads(stdout.getvalue())
                self.assertEqual(command, artifact["command"])
                self.assertEqual(status, artifact["status"])
                if exact_bytes is not None:
                    self.assertEqual(exact_bytes, stdout.getvalue())

    def test_abbreviated_long_options_are_refused(self) -> None:
        self.initialize()
        cases = (
            ("register", "--roo", str(self.home), "sender-root", "--harness", "Codex"),
            ("register", "--root", str(self.home), "sender-harness", "--har", "Codex"),
        )
        for args in cases:
            with self.subTest(option=args[1] if args[1] != "--root" else args[-2]):
                result = self.run_cli(*args)
                self.assertEqual(20, result.returncode)
                artifact = self.artifact(result)
                self.assertEqual("register", artifact["command"])
                self.assertEqual("arguments_invalid", artifact["evidence"]["code"])

    def test_absolute_launcher_anchors_to_checkout_from_outside_cwd(self) -> None:
        self.assertTrue(os.access(LAUNCHER, os.X_OK))
        outside = Path(self.temp.name) / "outside"
        unrelated = outside / "floati"
        unrelated.mkdir(parents=True)
        (unrelated / "__main__.py").write_text(
            "raise SystemExit(91)\n",
            encoding="utf-8",
        )

        result = self.run_cli("init", "--root", str(self.home), cwd=outside)

        self.assertEqual(0, result.returncode, result.stderr)
        artifact = self.artifact(result)
        self.assertEqual("init", artifact["command"])
        self.assertEqual(str(self.home.resolve()), artifact["evidence"]["root"])

        refusal = self.run_cli("register", cwd=outside)
        self.assertEqual(20, refusal.returncode)
        refusal_artifact = self.artifact(refusal)
        self.assertEqual("register", refusal_artifact["command"])
        self.assertEqual("arguments_invalid", refusal_artifact["evidence"]["code"])

    def _probe_main(self, mode: str, arguments: list[str]) -> dict[str, object]:
        script = """
import io, json, sys
from contextlib import redirect_stdout
from floati import cli

mode = sys.argv[1]
arguments = sys.argv[2:]
if mode == "help":
    from floati.helptext import help_for
    expected = help_for(arguments)
    parsers = []
    original = cli._parser
    def counting():
        value = original()
        parsers.append(value)
        return value
    cli._parser = counting
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(arguments)
    json.dump({
        "code": code,
        "parser_count": len(parsers),
        "distinct_parsers": len(set(map(id, parsers))),
        "stdout": buffer.getvalue(),
        "expected": expected,
    }, sys.stdout)
elif mode == "plain":
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(arguments)
    json.dump({
        "code": code,
        "stdout": buffer.getvalue(),
        "helptext": "floati.helptext" in sys.modules,
        "generated_help": "floati.generated_help" in sys.modules,
    }, sys.stdout)
else:
    raise SystemExit("unknown probe mode")
"""
        environment = {
            key: value
            for key, value in os.environ.items()
            if key != "FLOATI_BUS_ROOT"
        }
        environment["HOME"] = self.temp.name
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-c", script, mode, *arguments],
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        return json.loads(completed.stdout)

    def test_command_help_prints_help_for_text_and_builds_the_parser_once(self) -> None:
        """floati <cmd> --help is help_for's text, exit 0, one parser build."""
        arguments = ["status", "--help"]
        probed = self._probe_main("help", arguments)
        self.assertEqual(probed["expected"], probed["stdout"])
        self.assertEqual(0, probed["code"])
        self.assertLessEqual(probed["parser_count"], 1)
        self.assertEqual(probed["parser_count"], probed["distinct_parsers"])
        self.assertGreaterEqual(probed["parser_count"], 1)

    def test_non_help_invocation_does_not_import_helptext(self) -> None:
        """A command without --help leaves the static help modules unloaded."""
        probed = self._probe_main("plain", ["effects", "--record", "hostile"])
        self.assertNotEqual(0, probed["code"])
        self.assertFalse(probed["helptext"])
        self.assertFalse(probed["generated_help"])
        artifact = json.loads(probed["stdout"])
        self.assertEqual("effects", artifact["command"])
        self.assertNotIn("NAME\n", probed["stdout"])

    def test_help_token_passed_as_a_value_is_not_a_help_request(self) -> None:
        """--help as an option value or after -- is the value, not a help request."""
        from floati.cli import main
        from types import SimpleNamespace

        def invoke(*arguments):
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(list(arguments))
            return SimpleNamespace(returncode=code, stdout=output.getvalue(),
                                   stderr=errors.getvalue())

        initialized = invoke("init", "--root", str(self.home))
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        ordinary = invoke(
            "send", "--root", str(self.home),
            "--from", "sender", "--to", "recipient",
            "--repo", "floati", "--sha", "b" * 40,
            "--doc", "docs/evidence/checkpoint.md",
            "--note=ordinary-note",
        )
        literal = invoke(
            "send", "--root", str(self.home),
            "--from", "sender", "--to", "recipient",
            "--repo", "floati", "--sha", "b" * 40,
            "--doc", "docs/evidence/checkpoint.md",
            "--note=--help",
        )
        self.assertNotEqual(0, literal.returncode)
        self.assertFalse(literal.stdout.startswith("NAME\n"))
        self.assertEqual(ordinary.returncode, literal.returncode)
        self.assertEqual(
            self.artifact(ordinary)["evidence"]["code"],
            self.artifact(literal)["evidence"]["code"],
        )
        rejected = invoke(
            "register", "--root", str(self.home), "--harness", "Codex",
            "--", "--not-a-node",
        )
        dashed = invoke(
            "register", "--root", str(self.home), "--harness", "Codex",
            "--", "--help",
        )
        accepted = invoke(
            "register", "--root", str(self.home), "--harness", "Codex",
            "--", "node-a",
        )
        self.assertEqual(0, accepted.returncode, accepted.stderr)
        self.assertEqual("node-a", self.artifact(accepted)["evidence"]["node_id"])
        self.assertNotEqual(0, dashed.returncode)
        self.assertFalse(dashed.stdout.startswith("NAME\n"))
        self.assertEqual(rejected.returncode, dashed.returncode)
        self.assertEqual(
            self.artifact(rejected)["evidence"]["code"],
            self.artifact(dashed)["evidence"]["code"],
        )


class SequencerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "demo-fleet"

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *args], cwd=REPOSITORY_ROOT,
            text=True, capture_output=True, check=False,
        )

    def artifact(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual("", result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        return json.loads(result.stdout)

    def initialize(self) -> None:
        result = self.run_cli("init", "--root", str(self.home))
        self.assertEqual(0, result.returncode, result.stderr)

    def test_serve_holds_live_local_lock_and_sigterm_gracefully_releases_epoch(self) -> None:
        """Catches CLI exit abandoning an entered epoch or claiming service without the owner lock."""
        self.initialize()
        ready_descriptor, child_ready_descriptor = os.pipe()
        try:
            process = subprocess.Popen(
                [
                    "/usr/bin/python3", "-B", "-m", "tests.test_cli",
                    "--scratch-sequencer-ready", str(child_ready_descriptor), str(self.home),
                ],
                cwd=REPOSITORY_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(child_ready_descriptor,),
            )
        except BaseException:
            os.close(ready_descriptor)
            raise
        finally:
            os.close(child_ready_descriptor)

        def collect_owned_service():
            saved = getattr(process, "_floati_test_collection", None)
            if saved is not None:
                return saved
            before = process.poll()
            if before is None:
                process.send_signal(signal.SIGTERM)
            child_stdout, child_stderr = process.communicate()
            saved = {"pid": process.pid, "exit_before_cleanup": before,
                     "returncode": process.returncode, "stdout": child_stdout,
                     "stderr": child_stderr, "collected": True}
            process._floati_test_collection = saved
            return saved

        # LIFO cleanup collects the exact owned child before setUp's directory
        # removal, including an assertion before native service testimony.
        self.addCleanup(collect_owned_service)
        try:
            ready = os.read(ready_descriptor, 1)
        finally:
            os.close(ready_descriptor)
        evidence = collect_owned_service() if ready != b"1" else {}
        self.assertEqual(b"1", ready,
                         "serve exited before native readiness: " + json.dumps(evidence, sort_keys=True))
        status = self.run_cli("sequencer", "status", "--root", str(self.home))
        self.assertEqual(0, status.returncode, status.stderr)
        observed = self.artifact(status)["evidence"]
        self.assertTrue(observed["local_service_live"], observed)
        self.assertTrue(observed["managed_epoch_open"], observed)
        self.assertIsNone(process.poll(), "ready service exited before SIGTERM")
        collected = collect_owned_service()
        self.assertEqual(0, collected["returncode"], collected)
        self.assertEqual("", collected["stderr"])
        self.assertEqual("ok", json.loads(collected["stdout"])["status"])
        status = self.run_cli("sequencer", "status", "--root", str(self.home))
        final = self.artifact(status)["evidence"]
        self.assertFalse(final["managed_epoch_open"])
        self.assertFalse(final["local_service_live"])

class WakeEvaluateCliTests(unittest.TestCase):
    """The internal wake gate stays explicit, closed, and absent from public copy."""

    class _InProcessSetupResult:
        """Actual main return and captured streams, without a child-process claim."""

        def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _run_setup_main(self, *args: str) -> _InProcessSetupResult:
        if not args or args[0] not in ("init", "register"):
            raise ValueError("in-process setup supports only init and register")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            from floati.cli import main

            returncode = main(list(args))
        return self._InProcessSetupResult(returncode, stdout.getvalue(), stderr.getvalue())

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "demo-fleet"

    def run_cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *args], cwd=REPOSITORY_ROOT if cwd is None else cwd,
            text=True, capture_output=True, check=False,
        )

    def artifact(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual("", result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        self.assertTrue(result.stdout.endswith("\n"))
        artifact = json.loads(result.stdout)
        self.assertEqual(
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            result.stdout,
        )
        return artifact

    def initialize(self, *, native_setup: bool = False) -> None:
        run = self.run_cli if native_setup else self._run_setup_main
        result = run("init", "--root", str(self.home))
        self.assertEqual(0, result.returncode, result.stderr)

    def register(self, node: str, *, native_setup: bool = False) -> None:
        run = self.run_cli if native_setup else self._run_setup_main
        result = run(
            "register", "--root", str(self.home), node, "--harness", "Codex",
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def _wake(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            "wake-evaluate", "--root", str(self.home), "--as", "bob",
            "--idempotency-key", "wake-key", *arguments,
        )

    def _outer(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        artifact = self.artifact(result)
        self.assertEqual(1, artifact["schema_version"])
        self.assertEqual(0, artifact["artifact_version"])
        self.assertEqual("wake-evaluate", artifact["command"])
        return artifact

    def _seed(self, count: int = 1, *, native_setup: bool = False) -> None:
        self.initialize(native_setup=native_setup)
        self.register(public_ids.worker('alpha'), native_setup=native_setup)
        self.register("bob", native_setup=native_setup)
        # SEND-SHA-RESOLVES-1: seeded mail is sent from a repository that
        # holds the sha it names.
        self.send_repo, self.send_sha = banked_send_repository(Path(self.temp.name))
        for index in range(count):
            result = self.run_cli(
                "send", "--root", str(self.home), "--from", public_ids.worker('alpha'), "--to", "bob",
                "--repo", "floati", "--sha", self.send_sha,
                "--doc", f"docs/evidence/wake-{index}.md", "--note", f"wake {index}",
                "--idempotency-key", f"message-{index}",
                cwd=self.send_repo,
            )
            self.assertEqual(0, result.returncode, result.stderr)

    def test_hidden_command_emits_closed_fresh_held_and_caught_up_envelopes(self) -> None:
        """Catches a wake artifact lacking its v1 envelope or misclassifying silent work."""
        self._seed(native_setup=True)
        fresh = self._wake()
        self.assertEqual(0, fresh.returncode, fresh.stderr)
        fresh_artifact = self._outer(fresh)
        self.assertEqual("ok", fresh_artifact["status"])
        self.assertEqual("fresh_work", fresh_artifact["evidence"]["state"])
        self.assertTrue(fresh_artifact["evidence"]["wake_required"])
        self.assertIsNotNone(fresh_artifact["evidence"]["receipt"])

        held = self._wake("--idempotency-key", "held-key")
        self.assertEqual(31, held.returncode)
        held_artifact = self._outer(held)
        self.assertEqual("intentional_silence", held_artifact["status"])
        self.assertEqual("held_only", held_artifact["evidence"]["state"])
        self.assertFalse(held_artifact["evidence"]["wake_required"])
        self.assertIsNone(held_artifact["evidence"]["receipt"])

        fresh_message = fresh_artifact["evidence"]["fresh_messages"][0]
        acknowledged = self.run_cli(
            "ack", "--root", str(self.home), "--as", "bob",
            "--session", "cli-session", "--id", fresh_message["id"],
        )
        self.assertEqual(0, acknowledged.returncode, acknowledged.stderr)
        caught_up = self._wake("--idempotency-key", "caught-up-key")
        self.assertEqual(31, caught_up.returncode)
        caught_up_artifact = self._outer(caught_up)
        self.assertEqual("intentional_silence", caught_up_artifact["status"])
        self.assertEqual("caught_up", caught_up_artifact["evidence"]["state"])
        self.assertFalse(caught_up_artifact["evidence"]["wake_required"])
        self.assertIsNone(caught_up_artifact["evidence"]["receipt"])

    def test_hidden_wake_record_command_persists_actual_session_after_prompt(self) -> None:
        """Catches the host prompt succeeding without countable node/session wake evidence."""
        self._seed()
        decision_result = self._wake()
        self.assertEqual(0, decision_result.returncode, decision_result.stderr)
        decision = self._outer(decision_result)["evidence"]
        message_id = decision["fresh_messages"][0]["id"]

        result = self.run_cli(
            "wake-record", "--root", str(self.home), "--as", "bob",
            "--session", "session-018f7e9b3c137abc8def0123456789ab",
            "--id", message_id,
            "--decision", decision["receipt"]["id"],
            "--idempotency-key", "actual-prompt-action",
            "--outcome", "woke",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        artifact = self.artifact(result)
        self.assertEqual(1, artifact["schema_version"])
        self.assertEqual("wake-record", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual("wake_attempt_receipt", artifact["evidence"]["kind"])
        self.assertEqual(
            "session-018f7e9b3c137abc8def0123456789ab",
            artifact["evidence"]["acting_session_id"],
        )

    def test_hidden_command_validates_a_mixed_fresh_and_held_artifact(self) -> None:
        """Catches an envelope that loses held context when fresh work also wakes."""
        from tests.schema_validation import validate_json_schema

        self._seed()
        first = self._wake()
        self.assertEqual(0, first.returncode, first.stderr)
        sent = self.run_cli(
            "send", "--root", str(self.home), "--from", public_ids.worker('alpha'), "--to", "bob",
            "--repo", "floati", "--sha", self.send_sha, "--doc", "docs/evidence/mixed.md",
            "--note", "mixed", "--idempotency-key", "mixed-message",
            cwd=self.send_repo,
        )
        self.assertEqual(0, sent.returncode, sent.stderr)

        mixed = self._wake("--idempotency-key", "mixed-key")

        self.assertEqual(0, mixed.returncode, mixed.stderr)
        evidence = self._outer(mixed)["evidence"]
        self.assertEqual("fresh_work", evidence["state"])
        self.assertEqual(1, len(evidence["fresh_messages"]))
        self.assertEqual(1, len(evidence["held_items"]))
        validate_json_schema(
            evidence,
            REPOSITORY_ROOT / "schemas/v1/wake-decision-artifact.schema.json",
        )

    def test_hidden_command_requires_its_exact_closed_arguments_and_never_uses_environment_root(self) -> None:
        """Catches aliases, an implicit root, or an out-of-range selection limit."""
        environment_root = Path(self.temp.name) / "environment-root"
        omitted_root = subprocess.run(
            [str(LAUNCHER), "wake-evaluate", "--as", "bob", "--idempotency-key", "key"],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "FLOATI_BUS_ROOT": str(environment_root)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(20, omitted_root.returncode, omitted_root.stderr)
        omitted_artifact = self._outer(omitted_root)
        self.assertEqual("refused", omitted_artifact["status"])
        self.assertEqual("arguments_invalid", omitted_artifact["evidence"]["code"])
        self.assertFalse(environment_root.exists())

        self._seed()
        for arguments in (
            ("--limit", "0"), ("--limit", "1001"), ("--limit", "not-an-integer"),
            ("--worker-session", "worker-session", "--unknown", "value"),
        ):
            with self.subTest(arguments=arguments):
                result = self._wake(*arguments)
                self.assertEqual(20, result.returncode, result.stderr)
                artifact = self._outer(result)
                self.assertEqual("refused", artifact["status"])

    def test_hidden_command_maps_corrupt_wake_evidence_to_the_typed_v1_envelope(self) -> None:
        """Catches malformed durable testimony degrading into a silent wake result."""
        self._seed()
        (self.home / "events.jsonl").write_text("not-json\n", encoding="utf-8")

        result = self._wake()

        self.assertEqual(33, result.returncode)
        artifact = self._outer(result)
        self.assertEqual("malformed_evidence", artifact["status"])
        self.assertEqual("consumption_state_unavailable", artifact["evidence"]["code"])

    def test_hidden_command_is_absent_from_static_help_registered_help_and_copy_ledger(self) -> None:
        """Catches an internal wake control becoming a visible product command or string."""
        from floati.helptext import _RAW, help_for

        top_level = self.run_cli("--help")
        self.assertEqual(0, top_level.returncode, top_level.stderr)
        self.assertNotIn("wake-evaluate", top_level.stdout)
        self.assertNotIn("wake-evaluate", _RAW)
        self.assertIsNone(help_for(("wake-evaluate", "--help")))
        hidden_help = self.run_cli("wake-evaluate", "--help")
        self.assertEqual(20, hidden_help.returncode)
        ledger = subprocess.run(
            ["python3", "-m", "floati.copy"], cwd=REPOSITORY_ROOT,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, ledger.returncode, ledger.stderr)
        self.assertNotIn("wake-evaluate", ledger.stdout)

    def test_near_miss_invalid_command_diagnostic_names_only_public_commands(self) -> None:
        """Catches argparse advertising a hidden command through its invalid-choice detail."""
        result = self.run_cli("wake-evaluat")

        self.assertEqual(20, result.returncode)
        artifact = self.artifact(result)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("arguments_invalid", artifact["evidence"]["code"])
        self.assertIn("'init'", artifact["evidence"]["detail"])
        self.assertIn("'register'", artifact["evidence"]["detail"])
        self.assertNotIn("wake-evaluate", result.stdout)
        self.assertNotIn("wake-evaluate", result.stderr)
        self.assertNotIn("wake-callback", result.stdout)
        self.assertNotIn("wake-callback", result.stderr)

    def test_hidden_command_has_no_public_alias_and_exact_pre_dispatch_arguments(self) -> None:
        """Catches a public spelling or parser alias reaching the hidden wake evaluator."""
        from floati.cli import _parser
        from floati.errors import ProtocolRefusal

        parser = _parser()
        with self.assertRaises(ProtocolRefusal):
            parser.parse_args(["wake", "--root", str(self.home), "--as", "bob", "--idempotency-key", "key"])
        parsed = parser.parse_args([
            "wake-evaluate", "--root", str(self.home), "--as", "bob", "--idempotency-key", "key",
        ])
        self.assertEqual("wake-evaluate", parsed.command)
        self.assertEqual("bob", parsed.recipient)

    def test_wake_decision_schema_and_runtime_close_cross_field_and_boundary_variants(self) -> None:
        """Catches schema/runtime drift on closed slices, receipt linkage, and bounded totals."""
        from floati.errors import ProtocolRefusal
        from floati.wake_hold import validate_wake_decision_artifact
        from tests.schema_validation import SchemaValidationError, validate_json_schema

        self._seed(2)
        result = self._wake("--limit", "1")
        self.assertEqual(0, result.returncode, result.stderr)
        evidence = self._outer(result)["evidence"]
        schema = REPOSITORY_ROOT / "schemas/v1/wake-decision-artifact.schema.json"
        self.assertEqual(evidence, validate_wake_decision_artifact(evidence, tenant_id="demo-fleet"))
        validate_json_schema(evidence, schema)

        receipt = dict(evidence["receipt"])
        receipt["item_ids"] = []
        hostile = (
            dict(evidence, extra=True),
            {key: value for key, value in evidence.items() if key != "limit"},
            dict(evidence, worker_session_id="bad\u202e-session"),
            dict(evidence, fresh_messages=evidence["fresh_messages"] * 1001),
            dict(evidence, fresh_total=100001, fresh_truncated=True),
            dict(evidence, receipt=receipt),
            dict(evidence, fresh_messages=[], fresh_total=0, fresh_truncated=False),
        )
        for case_index, candidate in enumerate(hostile):
            with self.subTest(case=(
                "extra-field",
                "missing-limit",
                "session-bidi-U202E",
                "message-count-1001",
                "fresh-total-100001-truncated",
                "receipt-with-empty-item-ids",
                "empty-fresh-inconsistent-with-receipt",
            )[case_index]):
                with self.assertRaises(ProtocolRefusal):
                    validate_wake_decision_artifact(candidate, tenant_id="demo-fleet")
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(candidate, schema)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--scratch-sequencer-ready":
        raise SystemExit(_scratch_sequencer_ready(int(sys.argv[2]), sys.argv[3]))
    unittest.main()
