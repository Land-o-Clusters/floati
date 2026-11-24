"""Private native lifecycle/runtime workloads; no test imports or live hooks.

Preparation runs on one unscaled owned two-node prelude before native padding.
All native outputs remain raw evidence; unknown prerequisites fail explicitly.
"""
from __future__ import annotations

import importlib.util as _fixture_proof_util
from pathlib import Path as _FixtureProofPath

_fixture_proof_specification = _fixture_proof_util.spec_from_file_location(
    "_bus_scale_fixture_proof_bootstrap", _FixtureProofPath(__file__).with_name("_proof_namespace.py"))
_fixture_proof_bootstrap = _fixture_proof_util.module_from_spec(_fixture_proof_specification)
_fixture_proof_specification.loader.exec_module(_fixture_proof_bootstrap)
_fixture_proof_bootstrap.bind()


import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from floati.root import FloatiRoot

CASES = frozenset([
    "reclaim",
    "lane-open",
    "lane-close",
    "wake-evaluate",
    "wake-record",
    "node-boot",
    "node-teardown",
    "node-explain",
    "node-prep-clear",
    "node-state-flush",
    "node-prompts",
    "seat-board",
    "wake-pause",
    "wake-resume",
    "wake-arm",
    "wake-wait",
    "context-reading-record",
    "wake-callback",
    "wake-daemon-serve"
])
SOURCE = Path(__file__).resolve().parents[3]
ACTOR, COORDINATOR = "scale-worker", "architect"
SESSION = "bus-scale-native-lifecycle"
ROW = "census-lifecycle"


class Unresolved(ValueError):
    code = "bus_scale_lifecycle_unresolved"


def _require(value, detail):
    if not value:
        raise Unresolved(detail)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _equal(left, right, label):
    if type(left) in (set,frozenset) or type(right) in (set,frozenset):
        _require(type(left) in (set,frozenset) and type(right) in (set,frozenset) and left==right,
                 label+" differs from native testimony")
        return
    _require(_json(left) == _json(right), label + " differs from native testimony")


def _private(path):
    path = Path(path)
    temporary = Path("\x2fprivate/tmp" if os.uname().sysname == "Darwin" else "\x2ftmp")
    _require(path.is_absolute() and path.resolve(strict=True) == path and temporary in path.parents
             and path.is_dir() and not path.is_symlink() and path.stat().st_uid == os.getuid()
             and not stat.S_IMODE(path.stat().st_mode) & 0o077, "ordinary owned private temporary directory required")
    return path


def _root(case, home):
    _require(case in CASES, "unknown lifecycle case")
    home = _private(home)
    if case == "wake-callback":
        _require(home.parent.name == "tenants", "callback requires its real namespace-root tenant clone")
        return FloatiRoot.open(home.parent.parent, home.name)
    return FloatiRoot.open_direct_home(home, create=False)


def _write(path, value):
    path = Path(path)
    _require(not path.is_symlink(), "fixture JSON path cannot be an alias")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (_json(value) + "\n").encode()
    with path.open("wb") as stream:
        os.chmod(path, 0o600)
        stream.write(encoded)


def snapshot(home):
    """Retained immediate pre-invocation raw bytes for the native witness."""
    home = _private(home)
    result = {}
    for path in sorted(home.rglob("*")):
        _require(not path.is_symlink(), "fixture snapshot cannot follow aliases")
        if path.is_file():
            result[path.relative_to(home).as_posix()] = path.read_bytes()
    return result


def _roles(root):
    from floati.admin_registry import RegistryAdminBackend
    from floati.ids import uuid7_hex
    from floati.role_assignment import RoleStepWizard
    from floati.role_library import RoleTemplateLibrary
    backend = RegistryAdminBackend(root)
    templates = RoleTemplateLibrary(root).templates()
    wizard = RoleStepWizard(root, backend, templates, id_factory=uuid7_hex)
    for node, role in ((COORDINATOR, "architect"), (ACTOR, "builder")):
        if backend.current_role(node) is not None:
            _require(backend.role_record(node)["template_role"] == role, "existing native role differs from fixture")
            continue
        answers = {"repo": "floati", "never_touch": "foreign-project", "owner_stops": "owner-tier",
                   "architect": COORDINATOR}
        provided = [answers.get(question.key, "" if question.default is not None else "bounded-private-fixture")
                    for question in templates[role].questions]
        wizard.assign_from_keys([node, role, *provided], io.StringIO())


def _binding(root, workspace, *, claim=False):
    from floati.codex_wait_contract import (write_workspace_map, resolve_participant,
        CodexWaitConsentLedger, CodexWaitSessionLedger)
    write_workspace_map(root.tenant_home, workspace, ACTOR)
    participant = resolve_participant(root.tenant_home, workspace, _strict=True)
    _require(participant is not None and participant.binding.node_id == ACTOR, "native participant unresolved")
    consent = CodexWaitConsentLedger(root).arm(participant.binding, hook_timeout_seconds=10,
        wait_deadline_seconds=2, idempotency_key="census-lifecycle-consent")
    if claim:
        CodexWaitSessionLedger(root).arm(participant.binding, consent, SESSION,
            idempotency_key="census-lifecycle-claim")
    return consent


def _selected(root, recipient):
    from floati.events import EventLog
    messages = [row for row in EventLog(root).event_records()
                if row["kind"] == "message_envelope" and row["recipient"] == recipient]
    _require(len(messages) == 1, "native selected preface must contain exactly one envelope before padding")
    return messages[0]


def _provider(bank):
    # A private fixed fail-closed provider fixture. Quiet daemon work must
    # never invoke it. It cannot manufacture a successful provider result.
    path = bank / "provider-must-not-run"
    content = b"#!/bin/sh\nprintf 'unexpected provider invocation\\n' >&2\nexit 97\n"
    if path.exists():
        _require(path.read_bytes() == content, "owned provider fixture changed")
    else:
        path.write_bytes(content)
        path.chmod(0o700)
    return path


def _prepare_callback(root):
    from floati.contracts import TaskContract, contract_digest
    from floati.ids import uuid7_hex
    from floati.runtruth import RunLedger
    from floati.scheduler import RunScheduler, RetryPolicy
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    run_id, item_id = "run-" + uuid7_hex(), "work-" + uuid7_hex()
    digest = hashlib.sha256(b"native private callback preface").hexdigest()
    ledger = RunLedger(root)
    def append(kind, prefix, **fields):
        return ledger.append(dict(schema_version=0, id=prefix + uuid7_hex(), tenant_id=root.tenant_id,
                                  timestamp=stamp, kind=kind, **fields))
    append("run_created", "run-created-", run_id=run_id, plan_digest=digest,
           item_ids=[item_id], dependency_edges=[])
    contract = TaskContract.create(objective="revalidate one genuine due native wake",
        non_goals=["no provider execution"], areas_to_avoid=[{"path":"foreign-project","region":"all"}],
        input_hashes={"brief":digest}, acceptance_checks={"tests.native":"native correctness control"},
        constraints={"network":"dark"}, risk_class="high",
        retry_policy={"max_attempts":2,"backoff":{"base_delay_ms":100,"cap_delay_ms":1000,"strategy":"exponential"}},
        dependencies=[])
    append("task_contract", "task-contract-", run_id=run_id, item_id=item_id,
           **contract.canonical(), contract_digest=contract_digest(contract))
    append("run_policy_bound", "run-policy-bound-", run_id=run_id, policy_digest=digest)
    append("worker_pool_bound", "run-worker-pool-bound-", run_id=run_id, worker_ids=[ACTOR])
    opened = RunScheduler(ledger).open_attempt(run_id, item_id, RetryPolicy(2,100,1000), 1, now=now)
    return dict(run_id=run_id, item_id=item_id, attempt_id=opened["attempt_id"],
                scheduler_epoch=opened["scheduler_epoch"], fence_token=opened["fence_token"],
                wake_at=(now-timedelta(seconds=1)).isoformat(timespec="milliseconds").replace("+00:00","Z"),
                opened=opened)


