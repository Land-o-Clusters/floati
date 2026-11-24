"""Native positive teardown at both exact EVENTS census boundaries.

These are correctness controls, not performance observations. Native authors
establish the common preface; the existing seed builder publishes and validates
the 1,000/10,000-frame histories. The real activity observer is never replaced.
Every invocation retains its actual artifact and fixture even on refusal; an
expected-success assertion must not relabel a refusal as completed work.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import traceback
import unittest
from tests.bus_scale_test_profile import StableSourceFixture

from floati import cli
from floati.admin_registry import RegistryAdminBackend
from floati.codex_wait_contract import (
    WORKSPACE_MAP_RELATIVE, CodexWaitConsentLedger, CodexWaitSessionLedger,
    resolve_participant, write_workspace_map,
)
from floati.events import EventLog, validate_event_records
from floati.git_process import fixed_git_command, fixed_git_environment
from floati.ids import uuid7_hex
from floati.jsonl import MAX_RECORD_BYTES, read_records_snapshot
from floati.prep_clear import PrepClear
from floati.provider_switch import ProviderSwitchWizard
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_declaration import FleetGovernance, SeatDeclaration
from floati.seat_teardown import SeatTeardown, completed_teardown_for_session
from floati.seat_teardown_prefix import prefix_proof
from floati.wake_control import WakeController
from tests.bus_scale_case_seeds import inventory, make_case_seed_pair
from tests.bus_scale_test_profile import full_scale_enabled, seed_counts
from tests.bus_scale_bounded_transport import bounded_activity
from tests.temp_roots import REAL_TEMP_ROOT, _PLATFORM_TEMP_ROOT


SOURCE = Path(__file__).resolve().parents[1]
ACTOR = "scale-worker"
SESSION = "census-private-seat-teardown"
SOURCE_PATHS = (
    "tests/test_bus_scale_seat_teardown_boundary.py", "tests/bus_scale_case_seeds.py",
    "tests/bus_scale_seeds.py", "floati/events.py", "floati/registry.py",
    "floati/codex_wait_contract.py", "floati/prep_clear.py", "floati/seat_declaration.py",
    "floati/seat_teardown.py", "floati/seat_teardown_activity.py",
    "floati/admin_cli.py", "floati/records.py", "floati/jsonl.py",
    "schemas/v1/seat-teardown-record.schema.json",
)


def _json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _source_hashes():
    return {relative: hashlib.sha256((SOURCE / relative).read_bytes()).hexdigest()
            for relative in SOURCE_PATHS}


def _inventory(path):
    """Retain every ordinary input byte's identity, without hiding Git files."""
    return {relative: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                       "mode": stat.S_IMODE((path / relative).lstat().st_mode)}
            for relative, raw in inventory(path).items()}


class _NativePrelude:
    def __init__(self, git, seat_home):
        self.git, self.seat_home = git, seat_home

    def prepare(self, case, home, workspace, bank):
        if case != "seat-teardown" or workspace != self.seat_home / "repo":
            raise AssertionError("private teardown preface escaped its declared primary")
        root = FloatiRoot.open_direct_home(home)
        backend = RegistryAdminBackend(root)
        plan = ProviderSwitchWizard(root, backend, id_factory=uuid7_hex)._plan(
            ("architect", "architect", "census-private-coordinator"))
        backend.commit_switch(plan)
        governance = FleetGovernance.create(root, topology="star", coordinator="architect",
            coordinator_authority=["dispatch_bounded_work"], owner_tier=["publishing"])
        SeatDeclaration.publish_declared_workspace(workspace, ACTOR, root, governance)
        self.git(workspace, "add", "--", "note.md", "SEAT.json")
        self.git(workspace, "commit", "--quiet", "-m", "Private native teardown seat")
        self.git(workspace, "push", "--quiet", "--set-upstream", "origin", "main")
        sha = self.git(workspace, "rev-parse", "HEAD").strip()
        write_workspace_map(root.tenant_home, workspace, ACTOR)
        binding = resolve_participant(root.tenant_home, workspace, _strict=True).binding
        consent = CodexWaitConsentLedger(root).arm(binding, hook_timeout_seconds=10,
            wait_deadline_seconds=2, idempotency_key="census-private-teardown-consent")
        claim = CodexWaitSessionLedger(root).arm(binding, consent, SESSION,
            idempotency_key="census-private-teardown-claim")
        selected = EventLog(root).send(ACTOR, "architect", "floati", sha, "note.md",
            "native private teardown prerequisite", idempotency_key="census-private-teardown-mail")["message"]
        return {"home": str(self.seat_home), "workspace": str(workspace), "sha": sha,
                "session": SESSION, "claim_id": claim["id"], "consent_id": consent["id"],
                "selected_id": selected["id"], "map_digest": binding.map_digest}


