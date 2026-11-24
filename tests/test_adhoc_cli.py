"""Public ad-hoc verbs compose real scratch profiles and admission state."""
from __future__ import annotations

import contextlib
import io
import json
import shlex
from datetime import timedelta
from pathlib import Path
import unittest

from floati.cli import main
from tests.fixtures.adhoc_stage1 import AdhocFixture
from tests.test_adhoc_threads import ControllerFixture


class AdhocCliTests(AdhocFixture, unittest.TestCase):
    def public(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(list(arguments))
        return code, json.loads(output.getvalue())

    def add_bound_work(self, profile, title="public bound work"):
        code, artifact = self.public("work", "add", "--root", str(self.root.path),
            "--owner", "temporary", "--title", title, "--adhoc-profile", profile["id"])
        self.assertEqual(0, code, artifact)
        return artifact["evidence"]

    def invoke(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["worker", "adhoc", *arguments])
        artifact = json.loads(output.getvalue())
        self.assertEqual(code == 0, artifact["status"] == "ok")
        return code, artifact

    def create_profile(self, *extra):
        code, artifact = self.invoke("profile", "--root", str(self.root.path),
            "--as", "temporary", "--adapter", "codex", "--lane-opening",
            self.opened["record"]["id"], "--repository", "fixture", "--to",
            "coordinator", "--idempotency-key", "cli-profile", *extra)
        self.assertEqual(0, code, artifact)
        profiles = self.profiles().records()
        self.assertEqual(1, len(profiles))
        profile = profiles[0]
        self.assertEqual(self.lease["id"], profile["node_lease_id"])
        self.assertEqual(str(self.workspace), profile["workspace"])
        self.assert_no_provider_effects()
        return profile

    def test_cli_dispatch_has_no_provider_coordinate_override(self):
        profile = self.create_profile()
        for option in ("--provider-id", "--session-file", "--permission",
                       "--duration", "--operation", "--workspace", "--executable"):
            with self.subTest(option=option):
                before = self.tree_bytes()
                code, artifact = self.invoke("run", "--root", str(self.root.path),
                    "--as", "temporary", "--session", "fixture-parent", "--profile",
                    profile["id"], "--envelope", "message-00000000000070008000000000000000",
                    "--idempotency-key", "closed-cli", option, "caller-override")
                self.assertEqual(20, code, artifact)
                self.assertEqual("arguments_invalid", artifact["evidence"]["code"])
                self.assertEqual(before, self.tree_bytes())
                self.assert_no_provider_effects()

    def test_cli_run_requires_exact_authenticated_envelope(self):
        profile = self.create_profile()
        before = self.tree_bytes()
        code, artifact = self.invoke("run", "--root", str(self.root.path),
            "--as", "temporary", "--session", "fixture-parent", "--profile",
            profile["id"], "--envelope", "message-00000000000070008000000000000000",
            "--idempotency-key", "missing-envelope")
        self.assertEqual(20, code, artifact)
        self.assertEqual("adhoc_dispatch_authentication_failed", artifact["evidence"]["code"])
        self.assertEqual(before, self.tree_bytes())
        self.assert_no_provider_effects()

    def test_cli_show_refuses_missing_dispatch_without_creating_state(self):
        before = self.tree_bytes()
        code, artifact = self.invoke("show", "--root", str(self.root.path),
            "--dispatch", "adhoc-dispatch-00000000000070008000000000000000")
        self.assertEqual(20, code, artifact)
        self.assertEqual("adhoc_dispatch_authentication_failed", artifact["evidence"]["code"])
        self.assertEqual(before, self.tree_bytes())

    def test_cli_profile_refuses_stage_two_adapter_and_implicit_coordinates(self):
        for extra in (("--adapter", "template"), ("--adapter", "codex", "--workspace", "\x2ftmp/foreign")):
            with self.subTest(extra=extra):
                before = self.tree_bytes()
                code, artifact = self.invoke("profile", "--root", str(self.root.path),
                    "--as", "temporary", "--lane-opening", self.opened["record"]["id"],
                    "--repository", "fixture", "--to", "coordinator", "--idempotency-key",
                    "unsupported", *extra)
                self.assertEqual(20, code, artifact)
                self.assertEqual("arguments_invalid", artifact["evidence"]["code"])
                self.assertEqual(before, self.tree_bytes())


class AdhocWorkBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = None

    def setUp(self):
        if self._testMethodName == "test_public_bound_work_reaches_native_execution_admission":
            # Launch approval writes root-wide policy, so this property needs
            # its own fleet; remedies below must still reach policy_missing.
            self.case = AdhocCliTests()
            self.addCleanup(self.case.doCleanups)
            self.case.setUp()
            self.profile = self.case.create_profile("--authority-subject", "adhoc-work",
                "--authority-epoch", "1", "--grant-launch")
        else:
            cls = type(self)
            if cls.shared is None:
                # Lazy setup keeps its entire cost inside the first test's
                # timing interval, including when selectors run individually.
                case = AdhocCliTests()
                cls.addClassCleanup(case.doCleanups)
                case.setUp()
                code, artifact = case.public("node", "role", "--root", str(case.root.path),
                    "--node", "coordinator", "--template", "architect", "--answer", "repo=fixture",
                    "--answer", "never_touch=foreign-project", "--answer", "owner_stops=owner-tier")
                self.assertEqual(0, code, artifact)
                cls.shared_profile = case.create_profile()
                cls.shared = case
            self.case = cls.shared
            self.profile = cls.shared_profile

    def bank_dispatches(self, dispatches):
        """Bank immutable variants together; every envelope is still a real send."""
        c = self.case
        documents = {}
        for name, dispatch in dispatches.items():
            relative = "docs/dispatch/" + name + ".json"
            path = c.repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(dispatch, sort_keys=True, separators=(",", ":")) + "\n")
            documents[name] = relative
        c.git(c.repository, "add", *documents.values())
        c.git(c.repository, "commit", "--quiet", "-m", "authority variants")
        sha = c.git(c.repository, "rev-parse", "HEAD")
        c.git(c.repository, "update-ref", "refs/remotes/origin/main", sha)
        return {name: c.events.send("coordinator", "temporary", "fixture", sha, relative,
            "coordinate-free notice", idempotency_key=name, now=c.now)
            for name, relative in documents.items()}

    def assert_tree_unchanged(self, before):
        after = self.case.tree_bytes()
        self.assertEqual([], sorted(path for path in before.keys() | after.keys()
            if before.get(path) != after.get(path)))

    def test_public_bound_work_reaches_native_execution_admission(self):
        c = self.case
        from unittest import mock
        from floati.errors import ProtocolRefusal
        from floati.workers import WorkerRunner
        profile = self.profile
        item = c.add_bound_work(profile)
        dispatch = c.dispatch(profile, work_item_id=item["id"])
        envelope = c.envelope(dispatch)
        # Stop at the provider boundary: real CLI, profile, admission, launch
        # approval and effect preparation must all accept this CLI-created work.
        def fake_provider(runner, node, adapter, work, **kwargs):
            self.assertEqual(item["id"], work)
            self.assertEqual(profile["workspace"], item["workspace"])
            self.assertEqual(profile["id"], kwargs["native_thread_context"]["workspace_binding"]["profile_id"])
            raise ProtocolRefusal("fixture_provider_stopped", "fake provider reached")
        with mock.patch.object(WorkerRunner, "run_exact", fake_provider):
            code, artifact = c.invoke("run", "--root", str(c.root.path),
                "--as", "temporary", "--session", "fixture-parent", "--profile", profile["id"],
                "--envelope", envelope["id"], "--idempotency-key", "public-bound-run")
        self.assertEqual(20, code, artifact)
        self.assertEqual("fixture_provider_stopped", artifact["evidence"]["code"])
        admitted = [row for row in c.journal().records() if row["kind"] == "adhoc_dispatch_record"]
        self.assertEqual([item["id"]], [row["dispatch"]["work_item_id"] for row in admitted])

    def test_work_ownership_remedy_creates_exact_bound_replacement(self):
        c = self.case
        profile = self.profile
        unbound = c.work.add("unbound operator work", "temporary", [])
        envelope = c.envelope(c.dispatch(profile, work_item_id=unbound["id"]))
        before = c.tree_bytes()
        code, artifact = c.invoke("run", "--root", str(c.root.path),
            "--as", "temporary", "--session", "fixture-parent", "--profile", profile["id"],
            "--envelope", envelope["id"], "--idempotency-key", "unbound-run")
        self.assertEqual(20, code, artifact)
        self.assertEqual("adhoc_dispatch_authentication_failed", artifact["evidence"]["code"])
        self.assertIn("ownership", artifact["evidence"]["detail"])
        self.assert_tree_unchanged(before)
        self.assertIsInstance(artifact["evidence"]["remedy"], str, artifact)
        remedy = shlex.split(artifact["evidence"]["remedy"])
        self.assertEqual("floati", remedy[0])
        code, repaired = c.public(*remedy[1:])
        self.assertEqual(0, code, repaired)
        replacement = repaired["evidence"]
        self.assertEqual("temporary", replacement["owner"])
        self.assertEqual(profile["id"], replacement["adhoc_binding"]["profile_id"])
        envelope = c.envelope(c.dispatch(profile, work_item_id=replacement["id"]), name="repaired-work")
        code, artifact = c.invoke("run", "--root", str(c.root.path),
            "--as", "temporary", "--session", "fixture-parent", "--profile", profile["id"],
            "--envelope", envelope["id"], "--idempotency-key", "repaired-work")
        self.assertEqual(20, code, artifact)
        self.assertEqual("policy_missing", artifact["evidence"]["code"])
        self.assertNotIn("adhoc_binding", c.work.show(unbound["id"])[0])

    def test_authority_remedy_grants_runnable_next_coordinate(self):
        c = self.case
        from floati.planes import AuthorityGrantStore
        profile = self.profile
        store = AuthorityGrantStore(c.root)
        item = c.add_bound_work(profile, "authority remedy work")
        states = (("missing", 1), ("revoked", 2), ("expired", 2), ("foreign", 2), ("stale", 2))
        dispatches = {}
        for state, expected_epoch in states:
            dispatches[state] = c.dispatch(profile, work_item_id=item["id"],
                authority_subject="authority-" + state)
            dispatches["repaired-" + state] = c.dispatch(profile, work_item_id=item["id"],
                authority_subject="authority-" + state, authority_epoch=expected_epoch)
        envelopes = self.bank_dispatches(dispatches)
        # One scratch fleet, distinct authority subjects: immutable expired or
        # revoked epochs cannot be repaired by replaying the old coordinate.
        for state, expected_epoch in states:
            with self.subTest(state=state):
                subject = "authority-" + state
                if state != "missing":
                    when = c.now - timedelta(days=2) if state == "expired" else c.now
                    store.grant_exact(subject, "foreign" if state == "foreign" else "temporary", 1, when)
                    if state in {"revoked", "stale"}:
                        store.revoke_exact(subject, "temporary", 1, c.now)
                    if state == "stale":
                        store.grant_exact(subject, "temporary", 2, c.now)
                envelope = envelopes[state]
                before = c.tree_bytes()
                code, artifact = c.invoke("run", "--root", str(c.root.path),
                    "--as", "temporary", "--session", "fixture-parent", "--profile", profile["id"],
                    "--envelope", envelope["id"], "--idempotency-key", state)
                self.assertEqual(20, code, artifact)
                self.assertEqual("adhoc_dispatch_authentication_failed", artifact["evidence"]["code"])
                self.assertIn("authority", artifact["evidence"]["detail"])
                self.assert_tree_unchanged(before)
                self.assertIsInstance(artifact["evidence"]["remedy"], str, artifact)
                remedy = shlex.split(artifact["evidence"]["remedy"])
                self.assertEqual("floati", remedy[0])
                code, granted = c.public(*remedy[1:])
                self.assertEqual(0, code, granted)
                self.assertEqual(expected_epoch, granted["evidence"]["record"]["epoch"])
                envelope = envelopes["repaired-" + state]
                code, artifact = c.invoke("run", "--root", str(c.root.path),
                    "--as", "temporary", "--session", "fixture-parent", "--profile", profile["id"],
                    "--envelope", envelope["id"], "--idempotency-key", "repaired-" + state)
                self.assertEqual(20, code, artifact)
                self.assertEqual("policy_missing", artifact["evidence"]["code"])

    def test_bound_work_rejects_foreign_stale_and_conflicting_profiles(self):
        c = self.case
        profile = self.profile
        for owner, extra, expected in (("foreign", (), "work_owner_mismatch"),
                ("temporary", ("--workspace",), "adhoc_binding_changed")):
            with self.subTest(owner=owner, extra=extra):
                before = c.tree_bytes()
                code, artifact = c.public("work", "add", "--root", str(c.root.path),
                    "--title", "invalid binding", "--owner", owner,
                    "--adhoc-profile", profile["id"], *extra)
                self.assertEqual(20, code, artifact)
                self.assertEqual(expected, artifact["evidence"]["code"])
                self.assert_tree_unchanged(before)
        code, artifact = c.invoke("profile", "--root", str(c.root.path),
            "--as", "foreign", "--adapter", "codex", "--lane-opening",
            c.foreign_opened["record"]["id"], "--repository", "fixture", "--to", "coordinator",
            "--idempotency-key", "stale-control")
        self.assertEqual(0, code, artifact)
        stale_profile = artifact["evidence"]["profile"]
        c.add_lease("foreign")
        # The fixture's generic append does not advance the selected registry
        # index. Finish that real reader's observation before either this
        # refusal or a later shared-fleet property takes a byte snapshot.
        with c.registry.read_once(node_id="foreign"):
            pass
        before = c.tree_bytes()
        code, artifact = c.public("work", "add", "--root", str(c.root.path),
            "--title", "stale binding", "--owner", "foreign", "--adhoc-profile", stale_profile["id"])
        self.assertEqual(20, code, artifact)
        self.assertEqual("adhoc_lease_invalid", artifact["evidence"]["code"])
        self.assert_tree_unchanged(before)


