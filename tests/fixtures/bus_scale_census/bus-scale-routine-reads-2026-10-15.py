"""Closed native read fixtures and exact leaf-audited equivalence.

Preparation runs once on a case seed. Its receipts and context are copied to
both scales and all independent children. This helper never imports tests,
changes HOME, calls providers, starts a waiter, or discovers a bus root.
"""
from __future__ import annotations

import importlib.util as _fixture_proof_util
from pathlib import Path as _FixtureProofPath

_fixture_proof_specification = _fixture_proof_util.spec_from_file_location(
    "_bus_scale_fixture_proof_bootstrap", _FixtureProofPath(__file__).with_name("_proof_namespace.py"))
_fixture_proof_bootstrap = _fixture_proof_util.module_from_spec(_fixture_proof_specification)
_fixture_proof_specification.loader.exec_module(_fixture_proof_bootstrap)
_fixture_proof_bootstrap.bind()


import copy
import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import re
import stat
import subprocess
import uuid

CASES = frozenset({
    "describe", "verify", "overlap-report", "sweep", "sent", "effects", "effect-show",
    "graph", "supervise", "presence-show", "sequencer-status", "work-show",
    "role-list", "role-show", "role-validate", "quota-show", "chart",
    "chart-timings", "context-status", "context-turnover", "context-policy-show",
    "wake-status", "wake-daemon-status", "watch",
})
SESSION = "bus-scale-native-census"
_ROOT = Path(__file__).resolve().parents[3]
EFFECT_KINDS = {
    "describe": "command_contract_returned", "verify": "delivery_claim_verified", "overlap-report": "overlap_report_returned",
    "sweep": "lane_sweep_returned", "sent": "sender_receipts_returned",
    "effects": "effects_returned", "effect-show": "effect_returned", "graph": "graph_returned",
    "supervise": "supervision_returned", "presence-show": "presence_reports_returned",
    "sequencer-status": "sequencer_status_returned", "work-show": "work_items_returned",
    "role-list": "roles_returned", "role-show": "role_returned", "role-validate": "role_validated",
    "quota-show": "quota_testimony_returned", "chart": "declared_chart_returned",
    "chart-timings": "chart_timings_returned", "context-status": "context_status_returned",
    "context-turnover": "context_turnover_returned", "context-policy-show": "context_policy_returned",
    "wake-status": "wake_status_returned", "wake-daemon-status": "wake_daemon_status_returned",
    "watch": "bounded_watch_returned",
}


class UnresolvedRead(ValueError):
    code = "routine_read_prerequisite_unresolved"


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))