def prepare(case, home, workspace, bank):
    """Author native prerequisites once, on the ruled unscaled private prelude."""
    root = _root(case, home)
    bank, workspace = _private(bank), _private(workspace)
    # The coordinator keeps prepared home, native-preparation bank and the
    # actual Git workspace in sibling directories under one private run bank.
    # Do not pretend its preparation subdirectory is their common parent.
    owned_scope = _private(Path(os.path.commonpath((str(root.tenant_home),str(bank),str(workspace)))))
    from floati.registry import Registry
    for node in (ACTOR, COORDINATOR):
        Registry(root).require_active(node)
    # Preparation is forbidden after padding; the registered prelude has two
    # real memberships. Role/provider records subsequently belong to preface.
    registry_members = [row for row in Registry(root).known_records() if row["kind"] == "registry_entry"]
    _require(len(registry_members) == 2, "native lifecycle prepare must precede all unselected padding")
    context = dict(case=case, actor=ACTOR, session=SESSION, seed_home=str(root.tenant_home),
                   workspace=str(workspace), bank=str(bank), owned_scope=str(owned_scope),
                   constructed_native_preface=True)
    if case in {"node-boot","node-teardown","node-explain","node-prompts"}:
        _roles(root)
    if case in {"wake-arm","node-prep-clear","seat-board"}:
        context["consent"] = _binding(root, workspace, claim=case=="node-prep-clear")
    if case == "wake-resume":
        from floati.wake_control import WakeController
        context["predecessor"] = WakeController(root).pause(ACTOR, SESSION,
            idempotency_key="census-lifecycle-before-resume")["receipt"]
    if case == "node-state-flush":
        path = root.path / "nodes" / ACTOR / "STATE.md"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text("Private native state vessel.\n", encoding="utf-8")
        context.update(state_size=path.stat().st_size, state_mtime_ns=path.stat().st_mtime_ns)
    if case == "reclaim":
        tree = bank / "idle-declared-tree"
        candidate = tree / ".build"
        candidate.mkdir(mode=0o700, parents=True)
        (candidate / "payload.bin").write_bytes(b"private-native-reclaim\n")
        age = time.time() - 48*3600
        for path in (candidate/"payload.bin", candidate, tree):
            os.utime(path, (age,age))
        _write(root.path/"state/seat-onboarding/idle.json", {"home":str(tree)})
        _write(root.path/"state/reclaim-declarations.v0.json", {"schema_version":0,"paths":[".build"]})
        context.update(reclaim_tree=str(tree), candidate=str(candidate),
                       payload_sha256=hashlib.sha256((candidate/"payload.bin").read_bytes()).hexdigest())
    if case in {"lane-open","lane-close"}:
        lanes = bank / "lanes"
        lanes.mkdir(mode=0o700, exist_ok=True)
        _write(root.path/"state/lanes-root.json", {"schema_version":0,"path":str(lanes)})
        _write(root.path/"state/lane-repositories.json", {"schema_version":0,"repositories":{
            "census":{"path":str(workspace),"default_base":"refs/remotes/origin/main"}}})
        if case == "lane-close":
            from floati.lane_workspaces import LaneWorkspaces
            context["opened"] = LaneWorkspaces(root).open(ACTOR, ROW, "census")
    if case in {"wake-evaluate","wake-record","wake-wait"}:
        from floati.events import EventLog
        recipient = ACTOR if case=="wake-wait" else COORDINATOR
        log = EventLog(root)
        messages = [row for row in log.event_records() if row["kind"]=="message_envelope" and row["recipient"]==recipient]
        if not messages:
            reference = workspace/".git/refs/remotes/origin/main"
            sha = reference.read_text(encoding="ascii").strip()
            log.send(COORDINATOR if recipient==ACTOR else ACTOR, recipient, "floati", sha,
                     "note.md", "native selected lifecycle preface", idempotency_key="census-lifecycle-selected")
        context["message"] = _selected(root, recipient)
        context["recipient"] = recipient
        if case == "wake-record":
            from floati.wake_hold import WakeHoldController
            decision = WakeHoldController(root).evaluate(recipient, limit=1,
                idempotency_key="census-lifecycle-decision")
            _require(decision["state"]=="fresh_work", "native preface did not produce fresh wake decision")
            context["decision"] = decision
    if case == "wake-wait":
        # Native Cursor participation must be genuine, not a Codex-role fallback.
        from floati.admin_registry import RegistryAdminBackend
        from floati.provider_switch import ProviderSwitchWizard
        from floati.ids import uuid7_hex
        backend = RegistryAdminBackend(root)
        plan = ProviderSwitchWizard(root, backend,id_factory=uuid7_hex)._plan((ACTOR,"cursor","census-fixture"))
        backend.commit_switch(plan)
        from floati.waiter_consent import arm_waiter_consent
        arm_waiter_consent(root.tenant_home, workspace, ACTOR, "cursor",10,2)
        from floati.codex_wait_contract import (resolve_participant,CodexWaitConsentLedger,CodexWaitSessionLedger)
        participant = resolve_participant(root.tenant_home,workspace,_strict=True)
        consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
        CodexWaitSessionLedger(root).arm(participant.binding,consent,SESSION,idempotency_key="census-cursor-claim")
        context.update(runtime=str(_provider(bank)), stdin={
            "status":"completed","loop_count":0,"conversation_id":SESSION})
    if case == "wake-callback":
        context["callback"] = _prepare_callback(root)
    if case == "wake-daemon-serve":
        from floati.events import EventLog
        _require(EventLog(root).unacked_ids(ACTOR)==[], "daemon idle fixture must have no selected work")
        from floati.wake_daemon_contract import DaemonCoordinate,DaemonConsentLedger,AdapterBindingStore
        from floati.wake_daemon_adapters import adapter_contract_digest
        coordinate = DaemonCoordinate(root,ACTOR,"codex")
        AdapterBindingStore(root).write(coordinate,session_id=SESSION,workspace=workspace,
            executable=_provider(bank),adapter_version="1",adapter_digest=adapter_contract_digest("codex"),binding_epoch=1)
        DaemonConsentLedger(root).consent(coordinate,adapter_version="1",adapter_digest=adapter_contract_digest("codex"),
            min_poll_seconds=1,max_poll_seconds=4,max_backoff_seconds=8,activation_epoch=1,
            idempotency_key="census-daemon-consent")
        context["coordinate_digest"] = coordinate.digest
    return context


