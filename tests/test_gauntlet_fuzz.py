from __future__ import annotations

import hashlib
import json
import os
import copy
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.effects import EffectProjection
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.decisions import DecisionRegister, decision_digest
from floati.runtruth import RunLedger
from floati.run_segments import SegmentConfig, SegmentedRunStore
from floati.sequencer import SequencerClient, SequencerConfig, SequencerService
from floati.records import run_admission_digest
from floati import root as root_module
from tests.test_spawn_groups import (
    SpawnGroupFixtures,
    _ManagedSpawnCase,
    _Task3Case,
)
from tests.test_effects import EffectRecordFixture
from tests.hm3i_gauntlet_fixtures import (
    CANONICAL_RUN_KINDS,
    ITEM5_OUTCOMES,
    ITEM5_RUN_OUTCOMES,
    axis_coverage_from_traces,
    assert_physical_projection,
    build_cancellation_trace,
    build_admission_case,
    build_decision_case,
    build_foc_orphan_trace,
    build_full_run_trace_set,
    build_policy_case,
    build_retry_stale_trace,
    build_success_trace,
    canonical_observation_from_records,
)
from floati.runtruth import LEGACY_RUN_KINDS


REPO_ROOT = Path(__file__).resolve().parents[1]
HOSTILE = "danger\x1b[31m\u202eoverride"
UUID7 = "018f7e9b3c117abc8def0123456789ab"
UUID7_B = "018f7e9b3c127abc8def0123456789ab"
DECISION_UUID7 = UUID7_B


def _common(prefix: str, kind: str) -> dict[str, object]:
    return {
        "schema_version": 0,
        "id": f"{prefix}{UUID7}",
        "tenant_id": "alpha",
        "timestamp": "2026-08-01T12:00:00.000Z",
        "kind": kind,
    }


def _message() -> dict[str, object]:
    return {
        **_common("msg-", "message_envelope"),
        "sender": "alice",
        "recipient": "bob",
        "repo": "slipway",
        "sha": "a" * 40,
        "doc": "docs/evidence/hm3h.md",
        "note": HOSTILE,
        "idempotency_key": "gauntlet-hostile",
    }


def _denial() -> dict[str, object]:
    return {
        **_common("denial-", "denial_receipt"),
        "attempt_id": f"attempt-{UUID7}",
        "claimed_sender": HOSTILE,
        "claimed_recipient": "bob",
        "reason_code": "unknown_sender",
    }


def _work_item() -> dict[str, object]:
    return {
        **_common("work-", "work_item"),
        "title": HOSTILE,
        "owner": "alice",
        "artifact_bindings": [],
    }


def _registry() -> dict[str, object]:
    return {
        **_common("registry-", "registry_entry"),
        "node_id": "alice",
        "role": HOSTILE,
        "state": "active",
    }


def _safe_record(factory: object) -> dict[str, object]:
    record = factory()
    if record["kind"] == "message_envelope":
        record["note"] = "safe note"
    elif record["kind"] == "denial_receipt":
        record["claimed_sender"] = "alice"
    elif record["kind"] == "work_item":
        record["title"] = "safe title"
    elif record["kind"] == "registry_entry":
        record["role"] = "worker"
    return record


def _run_created() -> dict[str, object]:
    return {"schema_version": 0, "id": f"run-created-{UUID7}", "tenant_id": "alpha",
        "timestamp": "2026-08-02T12:00:00.000Z", "kind": "run_created", "run_id": f"run-{UUID7}",
        "plan_digest": "a" * 64, "item_ids": [f"work-{UUID7}"], "dependency_edges": []}


def _attempt_opened() -> dict[str, object]:
    return {**_common("attempt-opened-", "attempt_opened"), "run_id": f"run-{UUID7}",
        "item_id": f"work-{UUID7}", "attempt_id": f"attempt-{UUID7_B}", "ordinal": 1,
        "scheduler_epoch": 1, "fence_token": "a" * 64, "max_attempts": 2,
        "backoff": {"strategy": "fixed", "base_delay_ms": 1, "cap_delay_ms": 1, "jitter": "sha256_25pct"}}


def _decision_proposal() -> dict[str, object]:
    record: dict[str, object] = {
        **_common("decision-record-", "decision_record"),
        "repository": "owner/repo",
        "decision_id": f"decision-{DECISION_UUID7}",
        "scope": {"kind": "repository"},
        "statement": "Fuzzed decision evidence stays fail closed.",
        "status": "proposed",
        "author_authority": "worker",
        "source_artifact_ids": ["run:run-018f7e9b3c137abc8def0123456789ab"],
        "task_contract_id": None,
        "decided_by": "fable",
        "supersedes": None,
    }
    record["decision_digest"] = decision_digest(record)
    return record


def _seed_decision_source(root: FloatiRoot) -> None:
    RunLedger(root).append(
        {
            "schema_version": 0,
            "id": "run-created-018f7e9b3c147abc8def0123456789ab",
            "tenant_id": root.tenant_id,
            "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "run_created",
            "run_id": "run-018f7e9b3c137abc8def0123456789ab",
            "plan_digest": "a" * 64,
            "item_ids": ["work-018f7e9b3c157abc8def0123456789ab"],
            "dependency_edges": [],
        }
    )


class ReaderFuzzGauntletTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        environment = dict(os.environ)
        environment["PYTHONPYCACHEPREFIX"] = "/tmp/slipway-hm3h-fuzz-pycache"
        return subprocess.run(
            [sys.executable, "-m", "floati", *arguments],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            timeout=5,
            check=False,
        )

    def write_record(self, root: FloatiRoot, relative: str, record: dict[str, object]) -> None:
        path = root.resolve_relative(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode_frame(record))

    @staticmethod
    def file_bytes(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }

    @staticmethod
    def tree_digest(root: Path) -> str:
        """Name the pre-write tree so lexical CLI refusal cannot hide a receipt."""
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix().encode("utf-8")
            if path.is_dir():
                digest.update(b"D\0" + relative + b"\0")
            elif path.is_file() and not path.is_symlink():
                digest.update(b"F\0" + relative + b"\0" + path.read_bytes())
            else:
                digest.update(b"L\0" + relative + b"\0")
        return digest.hexdigest()

    def reader_cases(self, home: Path) -> tuple[tuple[str, str, object, tuple[str, ...]], ...]:
        return (
            ("inbox", "events.jsonl", _message, ("inbox", "--root", str(home), "--as", "bob")),
            ("log", "events.jsonl", _message, ("log", "--root", str(home))),
            ("replay", "receipts/denials.jsonl", _denial, ("log", "--root", str(home), "--replay", "--plain")),
            ("board", "work/items.jsonl", _work_item, ("board", "--root", str(home), "--no-animation")),
            ("graph", "registry/entries.jsonl", _registry, ("graph", "--root", str(home), "--json")),
            ("doctor", "registry/entries.jsonl", _registry, ("doctor", "--root", str(home), "--source", str(REPO_ROOT), "--ref", "HEAD")),
            ("status", "registry/entries.jsonl", _registry, ("status", "--root", str(home), "--json")),
        )

    def seed_reader_dependencies(self, root: FloatiRoot, name: str) -> None:
        if name in {"inbox", "log"}:
            registry = Registry(root)
            registry.register("alice", "worker")
            registry.register("bob", "worker")
        elif name == "board":
            Registry(root).register("alice", "worker")

    def test_hostile_control_and_bidi_strings_are_typed_before_every_reader_renders(self) -> None:
        for name in ("inbox", "log", "replay", "board", "graph", "doctor", "status"):
            with self.subTest(reader=name), tempfile.TemporaryDirectory() as directory:
                home = Path(directory) / "alpha"
                root = FloatiRoot.open_direct_home(home, create=True)
                case = next(case for case in self.reader_cases(home) if case[0] == name)
                _, relative, record_factory, command = case
                self.seed_reader_dependencies(root, name)
                self.write_record(root, relative, record_factory())

                completed = self.invoke(*command)

                output = completed.stdout + completed.stderr
                self.assertEqual(33, completed.returncode, output.decode("utf-8", "replace"))
                self.assertNotIn(b"Traceback", output)
                self.assertNotIn(b"\x1b", output)
                self.assertNotIn("\u202e".encode("utf-8"), output)

    def test_malformed_truncated_duplicated_oversized_and_invalid_utf8_are_typed_by_every_reader(self) -> None:
        mutations = {
            "malformed": (lambda _frame: b"{bad}\n", "malformed_json"),
            "truncated": (lambda frame: frame[:-1], "incomplete_jsonl_line"),
            "duplicated": (lambda frame: frame + frame, "duplicate_record_id"),
            "oversized": (
                lambda _frame: b'{"note":"' + b"a" * 1_048_576 + b'"}\n',
                "record_too_large",
            ),
            "invalid_utf8": (lambda _frame: b'{"value":"\xff"}\n', "malformed_json"),
        }
        for reader in ("inbox", "log", "replay", "board", "graph", "doctor", "status"):
            for mutation, (mutate, expected_code) in mutations.items():
                with self.subTest(reader=reader, mutation=mutation), tempfile.TemporaryDirectory() as directory:
                    home = Path(directory) / "alpha"
                    root = FloatiRoot.open_direct_home(home, create=True)
                    case = next(case for case in self.reader_cases(home) if case[0] == reader)
                    _, relative, record_factory, command = case
                    self.seed_reader_dependencies(root, reader)
                    path = root.resolve_relative(relative)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    frame = encode_frame(_safe_record(record_factory))
                    path.write_bytes(mutate(frame))

                    completed = self.invoke(*command)

                    output = completed.stdout + completed.stderr
                    self.assertEqual(33, completed.returncode, output.decode("utf-8", "replace"))
                    visible_code = (
                        "consumption_state_unavailable"
                        if reader == "board"
                        else expected_code
                    )
                    self.assertIn(visible_code.encode("ascii"), output)
                    self.assertNotIn(b"Traceback", output)

    def test_causally_reordered_mail_and_work_records_are_typed_by_every_reader(self) -> None:
        original = {
            **_message(),
            "note": "original",
            "idempotency_key": "original",
        }
        reply = {
            **_message(),
            "id": f"msg-{UUID7_B}",
            "sender": "bob",
            "recipient": "alice",
            "note": "reply",
            "idempotency_key": "reply",
            "reply_to": original["id"],
        }
        item = {**_work_item(), "title": "ordered work"}
        transition = {
            **_common("transition-", "work_transition"),
            "id": f"transition-{UUID7_B}",
            "work_item_id": item["id"],
            "action": "claim",
            "actor": "alice",
            "authority_subject": "work-claims",
            "authority_epoch": 1,
            "artifact_bindings": [],
        }
        for reader in ("inbox", "log", "replay", "board", "graph", "doctor", "status"):
            with self.subTest(reader=reader), tempfile.TemporaryDirectory() as directory:
                home = Path(directory) / "alpha"
                root = FloatiRoot.open_direct_home(home, create=True)
                Registry(root).register("alice", "worker")
                if reader in {"inbox", "log"}:
                    Registry(root).register("bob", "worker")
                    relative = "events.jsonl"
                    payload = encode_frame(reply) + encode_frame(original)
                    command = (
                        ("inbox", "--root", str(home), "--as", "alice")
                        if reader == "inbox"
                        else ("log", "--root", str(home))
                    )
                else:
                    relative = "work/items.jsonl"
                    payload = encode_frame(transition) + encode_frame(item)
                    command = {
                        "replay": ("log", "--root", str(home), "--replay", "--plain"),
                        "board": ("board", "--root", str(home), "--no-animation"),
                        "graph": ("graph", "--root", str(home), "--json"),
                        "doctor": ("doctor", "--root", str(home), "--source", str(REPO_ROOT), "--ref", "HEAD"),
                        "status": ("status", "--root", str(home), "--json"),
                    }[reader]
                path = root.resolve_relative(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)

                completed = self.invoke(*command)

                output = completed.stdout + completed.stderr
                self.assertEqual(33, completed.returncode, output.decode("utf-8", "replace"))
                self.assertNotIn(b"Traceback", output)

    def test_run_ledger_fuzz_refuses_typed_malformed_duplicate_oversize_and_invalid_utf8(self) -> None:
        mutations = {
            "malformed": b"{bad}\n", "truncated": encode_frame(_run_created())[:-1],
            "duplicate": encode_frame(_run_created()) * 2, "oversize": b"x" * 65537 + b"\n",
            "invalid_utf8": b'{"bad":"\xff"}\n',
        }
        for name, payload in mutations.items():
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open(Path(directory), "alpha")
                path = root.resolve_relative(RunLedger.relative_path); path.parent.mkdir(parents=True); path.write_bytes(payload)
                with self.assertRaises(IntegrityFailure) as caught: RunLedger(root).records()
                self.assertIn(caught.exception.code, {"malformed_json", "incomplete_jsonl_line", "duplicate_record_id", "record_too_large"})
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open(Path(directory), "alpha")
            candidate = _run_created(); candidate["plan_digest"] = HOSTILE
            with self.assertRaises(ProtocolRefusal) as caught: RunLedger(root).append(candidate)
            self.assertEqual("plan_digest_invalid", caught.exception.code)
            self.assertEqual([], RunLedger(root).records())

    def test_attempt_family_unknown_field_hostile_and_causal_reorder_fail_closed(self) -> None:
        """Catches persisted attempt records that bypass strict fields or physical-order causality."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open(Path(directory), "alpha")
            malformed = _attempt_opened(); malformed["unexpected"] = HOSTILE
            path = root.resolve_relative(RunLedger.relative_path); path.parent.mkdir(parents=True)
            path.write_bytes(encode_frame(malformed))
            with self.assertRaises(IntegrityFailure) as caught:
                RunLedger(root).project()
            self.assertEqual("record_fields_invalid", caught.exception.code)
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open(Path(directory), "alpha")
            path = root.resolve_relative(RunLedger.relative_path); path.parent.mkdir(parents=True)
            path.write_bytes(encode_frame(_attempt_opened()) + encode_frame(_run_created()))
            with self.assertRaises(IntegrityFailure) as caught:
                RunLedger(root).project()
            self.assertEqual("run_missing", caught.exception.code)

    def test_decision_ledger_fuzz_refuses_hostile_and_causally_invalid_frames_without_partial_capsule(self) -> None:
        """Catches malformed decision evidence that yields a partial accepted capsule or leaks an untyped parser error."""
        invalid_status = _decision_proposal()
        invalid_status["status"] = "superseded"
        unknown_field = _decision_proposal()
        unknown_field["memory"] = "invented"
        invalid_id = _decision_proposal()
        invalid_id["decision_id"] = "decision-not-a-uuid7"
        mutations = {
            "malformed": b"{bad}\n",
            "truncated": encode_frame(_decision_proposal())[:-1],
            "duplicate": encode_frame(_decision_proposal()) * 2,
            "wrong_kind": encode_frame(_run_created()),
            "literal_superseded": encode_frame(invalid_status),
            "unknown_field": encode_frame(unknown_field),
            "invalid_logical_id": encode_frame(invalid_id),
            "invalid_utf8": b'{"value":"\xff"}\n',
        }
        for name, payload in mutations.items():
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open(Path(directory), "alpha")
                _seed_decision_source(root)
                register = DecisionRegister(root, "owner/repo")
                path = root.resolve_relative(register.relative_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                with self.assertRaises(IntegrityFailure):
                    register.capsule()

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open(Path(directory), "alpha")
            _seed_decision_source(root)
            register = DecisionRegister(root, "owner/repo")
            hostile = _decision_proposal()
            hostile["statement"] = HOSTILE
            with self.assertRaises(ProtocolRefusal) as caught:
                register.append(hostile)
            self.assertEqual("statement_invalid", caught.exception.code)
            self.assertEqual([], register.records())

    def test_hostile_cli_node_spellings_refuse_before_every_durable_entrypoint_writes(self) -> None:
        """Catches lexical node input that reaches denial, registry, or cursor writes."""
        node_inputs = ("", "--hostile-node")
        for node in node_inputs:
            field = "=" + node
            cases = (
                ("init-solo", ("init", "--root={root}", "--solo" + field)),
                ("register", ("register", "--root={root}", "--harness=Codex", "--", node)),
                ("send-from", ("send", "--root={root}", "--from" + field, "--to=recipient", "--repo=slipway", "--sha=" + "a" * 40, "--doc=docs/evidence/td1.md", "--note=td1")),
                ("send-to", ("send", "--root={root}", "--from=sender", "--to" + field, "--repo=slipway", "--sha=" + "a" * 40, "--doc=docs/evidence/td1.md", "--note=td1")),
                ("inbox", ("inbox", "--root={root}", "--as" + field)),
                ("ack", ("ack", "--root={root}", "--as" + field, "--id=msg-018f7e9b3c117abc8def0123456789ab")),
                ("receipts", ("receipts", "--root={root}", "--", node)),
                ("worker", ("worker", "run", "--root={root}", "--as" + field, "--adapter=codex")),
                ("work-owner", ("work", "add", "--root={root}", "--title=td1", "--owner" + field)),
                ("work-actor-claim", ("work", "claim", "--root={root}", "--id=work-018f7e9b3c117abc8def0123456789ab", "--as" + field)),
                ("work-actor-complete", ("work", "complete", "--root={root}", "--id=work-018f7e9b3c117abc8def0123456789ab", "--as" + field)),
            )
            for name, template in cases:
                with self.subTest(node=node or "empty", entrypoint=name), tempfile.TemporaryDirectory() as directory:
                    home = Path(directory) / "alpha"
                    FloatiRoot.open_direct_home(home, create=True)
                    before = self.tree_digest(home)
                    completed = self.invoke(
                        *(part.format(root=home) for part in template)
                    )
                    output = completed.stdout + completed.stderr
                    self.assertEqual(20, completed.returncode, output.decode("utf-8", "replace"))
                    self.assertNotIn(b"Traceback", output)
                    self.assertEqual(before, self.tree_digest(home))

    def test_hostile_init_solo_inputs_leave_a_nonexistent_root_at_zero_state(self) -> None:
        """TD1 extends init's lexical zero-state guarantee to its create-root path."""
        cases = (
            ("hostile-node", ("--solo=--hostile-node",), "node_invalid"),
            ("empty-harness", ("--solo=valid-node", "--harness="), "role_invalid"),
        )
        for suffix, arguments, code in cases:
            with self.subTest(case=suffix), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                home = base / f"new-fleet-{suffix}"
                before = self.tree_digest(base)
                completed = self.invoke("init", f"--root={home}", *arguments)
                output = completed.stdout + completed.stderr

                self.assertEqual(20, completed.returncode, output.decode("utf-8", "replace"))
                artifact = json.loads(completed.stderr)
                self.assertEqual("init", artifact["command"])
                self.assertEqual(code, artifact["evidence"]["code"])
                self.assertFalse(home.exists())
                self.assertEqual(before, self.tree_digest(base))

    def test_terminal_unsafe_solo_harnesses_leave_a_nonexistent_root_at_zero_state(self) -> None:
        """A control or Bidi role must refuse before the direct-home create path."""
        cases = (
            ("control", "bad\x1brole"),
            ("bidi", "bad\u202erole"),
        )
        for suffix, harness in cases:
            with self.subTest(case=suffix), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                home = base / f"new-fleet-harness-{suffix}"
                before = self.tree_digest(base)
                completed = self.invoke(
                    "init",
                    f"--root={home}",
                    "--solo=valid-node",
                    f"--harness={harness}",
                )
                output = completed.stdout + completed.stderr

                self.assertEqual(20, completed.returncode, output.decode("utf-8", "replace"))
                artifact = json.loads(completed.stderr)
                self.assertEqual("init", artifact["command"])
                self.assertEqual("role_invalid", artifact["evidence"]["code"])
                self.assertFalse(home.exists())
                self.assertEqual(before, self.tree_digest(base))

    def test_root_resolution_is_shared_explicit_then_environment_without_config_fallback(self) -> None:
        """Catches per-command ambient-root discovery or a config fallback that can silently select state."""
        resolver = getattr(root_module, "resolve_command_root", None)
        self.assertIsNotNone(resolver, "root-taking commands need one ruled resolver")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            explicit = FloatiRoot.open_direct_home(base / "explicit", create=True)
            environment = FloatiRoot.open_direct_home(base / "environment", create=True)
            ignored_config = FloatiRoot.open_direct_home(base / "ignored-config", create=True)
            destination = base / "prospective-install"
            scan_root = base / "enumerated-path"
            scan_root.mkdir()
            assert resolver is not None
            resolved = resolver(
                str(explicit.path),
                create=False,
                environ={"FLOATI_BUS_ROOT": str(environment.path)},
            )
            self.assertEqual(explicit.path, resolved.path)
            self.assertEqual(
                environment.path,
                resolver(None, create=False, environ={"FLOATI_BUS_ROOT": str(environment.path)}).path,
            )
            with self.assertRaises(ProtocolRefusal) as absent:
                resolver(None, create=False, environ={"FLOATI_BUS_CONFIG": str(ignored_config.path)})
            self.assertEqual("cannot_speak", absent.exception.code)

            init_explicit = subprocess.run(
                [sys.executable, "-m", "floati", "init", "--root", str(explicit.path)],
                cwd=REPO_ROOT,
                env={**os.environ, "FLOATI_BUS_ROOT": str(environment.path)},
                text=True, capture_output=True, check=False, timeout=5,
            )
            self.assertEqual(0, init_explicit.returncode, init_explicit.stderr)
            self.assertEqual(
                str(explicit.path), json.loads(init_explicit.stdout)["evidence"]["root"]
            )
            init_environment = subprocess.run(
                [sys.executable, "-m", "floati", "init"], cwd=REPO_ROOT,
                env={**os.environ, "FLOATI_BUS_ROOT": str(environment.path)},
                text=True, capture_output=True, check=False, timeout=5,
            )
            self.assertEqual(0, init_environment.returncode, init_environment.stderr)
            self.assertEqual(
                str(environment.path), json.loads(init_environment.stdout)["evidence"]["root"]
            )

            command = [
                sys.executable,
                "-m",
                "floati",
                "status",
                "--json",
                "--destination",
                str(destination),
            ]
            explicit_result = subprocess.run(
                [*command, "--root", str(explicit.path)], cwd=REPO_ROOT,
                env={
                    **os.environ,
                    "PATH": str(scan_root),
                    "FLOATI_BUS_ROOT": str(environment.path),
                },
                capture_output=True, check=False, timeout=5,
            )
            self.assertEqual(20, explicit_result.returncode, explicit_result.stderr)
            explicit_artifact = json.loads(explicit_result.stderr)
            self.assertEqual("ok", explicit_artifact["status"])
            self.assertEqual(1, explicit_artifact["evidence"]["status_schema_version"])
            self.assertEqual(
                {
                    "outcome": "affirmative_none",
                    "enumerated_roots": [str(scan_root.resolve())],
                    "found": [],
                    "reason": "Every PATH entry was checked; the installed floati answers first.",
                },
                explicit_artifact["evidence"]["installer_shadow"],
            )
            self.assertEqual(
                str(explicit.path),
                explicit_artifact["evidence"]["root"],
            )
            environment_result = subprocess.run(
                command, cwd=REPO_ROOT,
                env={
                    **os.environ,
                    "PATH": str(scan_root),
                    "FLOATI_BUS_ROOT": str(environment.path),
                },
                capture_output=True, check=False, timeout=5,
            )
            self.assertEqual(20, environment_result.returncode, environment_result.stderr)
            environment_artifact = json.loads(environment_result.stderr)
            self.assertEqual("ok", environment_artifact["status"])
            self.assertEqual(1, environment_artifact["evidence"]["status_schema_version"])
            self.assertEqual(
                "affirmative_none",
                environment_artifact["evidence"]["installer_shadow"]["outcome"],
            )
            self.assertEqual(
                str(environment.path),
                environment_artifact["evidence"]["root"],
            )
            absent_result = subprocess.run(
                command, cwd=REPO_ROOT,
                env={**os.environ, "FLOATI_BUS_CONFIG": str(ignored_config.path)},
                capture_output=True, check=False, timeout=5,
            )
            self.assertEqual(22, absent_result.returncode)
            absent_artifact = json.loads(absent_result.stderr)
            self.assertEqual("cannot_speak", absent_artifact["status"])
            self.assertEqual("cannot_speak", absent_artifact["evidence"]["code"])

    def test_every_root_taking_parser_defers_to_the_shared_resolver(self) -> None:
        """Catches a command retaining argparse-required root while its peers honor the ruling."""
        from floati import cli

        parser = cli._parser()
        cases = (
            ("init",),
            ("register", "node", "--harness", "Codex"),
            ("send", "--from", "sender", "--to", "recipient", "--repo", "slipway", "--sha", "a" * 40, "--doc", "docs/evidence/td2.md", "--note", "td2"),
            ("inbox", "--as", "recipient"),
            ("ack", "--as", "recipient", "--session", "fuzz-session", "--id", "msg-" + "0" * 32),
            ("log",),
            ("status",),
            ("graph", "--json"),
            ("doctor", "--source", str(REPO_ROOT)),
            ("watch",),
            ("receipts", "recipient"),
            ("supervise",),
            ("orchestrate", "--plan", "/tmp/td2-plan.json", "--adapter", "codex", "--deadline", "1"),
            ("grant", "--as", "architect-a", "--holder", "lane-a", "--subject", "work-claims", "--epoch", "1"),
            ("grant", "revoke", "--as", "architect-a", "--holder", "lane-a", "--subject", "work-claims", "--epoch", "1"),
            ("work", "add", "--title", "td2"),
            ("work", "claim", "--id", "work-" + "0" * 32),
            ("work", "complete", "--id", "work-" + "0" * 32),
            ("work", "show"),
            ("worker", "run", "--as", "worker", "--adapter", "codex"),
        )
        for arguments in cases:
            with self.subTest(command=" ".join(arguments[:2])):
                parsed = parser.parse_args(arguments)
                self.assertTrue(hasattr(parsed, "root"))
                self.assertIsNone(parsed.root)

        lifecycle = (
            ("install", "--source", str(REPO_ROOT), "--destination", "/tmp/td2-destination"),
            ("update", "--source", str(REPO_ROOT), "--destination", "/tmp/td2-destination"),
            ("uninstall", "--destination", "/tmp/td2-destination", "--dry-run"),
        )
        for arguments in lifecycle:
            with self.subTest(lifecycle=" ".join(arguments[:2])):
                parsed = parser.parse_args(arguments)
                self.assertFalse(hasattr(parsed, "root"))

    def test_hm3i_literal_run_inventory_foc_joins_and_malformed_prefixes_fail_closed(self) -> None:
        """Item 10 spans its 26 frozen kinds while Spawn adds exactly two cancellation kinds."""
        self.assertEqual(
            CANONICAL_RUN_KINDS | {
                "attempt_cancelled_before_start",
                "spawn_child_cancelled_without_attempt",
            },
            LEGACY_RUN_KINDS,
        )
        self.assertEqual(
            {"succeeded", "failed", "cancelled", "skipped", "needs_operator", "uncertain"},
            ITEM5_OUTCOMES,
        )
        self.assertEqual(ITEM5_OUTCOMES | {"partially_succeeded"}, ITEM5_RUN_OUTCOMES)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            traces = build_full_run_trace_set(base)
            observed_kinds = {
                str(record["kind"])
                for trace in traces
                for record in trace.records
            }
            self.assertEqual(CANONICAL_RUN_KINDS, observed_kinds)
            self.assertTrue(all(axis_coverage_from_traces("fuzz", traces).values()))
            for trace in traces:
                assert_physical_projection(trace)

            foc = traces[-1]
            bindings = foc.records_by_kind["attempt_harness_session_bound"]
            self.assertEqual(1, len(bindings))
            self.assertEqual(
                [1, 2],
                [segment["ordinal"] for segment in bindings[0]["harness_segments"]],
            )
            self.assertEqual(
                {"owner_loss", "unregister", "lease_abandonment"},
                {
                    record["orphan_class"]
                    for record in foc.records_by_kind["supervisor_orphaned"]
                },
            )
            self.assertTrue(
                all(
                    record["claim_id"] == foc.claim_id
                    and record["lease_id"] == foc.lease_id
                    and record["worker_session_id"] == foc.worker_session_id
                    for record in foc.records_by_kind["supervisor_orphaned"]
                )
            )

            success = traces[0]
            reordered = [dict(record) for record in success.records]
            reordered[0], reordered[1] = reordered[1], reordered[0]
            with self.assertRaises(IntegrityFailure):
                canonical_observation_from_records(success, reordered)
            malformed = [dict(record) for record in success.records]
            malformed[0]["unknown_field"] = True
            with self.assertRaises(IntegrityFailure):
                canonical_observation_from_records(success, malformed)

            policy = build_policy_case(
                FloatiRoot.open_direct_home(base / "policy", create=True)
            )
            self.assertEqual("DEPLOYED", policy.status)
            admission = build_admission_case(
                FloatiRoot.open_direct_home(base / "admission", create=True)
            )
            self.assertEqual("admitted", admission.outcome)
            decision = build_decision_case(
                FloatiRoot.open_direct_home(base / "decision", create=True)
            )
            self.assertEqual("accepted", decision.record["status"])
            self.assertEqual("handoff_capsule", decision.capsule["kind"])
            self.assertEqual(1, len(decision.capsule["entries"]))
            entry = decision.capsule["entries"][0]
            self.assertEqual(decision.record["decision_id"], entry["decision_id"])
            self.assertEqual(decision.record["decision_digest"], entry["decision_digest"])

    def test_hm3i_owned_trace_mutations_never_project_partial_contract_or_acceptance_truth(self) -> None:
        """One malformed physical frame at a time fails closed after an owner-built prefix."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            mutation_names = (
                "unknown_field",
                "wrong_tenant",
                "duplicate_id",
                "invalid_digest",
                "forward_frame",
                "invalid_amendment_predecessor",
                "invalid_acceptance_evidence",
                "duplicate_bounded_set",
                "hostile_control",
                "oversized",
                "truncated",
                "invalid_utf8",
            )
            for name in mutation_names:
                with self.subTest(mutation=name):
                    root = FloatiRoot.open_direct_home(base / name, create=True)
                    trace = build_success_trace(root)
                    records = copy.deepcopy(list(trace.records))
                    by_kind = {
                        str(record["kind"]): record
                        for record in records
                    }
                    if name == "unknown_field":
                        records[0]["unknown_field"] = True
                        payload = b"".join(encode_frame(record) for record in records)
                    elif name == "wrong_tenant":
                        records[0]["tenant_id"] = "other"
                        payload = b"".join(encode_frame(record) for record in records)
                    elif name == "duplicate_id":
                        records[1]["id"] = records[0]["id"]
                        payload = b"".join(encode_frame(record) for record in records)
                    elif name == "invalid_digest":
                        records[0]["plan_digest"] = "z" * 64
                        payload = b"".join(encode_frame(record) for record in records)
                    elif name == "forward_frame":
                        records[0], records[1] = records[1], records[0]
                        payload = b"".join(encode_frame(record) for record in records)
                    elif name == "invalid_amendment_predecessor":
                        by_kind["plan_amendment"]["previous_digest"] = "b" * 64
                        payload = b"".join(encode_frame(record) for record in records)
                    elif name == "invalid_acceptance_evidence":
                        by_kind["acceptance_receipt"]["evidence_bindings"] = [
                            "worker-receipt-018f7e9b3c117abc8def0123456789ab"
                        ]
                        payload = b"".join(encode_frame(record) for record in records)
                    elif name == "duplicate_bounded_set":
                        by_kind["worker_pool_bound"]["worker_ids"] = [
                            "worker-a", "worker-a"
                        ]
                        payload = b"".join(encode_frame(record) for record in records)
                    elif name == "hostile_control":
                        by_kind["dispatch_decision"]["reason_code"] = "policy\x00route"
                        payload = b"".join(encode_frame(record) for record in records)
                    elif name == "oversized":
                        payload = b"x" * 65_537 + b"\n"
                    elif name == "truncated":
                        payload = b"".join(encode_frame(record) for record in records)[:-1]
                    else:
                        payload = b'{"value":"\xff"}\n'

                    path = root.resolve_relative(RunLedger.relative_path)
                    path.write_bytes(payload)
                    with self.assertRaises(IntegrityFailure) as caught:
                        RunLedger(root).project()
                    self.assertNotIn("Traceback", str(caught.exception))

    def test_session_scoped_ack_and_append_only_retraction_do_not_cross_parties(self) -> None:
        """Fable TD3: session receipts and append-only retractions stay party-local."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            registry = Registry(root)
            for node in ("sender", "recipient"):
                registry.register(node, "worker")
            events = EventLog(root, registry)
            cursor = SparseCursor(root)
            session_a = "worker-" + uuid7_hex()
            session_b = "worker-" + uuid7_hex()
            message_a = events.send(
                "sender", "recipient", "slipway", "a" * 40,
                "docs/evidence/td3-a.md", "session a",
                idempotency_key="td3-a", worker_session_id=session_a,
            )
            message_b = events.send(
                "sender", "recipient", "slipway", "b" * 40,
                "docs/evidence/td3-b.md", "session b",
                idempotency_key="td3-b", worker_session_id=session_b,
            )

            presented_a, delivery_a = events.present(
                "recipient", worker_session_id=session_a
            )
            presented_b, delivery_b = events.present(
                "recipient", worker_session_id=session_b
            )
            self.assertEqual([message_a["id"]], [item["id"] for item in presented_a])
            self.assertEqual([message_b["id"]], [item["id"] for item in presented_b])
            self.assertIsNotNone(delivery_a)
            self.assertIsNotNone(delivery_b)
            self.assertNotEqual(
                cursor.delivery_path_for("recipient", worker_session_id=session_a),
                cursor.delivery_path_for("recipient", worker_session_id=session_b),
            )
            acks_before = cursor.path_for("recipient", worker_session_id=session_b)
            before = acks_before.read_bytes() if acks_before.exists() else b""
            with self.assertRaises(ProtocolRefusal):
                cursor.ack(
                    "recipient", [str(message_a["id"])],
                    acting_session_id="fuzz-session", worker_session_id=session_b
                )
            after = acks_before.read_bytes() if acks_before.exists() else b""
            self.assertEqual(before, after)

            retraction = events.retract(
                str(message_a["id"]),
                worker_session_id=session_a,
                reason="sent_in_error",
                author="sender",
            )
            self.assertEqual("message_retracted", retraction["kind"])
            self.assertTrue(str(retraction["id"]).startswith("ret-"))
            self.assertEqual(
                {
                    "schema_version", "id", "tenant_id", "timestamp", "kind",
                    "retracted_message_id", "worker_session_id", "reason", "author",
                },
                set(retraction),
            )
            self.assertEqual(message_a["id"], retraction["retracted_message_id"])
            self.assertEqual(session_a, retraction["worker_session_id"])
            self.assertEqual("sent_in_error", retraction["reason"])
            self.assertEqual(
                ["message_envelope", "message_envelope", "message_retracted"],
                [record["kind"] for record in events.event_records()],
            )
            self.assertIn(
                message_a["id"], [record["id"] for record in events.records()]
            )
            self.assertEqual([], events.present("recipient", worker_session_id=session_a)[0])
            still_b, _ = events.present("recipient", worker_session_id=session_b)
            self.assertEqual([message_b["id"]], [item["id"] for item in still_b])
            cursor.ack(
                "recipient", [str(message_b["id"])],
                acting_session_id="fuzz-session", worker_session_id=session_b
            )
            self.assertEqual(
                frozenset(), cursor.acked_ids("recipient", worker_session_id=session_a)
            )
            self.assertEqual(
                frozenset({str(message_b["id"])}),
                cursor.acked_ids("recipient", worker_session_id=session_b),
            )

    def test_legacy_attempt_binding_is_literal_and_dead_holder_projects_stale_send(self) -> None:
        """Fable TD4: partial bindings collapse to legacy; dead lease sends never renew."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            registry = Registry(root)
            for node in ("sender", "recipient", "holder"):
                registry.register(node, "worker")
            events = EventLog(root, registry)
            session = "worker-" + uuid7_hex()
            legacy = events.send(
                "sender", "recipient", "slipway", "a" * 40,
                "docs/evidence/td4-legacy.md", "legacy",
                idempotency_key="td4-legacy",
            )
            partial = events.send(
                "sender", "recipient", "slipway", "b" * 40,
                "docs/evidence/td4-partial.md", "partial",
                idempotency_key="td4-partial", worker_session_id=session,
                attempt_binding={"attempt_id": "attempt-" + uuid7_hex()},
            )
            self.assertEqual("absent_legacy", legacy["attempt_binding"])
            self.assertEqual("absent_legacy", partial["attempt_binding"])

            binding = {
                "attempt_id": "attempt-" + uuid7_hex(),
                "claim_id": "claim-" + uuid7_hex(),
                "lease_id": "lease-" + uuid7_hex(),
                "worker_session_id": session,
            }
            now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
            grant = AuthorityGrantStore(root).claim(
                str(binding["lease_id"]), "holder", 60, 60, now
            )
            AuthorityGrantStore(root).expire(
                str(binding["lease_id"]), "holder", int(grant["epoch"]),
                now + timedelta(seconds=1),
            )
            stale = events.send(
                "sender", "recipient", "slipway", "c" * 40,
                "docs/evidence/td4-stale.md", "stale",
                idempotency_key="td4-stale", worker_session_id=session,
                attempt_binding=binding,
            )
            grant_path = AuthorityGrantStore(root).path_for(str(binding["lease_id"]))
            before = grant_path.read_bytes()
            projections = events.stale_send_projection()
            self.assertEqual(before, grant_path.read_bytes())
            projection = next(
                item for item in projections if item["message_id"] == stale["id"]
            )
            self.assertEqual(
                {
                    "state", "message_id", "holder_lease_id",
                    "lease_state_at_projection", "physical_frame", "renews",
                },
                set(projection),
            )
            self.assertEqual("stale_send", projection["state"])
            self.assertEqual(binding["lease_id"], projection["holder_lease_id"])
            self.assertEqual("expired", projection["lease_state_at_projection"])
            self.assertIsInstance(projection["physical_frame"], int)
            self.assertIs(False, projection["renews"])


class ApprovalSuspensionHostileGauntletTests(unittest.TestCase):
    """Adversarial proof for the four approval-suspension durable frames."""

    @staticmethod
    def bytes_under(path: Path) -> dict[str, bytes]:
        return {
            item.relative_to(path).as_posix(): item.read_bytes()
            for item in sorted(path.rglob("*"))
            if item.is_file() and not item.is_symlink()
        }

    @staticmethod
    def replace_record(
        path: Path, record_id: str, replacement: bytes | dict[str, object]
    ) -> None:
        lines = path.read_bytes().splitlines(keepends=True)
        index = next(
            index
            for index, line in enumerate(lines)
            if json.loads(line)["id"] == record_id
        )
        lines[index] = (
            encode_frame(replacement)
            if isinstance(replacement, dict)
            else replacement
        )
        path.write_bytes(b"".join(lines))

    def test_new_frames_refuse_physical_corruption_without_repairing_bytes(self) -> None:
        """Catches any new reader skipping corrupt truth or repairing it on refusal."""
        from tests.test_approval_suspension import _DirectSuspensionContext

        with tempfile.TemporaryDirectory() as directory:
            positive = _DirectSuspensionContext(Path(directory) / "positive")
            positive.prepare_approved_resume()
            positive.controller.consume(**positive.consume_args)
            before = self.bytes_under(positive.home)
            self.assertEqual("resumed", positive.attempt_state()["state"])
            self.assertEqual(before, self.bytes_under(positive.home))

        mutations = {
            "malformed": lambda _frame, _record_id: b"{bad}\n",
            "truncated": lambda frame, _record_id: frame[:-1],
            "duplicate_key": lambda frame, record_id: (
                frame.rstrip(b"\n")[:-1]
                + b',"id":"'
                + record_id.encode("ascii")
                + b'"}\n'
            ),
            "oversized": lambda _frame, _record_id: (
                b'{"padding":"' + b"a" * 1_048_576 + b'"}\n'
            ),
            "non_utf8": lambda _frame, _record_id: b'{"value":"\xff"}\n',
        }
        kinds = (
            "approval_request",
            "approval_decision",
            "attempt_suspended_for_approval",
            "approval_consumed_for_resume",
        )
        for kind in kinds:
            for mutation_name, mutate in mutations.items():
                with self.subTest(kind=kind, mutation=mutation_name), tempfile.TemporaryDirectory() as directory:
                    context = _DirectSuspensionContext(Path(directory) / "case")
                    if kind == "approval_request":
                        record = context.request
                        path = context.root.resolve_relative(context.approvals.request_path)
                        operation = lambda: context.controller.suspend(**context.suspend_args)
                    elif kind == "approval_decision":
                        context.prepare_approved_resume()
                        record = context.decision
                        path = context.root.resolve_relative(context.approvals.decision_path)
                        operation = lambda: context.controller.consume(**context.consume_args)
                    elif kind == "attempt_suspended_for_approval":
                        record = context.controller.suspend(**context.suspend_args)
                        path = context.root.resolve_relative(RunLedger.relative_path)
                        operation = context.ledger.project
                    else:
                        context.prepare_approved_resume()
                        record = context.controller.consume(**context.consume_args)
                        path = context.root.resolve_relative(RunLedger.relative_path)
                        operation = context.ledger.project
                    frame = encode_frame(record)
                    self.replace_record(path, str(record["id"]), mutate(frame, str(record["id"])))
                    before = self.bytes_under(context.home)
                    with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
                        operation()
                    self.assertEqual(before, self.bytes_under(context.home))

    def test_causal_tamper_authority_and_workspace_attacks_preserve_bytes(self) -> None:
        """Catches reordered or divergent evidence becoming a second durable truth."""
        from tests.test_approval_suspension import (
            DIRECT_NOW,
            _DirectSuspensionContext,
        )

        with tempfile.TemporaryDirectory() as directory:
            positive = _DirectSuspensionContext(Path(directory) / "positive")
            positive.prepare_approved_resume()
            positive.controller.consume(**positive.consume_args)
            before = self.bytes_under(positive.home)
            positive.controller.consume(**positive.consume_args)
            self.assertEqual(before, self.bytes_under(positive.home))

        with tempfile.TemporaryDirectory() as directory:
            reordered = _DirectSuspensionContext(Path(directory) / "reordered")
            reordered.prepare_approved_resume()
            consumed = reordered.controller.consume(**reordered.consume_args)
            suspended = reordered.suspension_records()[0]
            path = reordered.root.resolve_relative(RunLedger.relative_path)
            lines = path.read_bytes().splitlines(keepends=True)
            positions = {
                json.loads(line)["id"]: index for index, line in enumerate(lines)
            }
            left, right = positions[suspended["id"]], positions[consumed["id"]]
            lines[left], lines[right] = lines[right], lines[left]
            path.write_bytes(b"".join(lines))
            before = self.bytes_under(reordered.home)
            with self.assertRaises(IntegrityFailure):
                reordered.ledger.project()
            self.assertEqual(before, self.bytes_under(reordered.home))

        tamper_cases = ("request", "decision_id", "decision_scope", "decision_time")
        for case in tamper_cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                context = _DirectSuspensionContext(Path(directory) / case)
                context.prepare_approved_resume()
                if case == "request":
                    path = context.root.resolve_relative(context.approvals.request_path)
                    changed = dict(context.request, exact_action_digest="c" * 64)
                    self.replace_record(path, str(context.request["id"]), changed)
                    operation = lambda: context.controller.suspend(**context.suspend_args)
                else:
                    path = context.root.resolve_relative(context.approvals.decision_path)
                    changed = dict(context.decision)
                    if case == "decision_id":
                        changed["request_id"] = context.changed_action_request["id"]
                    elif case == "decision_scope":
                        changed["granted_scope"] = "repo:other"
                    else:
                        changed["timestamp"] = "2026-08-09T11:59:59.000Z"
                        changed["decided_at"] = "2026-08-09T11:59:59.000Z"
                    self.replace_record(path, str(context.decision["id"]), changed)
                    operation = lambda: context.controller.consume(**context.consume_args)
                before = self.bytes_under(context.home)
                with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
                    operation()
                self.assertEqual(before, self.bytes_under(context.home))

        with tempfile.TemporaryDirectory() as directory:
            replay = _DirectSuspensionContext(Path(directory) / "authority")
            replay.controller.suspend(**replay.suspend_args)
            authority_path = replay.root.resolve_relative(
                "authority-grants/execute-run.jsonl"
            )
            rows = [json.loads(line) for line in authority_path.read_bytes().splitlines()]
            rows[-1]["state"] = "active"
            rows[-1]["released_at"] = None
            authority_path.write_bytes(b"".join(encode_frame(row) for row in rows))
            replay.approve()
            replay.consume_args = {
                "run_id": replay.run_id,
                "item_id": replay.item_id,
                "attempt_id": replay.attempt_id,
                "approval_decision_id": replay.decision["id"],
                "workspace_checkpoint": dict(replay.checkpoint),
                "resume_authority_subject": "execute-run",
                "resume_authority_holder": "worker-a",
                "resume_authority_epoch": replay.execution_authority["epoch"],
                "now": DIRECT_NOW + timedelta(seconds=5),
            }
            before = self.bytes_under(replay.home)
            with self.assertRaises(ProtocolRefusal):
                replay.controller.consume(**replay.consume_args)
            self.assertEqual(before, self.bytes_under(replay.home))

        with tempfile.TemporaryDirectory() as directory:
            release_attack = _DirectSuspensionContext(Path(directory) / "newer")
            release_attack.controller.suspend(**release_attack.suspend_args)
            newer = release_attack.activate_resume(holder="worker-b")
            before = self.bytes_under(release_attack.home)
            release_attack.controller.suspend(**release_attack.suspend_args)
            self.assertEqual("active", release_attack.authority_tail()["state"])
            self.assertEqual(newer["id"], release_attack.authority_tail()["id"])
            self.assertEqual(before, self.bytes_under(release_attack.home))

        with tempfile.TemporaryDirectory() as directory:
            collision = _DirectSuspensionContext(Path(directory) / "workspace")
            collision.controller.suspend(**collision.suspend_args)
            collision.run_id = "run-" + uuid7_hex()
            collision._seed_started_attempt()
            authority = collision.authorities.claim(
                "execute-run", "worker-b", 600, 600, DIRECT_NOW + timedelta(seconds=4)
            )
            arguments = dict(
                collision.suspend_args,
                run_id=collision.run_id,
                attempt_id=collision.attempt_id,
                execution_authority_holder="worker-b",
                execution_authority_epoch=authority["epoch"],
                now=DIRECT_NOW + timedelta(seconds=5),
            )
            before = self.bytes_under(collision.home)
            with self.assertRaises(ProtocolRefusal) as caught:
                collision.controller.suspend(**arguments)
            self.assertEqual("workspace_reserved", caught.exception.code)
            self.assertEqual(before, self.bytes_under(collision.home))

    def test_raw_private_socket_frames_refuse_after_lawful_controls(self) -> None:
        """Catches raw clients acquiring either private suspension append kind."""
        from tests.test_approval_suspension import (
            DIRECT_NOW,
            _ManagedSuspensionContext,
        )

        with tempfile.TemporaryDirectory() as directory:
            managed = _ManagedSuspensionContext(
                Path(directory) / "managed",
                service_now=DIRECT_NOW + timedelta(seconds=2),
            )
            try:
                suspended = managed.controller.suspend(**managed.suspend_args)
                before = self.bytes_under(managed.home)
                response = managed.raw_append(suspended)
                self.assertEqual(
                    ("refused", "suspension_controller_only"),
                    (response["status"], response["code"]),
                )
                self.assertEqual(before, self.bytes_under(managed.home))

                managed.approve()
                authority = managed.activate_resume()
                managed.consume_args = {
                    "run_id": managed.run_id,
                    "item_id": managed.item_id,
                    "attempt_id": managed.attempt_id,
                    "approval_decision_id": managed.decision["id"],
                    "workspace_checkpoint": dict(managed.checkpoint),
                    "resume_authority_subject": "execute-run",
                    "resume_authority_holder": "worker-b",
                    "resume_authority_epoch": authority["epoch"],
                    "now": DIRECT_NOW + timedelta(seconds=5),
                }
                managed.service_now = DIRECT_NOW + timedelta(seconds=5)
                consumed = managed.controller.consume(**managed.consume_args)
                before = self.bytes_under(managed.home)
                response = managed.raw_append(consumed)
                self.assertEqual(
                    ("refused", "suspension_controller_only"),
                    (response["status"], response["code"]),
                )
                self.assertEqual(before, self.bytes_under(managed.home))
            finally:
                managed.close()


class SpawnGroupHostileGauntletTests(unittest.TestCase):
    """Cross-boundary hostile proof for the complete governed spawn lifecycle."""

    def lawful_full_lifecycle(self) -> _Task3Case:
        from floati.runtruth import RunProjection

        case = _Task3Case(self)
        created, amendment = case.activate(
            join_mode="all_terminal",
            on_child_failure="continue_until_join_impossible",
        )
        rejected = case.reject()
        closed = case.controller.close_group(
            case.run_id, created["id"], now=case.now(3602),
        )
        _runner, worker_result = case.run_worker()
        self.assertEqual("complete", worker_result["transition"])
        observation = case.ledger.project().run(case.run_id)[
            "descendant_observation_close"
        ][case.opened["attempt_id"]]
        self.assertEqual(created["id"], amendment["spawn_group_id"])
        self.assertEqual("child_rejected", rejected["kind"])
        self.assertEqual(("satisfied", "all_members_terminal"), (
            closed["outcome"], closed["close_reason"],
        ))
        self.assertEqual([], observation["observed_descendant_ids"])

        complete = SpawnGroupFixtures()
        started = complete.started_parent()
        complete_group = complete.group(
            on_child_failure="continue_until_join_impossible",
        )
        complete_amendment = complete.amendment(complete_group)
        complete_rejected = complete.rejected(complete_group, complete_amendment)
        complete_close = complete.close(
            complete_group,
            complete_amendment,
            outcome="satisfied",
            close_reason="all_members_terminal",
            rejected_item_ids=[complete.child],
        )
        result_records, receipt = complete.parent_result_records(started)
        terminal = complete.parent_terminal(
            started,
            terminal_state="completed",
            policy_class=None,
            reason_code="completed",
        )
        projection = RunProjection.from_records(
            [
                *started,
                complete_group,
                complete_amendment,
                complete_rejected,
                complete_close,
                complete.observation_close(),
                *result_records,
                terminal,
            ],
            worker_receipts=[receipt],
            integrity=False,
        ).run(complete.run_id)
        self.assertEqual(
            "completed",
            projection["attempts"][complete.attempt]["terminal"]["terminal_state"],
        )
        return case

    @staticmethod
    def run_bytes(case: _Task3Case) -> bytes:
        return case.root.resolve_relative(RunLedger.relative_path).read_bytes()

    @staticmethod
    def replace_record(
        case: _Task3Case,
        record_id: str,
        change: object,
    ) -> bytes:
        path = case.root.resolve_relative(RunLedger.relative_path)
        records = [json.loads(line) for line in path.read_bytes().splitlines()]
        for record in records:
            if record.get("id") == record_id:
                change(record)
                break
        else:
            raise AssertionError(f"missing fixture record {record_id}")
        path.write_bytes(b"".join(encode_frame(record) for record in records))
        return path.read_bytes()

    def test_spawn_frames_refuse_duplicate_truncated_oversize_non_utf8_and_non_finite_bytes(self) -> None:
        """Catches corrupt spawn truth being skipped, normalized, or repaired by a writer."""

        expected = {
            "duplicate_key": "duplicate_json_key",
            "truncated": "incomplete_jsonl_line",
            "oversize": "record_too_large",
            "non_utf8": "malformed_json",
            "non_finite": "plan_digest_invalid",
        }
        for hostile, code in expected.items():
            with self.subTest(hostile=hostile):
                case = self.lawful_full_lifecycle()
                ledger = RunLedger(case.root)
                lawful = ledger.records()
                self.assertIn("spawn_group_closed", {row["kind"] for row in lawful})
                path = case.root.resolve_relative(RunLedger.relative_path)
                lines = path.read_bytes().splitlines(keepends=True)
                if hostile == "duplicate_key":
                    frame = lines[-1].rstrip(b"\n")
                    lines[-1] = frame[:-1] + b',"kind":"attempt_terminal"}\n'
                    path.write_bytes(b"".join(lines))
                elif hostile == "truncated":
                    path.write_bytes(b"".join(lines)[:-1])
                elif hostile == "oversize":
                    path.write_bytes(
                        b"".join(lines) + b'{"padding":"' + b"x" * 65536 + b'"}\n'
                    )
                elif hostile == "non_utf8":
                    path.write_bytes(b"".join(lines) + b'{"padding":"\xff"}\n')
                else:
                    first = lines[0]
                    first = first.replace(
                        b'"plan_digest":"' + bytes(str(lawful[0]["plan_digest"]), "ascii") + b'"',
                        b'"plan_digest":NaN',
                    )
                    self.assertNotEqual(lines[0], first)
                    path.write_bytes(first + b"".join(lines[1:]))
                corrupt = path.read_bytes()
                with self.assertRaises(IntegrityFailure) as reader:
                    ledger.records()
                self.assertEqual(code, reader.exception.code)
                with self.assertRaises((IntegrityFailure, ProtocolRefusal)) as writer:
                    ledger.append(copy.deepcopy(lawful[0]))
                self.assertEqual(code, writer.exception.code)
                self.assertEqual(corrupt, path.read_bytes())

    def test_spawn_graph_digest_membership_and_bound_hostiles_refuse_without_append(self) -> None:
        """Catches caller-controlled graph truth or resource widening crossing activation."""

        self.lawful_full_lifecycle()
        from unittest.mock import patch

        missing_enablement = _Task3Case(self)
        observed_bytes: dict[str, bytes] = {}
        bind_policy = missing_enablement.controller.bind_attempt_policy

        def bind_without_enablement(*args: object, **kwargs: object) -> object:
            observed_bytes["before"] = self.run_bytes(missing_enablement)
            try:
                return bind_policy(*args, **kwargs)
            finally:
                observed_bytes["after"] = self.run_bytes(missing_enablement)

        with patch.object(
            missing_enablement.controller,
            "bind_attempt_policy",
            side_effect=bind_without_enablement,
        ):
            with self.assertRaises(ProtocolRefusal) as disabled:
                missing_enablement.prepare_parent(enable=False)
        self.assertEqual("spawn_admission_disabled", disabled.exception.code)
        self.assertEqual(observed_bytes["before"], observed_bytes["after"])
        self.assertNotIn(
            "attempt_spawn_policy_bound",
            {row["kind"] for row in missing_enablement.ledger.records()},
        )

        pre_admission = _Task3Case(self)
        pre_admission.activate()
        before = self.run_bytes(pre_admission)
        from floati.scheduler import RetryPolicy
        with self.assertRaises(ProtocolRefusal) as launch:
            pre_admission.scheduler.open_attempt(
                pre_admission.run_id,
                pre_admission.child,
                RetryPolicy(1, 0, 0, strategy="fixed"),
                1,
                now=pre_admission.now(8),
            )
        self.assertEqual("spawn_child_admission_missing", launch.exception.code)
        self.assertEqual(before, self.run_bytes(pre_admission))

        divergent = _Task3Case(self)
        divergent.activate()
        changed_child = "work-" + uuid7_hex()
        changed = divergent.child_descriptor(
            item_id=changed_child,
            task_contract_id="task-contract-" + uuid7_hex(),
            workspace_key="workspace-changed",
            concurrency_key="concurrency-changed",
        )
        before = self.run_bytes(divergent)
        with self.assertRaises(ProtocolRefusal) as membership:
            divergent.controller.create_group(**divergent.create_kwargs(
                children=[changed],
                dependency_edges=[{
                    "source": divergent.parent,
                    "target": changed_child,
                    "requires": "accepted",
                    "failure_policy": "fail_run",
                }],
            ))
        self.assertEqual("spawn_group_input_divergent", membership.exception.code)
        self.assertEqual(before, self.run_bytes(divergent))

        hostile_requests = (
            ("graph", "spawn_admission_not_admitted", lambda case: case.create_kwargs(
                dependency_edges=[{
                    "source": case.parent,
                    "target": "work-" + uuid7_hex(),
                    "requires": "accepted",
                    "failure_policy": "fail_run",
                }],
            )),
            ("limit", "spawn_group_widening", lambda case: case.create_kwargs(
                max_children=3,
            )),
            ("capability", "spawn_capability_widening", lambda case: case.create_kwargs(
                children=[case.child_descriptor(
                    capability_ceiling=["review", "workspace_write"],
                )],
            )),
            ("budget", "spawn_budget_widening", lambda case: case.create_kwargs(
                aggregate_budget=[{"budget_id": "build", "amount": 3}],
            )),
        )
        for label, refusal_code, arguments in hostile_requests:
            with self.subTest(hostile=label):
                case = _Task3Case(self)
                case.prepare_parent()
                before = self.run_bytes(case)
                with self.assertRaises(ProtocolRefusal) as caught:
                    case.controller.create_group(**arguments(case))
                self.assertEqual(refusal_code, caught.exception.code)
                self.assertEqual(before, self.run_bytes(case))

        causal = _Task3Case(self)
        created, amendment = causal.activate()
        path = causal.root.resolve_relative(RunLedger.relative_path)
        records = [json.loads(line) for line in path.read_bytes().splitlines()]
        positions = {row["id"]: index for index, row in enumerate(records)}
        left, right = positions[created["id"]], positions[amendment["id"]]
        records[left], records[right] = records[right], records[left]
        path.write_bytes(b"".join(encode_frame(row) for row in records))
        corrupt = path.read_bytes()
        with self.assertRaises(IntegrityFailure) as reordered:
            causal.ledger.project()
        self.assertEqual("spawn_membership_immutable", reordered.exception.code)
        writer_candidate = dict(
            created,
            id="spawn-group-created-" + uuid7_hex(),
            group_key="writer-probe",
        )
        with self.assertRaises(ProtocolRefusal) as reordered_writer:
            causal.ledger._append_spawn_group(
                writer_candidate,
                causal.ledger._spawn_group_capability_for(causal.controller),
            )
        self.assertEqual("spawn_membership_immutable", reordered_writer.exception.code)
        self.assertEqual(corrupt, path.read_bytes())

        digest = _Task3Case(self)
        _created, amendment = digest.activate()
        corrupt = self.replace_record(
            digest,
            str(amendment["id"]),
            lambda row: row.update(plan_digest="f" * 64),
        )
        with self.assertRaises(IntegrityFailure) as changed_digest:
            digest.ledger.project()
        self.assertEqual("spawn_plan_digest_invalid", changed_digest.exception.code)
        writer_candidate = dict(
            digest.created,
            id="spawn-group-created-" + uuid7_hex(),
            group_key="writer-probe",
        )
        with self.assertRaises(ProtocolRefusal) as digest_writer:
            digest.ledger._append_spawn_group(
                writer_candidate,
                digest.ledger._spawn_group_capability_for(digest.controller),
            )
        self.assertEqual("spawn_plan_digest_invalid", digest_writer.exception.code)
        self.assertEqual(corrupt, self.run_bytes(digest))

        chain = _Task3Case(self)
        _created, amendment = chain.activate()
        corrupt = self.replace_record(
            chain,
            str(amendment["id"]),
            lambda row: row.update(previous_admission_digest="f" * 64),
        )
        with self.assertRaises(IntegrityFailure) as drift:
            chain.ledger.project()
        self.assertEqual("spawn_admission_chain_invalid", drift.exception.code)
        writer_candidate = dict(
            chain.created,
            id="spawn-group-created-" + uuid7_hex(),
            group_key="writer-probe",
        )
        with self.assertRaises(ProtocolRefusal) as chain_writer:
            chain.ledger._append_spawn_group(
                writer_candidate,
                chain.ledger._spawn_group_capability_for(chain.controller),
            )
        self.assertEqual("spawn_admission_chain_invalid", chain_writer.exception.code)
        self.assertEqual(corrupt, self.run_bytes(chain))

    def test_spawn_cancellation_join_and_terminal_hostiles_cannot_forge_lifecycle_truth(self) -> None:
        """Catches races or caller-nominated sets bypassing the physical join prefix."""

        self.lawful_full_lifecycle()
        from floati.cancellation import CancellationCoordinator
        from floati.errors import DurabilityFailure
        from floati.scheduler import RetryPolicy
        from unittest.mock import patch

        pending = _Task3Case(self)
        pending.prepare_parent()
        original = pending.ledger._append_spawn_group
        with patch.object(
            pending.ledger,
            "_append_spawn_group",
            side_effect=lambda record, *args, **kwargs: (
                (_ for _ in ()).throw(DurabilityFailure("jsonl_fsync_failed", "activation"))
                if record["kind"] == "plan_amendment"
                else original(record, *args, **kwargs)
            ),
        ):
            with self.assertRaises(DurabilityFailure):
                pending.controller.create_group(**pending.create_kwargs())
        before = self.run_bytes(pending)
        with self.assertRaises(ProtocolRefusal) as terminal_race:
            pending.scheduler.terminal_attempt(
                pending.run_id,
                pending.parent,
                str(pending.opened["attempt_id"]),
                "failed",
                "permanent",
                "malformed_evidence",
                "idempotent",
                now=pending.now(8),
            )
        self.assertEqual("spawn_group_pending", terminal_race.exception.code)
        self.assertEqual(before, self.run_bytes(pending))
        CancellationCoordinator(pending.ledger).request(
            pending.run_id, {}, item_id=pending.parent, now=pending.now(9),
        )
        pending_group = next(iter(
            pending.ledger.project().run(pending.run_id)["spawn_groups"].values()
        ))
        self.assertEqual("aborted", pending_group["state"])

        cancelled = _Task3Case(self)
        cancelled.activate()
        cancelled.admit()
        resolved = CancellationCoordinator(cancelled.ledger).request_exact_items(
            cancelled.run_id,
            [cancelled.child],
            {},
            spawn_group_id=cancelled.created["id"],
            now=cancelled.now(9),
        )
        self.assertEqual([cancelled.child], resolved["item_ids"])
        before = self.run_bytes(cancelled)
        with self.assertRaises(ProtocolRefusal) as retry:
            cancelled.scheduler.open_attempt(
                cancelled.run_id,
                cancelled.child,
                RetryPolicy(1, 0, 0, strategy="fixed"),
                1,
                now=cancelled.now(10),
            )
        self.assertEqual("cancel_request_fence", retry.exception.code)
        self.assertEqual(before, self.run_bytes(cancelled))

        open_group = _Task3Case(self)
        open_group.activate()
        before = self.run_bytes(open_group)
        with self.assertRaises(ProtocolRefusal) as parent_acceptance:
            open_group.scheduler.terminal_attempt(
                open_group.run_id,
                open_group.parent,
                str(open_group.opened["attempt_id"]),
                "completed",
                None,
                "completed",
                "idempotent",
                now=open_group.now(9),
            )
        self.assertEqual("spawn_group_open", parent_acceptance.exception.code)
        self.assertEqual(before, self.run_bytes(open_group))

        forged_case = _Task3Case(self)
        forged_case.activate(
            on_child_failure="continue_until_join_impossible",
        )
        forged_case.reject()
        forged = {
            "schema_version": 1,
            "id": "spawn-group-closed-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-10T13:00:02.000Z",
            "kind": "spawn_group_closed",
            "run_id": forged_case.run_id,
            "spawn_group_id": forged_case.created["id"],
            "plan_amendment_id": forged_case.amendment["id"],
            "parent_attempt_id": forged_case.opened["attempt_id"],
            "member_item_ids": [forged_case.child],
            "accepted_item_ids": [forged_case.child],
            "terminal_item_ids": [],
            "rejected_item_ids": [forged_case.child],
            "join_mode": "all_terminal",
            "required_count": None,
            "outcome": "satisfied",
            "close_reason": "all_members_terminal",
            "cancel_scope_resolved_id": None,
            "closed_at_testimony": "2026-08-10T13:00:02.000Z",
        }
        before = self.run_bytes(forged_case)
        with self.assertRaises(ProtocolRefusal) as join_sets:
            forged_case.ledger._append_spawn_group(
                forged,
                forged_case.ledger._spawn_group_capability_for(
                    forged_case.controller,
                ),
            )
        self.assertEqual("spawn_group_close_sets_invalid", join_sets.exception.code)
        self.assertEqual(before, self.run_bytes(forged_case))

        missing_close = _Task3Case(self)
        missing_close.activate()
        missing_close.admit()
        CancellationCoordinator(missing_close.ledger).request(
            missing_close.run_id, {}, item_id=missing_close.parent,
            now=missing_close.now(9),
        )
        path = missing_close.root.resolve_relative(RunLedger.relative_path)
        records = [json.loads(line) for line in path.read_bytes().splitlines()]
        records = [row for row in records if row["kind"] != "spawn_group_closed"]
        path.write_bytes(b"".join(encode_frame(row) for row in records))
        corrupt = path.read_bytes()
        open_projection = missing_close.ledger.project().run(missing_close.run_id)
        self.assertEqual(
            "activated",
            open_projection["spawn_groups"][missing_close.created["id"]]["state"],
        )
        with self.assertRaises(ProtocolRefusal) as absent_close:
            missing_close._append(
                "run_terminal",
                "run-terminal-",
                outcome=missing_close.ledger.project().run_outcome(missing_close.run_id),
            )
        self.assertEqual("spawn_group_open", absent_close.exception.code)
        self.assertEqual(corrupt, path.read_bytes())

    def test_spawn_dependency_sibling_cancellation_stays_exact_and_cannot_close_group(self) -> None:
        """Catches exact child cancellation expanding through a sibling dependency."""

        self.lawful_full_lifecycle()
        from floati.cancellation import CancellationCoordinator
        from floati.contracts import contract_digest
        from floati.scheduler import RetryPolicy

        case = _Task3Case(self)
        case.prepare_parent()
        sibling = "work-" + uuid7_hex()
        sibling_contract = case.contract([case.child])
        children = sorted(
            [
                case.child_descriptor(),
                case.child_descriptor(
                    item_id=sibling,
                    task_contract_id="task-contract-" + uuid7_hex(),
                    task_contract=sibling_contract.canonical(),
                    task_contract_digest=contract_digest(sibling_contract),
                    depth=1,
                    workspace_key="workspace-sibling",
                    concurrency_key="concurrency-sibling",
                ),
            ],
            key=lambda row: str(row["item_id"]),
        )
        edges = sorted(
            [
                {
                    "source": case.parent,
                    "target": case.child,
                    "requires": "accepted",
                    "failure_policy": "fail_run",
                },
                {
                    "source": case.child,
                    "target": sibling,
                    "requires": "accepted",
                    "failure_policy": "fail_run",
                },
            ],
            key=lambda row: (str(row["source"]), str(row["target"])),
        )
        case.created, case.amendment = case.controller.create_group(
            **case.create_kwargs(
                children=children,
                dependency_edges=edges,
                max_children=2,
                aggregate_budget=[{"budget_id": "build", "amount": 2}],
            )
        )
        case.admit()
        case.controller.admit_child(
            case.run_id,
            case.created["id"],
            sibling,
            now=case.now(8),
        )
        resolved = CancellationCoordinator(case.ledger).request_exact_items(
            case.run_id,
            [case.child],
            {},
            spawn_group_id=case.created["id"],
            now=case.now(9),
        )
        self.assertEqual([case.child], resolved["item_ids"])
        self.assertNotIn(sibling, resolved["item_ids"])

        before = self.run_bytes(case)
        with self.assertRaises(ProtocolRefusal) as sibling_scope:
            case.controller.close_group(
                case.run_id,
                case.created["id"],
                cancel_scope_resolved_id=resolved["id"],
                outcome="cancelled",
                now=case.now(10),
            )
        self.assertEqual("spawn_cancellation_incomplete", sibling_scope.exception.code)
        self.assertEqual(before, self.run_bytes(case))

        opened = case.scheduler.open_attempt(
            case.run_id,
            sibling,
            RetryPolicy(1, 0, 0, strategy="fixed"),
            1,
            now=case.now(11),
        )
        self.assertEqual(sibling, opened["item_id"])

    def test_spawn_zero_attempt_run_and_item_cancellation_fence_future_writes(self) -> None:
        """Catches run or parent-item cancellation omitting zero-attempt children."""

        self.lawful_full_lifecycle()
        from floati.cancellation import CancellationCoordinator
        from floati.scheduler import RetryPolicy

        for scope, parent_item in (("run", None), ("item", "parent")):
            with self.subTest(scope=scope):
                case = _Task3Case(self)
                case.activate()
                admitted = case.admit()
                CancellationCoordinator(case.ledger).request(
                    case.run_id,
                    {},
                    item_id=None if parent_item is None else case.parent,
                    now=case.now(9),
                )
                run = case.ledger.project().run(case.run_id)
                self.assertEqual([], run["item_attempt_ids"].get(case.child, []))
                self.assertEqual("cancelled", run["spawn_item_outcomes"][case.child])
                self.assertEqual(
                    admitted["id"],
                    next(
                        row["child_admitted_id"]
                        for row in case.ledger.records()
                        if row["kind"] == "spawn_child_cancelled_without_attempt"
                    ),
                )
                group = run["spawn_groups"][case.created["id"]]
                self.assertEqual(("closed", "cancelled"), (
                    group["state"], group["closed"]["outcome"],
                ))

                before = self.run_bytes(case)
                with self.assertRaises(ProtocolRefusal) as future_attempt:
                    case.scheduler.open_attempt(
                        case.run_id,
                        case.child,
                        RetryPolicy(1, 0, 0, strategy="fixed"),
                        1,
                        now=case.now(10),
                    )
                self.assertEqual("cancel_request_fence", future_attempt.exception.code)
                self.assertEqual(before, self.run_bytes(case))

    def test_spawn_parent_cancel_request_before_create_preserves_cancelled_prefix(self) -> None:
        """Catches group creation racing ahead of a durable parent cancellation request."""

        self.lawful_full_lifecycle()
        from floati.cancellation import CancellationCoordinator

        case = _Task3Case(self)
        case.prepare_parent()
        resolved = CancellationCoordinator(case.ledger).request(
            case.run_id,
            {},
            item_id=case.parent,
            now=case.now(7),
        )
        self.assertIn(case.parent, resolved["item_ids"])
        before = self.run_bytes(case)
        with self.assertRaises(ProtocolRefusal) as create:
            case.controller.create_group(**case.create_kwargs(now=case.now(8)))
        self.assertEqual("spawn_parent_cancel_requested", create.exception.code)
        self.assertEqual(before, self.run_bytes(case))
        self.assertNotIn(
            "spawn_group_created",
            {row["kind"] for row in case.ledger.records()},
        )

    def test_spawn_late_result_cannot_reopen_join_or_use_forged_operator_capability(self) -> None:
        """Catches late testimony reopening a group or bypassing operator authority."""

        self.lawful_full_lifecycle()
        from floati.approvals import CapabilityLedger
        from floati.jsonl import append_record
        from floati.records import capability_set_digest
        from floati.scheduler import RetryPolicy
        from floati.workers import WORKER_KINDS

        def late_result_case() -> tuple[_Task3Case, dict[str, object]]:
            case = _Task3Case(self)
            case.activate(
                deadline="2026-08-10T12:00:08.000Z",
                on_late_result="operator_decision",
            )
            case.admit(now_offset=7)
            opened = case.scheduler.open_attempt(
                case.run_id,
                case.child,
                RetryPolicy(1, 0, 0, strategy="fixed"),
                1,
                now=case.now(7),
            )
            grants = [{
                "capability_name": "review",
                "grant_id": "capability-grant-" + uuid7_hex(),
                "physical_position": 1,
            }]
            capability = {
                "schema_version": 1,
                "id": "capability-set-bound-" + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-10T12:00:07.000Z",
                "kind": "capability_set_bound",
                "run_id": case.run_id,
                "item_id": case.child,
                "attempt_id": opened["attempt_id"],
                "fence_token": opened["fence_token"],
                "chosen_worker": "node-a",
                "policy_digest": case.policy.digest,
                "routing_rank": 0,
                "evaluated_at_testimony": "2026-08-10T12:00:07.000Z",
                "grant_ledger_high_watermark": 1,
                "effective_grants": grants,
                "capability_digest": capability_set_digest(grants),
            }
            bound = case.ledger._append_capability_set(
                capability,
                case.ledger._capability_binding_capability_for(
                    case.capability_binder,
                ),
            )
            dispatch = case.capability_binder.dispatch(
                bound["id"],
                ["node-a"],
                "policy.route",
                case.policy,
                now=case.now(7),
            )
            case.scheduler.start_attempt(
                case.run_id,
                case.child,
                opened["attempt_id"],
                dispatch["id"],
                now=case.now(7),
            )
            closed = case.controller.close_group(
                case.run_id,
                case.created["id"],
                now=case.now(9),
            )
            self.assertEqual(("deadline", "deadline_expired"), (
                closed["outcome"], closed["close_reason"],
            ))
            receipt = {
                "schema_version": 0,
                "id": "worker-receipt-" + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-10T12:00:09.000Z",
                "kind": "worker_receipt",
                "session_id": "worker-" + uuid7_hex(),
                "work_item_id": case.child,
                "node_id": "node-a",
                "adapter": "codex",
                "transition": "claim",
                "outcome_code": None,
                "authority_subject": "execute-run",
                "authority_epoch": 1,
                "artifact_bindings": [],
            }
            append_record(
                case.root,
                Path("receipts/workers.jsonl"),
                receipt,
                allowed_kinds=WORKER_KINDS,
            )
            produced = case.ledger.append({
                "schema_version": 0,
                "id": "run-result-produced-" + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-10T12:00:09.000Z",
                "kind": "result_produced",
                "run_id": case.run_id,
                "item_id": case.child,
                "attempt_id": opened["attempt_id"],
                "dispatch_decision_id": dispatch["id"],
                "worker_receipt_ids": [receipt["id"]],
            })
            self.assertIn(
                produced["id"],
                case.ledger.project().run(case.run_id)["spawn_groups"][
                    case.created["id"]
                ]["late_result_ids"],
            )
            return case, produced

        lawful, produced = late_result_case()
        Registry(lawful.root).register("operator-a", "Operator")
        grant = AuthorityGrantStore(lawful.root).claim(
            "spawn-admin",
            "operator-a",
            120,
            120,
            lawful.now(10),
        )
        capability = CapabilityLedger(lawful.root).declare(
            "operator-a",
            "spawn.late_result.dispose",
            "read_write",
            "run",
            60,
            now=lawful.now(10),
        )
        disposition = lawful.controller.dispose_late_result(
            lawful.run_id,
            lawful.created["id"],
            lawful.child,
            produced["id"],
            "retain_as_non_join_evidence",
            operator_id="operator-a",
            authority_subject="spawn-admin",
            authority_epoch=grant["epoch"],
            capability_record_id=capability["id"],
            now=lawful.now(11),
        )
        self.assertEqual("spawn_late_result_disposition", disposition["kind"])
        self.assertEqual(
            "deadline",
            lawful.ledger.project().run(lawful.run_id)["spawn_groups"][
                lawful.created["id"]
            ]["closed"]["outcome"],
        )

        before = self.run_bytes(lawful)
        with self.assertRaises(ProtocolRefusal) as reopening:
            lawful.controller.close_group(
                lawful.run_id,
                lawful.created["id"],
                outcome="satisfied",
                now=lawful.now(12),
            )
        self.assertEqual("spawn_group_close_divergent", reopening.exception.code)
        self.assertEqual(before, self.run_bytes(lawful))

        duplicate = dict(
            disposition,
            id="spawn-late-result-disposition-" + uuid7_hex(),
        )
        with self.assertRaises(ProtocolRefusal) as duplicate_disposition:
            lawful.ledger._append_spawn_group(
                duplicate,
                lawful.ledger._spawn_group_capability_for(lawful.controller),
            )
        self.assertEqual(
            "late_result_disposition_duplicate",
            duplicate_disposition.exception.code,
        )
        self.assertEqual(before, self.run_bytes(lawful))

        forged, forged_result = late_result_case()
        Registry(forged.root).register("operator-b", "Operator")
        forged_grant = AuthorityGrantStore(forged.root).claim(
            "spawn-admin",
            "operator-b",
            120,
            120,
            forged.now(10),
        )
        CapabilityLedger(forged.root).declare(
            "operator-b",
            "spawn.late_result.dispose",
            "read_write",
            "run",
            60,
            now=forged.now(10),
        )
        before = self.run_bytes(forged)
        with self.assertRaises(ProtocolRefusal) as forged_capability:
            forged.controller.dispose_late_result(
                forged.run_id,
                forged.created["id"],
                forged.child,
                forged_result["id"],
                "retain_as_non_join_evidence",
                operator_id="operator-b",
                authority_subject="spawn-admin",
                authority_epoch=forged_grant["epoch"],
                capability_record_id="capability-" + uuid7_hex(),
                now=forged.now(11),
            )
        self.assertEqual(
            "operator_capability_invalid",
            forged_capability.exception.code,
        )
        self.assertEqual(before, self.run_bytes(forged))

    def test_spawn_parent_acceptance_requires_actual_observation_close_writer_record(self) -> None:
        """Catches parent acceptance when the launch observation was never closed."""

        self.lawful_full_lifecycle()
        from floati.jsonl import append_record
        from floati.workers import WORKER_KINDS

        def parent_result_case(
            *, close_observation: bool,
        ) -> tuple[_Task3Case, dict[str, object]]:
            case = _Task3Case(self)
            case.activate(
                on_child_failure="continue_until_join_impossible",
            )
            case.reject()
            closed = case.controller.close_group(
                case.run_id,
                case.created["id"],
                now=case.now(3602),
            )
            self.assertEqual("satisfied", closed["outcome"])
            if close_observation:
                _runner, worker_result = case.run_worker()
                self.assertEqual("complete", worker_result["transition"])
                observation = case.ledger.project().run(case.run_id)[
                    "descendant_observation_close"
                ][case.opened["attempt_id"]]
                self.assertEqual([], observation["observed_descendant_ids"])
            receipt = {
                "schema_version": 0,
                "id": "worker-receipt-" + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-10T13:00:04.000Z",
                "kind": "worker_receipt",
                "session_id": "worker-" + uuid7_hex(),
                "work_item_id": case.parent,
                "node_id": "node-a",
                "adapter": "codex",
                "transition": "claim",
                "outcome_code": None,
                "authority_subject": "execute-run",
                "authority_epoch": 1,
                "artifact_bindings": [],
            }
            append_record(
                case.root,
                Path("receipts/workers.jsonl"),
                receipt,
                allowed_kinds=WORKER_KINDS,
            )
            produced = case.ledger.append({
                "schema_version": 0,
                "id": "run-result-produced-" + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-10T13:00:04.000Z",
                "kind": "result_produced",
                "run_id": case.run_id,
                "item_id": case.parent,
                "attempt_id": case.opened["attempt_id"],
                "dispatch_decision_id": case.dispatch["id"],
                "worker_receipt_ids": [receipt["id"]],
            })
            accepted = {
                "schema_version": 0,
                "id": "run-result-accepted-" + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-10T13:00:04.000Z",
                "kind": "result_accepted",
                "run_id": case.run_id,
                "item_id": case.parent,
                "attempt_id": case.opened["attempt_id"],
                "predecessor_result_id": produced["id"],
                "acceptance_mode": "accepted_unverified",
                "acceptance_receipt_id": None,
                "worker_receipt_ids": [receipt["id"]],
            }
            return case, accepted

        lawful, lawful_acceptance = parent_result_case(close_observation=True)
        accepted = lawful.ledger.append(lawful_acceptance)
        self.assertEqual("result_accepted", accepted["kind"])

        hostile, hostile_acceptance = parent_result_case(close_observation=False)
        before = self.run_bytes(hostile)
        with self.assertRaises(ProtocolRefusal) as missing_close:
            hostile.ledger.append(hostile_acceptance)
        self.assertEqual(
            "descendant_observation_close_missing",
            missing_close.exception.code,
        )
        self.assertEqual(before, self.run_bytes(hostile))

    def test_spawn_descendant_and_socket_hostiles_require_live_process_local_authority(self) -> None:
        """Catches bearer, post-close, disabled-mode, or raw-frame spawn authority."""

        self.lawful_full_lifecycle()
        direct = _Task3Case(self)
        direct.activate()
        before = direct.ledger.records()
        with self.assertRaises(ProtocolRefusal) as forged:
            direct.controller.record_untracked_descendant(
                direct.run_id,
                str(direct.opened["attempt_id"]),
                "forged-capability",
                "observed",
                _launch_capability=object(),
                now=direct.now(8),
            )
        self.assertEqual("descendant_launch_capability_required", forged.exception.code)
        self.assertEqual(before, direct.ledger.records())

        runner, worker_result = direct.run_worker()
        self.assertEqual("complete", worker_result["transition"])
        before = direct.ledger.records()
        with self.assertRaises(ProtocolRefusal) as post_close:
            runner._handle_spawn_event(direct.run_id, direct.opened["attempt_id"], {
                "provider_descendant_id": "late-pipe-child",
                "state": "observed",
                "adopted_item_id": None,
            })
        self.assertEqual("descendant_observation_closed", post_close.exception.code)
        self.assertEqual(before, direct.ledger.records())

        disabled = _Task3Case(self)
        disabled.prepare_parent(mode="disabled")
        before = disabled.ledger.records()
        with self.assertRaises(ProtocolRefusal) as launch_mode:
            disabled.controller.record_untracked_descendant(
                disabled.run_id,
                str(disabled.opened["attempt_id"]),
                "disabled-child",
                "observed",
                now=disabled.now(8),
            )
        self.assertEqual("descendant_observation_invalid", launch_mode.exception.code)
        self.assertEqual(before, disabled.ledger.records())

        managed_case = _Task3Case(self)
        managed_case.activate()
        managed = _ManagedSpawnCase(self, managed_case)
        before = self.run_bytes(managed_case)
        with self.assertRaises(ProtocolRefusal) as private:
            managed.client.append(managed_case.created)
        self.assertEqual("spawn_group_controller_only", private.exception.code)
        self.assertEqual(before, self.run_bytes(managed_case))

        policy = {
            "canonical_b64": __import__("base64").b64encode(
                managed_case.policy.canonical_bytes
            ).decode("ascii"),
            "digest": managed_case.policy.digest,
        }
        intent = {
            "run_id": managed_case.run_id,
            "parent_attempt_id": managed_case.opened["attempt_id"],
            "provider_descendant_id": "forged-over-socket",
            "state": "observed",
            "adopted_item_id": None,
            "policy": policy,
        }
        request = managed.client._evaluation_request(
            "untracked_descendant_evaluation",
            "untracked-descendant-evaluation-" + uuid7_hex(),
            intent,
        )
        response = managed.raw(encode_frame(request))
        self.assertEqual(
            ("refused", "operation_invalid"),
            (response["status"], response["code"]),
        )
        self.assertEqual(before, self.run_bytes(managed_case))


class SequencerHostileGauntletTests(unittest.TestCase):
    NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    KINDS = frozenset({"run_created"})

    @staticmethod
    def run_created(tenant: str, index: int) -> dict[str, object]:
        suffix = list(f"{index:032x}")
        suffix[12] = "7"
        suffix[16] = "8"
        identity = "".join(suffix)
        item_suffix = list(f"{index + 1_000_000:032x}")
        item_suffix[12] = "7"
        item_suffix[16] = "8"
        item_identity = "".join(item_suffix)
        return {
            "schema_version": 0,
            "id": "run-created-" + identity,
            "tenant_id": tenant,
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "run_created",
            "run_id": "run-" + identity,
            "plan_digest": "a" * 64,
            "item_ids": ["work-" + item_identity],
            "dependency_edges": [],
        }

    @staticmethod
    def bytes_under(path: Path) -> dict[str, bytes]:
        return {
            item.relative_to(path).as_posix(): item.read_bytes()
            for item in sorted(path.rglob("*"))
            if item.is_file() and not item.is_symlink()
        }

    @staticmethod
    def append(store: SegmentedRunStore, record: dict[str, object]) -> None:
        store.transact(lambda _snapshot: (record, record))

    def test_segment_reader_refuses_hostile_frames_and_writer_preserves_bytes(self) -> None:
        """Catches a reader skipping corrupt segment truth or a failed writer repairing it."""
        cases = (
            "malformed",
            "truncated",
            "oversized",
            "non_utf8",
            "duplicate_key",
            "reordered_metadata",
            "missing_segment",
            "altered_seal",
            "altered_sealed_bytes",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
                store = SegmentedRunStore(root, self.KINDS, SegmentConfig(max_records=1))
                store.activate(now=self.NOW)
                one, two = self.run_created("alpha", 1), self.run_created("alpha", 2)
                self.append(store, one)
                self.append(store, two)
                self.assertEqual([one, two], store.records())  # positive reader/writer control
                segments = root.resolve_relative("runs/segments")
                active = segments / "00000001.jsonl"
                metadata = segments / "events.jsonl"
                if case == "malformed":
                    active.write_bytes(b"{bad}\n")
                elif case == "truncated":
                    active.write_bytes(active.read_bytes()[:-1])
                elif case == "oversized":
                    active.write_bytes(b"{" + b" " * 65536 + b"}\n")
                elif case == "non_utf8":
                    active.write_bytes(b'{"value":"\xff"}\n')
                elif case == "duplicate_key":
                    text = active.read_text(encoding="utf-8").rstrip("\n")
                    active.write_text(text[:-1] + ',"id":"' + str(two["id"]) + '"}\n', encoding="utf-8")
                elif case == "reordered_metadata":
                    lines = metadata.read_bytes().splitlines(keepends=True)
                    lines[0], lines[1] = lines[1], lines[0]
                    metadata.write_bytes(b"".join(lines))
                elif case == "missing_segment":
                    (segments / "00000000.jsonl").unlink()
                elif case == "altered_seal":
                    rows = [json.loads(line) for line in metadata.read_text(encoding="utf-8").splitlines()]
                    rows[1]["segment_sha256"] = "f" * 64
                    metadata.write_bytes(b"".join(encode_frame(row) for row in rows))
                elif case == "altered_sealed_bytes":
                    sealed = segments / "00000000.jsonl"
                    sealed.write_bytes(sealed.read_bytes().replace(b'"plan_digest":"' + b"a" * 64, b'"plan_digest":"' + b"b" * 64))
                before = self.bytes_under(root.tenant_home)
                with self.assertRaises(IntegrityFailure):
                    store.records()
                with self.assertRaises(IntegrityFailure):
                    self.append(store, self.run_created("alpha", 3))
                self.assertEqual(before, self.bytes_under(root.tenant_home))

    def test_socket_refusals_preserve_writer_bytes_after_positive_control(self) -> None:
        """Catches stale/unknown socket requests mutating run or epoch evidence."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            service = SequencerService(
                root, "gauntlet", config=SequencerConfig(select_timeout=0.01)
            )
            stop = threading.Event()
            thread = threading.Thread(target=service.serve_forever, args=(stop,), daemon=True)
            thread.start()
            try:
                client = SequencerClient(service.socket_path, service.epoch, "positive")
                positive = self.run_created("alpha", 10)
                self.assertEqual("ok", client.append(positive)["status"])
                baseline = self.bytes_under(root.tenant_home)
                stale = SequencerClient(service.socket_path, service.epoch + 1, "stale")
                with self.assertRaises(ProtocolRefusal) as stale_refusal:
                    stale.append(self.run_created("alpha", 11))
                self.assertEqual("sequencer_epoch_mismatch", stale_refusal.exception.code)
                request = client.request(self.run_created("alpha", 12))
                request["operation"] = "unknown_socket_operation"
                payload = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                    channel.settimeout(3)
                    channel.connect(str(service.socket_path))
                    channel.sendall(payload)
                    response = json.loads(channel.recv(65536))
                self.assertEqual("refused", response["status"])
                self.assertEqual("operation_invalid", response["code"])
                self.assertEqual(baseline, self.bytes_under(root.tenant_home))
                self.assertEqual([positive], RunLedger(root).records())
            finally:
                stop.set()
                thread.join(3)
                service.close()

    def test_semantically_hostile_admission_refuses_replay_and_writer_preserves_bytes(self) -> None:
        """Catches schema-valid admission evidence bypassing semantic replay validation."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            created = self.run_created("alpha", 20)
            item_id = str(created["item_ids"][0])
            workers = [{"node_id": "node-a", "worker_profile": "codex"}]
            items = [{
                "item_id": item_id,
                "workspace_key": "workspace-a",
                "concurrency_key": "concurrency-a",
                "capability_selector": "review_write",
            }]
            hostile = {
                "schema_version": 1,
                "id": "run-admission-bound-" + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-09T12:00:00.000Z",
                "kind": "run_admission_bound",
                "run_id": created["run_id"],
                "plan_digest": "b" * 64,
                "policy_digest": "c" * 64,
                "max_active_attempts": 1,
                "workers": workers,
                "budget_reservations": [],
                "items": items,
                "admission_digest": run_admission_digest(workers, 1, [], items),
            }
            path = root.resolve_relative("runs/events.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encode_frame(created) + encode_frame(hostile))
            ledger = RunLedger(root)
            self.assertEqual([created, hostile], ledger.records())  # positive framed-reader control
            before = path.read_bytes()
            with self.assertRaises(IntegrityFailure) as replay:
                ledger.project()
            self.assertEqual("run_admission_plan_mismatch", replay.exception.code)
            with self.assertRaises(ProtocolRefusal) as writer:
                ledger.append(self.run_created("alpha", 21))
            self.assertEqual("run_admission_plan_mismatch", writer.exception.code)
            self.assertEqual(before, path.read_bytes())

    def test_dead_holder_send_projects_over_a_complete_foc_run_prefix(self) -> None:
        """Catches an orphan-only reader rejecting the lawful run frames before FOC evidence."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            self.assertIn("run_created", trace.records_by_kind)
            self.assertIn("supervisor_orphaned", trace.records_by_kind)
            self.assertIsNotNone(trace.claim_id)
            self.assertIsNotNone(trace.lease_id)
            self.assertIsNotNone(trace.worker_session_id)

            registry = Registry(root)
            for node in ("sender", "recipient"):
                registry.register(node, "worker")
            message = EventLog(root, registry).send(
                "sender", "recipient", "slipway", "d" * 40,
                "docs/evidence/td4-foc.md", "full FOC prefix",
                idempotency_key="td4-foc",
                worker_session_id=trace.worker_session_id,
                attempt_binding={
                    "attempt_id": trace.attempt_ids[0],
                    "claim_id": trace.claim_id,
                    "lease_id": trace.lease_id,
                    "worker_session_id": trace.worker_session_id,
                },
            )
            try:
                projections = EventLog(root, registry).stale_send_projection()
            except IntegrityFailure as exc:
                self.fail(
                    "stale-send projection rejected its lawful full FOC prefix: "
                    + exc.code
                )
            projection = next(
                item for item in projections if item["message_id"] == message["id"]
            )
            self.assertEqual("stale_send", projection["state"])
            self.assertEqual(trace.lease_id, projection["holder_lease_id"])
            self.assertEqual("abandoned", projection["lease_state_at_projection"])
            self.assertIs(False, projection["renews"])


