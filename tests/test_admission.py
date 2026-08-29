"""RED-first behavior tests for the pure HM-3I admission surface."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot


ITEM_A = "work-018f7e9b3c117abc8def0123456789ab"
ITEM_B = "work-018f7e9b3c127abc8def0123456789ab"
ITEM_C = "work-018f7e9b3c137abc8def0123456789ab"
ITEM_D = "work-018f7e9b3c147abc8def0123456789ab"
RUN_ID = "run-018f7e9b3c157abc8def0123456789ab"
TIMESTAMP = "2026-08-08T00:00:00.000Z"


VALID_POLICY = '''schema_version = 0
capability_registry = ["review", "workspace_write"]

[limits]
max_items = 8
max_depth = 4
max_fan_out = 2
max_active_attempts = 2

[budgets.build]
unit = "attempts"
limit = 5

[worker_profiles.good]
capabilities = ["review", "workspace_write"]
cancel_mode = "native"
callback_support = true
max_concurrency = 2

[worker_profiles.unrouted]
capabilities = ["review", "workspace_write"]
cancel_mode = "native"
callback_support = true
max_concurrency = 2

[worker_profiles.uncancellable]
capabilities = ["review", "workspace_write"]
cancel_mode = "unavailable"
callback_support = true
max_concurrency = 2

[worker_profiles.callbackless]
capabilities = ["review", "workspace_write"]
cancel_mode = "native"
callback_support = false
max_concurrency = 2

[capability_selectors.review_write]
all_of = ["review", "workspace_write"]

[routing.good_route]
worker_profile = "good"
capability_selector = "review_write"
rank = 0

[routing.uncancellable_route]
worker_profile = "uncancellable"
capability_selector = "review_write"
rank = 1

[routing.callbackless_route]
worker_profile = "callbackless"
capability_selector = "review_write"
rank = 2

[retry_classes.transient]
automatic = true
[retry_classes.permanent]
automatic = false
[retry_classes.operator_required]
automatic = false
[retry_classes.policy_refusal]
automatic = false
[retry_classes.cancelled]
automatic = false
[retry_classes.unknown_effect]
automatic = false

[approval_requirements.low]
required = false
[approval_requirements.medium]
required = false
[approval_requirements.high]
required = true
[approval_requirements.critical]
required = true

[verification.unit]
argv = ["python3", "-m", "unittest", "tests.test_admission"]

[merge_gates.local]
verification_ids = ["unit"]
'''


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _reverse_objects(value: object) -> object:
    if isinstance(value, dict):
        return {key: _reverse_objects(item) for key, item in reversed(tuple(value.items()))}
    if isinstance(value, list):
        return [_reverse_objects(item) for item in value]
    return value


class AdmissionPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # A resolved temporary path avoids platform aliases such as /var -> /private/var.
        self.directory = Path(self.temp.name).resolve()
        self.plan_path = self.directory / "admission-plan.json"
        self.policy_path = self.directory / "FLOATI.toml"

    @staticmethod
    def contract(dependencies: tuple[str, ...] = ()) -> dict[str, object]:
        return {
            "objective": "admit bounded work",
            "non_goals": ["no model authority"],
            "areas_to_avoid": [{"path": "slip/graph.py", "region": "all"}],
            "input_hashes": {"brief": "a" * 64},
            "acceptance_checks": {"tests.unit": "python3 -m unittest"},
            "constraints": {"network": "dark"},
            "risk_class": "low",
            "retry_policy": {
                "max_attempts": 1,
                "backoff": {
                    "base_delay_ms": 0,
                    "cap_delay_ms": 1,
                    "strategy": "fixed",
                },
            },
            "dependencies": list(dependencies),
        }

    def item(self, item_id: str, dependencies: tuple[str, ...] = (), **changes: object) -> dict[str, object]:
        value: dict[str, object] = {
            "item_id": item_id,
            "contract": self.contract(dependencies),
            "capability_selector": "review_write",
            "requires_cancellation": True,
            "requires_callback": True,
            "workspace_key": "workspace-a",
            "concurrency_key": "concurrency-a",
            "retry_class": "transient",
            "effect_safety": "idempotent",
            "merge_gate": None,
        }
        value.update(changes)
        return value

    def valid_plan(self) -> dict[str, object]:
        return {
            "schema_version": 0,
            "workers": [{"node_id": "node-a", "worker_profile": "good"}],
            "max_active_attempts": 1,
            "budget_reservations": [{"budget_id": "build", "amount": 1}],
            "items": [self.item(ITEM_A)],
            "dependency_edges": [],
        }

    @staticmethod
    def _edge_key(edge: dict[str, object]) -> tuple[str, str, str, str]:
        return (
            str(edge["source"]),
            str(edge["target"]),
            str(edge["requires"]),
            str(edge["failure_policy"]),
        )

    def add_item(self, plan: dict[str, object], item_id: str, **changes: object) -> None:
        items = plan["items"]
        self.assertIsInstance(items, list)
        items.append(self.item(item_id, **changes))
        items.sort(key=lambda item: str(item["item_id"]))

    def add_edge(self, plan: dict[str, object], source: str, target: str) -> None:
        edges = plan["dependency_edges"]
        self.assertIsInstance(edges, list)
        edges.append(
            {
                "source": source,
                "target": target,
                "requires": "accepted",
                "failure_policy": "fail_run",
            }
        )
        edges.sort(key=self._edge_key)
        items = plan["items"]
        self.assertIsInstance(items, list)
        for item in items:
            contract = item["contract"]
            self.assertIsInstance(contract, dict)
            contract["dependencies"] = sorted(
                str(edge["source"])
                for edge in edges
                if edge["target"] == item["item_id"]
            )

    def write_plan(self, value: object) -> Path:
        self.plan_path.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return self.plan_path

    def write_policy(self, value: str = VALID_POLICY) -> Path:
        self.policy_path.write_text(value, encoding="utf-8")
        return self.policy_path

    def load(self, value: Optional[object] = None, policy: str = VALID_POLICY):
        from floati.admission import AdmissionPlan
        from floati.policy import RepositoryPolicy

        self.write_policy(policy)
        self.write_plan(self.valid_plan() if value is None else value)
        return AdmissionPlan.load(self.plan_path), RepositoryPolicy.load(self.policy_path)

    def evaluate(self, value: Optional[object] = None, policy: str = VALID_POLICY):
        from floati.admission import AdmissionEvaluator

        plan, loaded_policy = self.load(value, policy)
        return plan, loaded_policy, AdmissionEvaluator.evaluate(plan, loaded_policy)

    def test_admitted_plan_has_a_compact_immutable_machine_artifact(self) -> None:
        """Catches an evaluator that adds authority, predictions, or mutable state to a read-only report."""
        from floati.admission import AdmissionPlan

        plan, policy, artifact = self.evaluate()
        shuffled_path = self.directory / "shuffled.json"
        shuffled_path.write_text(
            json.dumps(_reverse_objects(self.valid_plan()), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        equivalent = AdmissionPlan.load(shuffled_path)

        self.assertEqual("admitted", artifact.outcome)
        self.assertEqual(plan.canonical_bytes(), equivalent.canonical_bytes())
        self.assertEqual(plan.digest, equivalent.digest)
        self.assertEqual(plan.digest, artifact.plan_digest)
        self.assertEqual(policy.digest, artifact.policy_digest)
        machine = artifact.machine()
        self.assertEqual(
            {
                "admission_schema_version",
                "kind",
                "outcome",
                "plan_digest",
                "policy_digest",
                "reasons",
            },
            set(machine),
        )
        self.assertEqual(0, machine["admission_schema_version"])
        self.assertEqual("plan_admission", machine["kind"])
        self.assertEqual([], machine["reasons"])
        rendered = json.dumps(machine, sort_keys=True)
        for forbidden in (
            "timestamp", "run_id", "lease", "claim", "session", "success", "runtime",
            "cost", "quality", "score", "prediction", "estimate",
        ):
            self.assertNotIn(forbidden, rendered)
        with self.assertRaises((dataclasses.FrozenInstanceError, AttributeError)):
            artifact.outcome = "refused"  # type: ignore[misc]

    def test_each_governed_category_returns_a_deterministic_typed_reason(self) -> None:
        """Catches omitted graph, routing, safety, resource, and external-gate admission checks."""
        cases: list[tuple[str, dict[str, object], str, str, str]] = []

        cycle = self.valid_plan()
        self.add_item(cycle, ITEM_B, workspace_key="workspace-b", concurrency_key="concurrency-b")
        self.add_edge(cycle, ITEM_A, ITEM_B)
        self.add_edge(cycle, ITEM_B, ITEM_A)
        cases.append(("cycle", cycle, VALID_POLICY, "refused", "graph"))

        fan_out = self.valid_plan()
        self.add_item(fan_out, ITEM_B, workspace_key="workspace-b", concurrency_key="concurrency-b")
        self.add_item(fan_out, ITEM_C, workspace_key="workspace-c", concurrency_key="concurrency-c")
        self.add_edge(fan_out, ITEM_A, ITEM_B)
        self.add_edge(fan_out, ITEM_A, ITEM_C)
        cases.append(("fan_out", fan_out, VALID_POLICY.replace("max_fan_out = 2", "max_fan_out = 1"), "refused", "fan_out"))

        capability = self.valid_plan()
        capability["items"][0]["capability_selector"] = "missing"  # type: ignore[index]
        cases.append(("capability", capability, VALID_POLICY, "refused", "capability"))

        no_route = self.valid_plan()
        no_route["workers"][0]["worker_profile"] = "unrouted"  # type: ignore[index]
        cases.append(("no_routed_profile", no_route, VALID_POLICY, "refused", "capability"))

        cancellation = self.valid_plan()
        cancellation["workers"][0]["worker_profile"] = "uncancellable"  # type: ignore[index]
        cases.append(("cancellation", cancellation, VALID_POLICY, "refused", "cancellation"))

        callback = self.valid_plan()
        callback["workers"][0]["worker_profile"] = "callbackless"  # type: ignore[index]
        cases.append(("callback", callback, VALID_POLICY, "refused", "callback"))

        workspace = self.valid_plan()
        self.add_item(workspace, ITEM_B, workspace_key="workspace-a", concurrency_key="concurrency-b")
        cases.append(("workspace", workspace, VALID_POLICY, "refused", "workspace"))

        concurrency = self.valid_plan()
        self.add_item(concurrency, ITEM_B, workspace_key="workspace-b", concurrency_key="concurrency-a")
        cases.append(("concurrency_key", concurrency, VALID_POLICY, "refused", "concurrency"))

        capacity = self.valid_plan()
        capacity["max_active_attempts"] = 3
        cases.append(("requested_concurrency", capacity, VALID_POLICY, "refused", "concurrency"))

        retry = self.valid_plan()
        retry["items"][0]["effect_safety"] = "unknown_effect"  # type: ignore[index]
        retry["items"][0]["contract"]["retry_policy"]["max_attempts"] = 2  # type: ignore[index]
        cases.append(("retry", retry, VALID_POLICY, "refused", "retry"))

        budget = self.valid_plan()
        budget["budget_reservations"][0]["amount"] = 6  # type: ignore[index]
        cases.append(("budget", budget, VALID_POLICY, "refused", "budget"))

        approval = self.valid_plan()
        approval["items"][0]["contract"]["risk_class"] = "high"  # type: ignore[index]
        cases.append(("operator", approval, VALID_POLICY, "needs_operator", "operator"))

        merge = self.valid_plan()
        merge["items"][0]["merge_gate"] = "local"  # type: ignore[index]
        cases.append(("merge", merge, VALID_POLICY, "needs_operator", "merge"))

        missing_merge = self.valid_plan()
        missing_merge["items"][0]["merge_gate"] = "missing"  # type: ignore[index]
        cases.append(("invalid_merge", missing_merge, VALID_POLICY, "refused", "merge"))

        for name, value, policy, outcome, category in cases:
            with self.subTest(name=name):
                _plan, _policy, artifact = self.evaluate(value, policy)
                self.assertEqual(outcome, artifact.outcome)
                self.assertIn(category, [reason["category"] for reason in artifact.machine()["reasons"]])

    def test_graph_invariants_cover_all_declared_member_edge_and_depth_rules(self) -> None:
        """Catches evaluator shortcuts that check only cycles while accepting malformed graph semantics."""
        cases: list[tuple[str, dict[str, object], str]] = []

        item_limit = self.valid_plan()
        self.add_item(item_limit, ITEM_B, workspace_key="workspace-b", concurrency_key="concurrency-b")
        cases.append(("item_limit", item_limit, VALID_POLICY.replace("max_items = 8", "max_items = 1")))

        depth = self.valid_plan()
        self.add_item(depth, ITEM_B, workspace_key="workspace-b", concurrency_key="concurrency-b")
        self.add_item(depth, ITEM_C, workspace_key="workspace-c", concurrency_key="concurrency-c")
        self.add_item(depth, ITEM_D, workspace_key="workspace-d", concurrency_key="concurrency-d")
        self.add_edge(depth, ITEM_A, ITEM_B)
        self.add_edge(depth, ITEM_B, ITEM_C)
        self.add_edge(depth, ITEM_C, ITEM_D)
        cases.append(("depth", depth, VALID_POLICY.replace("max_depth = 4", "max_depth = 3")))

        duplicate_member = self.valid_plan()
        duplicate_member["items"].append(self.item(ITEM_A))  # type: ignore[index]
        duplicate_member["items"].sort(key=lambda item: str(item["item_id"]))  # type: ignore[index]
        cases.append(("duplicate_member", duplicate_member, VALID_POLICY))

        duplicate_edge = self.valid_plan()
        self.add_item(duplicate_edge, ITEM_B, workspace_key="workspace-b", concurrency_key="concurrency-b")
        self.add_edge(duplicate_edge, ITEM_A, ITEM_B)
        duplicate_edge["dependency_edges"].append(dict(duplicate_edge["dependency_edges"][0]))  # type: ignore[index]
        duplicate_edge["dependency_edges"].sort(key=self._edge_key)  # type: ignore[index]
        cases.append(("duplicate_edge", duplicate_edge, VALID_POLICY))

        self_edge = self.valid_plan()
        self_edge["dependency_edges"].append({
            "source": ITEM_A,
            "target": ITEM_A,
            "requires": "accepted",
            "failure_policy": "fail_run",
        })  # type: ignore[index]
        cases.append(("self_edge", self_edge, VALID_POLICY))

        unknown_endpoint = self.valid_plan()
        unknown_endpoint["dependency_edges"].append({
            "source": ITEM_A,
            "target": ITEM_B,
            "requires": "accepted",
            "failure_policy": "fail_run",
        })  # type: ignore[index]
        cases.append(("unknown_endpoint", unknown_endpoint, VALID_POLICY))

        contract_drift = self.valid_plan()
        self.add_item(contract_drift, ITEM_B, workspace_key="workspace-b", concurrency_key="concurrency-b")
        contract_drift["dependency_edges"].append({
            "source": ITEM_A,
            "target": ITEM_B,
            "requires": "accepted",
            "failure_policy": "fail_run",
        })  # type: ignore[index]
        cases.append(("contract_drift", contract_drift, VALID_POLICY))

        for name, value, policy in cases:
            with self.subTest(name=name):
                _plan, _policy, artifact = self.evaluate(value, policy)
                self.assertEqual("refused", artifact.outcome)
                self.assertIn("graph", [reason["category"] for reason in artifact.machine()["reasons"]])

    def test_hard_refusals_precede_external_gates_and_reason_order_is_stable(self) -> None:
        """Catches an operator result that masks a hard-invalid plan or nondeterministic reason ordering."""
        value = self.valid_plan()
        self.add_item(value, ITEM_B, workspace_key="workspace-b", concurrency_key="concurrency-b")
        self.add_edge(value, ITEM_A, ITEM_B)
        self.add_edge(value, ITEM_B, ITEM_A)
        value["budget_reservations"][0]["amount"] = 6  # type: ignore[index]
        value["items"][0]["contract"]["risk_class"] = "high"  # type: ignore[index]
        value["items"][1]["merge_gate"] = "missing"  # type: ignore[index]

        _plan, _policy, artifact = self.evaluate(value)
        reasons = artifact.machine()["reasons"]
        categories = [reason["category"] for reason in reasons]

        self.assertEqual("refused", artifact.outcome)
        self.assertEqual(["graph", "budget", "operator", "merge"], categories)
        self.assertEqual(reasons, sorted(reasons, key=lambda reason: (
            ("graph", "fan_out", "capability", "cancellation", "callback", "workspace", "concurrency", "retry", "budget", "operator", "merge").index(reason["category"]),
            reason["code"],
            reason.get("subject", ""),
            json.dumps({key: value for key, value in reason.items() if key not in {"category", "code", "subject"}}, sort_keys=True, separators=(",", ":")),
        )))

    def test_loader_refuses_hostile_noncanonical_or_malformed_inputs_as_typed_refusals(self) -> None:
        """Catches raw JSON, path, symlink, UTF-8, and shape failures escaping the input boundary."""
        from floati.admission import AdmissionPlan

        self.write_policy()
        invalid_uuid = self.valid_plan()
        invalid_uuid["items"][0]["item_id"] = "work-not-a-uuid"
        unknown = self.valid_plan()
        unknown["unexpected"] = True
        nested_unknown = self.valid_plan()
        nested_unknown["items"][0]["contract"]["unexpected"] = True  # type: ignore[index]
        unsorted_workers = self.valid_plan()
        unsorted_workers["workers"] = [
            {"node_id": "node-b", "worker_profile": "good"},
            {"node_id": "node-a", "worker_profile": "good"},
        ]
        cases: list[tuple[str, object, bytes | None]] = [
            ("unknown", unknown, None),
            ("nested_unknown", nested_unknown, None),
            ("unsorted_workers", unsorted_workers, None),
            ("invalid_uuid", invalid_uuid, None),
            ("duplicate_keys", None, b'{"schema_version":0,"schema_version":0}'),
            ("not_utf8", None, b"\xff"),
            ("oversize", None, b" " * (64 * 1024 + 1)),
        ]
        for name, value, raw in cases:
            with self.subTest(name=name):
                if raw is None:
                    self.write_plan(value)
                else:
                    self.plan_path.write_bytes(raw)
                with self.assertRaises(ProtocolRefusal):
                    AdmissionPlan.load(self.plan_path)

        duplicate_nested = json.dumps(self.valid_plan(), separators=(",", ":"))
        duplicate_nested = duplicate_nested.replace(
            '"objective":"admit bounded work"',
            '"objective":"admit bounded work","objective":"duplicate"',
            1,
        )
        self.plan_path.write_text(duplicate_nested, encoding="utf-8")
        with self.assertRaises(ProtocolRefusal):
            AdmissionPlan.load(self.plan_path)

        with self.assertRaises(ProtocolRefusal):
            AdmissionPlan.load("relative-admission.json")
        with self.assertRaises(ProtocolRefusal):
            AdmissionPlan.load(str(self.directory) + "/./admission-plan.json")

        class ExplodingPath:
            def __fspath__(self) -> str:
                raise RuntimeError("pathlike boom")

        with self.assertRaises(ProtocolRefusal):
            AdmissionPlan.load(ExplodingPath())
        if hasattr(os, "symlink"):
            self.write_plan(self.valid_plan())
            linked = self.directory / "linked-plan.json"
            os.symlink(self.plan_path, linked)
            with self.assertRaises(ProtocolRefusal):
                AdmissionPlan.load(linked)
            parent_link = self.directory / "linked-parent"
            os.symlink(self.directory, parent_link)
            with self.assertRaises(ProtocolRefusal):
                AdmissionPlan.load(parent_link / "admission-plan.json")
        nonregular = self.directory / "directory-plan.json"
        nonregular.mkdir()
        with self.assertRaises(ProtocolRefusal):
            AdmissionPlan.load(nonregular)
        with self.assertRaises(ProtocolRefusal):
            AdmissionPlan.load(self.directory / "missing-plan.json")

    def test_each_mutable_governed_plan_field_changes_the_canonical_digest(self) -> None:
        """Catches a digest that omits an admission-relevant input field."""
        from floati.admission import AdmissionPlan

        baseline = self.valid_plan()
        self.write_plan(baseline)
        baseline_digest = AdmissionPlan.load(self.plan_path).digest

        def changed(mutator: object) -> dict[str, object]:
            value = json.loads(json.dumps(baseline))
            mutator(value)  # type: ignore[operator]
            return value

        cases = {
            "worker_node": changed(lambda value: value["workers"][0].update(node_id="node-b")),
            "worker_profile": changed(lambda value: value["workers"][0].update(worker_profile="callbackless")),
            "max_active_attempts": changed(lambda value: value.update(max_active_attempts=2)),
            "budget_id": changed(lambda value: value["budget_reservations"][0].update(budget_id="other")),
            "budget_amount": changed(lambda value: value["budget_reservations"][0].update(amount=2)),
            "item_id": changed(lambda value: value["items"][0].update(item_id=ITEM_B)),
            "objective": changed(lambda value: value["items"][0]["contract"].update(objective="other objective")),
            "non_goals": changed(lambda value: value["items"][0]["contract"].update(non_goals=["different non-goal"])),
            "areas_to_avoid": changed(lambda value: value["items"][0]["contract"].update(areas_to_avoid=[{"path": "slip/other.py", "region": "all"}])),
            "areas_to_avoid_region": changed(lambda value: value["items"][0]["contract"].update(areas_to_avoid=[{"path": "slip/graph.py", "region": "part"}])),
            "input_hashes": changed(lambda value: value["items"][0]["contract"].update(input_hashes={"brief": "b" * 64})),
            "acceptance_checks": changed(lambda value: value["items"][0]["contract"].update(acceptance_checks={"tests.other": "python3 -m unittest"})),
            "constraints": changed(lambda value: value["items"][0]["contract"].update(constraints={"network": "offline"})),
            "risk_class": changed(lambda value: value["items"][0]["contract"].update(risk_class="medium")),
            "retry_policy": changed(lambda value: value["items"][0]["contract"]["retry_policy"].update(max_attempts=2)),
            "retry_backoff_base": changed(lambda value: value["items"][0]["contract"]["retry_policy"]["backoff"].update(base_delay_ms=1)),
            "retry_backoff_cap": changed(lambda value: value["items"][0]["contract"]["retry_policy"]["backoff"].update(cap_delay_ms=2)),
            "retry_backoff_strategy": changed(lambda value: value["items"][0]["contract"]["retry_policy"]["backoff"].update(strategy="exponential")),
            "dependencies": changed(lambda value: value["items"][0]["contract"].update(dependencies=[ITEM_B])),
            "capability_selector": changed(lambda value: value["items"][0].update(capability_selector="missing")),
            "requires_cancellation": changed(lambda value: value["items"][0].update(requires_cancellation=False)),
            "requires_callback": changed(lambda value: value["items"][0].update(requires_callback=False)),
            "workspace_key": changed(lambda value: value["items"][0].update(workspace_key="workspace-b")),
            "concurrency_key": changed(lambda value: value["items"][0].update(concurrency_key="concurrency-b")),
            "retry_class": changed(lambda value: value["items"][0].update(retry_class="permanent")),
            "effect_safety": changed(lambda value: value["items"][0].update(effect_safety="non_idempotent")),
            "merge_gate": changed(lambda value: value["items"][0].update(merge_gate="local")),
        }
        edge = self.valid_plan()
        self.add_item(edge, ITEM_B, dependencies=(ITEM_A,), workspace_key="workspace-b", concurrency_key="concurrency-b")
        self.add_edge(edge, ITEM_A, ITEM_B)
        cases["dependency_edges"] = edge

        for name, value in cases.items():
            with self.subTest(name=name):
                self.write_plan(value)
                self.assertNotEqual(baseline_digest, AdmissionPlan.load(self.plan_path).digest)

    def test_evaluation_is_side_effect_free(self) -> None:
        """Catches admission that writes a root, launches an adapter, or executes verification argv."""
        from floati.admission import AdmissionEvaluator

        plan, policy = self.load()
        before = _digest_tree(self.directory)
        with patch("floati.runtruth.RunLedger.append", side_effect=AssertionError("append")), patch(
            "floati.work.WorkLog.add", side_effect=AssertionError("work writer")
        ), patch("floati.workers.WorkerRunner.run", side_effect=AssertionError("worker runner")), patch(
            "subprocess.Popen", side_effect=AssertionError("process")
        ), patch("multiprocessing.Process", side_effect=AssertionError("process")):
            artifact = AdmissionEvaluator.evaluate(plan, policy)
        after = _digest_tree(self.directory)

        self.assertEqual("admitted", artifact.outcome)
        self.assertEqual(before, after)

    def test_current_admission_rejects_tampered_stale_or_nonadmitted_artifacts(self) -> None:
        """Catches a run-creation gate that trusts a caller-provided or stale admission result."""
        from floati.admission import AdmissionEvaluator

        plan, policy, admitted = self.evaluate()
        self.assertIsNone(AdmissionEvaluator.require_current_admission(plan, policy, admitted))
        tampered = dataclasses.replace(admitted, outcome="refused")
        mismatch = dataclasses.replace(admitted, plan_digest="0" * 64)
        high = self.valid_plan()
        high["items"][0]["contract"]["risk_class"] = "high"  # type: ignore[index]
        high_plan, high_policy, pending = self.evaluate(high)
        refused_value = self.valid_plan()
        refused_value["budget_reservations"][0]["amount"] = 6  # type: ignore[index]
        refused_plan, refused_policy, refused = self.evaluate(refused_value)

        for label, args in (
            ("tampered", (plan, policy, tampered)),
            ("mismatch", (plan, policy, mismatch)),
            ("refused", (refused_plan, refused_policy, refused)),
            ("needs_operator", (high_plan, high_policy, pending)),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ProtocolRefusal):
                    AdmissionEvaluator.require_current_admission(*args)

        # Public admission APIs keep hostile direct callers in the typed
        # refusal boundary too; only loaded immutable values are accepted.
        for label, args in (
            ("raw_plan", (object(), policy, admitted)),
            ("raw_policy", (plan, object(), admitted)),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ProtocolRefusal):
                    AdmissionEvaluator.require_current_admission(*args)  # type: ignore[arg-type]

    def test_evaluator_rejects_mutated_plan_and_policy_values_with_stale_cached_digests(self) -> None:
        """Catches an evaluator that trusts mutable public fields while reporting old plan or policy digests."""
        from floati.admission import AdmissionArtifact, AdmissionEvaluator

        high = self.valid_plan()
        high["items"][0]["contract"]["risk_class"] = "high"  # type: ignore[index]
        policy_plan, policy = self.load(high)
        approval_requirements = dict(policy.approval_requirements)
        approval_requirements["high"] = False
        forged_policy = dataclasses.replace(policy, approval_requirements=approval_requirements)

        plan_for_mutation, policy_for_plan = self.load(high)
        changed_contract = dataclasses.replace(plan_for_mutation.items[0].contract, risk_class="low")
        changed_item = dataclasses.replace(plan_for_mutation.items[0], contract=changed_contract)
        forged_plan = plan_for_mutation
        object.__setattr__(forged_plan, "items", (changed_item,))

        route_plan, route_policy = self.load()
        reordered_routes = dataclasses.replace(route_policy, routes=tuple(reversed(route_policy.routes)))

        for label, candidate_plan, candidate_policy, artifact in (
            (
                "approval_requirements",
                policy_plan,
                forged_policy,
                AdmissionArtifact("admitted", policy_plan.digest, forged_policy.digest),
            ),
            (
                "plan_items",
                forged_plan,
                policy_for_plan,
                AdmissionArtifact("admitted", forged_plan.digest, policy_for_plan.digest),
            ),
            (
                "routes",
                route_plan,
                reordered_routes,
                AdmissionArtifact("admitted", route_plan.digest, reordered_routes.digest),
            ),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ProtocolRefusal):
                    AdmissionEvaluator.evaluate(candidate_plan, candidate_policy)
                with self.assertRaises(ProtocolRefusal):
                    AdmissionEvaluator.require_current_admission(candidate_plan, candidate_policy, artifact)

    def test_public_artifact_and_reason_reject_unhashable_closed_fields_as_typed_refusals(self) -> None:
        """Catches raw TypeError escapes from list or dict values in public immutable admission objects."""
        from floati.admission import AdmissionArtifact, AdmissionReason

        for value in ([], {}):
            with self.subTest(object="artifact", value_type=type(value).__name__):
                with self.assertRaises(ProtocolRefusal):
                    AdmissionArtifact(value, "a" * 64, "b" * 64)  # type: ignore[arg-type]
            with self.subTest(object="reason", value_type=type(value).__name__):
                with self.assertRaises(ProtocolRefusal):
                    AdmissionReason(value, "reason_code")  # type: ignore[arg-type]

    def test_guarded_run_append_checks_current_admission_before_run_created(self) -> None:
        """Catches a durable run frame emitted before the narrow in-process admission gate."""
        from floati.admission import AdmissionEvaluator
        from floati.orchestrate import append_admitted_run
        from floati.runtruth import RunLedger

        plan, policy, artifact = self.evaluate()
        root = FloatiRoot.open_direct_home(self.directory / "fleet", create=True)
        ledger = RunLedger(root)
        events: list[str] = []
        original_gate = AdmissionEvaluator.require_current_admission
        original_append = ledger.append

        def gate(*args: object) -> None:
            events.append("gate")
            original_gate(*args)  # type: ignore[arg-type]

        def append(record: dict[str, object]) -> dict[str, object]:
            events.append(str(record["kind"]))
            return original_append(record)

        with patch.object(AdmissionEvaluator, "require_current_admission", side_effect=gate), patch.object(
            ledger, "append", side_effect=append
        ):
            created, bound = append_admitted_run(
                ledger, plan, policy, artifact, run_id=RUN_ID, timestamp=TIMESTAMP
            )

        self.assertEqual(["gate", "run_created", "run_policy_bound"], events)
        self.assertEqual(artifact.plan_digest, created["plan_digest"])
        self.assertEqual(artifact.policy_digest, created.get("policy_digest"))
        self.assertEqual(artifact.policy_digest, bound["policy_digest"])
        persisted = ledger.records()
        self.assertEqual(["run_created", "run_policy_bound"], [row["kind"] for row in persisted])
        self.assertEqual(artifact.policy_digest, persisted[0].get("policy_digest"))
        self.assertEqual(
            {
                "status": "bound",
                "plan_digest": artifact.plan_digest,
                "policy_digest": artifact.policy_digest,
            },
            ledger.project().run(RUN_ID).get("admitted_pair_proof"),
        )

        denied = RunLedger(FloatiRoot.open_direct_home(self.directory / "denied", create=True))
        with patch.object(AdmissionEvaluator, "require_current_admission", side_effect=ProtocolRefusal("admission_required", "denied")):
            with self.assertRaises(ProtocolRefusal):
                append_admitted_run(denied, plan, policy, artifact, run_id=RUN_ID, timestamp=TIMESTAMP)
        self.assertEqual([], denied.records())


if __name__ == "__main__":
    unittest.main()