def arguments(case, home, workspace, context):
    root = _root(case, home)
    _require(context["case"] == case and context["actor"] == ACTOR, "case context identity differs")
    workspace = _private(workspace)
    common = ["--root",str(root.tenant_home)]
    if case == "wake-daemon-serve": return None
    if case == "reclaim": return ["reclaim",*common]
    if case in {"lane-open","lane-close"}:
        argv = ["lane","open" if case=="lane-open" else "close",*common,"--as",ACTOR,"--row",ROW]
        return argv+["--repo","census"] if case=="lane-open" else argv
    if case in {"wake-pause","wake-resume","wake-arm"}:
        argv = ["wake",case[5:],*common,"--as",ACTOR,"--session",SESSION,
                "--idempotency-key","census-"+case]
        return argv+["--workspace",str(workspace)] if case=="wake-arm" else argv
    if case == "seat-board":
        from floati.seat_declaration import FleetGovernance,SeatDeclaration,COORDINATOR_AUTHORITIES,OWNER_TIERS
        governance = FleetGovernance(1,root.tenant_id,str(root.path),"star",COORDINATOR,
                                     tuple(COORDINATOR_AUTHORITIES),tuple(OWNER_TIERS))
        # Only this owned private marker is rebound between serialized clones.
        marker = workspace/"SEAT.json"
        if marker.exists():
            prior = SeatDeclaration.load(workspace)
            _require(prior.node_id==ACTOR and Path(prior.root).is_relative_to(_private(context["owned_scope"])),
                     "private declaration cannot overwrite a foreign coordinate")
            marker.unlink()
        SeatDeclaration.publish_declared_workspace(workspace,ACTOR,root,governance)
        return ["seat","board",*common,"--as",ACTOR,"--workspace",str(workspace),"--session",SESSION,
                "--idempotency-key","census-seat-board"]
    if case in {"node-boot","node-teardown","node-explain"}:
        declarations = root.tenant_home.parent/"lifecycle-declared-roots.json"
        _write(declarations,{"schema_version":0,"roots":[{"bus_id":"census","root":str(root.tenant_home),
                "architect_node":COORDINATOR,"downstream":[]}]})
        return ["node",case[5:],*common,"--node",ACTOR,"--declared-roots",str(declarations),
                "--managed-executable",str(SOURCE/"scripts/floati"),"--profile","census-lifecycle","--json"]
    if case == "node-state-flush": return ["node","state-flush",*common,"--node",ACTOR]
    if case == "node-prompts":
        return ["node","prompts",*common,"--as",ACTOR,"--harness","codex",
                "--out",str(root.tenant_home.parent/"lifecycle-prompts")]
    if case == "node-prep-clear":
        return ["node","prep-clear",*common,"--as",ACTOR,"--session",SESSION,"--workspace",str(workspace),
                "--repo","floati","--doc","note.md","--note","native lifecycle census checkpoint",
                "--to",COORDINATOR,"--idempotency-key","census-prep-clear"]
    if case == "wake-evaluate":
        return ["wake-evaluate",*common,"--as",context["recipient"],"--limit","1",
                "--idempotency-key","census-wake-evaluate"]
    if case == "wake-record":
        return ["wake-record",*common,"--as",context["recipient"],"--session",SESSION,
                "--id",context["message"]["id"],"--decision",context["decision"]["receipt"]["id"],
                "--outcome","queued","--idempotency-key","census-wake-record"]
    if case == "wake-wait":
        return ["wake","wait",*common,"--as",ACTOR,"--harness","cursor","--runtime",context["runtime"],
                "--deadline-seconds","2","--hook-timeout-seconds","10","--poll-seconds","1","--loop-limit","1"]
    if case == "context-reading-record":
        return ["context","reading","record",*common,"--as",ACTOR,"--metric","self_reported_context_fraction",
                "--value","0.25","--command","/context","--idempotency-key","census-context-reading","--json"]
    if case == "wake-callback":
        request = context["callback"]
        argv = ["wake-callback","--root",str(root.path),"--tenant",root.tenant_id]
        for flag,key in (("--run-id","run_id"),("--item-id","item_id"),("--attempt-id","attempt_id"),
                         ("--wake-at","wake_at"),("--scheduler-epoch","scheduler_epoch"),("--fence-token","fence_token")):
            argv.extend((flag,str(request[key])))
        return argv
    raise Unresolved("native arguments missing for "+case)


def execute_seam(case, home, workspace, context):
    _require(case=="wake-daemon-serve", "only the full one-iteration daemon serve seam is authorized")
    root = _root(case, home)
    _require(str(root.tenant_home)==context["seed_home"],
             "root-bound native daemon consent cannot be copied to a foreign coordinate; use the ruled stable root")
    from floati.wake_daemon import WakeDaemon,DaemonOwner
    from floati.wake_daemon_contract import DaemonCoordinate
    from floati.wake_daemon_adapters import wake_adapter_for
    coordinate = DaemonCoordinate(root,ACTOR,"codex")
    _require(coordinate.digest==context["coordinate_digest"],
             "root-bound native daemon consent cannot be copied to a foreign coordinate; use the ruled stable root")
    daemon = WakeDaemon(coordinate,wake_adapter_for(root,ACTOR,"codex"))
    checks = 0
    def stop_requested():
        nonlocal checks
        checks += 1
        return checks > 1
    daemon.serve(stop_requested)
    runtime = daemon.read_runtime()
    _require(checks==2 and runtime["last_state"]=="idle" and runtime["cycle_index"]==1,
             "native daemon did not complete exactly one genuine idle poll")
    with DaemonOwner(coordinate):
        pass
    return 0,dict(artifact_version=0,command="wake-daemon-serve",status="ok",
                  evidence={"runtime":runtime,"completed_native_iterations":1,"owner_reacquired_after":True})


# Every admitted durable namespace uses its real native kind vocabulary.
_KINDS = {
    "events.jsonl": {"message_envelope","send_readiness_observation"},
    "nodes/scale-worker/lanes.jsonl": {"lane_workspace_record"},
    "receipts/wake-control/scale-worker.jsonl": {"wake_control_receipt"},
    "receipts/codex-wait-session/scale-worker.jsonl": {"codex_wait_session_receipt"},
    "receipts/seat-board/scale-worker.jsonl": {"seat_board_receipt"},
    "receipts/prep-clear/scale-worker.jsonl": {"prep_clear_receipt"},
    "receipts/deliveries/architect.jsonl": {"delivery_receipt","delivered_unpresented","wake_hold_receipt","wake_attempt_receipt"},
    "receipts/wakes/architect.jsonl": {"wake_attempt_receipt"},
    "receipts/tide-testimony/scale-worker.jsonl": {"tide_testimony_record"},
    "receipts/deliveries/scale-worker.jsonl": {"delivery_receipt","delivered_unpresented","wake_hold_receipt"},
    "receipts/cursor-presentations/scale-worker.jsonl": {"cursor_presentation_receipt"},
    "receipts/stop-holds/scale-worker.jsonl": {"stop_hold_receipt"},
    "receipts/wake-daemon/scale-worker.jsonl": {"wake_daemon_lifecycle_receipt"},
    "receipts/timings/wake-resume.jsonl": {"timing_receipt"},
    "receipts/timings/seat-board.jsonl": {"timing_receipt"},
}
_REQUIRED = {
    "lane-open": ("nodes/scale-worker/lanes.jsonl","lane_workspace_record",1),
    "lane-close": ("nodes/scale-worker/lanes.jsonl","lane_workspace_record",1),
    "wake-pause": ("receipts/wake-control/scale-worker.jsonl","wake_control_receipt",1),
    "wake-resume": ("receipts/wake-control/scale-worker.jsonl","wake_control_receipt",1),
    "wake-arm": ("receipts/codex-wait-session/scale-worker.jsonl","codex_wait_session_receipt",1),
    "seat-board": ("receipts/seat-board/scale-worker.jsonl","seat_board_receipt",5),
    "node-prep-clear": ("receipts/prep-clear/scale-worker.jsonl","prep_clear_receipt",1),
    "wake-evaluate": ("receipts/deliveries/architect.jsonl","wake_hold_receipt",1),
    "wake-record": ("receipts/wakes/architect.jsonl","wake_attempt_receipt",1),
    "context-reading-record": ("receipts/tide-testimony/scale-worker.jsonl","tide_testimony_record",1),
}
_EFFECTS = {
    "reclaim":"reclaim_plan_returned","lane-open":"lane_workspace_opened","lane-close":"lane_workspace_closed",
    "wake-evaluate":"wake_decision_returned","wake-record":"wake_attempt_recorded","node-boot":"node_boot_returned",
    "node-teardown":"node_teardown_returned","node-explain":"node_explanation_returned",
    "node-prep-clear":"prep_clear_receipted","node-state-flush":"state_flush_receipted",
    "node-prompts":"lifecycle_prompts_written","seat-board":"seat_board_receipted","wake-pause":"wake_paused",
    "wake-resume":"wake_resumed","wake-arm":"wake_armed","wake-wait":"cursor_followup_returned",
    "context-reading-record":"context_reading_appended","wake-callback":"wake_callback_receipted",
    "wake-daemon-serve":"daemon_poll_completed",
}


def _frames(root, path, data):
    from floati.framing import decode_frames
    from floati.records import validate_record
    _require(path in _KINDS, "unreviewed native lifecycle ledger: "+path)
    rows = decode_frames(data)
    for row in rows:
        validate_record(row,root.tenant_id,_KINDS[path],integrity=True)
    return rows


def _appended(root, before, path):
    _require(isinstance(before,dict) and all(isinstance(value,bytes) for value in before.values()),
             "witness needs the retained immediate raw-file inventory")
    old = before.get(path,b"")
    after = root.resolve_relative(path).read_bytes()
    _require(after.startswith(old), "native lifecycle ledger rewrote its retained prefix")
    _frames(root,path,after)
    return _frames(root,path,after[len(old):])