def _private(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path or path.is_symlink():
        raise ValueError("read fixture must be canonical and ordinary")
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ValueError("read fixture must be an owned directory")
    owners = [parent for parent in (path, *path.parents)
              if parent.name.startswith(("bus-scale-census-owned-", "bus-scale-read-controls-"))]
    if not owners:
        raise ValueError("read fixture is outside its owned scratch census")
    for owner in owners:
        metadata = owner.stat()
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("read fixture requires its private owned scratch ancestor")
    if not any(str(parent) in ("\x2fprivate/tmp", "\x2ftmp") for parent in path.parents):
        raise ValueError("read fixture is outside native temporary storage")
    return path


def _case(case, home):
    if case not in CASES:
        raise ValueError("unruled native read case")
    from floati.root import FloatiRoot
    return FloatiRoot.open_direct_home(_private(home), create=False)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(_json(value) + "\n")
    path.chmod(0o600)


def _time(value):
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError("native observation must be UTC")
    return moment


def _verify_test_source(note_digest):
    return ("import hashlib\nfrom pathlib import Path\nimport unittest\n\n"
            "class DeliveryArtifact(unittest.TestCase):\n"
            "    def test_owned_artifact(self):\n"
            "        self.assertEqual(" + repr(note_digest) + ", "
            "hashlib.sha256(Path('note.md').read_bytes()).hexdigest())\n").encode()


def _verify_prepare(root, workspace, context):
    """One real constant repository bank, authored before history padding."""
    from floati.events import EventLog
    from floati.git_process import fixed_git_command, fixed_git_environment
    def git(*query):
        return subprocess.run(fixed_git_command("/usr/bin/git", workspace, query),
            env=fixed_git_environment("/usr/bin/git"), check=True, capture_output=True, timeout=15).stdout
    note = workspace / "note.md"
    if note.resolve(strict=True) != note or not stat.S_ISREG(note.lstat().st_mode):
        raise UnresolvedRead("native verification requires one ordinary owned note.md")
    note_raw = note.read_bytes()
    wanted = {"note.md": note_raw, "tests/__init__.py": b"",
              "tests/test_census_delivery.py": _verify_test_source(hashlib.sha256(note_raw).hexdigest())}
    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise UnresolvedRead("native verification requires its clean isolated case workspace")
    def commit_tree():
        rows = []
        for frame in git("ls-tree", "-r", "-z", "--full-tree", "HEAD").split(b"\0"):
            if not frame: continue
            header, name = frame.split(b"\t", 1)
            mode, kind, blob = header.decode("ascii").split(" ")
            rows.append(dict(path=name.decode("utf-8"), mode=mode, kind=kind, blob=blob))
        return rows
    original = commit_tree()
    if {row["path"] for row in original} == {"note.md"}:
        tests = workspace / "tests"
        tests.mkdir(mode=0o700)
        for relative in ("tests/__init__.py", "tests/test_census_delivery.py"):
            with (workspace / relative).open("xb") as stream: stream.write(wanted[relative])
        git("add", "--", "tests/__init__.py", "tests/test_census_delivery.py")
        git("-c", "user.name=Owned Census Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Native delivery verification bank")
    rows = commit_tree()
    if {row["path"] for row in rows} != set(wanted) or len(rows) != len(wanted):
        raise UnresolvedRead("verification commit tree must contain exactly the reviewed note and unittest bank")
    proof = []
    for row in rows:
        relative = row["path"]
        path = workspace / relative
        raw = wanted[relative]
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        info = path.lstat()
        if (row != dict(path=relative, mode="100644", kind="blob", blob=blob)
                or path.resolve(strict=True) != path or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_nlink != 1 or path.read_bytes() != raw):
            raise UnresolvedRead("verification source tree differs from the reviewed exact constant bank")
        proof.append(dict(row, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), mode_bits=stat.S_IMODE(info.st_mode)))
    sha = git("rev-parse", "HEAD").decode("ascii").strip()
    git("update-ref", "refs/remotes/origin/main", sha)
    claim_payload = dict(kind="delivery_claim", schema_version=0, sha=sha, repo_path=str(workspace),
        bank=["tests.test_census_delivery"], declared=dict(ran=1, result="OK"),
        artifacts=[dict(path="note.md", sha256=hashlib.sha256(note_raw).hexdigest())], deadline_seconds=15)
    events = EventLog(root)
    matches = [row for row in events.event_records() if row["kind"] == "delivery_claim"
               and all(row.get(key) == value for key, value in claim_payload.items())]
    if len(matches) > 1:
        raise UnresolvedRead("native verification preface has multiple exact selected claims")
    claim = matches[0] if matches else events.send("scale-worker", "architect", "floati", sha,
        "note.md", "native verification common preface", idempotency_key="read-verify-claim",
        claim=claim_payload, now=_time(context["canonical_time"]))["claim"]
    git_files = {}
    for relative in (".git/HEAD", ".git/config", ".git/refs/remotes/origin/main"):
        path = workspace / relative
        if path.resolve(strict=True) != path or not stat.S_ISREG(path.lstat().st_mode):
            raise UnresolvedRead("verification Git proof is not ordinary")
        git_files[relative] = dict(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    context.update(claim=claim, verification_repo_sha=sha, verification_tree=proof,
                   verification_git_files=git_files)


def _verify_evidence(root, context):
    """Validate native authored proof without rerunning a test or a Git query."""
    from floati.verification import DeliveryVerifier
    from floati.jsonl import read_records_snapshot
    workspace = _private(context["workspace"])
    paths = {"note.md", "tests/__init__.py", "tests/test_census_delivery.py"}
    if (type(context["verification_tree"]) is not list or len(context["verification_tree"]) != 3
            or {row["path"] for row in context["verification_tree"]} != paths
            or set(context["verification_git_files"]) != {".git/HEAD", ".git/config", ".git/refs/remotes/origin/main"}):
        raise ValueError("verification source proof has an unreviewed coordinate")
    claim = DeliveryVerifier(root)._claim(context["claim"]["id"])
    if claim != context["claim"] or claim["repo_path"] != str(workspace) or claim["sha"] != context["verification_repo_sha"]:
        raise ValueError("verification claim differs from its exact native preparation")
    for row in context["verification_tree"]:
        path = workspace / row["path"]
        info = path.lstat()
        raw = path.read_bytes()
        if (path.resolve(strict=True) != path or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != row["mode_bits"]
                or len(raw) != row["bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]
                or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != row["blob"]):
            raise ValueError("verification repository source differs from its native preparation")
    for relative, expected in context["verification_git_files"].items():
        path = workspace / relative
        if (path.resolve(strict=True) != path or not stat.S_ISREG(path.lstat().st_mode)
                or dict(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest()) != expected):
            raise ValueError("verification repository Git identity changed")
    rows = read_records_snapshot(root, "receipts/verifications.jsonl", allowed_kinds={"verification_receipt"})
    selected = [row for row in rows if row["claim_id"] == claim["id"] and row["verifier"] == "architect"]
    if not selected:
        raise ValueError("native verification did not author its selected receipt")
    receipt = selected[-1]
    scratch = root.path / "scratch/verifications" / claim["id"]
    expected = dict(claim_id=claim["id"], verifier="architect", outcome="verified_match", reason_code=None,
        remedy="", runner_argv=["PYTHONDONTWRITEBYTECODE=1", "python3", "-m", "unittest", "tests.test_census_delivery"],
        python_version=platform.python_version(), repo_path=str(workspace), unchecked_scope="claimed_bank_only",
        claim=claim, scratch=dict(path=str(scratch), created=True, destroyed=True))
    if any(receipt[key] != value for key, value in expected.items()) or scratch.exists() or scratch.is_symlink():
        raise ValueError("native verification did not reproduce and clean its exact declared bank")
    measurement = dict(declared=claim["declared"], artifacts=[dict(row, present=True) for row in claim["artifacts"]],
                       returncode=0, output_sha256=receipt["output_sha256"])
    if receipt["measurement"] != measurement or not re.fullmatch(r"[0-9a-f]{64}", receipt["output_sha256"]):
        raise ValueError("native verification omitted or altered its actual measurement")
    return receipt


def _effect_fixture(root, bank):
    """One admitted, capability-bound, started native attempt and its intent."""
    from floati.admission import AdmissionBinder, AdmissionPlan
    from floati.approvals import ApprovalLedger
    from floati.capabilities import CapabilityGrantLedger
    from floati.capability_binding import CapabilityBinder
    from floati.contracts import contract_digest
    from floati.effects import EffectController, EffectLedger
    from floati.ids import uuid7_hex
    from floati.planes import AuthorityGrantStore
    from floati.policy import RepositoryPolicy
    from floati.runtruth import RunLedger
    from floati.scheduler import RetryPolicy, RunScheduler
    policy_path = bank / "FLOATI.toml"
    policy_path.write_text('''schema_version = 0
capability_registry = ["review"]
[limits]
max_items = 1
max_depth = 1
max_fan_out = 1
max_active_attempts = 1
[budgets.build]
unit = "attempts"
limit = 2
[worker_profiles.reader]
capabilities = ["review"]
cancel_mode = "native"
callback_support = true
max_concurrency = 1
[capability_selectors.read]
all_of = ["review"]
[routing.read]
worker_profile = "reader"
capability_selector = "read"
rank = 0
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
[verification.local]
argv = ["/usr/bin/true"]
[merge_gates.local]
verification_ids = ["local"]
''', encoding="utf-8")
    policy = RepositoryPolicy.load(policy_path)
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    item, run = "work-" + uuid7_hex(), "run-" + uuid7_hex()
    contract = dict(objective="retain a native effect intent for read projection",
        non_goals=["no external effect dispatch"], areas_to_avoid=[dict(path="foreign", region="all")],
        input_hashes={"brief": "b" * 64}, acceptance_checks={"native": "/usr/bin/true"},
        constraints={"network": "dark"}, risk_class="low",
        retry_policy=dict(max_attempts=1, backoff=dict(base_delay_ms=0, cap_delay_ms=0, strategy="fixed")),
        dependencies=[])
    value = dict(schema_version=0, workers=[dict(node_id="scale-worker", worker_profile="reader")],
        max_active_attempts=1, budget_reservations=[dict(budget_id="build", amount=1)],
        items=[dict(item_id=item, contract=contract, capability_selector="read", requires_cancellation=True,
                    requires_callback=True, workspace_key="read-workspace", concurrency_key="read-one",
                    retry_class="transient", effect_safety="idempotent", merge_gate=None)], dependency_edges=[])
    plan_path = bank / "read-effect-plan.json"
    _write(plan_path, value)
    plan = AdmissionPlan.load(plan_path)
    ledger, approvals = RunLedger(root), ApprovalLedger(root)
    authority = AuthorityGrantStore(root).claim("read-approval", "architect", 300, 300, now)
    request = approvals.request("scale-worker", "review", "worker:scale-worker", 120,
                                "read-approval", authority["epoch"], now=now + timedelta(seconds=1))
    decision = approvals.decide(request["id"], "architect", "approved", None,
        granted_scope="worker:scale-worker", granted_ttl_seconds=90, now=now + timedelta(seconds=2))
    grants = CapabilityGrantLedger(root)
    grants.grant("scale-worker", "review", policy, request["id"], decision["id"],
                 now=now + timedelta(seconds=3))
    def append(kind, prefix, **fields):
        return ledger.append(dict(schema_version=0, id=prefix + uuid7_hex(), tenant_id=root.tenant_id,
            timestamp="2026-10-09T00:00:00.000Z", kind=kind, run_id=run, **fields))
    append("run_created", "run-created-", plan_digest=plan.digest, policy_digest=policy.digest,
           item_ids=[item], dependency_edges=[])
    append("task_contract", "task-contract-", item_id=item, **plan.items[0].contract.canonical(),
           contract_digest=contract_digest(plan.items[0].contract))
    append("run_policy_bound", "run-policy-bound-", policy_digest=policy.digest)
    append("worker_pool_bound", "run-worker-pool-bound-", worker_ids=["scale-worker"])
    AdmissionBinder.bind(ledger, run, plan, policy, now=now)
    scheduler = RunScheduler(ledger)
    opened = scheduler.open_attempt(run, item, RetryPolicy(1, 0, 0, strategy="fixed"), 1, now=now)
    binder = CapabilityBinder(ledger, grants)
    snapshot = binder.bind(run, item, opened["attempt_id"], "scale-worker", "reader", policy, 0,
                           now=now + timedelta(seconds=10))
    dispatched = binder.dispatch(snapshot["id"], ["scale-worker"], "policy.route", policy,
                                  now=now + timedelta(seconds=20))
    scheduler.start_attempt(run, item, opened["attempt_id"], dispatched["id"],
                            now=now + timedelta(seconds=21))
    intent = EffectController(EffectLedger(root), ledger, policy, approvals).intent(
        run_id=run, item_id=item, attempt_id=opened["attempt_id"], fence_token=opened["fence_token"],
        effect_type="git_ref_update", target=dict(kind="git_ref", coordinate="fixture/read:refs/heads/main",
            identity_digest="a" * 64), request_digest="c" * 64, idempotency_key="read-effect-intent",
        expected_confirmation=dict(kind="git_ref_equals", locator="refs/heads/main", expected_digest="b" * 64),
        reconciliation_adapter="git_local", risk_class="low", budget_claim=[dict(budget_id="build", amount=1)],
        requested_by="scale-worker", now=now + timedelta(seconds=22))
    return intent["operation_id"]


def prepare(case, home, workspace, bank):
    root = _case(case, home)
    workspace, bank = _private(workspace), _private(bank)
    from floati.registry import Registry
    for node in ("scale-worker", "architect"):
        Registry(root).require_active(node)
    context = dict(case=case, seed_home=str(root.path), workspace=str(workspace), bank=str(bank),
                   canonical_time=datetime.now(timezone.utc).isoformat(), path_bindings=[])
    # The unsized shared seed needs real selected mail before a prefix inbox
    # read can author timing testimony, and before sent can return positive
    # work. Existing selected mail is preserved without adding another item.
    from floati.events import EventLog
    events = EventLog(root)
    if not any(row["sender"] == "scale-worker" and row["recipient"] == "architect"
               for row in events.records()):
        from floati.git_process import fixed_git_command, fixed_git_environment
        sha = subprocess.run(fixed_git_command("/usr/bin/git", workspace, ("rev-parse", "HEAD")),
                             env=fixed_git_environment("/usr/bin/git"), capture_output=True,
                             text=True, check=True, timeout=15).stdout.strip()
        if not (workspace / "note.md").is_file():
            raise UnresolvedRead("native common mail preface requires the owned committed note.md")
        events.send("scale-worker", "architect", "floati", sha, "note.md",
                    "native read common preface", idempotency_key="routine-read-common-prefix",
                    now=_time(context["canonical_time"]))
    if case == "verify":
        _verify_prepare(root, workspace, context)
    elif case in {"effects", "effect-show"}:
        context["operation_id"] = _effect_fixture(root, bank)
    elif case == "work-show":
        from floati.work import WorkLog
        context["item_id"] = WorkLog(root).add("native read fixture", "scale-worker", [])["id"]
    elif case == "presence-show":
        from floati.presence import PresenceService
        PresenceService(root).report_self("scale-worker", ttl_seconds=86400, now=_time(context["canonical_time"]))
    elif case == "context-policy-show":
        from floati.tide_policy import TidePolicyLedger
        TidePolicyLedger(root).set("scale-worker", "self_reported_context_fraction", "75%", "recommend",
                                   idempotency_key="read-policy")
    elif case == "quota-show":
        from floati.quota import QuotaFact, QuotaLedger, QuotaReceipt, QuotaState
        source = _ROOT / "tests/fixtures/bus-scale-census/native-codex-quota-2026-10-15.json"
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != "a391ffe65265dd6f4028a26c8c54c3cff255641f103f37f04f70b699fa626801":
            raise ValueError("retained native quota projection differs from its reviewed source pin")
        provided = json.loads(raw)
        primary = provided["primary"]
        fraction = Decimal(str(primary["usedPercent"])) / Decimal(100)
        captured = datetime.strptime(provided["captured_at"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        reset = datetime.fromtimestamp(primary["resetsAt"], timezone.utc)
        evidence = bank / source.name
        evidence.write_bytes(raw)
        receipt = QuotaReceipt.create(provider="openai_codex", endpoint_id="provided-native-codex-primary",
            facts=[QuotaFact("openai_codex", provided["source_fields"][0],
                    QuotaState("consumed_fraction", str(fraction)), "DERIVED",
                    "PROVIDED " + provided["source_tool"] + " projection: " + source.name
                    + "; " + ", ".join(provided["source_fields"])
                    + "; windowDurationMins=" + str(primary["windowDurationMins"]), digest,
                    captured.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    reset.isoformat(timespec="milliseconds").replace("+00:00", "Z"))],
            idempotency_key="read-quota")
        QuotaLedger(root).append(receipt)
    elif case == "role-validate":
        from floati.role_library import RoleTemplateLibrary
        path = bank / "builder-role.json"
        _write(path, RoleTemplateLibrary(root).templates()["builder"].record)
        context["role_path"] = str(path)
    elif case == "context-turnover":
        from floati.admin_registry import RegistryAdminBackend
        from floati.errors import ProtocolRefusal
        from floati.role_assignment import RoleStepWizard
        from floati.role_library import RoleTemplateLibrary
        from floati.ids import uuid7_hex
        from io import StringIO
        try:
            RegistryAdminBackend(root).role_record("scale-worker")
        except ProtocolRefusal as error:
            if error.code != "role_assignment_missing": raise
            if len(Registry(root).known_records()) != 2:
                raise UnresolvedRead("role assignment requires the unsized two-node common registry preface") from error
            RoleStepWizard(root, RegistryAdminBackend(root), RoleTemplateLibrary(root).templates(),
                id_factory=uuid7_hex).assign_from_keys(
                    ["scale-worker", "builder", "floati", "foreign", "architect"], StringIO())
    elif case == "chart":
        relative = "census-read/declared-roots.json"
        _write(root.path / relative, dict(schema_version=0, roots=[dict(bus_id="scale", root=str(root.path),
                                                       architect_node="architect", downstream=[])]))
        context["declared_roots_relative"] = relative
        context["path_bindings"].append(dict(relative=relative, pointer="/roots/0/root", source=str(root.path)))
    elif case == "sweep":
        lanes = bank / "lanes"
        lanes.mkdir(mode=0o700)
        for filename, value in (
            ("lanes-root.json", dict(schema_version=0, path=str(lanes))),
            ("lane-repositories.json", dict(schema_version=0, repositories={})),
            ("lane-board.json", dict(schema_version=0, rows={}))):
            _write(root.path / "state" / filename, value)
    elif case == "chart-timings":
        # A real local read creates the native timing receipt. No simulated
        # duration is authored and no provider or waiter is launched.
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from floati import cli
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = cli.main(["inbox", "--root", str(root.path), "--as", "architect", "--peek"])
        if code != 0:
            raise UnresolvedRead("chart-timings requires a successful native prefix inbox read")
    elif case == "overlap-report":
        from floati.git_process import fixed_git_command, fixed_git_environment
        def git(argv, data=None, *, commit=False):
            environment = fixed_git_environment("/usr/bin/git")
            if commit:
                environment.update(GIT_AUTHOR_NAME="Owned Native Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                    GIT_COMMITTER_NAME="Owned Native Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
                    GIT_AUTHOR_DATE="2026-10-09T00:00:00Z", GIT_COMMITTER_DATE="2026-10-09T00:00:00Z")
            result = subprocess.run(fixed_git_command("/usr/bin/git", workspace, argv), env=environment,
                                    input=data, capture_output=True, text=True, check=True, timeout=15)
            return result.stdout.strip()
        context["base_ref"] = git(("rev-parse", "HEAD"))
        for side, number in (("left", 1), ("right", 2)):
            blob = git(("hash-object", "-w", "--stdin"), "def selected_overlap():\n    return " + str(number) + "\n")
            tree = git(("mktree",), "100644 blob " + blob + "\toverlap.py\n")
            context[side + "_ref"] = git(("commit-tree", tree, "-p", context["base_ref"]),
                                           "owned native " + side + " overlap\n", commit=True)
    return context


def arguments(case, home, workspace, context):
    _case(case, home)
    home, workspace = Path(home), _private(workspace)
    if context.get("case") != case or context.get("workspace") != str(workspace):
        raise ValueError("read context belongs to another case/workspace")
    common = ["--root", str(home)]
    if case == "verify": return ["verify", *common, "--as", "architect", "--claim", context["claim"]["id"], "--json"]
    if case == "describe": return ["describe", "--json"]
    if case == "overlap-report":
        return ["overlap", "report", "--repository", str(workspace), "--base-ref", context["base_ref"],
                "--left-ref", context["left_ref"], "--right-ref", context["right_ref"]]
    if case in {"graph", "watch"}:
        return (["graph", *common, "--json"] if case == "graph" else
                ["watch", *common, "--iterations", "1", "--interval", "0.05"])
    if case in {"sweep", "effects", "supervise"}: return [case, *common]
    if case == "sent": return ["sent", *common, "--as", "scale-worker"]
    if case == "effect-show": return ["effect", "show", *common, "--operation", context["operation_id"]]
    if case == "work-show": return ["work", "show", *common, "--id", context["item_id"]]
    if case in {"presence-show", "sequencer-status"}: return [*case.split("-"), *common]
    if case in {"role-list", "role-show", "role-validate"}:
        return (["role", "list", *common] if case == "role-list" else
                ["role", "show", *common, "builder"] if case == "role-show" else
                ["role", "validate", *common, "--from", context["role_path"]])
    if case == "quota-show": return ["quota", "show", *common, "--provider", "openai_codex"]
    if case == "chart": return ["chart", "--declared-roots", str(home / context["declared_roots_relative"]), "--json"]
    if case == "chart-timings": return ["chart", "timings", *common, "--command", "inbox"]
    if case in {"context-status", "context-turnover"}: return ["context", case.split("-")[1], *common, "--as", "scale-worker", "--json"]
    if case == "context-policy-show": return ["context", "policy", "show", *common, "--node", "scale-worker", "--json"]
    if case == "wake-status": return ["wake", "status", *common, "--as", "scale-worker", "--session", SESSION]
    if case == "wake-daemon-status": return ["wake", "daemon", "status", *common, "--as", "scale-worker", "--harness", "codex"]
    raise ValueError("unruled native read case")


def _evidence(case, root, context, now):
    if case == "describe":
        from floati.cli import _parser
        from floati.command_contract import describe_parser
        return describe_parser(_parser())
    if case == "sent":
        from floati.sent import SentProjection
        return SentProjection(root).artifact("scale-worker", now=now)
    if case in {"effects", "effect-show"}:
        from floati.projection import EffectStatusProjection
        return EffectStatusProjection(root).artifact(now, operation_id=context["operation_id"] if case == "effect-show" else None)
    if case == "graph":
        from floati.graph import HarborGraph
        return HarborGraph(root).artifact()
    if case == "supervise":
        from floati.supervisor import Supervisor
        return Supervisor(root).snapshot(now)
    if case == "presence-show":
        from floati.presence import PresenceService
        return dict(observed_at=now.isoformat(timespec="milliseconds").replace("+00:00", "Z"), reports=PresenceService(root).reports(now))
    if case == "work-show":
        from floati.work import WorkLog
        return dict(items=WorkLog(root).show(context["item_id"]))
    if case in {"role-list", "role-show", "role-validate"}:
        from floati.role_library import RoleTemplateLibrary
        library = RoleTemplateLibrary(root)
        if case == "role-list": return dict(roles=sorted(library.templates()))
        template = library.validate_file(Path(context["role_path"])) if case == "role-validate" else library.templates()["builder"]
        return dict(template=template.record, sha256=template.digest)
    if case == "quota-show":
        from floati.quota import QuotaLedger
        record, receipt = QuotaLedger(root).latest_record("openai_codex")
        return dict(provider="openai_codex", receipt=receipt.to_dict(), ledger_record_id=record)
    if case == "chart":
        from floati.multi_bus_chart import MultiBusHarborChart
        return MultiBusHarborChart(root.path / context["declared_roots_relative"], now=now).artifact()
    if case == "chart-timings":
        from floati.timings import derive_chart
        return derive_chart(root, command="inbox")
    if case in {"context-status", "context-turnover"}:
        from floati.context import ContextStatusProjection, ContextTurnoverProjection
        cls = ContextStatusProjection if case == "context-status" else ContextTurnoverProjection
        return cls(root, "scale-worker").project()
    if case == "context-policy-show":
        from floati.tide import TideEvaluator
        return TideEvaluator(root, source_sha="f2b587634cfc6d6a52cc24bd02bfd978919c359b").projection("scale-worker")
    if case == "wake-status":
        from floati.wake_control import WakeController
        from floati.doctor_disk_watch import attach_disk_watch_report
        return attach_disk_watch_report(root, WakeController(root).status("scale-worker", SESSION))
    if case == "wake-daemon-status":
        from floati.wake_daemon_contract import DaemonCoordinate, DaemonConsentLedger
        from floati.copy import WAKE_DAEMON_INACTIVE_DISPLAY
        from floati.errors import ProtocolRefusal
        coordinate = DaemonCoordinate(root, "scale-worker", "codex")
        try: DaemonConsentLedger(root).require_active(coordinate)
        except ProtocolRefusal as error:
            if error.code != "wake_daemon_consent_absent": raise
            return dict(schema_version=0, state="inactive", node_id="scale-worker", harness="codex",
                        coordinate_digest=coordinate.digest, display=WAKE_DAEMON_INACTIVE_DISPLAY)
        raise UnresolvedRead("active wake-daemon status fixture is outside the closed inactive native case")
    if case == "sweep":
        from floati.lane_workspaces import LaneWorkspaces
        return LaneWorkspaces(root).sweep(apply=False)
    if case == "overlap-report":
        from floati.overlap_radar import derive_overlap_report
        return derive_overlap_report(Path(context["workspace"]), context["base_ref"], context["left_ref"], context["right_ref"])
    if case == "sequencer-status":
        from floati.sequencer import sequencer_socket_path
        from floati.sequencer_epoch import SequencerEpochLedger
        if SequencerEpochLedger(root)._current_snapshot() is not None:
            raise UnresolvedRead("sequencer read fixture requires the native direct state")
        return dict(mode="direct", managed_epoch_open=False, local_service_live=False, epoch=None,
                    sequencer_id=None, socket_path=str(sequencer_socket_path(root)))
    if case == "watch":
        from floati.projection import FleetProjection
        from floati.installer_shadow import observe_installer_shadow
        snapshot = FleetProjection(root).snapshot(now)
        snapshot["installer_shadow"] = observe_installer_shadow(None)
        return dict(delta=dict(kind="initial", snapshot=snapshot))
    raise ValueError("unruled native read case")


def witness(case, home, before, artifact, context, detail, *, registry_prefix_input=None):
    root = _case(case, home)
    if case == "verify":
        return _verify_witness(root, before, artifact, context,registry_prefix_input=registry_prefix_input)
    if artifact.get("status") != "ok" or artifact.get("command") != arguments(case, home, context["workspace"], context)[0]:
        raise ValueError("native read did not succeed at its exact command")
    evidence = copy.deepcopy(artifact["evidence"])
    scope = evidence.pop("scope", None)
    if scope is not None and scope != dict(root=str(root.path), tenant=root.tenant_id, root_source="explicit"):
        raise ValueError("native read scope belongs to another root")
    now = _projection_time(case, evidence)
    expected = _evidence(case, root, context, now)
    if _json(evidence) != _json(expected):
        raise ValueError("complete native read artifact differs from its retained source projection")
    if any(not isinstance(path, str) or not isinstance(raw, bytes) for path, raw in before.items()):
        raise ValueError("read witness requires the retained relative-path/raw-byte inventory")
    registry_paths = set()
    if before:
        after = _verify_files(root)
        if any((path==".registry-prefix" or path.startswith(".registry-prefix/"))
               and before.get(path)!=after.get(path) for path in set(before)|set(after)):
            from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import verify_registry_prefix_effect
            from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import raw_candidate_delta
            proof = verify_registry_prefix_effect(home,before,after,
                raw_candidate_delta(before,after),registry_prefix_input)
            registry_paths.update(proof["changed_files"])
    for relative, raw in before.items():
        if relative in registry_paths:
            continue
        path = root.resolve_relative(relative)
        if not path.is_file() or path.read_bytes() != raw:
            raise ValueError("native read changed a retained payload: " + relative)
    # A successful empty quota/effect/work/history result cannot stand in for
    # the prepared positive work those cases are specifically meant to read.
    for selected, key in (("sent", "items"), ("effects", "operations"), ("effect-show", "operations"),
                          ("work-show", "items"), ("presence-show", "reports")):
        if case == selected and not evidence[key]: raise ValueError("native read omitted its selected work")
    return dict(exit_code=0, status="ok", effect=dict(kind=EFFECT_KINDS[case], count=1))


def _verify_files(root):
    result = {}
    for path in root.path.rglob("*"):
        info = path.lstat()
        if (path.resolve(strict=True) != path or info.st_uid != os.getuid()
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            raise ValueError("verification evidence contains an alias or non-owned object")
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise ValueError("verification evidence contains a shared hardlink")
            result[path.relative_to(root.path).as_posix()] = path.read_bytes()
    return result


def _verify_witness(root, before, artifact, context, *, registry_prefix_input=None):
    from floati.jsonl import _decode_path_records
    if (type(artifact) is not dict or set(artifact) != {"artifact_version", "command", "status", "evidence"}
            or type(artifact["artifact_version"]) is not int or artifact["artifact_version"] != 0
            or artifact["command"] != "verify" or artifact["status"] != "ok"):
        raise ValueError("verification command did not produce its complete successful artifact")
    receipt = _verify_evidence(root, context)
    evidence = copy.deepcopy(artifact["evidence"])
    scope = evidence.pop("scope", None)
    if scope is not None and scope != dict(root=str(root.path), tenant=root.tenant_id, root_source="explicit"):
        raise ValueError("native verification scope belongs to another root")
    if evidence != receipt:
        raise ValueError("complete native verification artifact differs from its authored receipt")
    if type(before) is not dict or any(type(path) is not str or type(raw) is not bytes for path, raw in before.items()):
        raise ValueError("verification witness requires retained raw input bytes")
    current = _verify_files(root)
    registry_paths = set()
    if before and any((path==".registry-prefix" or path.startswith(".registry-prefix/"))
                     and before.get(path)!=current.get(path) for path in set(before)|set(current)):
        from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import verify_registry_prefix_effect
        from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import raw_candidate_delta
        proof = verify_registry_prefix_effect(root.path,before,current,
            raw_candidate_delta(before,current),registry_prefix_input)
        registry_paths.update(proof["changed_files"])
    relative = "receipts/verifications.jsonl"
    for path, raw in before.items():
        if path in registry_paths:
            continue
        if path == relative:
            if not current.get(path, b"").startswith(raw):
                raise ValueError("verification receipt append rewrote retained history")
        elif current.get(path) != raw:
            raise ValueError("verification changed retained input: " + path)
    # An empty before map is used only for the already-authored full receipt
    # comparison above. Collected observations always supply their inventory.
    if before:
        for path in set(current) - set(before):
            if path not in registry_paths and path != relative and (not path.endswith(".jsonl.lock") or current[path]):
                raise ValueError("verification wrote an unreviewed payload: " + path)
        appended = _decode_path_records(root.path / relative, root.tenant_id,
            frozenset({"verification_receipt"}), current[relative][len(before.get(relative, b"")):])
        if appended != [receipt]:
            raise ValueError("verification must append exactly its one complete native receipt")
    return dict(exit_code=0, status="ok", effect=dict(kind=EFFECT_KINDS["verify"], count=1))


def _verify_output_proof(output_sha256, wall_time_seconds):
    """Prove one exact stock success transcript within its native duration.

    The constant one-test bank prints no test-authored output. Its complete
    unittest transcript varies only by the elapsed milliseconds printed by
    the stock runner. Hashes of arbitrary or additional output do not qualify.
    The half-millisecond allowance is solely the runner's three-place rounding;
    no candidate may exceed the bank's fixed fifteen-second deadline.
    """
    if not isinstance(output_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", output_sha256) is None:
        raise ValueError("verification output hash must be a complete lowercase SHA256")
    if isinstance(wall_time_seconds, bool) or not isinstance(wall_time_seconds, (str, int, float, Decimal)):
        raise ValueError("verification output proof requires a finite nonnegative native duration")
    try:
        duration = Decimal(str(wall_time_seconds))
    except InvalidOperation as error:
        raise ValueError("verification output proof requires a finite nonnegative native duration") from error
    if not duration.is_finite() or duration < 0:
        raise ValueError("verification output proof requires a finite nonnegative native duration")
    bounded_duration = min(Decimal("15"), duration)
    maximum = min(15000, int((bounded_duration + Decimal("0.0005")) * 1000))
    def transcript(elapsed):
        return b".\n" + b"-" * 70 + b"\nRan 1 test in " + elapsed.encode("ascii") + b"s\n\nOK\n"
    matches = []
    for milliseconds in range(maximum + 1):
        raw = transcript("%d.%03d" % (milliseconds // 1000, milliseconds % 1000))
        if hashlib.sha256(raw).hexdigest() == output_sha256:
            matches.append((milliseconds, len(raw)))
    if len(matches) != 1:
        raise ValueError("verification output hash has no unique exact stock transcript preimage within native duration")
    milliseconds, byte_count = matches[0]
    canonical = transcript("<bounded-native-unittest-duration>")
    return dict(kind="exact_bounded_native_unittest_output", observed_output_sha256=output_sha256,
                elapsed_milliseconds=milliseconds, transcript_bytes=byte_count,
                canonical_output_sha256=hashlib.sha256(canonical).hexdigest())


def _normalize_verify(root, delta, artifact, context, bounds):
    from floati.framing import decode_frames
    from floati.jsonl import MAX_LEDGER_BYTES, _decode_path_records
    after = _verify_files(root)
    before, semantic = copy.deepcopy(after), {}
    if type(delta) is not dict:
        raise ValueError("verification raw delta is not an object")
    registry_proof = None
    if any(type(path) is str and (path==".registry-prefix" or path.startswith(".registry-prefix/")) for path in delta):
        from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import SUMMARY as REGISTRY_SUMMARY, verify_registry_prefix_effect
        if bounds.get("before_raw_files") is None:
            raise ValueError("registry cache equivalence requires exact pre-child raw bytes")
        registry_proof = verify_registry_prefix_effect(root.path,bounds["before_raw_files"],after,
            delta,bounds.get("registry_prefix_input"))
        delta = {path:change for path,change in delta.items() if path not in registry_proof["changed_files"]}
        semantic[REGISTRY_SUMMARY] = {"operation":"native_cache_rebuild","kind":"complete_native_registry_prefix"}
    relative = "receipts/verifications.jsonl"
    if type(delta) is not dict or relative not in delta:
        raise ValueError("verification normalization requires the actual native receipt append")
    for path, change in delta.items():
        if path != relative:
            _validate_lock_delta(root, path, change)
            before.pop(path, None)
            semantic[path] = dict(operation="create_lock")
            continue
        fields = {"operation", "before_bytes", "after_bytes", "before_sha256", "after_sha256", "payload_base64"}
        if (type(change) is not dict or set(change) != fields or change["operation"] != "append"
                or any(type(change[key]) is not int or not 0 <= change[key] <= MAX_LEDGER_BYTES
                       for key in ("before_bytes", "after_bytes"))
                or any(type(change[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", change[key]) is None
                       for key in ("before_sha256", "after_sha256"))):
            raise ValueError("verification raw receipt delta is malformed")
        raw, count = before[relative], change["before_bytes"]
        payload = base64.b64decode(change["payload_base64"], validate=True)
        if (base64.b64encode(payload).decode() != change["payload_base64"] or len(raw) != change["after_bytes"]
                or count > len(raw) or hashlib.sha256(raw).hexdigest() != change["after_sha256"]
                or hashlib.sha256(raw[:count]).hexdigest() != change["before_sha256"] or raw[count:] != payload):
            raise ValueError("verification delta differs from its exact current prefix/suffix")
        _decode_path_records(root.path / relative, root.tenant_id, frozenset({"verification_receipt"}), raw)
        if count: before[relative] = raw[:count]
        else: before.pop(relative)
        semantic[relative] = dict(operation="append", frames=decode_frames(payload))
    if "before_raw_files" in bounds:
        proved = set(registry_proof["changed_files"]) if registry_proof is not None else set()
        if any(path not in proved and bounds["before_raw_files"].get(path)!=before.get(path)
               for path in set(bounds["before_raw_files"])|set(before)):
            raise ValueError("verification delta omits or invents a retained input change")
        before = bounds["before_raw_files"]
    _verify_witness(root, before, artifact, context,registry_prefix_input=bounds.get("registry_prefix_input"))
    receipt = _verify_evidence(root, context)
    if semantic[relative]["frames"] != [receipt]:
        raise ValueError("verification delta is not its one complete authored receipt")
    begin, end = bounds.get("wall_begin"), bounds.get("wall_end")
    if (not isinstance(begin, datetime) or not isinstance(end, datetime) or begin.tzinfo is None
            or end.tzinfo is None or begin > end):
        raise ValueError("verification normalization requires retained aware invocation walls")
    timestamp = _time(receipt["timestamp"])
    identity = uuid.UUID(hex=receipt["id"].rsplit("-", 1)[-1])
    born = datetime.fromtimestamp((identity.int >> 80) / 1000, timezone.utc)
    if (identity.version != 7 or identity.variant != uuid.RFC_4122
            or not begin - timedelta(milliseconds=1) <= timestamp <= end
            or not begin - timedelta(milliseconds=1) <= born <= end):
        raise ValueError("verification generated receipt identity/time lies outside invocation")
    duration = float(receipt["wall_time_seconds"])
    if not math.isfinite(duration) or not 0 <= duration <= (end - begin).total_seconds() + .001:
        raise ValueError("native verification duration exceeds retained invocation")
    output_proof = _verify_output_proof(receipt["output_sha256"], receipt["wall_time_seconds"])
    result = dict(artifact=copy.deepcopy(artifact), delta=copy.deepcopy(semantic), normalizations=[])
    original = copy.deepcopy(result)
    def leaf(parts, expected, replacement, reason):
        target = result
        for part in parts[:-1]: target = target[part]
        if target[parts[-1]] != expected or isinstance(expected, (dict, list)):
            raise ValueError("verification audit is not one bound native scalar")
        target[parts[-1]] = replacement
        pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)
        # Artifact is already the complete top-level artifact, matching the
        # existing read adapter's /evidence/... pointer convention.
        if parts[0] == "artifact": pointer = pointer[len("/artifact"):]
        result["normalizations"].append(dict(pointer=pointer, before=expected, after=replacement, reason=reason))
    for target in (["artifact", "evidence"], ["delta", relative, "frames", 0]):
        for field, replacement in (("id", "<native-verification-receipt>"), ("timestamp", "<invocation-time>"),
                                   ("wall_time_seconds", "<bounded-native-verification-duration>")):
            leaf([*target, field], receipt[field], replacement, "exact authored native verification and invocation bounds validated")
        leaf([*target, "scratch", "path"], receipt["scratch"]["path"],
             "<bus-scale-fixture>/scratch/verifications/" + context["claim"]["id"], "exact native scratch coordinate was created and destroyed")
        for suffix in (["output_sha256"], ["measurement", "output_sha256"]):
            leaf([*target, *suffix], receipt["output_sha256"], output_proof["canonical_output_sha256"],
                 "exact bounded stock unittest transcript preimage validated: " + _json(output_proof))
    if "scope" in artifact["evidence"]:
        leaf(["artifact", "evidence", "scope", "root"], str(root.path), "<bus-scale-fixture>", "exact native invocation root")
    restored = copy.deepcopy(result)
    for audit in result["normalizations"]:
        parts = [part.replace("~1", "/").replace("~0", "~") for part in audit["pointer"].split("/")[1:]]
        if parts[0] != "delta": parts.insert(0, "artifact")
        old, target = original, restored
        for part in parts[:-1]:
            old = old[int(part)] if isinstance(old, list) else old[part]
            target = target[int(part)] if isinstance(target, list) else target[part]
        key = int(parts[-1]) if isinstance(target, list) else parts[-1]
        target[key] = copy.deepcopy(old[key])
    restored["normalizations"] = []
    if restored != original or _verify_files(root) != after:
        raise ValueError("verification complete scalar audit round trip differs")
    return result


def _observation_time(case, evidence):
    if case == "watch":
        snapshot = evidence["delta"]["snapshot"]
        return _time(snapshot["nodes"][0]["wake_health"]["recent_sessions"]["window_end"])
    if case == "chart":
        bus = evidence["buses"][0]
        if bus["last_activity_at"] is not None:
            return _time(bus["last_activity_at"]) + timedelta(seconds=bus["last_activity_age_seconds"])
    return _time(evidence["observed_at"]) if "observed_at" in evidence else datetime.now(timezone.utc)


def _projection_time(case, evidence):
    observed = _observation_time(case, evidence)
    if case == "supervise":
        # Supervisor publishes milliseconds, while its native waiting ages
        # retain the input clock's microseconds. Reconstruct only the exact
        # source comparison time; never replace the published observation.
        for node in evidence["nodes"]:
            attention = node["seat_attention"]
            age = attention["oldest_waiting_age_seconds"]
            if attention["oldest_waiting_at"] is not None and age is not None and age > 0:
                exact = _time(attention["oldest_waiting_at"]) + timedelta(seconds=age)
                published = exact.replace(microsecond=(exact.microsecond // 1000) * 1000)
                if published != observed:
                    raise ValueError("native waiting age disagrees with the published observation time")
                return exact
    return observed


def _validate_lock_delta(root, relative, change, *, case=None):
    empty_digest = hashlib.sha256(b"").hexdigest()
    fields = {"operation", "before_bytes", "after_bytes", "before_sha256", "after_sha256", "payload_base64"}
    # Native coordination paths are exact per-command inputs, not a general
    # exemption for arbitrary .lock files beside the measured ledgers.
    coordination = {"graph": {"effects/acceptance.lock"}, "sweep": {"state/lane-workspaces.lock"}}
    if (not isinstance(relative, str)
            or not (relative.endswith(".jsonl.lock") or relative in coordination.get(case, set()))
            or Path(relative).is_absolute() or Path(relative).as_posix() != relative
            or any(part in {".", ".."} for part in Path(relative).parts)
            or not isinstance(change, dict) or set(change) != fields
            or change["operation"] != "create_lock"
            or type(change["before_bytes"]) is not int or change["before_bytes"] != 0
            or type(change["after_bytes"]) is not int or change["after_bytes"] != 0
            or change["before_sha256"] != empty_digest or change["after_sha256"] != empty_digest
            or change["payload_base64"] != ""):
        raise ValueError("native read supplied malformed lock testimony: " + str(relative))
    path = root.path
    try:
        parts = Path(relative).parts
        for index, part in enumerate(parts):
            path = path / part
            metadata = path.lstat()
            last = index == len(parts) - 1
            if (metadata.st_uid != os.getuid() or metadata.st_mode & 0o022
                    or (not last and not stat.S_ISDIR(metadata.st_mode))
                    or (last and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1))):
                raise ValueError("native read lock must be an ordinary owned file: " + relative)
        if metadata.st_size != 0 or path.read_bytes() != b"":
            raise ValueError("native read lock bytes differ from their raw testimony: " + relative)
    except OSError as error:
        raise ValueError("native read lock testimony names no readable owned file: " + relative) from error


def normalize(case, delta, artifact, context, **bounds):
    home = Path(bounds.get("home", context["seed_home"]))
    root = _case(case, home)
    if type(delta) is not dict:
        raise ValueError("native read raw delta is not an object")
    if case == "verify":
        return _normalize_verify(root, delta, artifact, context, bounds)
    registry_proof = None
    if any(type(path) is str and (path==".registry-prefix" or path.startswith(".registry-prefix/")) for path in delta):
        from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import SUMMARY as REGISTRY_SUMMARY, verify_registry_prefix_effect
        if bounds.get("before_raw_files") is None:
            raise ValueError("registry cache equivalence requires exact pre-child raw bytes")
        registry_proof = verify_registry_prefix_effect(home,bounds["before_raw_files"],_verify_files(root),
            delta,bounds.get("registry_prefix_input"))
        delta = {path:change for path,change in delta.items() if path not in registry_proof["changed_files"]}
    witness(case, home, {}, artifact, context, {})
    result, audits = copy.deepcopy(artifact), []
    evidence = result["evidence"]
    def replace(pointer, value, reason):
        target = result
        parts = pointer.strip("/").split("/")
        for part in parts[:-1]: target = target[int(part)] if isinstance(target, list) else target[part]
        key = int(parts[-1]) if isinstance(target, list) else parts[-1]
        old = target[key]
        if old != value:
            audits.append(dict(pointer=pointer, before=copy.deepcopy(old), after=copy.deepcopy(value), reason=reason))
            target[key] = value
    if "scope" in evidence: replace("/evidence/scope/root", "<bus-scale-fixture>", "exact native clone scope")
    own_clock = ("observed_at" in evidence or case in {"watch", "chart"})
    exact = _projection_time(case, evidence)
    canonical = _time(context["canonical_time"])
    if own_clock and "wall_begin" in bounds and "wall_end" in bounds:
        # MultiBusHarborChart._bus truncates its age to integer seconds.
        # Watch retains the precise recent_sessions.window_end; other reads
        # publish milliseconds (or reconstruct that observation more exactly).
        # Truncation can only precede begin, never follow the collected end.
        precision = (timedelta(seconds=1) if case == "chart" else
                     timedelta(0) if case == "watch" else timedelta(milliseconds=1))
        if not bounds["wall_begin"] - precision <= exact <= bounds["wall_end"]:
            raise ValueError("native read clock is outside its collected invocation")
    original = _evidence(case, root, context, exact)
    normal = _evidence(case, root, context, canonical)
    allowed = {
        "sent": (r"/observed_at", r"/items/\d+/(message_age_seconds|attention_age_seconds)"),
        "effects": (r"/observed_at",), "effect-show": (r"/observed_at",),
        "presence-show": (r"/observed_at",),
        "chart": (r"/buses/\d+/last_activity_age_seconds",),
        "supervise": (r"/observed_at", r"/nodes/\d+/oldest_unread/(age_minutes|observed_at)",
                       r"/nodes/\d+/seat_attention/(observed_at|oldest_waiting_age_seconds|detail)"),
        "watch": (r"/delta/snapshot/observed_at", r"/delta/snapshot/nodes/\d+/oldest_unread/(age_minutes|observed_at)",
                  r"/delta/snapshot/nodes/\d+/seat_attention/(observed_at|oldest_waiting_age_seconds|detail)",
                  r"/delta/snapshot/nodes/\d+/wake_health/observed_at",
                  r"/delta/snapshot/nodes/\d+/wake_health/oldest_unread/(age_minutes|observed_at)",
                  r"/delta/snapshot/nodes/\d+/wake_health/recent_sessions/window_(start|end)"),
    }.get(case, ())
    attention_clocks = case in {"supervise", "watch"}
    if attention_clocks:
        snapshot = original if case == "supervise" else original["delta"]["snapshot"]
        prefix = "/evidence/" if case == "supervise" else "/evidence/delta/snapshot/"
        for index, node in enumerate(snapshot["nodes"]):
            attention = node["seat_attention"]
            detail = attention["detail"]
            if not detail:
                continue
            # Full native witnessing above has already proved this prose.
            # Retain its branch, counts, hold facts and remedy; only its exact
            # elapsed-minute token is a clock-dependent comparison value.
            age = attention["oldest_waiting_age_seconds"]
            if type(age) not in (int, float) or not math.isfinite(age) or age < 0:
                raise ValueError("native attention prose has no finite waiting age")
            token = "oldest waiting " + str(int(age // 60)) + "m;"
            if detail.count(token) != 1:
                raise ValueError("native attention duration token differs from its waiting age")
            replace(prefix + "nodes/" + str(index) + "/seat_attention/detail",
                    detail.replace(token, "oldest waiting <elapsed-minutes>m;", 1),
                    "exact native elapsed-minute token; all branch prose retained")
    def clock_diff(left, right, path=""):
        if type(left) is dict and type(right) is dict and set(left) == set(right):
            for key in left: clock_diff(left[key], right[key], path + "/" + key)
        elif type(left) is list and type(right) is list and len(left) == len(right):
            for index, (a, b) in enumerate(zip(left, right)): clock_diff(a, b, path + "/" + str(index))
        elif _json(left) != _json(right):
            if attention_clocks:
                # Seed-time replay can cross an attention, liveness or recent
                # activity boundary. Those are actual native facts, not clock
                # leaves. Keep their values and structure literally; complete
                # mode/pair comparison must reject any categorical mismatch.
                if path.endswith("/seat_attention/detail"):
                    return
                if (not any(re.fullmatch(pattern, path) for pattern in allowed)
                        or left is None or right is None
                        or type(left) in (dict, list) or type(right) in (dict, list)):
                    return
                replace("/evidence" + path, right,
                        "validated continuous native clock leaf; actual categories and nullness retained")
                return
            if not any(re.fullmatch(pattern, path) for pattern in allowed):
                raise ValueError("native normalization would change unreviewed testimony: " + path)
            replace("/evidence" + path, right, "validated own-clock native projection to shared seed clock")
    clock_diff(original, normal)
    if case == "sequencer-status": replace("/evidence/socket_path", "<bus-scale-fixture>/sequencer/writer.sock", "validated native clone socket")
    if case == "wake-status": replace("/evidence/marker", "<bus-scale-fixture>/" + str(Path(evidence["marker"]).relative_to(root.path)), "validated native clone marker")
    if case == "wake-daemon-status":
        from floati.wake_daemon_contract import DaemonCoordinate
        if evidence["coordinate_digest"] != DaemonCoordinate(root, "scale-worker", "codex").digest:
            raise ValueError("foreign daemon coordinate")
        replace("/evidence/coordinate_digest", "<native-clone-coordinate>", "validated native daemon coordinate")
    if case == "chart": replace("/evidence/buses/0/root", "<bus-scale-fixture>", "validated declared clone root")
    if case == "context-turnover":
        if evidence["state_file"] != str(root.path / "nodes/scale-worker/STATE.md"):
            raise ValueError("turnover recipe names another node/root")
        replace("/evidence/state_file", "<bus-scale-fixture>/nodes/scale-worker/STATE.md",
                "exact native turnover state-file coordinate")
        for index, step in enumerate(evidence["steps"]):
            if step["argv"][3:5] != ["--root", str(root.path)]:
                raise ValueError("turnover recipe has a foreign root argument")
            replace("/evidence/steps/" + str(index) + "/argv/4", "<bus-scale-fixture>",
                    "exact native turnover command root argument")
    for path, change in delta.items():
        _validate_lock_delta(root, path, change, case=case)
    normalized_delta = copy.deepcopy(delta)
    if registry_proof is not None:
        normalized_delta[REGISTRY_SUMMARY] = {"operation":"native_cache_rebuild","kind":"complete_native_registry_prefix"}
    return dict(artifact=result, delta=normalized_delta, normalizations=audits)
