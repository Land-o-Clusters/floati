"""Native legacy, borrowed readiness, and direct kernel-policy controls."""
from __future__ import annotations

import json
import unittest
import errno
from pathlib import Path
from unittest import mock

from floati.worker_isolation import cleanup_worker_isolation
from floati.worker_errors import WorkerAdapterFailure
from floati.errors import ProtocolRefusal
from floati.workers import WorkerRunner
from tests.fixtures.adhoc_stage1 import AdhocFixture


class _LegacyFixtureAdapter:
    name = "fixture"

    def __init__(self, work, observation):
        self.work = work
        self.observation = observation

    def spawn(self, item, *, deadline_seconds):
        state = self.work.show(item["id"])[0]
        if state["state"] != "claimed":
            raise AssertionError("native worker launched before durable claim")
        self.observation.write_text(json.dumps({"work_item_id": item["id"],
            "state": state["state"], "deadline_seconds": deadline_seconds}))
        return None

    def drive(self, handle, item, *, deadline_seconds):
        return list(item["artifact_bindings"])


class AdhocWorkerBaselineTests(AdhocFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        (self.root.tenant_home / "effects").mkdir(mode=0o700)

    def test_native_exact_claim_leaves_older_owned_item_open(self):
        """An exact native claim keeps an older owned item open."""
        older = self.item
        requested = self.work.add("requested later item", "temporary", [], now=self.now)
        self.assertEqual([older["id"], requested["id"]],
                         [item["id"] for item in self.work.show()])
        transition = self.work.claim(requested["id"], "temporary", "adhoc-work", 1,
                                     now=self.now)
        self.assertEqual(requested["id"], transition["work_item_id"])
        self.assertEqual("open", self.work.show(older["id"])[0]["state"])
        selected = self.work.show(requested["id"])[0]
        self.assertEqual("claimed", selected["state"])
        self.assertEqual("temporary", selected["holder"])

    def test_legacy_runner_still_selects_oldest_owned_item(self):
        later = self.work.add("later legacy item", "temporary", [], now=self.now)
        observation = self.base / "adapter-observation.json"
        runner = WorkerRunner(self.root,
            {"fixture": _LegacyFixtureAdapter(self.work, observation)}, call_timeout=2)
        result = runner.run("temporary", "fixture", now=self.now)
        self.assertEqual("complete", result["transition"])
        evidence = json.loads(observation.read_text())
        self.assertEqual(self.item["id"], evidence["work_item_id"])
        self.assertEqual("claimed", evidence["state"])
        self.assertGreater(evidence["deadline_seconds"], 0)
        self.assertLessEqual(evidence["deadline_seconds"], 2)
        self.assertEqual("completed", self.work.show(self.item["id"])[0]["state"])
        self.assertEqual("open", self.work.show(later["id"])[0]["state"])
        self.assertEqual([], runner.last_process_audit["alive_after_cleanup"])
        self.assertEqual("retained base\n", (self.workspace / "tracked.txt").read_text())

    def test_legacy_oldest_claim_does_not_adopt_a_borrowed_item(self):
        profile = self.profile()
        requested = self.work.add("borrowed exact-only item", "temporary", [],
            adhoc_profile_id=profile["id"], now=self.now)
        self.work.claim(self.item["id"], "temporary", "adhoc-work", 1, now=self.now)
        before = self.work.consumption.records()
        self.refuses("workspace_invalid", lambda: self.work.claim_owned_oldest(
            "temporary", "adhoc-work", 1, now=self.now))
        self.assertEqual(before, self.work.consumption.records())
        self.assertEqual("open", self.work.show(requested["id"])[0]["state"])


class BorrowedIsolationTests(AdhocFixture, unittest.TestCase):
    def test_direct_borrowed_policy_denies_foreign_writes_and_allows_exact_slot(self):
        # Real native policy activation in an owned child, with an executed
        # positive payload. This is kernel policy evidence, not provider login.
        from floati.worker_isolation import prepare_borrowed_worker_isolation
        from tests.test_worker_isolation import _child_result, _try_write
        profile = self.profile()
        binding = {key: profile[key] for key in
                   ("lane_opening_id", "node_lease_id", "workspace")}
        binding["profile_id"] = profile["id"]
        (self.root.tenant_home / "effects").mkdir(mode=0o700)
        slot = self.root.tenant_home / profile["provider_state"]
        foreign_slot = self.root.tenant_home / "provider-state/foreign-slot"
        foreign_slot.mkdir(mode=0o700)
        sibling = Path(self.foreign_opened["workspace"])
        targets = [sibling / "tracked.txt", self.repository / ".git/config",
            self.repository / ".git/hooks/fixture-hook", self.repository / ".git/refs/heads/main",
            self.root.tenant_home / "state/fixture-truth", self.base / "private-home/fixture-config",
            foreign_slot / "history", self.root.tenant_home / "provider-state/parent-write"]
        for target in targets:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"foreign initial bytes\n")
        before = {str(path): path.read_bytes() for path in targets}
        try:
            policy = prepare_borrowed_worker_isolation(self.root, binding, "borrowed-kernel")
        except WorkerAdapterFailure as exc:
            self.assertEqual("effect_worker_isolation_unavailable", exc.code)
            self.assertIn("UNSUPPORTED", exc.detail)
            self.assertEqual(before, {str(path): path.read_bytes() for path in targets})
            return
        self.addCleanup(cleanup_worker_isolation, policy)

        def payload():
            denied = {str(path): _try_write(path) for path in targets}
            denied["scratch"] = _try_write(policy.scratch / "fixture-private", create=True)
            allowed = {"workspace": _try_write(self.workspace / "tracked.txt", truncate=True),
                "provider_state": _try_write(slot / "fixture-history", create=True)}
            return {"denied": denied, "allowed": allowed}

        result = _child_result(policy, payload)
        if result["status"] == "refused":
            self.assertEqual("effect_worker_isolation_unavailable", result["code"])
            self.assertEqual(0, result["payload_callbacks"])
        else:
            self.assertEqual("ready", result["status"], result)
            self.assertEqual(1, result["payload_callbacks"])
            self.assertTrue(all(value is None for value in result["payload"]["allowed"].values()))
            self.assertTrue(all(value in {errno.EACCES, errno.EPERM}
                                for value in result["payload"]["denied"].values()))
            self.assertTrue((slot / "fixture-history").is_file())
            self.assertEqual(b"changed", (self.workspace / "tracked.txt").read_bytes())
        self.assertEqual(before, {str(path): path.read_bytes() for path in targets})

    def test_unsupported_borrowed_backend_refuses_before_child_spawn(self):
        from floati.worker_isolation import prepare_borrowed_worker_isolation
        profile = self.profile()
        binding = {key: profile[key] for key in
                   ("lane_opening_id", "node_lease_id", "workspace")}
        binding["profile_id"] = profile["id"]
        before = self.tree_bytes()
        with mock.patch("floati.worker_isolation.sys.platform", "fixture-unsupported"):
            with self.assertRaises(WorkerAdapterFailure) as caught:
                prepare_borrowed_worker_isolation(self.root, binding, "unsupported-borrowed")
        self.assertEqual("effect_worker_isolation_unavailable", caught.exception.code)
        self.assertIn("UNSUPPORTED", caught.exception.detail)
        self.assertEqual(before, self.tree_bytes())

    def test_borrowed_tracked_hardlink_layout_refuses_before_activation(self):
        import os
        from floati.worker_isolation import prepare_borrowed_worker_isolation
        foreign = Path(self.foreign_opened["workspace"]) / "tracked.txt"
        os.link(foreign, self.workspace / "aliased-foreign.txt")
        self.git(self.workspace, "add", "aliased-foreign.txt")
        self.git(self.workspace, "commit", "--quiet", "-m", "fixture tracked hardlink")
        self.assertGreater((self.workspace / "aliased-foreign.txt").stat().st_nlink, 1)
        profile = self.profile()
        binding = {key: profile[key] for key in ("lane_opening_id", "node_lease_id", "workspace")}
        binding["profile_id"] = profile["id"]
        before = foreign.read_bytes()
        (self.root.tenant_home / "effects").mkdir(mode=0o700)
        with self.assertRaises(WorkerAdapterFailure) as caught:
            prepare_borrowed_worker_isolation(self.root, binding, "hardlink-layout")
        self.assertEqual("effect_worker_isolation_unavailable", caught.exception.code)
        self.assertIn("UNSUPPORTED", caught.exception.detail)
        self.assertEqual(before, foreign.read_bytes())