def witness(case, home, before, artifact, context, detail, *, delivery_prefix_input=None,
            registry_prefix_input=None, inbox_prefix_input=None, inbox_append_input=None, cut_input=None):
    """Prove positive native work; a refusal/no-op cannot masquerade as it."""
    root = _root(case,home)
    _require(context["case"]==case, "native lifecycle context case mismatch")
    after = snapshot(home)
    from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import raw_candidate_delta
    _hint_effects(case, root, before, after, raw_candidate_delta(before, after))
    registry_paths = set()
    inbox_paths = set()
    from _bus_scale_fixture_proofs.bus_scale_cut_prefix_effects import CACHE as CUT_CACHE, verify_cut_prefix_effect
    if any((path==CUT_CACHE or path.startswith(CUT_CACHE+"/"))
           and before.get(path)!=after.get(path) for path in set(before)|set(after)):
        cut_proof = verify_cut_prefix_effect(home,before,after,raw_candidate_delta(before,after),cut_input)
        inbox_paths.update(cut_proof["changed_files"])
    if any((path==".registry-prefix" or path.startswith(".registry-prefix/"))
           and before.get(path)!=after.get(path) for path in set(before)|set(after)):
        from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import verify_registry_prefix_effect
        from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import raw_candidate_delta
        registry_proof = verify_registry_prefix_effect(home,before,after,
            raw_candidate_delta(before,after),registry_prefix_input)
        registry_paths.update(registry_proof["changed_files"])
    from floati.inbox_prefix import storage_declaration
    inbox_storage = storage_declaration()
    inbox_name, inbox_source = inbox_storage["index"]["path"], inbox_storage["source"]["path"]
    if (inbox_append_input is not None and before.get(inbox_source)!=after.get(inbox_source)) or any((path==inbox_name or path.startswith(inbox_name+"/"))
           and before.get(path)!=after.get(path) for path in set(before)|set(after)):
        from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import verify_inbox_prefix_effect
        from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import verify_inbox_prefix_append_effect
        from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import raw_candidate_delta
        if case=="node-prep-clear":
            _require(inbox_prefix_input is None,"prep-clear requires the explicit inbox append declaration")
            inbox_proof = verify_inbox_prefix_append_effect(home,before,after,
                raw_candidate_delta(before,after),inbox_append_input)
        else:
            _require(inbox_append_input is None,"inbox append declaration belongs only to prep-clear")
            inbox_proof = verify_inbox_prefix_effect(home,before,after,
                raw_candidate_delta(before,after),inbox_prefix_input)
        inbox_paths.update(inbox_proof["changed_files"])
    if case == "wake-wait":
        if delivery_prefix_input is not None:
            from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import verify_delivery_prefix_effect, raw_candidate_delta
            after = snapshot(home)
            verify_delivery_prefix_effect(home, before, after, raw_candidate_delta(before, after),
                delivery_prefix_input, node=ACTOR)
        from floati.cursor_presentations import CursorPresentationLedger,notification
        body = artifact.get("evidence",artifact)
        expected = notification(root.tenant_home,ACTOR,Path(context["runtime"]),SESSION,[context["message"]])
        _equal(body,expected,"actual native Cursor followup body")
        rows = CursorPresentationLedger(root).read(ACTOR)
        flushed = [row for row in rows if row["phase"]=="flushed" and row["acting_session_id"]==SESSION]
        _require(len(flushed)==1 and flushed[0]["item_ids"]==[context["message"]["id"]],
                 "native Cursor publication did not flush the exact selected envelope")
        return dict(exit_code=0,status="ok",effect={"kind":_EFFECTS[case],"count":1})
    _require(artifact.get("artifact_version")==0 and artifact.get("status")=="ok"
             and isinstance(artifact.get("evidence"),dict), "native lifecycle command did not return successful work")
    expected_command = "wake-daemon-serve" if case=="wake-daemon-serve" else (
        "lane" if case.startswith("lane-") else "node" if case.startswith("node-") else
        "seat" if case=="seat-board" else "context" if case=="context-reading-record" else
        "wake" if case in {"wake-arm","wake-pause","wake-resume"} else case)
    _equal(artifact["command"],expected_command,"actual native lifecycle command")
    evidence = artifact["evidence"]
    rows = []
    if case in _REQUIRED:
        path,kind,count = _REQUIRED[case]
        rows = [row for row in _appended(root,before,path) if row["kind"]==kind]
        _require(len(rows)==count,"native selected work did not append its exact positive receipt count")
    if case in {"lane-open","lane-close"}:
        _equal(evidence["record"],rows[0],"complete native lane receipt")
        _equal(rows[0]["node_id"],ACTOR,"lane owner")
        _equal(rows[0]["row"],ROW,"lane row")
        workspace = Path(evidence["workspace"] if case=="lane-open" else evidence["removed"])
        _require(workspace.is_dir() if case=="lane-open" else not workspace.exists(),
                 "native lane workspace state differs from its receipt")
    elif case in {"wake-pause","wake-resume"}:
        from floati.wake_control import WakeController
        _equal(evidence["receipt"],rows[0],"complete native wake control receipt")
        _equal(rows[0]["operation"],"pause" if case=="wake-pause" else "resume","wake operation")
        expected_state = "paused" if case=="wake-pause" else "active"
        _equal(WakeController(root).status(ACTOR,SESSION)["state"],expected_state,"native marker state")
        _equal(evidence["state"],expected_state,"wake stdout state")
        _equal(evidence,WakeController(root)._artifact(node=ACTOR,session_digest=rows[0]["session_digest"],
            state=expected_state,marker=Path(evidence["marker"]),receipt=rows[0],
            paused_at=rows[0]["timestamp"] if case=="wake-pause" else None),"complete native wake command artifact")
    elif case == "wake-arm":
        _equal(evidence,rows[0],"complete native acting-session receipt")
        _equal(rows[0]["acting_session_id"],SESSION,"acting session")
        _equal(rows[0]["state"],"armed","native claim state")
    elif case == "seat-board":
        from floati.seat_board import SeatBoard
        _equal([row["step"] for row in rows],["started","armed","resumed","drain_started","completed"],
               "complete native boarding steps")
        _equal(evidence,SeatBoard(root)._result(rows[-1],workspace=str(context["workspace"])),
               "complete frozen native boarding result")
        _equal(evidence["item_ids"],[],"quiet selected boarding work")
    elif case == "node-prep-clear":
        _equal(evidence,rows[0],"complete native wind-down receipt")
        _equal(rows[0]["wake_release_outcome"],"released","actual held claim release")
        events = _appended(root,before,"events.jsonl")
        envelopes = [row for row in events if row["kind"]=="message_envelope"]
        _require(len(envelopes)==1 and envelopes[0]["id"]==evidence["envelope_id"],
                 "wind-down lacks its exact native checkpoint envelope")
    elif case == "wake-evaluate":
        from floati.wake_hold import validate_wake_decision_artifact
        validate_wake_decision_artifact(evidence,tenant_id=root.tenant_id)
        _equal(evidence["receipt"],rows[0],"complete native wake decision")
        _equal(rows[0]["item_ids"],[context["message"]["id"]],"selected common-prefix envelope")
        _equal(evidence["state"],"fresh_work","positive native wake work")
    elif case == "wake-record":
        _equal(evidence,rows[0],"complete native wake attempt")
        _equal(evidence["outcome"],"queued","native external prompt outcome")
        _equal(evidence["decision_receipt_id"],context["decision"]["receipt"]["id"],"native decision binding")
    elif case == "context-reading-record":
        from floati.tide_policy import normalize_threshold
        from floati.tide_catalog import metric_for
        _equal(evidence,rows[0],"complete native class-B reading")
        _equal(evidence["metric"],"self_reported_context_fraction","native context metric")
        _equal(evidence["value"],normalize_threshold("0.25",metric_for("codex",evidence["metric"])),
               "native measured context fraction")
    elif case == "node-state-flush":
        from floati.state_receipts import _validate_receipt
        _validate_receipt(evidence)
        path = root.path/"nodes"/ACTOR/"STATE.md"
        _equal(evidence["observed_size_bytes"],path.stat().st_size,"actual state vessel size")
        _equal(evidence["observed_mtime_ns"],path.stat().st_mtime_ns,"actual state vessel mtime")
        current = snapshot(home)
        _require({path:raw for path,raw in current.items() if path not in registry_paths|inbox_paths}
                 == {path:raw for path,raw in before.items() if path not in registry_paths|inbox_paths},
                 "metadata-only state observation changed root bytes")
    elif case in {"node-boot","node-teardown","node-explain"}:
        from floati import admin_cli
        from floati.node_projections import NodeBootProjection,NodeTeardownProjection
        from floati.node_explain import NodeExplainProjection
        from types import SimpleNamespace
        declarations = root.tenant_home.parent/"lifecycle-declared-roots.json"
        args = SimpleNamespace(declared_roots=str(declarations),
            managed_executable=str(SOURCE/"scripts/floati"),profile="census-lifecycle")
        source = admin_cli._projection_source(args,root)
        projection = {"node-boot":NodeBootProjection,"node-teardown":NodeTeardownProjection,
                      "node-explain":NodeExplainProjection}[case]
        _equal(evidence,projection(root,ACTOR,source,admin_cli._templates(root)).project(),
               "complete native lifecycle projection")
    elif case == "node-prompts":
        from floati.admin_registry import RegistryAdminBackend
        from floati.role_library import RoleTemplateLibrary
        from floati.lifecycle_prompts import (HARNESS_ADAPTERS,LIFECYCLE_VERBS,DASH_RECEIPTS,
            SEAT_COMMAND_HARNESSES,_header,_file_stem,_verb_body,_seat_command_contents,harbor_sha)
        out = root.tenant_home.parent/"lifecycle-prompts"
        _equal(evidence["out"],str(out),"native prompt publication coordinate")
        _equal(evidence["node"],ACTOR,"native prompt node")
        _equal(evidence["harness"],"codex","native prompt harness")
        _equal(set(path.name for path in out.iterdir()),{row["path"] for row in evidence["files"]},
               "complete published lifecycle file population")
        adapter = HARNESS_ADAPTERS["codex"]
        role = RegistryAdminBackend(root).role_record(ACTOR)
        template = RoleTemplateLibrary(root).templates()[role["template_role"]]
        files,parity = [],{}
        for verb in LIFECYCLE_VERBS:
            parity[verb] = ("file:boot-seat.md" if verb=="boot" and "codex" in SEAT_COMMAND_HARNESSES else
                           "file:"+_file_stem(adapter,verb,ACTOR)+".md" if verb in adapter["verbs"]
                           else DASH_RECEIPTS[("codex",verb)])
        for verb in adapter["verbs"]:
            filename = _file_stem(adapter,verb,ACTOR)+".md"
            parity.setdefault(verb,"file:"+filename)
            if verb=="boot" and "codex" in SEAT_COMMAND_HARNESSES:
                continue
            content = "\n".join([_header("codex "+verb+" prompt",harbor_sha()),"",
                _verb_body(verb,ACTOR,role,template,str(root.tenant_home)),""])+"\n"
            _require((out/filename).read_bytes()==content.encode(),"native prompt content differs from its source projection")
            files.append({"path":filename,"sha256":hashlib.sha256(content.encode()).hexdigest()})
        if "codex" in SEAT_COMMAND_HARNESSES:
            for filename,content in _seat_command_contents("codex",harbor_sha()).items():
                _require((out/filename).read_bytes()==content.encode(),"native shared prompt content differs from its source projection")
                files.append({"path":filename,"sha256":hashlib.sha256(content.encode()).hexdigest()})
            parity["teardown"]="file:teardown-seat.md"
        _equal(evidence["files"],sorted(files,key=lambda row:row["path"]),"complete native prompt publication")
        _equal(evidence["parity"],{"codex":parity},"complete native prompt parity")
    elif case == "reclaim":
        candidate = Path(context["candidate"])
        _require(evidence["dry_run"] is True and evidence["deleted"]==[]
                 and evidence["df_moved_bytes"]==0,"native reclaim performed deletion or lost dry-run testimony")
        _require(any(row["path"]==str(candidate) for row in evidence["listed"]),
                 "native reclaim did not identify the actual idle declared candidate")
        _equal(hashlib.sha256((candidate/"payload.bin").read_bytes()).hexdigest(),context["payload_sha256"],
               "reclaim retained actual payload bytes")
    elif case == "wake-callback":
        from floati.runtruth import RunLedger
        opened = context["callback"]["opened"]
        run = RunLedger(root).project().run(context["callback"]["run_id"])
        _equal(run["attempts"][opened["attempt_id"]]["opened"],opened,"native current callback fence")
        _equal(evidence,{"state":"replayed","records":[]},"complete canonical native callback replay")
    elif case == "wake-daemon-serve":
        from floati.wake_daemon_contract import DaemonCoordinate,lifecycle_relative
        from floati.wake_daemon import WakeDaemon
        from floati.wake_daemon_adapters import wake_adapter_for
        coordinate = DaemonCoordinate(root,ACTOR,"codex")
        daemon = WakeDaemon(coordinate,wake_adapter_for(root,ACTOR,"codex"))
        _equal(evidence["runtime"],daemon.read_runtime(),"complete native daemon runtime")
        _require(evidence["runtime"]["last_state"]=="idle" and evidence["runtime"]["cycle_index"]==1
                 and evidence["completed_native_iterations"]==1 and evidence["owner_reacquired_after"] is True,
                 "native daemon did not complete one quiet owned poll")
    else:
        raise Unresolved("native witness missing for "+case)
    return dict(exit_code=0,status="ok",effect={"kind":_EFFECTS[case],"count":1})


