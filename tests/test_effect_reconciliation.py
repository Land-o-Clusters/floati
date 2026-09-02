from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest import mock

import floati.effects as effects_module
from floati.approvals import ApprovalLedger
from floati.effect_reconciliation_protocol import (
    ReconciliationRequest,
    ReconciliationResult,
    build_request,
    build_result,
)
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.effects import EffectController, EffectLedger
from tests.test_effect_controller import NOW, _EffectCase


_GIT = "/usr/bin/git"


class _GitFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repository = self.base / "repository"
        self.repository.mkdir()
        self.git("init", "--quiet", "--object-format=sha256", "--initial-branch=main")
        (self.repository / "README.md").write_text("effect reconciliation\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git(
            "-c", "user.name=Floati Tests",
            "-c", "user.email=tests@floati.invalid",
            "commit", "--quiet", "-m", "fixture",
        )
        self.sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(64, len(self.sha), "the positive control must use a full SHA-256 object id")
        self.identity_digest = self.repository_identity(self.repository)

    def git(self, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        environment = {
            "HOME": str(self.base / "git-home"),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        return subprocess.run(
            [_GIT, *arguments],
            cwd=self.repository if cwd is None else cwd,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )

    @staticmethod
    def repository_identity(repository: Path) -> str:
        metadata = repository.stat()
        payload = {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "path": str(repository.resolve(strict=True)),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def local_target(self, **changes: object) -> dict[str, object]:
        target: dict[str, object] = {
            "kind": "git_ref",
            "coordinate": str(self.repository.resolve(strict=True)),
            "identity_digest": self.identity_digest,
        }
        target.update(changes)
        return target

    def remote_target(self, coordinate: str | None = None, **changes: object) -> dict[str, object]:
        selected = str(self.repository) if coordinate is None else coordinate
        target: dict[str, object] = {
            "kind": "git_remote_ref",
            "coordinate": selected,
            "identity_digest": hashlib.sha256(selected.encode("utf-8")).hexdigest(),
        }
        target.update(changes)
        return target

    def expected(self, *, kind: str = "git_ref_equals", locator: str = "refs/heads/main", digest: str | None = None) -> dict[str, object]:
        return {
            "kind": kind,
            "locator": locator,
            "expected_digest": self.sha if digest is None else digest,
        }


class EffectControllerReconciliationTests(_GitFixture):
    def _case_with_unknown(self, *, adapter: str = "git_local"):
        case = _EffectCase(self)
        target: dict[str, object]
        effect_type = "git_ref_update"
        expected_kind = "git_ref_equals"
        if adapter == "git_local":
            target = self.local_target()
        elif adapter == "git_remote_explicit":
            coordinate = str(self.repository.resolve(strict=True))
            target = self.remote_target(coordinate)
            effect_type = "git_remote_ref_update"
            expected_kind = "git_remote_ref_equals"
        elif adapter == "github_explicit":
            target = {
                "kind": "github_resource", "coordinate": "owner/repo#1",
                "identity_digest": "a" * 64,
            }
            effect_type = "github_mutation"
            expected_kind = "github_idempotency_marker"
        else:
            target = {
                "kind": "shell_environment", "coordinate": "workspace",
                "identity_digest": "a" * 64,
            }
            effect_type = "shell_command"
            expected_kind = "none"
        intent = case.controller.intent(**case.intent_args(
            effect_type=effect_type,
            target=target,
            expected_confirmation=self.expected(kind=expected_kind),
            reconciliation_adapter=adapter,
        ))
        case.controller.dispatched(
            intent["operation_id"], dispatch_adapter=(
                adapter if adapter != "none" else "shell_explicit"
            ), dispatch_evidence_digest="d" * 64,
            now=NOW + timedelta(seconds=23),
        )
        case.controller.unknown(
            intent["operation_id"], reason_code="confirmation_absent",
            evidence_digest="e" * 64, spend_status="unknown",
            now=NOW + timedelta(seconds=24),
        )
        return case, intent

    def test_controller_selects_only_frozen_reconciliation_adapter(self) -> None:
        """Catches caller-selected adapters or a reconciler result bypassing frozen intent."""
        cases = (
            ("git_local", "reconciled_unknown"),
            ("git_remote_explicit", "reconciled_unknown"),
            ("github_explicit", "reconciled_unknown"),
            ("none", "reconciled_unknown"),
        )
        for adapter, state in cases:
            with self.subTest(adapter=adapter):
                case, intent = self._case_with_unknown(adapter=adapter)
                row = case.controller.reconcile(
                    intent["operation_id"], now=NOW + timedelta(seconds=25),
                )
                self.assertEqual("effect_reconciled", row["kind"])
                self.assertEqual(adapter, row["reconciliation_adapter"])
                self.assertEqual(
                    state,
                    case.effect_ledger.project().operation(intent["operation_id"])["state"],
                )

    def test_exact_git_reconciliation_without_measurement_blocks_acceptance(self) -> None:
        """Catches Git state observation authorizing success without measured spend."""
        from tests.test_runtruth import EffectAcceptanceTests

        for adapter in ("git_local", "git_remote_explicit"):
            with self.subTest(adapter=adapter):
                case, intent = self._case_with_unknown(adapter=adapter)
                reconciled = case.controller.reconcile(
                    intent["operation_id"], now=NOW + timedelta(seconds=25),
                )
                self.assertEqual("unknown", reconciled["reconciled_outcome"])
                self.assertIsNone(reconciled["confirmation"])
                self.assertEqual("unknown", reconciled["spend_status"])
                self.assertIsNone(reconciled["measured_spend"])
                evidence = case.effect_ledger.project().acceptance_evidence(
                    case.run.run_id, case.opened["attempt_id"],
                )
                self.assertTrue(evidence.blockers)
                candidate = EffectAcceptanceTests._bound(
                    case.result_acceptance_candidate(), evidence,
                )
                with self.assertRaises(ProtocolRefusal) as caught:
                    case.run_ledger.append(candidate)
                self.assertEqual(
                    "effect_unknown_blocks_acceptance", caught.exception.code,
                )

    def test_controller_reconciliation_requires_current_negative_evidence(self) -> None:
        """Catches intent, dispatch, acknowledgement, or confirmed state being reconciled."""
        case = _EffectCase(self)
        intent = case.controller.intent(**case.intent_args())
        with self.assertRaises(ProtocolRefusal) as at_intent:
            case.controller.reconcile(intent["operation_id"])
        self.assertEqual("effect_transition_invalid", at_intent.exception.code)

    def _assert_reconciliation_unchanged(
        self, case: _EffectCase, intent: dict[str, object],
        before: list[dict[str, object]], state: object,
    ) -> None:
        self.assertEqual(before, case.effect_ledger.records())
        self.assertEqual(
            state,
            case.effect_ledger.project().operation(intent["operation_id"])["state"],
        )

    @staticmethod
    def _confirmed(request: ReconciliationRequest) -> ReconciliationResult:
        return build_result(
            request,
            outcome="confirmed",
            reason_code="exact_ref_and_object",
            observation={
                "observed_ref_digest": request.expected_confirmation["expected_digest"],
            },
            confirmation=request.expected_confirmation,
            spend_status="complete",
            measured_spend=request.budget_claim,
        )

    @staticmethod
    def _unknown(request: ReconciliationRequest) -> ReconciliationResult:
        return build_result(
            request,
            outcome="unknown",
            reason_code="git_observation_unavailable",
        )

    def _launcher_patch(self, side_effect):
        return mock.patch.object(
            effects_module,
            "observe_effect_reconciliation",
            side_effect=side_effect,
            create=True,
        )

    def test_controller_reconciliation_has_no_in_process_adapter_or_result_binding(self) -> None:
        """Catches the cutover retaining any old mutable adapter authority."""
        old_names = (
            "_GIT_" + "LOCAL_RECONCILER",
            "_GIT_" + "REMOTE_RECONCILER",
            "_UNAVAILABLE_" + "RECONCILER",
            "_RECONCILIATION_" + "RESULT",
        )
        for name in old_names:
            self.assertFalse(hasattr(effects_module, name), name)
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("floati.effect_" + "reconciliation")

    def test_old_closure_cell_and_live_binding_attack_cannot_reach_append_path(self) -> None:
        """Catches the dfabd7e wrapper closure restoring in-process append authority."""
        case, intent = self._case_with_unknown(adapter="git_local")
        legacy_called: list[str] = []
        method = EffectController.reconcile
        cells = dict(zip(method.__code__.co_freevars, method.__closure__ or ()))
        original_cell = cells.get("original")
        saved_cell = None if original_cell is None else original_cell.cell_contents
        names = (
            "_GIT_" + "LOCAL_RECONCILER",
            "_GIT_" + "REMOTE_RECONCILER",
            "_UNAVAILABLE_" + "RECONCILER",
            "_RECONCILIATION_" + "RESULT",
        )
        saved_bindings = {
            name: getattr(effects_module, name)
            for name in names
            if hasattr(effects_module, name)
        }

        if original_cell is not None:
            legacy_module = importlib.import_module("floati.effect_" + "reconciliation")
            legacy_result = getattr(
                legacy_module, "Reconciliation" + "Result",
            )

            class HostileAdapter:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    legacy_called.append("constructed")

                def reconcile(self, **_kwargs: object) -> object:
                    legacy_called.append("reconciled")
                    return legacy_result(
                        "confirmed", "f" * 64,
                        dict(intent["expected_confirmation"]),
                        "complete", {"build": 1},
                    )

            forged = (
                HostileAdapter, HostileAdapter, HostileAdapter, legacy_result,
                HostileAdapter.reconcile, HostileAdapter.reconcile,
                HostileAdapter.reconcile,
            )
            original_cell.cell_contents = forged
            for name, value in zip(names[:3], forged[:3]):
                setattr(effects_module, name, value)
            setattr(effects_module, names[3], legacy_result)

        try:
            with self._launcher_patch(self._confirmed) as launcher:
                row = case.controller.reconcile(intent["operation_id"])
        finally:
            if original_cell is not None:
                original_cell.cell_contents = saved_cell
            for name in names:
                if name in saved_bindings:
                    setattr(effects_module, name, saved_bindings[name])
                elif hasattr(effects_module, name):
                    delattr(effects_module, name)
        self.assertEqual([], legacy_called)
        self.assertEqual(1, launcher.call_count)
        self.assertEqual("confirmed", row["reconciled_outcome"])

    def test_controller_passes_only_closed_request_to_fresh_exec_launcher(self) -> None:
        """Catches ledger/root objects or live projection maps crossing the exec boundary."""
        case, intent = self._case_with_unknown(adapter="git_local")
        captured: list[ReconciliationRequest] = []

        def launcher(request: ReconciliationRequest) -> ReconciliationResult:
            captured.append(request)
            return self._confirmed(request)

        with self._launcher_patch(launcher):
            row = case.controller.reconcile(intent["operation_id"])
        self.assertEqual(1, len(captured))
        request = captured[0]
        self.assertIs(type(request), ReconciliationRequest)
        self.assertIs(type(request.target), dict)
        self.assertIs(type(request.expected_confirmation), dict)
        self.assertIs(type(request.budget_claim), dict)
        self.assertEqual(intent["operation_id"], request.operation_id)
        self.assertEqual(row["prior_effect_evidence_id"], request.current_evidence_id)
        self.assertEqual(intent["reconciliation_adapter"], request.adapter)

    def test_controller_refreshes_all_request_bound_truth_inside_append_transaction(self) -> None:
        """Catches any stale post-observation binding becoming durable truth."""
        mutations = (
            ("adapter", lambda operation: operation.__setitem__("reconciliation_adapter", "none")),
            ("target", lambda operation: operation["target"].__setitem__("identity_digest", "9" * 64)),
            ("confirmation", lambda operation: operation["expected_confirmation"].__setitem__("expected_digest", "8" * 64)),
            ("budget", lambda operation: operation.__setitem__(
                "budget_claim", [{"budget_id": "build", "amount": 0}],
            )),
            ("intent_id", lambda operation: operation.__setitem__("intent_id", "effect-intent-018f7e9b3c117abc8def0123456789ab")),
            ("current_evidence_id", lambda operation: operation.__setitem__("current_evidence_id", "effect-unknown-018f7e9b3c117abc8def0123456789ab")),
        )
        for name, mutate in mutations:
            with self.subTest(field=name):
                case, intent = self._case_with_unknown(adapter="git_local")
                before = case.effect_ledger.records()
                calls = 0
                original = effects_module.EffectProjection.from_records

                def projected(records, *, integrity=True):
                    nonlocal calls
                    calls += 1
                    projection = original(records, integrity=integrity)
                    if calls >= 3:
                        mutate(projection._operations[intent["operation_id"]])
                    return projection

                with mock.patch.object(
                    effects_module.EffectProjection,
                    "from_records",
                    side_effect=projected,
                ), self._launcher_patch(self._confirmed):
                    with self.assertRaises(ProtocolRefusal) as caught:
                        case.controller.reconcile(intent["operation_id"])
                self.assertEqual("effect_reconciliation_stale", caught.exception.code)
                self.assertGreaterEqual(calls, 2)
                self.assertEqual(before, case.effect_ledger.records())

    def test_controller_changed_current_evidence_after_observation_appends_nothing(self) -> None:
        """Catches a lawful competing result losing the append-transaction race fence."""
        case, intent = self._case_with_unknown(adapter="git_local")
        before = case.effect_ledger.records()
        calls = 0

        def racing_launcher(request: ReconciliationRequest) -> ReconciliationResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                competing = case.controller.reconcile(
                    intent["operation_id"], now=NOW + timedelta(seconds=25),
                )
                self.assertEqual("unknown", competing["reconciled_outcome"])
                return self._confirmed(request)
            return self._unknown(request)

        with self._launcher_patch(racing_launcher):
            with self.assertRaises(ProtocolRefusal) as caught:
                case.controller.reconcile(
                    intent["operation_id"], now=NOW + timedelta(seconds=26),
                )
        self.assertEqual("effect_reconciliation_stale", caught.exception.code)
        after = case.effect_ledger.records()
        self.assertEqual(len(before) + 1, len(after))
        self.assertEqual(1, sum(row["kind"] == "effect_reconciled" for row in after))

    def test_controller_exact_concurrent_reconciliation_retry_has_one_physical_row(self) -> None:
        """Catches same-request retries racing into duplicate durable rows."""
        case, intent = self._case_with_unknown(adapter="git_local")
        second = EffectController(
            EffectLedger(case.root), case.run_ledger, case.run.policy,
            ApprovalLedger(case.root),
        )
        barrier = threading.Barrier(2)
        first_appended = threading.Event()
        second_precheck = threading.Event()
        results: dict[str, dict[str, object]] = {}
        failures: list[BaseException] = []
        binding_calls: dict[str, int] = {}
        original_binding = EffectController._operation_binding

        def launcher(request: ReconciliationRequest) -> ReconciliationResult:
            barrier.wait(5)
            return self._confirmed(request)

        def ordered_binding(
            controller: EffectController, operation_id: object,
        ) -> dict[str, object]:
            name = threading.current_thread().name
            binding_calls[name] = binding_calls.get(name, 0) + 1
            if name == "reconciliation-retry-b" and binding_calls[name] == 2:
                second_precheck.set()
                if not first_appended.wait(5):
                    raise AssertionError("canonical retry row was not appended")
            return original_binding(controller, operation_id)

        def reconcile(name: str, controller: EffectController) -> None:
            try:
                results[name] = controller.reconcile(
                    intent["operation_id"], now=NOW + timedelta(seconds=25),
                )
            except BaseException as exc:
                failures.append(exc)
            finally:
                if name == "a":
                    first_appended.set()

        with self._launcher_patch(launcher) as launched, mock.patch.object(
            EffectController, "_operation_binding", ordered_binding,
        ):
            threads = [
                threading.Thread(
                    target=reconcile,
                    args=(name, controller),
                    name=f"reconciliation-retry-{name}",
                )
                for name, controller in (("b", second), ("a", case.controller))
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertTrue(second_precheck.is_set())
        self.assertEqual([], failures)
        self.assertEqual(2, launched.call_count)
        self.assertEqual(2, len(results))
        self.assertEqual(1, len({str(row["id"]) for row in results.values()}))
        self.assertEqual(1, sum(
            row["kind"] == "effect_reconciled"
            for row in case.effect_ledger.records()
        ))

    def test_controller_overtaken_exact_retry_refuses_stale_historical_row(self) -> None:
        """Catches an old exact retry returning after newer evidence becomes current."""
        case, intent = self._case_with_unknown(adapter="git_local")
        controllers = [
            EffectController(
                EffectLedger(case.root), case.run_ledger, case.run.policy,
                ApprovalLedger(case.root),
            )
            for _ in range(2)
        ]
        paused = threading.Event()
        resume = threading.Event()
        results: list[dict[str, object]] = []
        failures: list[BaseException] = []
        original_now = effects_module._effect_now

        def pause_after_snapshot(value):
            if threading.current_thread().name == "overtaken-reconciliation":
                paused.set()
                if not resume.wait(5):
                    raise AssertionError("overtaken reconciliation was not resumed")
            return original_now(value)

        def reconcile_delayed() -> None:
            try:
                results.append(case.controller.reconcile(
                    intent["operation_id"], now=NOW + timedelta(seconds=27),
                ))
            except BaseException as exc:
                failures.append(exc)

        delayed = threading.Thread(
            target=reconcile_delayed, name="overtaken-reconciliation",
        )
        with self._launcher_patch(self._unknown), mock.patch.object(
            effects_module, "_effect_now", side_effect=pause_after_snapshot,
        ):
            delayed.start()
            self.assertTrue(paused.wait(5), "delayed reconciliation never paused")
            rows = [
                controllers[0].reconcile(
                    intent["operation_id"], now=NOW + timedelta(seconds=25),
                ),
                controllers[1].reconcile(
                    intent["operation_id"], now=NOW + timedelta(seconds=26),
                ),
            ]
            resume.set()
            delayed.join(10)
        self.assertFalse(delayed.is_alive())
        self.assertEqual([], results)
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], ProtocolRefusal)
        self.assertEqual("effect_reconciliation_stale", failures[0].code)
        current = case.effect_ledger.project().operation(intent["operation_id"])
        self.assertEqual(rows[-1]["id"], current["current_evidence_id"])
        self.assertEqual(2, sum(
            row["kind"] == "effect_reconciled"
            for row in case.effect_ledger.records()
        ))

    def test_controller_malformed_confirmed_child_data_never_becomes_durable_truth(self) -> None:
        """Catches exact-dataclass child forgery bypassing parent protocol validation."""
        case, intent = self._case_with_unknown(adapter="git_local")
        before = case.effect_ledger.records()

        def malformed(request: ReconciliationRequest) -> ReconciliationResult:
            return ReconciliationResult(
                schema_version=request.schema_version,
                request_id=request.request_id,
                request_digest=request.request_digest,
                outcome="confirmed",
                evidence_digest="f" * 64,
                reason_code="exact_ref_and_object",
                observation={"child": "forged"},
                confirmation=dict(request.expected_confirmation),
                spend_status="complete",
                measured_spend={"build": 0},
            )

        with self._launcher_patch(malformed):
            with self.assertRaises(ProtocolRefusal):
                case.controller.reconcile(intent["operation_id"])
        self.assertEqual(before, case.effect_ledger.records())

    def test_controller_launch_and_protocol_failures_cannot_append_confirmed_truth(self) -> None:
        """Catches timeout/death/malformed/cleanup paths retaining child confirmation."""
        cases = (
            (TimeoutError("observer deadline"), "observer_timeout"),
            (ChildProcessError("observer died"), "observer_child_died"),
            (b"not a result frame", "observer_protocol_invalid"),
            (OSError("observer cleanup failed"), "observer_cleanup_failed"),
        )
        for failure, reason in cases:
            with self.subTest(reason=reason):
                case, intent = self._case_with_unknown(adapter="git_local")
                captured: list[ReconciliationRequest] = []

                def side_effect(
                    request: ReconciliationRequest, value=failure,
                ) -> object:
                    captured.append(request)
                    if isinstance(value, BaseException):
                        raise value
                    return value

                with self._launcher_patch(side_effect):
                    row = case.controller.reconcile(intent["operation_id"])
                expected = build_result(
                    captured[0], outcome="unknown", reason_code=reason,
                )
                self.assertEqual("unknown", row["reconciled_outcome"])
                self.assertEqual(
                    expected.evidence_digest,
                    row["reconciliation_evidence_digest"],
                )
                self.assertIsNone(row["confirmation"])
                self.assertEqual("unknown", row["spend_status"])
                self.assertIsNone(row["measured_spend"])

    def test_controller_parent_unknown_digest_is_derived_from_request_and_closed_reason(self) -> None:
        """Catches failed-path evidence digests preserving unruled child fields."""
        case, intent = self._case_with_unknown(adapter="git_local")
        captured: list[ReconciliationRequest] = []

        def failure(request: ReconciliationRequest) -> ReconciliationResult:
            captured.append(request)
            raise TimeoutError("hostile child confirmation='forged' spend=999")

        with self._launcher_patch(failure):
            row = case.controller.reconcile(intent["operation_id"])
        request = captured[0]
        expected = build_result(
            request,
            outcome="unknown",
            reason_code="observer_timeout",
        )
        self.assertEqual(expected.evidence_digest, row["reconciliation_evidence_digest"])
        self.assertIsNone(row["confirmation"])
        self.assertIsNone(row["measured_spend"])

    def test_controller_ledger_corruption_and_append_failure_remain_refusal_without_row(self) -> None:
        """Catches durable-read or sole-writer failure being mislabeled as reconciliation."""
        case, intent = self._case_with_unknown(adapter="git_local")
        before = case.effect_ledger.records()
        failure = IntegrityFailure("effect_test_append_failed", "injected append failure")
        with self._launcher_patch(self._confirmed), mock.patch.object(
            effects_module, "_transact_effect_records", side_effect=failure,
        ):
            with self.assertRaises(IntegrityFailure) as caught:
                case.controller.reconcile(intent["operation_id"])
        self.assertEqual("effect_test_append_failed", caught.exception.code)
        self.assertEqual(before, case.effect_ledger.records())

    def test_runtime_and_tests_have_no_legacy_reconciliation_import_or_class_consumer(self) -> None:
        """Catches any Python consumer retaining the deleted in-process adapter path."""
        import ast

        root = Path(__file__).resolve().parents[1]
        legacy_module = "floati.effect_" + "reconciliation"
        class_names = (
            "Git" + "LocalReconciler",
            "Git" + "RemoteExplicitReconciler",
            "Unavailable" + "Reconciler",
        )
        consumers: list[str] = []
        for parent in (root / "floati", root / "tests"):
            for path in sorted(parent.rglob("*.py")):
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
                if any(
                    (
                        isinstance(node, ast.Import)
                        and any(alias.name == legacy_module for alias in node.names)
                    )
                    or (
                        isinstance(node, ast.ImportFrom)
                        and node.module == legacy_module
                    )
                    or (
                        isinstance(node, ast.ImportFrom)
                        and node.module == "floati"
                        and any(
                            alias.name == "effect_reconciliation"
                            for alias in node.names
                        )
                    )
                    or (
                        isinstance(node, ast.ImportFrom)
                        and node.level > 0
                        and (
                            node.module == "effect_reconciliation"
                            or (
                                node.module is None
                                and any(
                                    alias.name == "effect_reconciliation"
                                    for alias in node.names
                                )
                            )
                        )
                    )
                    for node in ast.walk(tree)
                ) or any(name in source for name in class_names):
                    consumers.append(str(path.relative_to(root)))
        self.assertEqual([], consumers)


class CompensationTests(_GitFixture):
    def setUp(self) -> None:
        super().setUp()
        self.case = _EffectCase(self)
        self.source = self.case.controller.intent(**self.case.intent_args())
        self.case.controller.dispatched(
            self.source["operation_id"], dispatch_adapter="git_local",
            dispatch_evidence_digest="d" * 64,
            now=NOW + timedelta(seconds=23),
        )
        self.source_terminal = self.case.controller.failed(
            self.source["operation_id"], reason_code="effect_not_applied",
            evidence_digest="e" * 64, spend_status="complete",
            measured_spend=[{"budget_id": "build", "amount": 0}],
            now=NOW + timedelta(seconds=24),
        )

    def preview_args(self, **changes: object) -> dict[str, object]:
        values: dict[str, object] = {
            "reason_code": "operator_requested",
            "effect_type": "git_ref_update",
            "target": self.local_target(),
            "request_digest": hashlib.sha256(b"compensation action").hexdigest(),
            "idempotency_key": "effect-compensation-one",
            "expected_confirmation": self.expected(),
            "reconciliation_adapter": "git_local",
            "risk_class": "low",
            "budget_claim": [{"budget_id": "build", "amount": 0}],
            "requested_by": "node-a",
        }
        values.update(changes)
        return values

    def confirm(self, preview: dict[str, object], **changes: object):
        values: dict[str, object] = {
            "plan": preview["plan"],
            "plan_digest": preview["plan_digest"],
            "now": NOW + timedelta(seconds=25),
        }
        values.update(changes)
        return self.case.controller.compensation_confirm(
            self.source["operation_id"], **values,
        )

    def test_compensation_preview_is_read_only_and_digest_stable(self) -> None:
        """Catches preview acquiring durable truth or including ambient/time-varying data."""
        before = self.case.effect_ledger.records()
        first = self.case.controller.compensation_preview(
            self.source["operation_id"], **self.preview_args(),
        )
        second = self.case.controller.compensation_preview(
            self.source["operation_id"], **self.preview_args(),
        )
        self.assertEqual(first, second)
        self.assertEqual(
            hashlib.sha256(json.dumps(
                first["plan"], ensure_ascii=False, allow_nan=False,
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            first["plan_digest"],
        )
        self.assertEqual(before, self.case.effect_ledger.records())

    def test_compensation_confirm_requires_exact_preview_digest_and_approval(self) -> None:
        """Catches digest drift or missing/wrong approval creating proposal truth."""
        preview = self.case.controller.compensation_preview(
            self.source["operation_id"], **self.preview_args(),
        )
        before = self.case.effect_ledger.records()
        for changed in (
            {"plan_digest": "0" * 64},
            {"plan": {**preview["plan"], "reason_code": "policy_required"}},
        ):
            with self.subTest(changed=changed), self.assertRaises(ProtocolRefusal):
                self.confirm(preview, **changed)
            self.assertEqual(before, self.case.effect_ledger.records())

        high = self.case.controller.compensation_preview(
            self.source["operation_id"], **self.preview_args(
                risk_class="high", idempotency_key="effect-compensation-high",
            ),
        )
        with self.assertRaises(ProtocolRefusal) as missing:
            self.confirm(high)
        self.assertEqual("effect_approval_required", missing.exception.code)
        self.assertEqual(before, self.case.effect_ledger.records())
        request, decision = self.case.approve_action(
            digest=high["plan"]["request_digest"],
        )
        result = self.confirm(
            high, approval_request_id=request["id"],
            approval_decision_id=decision["id"],
        )
        self.assertEqual(request["id"], result["proposal"]["approval_request_id"])

    def test_proposal_creates_separate_compensation_operation(self) -> None:
        """Catches a proposal mutating the source operation instead of creating new intent."""
        preview = self.case.controller.compensation_preview(
            self.source["operation_id"], **self.preview_args(),
        )
        result = self.confirm(preview)
        self.assertNotEqual(self.source["operation_id"], result["operation"]["operation_id"])
        self.assertEqual(
            result["operation"]["operation_id"],
            result["proposal"]["compensation_operation_id"],
        )
        self.assertEqual("effect_intent", result["operation"]["kind"])
        self.assertEqual("proposed", self.case.effect_ledger.project().operation(
            self.source["operation_id"]
        )["compensation_state"])
        self.assertEqual("intent", self.case.effect_ledger.project().operation(
            result["operation"]["operation_id"]
        )["state"])

    def test_proposal_is_not_execution_and_process_exit_is_not_confirmation(self) -> None:
        """Catches plan confirmation, proposal, dispatch, or provider exit becoming execution proof."""
        preview = self.case.controller.compensation_preview(
            self.source["operation_id"], **self.preview_args(),
        )
        result = self.confirm(preview)
        operation_id = result["operation"]["operation_id"]
        self.case.controller.dispatched(
            operation_id, dispatch_adapter="git_local",
            dispatch_evidence_digest=hashlib.sha256(b"process exited zero").hexdigest(),
            now=NOW + timedelta(seconds=26),
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self.case.controller.compensation_executed(
                self.source["operation_id"], now=NOW + timedelta(seconds=27),
            )
        self.assertEqual("effect_transition_invalid", caught.exception.code)
        self.assertEqual("proposed", self.case.effect_ledger.project().operation(
            self.source["operation_id"]
        )["compensation_state"])

    def test_controller_executes_only_after_separate_reconciled_confirmation(self) -> None:
        """Catches execution naming stale or non-confirmed compensation evidence."""
        preview = self.case.controller.compensation_preview(
            self.source["operation_id"], **self.preview_args(),
        )
        result = self.confirm(preview)
        operation_id = result["operation"]["operation_id"]
        self.case.controller.dispatched(
            operation_id, dispatch_adapter="git_local",
            dispatch_evidence_digest="f" * 64,
            now=NOW + timedelta(seconds=26),
        )
        self.case.controller.unknown(
            operation_id, reason_code="confirmation_absent",
            evidence_digest="1" * 64, spend_status="unknown",
            now=NOW + timedelta(seconds=27),
        )
        with mock.patch.object(
            effects_module,
            "observe_effect_reconciliation",
            side_effect=EffectControllerReconciliationTests._confirmed,
        ):
            reconciled = self.case.controller.reconcile(
                operation_id, now=NOW + timedelta(seconds=28),
            )
        self.assertEqual("confirmed", reconciled["reconciled_outcome"])
        executed = self.case.controller.compensation_executed(
            self.source["operation_id"], now=NOW + timedelta(seconds=29),
        )
        self.assertEqual(reconciled["id"], executed["compensation_terminal_evidence_id"])
        self.assertEqual("executed", self.case.effect_ledger.project().operation(
            self.source["operation_id"]
        )["compensation_state"])

    def test_compensation_confirm_recovers_crash_after_proposal_exactly(self) -> None:
        """Catches a durable proposal becoming stranded after the next intent seam."""
        preview = self.case.controller.compensation_preview(
            self.source["operation_id"], **self.preview_args(),
        )
        real_uuid7 = effects_module.uuid7_hex
        calls = 0

        def crash_after_proposal() -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("interrupted after proposal")
            return real_uuid7()

        with mock.patch.object(
            effects_module, "uuid7_hex", side_effect=crash_after_proposal,
        ):
            with self.assertRaises(RuntimeError):
                self.confirm(preview)
        self.assertEqual(1, sum(
            row["kind"] == "compensation_proposed"
            for row in self.case.effect_ledger.records()
        ))
        self.assertEqual(1, sum(
            row["kind"] == "effect_intent"
            for row in self.case.effect_ledger.records()
        ))

        for changed_plan in (
            dict(preview["plan"], reason_code="policy_required"),
            dict(preview["plan"], requested_by="node-b"),
        ):
            with self.subTest(changed_field=next(
                key for key in changed_plan
                if changed_plan[key] != preview["plan"][key]
            )):
                changed_digest = hashlib.sha256(json.dumps(
                    changed_plan, ensure_ascii=False, allow_nan=False,
                    sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                before_changed = self.case.effect_ledger.records()
                with self.assertRaises(ProtocolRefusal):
                    self.case.controller.compensation_confirm(
                        self.source["operation_id"], plan=changed_plan,
                        plan_digest=changed_digest,
                        now=NOW + timedelta(seconds=26),
                    )
                self.assertEqual(before_changed, self.case.effect_ledger.records())

        recovered = self.confirm(preview, now=NOW + timedelta(seconds=26))
        retry = self.confirm(preview, now=NOW + timedelta(seconds=27))
        self.assertEqual(recovered["proposal"]["id"], retry["proposal"]["id"])
        self.assertEqual(recovered["operation"]["id"], retry["operation"]["id"])
        rows = self.case.effect_ledger.records()
        self.assertEqual(1, sum(row["kind"] == "compensation_proposed" for row in rows))
        self.assertEqual(2, sum(row["kind"] == "effect_intent" for row in rows))

    def test_proposal_only_crash_blocks_acceptance_until_exact_recovery(self) -> None:
        """Catches acceptance permanently stranding a durable proposal-only prefix."""
        from tests.test_runtruth import EffectAcceptanceTests

        case = _EffectCase(self)
        source, _confirmed = EffectAcceptanceTests._confirmed_effect(
            case, key="compensation-crash-source", spend=1,
        )
        preview = case.controller.compensation_preview(
            source["operation_id"], **self.preview_args(
                idempotency_key="compensation-crash-target",
            ),
        )
        real_uuid7 = effects_module.uuid7_hex
        calls = 0

        def crash_after_proposal() -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("interrupted after proposal")
            return real_uuid7()

        with mock.patch.object(
            effects_module, "uuid7_hex", side_effect=crash_after_proposal,
        ):
            with self.assertRaises(RuntimeError):
                case.controller.compensation_confirm(
                    source["operation_id"], plan=preview["plan"],
                    plan_digest=preview["plan_digest"],
                    now=NOW + timedelta(seconds=25),
                )

        rows = case.effect_ledger.records()
        self.assertEqual(1, sum(
            row["kind"] == "compensation_proposed" for row in rows
        ))
        self.assertEqual(1, sum(row["kind"] == "effect_intent" for row in rows))

        evidence = case.effect_ledger.project().acceptance_evidence(
            case.run.run_id, case.opened["attempt_id"],
        )
        candidate = EffectAcceptanceTests._bound(
            case.result_acceptance_candidate(), evidence,
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            case.run_ledger.append(candidate)
        self.assertEqual("effect_unknown_blocks_acceptance", caught.exception.code)

        recovered = case.controller.compensation_confirm(
            source["operation_id"], plan=preview["plan"],
            plan_digest=preview["plan_digest"],
            now=NOW + timedelta(seconds=26),
        )
        self.assertEqual(
            preview["plan"]["compensation_operation_id"],
            recovered["operation"]["operation_id"],
        )
        rows = case.effect_ledger.records()
        self.assertEqual(1, sum(
            row["kind"] == "compensation_proposed" for row in rows
        ))
        self.assertEqual(2, sum(row["kind"] == "effect_intent" for row in rows))

    def test_compensation_confirm_fences_acceptance_until_intent_is_durable(self) -> None:
        """Catches acceptance winning between proposal and compensation intent."""
        from contextlib import contextmanager
        import floati.runtruth as runtruth_module
        from tests.test_runtruth import EffectAcceptanceTests

        case = _EffectCase(self)
        intent, _confirmed = EffectAcceptanceTests._confirmed_effect(
            case, key="compensation-interleave-source", spend=1,
        )
        preview = case.controller.compensation_preview(
            intent["operation_id"], **self.preview_args(
                idempotency_key="compensation-interleave-target",
            ),
        )
        evidence = case.effect_ledger.project().acceptance_evidence(
            case.run.run_id, case.opened["attempt_id"],
        )
        acceptance = EffectAcceptanceTests._bound(
            case.result_acceptance_candidate(), evidence,
        )
        begin_acceptance = threading.Event()
        acceptance_entered = threading.Event()
        acceptance_outcome: list[object] = []

        def accept() -> None:
            begin_acceptance.wait(5)
            acceptance_entered.set()
            try:
                acceptance_outcome.append(case.run_ledger.append(acceptance))
            except BaseException as exc:
                acceptance_outcome.append(exc)

        thread = threading.Thread(target=accept, name="compensation-acceptance-race")
        thread.start()
        acceptance_blocked: list[bool] = []
        proposal_counts_at_guard: list[int] = []
        original_guard = runtruth_module.effect_acceptance_guard

        @contextmanager
        def observed_guard(root, *, exclusive=True):
            with original_guard(root, exclusive=exclusive):
                if (
                    exclusive
                    and threading.current_thread().name
                    != "compensation-acceptance-race"
                ):
                    proposal_counts_at_guard.append(sum(
                    row["kind"] == "compensation_proposed"
                    for row in case.effect_ledger.records()
                    ))
                    begin_acceptance.set()
                    self.assertTrue(acceptance_entered.wait(5))
                    thread.join(0.2)
                    acceptance_blocked.append(thread.is_alive())
                yield

        try:
            with mock.patch.object(
                runtruth_module, "effect_acceptance_guard", observed_guard,
            ):
                result = case.controller.compensation_confirm(
                    intent["operation_id"], plan=preview["plan"],
                    plan_digest=preview["plan_digest"],
                    now=NOW + timedelta(seconds=25),
                )
        finally:
            begin_acceptance.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual([0], proposal_counts_at_guard)
        self.assertEqual([True], acceptance_blocked)
        self.assertEqual("effect_intent", result["operation"]["kind"])
        self.assertEqual(1, len(acceptance_outcome))
        self.assertIsInstance(acceptance_outcome[0], ProtocolRefusal)
        self.assertIn(
            acceptance_outcome[0].code,
            {"effect_evidence_invalid", "effect_unknown_blocks_acceptance"},
        )

    def test_compensation_confirm_concurrent_exact_retry_and_changed_plan(self) -> None:
        """Catches exact retries duplicating rows or a changed retry joining them."""
        preview = self.case.controller.compensation_preview(
            self.source["operation_id"], **self.preview_args(),
        )
        second = EffectController(
            EffectLedger(self.case.root), self.case.run_ledger,
            self.case.run.policy, ApprovalLedger(self.case.root),
        )
        barrier = threading.Barrier(2)
        results: list[dict[str, dict[str, object]]] = []
        failures: list[BaseException] = []

        def confirm_exact(controller: EffectController) -> None:
            barrier.wait(5)
            try:
                results.append(controller.compensation_confirm(
                    self.source["operation_id"], plan=preview["plan"],
                    plan_digest=preview["plan_digest"],
                    now=NOW + timedelta(seconds=25),
                ))
            except BaseException as exc:
                failures.append(exc)

        threads = [
            threading.Thread(target=confirm_exact, args=(controller,))
            for controller in (self.case.controller, second)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([], failures)
        self.assertEqual(2, len(results))
        self.assertEqual(1, len({row["proposal"]["id"] for row in results}))
        self.assertEqual(1, len({row["operation"]["id"] for row in results}))
        rows = self.case.effect_ledger.records()
        self.assertEqual(1, sum(row["kind"] == "compensation_proposed" for row in rows))
        self.assertEqual(2, sum(row["kind"] == "effect_intent" for row in rows))

        # Lawful control and changed concurrent-plan refusal retain one winner.
        competing = _EffectCase(self)
        source = competing.controller.intent(**competing.intent_args())
        competing.controller.dispatched(
            source["operation_id"], dispatch_adapter="git_local",
            dispatch_evidence_digest="d" * 64, now=NOW + timedelta(seconds=23),
        )
        competing.controller.failed(
            source["operation_id"], reason_code="effect_not_applied",
            evidence_digest="e" * 64, spend_status="complete",
            measured_spend=[{"budget_id": "build", "amount": 0}],
            now=NOW + timedelta(seconds=24),
        )
        first = competing.controller.compensation_preview(
            source["operation_id"], **self.preview_args(idempotency_key="race-first"),
        )
        second = competing.controller.compensation_preview(
            source["operation_id"], **self.preview_args(idempotency_key="race-second"),
        )
        barrier = __import__("threading").Barrier(2)
        outcomes: list[tuple[str, str]] = []

        def confirm(candidate: dict[str, object]) -> None:
            barrier.wait()
            try:
                result = competing.controller.compensation_confirm(
                    source["operation_id"], plan=candidate["plan"],
                    plan_digest=candidate["plan_digest"],
                    now=NOW + timedelta(seconds=25),
                )
                outcomes.append(("ok", result["operation"]["operation_id"]))
            except ProtocolRefusal as exc:
                outcomes.append(("refused", exc.code))

        threads = [
            __import__("threading").Thread(target=confirm, args=(candidate,))
            for candidate in (first, second)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(2, len(outcomes))
        self.assertEqual(1, sum(status == "ok" for status, _ in outcomes))
        self.assertEqual(1, sum(status == "refused" for status, _ in outcomes))
        competing_rows = competing.effect_ledger.records()
        self.assertEqual(1, sum(
            row["kind"] == "compensation_proposed" for row in competing_rows
        ))
        self.assertEqual(2, sum(row["kind"] == "effect_intent" for row in competing_rows))


if __name__ == "__main__":
    unittest.main()