class AdhocCliCompositionTests(ControllerFixture, unittest.TestCase):
    def invoke(self, verb, *arguments):
        from unittest import mock
        from floati.adhoc_threads import AdhocThreadController
        from floati.errors import ProtocolRefusal
        from floati.lane_workspaces import LaneWorkspaces
        c = self.case
        output = io.StringIO()
        teardown = AdhocThreadController.teardown
        predicate_detail = []
        def fixture_lanes(root):
            # CLI construction stays real and receives the same observer
            # coordinates as this scenario's existing controller fixture.
            observations = self.controller.profiles.lanes
            return LaneWorkspaces(root, launch_agents=observations.launch_agents,
                lsof_executable=observations.lsof_executable)
        def diagnose_teardown(controller, *args, **kwargs):
            try:
                return teardown(controller, *args, **kwargs)
            except ProtocolRefusal as exc:
                while exc is not None:
                    predicate_detail.append({"code": exc.code, "detail": exc.detail}
                        if isinstance(exc, ProtocolRefusal) else str(exc))
                    exc = exc.__cause__
                raise
        with contextlib.redirect_stdout(output), mock.patch.object(
                AdhocThreadController, "teardown", diagnose_teardown),\
                mock.patch("floati.adhoc_profiles.LaneWorkspaces", side_effect=fixture_lanes):
            code = main(["worker", "adhoc", verb, "--root", str(c.root.path), *arguments])
        artifact = json.loads(output.getvalue())
        if predicate_detail:
            artifact["test_teardown_predicate_detail"] = predicate_detail
        return code, artifact

    def test_full_cli_banked_result_replay_show_teardown_uses_same_result_envelope(self):
        from copy import deepcopy
        import subprocess
        from unittest import mock
        from floati.adhoc_threads import AdhocThreadController
        from floati.approvals import ApprovalLedger
        from floati.effects import EffectController, EffectLedger
        from floati.policy import RepositoryPolicy
        from floati.records import validate_record
        from floati.runtruth import RunLedger
        from floati.workers import WorkerRunner
        c = self.case
        arguments = ("--as", "node-a", "--session", "fixture-parent", "--profile",
            c.profile_record["id"], "--envelope", c.envelope_record["id"],
            "--idempotency-key", "node-a-dispatch")
        def prepare(controller, profile, admission):
            effects = EffectController(EffectLedger(controller.root), RunLedger(controller.root),
                RepositoryPolicy.load(c.base / "FLOATI.toml"), ApprovalLedger(controller.root))
            owner = WorkerRunner(controller.root, {}, effect_controller=effects, clock=lambda: c.now)
            def execute(*args, **kwargs):
                try:
                    return self.execution.run_exact(*args, **kwargs)
                finally:
                    owner.last_process_audit = deepcopy(self.execution.last_process_audit)
                    owner.last_native_events = deepcopy(self.execution.last_native_events)
                    owner.last_native_transport = deepcopy(self.execution.last_native_transport)
            owner.run_exact = execute
            return owner, deepcopy(c.context["effect_binding"])
        native_run = subprocess.run
        def host_observation(command, *args, **kwargs):
            # The composition's fake child has its own explicit observers.
            # An incomplete host scan must not leak into that scratch scenario.
            if command[0] in ("/usr/sbin/lsof", "/usr/bin/lsof"):
                return subprocess.CompletedProcess(command, 1, "",
                    "lsof: WARNING: incomplete host scan\n")
            return native_run(command, *args, **kwargs)
        with mock.patch.object(AdhocThreadController, "_prepare_execution", prepare),\
                mock.patch("subprocess.run", side_effect=host_observation):
            code, artifact = self.invoke("run", *arguments)
            self.assertEqual(20, code, artifact)
            self.assertEqual("adhoc_result_unbanked", artifact["evidence"]["code"])
            self.assertEqual(set(), set(self.acked()))
            self.bank_result()
            code, artifact = self.invoke("run", *arguments)
            self.assertEqual(0, code, artifact)
            first = artifact["evidence"]
            code, replay = self.invoke("run", *arguments)
            self.assertEqual(0, code, replay)
            self.assertEqual(first, replay["evidence"])
            self.assertEqual(1, self.execution.launches)
            code, shown = self.invoke("show", "--dispatch", c.admitted["dispatch_id"])
            self.assertEqual(0, code, shown)
            self.assertEqual(c.coordinate, shown["evidence"]["provider_coordinate"])
            published = self.saved("result_published")
            envelope = next(row for row in c.events.event_records()
                if row["id"] == published[0]["evidence"]["envelope_id"])
            validated = validate_record(envelope, c.root.tenant_id,
                frozenset({"message_envelope"}), integrity=False)
            self.assertEqual(c.envelope_record["id"], validated["reply_to"])
            slot = c.root.tenant_home / c.profile_record["provider_state"]
            canary = slot / "private-history.jsonl"
            canary.write_bytes(b"private history remains inactive\n")
            size = sum(p.stat().st_size for p in slot.rglob("*") if p.is_file())
            code, torn_down = self.invoke("teardown", "--as", "node-a",
                "--session", "fixture-parent", "--dispatch", c.admitted["dispatch_id"],
                "--idempotency-key", "cli-teardown")
            self.assertEqual(0, code, torn_down)
            self.assertEqual("inactive", torn_down["evidence"]["history_state"])
            self.assertEqual("closed", torn_down["evidence"]["resource_dispositions"]["workspace"])
            self.assertFalse(Path(c.profile_record["workspace"]).exists())
            self.assertTrue(torn_down["evidence"]["resource_dispositions"]["lane_closing_id"].startswith("lane-workspace-"))
            self.assertEqual(size, torn_down["evidence"]["provider_state_bytes"])
            self.assertEqual(0o700, slot.stat().st_mode & 0o777)
            self.assertEqual(b"private history remains inactive\n", canary.read_bytes())
            replacement = c.add_lease("node-a", expires=c.now + timedelta(hours=3))
            code, replay = self.invoke("teardown", "--as", "node-a",
                "--session", "fixture-parent", "--dispatch", c.admitted["dispatch_id"],
                "--idempotency-key", "cli-teardown")
            self.assertEqual(0, code, replay)
            self.assertEqual(torn_down["evidence"], replay["evidence"])
            self.assertEqual(replacement["id"], c.registry.node_lease_state("node-a", now=c.now)["node_lease_id"])
            from floati.projection import FleetProjection
            status = FleetProjection(c.root).snapshot(now=c.now)["adhoc_threads"]["profiles"][0]
            self.assertEqual("inactive_non_resumable", status["history_state"])
            self.assertEqual({"value": "unknown", "evidence_class": "unknown"}, status["provider_status"])


if __name__ == "__main__":
    unittest.main()