def _object(data):
    def unique(pairs):
        result = {}
        for key,value in pairs:
            _require(key not in result,"native JSON repeats a field")
            result[key] = value
        return result
    result = json.loads(data,object_pairs_hook=unique)
    _json(result)
    return result


def _stamp(value, begin, end, *, seconds=False):
    pattern = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d" + (r"(?:\.\d{3})?Z" if seconds else r"\.\d{3}Z")
    _require(type(value) is str and re.fullmatch(pattern,value),"native generated clock has a foreign encoding")
    moment = datetime.fromisoformat(value[:-1]+"+00:00")
    precision = timedelta(seconds=1) if seconds else timedelta(milliseconds=1)
    _require(begin-precision<=moment<=end,"native generated clock is outside the retained invocation")
    return moment


def _generated(identifier, begin, end):
    _require(type(identifier) is str and re.fullmatch(r"[a-z][a-z-]*-[0-9a-f]{32}",identifier),
             "native generated identity has a foreign prefix or encoding")
    parsed = uuid.UUID(hex=identifier.rsplit("-",1)[1])
    _require(parsed.version==7 and parsed.variant==uuid.RFC_4122,"native generated identity is not UUIDv7")
    moment = datetime.fromtimestamp((parsed.int>>80)/1000,timezone.utc)
    _require(begin-timedelta(milliseconds=1)<=moment<=end,"native generated identity is outside the retained invocation")


def _mutable_paths(case, root):
    session_digest = hashlib.sha256(SESSION.encode()).hexdigest()
    marker = "state/wake-control/"+ACTOR+"/"+session_digest+".json"
    if case in {"wake-pause","wake-resume","node-prep-clear"}: return {marker}
    if case=="wake-wait": return {"state/cursor-wait/scale-worker/presentation-history.v1.json",
        "state/waiter-invocations/"+ACTOR+"/"+session_digest+".json"}
    if case=="wake-daemon-serve":
        from floati.wake_daemon_contract import DaemonCoordinate
        return {"state/wake-daemon/runtime/"+DaemonCoordinate(root,ACTOR,"codex").digest+".json"}
    return set()


def _append_paths(case):
    if case in _REQUIRED:
        result = {_REQUIRED[case][0]}
        if case=="seat-board": result.update({"receipts/codex-wait-session/scale-worker.jsonl","receipts/wake-control/scale-worker.jsonl"})
        if case in {"wake-resume","seat-board"}: result.add("receipts/timings/"+case+".jsonl")
        if case=="node-prep-clear": result.update({"events.jsonl","receipts/wake-control/scale-worker.jsonl"})
        return result
    if case=="wake-wait": return {"receipts/cursor-presentations/scale-worker.jsonl",
        "receipts/deliveries/scale-worker.jsonl","receipts/stop-holds/scale-worker.jsonl",
        "state/cursor-wait/scale-worker/journal.jsonl"}
    if case=="wake-daemon-serve": return {"receipts/wake-daemon/scale-worker.jsonl"}
    return set()


def _hint_effects(case, root, before, after, delta):
    from _bus_scale_fixture_proofs.bus_scale_wake_hint_effects import verify_wake_hint_effects
    return verify_wake_hint_effects(root, before, after, delta,
        ledger_kinds={path: _KINDS[path] for path in _append_paths(case) if path in _KINDS})