class BorrowedReadinessTests(AdhocFixture, unittest.TestCase):
    def setUp(self):
        import tempfile
        from floati.root import FloatiRoot
        from floati.seat_declaration import SeatDeclaration
        from tests.temp_roots import REAL_TEMP_ROOT
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        root = FloatiRoot.open_direct_home(Path(temporary.name).resolve() / "alpha", create=True)
        super().setUp(root=root)
        self.registry.register("node-a", "Codex")
        self.node_lease = self.add_lease("node-a")
        SeatDeclaration.create(self.root.path / "nodes/node-a", "node-a", self.root,
                               self.registry.governance())
        self.node_opened = self.lanes.open(actor="node-a", row="borrowed", repo="fixture")
        from floati.planes import AuthorityGrantStore
        AuthorityGrantStore(self.root).grant_exact("borrowed-work", "node-a", 1, self.now)

    def prepared(self, *, human_launch=True):
        from floati.adhoc_journal import AdhocJournal
        from floati.worker_bootstrap_protocol import BuiltInAdapterSpec, NATIVE_THREAD_BOUNDS
        profile = self.profiles().create("node-a", "codex", self.node_opened["record"]["id"],
            "fixture", "coordinator", idempotency_key="node-a-profile", now=self.now)
        older = self.work.add("older node-a owned item", "node-a", [], now=self.now)
        requested = self.work.add("requested native borrowed work", "node-a", [],
            adhoc_profile_id=profile["id"], now=self.now)
        dispatch = self.dispatch(profile, node_id="node-a", node_lease_id=self.node_lease["id"],
            lane_opening_id=profile["lane_opening_id"], work_item_id=requested["id"],
            authority_subject="borrowed-work")
        envelope = self.envelope(dispatch, recipient="node-a")
        admitted = AdhocJournal(self.root).admit(profile["id"], envelope["id"], dispatch,
            actor="node-a", acting_session_id="fixture-parent", idempotency_key="node-a-dispatch", now=self.now)
        if human_launch:
            from floati.adhoc_launch import grant_adhoc_launch
            from floati.adhoc_execution import prepare_adhoc_execution
            grant_adhoc_launch(self.root, profile, authority_subject="borrowed-work", authority_epoch=1, now=self.now)
            actual, effect_binding = prepare_adhoc_execution(self.root, profile, admitted, lambda: self.now)
            controller = actual.effect_controller
        else:
            controller, effect_binding = self.effect_fixture(requested["id"])
        context = self.native_context(profile, requested, admitted, effect_binding, NATIVE_THREAD_BOUNDS)
        executable = self.base / "fixture-native-provider"
        self.spawn_marker = self.base / "provider-was-started"
        executable.write_text("#!/usr/bin/python3\nfrom pathlib import Path\nPath(" + repr(str(self.spawn_marker)) + ").write_text('unexpected provider spawn')\n")
        executable.chmod(0o700)
        runner = WorkerRunner(self.root, {}, effect_controller=controller,
            effect_adapter_specs={"codex": BuiltInAdapterSpec("codex", (str(executable),), "borrowed")})
        return older, requested, context, runner

    def test_exact_borrowed_run_without_human_grant_refuses_before_isolation(self):
        older, requested, context, runner = self.prepared(human_launch=False)
        protocol = self.runtime_protocol(context, runner)
        store = Path(context["provider_state_binding"]["path"])
        before = self.tree_bytes()
        with mock.patch("floati.workers.require_borrowed_native_readiness",
                side_effect=ProtocolRefusal("adhoc_write_confinement_unsupported", "readiness reached")) as readiness:
            with self.assertRaises(ProtocolRefusal) as caught:
                runner.run_exact("node-a", "codex", requested["id"],
                    native_thread_context=context, on_native_event=protocol.accept, now=self.now)
        self.assertIn(caught.exception.code, {"policy_missing", "effect_approval_missing"},
            "a genuine callback and unrelated effect owner must not replace human launch authority")
        readiness.assert_not_called()
        self.assertEqual(before, self.tree_bytes())
        self.assertFalse((store / "launches.jsonl").exists())

    def test_exact_borrowed_run_rejects_substituted_ordinary_effect_owner(self):
        from floati.adhoc_launch import grant_adhoc_launch
        from floati.adhoc_profiles import AdhocProfiles
        older, requested, context, runner = self.prepared(human_launch=False)
        protocol = self.runtime_protocol(context, runner)
        store = Path(context["provider_state_binding"]["path"])
        profile = AdhocProfiles(self.root).get(context["profile_id"])
        grant_adhoc_launch(self.root, profile, authority_subject="borrowed-work", authority_epoch=1, now=self.now)
        before = self.tree_bytes()
        with mock.patch("floati.workers.require_borrowed_native_readiness",
                side_effect=ProtocolRefusal("adhoc_write_confinement_unsupported", "readiness reached")) as readiness:
            with self.assertRaises(ProtocolRefusal) as caught:
                runner.run_exact("node-a", "codex", requested["id"],
                    native_thread_context=context, on_native_event=protocol.accept, now=self.now)
        self.assertEqual("effect_launch_binding_invalid", caught.exception.code,
            "an exact human grant must be consumed by the actual native capability snapshot")
        readiness.assert_not_called()
        self.assertEqual(before, self.tree_bytes())
        self.assertFalse((store / "launches.jsonl").exists())

    def test_unbound_native_callback_refuses_before_exact_claim(self):
        older, requested, context, runner = self.prepared()
        store = Path(context["provider_state_binding"]["path"])
        with self.assertRaises(ProtocolRefusal) as caught:
            runner.run_exact("node-a", "codex", requested["id"],
                native_thread_context=context, on_native_event=lambda event: None, now=self.now)
        self.assertEqual(caught.exception.code, "adhoc_dispatch_authentication_failed")
        self.assertEqual(self.work.show(requested["id"])[0]["state"], "open")
        self.assertFalse((store / "launches.jsonl").exists())

    def runtime_protocol(self, context, runner):
        from floati.adhoc_journal import AdhocJournal, AdhocJournalController
        from floati.native_thread_protocol import NativeThreadProtocol
        return NativeThreadProtocol(context, adapter_kind="codex",
            journal_controller=AdhocJournalController(AdhocJournal(self.root)),
            effect_controller=runner.effect_controller, now=self.now)

    def test_borrowed_readiness_refuses_before_any_claim_or_spawn(self):
        from floati.errors import ProtocolRefusal
        from floati.adhoc_journal import AdhocJournal, AdhocJournalController
        from floati.native_thread_protocol import NativeThreadProtocol
        older, requested, context, runner = self.prepared()
        protocol = NativeThreadProtocol(context, adapter_kind="codex",
            journal_controller=AdhocJournalController(AdhocJournal(self.root)),
            effect_controller=runner.effect_controller, now=self.now)
        before = self.work.consumption.records()
        with mock.patch("floati.workers.spawn_effect_worker") as spawn, mock.patch(
                "floati.workers.require_borrowed_native_readiness",
                side_effect=ProtocolRefusal("adhoc_write_confinement_unsupported", "W1 UNSUPPORTED: fixture readiness")):
            with self.assertRaises(ProtocolRefusal) as caught:
                runner.run_exact("node-a", "codex", requested["id"],
                    native_thread_context=context, on_native_event=protocol.accept, now=self.now)
            self.assertEqual("adhoc_write_confinement_unsupported", caught.exception.code)
            self.assertIn("UNSUPPORTED", caught.exception.detail)
            spawn.assert_not_called()
        self.assertEqual(before, self.work.consumption.records())
        self.assertEqual("open", self.work.show(older["id"])[0]["state"])
        self.assertEqual("open", self.work.show(requested["id"])[0]["state"])
        self.assertEqual(120, runner.last_native_deadline)
        self.assertFalse(self.spawn_marker.exists())

    def test_foreign_effect_binding_refuses_before_claim_or_spawn(self):
        from floati.errors import ProtocolRefusal
        from floati.ids import uuid7_hex
        _older, requested, context, runner = self.prepared()
        context["effect_binding"]["attempt_id"] = "attempt-" + uuid7_hex()
        before = self.work.consumption.records()
        with mock.patch("floati.workers.spawn_effect_worker") as spawn:
            with self.assertRaises(ProtocolRefusal) as caught:
                runner.run_exact("node-a", "codex", requested["id"],
                    native_thread_context=context, on_native_event=lambda event: None, now=self.now)
            self.assertEqual("effect_launch_binding_invalid", caught.exception.code)
            spawn.assert_not_called()
        self.assertEqual(before, self.work.consumption.records())

    def test_v2_admission_rejects_a_different_profiles_work_binding(self):
        from floati.adhoc_journal import AdhocJournal
        from floati.errors import ProtocolRefusal
        first = self.profiles().create("node-a", "codex", self.node_opened["record"]["id"],
            "fixture", "coordinator", idempotency_key="node-first", now=self.now)
        second = self.profiles().create("node-a", "codex", self.node_opened["record"]["id"],
            "fixture", "coordinator", idempotency_key="node-second", now=self.now)
        self.assertNotEqual(first["id"], second["id"])
        item = self.work.add("foreign profile binding", "node-a", [],
            adhoc_profile_id=second["id"], now=self.now)
        dispatch = self.dispatch(first, node_id="node-a", node_lease_id=self.node_lease["id"],
            lane_opening_id=first["lane_opening_id"], work_item_id=item["id"],
            authority_subject="borrowed-work")
        envelope = self.envelope(dispatch, recipient="node-a")
        before = self.work.consumption.records()
        journal = AdhocJournal(self.root)
        with self.assertRaises(ProtocolRefusal) as caught:
            journal.admit(first["id"], envelope["id"], dispatch, actor="node-a",
                acting_session_id="fixture-parent", idempotency_key="foreign-profile", now=self.now)
        self.assertEqual("adhoc_dispatch_authentication_failed", caught.exception.code)
        self.assertEqual([], journal.records())
        self.assertEqual(before, self.work.consumption.records())

    def native_context(self, profile, requested, admitted, effect_binding, bounds):
        from copy import deepcopy
        return {"schema_version": 1, "profile_id": profile["id"], "profile_digest": profile["digest"],
            "dispatch_id": admitted["dispatch_id"], "dispatch_digest": admitted["dispatch_digest"],
            "node_id": profile["node_id"], "node_lease_id": profile["node_lease_id"],
            "work_item_id": requested["id"], "operation": "start", "provider_coordinate": None,
            "workspace_binding": deepcopy(requested["adhoc_binding"]),
            "provider_state_binding": {"path": str(self.root.tenant_home / profile["provider_state"]),
                **profile["filesystem_bindings"]["provider_state"]},
            "permission": deepcopy(admitted["dispatch"]["permission"]),
            "bounds": deepcopy(bounds), "effect_binding": deepcopy(effect_binding)}