class SeatTeardownEventsBoundaryTests(StableSourceFixture, unittest.TestCase):
    def _run_boundary(self, scale, count):
        # Separate from the runner's removable HOME/TMP tree: failed native
        # testimony survives scratch cleanup and is available for RED banking.
        retained = Path(tempfile.mkdtemp(prefix="bus-scale-seat-teardown-boundary-",
                                         dir=_PLATFORM_TEMP_ROOT)).resolve()
        temporary = tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-teardown-",
                                                dir=REAL_TEMP_ROOT)
        scratch = Path(temporary.name).resolve()
        record = {"scale": scale, "expected_event_frames": count,
                  "expected_status": "ok", "expected_step": "completed",
                  "native_activity_unmodified": full_scale_enabled(), "performance_evidence": False,
                  "transport_profile": "full_native" if full_scale_enabled() else "bounded_activity_fixture",
                  "scratch": str(scratch), "retained": str(retained),
                  "source_before": _source_hashes(), "git_commands": []}
        stdout, stderr = io.StringIO(), io.StringIO()
        artifact = None
        caught = None

        def git(workspace, *arguments):
            argv = fixed_git_command("/usr/bin/git", workspace, arguments)
            environment = fixed_git_environment("/usr/bin/git")
            result = subprocess.run(argv, cwd=str(scratch), env=environment,
                capture_output=True, text=True, timeout=30)
            record["git_commands"].append({"argv": argv, "cwd": str(scratch),
                "env": environment, "exit_code": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr})
            if result.returncode:
                raise AssertionError("private native Git prerequisite failed: " + result.stderr)
            return result.stdout

        try:
            record["source_head"] = git(SOURCE, "rev-parse", "HEAD").strip()
            seat_home = scratch / "seat-home"
            seat_home.mkdir(mode=0o700)
            workspace = seat_home / "repo"
            workspace.mkdir(mode=0o700)
            remote = scratch / "origin.git"
            git(scratch, "init", "--quiet", "--bare", str(remote))
            git(workspace, "init", "--quiet", "-b", "main")
            git(workspace, "config", "user.name", "Private Census Fixture")
            git(workspace, "config", "user.email", "fixture@example.invalid")
            git(workspace, "config", "commit.gpgsign", "false")
            git(workspace, "remote", "add", "origin", str(remote))
            (workspace / "note.md").write_text("owned native teardown evidence\n", encoding="utf-8")
            seeds = scratch / "seeds"
            seeds.mkdir(mode=0o700)
            pair = make_case_seed_pair(seeds, "events", "seat-teardown",
                                       _NativePrelude(git, seat_home), workspace, counts=seed_counts())
            home = pair["prepared_home"]
            # Native consent is path-bound. Restore the immutable arm at its
            # actual authored coordinate rather than redirecting the operation.
            shutil.rmtree(home)
            shutil.copytree(pair[scale], home)
            root = FloatiRoot.open_direct_home(home)
            context = pair["context"]
            binding = resolve_participant(home, workspace, _strict=True).binding
            self.assertEqual("architect", Registry(root).require_active("architect")["role"])
            self.assertEqual(context["claim_id"], PrepClear(root)._held_claim(binding, SESSION)["id"])
            self.assertEqual(context["consent_id"], CodexWaitConsentLedger(root).require_armed(binding)["id"])
            self.assertEqual(context["map_digest"], binding.map_digest)
            self.assertEqual("architect", FleetGovernance.load(root).coordinator)
            self.assertEqual(ACTOR, SeatDeclaration.load(workspace).node_id)
            self.assertTrue((workspace / ".git").is_dir())
            self.assertFalse(git(workspace, "status", "--porcelain=v1", "--untracked-files=all"))
            self.assertEqual(context["sha"], git(workspace, "rev-parse", "refs/remotes/origin/main").strip())
            self.assertFalse(git(workspace, "rev-list", "HEAD", "--not", "--remotes"))
            events = EventLog(root).event_records()
            validate_event_records(events)
            self.assertEqual(count, len(events))
            self.assertFalse(EventLog(root).unacked_ids(ACTOR))
            self.assertEqual({"work_item_ids": [], "worker_session_ids": [],
                              "thread_attachment_ids": []}, SeatTeardown(root)._work(ACTOR))
            selected = next(row for row in events if row["id"] == context["selected_id"])
            self.assertEqual("architect", selected["recipient"])
            self.assertEqual(ACTOR, selected["sender"])
            self.assertFalse(any(row.get("recipient") == ACTOR for row in events))
            immutable = {s: _inventory(pair[s]) for s in ("1", "10")}
            before = _inventory(home)
            _json(retained / "before-inventory.json", before)
            _json(retained / "immutable-seed-inventories.json", immutable)
            record["fixture_prerequisites_passed"] = True
            record["padding_provenance"] = {
                "builder": "tests.bus_scale_case_seeds.make_case_seed_pair",
                "family": "events", "published_frames": count,
                "native_author_commands_claimed": False,
                "selected_message_ids": [context["selected_id"]],
                "selected_message_count": 1,
                "actor_pending_message_count": 0,
                "unselected_history_recipient": "scale-history",
                "small_exact_prefix_of_large": (pair["10"] / "events.jsonl").read_bytes().startswith(
                    (pair["1"] / "events.jsonl").read_bytes()),
            }
            record["event_cutoff_ids"] = [row["id"] for row in events]
            record["cutoff_ids_json_bytes"] = len(json.dumps(record["event_cutoff_ids"],
                separators=(",", ":")).encode())
            record["native_record_limit_bytes"] = MAX_RECORD_BYTES
            record["argv"] = ["seat", "teardown", "--root", str(home), "--as", ACTOR,
                "--home", str(seat_home), "--session", SESSION, "--repo", "floati",
                "--doc", "note.md", "--idempotency-key", "census-private-teardown"]
            record["invocation_started_at"] = datetime.now(timezone.utc).isoformat()
            with bounded_activity(), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                record["exit_code"] = cli.main(record["argv"])
            record["invocation_finished_at"] = datetime.now(timezone.utc).isoformat()
            artifact = json.loads(stdout.getvalue())
            _json(retained / "artifact.json", artifact)
            record["status"] = artifact["status"]
            record["refusal"] = artifact["evidence"] if artifact["status"] != "ok" else None
            _json(retained / "after-inventory.json", _inventory(home))
            record["immutable_seeds_unchanged"] = all(_inventory(pair[s]) == immutable[s] for s in ("1", "10"))
            if record["exit_code"] == 0 and artifact["status"] == "ok":
                completed = artifact["evidence"]
                self.assertEqual("completed", completed["step"])
                self.assertEqual(completed, completed_teardown_for_session(root, ACTOR, SESSION))
                self.assertEqual(2, completed["schema_version"])
                self.assertEqual(prefix_proof(events, "events.jsonl"), completed["event_cutoff"])
                self.assertEqual(context["claim_id"], completed["claim_receipt_id"])
                self.assertEqual(context["consent_id"], completed["consent_receipt_id"])
                self.assertEqual({"population": "invoking_uid", "uid": os.getuid()},
                                 completed["activity"]["native_scope"])
                self.assertEqual([], completed["activity"]["native_jobs"])
                self.assertEqual([], completed["work_evidence"]["work_item_ids"])
                self.assertEqual([], completed["work_evidence"]["worker_session_ids"])
                self.assertEqual([], completed["work_evidence"]["thread_attachment_ids"])
                rows = read_records_snapshot(root, "receipts/seat-teardown/" + ACTOR + ".jsonl",
                                             allowed_kinds={"seat_teardown_receipt"})
                self.assertEqual(["started", "paused", "reported", "completed"],
                                 [row["step"] for row in rows])
                self.assertEqual("paused", WakeController(root).status(ACTOR, SESSION)["state"])
                after_events = EventLog(root).event_records()
                self.assertEqual(events, after_events[:count])
                self.assertEqual(["message_envelope", "send_readiness_observation"],
                                 [row["kind"] for row in after_events[count:]])
                self.assertEqual(completed["envelope_id"], after_events[count]["id"])
                self.assertEqual("architect", after_events[count]["recipient"])
                self.assertFalse(EventLog(root).unacked_ids(ACTOR))
                self.assertEqual(before[WORKSPACE_MAP_RELATIVE.as_posix()],
                                 _inventory(home)[WORKSPACE_MAP_RELATIVE.as_posix()])
                CodexWaitConsentLedger(root).require_armed(binding)
                record["native_completed_receipt_verified"] = True
            self.assertTrue(record["immutable_seeds_unchanged"])
        except BaseException as error:
            caught = error
            record["exception"] = {"type": type(error).__name__, "message": str(error),
                                   "traceback": traceback.format_exc()}
        finally:
            if "invocation_started_at" in record and "invocation_finished_at" not in record:
                record["invocation_finished_at"] = datetime.now(timezone.utc).isoformat()
            (retained / "stdout.txt").write_text(stdout.getvalue(), encoding="utf-8")
            (retained / "stderr.txt").write_text(stderr.getvalue(), encoding="utf-8")
            record["source_after"] = _source_hashes()
            try:
                for relative in SOURCE_PATHS:
                    target = retained / "source" / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    shutil.copyfile(SOURCE / relative, target)
                    target.chmod(0o600)
                shutil.copytree(scratch, retained / "fixture")
                record["retained_fixture_inventory"] = _inventory(retained / "fixture")
            finally:
                temporary.cleanup()
                record["scratch_removed"] = not scratch.exists()
                _json(retained / "result.json", record)
                print("native seat teardown boundary evidence: " + str(retained), flush=True)
        if caught is not None:
            raise caught
        self.assertTrue(record["scratch_removed"], str(retained))
        self.assertEqual(record["source_before"], record["source_after"])
        self.assertEqual(0, record["exit_code"], "expected native completed teardown; retained actual evidence: "
                         + str(retained) + "; " + json.dumps(record.get("refusal"), sort_keys=True))
        self.assertEqual("ok", artifact["status"], str(retained))
        self.assertTrue(record.get("native_completed_receipt_verified"), str(retained))

    def test_native_teardown_completes_at_1000_event_frames(self):
        self._run_boundary("1", seed_counts()[0])

    def test_native_teardown_completes_at_10000_event_frames(self):
        self._run_boundary("10", seed_counts()[1])