def normalize(case, delta, artifact, context, **bounds):
    """Bind complete native outputs, then audit each changed scalar leaf.

    Deletion/replacement requires the real immediate ``before_raw_files``
    inventory. No clock, identity, path or CPU value is removed from raw data.
    Unknown mutable state is UNRESOLVED rather than silently scrubbed.
    """
    from floati.jsonl import MAX_LEDGER_BYTES
    from floati.framing import encode_frame
    home = Path(bounds.get("home",context["seed_home"]))
    root = _root(case,home)
    begin,end = bounds.get("wall_begin"),bounds.get("wall_end")
    _require(isinstance(begin,datetime) and isinstance(end,datetime)
             and begin.tzinfo is not None and end.tzinfo is not None and begin<=end
             and type(bounds.get("child_pid")) is int and bounds["child_pid"]>0,
             "lifecycle normalization requires retained invocation walls and child identity")
    _require(type(delta) is dict,"native lifecycle delta is not an object")
    after = snapshot(home)
    retained = bounds.get("before_raw_files")
    _require(retained is None or (type(retained) is dict and all(type(p) is str and type(v) is bytes
             for p,v in retained.items())),"retained lifecycle inventory is malformed")
    before = dict(after) if retained is None else dict(retained)
    hint_semantic = _hint_effects(case, root, retained, after, delta)
    invocation = None
    semantic = {}
    mutable = _mutable_paths(case,root)
    cache_paths = set()
    from _bus_scale_fixture_proofs.bus_scale_cut_prefix_effects import CACHE as CUT_CACHE, SUMMARY as CUT_SUMMARY, verify_cut_prefix_effect
    if any(path==CUT_CACHE or path.startswith(CUT_CACHE+"/") for path in delta):
        _require(retained is not None,"native cut effect needs immediate before bytes")
        cut_proof = verify_cut_prefix_effect(home,before,after,delta,bounds.get("cut_input"))
        cache_paths.update(cut_proof["changed_files"])
        semantic[CUT_SUMMARY] = cut_proof["semantic"]
    registry_changed = {path for path in delta
        if type(path) is str and (path==".registry-prefix" or path.startswith(".registry-prefix/"))}
    if registry_changed:
        from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import SUMMARY as REGISTRY_SUMMARY, verify_registry_prefix_effect
        _require(retained is not None,"native registry cache effect needs immediate before bytes")
        registry_proof = verify_registry_prefix_effect(home,before,after,delta,bounds.get("registry_prefix_input"))
        cache_paths.update(registry_proof["changed_files"])
        semantic[REGISTRY_SUMMARY] = {"operation":"native_cache_rebuild","kind":"complete_native_registry_prefix"}
    from floati.inbox_prefix import storage_declaration
    inbox_storage = storage_declaration()
    inbox_name, inbox_source = inbox_storage["index"]["path"], inbox_storage["source"]["path"]
    inbox_changed = {path for path in delta
        if type(path) is str and (path==inbox_name or path.startswith(inbox_name+"/"))}
    if inbox_changed or (bounds.get("inbox_append_input") is not None
                         and before.get(inbox_source)!=after.get(inbox_source)):
        from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import SUMMARY as INBOX_SUMMARY, TAG, verify_inbox_prefix_effect
        _require(retained is not None,"native inbox index effect needs immediate before bytes")
        if case=="node-prep-clear":
            from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import verify_inbox_prefix_append_effect
            inbox_proof = verify_inbox_prefix_append_effect(home,before,after,delta,bounds.get("inbox_append_input"))
        else:
            inbox_proof = verify_inbox_prefix_effect(home,before,after,delta,bounds.get("inbox_prefix_input"))
        cache_paths.update(inbox_proof["changed_files"])
        semantic[INBOX_SUMMARY] = inbox_proof["semantic"] if case=="node-prep-clear" else dict(TAG)
    if case=="node-prep-clear":
        from _bus_scale_fixture_proofs.bus_scale_prefix_effects import SUMMARY, verify_prefix_effect
        _require(retained is not None,"native cache effect needs immediate before bytes")
        proof = verify_prefix_effect(home,before,after,delta,bounds.get("prefix_input"))
        cache_paths.update(proof["changed_files"])
        semantic[SUMMARY] = proof["semantic"]
    if case=="wake-wait" and bounds.get("delivery_prefix_input") is not None:
        from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import SUMMARY_DELIVERY, verify_delivery_prefix_effect
        _require(retained is not None,"native delivery cache effect needs immediate before bytes")
        proof = verify_delivery_prefix_effect(home,before,after,delta,bounds["delivery_prefix_input"],node=ACTOR)
        cache_paths.update(proof["changed_files"])
        semantic[SUMMARY_DELIVERY] = proof["semantic"]
    for path,change in delta.items():
        if path in cache_paths:
            continue
        _require(type(path) is str and Path(path).as_posix()==path and not Path(path).is_absolute()
                 and ".." not in Path(path).parts,"native delta path is not canonical and relative")
        _require(type(change) is dict and set(change)=={
            "operation","before_sha256","after_sha256","before_bytes","after_bytes","payload_base64"},
                 "lifecycle raw delta shape differs")
        for field in ("before_bytes","after_bytes"):
            _require(type(change[field]) is int and 0<=change[field]<=MAX_LEDGER_BYTES,"native delta length is unbounded")
        for field in ("before_sha256","after_sha256"):
            _require(type(change[field]) is str and re.fullmatch(r"[0-9a-f]{64}",change[field]),"native delta digest is malformed")
        payload = base64.b64decode(change["payload_base64"],validate=True)
        _require(base64.b64encode(payload).decode()==change["payload_base64"],"native delta payload is not canonical base64")
        current,operation = after.get(path),change["operation"]
        current_bytes = b"" if current is None else current
        _require(len(current_bytes)==change["after_bytes"]
                 and hashlib.sha256(current_bytes).hexdigest()==change["after_sha256"],
                 "lifecycle delta does not bind current raw bytes")
        count = change["before_bytes"]
        if retained is None:
            _require(operation not in {"delete","remove"} and (operation!="replace" or count==0),
                     "native deletion/replacement needs the retained immediate raw-file inventory")
            old = current_bytes[:count] if operation!="replace" else b""
            if count: before[path]=old
            else: before.pop(path,None)
        else:
            old = retained.get(path,b"")
        _require(len(old)==count and hashlib.sha256(old).hexdigest()==change["before_sha256"],
                 "lifecycle delta does not bind retained pre-invocation bytes")
        if path in hint_semantic:
            semantic[path] = hint_semantic[path]
        elif operation=="create_lock":
            _require(path.endswith(".lock") and current==b"" and not old and not payload,
                     "native new lock is not an empty ordinary lock")
            semantic[path]={"operation":"create_lock"}
        elif operation=="append":
            _require(path in _append_paths(case),"case changed an unreviewed native lifecycle ledger: "+path)
            _require(current is not None and current==old+payload,"native append rewrote its retained prefix")
            if case=="wake-wait" and path=="state/cursor-wait/scale-worker/journal.jsonl":
                rows = [_object(line) for line in payload.splitlines()]
                _require(len(rows)==1 and rows[0].get("publication")=="flushed"
                         and rows[0].get("node")==ACTOR and rows[0].get("conversation_id")==SESSION,
                         "Cursor journal does not retain one native successful publication")
                semantic[path]={"operation":"append","journal":rows}
            else:
                _frames(root,path,old)
                rows = _frames(root,path,current)
                frames = _frames(root,path,payload)
                _require(b"".join(encode_frame(row) for row in rows)==current,
                         "native lifecycle framing is not canonical")
                semantic[path]={"operation":"append","frames":frames}
        elif operation=="replace":
            _require(path in mutable and current is not None and payload==current,
                     "unreviewed native lifecycle mutable JSON: "+path)
            semantic[path]={"operation":"replace","value":_object(current)}
        elif operation in {"delete","remove"}:
            _require(case=="wake-resume" and path in mutable and current is None and not payload and bool(old),
                     "unreviewed native lifecycle deletion: "+path)
            semantic[path]={"operation":"delete","before":_object(old)}
        else:
            raise Unresolved("unreviewed native lifecycle delta operation: "+str(operation))
    changed = {path for path in set(before)|set(after) if before.get(path)!=after.get(path)}
    _equal(set(delta),changed,"complete native lifecycle raw change population")
    witness(case,home,before,artifact,context,{},
            registry_prefix_input=bounds.get("registry_prefix_input"),
            inbox_prefix_input=bounds.get("inbox_prefix_input"),
            inbox_append_input=bounds.get("inbox_append_input"),
            cut_input=bounds.get("cut_input"),
            **({"delivery_prefix_input":bounds["delivery_prefix_input"]}
               if bounds.get("delivery_prefix_input") is not None else {}))
    if case=="wake-wait":
        # Mail already present at entry returns before the native empty-wait
        # hold starts. The complete selected delivery witness above is required
        # before admitting its terminal, deliberately unheld invocation lease.
        from _bus_scale_fixture_proofs.bus_scale_waiter_invocation_effects import verify_waiter_invocation
        invocation = verify_waiter_invocation(root,ACTOR,SESSION,bounds["workspace"],retained,after,
            child_pid=bounds["child_pid"],wall_begin=begin,wall_end=end,allow_unheld=True)
    result = {"artifact":copy.deepcopy(artifact),"delta":semantic,"normalizations":[]}
    if case=="node-prompts":
        out = home.parent/"lifecycle-prompts"
        result["delta"]["@publication/lifecycle-prompts"]={"operation":"publish","files":{
            row["path"]:(out/row["path"]).read_text(encoding="utf-8") for row in artifact["evidence"]["files"]}}
    original = copy.deepcopy(result)
    def leaf(parts,replacement,reason):
        target = result
        for part in parts[:-1]: target=target[part]
        old = target[parts[-1]]
        _require(not isinstance(old,(dict,list)),"normalization must identify one scalar leaf")
        if _json(old)==_json(replacement): return
        target[parts[-1]]=replacement
        pointer = "/"+"/".join(str(part).replace("~","~0").replace("/","~1") for part in parts)
        _require(all(audit["pointer"]!=pointer for audit in result["normalizations"]),"duplicate normalization coordinate")
        result["normalizations"].append({"pointer":pointer,"reason":reason})
    generated,clocks = {},set()
    def admit_record(row,path,ordinal):
        _generated(row["id"],begin,end)
        _stamp(row["timestamp"],begin,end,seconds=row["kind"]=="stop_hold_receipt")
        _require(row["id"] not in generated,"native generated receipt identity is reused")
        generated[row["id"]]="<generated-"+row["kind"]+"-"+path+"-"+str(ordinal)+">"
        clocks.add(row["timestamp"])
    for path,change in semantic.items():
        for ordinal,row in enumerate(change.get("frames",[])): admit_record(row,path,ordinal)
        for row in change.get("journal",[]):
            for key in ("ts","published_at"):
                _stamp(row[key],begin,end,seconds=True)
                clocks.add(row[key])
    if case=="node-prep-clear":
        from floati.events import validate_event_records
        frames=semantic["events.jsonl"]["frames"]
        _require([row["kind"] for row in frames]==["message_envelope","send_readiness_observation"],
                 "native prep-clear must append its exact envelope/readiness pair")
        validate_event_records(_frames(root,"events.jsonl",after["events.jsonl"]))
        readiness=frames[1]
        _equal(readiness["envelope_id"],artifact["evidence"]["envelope_id"],"native prep-clear readiness envelope")
        _stamp(readiness["observed_at"],begin,end)
    timing="receipts/timings/"+case+".jsonl"
    if case in {"wake-resume","seat-board"}:
        from floati.timings import argv_digest
        argv=(["wake","resume","--root",str(home),"--as",ACTOR,"--session",SESSION,
               "--idempotency-key","census-wake-resume"] if case=="wake-resume" else
              ["seat","board","--root",str(home),"--as",ACTOR,"--workspace",str(bounds["workspace"]),
               "--session",SESSION,"--idempotency-key","census-seat-board"])
        rows=semantic[timing]["frames"]
        _require(len(rows)==1,"instrumented native lifecycle must append one timing receipt")
        row=rows[0]
        _equal(row["command"],"wake resume" if case=="wake-resume" else "seat board","native timed command")
        _equal(row["argv_digest"],argv_digest(argv,root.path),"complete native lifecycle timing arguments")
        _equal(row["node_id"],ACTOR,"native timing actor")
        _require(row["outcome"]=="ok" and row["refusal_code"] is None,"native timing records a refusal")
        _stamp(row["started_at"],begin,end)
        clocks.add(row["started_at"])
        cpu=bounds.get("child_cpu_seconds")
        for field in ("cpu_seconds","wall_seconds"):
            value=row[field]
            _require(type(value) in (int,float) and math.isfinite(value) and value>=0 and value==round(value,3),
                     "native timing is not finite millisecond testimony")
        _require(row["wall_seconds"]<=(end-begin).total_seconds()+.001
                 and type(cpu) in (int,float) and math.isfinite(cpu) and cpu>=0 and row["cpu_seconds"]<=cpu+.001,
                 "native timing exceeds collected invocation CPU/wall")
    if case=="node-state-flush": admit_record(artifact["evidence"],"stdout",0)
    if case=="reclaim":
        for collection in ("listed","kept"):
            for row in artifact["evidence"][collection]:
                _stamp(row["utc"],begin,end)
                clocks.add(row["utc"])
    if case=="wake-daemon-serve":
        runtime = artifact["evidence"]["runtime"]
        identity = runtime["daemon_instance_id"]
        _generated(identity,begin,end)
        generated[identity]="<native-daemon-instance>"
        _require(runtime["coordinate_digest"]==context["coordinate_digest"],"stable-root daemon coordinate changed")
    if case=="wake-wait":
        from floati.cursor_presentations import CursorPresentationLedger
        token = invocation["value"]["token"]
        generated[token]="<native-waiter-invocation-token>"
        plane = CursorPresentationLedger(root)
        value = semantic[plane.witness_relative(ACTOR).as_posix()]["value"]
        _generated(value["genesis_id"],begin,end)
        generated[value["genesis_id"]]="<native-presentation-genesis>"
        raw = after[plane.relative(ACTOR).as_posix()]
        _require(value["before_bytes"]<=len(raw)
                 and value["before_digest"]==hashlib.sha256(raw[:value["before_bytes"]]).hexdigest(),
                 "native presentation witness does not bind its exact retained prefix")
    def transform(value,parts):
        if type(value) is dict:
            for key,child in list(value.items()): transform(child,[*parts,key])
        elif type(value) is list:
            for index,child in enumerate(list(value)): transform(child,[*parts,index])
        elif type(value) is str:
            replacement = value
            if value in generated: replacement=generated[value]
            elif value in clocks and parts[-1] in {"timestamp","paused_at","opened_at","closed_at","started_at","ts","published_at","utc"}:
                replacement="<native-invocation-clock>"
            if str(home) in replacement:
                replacement=replacement.replace(str(home),"<bus-scale-fixture>")
            if case=="node-prompts" and parts==["artifact","evidence","out"]:
                _equal(value,str(home.parent/"lifecycle-prompts"),"exact native prompt publication coordinate")
                replacement="<native-lifecycle-prompt-publication>"
            if case=="wake-pause" and parts==["artifact","evidence","display"]:
                replacement=value.replace(artifact["evidence"]["paused_at"],"<native-invocation-clock>")
            if case=="wake-daemon-serve" and parts[-1]=="idempotency_key":
                identity=artifact["evidence"]["runtime"]["daemon_instance_id"]
                if replacement.startswith(identity+"-"):
                    replacement=generated[identity]+replacement[len(identity):]
            if replacement!=value:
                leaf(parts,replacement,"native source effect, exact generated identity/clock or owned root coordinate validated; raw retained")
    transform(result["artifact"],["artifact"])
    transform(result["delta"],["delta"])
    if invocation is not None:
        path = invocation["path"]
        for field in ("pid","process_start_epoch","parent_pid","parent_process_start_epoch"):
            leaf(["delta",path,"value",field],"<verified-native-"+field+">",
                 "canonical owned waiter state and collected PID validated; native birth testimony bounded by parent/token/launch walls; raw retained")
        _equal(invocation["value"]["workspace"],str(bounds["workspace"]),"exact selected waiter workspace")
        leaf(["delta",path,"value","workspace"],"<verified-native-workspace>",
             "native waiter workspace equals the selected owned invocation coordinate")
        for index,ancestor in enumerate(invocation["value"]["host_ancestry"]):
            for field in ("pid","process_start_epoch","parent_pid"):
                leaf(["delta",path,"value","host_ancestry",index,field],"<verified-native-host-"+field+">",
                     "complete exact native waiter ancestry and parent binding validated; raw retained")
        for index,row in enumerate(semantic.get("receipts/stop-holds/"+ACTOR+".jsonl",{}).get("frames",[])):
            for field in ("holder_pid","process_start_epoch"):
                leaf(["delta","receipts/stop-holds/"+ACTOR+".jsonl","frames",index,field],
                     "<verified-native-"+field+">","native stop-hold start/exit pair matches the exact waiter lease process")
    if case=="node-prep-clear":
        for suffix in (["observed_at"],["readiness","observed_at"]):
            leaf(["delta","events.jsonl","frames",1,*suffix],"<native-invocation-clock>",
                 "complete native envelope/readiness timestamp equality and collected invocation bounds validated; raw retained")
    if case in {"wake-resume","seat-board"}:
        for field in ("cpu_seconds","wall_seconds","argv_digest"):
            leaf(["delta",timing,"frames",0,field],"<bounded-native-"+field+">",
                 "complete native timing vector and collected invocation CPU/wall bounds validated; raw retained")
    if case in {"wake-evaluate","wake-wait"}:
        from floati.events import EVENT_KINDS,validate_event_records
        from floati.framing import decode_frames
        from floati.jsonl import VerifiedLedgerCursor
        from floati.records import validate_record,wake_hold_decision_digest
        from floati.wake_hold import WakeHoldController
        events=decode_frames(after["events.jsonl"])
        for row in events: validate_record(row,root.tenant_id,EVENT_KINDS,integrity=True)
        validate_event_records(events)
        source_digest=VerifiedLedgerCursor._initial_digest(WakeHoldController._EVENT_DOMAIN)
        source_digest.update(after["events.jsonl"])
        event_digest=source_digest.hexdigest()
        selected=[row for row in events if row["kind"]=="message_envelope"
                  and row["recipient"]==context["recipient"]]
        _equal(selected,[context["message"]],"one identical native selected envelope despite unselected history")
        def prefixes(value,parts):
            if type(value) is dict:
                for key,child in list(value.items()):
                    if key=="event_prefix_digest":
                        _equal(child,event_digest,"native complete event-source digest")
                        leaf([*parts,key],"<validated-native-unselected-event-prefix>",
                             "complete current native event source and unchanged selected common-prefix envelope validated")
                    else: prefixes(child,[*parts,key])
                if value.get("kind")=="wake_hold_receipt":
                    leaf([*parts,"decision_digest"],wake_hold_decision_digest(value),
                         "native wake decision checksum follows only validated event-prefix projection")
            elif type(value) is list:
                for index,child in enumerate(list(value)): prefixes(child,[*parts,index])
        prefixes(result["artifact"],["artifact"])
        prefixes(result["delta"],["delta"])
    if case=="wake-wait":
        from floati.cursor_presentations import CursorPresentationLedger
        plane=CursorPresentationLedger(root)
        ledger=plane.relative(ACTOR).as_posix()
        witness_path=plane.witness_relative(ACTOR).as_posix()
        rows=original["delta"][ledger]["frames"]
        _require(len(rows)==2 and [row["phase"] for row in rows]==["prepared","flushed"],
                 "native Cursor case must retain its exact prepared/flushed pair")
        body=artifact.get("evidence",artifact)
        digest=hashlib.sha256((json.dumps(body)+"\n").encode()).hexdigest()
        normal_body=result["artifact"].get("evidence",result["artifact"])
        normal_digest=hashlib.sha256((json.dumps(normal_body)+"\n").encode()).hexdigest()
        def outputs(value,parts):
            if type(value) is dict:
                for key,child in list(value.items()):
                    if key=="output_digest":
                        _equal(child,digest,"complete native Cursor stdout digest")
                        leaf([*parts,key],normal_digest,"exact native flushed body validated; digest follows only owned-root normalization")
                    else: outputs(child,[*parts,key])
            elif type(value) is list:
                for index,child in enumerate(list(value)): outputs(child,[*parts,index])
        outputs(result["delta"],["delta"])
        witness_value=original["delta"][witness_path]["value"]
        prepared_bytes=encode_frame(rows[0])
        _require(witness_value["before_bytes"]==len(prepared_bytes)
                 and witness_value["before_digest"]==hashlib.sha256(prepared_bytes).hexdigest()
                 and witness_value["reserved_record"]==rows[1],
                 "native Cursor witness differs from its exact prepared-prefix/flushed reservation")
        normalized_prepared=encode_frame(result["delta"][ledger]["frames"][0])
        leaf(["delta",witness_path,"value","before_bytes"],len(normalized_prepared),
             "exact native prepared prefix validated; virtual byte count follows only explicit scalar transforms")
        leaf(["delta",witness_path,"value","before_digest"],hashlib.sha256(normalized_prepared).hexdigest(),
             "exact native prepared prefix validated; digest follows only explicit scalar transforms")
    # Explicit daemon performance testimony is retained raw and bounded by the
    # collected child's CPU/wall. State, backoff, cycle count and budget verdict
    # remain literal; changing those cannot be hidden as a clock difference.
    if case=="wake-daemon-serve":
        runtime=artifact["evidence"]["runtime"]
        cpu=bounds.get("child_cpu_seconds")
        _require(type(cpu) in (int,float) and math.isfinite(cpu) and cpu>=0,
                 "daemon normalization requires collected child CPU testimony")
        measured=runtime["cpu_seconds_last_cycle"]
        _require(type(measured) in (int,float) and math.isfinite(measured) and 0<=measured<=cpu+.001
                 and runtime["cpu_seconds_total"]==measured and runtime["cpu_seconds_window"]==[measured],
                 "one-poll native CPU testimony exceeds the actual child")
        scheduled=runtime["next_poll_at"]-runtime["current_backoff"]
        _require(begin.timestamp()<=scheduled<=end.timestamp(),"native daemon schedule is outside invocation")
        path="state/wake-daemon/runtime/"+context["coordinate_digest"]+".json"
        _equal(original["delta"][path]["value"],runtime,"complete native daemon runtime publication")
        for parts in (["artifact","evidence","runtime"],["delta",path,"value"]):
            for field in ("cpu_seconds_last_cycle","cpu_seconds_total"):
                leaf([*parts,field],"<collected-native-cycle-cpu>","native one-cycle CPU is finite, equal and bounded by collected child CPU")
            leaf([*parts,"cpu_seconds_window",0],"<collected-native-cycle-cpu>","complete one-cycle native CPU window validated")
            leaf([*parts,"next_poll_at"],"<invocation-time-plus-native-backoff>","native schedule minus literal backoff is within invocation walls")
    if case=="node-prompts":
        for index,row in enumerate(result["artifact"]["evidence"]["files"]):
            content=result["delta"]["@publication/lifecycle-prompts"]["files"][row["path"]]
            leaf(["artifact","evidence","files",index,"sha256"],hashlib.sha256(content.encode()).hexdigest(),
                 "complete native prompt content validated; checksum follows only exact owned-root transformation")
    _require(snapshot(home)==after,"native lifecycle evidence changed during normalization")
    restored=copy.deepcopy(result)
    for audit in result["normalizations"]:
        parts=[part.replace("~1","/").replace("~0","~") for part in audit["pointer"].split("/")[1:]]
        old,target=original,restored
        for part in parts[:-1]:
            old=old[int(part)] if type(old) is list else old[part]
            target=target[int(part)] if type(target) is list else target[part]
        key=int(parts[-1]) if type(target) is list else parts[-1]
        _require(not isinstance(old[key],(dict,list)) and _json(old[key])!=_json(target[key]),"audit is not one changed scalar")
        target[key]=copy.deepcopy(old[key])
    restored["normalizations"]=[]
    _equal(restored,original,"complete native lifecycle leaf-audit round trip")
    _json(result)
    return result
