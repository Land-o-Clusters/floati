"""Real scratch components for Stage1; never launches a provider."""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.lane_workspaces import LaneWorkspaces
from floati.planes import AuthorityGrantStore
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from floati.seat_declaration import SeatDeclaration
from floati.work import WorkLog
from tests.temp_roots import REAL_TEMP_ROOT


def declared_api(case, module, name):
    """Final implementation APIs are required; missing names cannot skip GREEN."""
    loaded = importlib.import_module(module)
    case.assertTrue(hasattr(loaded, name), module + "." + name + " is required")
    return getattr(loaded, name)


class AdhocFixture:
    def setUp(self, *, root=None):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        private_home = self.base / "private-home"
        private_home.mkdir(mode=0o700)
        environment = mock.patch.dict(os.environ, {
            "HOME": str(private_home), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.now = datetime.now(timezone.utc)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True) if root is None else root
        self.assertIsInstance(self.root, FloatiRoot)
        self.registry = Registry(self.root)
        for node in ("coordinator", "temporary", "foreign", "permanent"):
            self.registry.register(node, "Codex")
        governance = self.registry.record_governance(
            topology="star", coordinator="coordinator",
            coordinator_authority=("dispatch_bounded_work",),
            owner_tier=("publishing", "credentials", "key_custody"))
        for node in ("temporary", "foreign", "permanent"):
            SeatDeclaration.create(self.root.path / "nodes" / node, node,
                                   self.root, governance)
        self.lease = self.add_lease("temporary")
        self.foreign_lease = self.add_lease("foreign")
        self.repository = self.base / "repository"
        self.repository.mkdir()
        self.git(self.repository, "init", "--quiet", "--initial-branch=main")
        self.git(self.repository, "config", "user.name", "Adhoc Fixture")
        self.git(self.repository, "config", "user.email", "fixture@example.invalid")
        (self.repository / "tracked.txt").write_text("retained base\n")
        self.git(self.repository, "add", "tracked.txt")
        self.git(self.repository, "commit", "--quiet", "-m", "fixture base")
        self.base_sha = self.git(self.repository, "rev-parse", "HEAD")
        self.git(self.repository, "update-ref", "refs/remotes/origin/main", self.base_sha)
        self.state("lanes-root.json", {"schema_version": 0, "path": str(self.base / "lanes")})
        self.state("lane-repositories.json", {"schema_version": 0, "repositories": {
            "fixture": {"path": str(self.repository), "default_base": "refs/remotes/origin/main"}}})
        self.state("lane-board.json", {"schema_version": 0, "rows": {}})
        self.lanes = LaneWorkspaces(self.root)
        self.opened = self.lanes.open(actor="temporary", row="adhoc-one", repo="fixture")
        self.workspace = Path(self.opened["workspace"])
        self.foreign_opened = self.lanes.open(actor="foreign", row="foreign-one", repo="fixture")
        self.work = WorkLog(self.root)
        self.item = self.work.add("exact ad-hoc fixture work", "temporary",
            [{"repo": "fixture", "sha": self.base_sha, "doc": "tracked.txt"}],
            car_binding={"row": "adhoc-one", "repository": "fixture",
                         "branch": "codex/lane/temporary/adhoc-one"}, now=self.now)
        AuthorityGrantStore(self.root).grant_exact("adhoc-work", "temporary", 1, self.now)
        self.events = EventLog(self.root)

    def git(self, repository, *arguments):
        result = subprocess.run(["/usr/bin/git", "-C", str(repository), *arguments],
            text=True, capture_output=True, check=True, timeout=15)
        return result.stdout.strip()

    def commit_row_entries(self, workspace, entries):
        """Commit ordinary tracked entries in a row before a profile binds it.

        A provider edits paths that already exist in its row; the Linux kernel
        boundary keeps the row root's own entry set fixed for the dispatch.
        """
        for relative, text in entries.items():
            path = Path(workspace) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            self.git(workspace, "add", relative)
        self.git(workspace, "commit", "--quiet", "-m", "fixture row entries")

    def state(self, name, value):
        path = self.root.path / "state" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) + "\n")

    def add_lease(self, node, *, expires=None, workspace=None):
        row = {"schema_version": 0, "id": "lease-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": self.now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "kind": "node_lease", "node_id": node,
            "workspace": str(self.root.path / "nodes" / node) if workspace is None else str(workspace),
            "expires_at": (self.now + timedelta(hours=1) if expires is None else expires
                ).isoformat(timespec="milliseconds").replace("+00:00", "Z"), "state": "active"}
        append_record(self.root, self.registry.relative_path, row, allowed_kinds=REGISTRY_KINDS)
        return row

    def profiles(self):
        return declared_api(self, "floati.adhoc_profiles", "AdhocProfiles")(self.root)

    def profile(self):
        return self.profiles().create("temporary", "codex", self.opened["record"]["id"],
            "fixture", "coordinator", idempotency_key="profile-one", now=self.now)

    def dispatch(self, profile, **changes):
        value = {"schema_version": 1, "profile_id": profile["id"],
            "profile_digest": profile["digest"], "node_id": "temporary",
            "node_lease_id": self.lease["id"], "lane_opening_id": self.opened["record"]["id"],
            "work_item_id": self.item["id"], "authority_subject": "adhoc-work", "authority_epoch": 1,
            "operation": "start", "permission": {"mode": "on_request_deny_unattended",
                "approval_policy": "on-request", "reviewer": "user", "sandbox": "workspace-write",
                "network": False},
            "requested_duration": 120, "result_document": "docs/evidence/result.json",
            "result_recipient": "coordinator"}
        value.update(changes)
        return value

    def envelope(self, dispatch, *, name="dispatch-one", sender="coordinator", recipient="temporary", raw=None, note="coordinate-free notice"):
        relative = "docs/dispatch/" + name + ".json"
        path = self.repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dispatch, sort_keys=True, separators=(",", ":")) + "\n" if raw is None else raw)
        self.git(self.repository, "add", relative)
        self.git(self.repository, "commit", "--quiet", "-m", name)
        sha = self.git(self.repository, "rev-parse", "HEAD")
        self.git(self.repository, "update-ref", "refs/remotes/origin/main", sha)
        return self.events.send(sender, recipient, "fixture", sha, relative, note,
                                idempotency_key=name, now=self.now)

    def journal(self):
        return declared_api(self, "floati.adhoc_journal", "AdhocJournal")(self.root)

    def admit(self, journal, profile, envelope, dispatch, *, key="dispatch-one"):
        return journal.admit(profile["id"], envelope["id"], dispatch, actor="temporary",
            acting_session_id="fixture-parent", idempotency_key=key, now=self.now)

    def tree_bytes(self, root=None):
        selected = self.root.path if root is None else root
        return {path.relative_to(selected).as_posix(): path.read_bytes()
                for path in selected.rglob("*") if path.is_file() and not path.is_symlink()}

    def refuses(self, code, action):
        with self.assertRaises(ProtocolRefusal) as caught:
            action()
        self.assertEqual(code, caught.exception.code)

    def assert_no_provider_effects(self):
        self.assertFalse((self.root.path / "receipts/workers.jsonl").exists())
        self.assertEqual("retained base\n", (self.workspace / "tracked.txt").read_text())
        self.assertEqual(self.base_sha, self.git(self.workspace, "rev-parse", "HEAD"))

    def replace_provider_slot_at_same_path(self, profile):
        """Retain identical slot bytes/mode while changing actual directory identity."""
        slot = self.root.path / "provider-state" / profile["id"]
        original = slot.stat()
        retained = self.base / "retained-original-provider-slot"
        slot.rename(retained)
        shutil.copytree(retained, slot)
        slot.chmod(0o700)
        replacement = slot.stat()
        self.assertNotEqual((original.st_dev, original.st_ino),
                            (replacement.st_dev, replacement.st_ino))
        self.assertEqual(self.tree_bytes(retained), self.tree_bytes(slot))
        return slot

    def effect_fixture(self, item_id):
        """Build actual native Run/Work/Effect owners around this exact work ID.

        Reuses the established fixture's parent admission/attempt operations;
        only its initial root/item coordinates and tenant-aware append differ.
        """
        from floati.admission import AdmissionBinder, AdmissionPlan
        from floati.approvals import ApprovalLedger
        from floati.capabilities import CapabilityGrantLedger
        from floati.capability_binding import CapabilityBinder
        from floati.contracts import contract_digest
        from floati.effects import EffectController, EffectLedger
        from floati.policy import RepositoryPolicy
        from floati.runtruth import RunLedger
        from floati.scheduler import RunScheduler
        from floati.spawn_groups import SpawnGroupController
        from tests.test_admission import VALID_POLICY
        from tests.test_spawn_groups import _Task2Case

        class BoundRun(_Task2Case):
            def __init__(bound):
                bound.testcase = self
                bound.root = self.root
                bound.run_id = "run-" + uuid7_hex()
                bound.parent = item_id
                policy_path = self.base / "FLOATI.toml"
                policy_path.write_text(VALID_POLICY)
                bound.policy = RepositoryPolicy.load(policy_path)
                bound.ledger = RunLedger(self.root)
                contract = bound.contract([])
                bound.plan = AdmissionPlan.from_canonical({"schema_version": 0,
                    "workers": [{"node_id": "node-a", "worker_profile": "good"}],
                    "max_active_attempts": 2, "budget_reservations": [{"budget_id": "build", "amount": 4}],
                    "items": [bound.item(item_id, contract, "borrowed")], "dependency_edges": []})
                bound._append("run_created", "run-created-", plan_digest=bound.plan.digest,
                    policy_digest=bound.policy.digest, item_ids=[item_id], dependency_edges=[])
                bound._append("task_contract", "task-contract-", item_id=item_id,
                    **contract.canonical(), contract_digest=contract_digest(contract))
                bound._append("run_policy_bound", "run-policy-bound-", policy_digest=bound.policy.digest)
                bound._append("worker_pool_bound", "run-worker-pool-bound-", worker_ids=["node-a"])
                bound.admission = AdmissionBinder.bind(bound.ledger, bound.run_id, bound.plan,
                    bound.policy, now=bound.now(0))
                bound.scheduler = RunScheduler(bound.ledger)
                bound.capability_binder = CapabilityBinder(bound.ledger, CapabilityGrantLedger(self.root))
                bound.controller = SpawnGroupController(bound.ledger, bound.policy)
                bound.opened = bound.snapshot = bound.spawn_policy = bound.dispatch = bound.started = None

            def _append(bound, kind, prefix, **fields):
                return bound.ledger.append({"schema_version": 0, "id": prefix + uuid7_hex(),
                    "tenant_id": self.root.tenant_id, "timestamp": "2026-08-10T12:00:00.000Z",
                    "kind": kind, "run_id": bound.run_id, **fields})

        # The inherited native prepare_parent contains the historical fixture's
        # literal alpha tenant in its capability frame; its owning root must be
        # alpha too, rather than weakening a durable tenant check.
        self.assertEqual("alpha", self.root.tenant_id)
        bound = BoundRun()
        bound.prepare_parent(mode="disabled")
        controller = EffectController(EffectLedger(self.root), bound.ledger,
            bound.policy, ApprovalLedger(self.root))
        return controller, {"run_id": bound.run_id, "item_id": item_id,
                            "attempt_id": bound.opened["attempt_id"]}