class WakeHoldFuzzTests(unittest.TestCase):
    """Hostile Wake/Hold records and callback races against real local files."""

    def _root_with_messages(self) -> tuple[tempfile.TemporaryDirectory[str], FloatiRoot, list[dict[str, object]]]:
        temporary = tempfile.TemporaryDirectory()
        root = FloatiRoot.open(Path(temporary.name), "alpha")
        registry = Registry(root)
        registry.register("alice", "worker")
        registry.register("bob", "worker")
        events = EventLog(root, registry)
        messages = [
            events.send(
                "alice", "bob", "slipway", "a" * 40,
                "docs/evidence/wake-fuzz.md", f"wake fuzz {index}",
                idempotency_key=f"wake-fuzz-message-{index}",
            )
            for index in range(2)
        ]
        return temporary, root, messages

    def test_empty_segment_bootstrap_is_not_misclassified_as_hold_testimony(self) -> None:
        """Catches sealed hold parsing rejecting the lawful empty segment durability marker."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = FloatiRoot.open_direct_home(Path(temporary.name) / "alpha", create=True)
        store = SegmentedRunStore(
            root, frozenset({"run_created"}), SegmentConfig(max_records=1),
        )

        opened = store.activate(now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc))

        self.assertEqual("segment_opened", opened["kind"])
        self.assertEqual(
            b"", root.resolve_relative("runs/segments/00000000.jsonl").read_bytes(),
        )

    def test_hold_record_runtime_and_schema_refuse_i_json_and_terminal_hostility(self) -> None:
        """Catches runtime/schema drift admitting an invalid hold receipt before replay."""
        from floati.records import validate_record
        from floati.wake_hold import wake_hold_receipt
        from tests.schema_validation import SchemaValidationError, validate_json_schema

        first = "msg-018f7e9b3c117abc8def0123456789ab"
        lawful = wake_hold_receipt(
            tenant_id="alpha", recipient="bob", worker_session_id=None,
            idempotency_key="fuzz-key", limit=1, item_ids=[first],
            event_prefix_digest="a" * 64, delivery_prefix_digest="b" * 64,
            acknowledgment_prefix_digest="c" * 64,
            now="2026-08-13T12:00:00.000Z",
            record_id="wake-hold-018f7e9b3c147abc8def0123456789ab",
        )
        schema = REPO_ROOT / "schemas/v1/wake-hold-receipt-record.schema.json"
        validate_record(copy.deepcopy(lawful), "alpha", frozenset({"wake_hold_receipt"}), integrity=False)
        validate_json_schema(lawful, schema)
        for name, value in (
            ("nul", "bad\x00key"),
            ("c1", "bad\x85key"),
            ("bidi", "bad\u202ekey"),
            ("overlong", "k" * 129),
            ("nonfinite", float("nan")),
            ("fraction", 1.5),
        ):
            with self.subTest(hostility=name):
                hostile = copy.deepcopy(lawful)
                if name in {"nonfinite", "fraction"}:
                    hostile["limit"] = value
                else:
                    hostile["idempotency_key"] = value
                with self.assertRaises(ProtocolRefusal):
                    validate_record(hostile, "alpha", frozenset({"wake_hold_receipt"}), integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(hostile, schema)

    def test_replay_refuses_duplicate_json_order_digest_and_coordinate_substitutions(self) -> None:
        """Catches malformed durable testimony degrading to a held-only or idle decision."""
        from floati.framing import encode_frame
        from floati.records import wake_hold_decision_digest
        from floati.wake_hold import WakeHoldController

        for mutation in ("duplicate_json_key", "order", "digest", "recipient", "session"):
            with self.subTest(mutation=mutation):
                temporary, root, messages = self._root_with_messages()
                self.addCleanup(temporary.cleanup)
                artifact = WakeHoldController(root).evaluate("bob", idempotency_key="fuzz-receipt")
                receipt = copy.deepcopy(artifact["receipt"])
                self.assertIsNotNone(receipt)
                path = root.resolve_relative("receipts/deliveries/bob.jsonl")
                if mutation == "duplicate_json_key":
                    encoded = encode_frame(receipt).replace(
                        b'"kind":"wake_hold_receipt",',
                        b'"kind":"wake_hold_receipt","kind":"wake_hold_receipt",',
                    )
                else:
                    if mutation == "order":
                        receipt["item_ids"] = list(reversed(messages))
                        receipt["item_ids"] = [row["id"] for row in receipt["item_ids"]]
                    elif mutation == "digest":
                        receipt["event_prefix_digest"] = "f" * 64
                    elif mutation == "recipient":
                        receipt["recipient"] = "charlie"
                    else:
                        receipt["worker_session_id"] = "worker-018f7e9b3c137abc8def0123456789ab"
                    receipt["decision_digest"] = wake_hold_decision_digest(receipt)
                    encoded = encode_frame(receipt)
                path.write_bytes(encoded)
                with self.assertRaises(IntegrityFailure) as caught:
                    WakeHoldController(root).evaluate("bob", idempotency_key="later-key")
                self.assertEqual("consumption_state_unavailable", caught.exception.code)

    def test_callback_preview_to_register_mutations_refuse_without_local_plist(self) -> None:
        """Catches a callback byte, inode, or pathname race writing stale local testimony."""
        from floati.wake import OneShotWakeRegistrar, OneShotWakeRequest

        for mutation in ("same_inode_bytes", "replacement", "disappearance"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
                root = FloatiRoot.open(Path(directory) / "root", "alpha")
                callback = Path(directory) / "callback"
                callback.write_bytes(b"#!/bin/sh\nexit 0\n")
                callback.chmod(0o700)
                request = OneShotWakeRequest(
                    root=root, run_id="run-018f7e9b3c117abc8def0123456789ab",
                    item_id="work-018f7e9b3c117abc8def0123456789ab",
                    attempt_id="attempt-018f7e9b3c117abc8def0123456789ab",
                    wake_at="2099-01-02T03:05:06.000Z", scheduler_epoch=7,
                    fence_token="a" * 64,
                )
                registrar = OneShotWakeRegistrar(
                    callback_path=callback,
                    now=lambda: datetime(2099, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
                )
                preview = registrar.preview(request)
                if mutation == "same_inode_bytes":
                    callback.write_bytes(b"#!/bin/sh\nexit 1\n")
                elif mutation == "replacement":
                    replacement = Path(directory) / "replacement"
                    replacement.write_bytes(b"#!/bin/sh\nexit 2\n")
                    replacement.chmod(0o700)
                    callback.unlink()
                    replacement.rename(callback)
                else:
                    callback.unlink()
                with self.assertRaises(ProtocolRefusal):
                    registrar.register(request, preview["digest"])
                self.assertFalse((root.tenant_home / "wake").exists())

    def test_wake_hold_source_inventory_has_no_resident_wake_mechanism(self) -> None:
        """Catches Wake/Hold growing a listener, poller, or second durable wake truth file."""
        sources = {
            path.name: path.read_text(encoding="utf-8")
            for path in (REPO_ROOT / "floati/wake_hold.py", REPO_ROOT / "floati/wake.py")
        }
        self.assertIn("WakeHoldController", sources["wake_hold.py"])
        self.assertIn("written_unloaded", sources["wake.py"])
        combined = "\n".join(sources.values()).lower()
        for forbidden in (
            "import socket", "socket.", "serve_forever", "threading.thread",
            "asyncio", "time.sleep", "while true", "wake-state.json",
            "wake-truth", "wake_truth",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)


class ThreadObservationFuzzTests(unittest.TestCase):
    def test_record_runtime_schema_hostile_matrix_has_lawful_controls(self) -> None:
        from floati.records import (
            THREAD_OBSERVATION_KINDS,
            thread_observation_digest,
            validate_record,
        )
        from tests.schema_validation import (
            SchemaValidationError,
            validate_json_schema,
        )
        from tests.test_thread_observations import thread_record_rows

        registered, observed, detached = thread_record_rows()
        schemas = {
            "thread_attachment_registered": REPO_ROOT
            / "schemas/v1/thread-attachment-registered-record.schema.json",
            "thread_observation_recorded": REPO_ROOT
            / "schemas/v1/thread-observation-recorded-record.schema.json",
            "thread_attachment_detached": REPO_ROOT
            / "schemas/v1/thread-attachment-detached-record.schema.json",
        }
        for lawful in (registered, observed, detached):
            validate_record(
                copy.deepcopy(lawful),
                "alpha",
                THREAD_OBSERVATION_KINDS,
                integrity=False,
            )
            validate_json_schema(lawful, schemas[str(lawful["kind"])])

        def string_paths(value, prefix=()):
            if isinstance(value, str):
                yield prefix
            elif isinstance(value, dict):
                for key, member in value.items():
                    yield from string_paths(member, prefix + (key,))
            elif isinstance(value, list):
                for index, member in enumerate(value):
                    yield from string_paths(member, prefix + (index,))

        def replace_path(row, path, value):
            current = row
            for part in path[:-1]:
                current = current[part]
            current[path[-1]] = value

        def refresh_digest(row, path):
            if (
                row["kind"] == "thread_observation_recorded"
                and path != ("observation_digest",)
            ):
                row["observation_digest"] = thread_observation_digest(row)

        hostile_rows: list[tuple[str, dict[str, object]]] = []
        hostile_strings = ("x\x00y", "x\x85y", "x\u202ey", "n" * 4097)
        for lawful in (registered, observed, detached):
            for path in string_paths(lawful):
                for value in hostile_strings:
                    row = copy.deepcopy(lawful)
                    replace_path(row, path, value)
                    refresh_digest(row, path)
                    hostile_rows.append((str(lawful["kind"]), row))

        malformed_ids = (
            "not-a-uuid",
            "018F3A2B-4C5D-7E8F-9A0B-1C2D3E4F5678",
            "018f3a2b-4c5d-6e8f-9a0b-1c2d3e4f5678",
        )
        for lawful in (registered, observed, detached):
            for path in string_paths(lawful):
                field = path[-1]
                if not isinstance(field, str) or (
                    field == "tenant_id"
                    or (field != "id" and not field.endswith("_id"))
                ):
                    continue
                for value in malformed_ids:
                    row = copy.deepcopy(lawful)
                    replace_path(row, path, value)
                    refresh_digest(row, path)
                    hostile_rows.append((str(lawful["kind"]), row))
        for value in (-1, 253402300800, 10**100):
            row = copy.deepcopy(observed)
            row["provider_updated_at"]["value"] = value
            refresh_digest(row, ("provider_updated_at", "value"))
            hostile_rows.append((str(observed["kind"]), row))
        for flags in (
            ["waiting_on_user_input", "waiting_on_approval"],
            ["waiting_on_approval", "waiting_on_approval"],
        ):
            row = copy.deepcopy(observed)
            row["active_flags"]["value"] = flags
            refresh_digest(row, ("active_flags", "value"))
            hostile_rows.append((str(observed["kind"]), row))
        for field in ("provider_status", "active_flags", "provider_updated_at"):
            row = copy.deepcopy(observed)
            row[field]["evidence_class"] = "derived"
            refresh_digest(row, (field, "evidence_class"))
            hostile_rows.append((str(observed["kind"]), row))
        extra = copy.deepcopy(detached)
        extra["provider_title"] = "HOSTILE"
        hostile_rows.append((str(detached["kind"]), extra))

        for index, (schema_kind, hostile) in enumerate(hostile_rows):
            with self.subTest(
                index=index, schema_kind=schema_kind, kind=hostile.get("kind"),
            ):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        copy.deepcopy(hostile),
                        "alpha",
                        THREAD_OBSERVATION_KINDS,
                        integrity=False,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(hostile, schemas[schema_kind])

    def test_production_source_inventory_is_pull_only_and_content_blind(self) -> None:
        source = (REPO_ROOT / "floati/thread_source.py").read_text(encoding="utf-8")
        controller = (REPO_ROOT / "floati/thread_observations.py").read_text(
            encoding="utf-8"
        )
        combined = source + controller
        self.assertEqual(1, combined.count('"thread/read"'))
        for forbidden in (
            '"thread/list"',
            '"turn/start"',
            '"turn/steer"',
            '"turn/interrupt"',
            '"thread/archive"',
            '"thread/resume"',
            '"thread/fork"',
            '"thread/name"',
            '"thread/pin"',
            '"title"',
            '"preview"',
            '"prompt"',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)
        for classifier in ("is_running", "is_complete", "is_failed", "is_stale"):
            self.assertNotIn(classifier, combined)


class EffectLedgerPrefixFuzzTests(unittest.TestCase):
    def test_lawful_prefixes_replay_and_illegal_suffixes_fail_closed(self) -> None:
        """Catches bounded prefix replay accepting duplicate, cross-bound, or stale suffixes."""
        fixture = EffectRecordFixture()
        rows = fixture.rows()
        confirmed = copy.deepcopy(rows["effect_confirmed"])
        confirmed["effect_acknowledged_id"] = None
        lawful = [rows["effect_intent"], rows["effect_dispatched"], confirmed]
        for length in range(len(lawful) + 1):
            with self.subTest(lawful_prefix=length):
                EffectProjection.from_records(copy.deepcopy(lawful[:length]))

        hostile_suffixes = []
        duplicate = copy.deepcopy(rows["effect_failed"])
        hostile_suffixes.append(duplicate)
        changed = copy.deepcopy(rows["effect_dispatched"])
        changed["request_digest"] = "9" * 64
        hostile_suffixes.append(changed)
        forward = copy.deepcopy(rows["effect_reconciled"])
        hostile_suffixes.append(forward)
        cases = (
            (lawful, hostile_suffixes[0]),
            ([rows["effect_intent"]], hostile_suffixes[1]),
            ([rows["effect_intent"], rows["effect_dispatched"]], hostile_suffixes[2]),
        )
        for prefix, suffix in cases:
            with self.subTest(suffix=suffix["kind"]):
                with self.assertRaises(IntegrityFailure):
                    EffectProjection.from_records(copy.deepcopy(prefix + [suffix]))

    def test_compensation_prefix_mutations_never_substitute_terminal_authority(self) -> None:
        """Catches compensation operation, digest, or terminal-reference substitution."""
        source = EffectRecordFixture()
        source_rows = source.rows()
        source_confirmed = copy.deepcopy(source_rows["effect_confirmed"])
        source_confirmed["effect_acknowledged_id"] = None
        compensation = EffectRecordFixture()
        compensation._binding.update({
            "run_id": source.run_id,
            "item_id": source.item_id,
            "attempt_id": source.attempt_id,
            "attempt_started_id": source.attempt_started_id,
            "fence_token": source.binding()["fence_token"],
            "request_digest": source_rows["compensation_proposed"]["compensation_request_digest"],
            "idempotency_key": "effect-compensation-gauntlet",
        })
        compensation_rows = compensation.rows()
        compensation_confirmed = copy.deepcopy(compensation_rows["effect_confirmed"])
        compensation_confirmed["effect_acknowledged_id"] = None
        proposal = copy.deepcopy(source_rows["compensation_proposed"])
        proposal["source_effect_evidence_id"] = source_confirmed["id"]
        proposal["compensation_operation_id"] = compensation.binding()["operation_id"]
        executed = copy.deepcopy(source_rows["compensation_executed"])
        executed["compensation_operation_id"] = compensation.binding()["operation_id"]
        executed["compensation_terminal_evidence_id"] = compensation_confirmed["id"]
        lawful = [
            source_rows["effect_intent"], source_rows["effect_dispatched"],
            source_confirmed, proposal, compensation_rows["effect_intent"],
            compensation_rows["effect_dispatched"], compensation_confirmed, executed,
        ]
        projection = EffectProjection.from_records(copy.deepcopy(lawful))
        self.assertEqual(
            "executed",
            projection.operation(source.binding()["operation_id"])["compensation_state"],
        )
        mutations = {
            "proposal_operation": lambda rows: rows[3].__setitem__(
                "compensation_operation_id", "effect-op-" + uuid7_hex()
            ),
            "intent_digest": lambda rows: rows[4].__setitem__("request_digest", "9" * 64),
            "executed_operation": lambda rows: rows[7].__setitem__(
                "compensation_operation_id", source.binding()["operation_id"]
            ),
            "terminal_evidence": lambda rows: rows[7].__setitem__(
                "compensation_terminal_evidence_id", source_confirmed["id"]
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                hostile = copy.deepcopy(lawful)
                mutate(hostile)
                with self.assertRaises(IntegrityFailure):
                    EffectProjection.from_records(hostile)

    def test_forged_effect_append_and_mutated_pipe_reentry_fail_closed(self) -> None:
        """Catches generic authority or repeated changed pipe testimony adding Effect truth."""
        from unittest import mock

        from floati.effects import EffectLedger
        from floati.jsonl import append_record
        from tests.test_effect_controller import _EffectCase
        from tests.test_workers import _EffectReportingAdapter, _EffectWorkerCase

        direct = _EffectCase(self)
        lawful = direct.controller.intent(**direct.intent_args())
        effect_path = direct.root.resolve_relative(EffectLedger.relative_path)
        before = effect_path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as forged:
            append_record(
                direct.root,
                EffectLedger.relative_path,
                lawful,
                allowed_kinds={"effect_intent"},
            )
        self.assertEqual("effect_controller_only", forged.exception.code)
        self.assertEqual(before, effect_path.read_bytes())

        first = _EffectWorkerCase.intent_event()
        duplicate = dict(first)
        changed = dict(first, request_digest="f" * 64)
        pipe = _EffectWorkerCase(
            self,
            _EffectReportingAdapter((first, duplicate, changed)),
        )
        with mock.patch(
            "floati.workers.apply_worker_isolation", return_value="macos-sandbox",
        ):
            result = pipe.execute()
        self.assertEqual(("degrade", "protocol_error"), (
            result["transition"], result["outcome_code"],
        ))
        self.assertEqual(
            ["effect_intent"],
            [row["kind"] for row in pipe.effect_ledger.records()],
        )

    def test_git_reconciliation_path_ref_and_output_attacks_fail_closed(self) -> None:
        """Catches hostile coordinates, refs, or Git stdout becoming confirmation."""
        from unittest import mock

        from floati import effect_reconciliation_observer as observer
        from floati.effect_reconciliation_protocol import build_request

        expected_digest = "f" * 64

        def request(coordinate: str, locator: str = "refs/heads/main") -> object:
            return build_request(
                operation_id="effect-op-018f7e9b3c117abc8def0123456789ab",
                current_evidence_id=(
                    "effect-unknown-018f7e9b3c117abc8def0123456789ab"
                ),
                adapter="git_remote_explicit",
                target={
                    "kind": "git_remote_ref",
                    "coordinate": coordinate,
                    "identity_digest": hashlib.sha256(
                        coordinate.encode("utf-8")
                    ).hexdigest(),
                },
                expected_confirmation={
                    "kind": "git_remote_ref_equals",
                    "locator": locator,
                    "expected_digest": expected_digest,
                },
                budget_claim={"git": 1},
                local_repository_identity=None,
                request_id="1" * 32,
            )

        hostile_coordinates = (
            "https://example.com/owner/../repo",
            "https://EXAMPLE.com/owner/repo",
            "ssh://git@example.com:22/owner/repo",
        )
        for coordinate in hostile_coordinates:
            with self.subTest(coordinate=coordinate), mock.patch.object(
                observer, "_run_git",
                side_effect=AssertionError("hostile coordinate reached Git"),
            ):
                result = observer._remote_result(request(coordinate), None)
                self.assertEqual(("unknown", "remote_coordinate_unsupported"), (
                    result.outcome, result.reason_code,
                ))

        with mock.patch.object(
            observer, "_run_git",
            side_effect=AssertionError("hostile ref reached Git"),
        ):
            result = observer._remote_result(
                request("https://example.com/owner/repo", "refs/heads/../main"),
                None,
            )
        self.assertEqual(("unknown", "contract_invalid"), (
            result.outcome, result.reason_code,
        ))

        lawful_request = request("https://example.com/owner/repo")
        lawful_stdout = (
            expected_digest + "\trefs/heads/main\n"
        ).encode("ascii")
        with mock.patch.object(
            observer, "_run_git", return_value=("ok", lawful_stdout, b""),
        ):
            lawful = observer._remote_result(lawful_request, None)
        self.assertEqual(("unknown", "reconciliation_inconclusive"), (
            lawful.outcome, lawful.reason_code,
        ))
        self.assertEqual(expected_digest, lawful.observation["observed_ref_digest"])
        self.assertIsNone(lawful.confirmation)
        self.assertIsNone(lawful.measured_spend)

        hostile_outputs = (
            lawful_stdout + lawful_stdout,
            (expected_digest + "\trefs/heads/other\n").encode("ascii"),
            b"\xff\trefs/heads/main\n",
        )
        for output in hostile_outputs:
            with self.subTest(output=output), mock.patch.object(
                observer, "_run_git", return_value=("ok", output, b""),
            ):
                result = observer._remote_result(lawful_request, None)
                self.assertEqual(("unknown", "evidence_malformed"), (
                    result.outcome, result.reason_code,
                ))

    def test_effect_unicode_runtime_and_schema_reject_the_same_hostiles(self) -> None:
        """Catches representative C0, C1, or bidi text drifting across validators."""
        from floati.records import validate_record
        from tests.schema_validation import (
            SchemaValidationError,
            validate_json_schema,
        )

        row = EffectRecordFixture().rows()["effect_intent"]
        schema = REPO_ROOT / "schemas/v1/effect-intent-record.schema.json"
        validate_record(
            copy.deepcopy(row), row["tenant_id"], {"effect_intent"},
            integrity=False,
        )
        validate_json_schema(copy.deepcopy(row), schema)
        for character in ("\x00", "\x85", "\u202e"):
            hostile = copy.deepcopy(row)
            hostile["target"]["coordinate"] += character
            with self.subTest(codepoint=f"U+{ord(character):04X}"):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        copy.deepcopy(hostile), hostile["tenant_id"],
                        {"effect_intent"}, integrity=False,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(copy.deepcopy(hostile), schema)

    def test_effect_source_inventory_has_one_ledger_and_closed_pipe_fields(self) -> None:
        """Catches a second Effect truth surface or credential-bearing pipe drift."""
        import re

        from floati.effect_reconciliation_protocol import REQUEST_FIELDS, RESULT_FIELDS
        from floati.workers import _EFFECT_EVENT_FIELDS

        production = tuple(sorted((REPO_ROOT / "floati").glob("*.py")))
        sources = {
            path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in production
        }
        effect_coordinates = {
            match
            for source in sources.values()
            for match in re.findall(r"effects/[A-Za-z0-9._/-]+[.]jsonl", source)
        }
        self.assertEqual({"effects/records.jsonl"}, effect_coordinates)

        effect_runtime = {
            name: source
            for name, source in sources.items()
            if (
                "effect" in Path(name).name
                or Path(name).name.startswith("worker_")
            )
        }
        forbidden_import = re.compile(
            r"^\s*(?:from\s+[.]?(?:registry|sequencer)(?:\s|[.]|$)"
            r"|import\s+(?:registry|sequencer)(?:\s|[.]|$))",
            re.MULTILINE,
        )
        self.assertTrue(effect_runtime)
        for name, source in effect_runtime.items():
            with self.subTest(source=name):
                self.assertIsNone(forbidden_import.search(source))

        pipe_fields = set(REQUEST_FIELDS) | set(RESULT_FIELDS)
        for fields in _EFFECT_EVENT_FIELDS.values():
            pipe_fields.update(fields)
        self.assertTrue(pipe_fields)
        self.assertTrue({"target", "request_digest"}.issubset(pipe_fields))
        self.assertTrue(
            {
                "authorization", "bearer", "cookie", "credential", "password",
                "raw_record", "request_body", "secret", "token",
            }.isdisjoint(pipe_fields)
        )


if __name__ == "__main__":
    unittest.main()