class NativeContextBoundaryTests(unittest.TestCase):
    def test_native_context_rejects_added_authority_and_detaches_bindings(self):
        from copy import deepcopy
        from floati.errors import ProtocolRefusal
        from floati.worker_bootstrap_protocol import validate_native_thread_context
        from floati.worker_bootstrap_protocol import NATIVE_THREAD_BOUNDS
        token = "0199e000000070008000000000000001"
        context = {"schema_version": 1,
            "profile_id": "adhoc-profile-" + token, "profile_digest": "1" * 64,
            "dispatch_id": "adhoc-dispatch-" + token, "dispatch_digest": "2" * 64,
            "node_id": "temporary", "node_lease_id": "lease-" + token,
            "work_item_id": "work-" + token, "operation": "start", "provider_coordinate": None,
            "provider_state_binding": {"path": "/fixture/provider", "dev": 1, "ino": 2},
            "workspace_binding": {"profile_id": "adhoc-profile-" + token,
                "lane_opening_id": "lane-workspace-" + token, "node_lease_id": "lease-" + token,
                "workspace": "/fixture/workspace"},
            "permission": {"mode": "tools_disabled"}, "bounds": dict(NATIVE_THREAD_BOUNDS),
            "effect_binding": {"run_id": "run-" + token, "item_id": "work-" + token,
                               "attempt_id": "attempt-" + token}}
        retained = deepcopy(context)
        detached = validate_native_thread_context(context)
        detached["workspace_binding"]["workspace"] = "/foreign"
        self.assertEqual(retained, context)
        context["git_common_dir"] = "/fixture/repository/.git"
        with self.assertRaises(ProtocolRefusal) as caught:
            validate_native_thread_context(context)
        self.assertEqual("adhoc_dispatch_authentication_failed", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
