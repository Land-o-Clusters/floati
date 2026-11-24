"""Private performing-seat teardown workload, isolated from the older cases.

Preparation authors real governance, Git, map, consent and held-session inputs
before history padding. The measured command retains the native activity
observer. Child Git/process-census bytes are outside the existing parent-Python
bus-byte observation; whole collected child-tree CPU is retained.
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
import configparser
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

from floati.root import FloatiRoot

CASES = frozenset({"seat-teardown"})
ACTOR, COORDINATOR = "scale-worker", "architect"
SESSION = "census-private-seat-teardown"
KEY = "census-private-teardown"
PRODUCT_SHA = "d66a94daa357bd392b3040c55cbe21e20ac884a3"
RECEIPTS = "receipts/seat-teardown/scale-worker.jsonl"
WAKE = "receipts/wake-control/scale-worker.jsonl"
KINDS = {RECEIPTS: {"seat_teardown_receipt"}, WAKE: {"wake_control_receipt"},
         "events.jsonl": {"message_envelope", "send_readiness_observation"}}


class Unresolved(ValueError):
    code = "bus_scale_teardown_unresolved"


def require(condition, detail):
    if not condition:
        raise Unresolved(detail)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def private(path, *, directory=True, restricted=True):
    path = Path(path)
    temporary = Path("\x2fprivate/tmp" if os.uname().sysname == "Darwin" else "\x2ftmp")
    require(path.is_absolute() and temporary in path.parents and path.resolve(strict=True) == path,
            "teardown input must be canonical below the platform temporary root")
    for part in (path, *path.parents):
        if part == temporary:
            break
        info = part.lstat()
        require(info.st_uid == os.getuid() and not stat.S_ISLNK(info.st_mode)
                and (stat.S_ISREG(info.st_mode) if part == path and not directory else stat.S_ISDIR(info.st_mode)),
                "teardown coordinate has a foreign owner, alias or special component")
        require(not stat.S_IMODE(info.st_mode) & 0o022, "teardown input has writable foreign permissions")
    info = path.lstat()
    require(not directory or not restricted or not stat.S_IMODE(info.st_mode) & 0o077,
            "teardown directory must be private")
    require(directory or info.st_nlink == 1, "teardown file is shared")
    return path


def snapshot(home):
    home = private(home)
    result = {}
    for path in sorted(home.rglob("*")):
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), "teardown snapshot contains a special object")
        if stat.S_ISREG(info.st_mode):
            private(path, directory=False)
            result[path.relative_to(home).as_posix()] = path.read_bytes()
    return result


def frames(root, path, raw):
    from floati.framing import decode_frames, encode_frame
    from floati.records import validate_record
    require(path in KINDS, "unreviewed teardown ledger")
    rows = decode_frames(raw)
    for row in rows:
        validate_record(row, root.tenant_id, KINDS[path], integrity=True)
    require(b"".join(encode_frame(row) for row in rows) == raw, "teardown ledger is not canonical native framing")
    return rows


def coordinate(home, workspace, context):
    """Read-only admission proof; no Git child or ledger flock is introduced."""
    from floati.codex_wait_contract import WORKSPACE_MAP_RELATIVE
    from floati.framing import decode_frames
    from floati.records import validate_record
    from floati.seat_declaration import SeatDeclaration, FleetGovernance
    home, workspace = private(home), private(workspace)
    seat_home = private(workspace.parent)
    require(type(context) is dict and context.get("case") == "seat-teardown"
            and context.get("actor") == ACTOR and context.get("session") == SESSION
            and context.get("seed_home") == str(home) and context.get("workspace") == str(workspace)
            and context.get("home") == str(seat_home) and workspace.name == "repo"
            and seat_home.name == "seat-home"
            and seat_home.parent.name.startswith("bus-scale-census-owned-"),
            "teardown requires its exact private primary seat-home/repo context")
    require(home != workspace and home not in workspace.parents and workspace not in home.parents,
            "teardown bus and primary checkout overlap")
    private(workspace / ".git", restricted=False)
    root = FloatiRoot.open_direct_home(home, create=False)
    declaration, governance = SeatDeclaration.load(workspace), FleetGovernance.load(root)
    require(declaration is not None and governance is not None and declaration.node_id == ACTOR
            and governance.coordinator == COORDINATOR
            and {k: v for k, v in declaration.artifact().items() if k != "node_id"} == governance.artifact(),
            "teardown primary declaration differs from its native fleet governance")
    # Admission binds bounded capability inputs. The native CLI and completed
    # receipt witness authoritatively replay registry/role history; duplicating
    # that scaled replay in argument preparation adds harness work to CPU.
    map_raw = private(home / WORKSPACE_MAP_RELATIVE, directory=False).read_bytes()
    mapping = json.loads(map_raw)
    require(mapping == {"schema_version": 0, "tenant_id": root.tenant_id,
                        "mappings": [{"workspace": str(workspace), "node_id": ACTOR}]}
            and hashlib.sha256(map_raw).hexdigest() == context["map_digest"],
            "teardown workspace map differs from the retained native binding")
    for namespace, kind, identity, state in (
        ("codex-wait-consent", "codex_wait_consent_receipt", "consent_id", "armed"),
        ("codex-wait-session", "codex_wait_session_receipt", "claim_id", "armed"),
    ):
        path = home / "receipts" / namespace / (ACTOR + ".jsonl")
        rows = decode_frames(private(path, directory=False).read_bytes())
        for row in rows:
            validate_record(row, root.tenant_id, {kind}, integrity=True)
        require(rows and rows[-1]["id"] == context[identity] and rows[-1]["state"] == state
                and rows[-1]["node_id"] == ACTOR and rows[-1]["workspace"] == str(workspace),
                "teardown native consent or held claim differs")
        if namespace == "codex-wait-session":
            require(rows[-1]["acting_session_id"] == SESSION
                    and rows[-1]["consent_receipt_id"] == context["consent_id"]
                    and rows[-1]["workspace_map_digest"] == context["map_digest"],
                    "teardown held session differs from its exact native authority")
    config = configparser.RawConfigParser()
    config.read_string(private(workspace / ".git/config", directory=False).read_text())
    require(set(config.sections()) <= {"core", "user", "commit", 'remote "origin"', 'branch "main"'}
            and config.get("core", "bare") == "false", "teardown primary Git configuration has unknown behavior")
    allowed_options = {"core": {"repositoryformatversion", "filemode", "bare", "logallrefupdates", "ignorecase", "precomposeunicode"},
        "user": {"name", "email"}, "commit": {"gpgsign"}, 'remote "origin"': {"url", "fetch"},
        'branch "main"': {"remote", "merge"}}
    require(all(set(config.options(section)) <= allowed_options[section] for section in config.sections())
            and (not config.has_section("commit") or config.get("commit", "gpgsign") == "false"),
            "teardown Git configuration contains an unreviewed executable, include or extension")
    origin = private(context.get("origin", seat_home.parent / "origin.git"), restricted=False)
    require(origin.parent == seat_home.parent and config.get('remote "origin"', "url") == str(origin),
            "teardown origin must be the exact owned sibling bare repository")
    bare = configparser.RawConfigParser()
    bare.read_string(private(origin / "config", directory=False).read_text())
    require(bare.get("core", "bare") == "true" and set(bare.sections()) == {"core"},
            "teardown origin is not its ordinary native bare repository")
    require(private(workspace / ".git/HEAD", directory=False).read_bytes() == b"ref: refs/heads/main\n",
            "teardown primary branch must remain main")
    sha = context["sha"]
    require(type(sha) is str and re.fullmatch(r"[0-9a-f]{40}", sha), "teardown banked source tip is malformed")
    for relative in ("refs/heads/main", "refs/remotes/origin/main"):
        require(private(workspace / ".git" / relative, directory=False).read_bytes() == (sha + "\n").encode(),
                "teardown actual branch or remote source changed")
    return root


def prepare(case, home, workspace, bank):
    """Author native preface once, on the unpadded two-node private seed."""
    from floati.admin_registry import RegistryAdminBackend
    from floati.codex_wait_contract import (write_workspace_map, resolve_participant,
        CodexWaitConsentLedger, CodexWaitSessionLedger)
    from floati.events import EventLog
    from floati.git_process import fixed_git_command, fixed_git_environment
    from floati.ids import uuid7_hex
    from floati.provider_switch import ProviderSwitchWizard
    from floati.registry import Registry
    from floati.seat_declaration import FleetGovernance, SeatDeclaration
    require(case in CASES, "unknown teardown workload")
    home, workspace, bank = private(home), private(workspace), private(bank)
    root = FloatiRoot.open_direct_home(home, create=False)
    scope = private(Path(os.path.commonpath((str(home), str(workspace), str(bank)))))
    require(workspace == scope / "seat-home/repo" and (workspace / ".git").is_dir(),
            "teardown preparation requires the owned primary seat-home/repo")
    require(len([r for r in Registry(root).known_records() if r["kind"] == "registry_entry"]) == 2,
            "teardown preparation must precede census padding")
    origin = scope / "origin.git"
    def git(repository, *query):
        return subprocess.run(fixed_git_command("/usr/bin/git", repository, query),
            env=fixed_git_environment("/usr/bin/git"), check=True, capture_output=True, text=True, timeout=30).stdout
    if not origin.exists():
        git(scope, "init", "--quiet", "--bare", str(origin))
        origin.chmod(0o700)
    private(origin)
    prior = configparser.RawConfigParser()
    prior.read_string(private(workspace / ".git/config", directory=False).read_text())
    if prior.has_section('remote "origin"'):
        git(workspace, "remote", "set-url", "origin", str(origin))
    else:
        git(workspace, "remote", "add", "origin", str(origin))
    git(workspace, "config", "commit.gpgsign", "false")
    backend = RegistryAdminBackend(root)
    backend.commit_switch(ProviderSwitchWizard(root, backend, id_factory=uuid7_hex)._plan(
        (COORDINATOR, "architect", "census-private-coordinator")))
    governance = FleetGovernance.create(root, topology="star", coordinator=COORDINATOR,
        coordinator_authority=["dispatch_bounded_work"], owner_tier=["publishing"])
    SeatDeclaration.publish_declared_workspace(workspace, ACTOR, root, governance)
    git(workspace, "add", "--", "note.md", "SEAT.json")
    git(workspace, "commit", "--quiet", "-m", "Private native teardown seat")
    git(workspace, "push", "--quiet", "--set-upstream", "origin", "main")
    sha = git(workspace, "rev-parse", "HEAD").strip()
    write_workspace_map(home, workspace, ACTOR)
    binding = resolve_participant(home, workspace, _strict=True).binding
    consent = CodexWaitConsentLedger(root).arm(binding, hook_timeout_seconds=10, wait_deadline_seconds=2,
        idempotency_key="census-private-teardown-consent")
    claim = CodexWaitSessionLedger(root).arm(binding, consent, SESSION,
        idempotency_key="census-private-teardown-claim")
    selected = EventLog(root).send(ACTOR, COORDINATOR, "floati", sha, "note.md",
        "native private teardown prerequisite", idempotency_key="census-private-teardown-mail")["message"]
    context = dict(case=case, actor=ACTOR, session=SESSION, seed_home=str(home), workspace=str(workspace),
        home=str(workspace.parent), bank=str(bank), owned_scope=str(scope), origin=str(origin), sha=sha,
        claim_id=claim["id"], consent_id=consent["id"], selected_id=selected["id"], map_digest=binding.map_digest,
        constructed_native_preface=True)
    coordinate(home, workspace, context)
    return context


def arguments(case, home, workspace, context):
    require(case in CASES, "unknown teardown workload")
    coordinate(home, workspace, context)
    return ["seat", "teardown", "--root", str(home), "--as", ACTOR, "--home", str(workspace.parent),
            "--session", SESSION, "--repo", "floati", "--doc", "note.md", "--idempotency-key", KEY]


def witness(case, home, before, artifact, context, detail):
    from floati.events import EventLog, validate_event_records
    from floati.registry import Registry
    from floati.seat_teardown import completed_teardown_for_session
    from floati.seat_teardown_prefix import prefix_proof
    from floati.wake_control import WakeController
    require(case in CASES and type(before) is dict and all(type(v) is bytes for v in before.values()),
            "teardown witness needs its complete immediate raw inventory")
    root = coordinate(home, Path(context["workspace"]), context)
    require(set(artifact) == {"artifact_version", "schema_version", "command", "status", "evidence"}
            and type(artifact["artifact_version"]) is int and artifact["artifact_version"] == 0
            and type(artifact["schema_version"]) is int and artifact["schema_version"] == 1
            and artifact["command"] == "seat" and artifact["status"] == "ok", "teardown did not return native completed work")
    completed = completed_teardown_for_session(root, ACTOR, SESSION)
    require(Registry(root).require_active(COORDINATOR)["role"] == "architect",
            "completed teardown no longer has its exact native architect authority")
    require(completed is not None and artifact["evidence"] == completed and completed["step"] == "completed"
            and completed["idempotency_key"] == KEY, "stdout differs from exact native completed teardown")
    require(completed["claim_receipt_id"] == context["claim_id"]
            and completed["consent_receipt_id"] == context["consent_id"]
            and completed["pushed_tip"] == context["sha"] and completed["workspace_map_digest"] == context["map_digest"],
            "completed teardown changed its prepared authority or Git source")
    require(completed["activity"]["native_scope"] == {"population": "invoking_uid", "uid": os.getuid()}
            and completed["activity"]["native_jobs"] == []
            and completed["activity"]["workspace_set"] == [context["workspace"]]
            and completed["activity"]["native_roots"] == [context["home"]]
            and completed["work_evidence"] == {"work_item_ids": [], "worker_session_ids": [], "thread_attachment_ids": []},
            "teardown scope or selected outstanding work differs")
    after = snapshot(home)
    appended = {}
    for path in KINDS:
        old, new = before.get(path, b""), after.get(path, b"")
        require(new.startswith(old), "teardown rewrote a native ledger prefix")
        frames(root, path, old)
        appended[path] = frames(root, path, new[len(old):])
    rows = appended[RECEIPTS]
    require([r["step"] for r in rows] == ["started", "paused", "reported", "completed"]
            and rows[-1] == completed and len(appended[WAKE]) == 1,
            "teardown lacks its exact four phases and one pause")
    initial_events = frames(root, "events.jsonl", before["events.jsonl"])
    require(completed["schema_version"] == 2
            and completed["event_cutoff"] == prefix_proof(initial_events, "events.jsonl"),
            "teardown cutoff changed native input")
    require(any(r["id"] == context["selected_id"] and r.get("recipient") == COORDINATOR for r in initial_events)
            and not any(r.get("recipient") == ACTOR for r in initial_events), "selected mail or actor pending mail changed")
    events = appended["events.jsonl"]
    require([r["kind"] for r in events] == ["message_envelope", "send_readiness_observation"]
            and events[0]["id"] == completed["envelope_id"]
            and events[1]["envelope_id"] == events[0]["id"], "teardown did not append the exact report/readiness pair")
    validate_event_records(EventLog(root).event_records())
    require(appended[WAKE][0] == completed["pause_intent"]
            and WakeController(root).status(ACTOR, SESSION)["state"] == "paused", "teardown reserved pause is not current")
    require(not EventLog(root).unacked_ids(ACTOR), "teardown silently left or consumed actor mail")
    return {"exit_code": 0, "status": "ok", "effect": {"kind": "seat_teardown_completed", "count": 1}}


def refusal_witness(case, home, before, artifact, context, detail):
    """Only the ruled EVENTS/10k first-frame refusal, with no durable effects."""
    from floati.events import EventLog, validate_event_records
    from floati.registry import Registry
    from floati.seat_teardown import SeatTeardown, completed_teardown_for_session
    require(case in CASES and type(before) is dict and all(type(v) is bytes for v in before.values()),
            "refusal witness needs complete immediate raw bytes")
    root = coordinate(home, Path(context["workspace"]), context)
    require(type(artifact) is dict and set(artifact) == {"artifact_version", "schema_version", "command", "status", "evidence"}
            and type(artifact["artifact_version"]) is int and artifact["artifact_version"] == 0
            and type(artifact["schema_version"]) is int and artifact["schema_version"] == 1
            and artifact["command"] == "seat" and artifact["status"] == "refused", "unruled native refusal artifact")
    evidence = artifact["evidence"]
    require(type(evidence) is dict and set(evidence) == {"code", "detail", "remedy"}
            and evidence["code"] == "record_too_large" and type(evidence["detail"]) is str,
            "unruled native refusal evidence")
    match = re.fullmatch(r"record is ([0-9]+) bytes; maximum is 65536", evidence["detail"])
    require(match is not None and int(match[1]) > 65536
            and evidence["remedy"] == {"kind": "none", "why": "no action was named for this refusal"},
            "native frame refusal differs from the ruled defect")
    events = frames(root, "events.jsonl", before["events.jsonl"])
    validate_event_records(events)
    cutoff_bytes = len(json.dumps([row["id"] for row in events], separators=(",", ":")).encode())
    require(len(events) == 10000 and cutoff_bytes > 65536 and int(match[1]) > cutoff_bytes,
            "native cutoff alone does not prove the ruled EVENTS/10k frame overflow")
    require(Registry(root).require_active(COORDINATOR)["role"] == "architect"
            and not EventLog(root).unacked_ids(ACTOR)
            and SeatTeardown(root)._work(ACTOR) == {"work_item_ids": [], "worker_session_ids": [], "thread_attachment_ids": []},
            "native refusal prerequisite authority/mail/work is not intact")
    require(completed_teardown_for_session(root, ACTOR, SESSION) is None,
            "native frame refusal has a completed teardown")
    after = snapshot(home)
    marker = "state/wake-control/" + ACTOR + "/" + hashlib.sha256(SESSION.encode()).hexdigest() + ".json"
    require(not any(path in after or path in before for path in (RECEIPTS, WAKE, marker)),
            "frame refusal has a teardown/pause/report effect or preexisting selected effect")
    locks = {RECEIPTS + ".lock", "state/wake-control/scale-worker.lock"}
    require(all(after.get(path) == raw for path, raw in before.items())
            and set(after) - set(before) <= locks, "native refusal changed an input or unreviewed file")
    for path in set(after) - set(before):
        require(after[path] == b"", "native refusal created a nonempty lock")
        private(Path(home) / path, directory=False)
    return {"exit_code": 20, "status": "refused", "code": "record_too_large",
            "event_records": len(events), "cutoff_ids_json_bytes": cutoff_bytes,
            "native_inputs_unchanged": True, "teardown_pause_report_absent": True}


def normalize_refusal(case, delta, artifact, context, **bounds):
    home, before = Path(bounds["home"]), bounds["before_raw_files"]
    refusal_witness(case, home, before, artifact, context, {})
    after = snapshot(home)
    require(set(delta) == {path for path in set(before) | set(after) if before.get(path) != after.get(path)},
            "refusal delta omits or invents a changed file")
    empty = hashlib.sha256(b"").hexdigest()
    fields = {"operation", "before_sha256", "after_sha256", "before_bytes", "after_bytes", "payload_base64"}
    allowed = {RECEIPTS + ".lock", "state/wake-control/scale-worker.lock"}
    for path, change in delta.items():
        require(path in allowed and path not in before and after.get(path) == b""
                and type(change) is dict and set(change) == fields
                and change["operation"] == "create_lock"
                and type(change["before_bytes"]) is int and change["before_bytes"] == 0
                and type(change["after_bytes"]) is int and change["after_bytes"] == 0
                and change["before_sha256"] == empty and change["after_sha256"] == empty
                and change["payload_base64"] == "", "refusal supplied unreviewed or forged lock delta")
        private(home / path, directory=False)
    return {"artifact": copy.deepcopy(artifact),
            "delta": {path: {"operation": "create_lock"} for path in delta}, "normalizations": []}


def normalize(case, delta, artifact, context, **bounds):
    """Complete native witness, exact raw change set, then audited scalar leaves."""
    from floati.framing import encode_frame
    home = Path(bounds["home"])
    root = coordinate(home, Path(context["workspace"]), context)
    before = bounds.get("before_raw_files")
    require(type(before) is dict and all(type(v) is bytes for v in before.values()), "teardown needs immediate raw before bytes")
    witness(case, home, before, artifact, context, {})
    after = snapshot(home)
    require(set(delta) == {p for p in set(before) | set(after) if before.get(p) != after.get(p)},
            "teardown delta omits or invents a changed file")
    begin, end, child_pid = bounds["wall_begin"], bounds["wall_end"], bounds["child_pid"]
    require(isinstance(begin, datetime) and isinstance(end, datetime) and begin.tzinfo is not None
            and end.tzinfo is not None and begin <= end and type(child_pid) is int and child_pid > 0,
            "teardown normalization lacks actual collected invocation bounds")
    marker = "state/wake-control/" + ACTOR + "/" + hashlib.sha256(SESSION.encode()).hexdigest() + ".json"
    waiter_lock = "state/waiter-invocations/" + ACTOR + "/" + hashlib.sha256(SESSION.encode()).hexdigest() + ".lock"
    locks = {RECEIPTS + ".lock", WAKE + ".lock", "state/wake-control/scale-worker.lock", waiter_lock}
    from _bus_scale_fixture_proofs.bus_scale_prefix_effects import SUMMARY, verify_prefix_effect
    prefix_proof = verify_prefix_effect(home,before,after,delta,bounds.get("prefix_input"))
    cache_paths = set(prefix_proof["changed_files"])
    semantic, generated, clocks = {SUMMARY: prefix_proof["semantic"]}, {}, set()
    from floati.inbox_prefix import storage_declaration
    inbox_storage = storage_declaration()
    inbox_name, inbox_source = inbox_storage["index"]["path"], inbox_storage["source"]["path"]
    if any(path==inbox_name or path.startswith(inbox_name+"/") for path in delta) or (
            bounds.get("inbox_append_input") is not None and before.get(inbox_source)!=after.get(inbox_source)):
        from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import SUMMARY as INBOX_SUMMARY, verify_inbox_prefix_append_effect
        inbox_proof = verify_inbox_prefix_append_effect(home,before,after,delta,bounds.get("inbox_append_input"))
        cache_paths.update(inbox_proof["changed_files"])
        semantic[INBOX_SUMMARY] = inbox_proof["semantic"]
    from _bus_scale_fixture_proofs.bus_scale_cut_prefix_effects import CACHE as CUT_CACHE, SUMMARY as CUT_SUMMARY, verify_cut_prefix_effect
    if any(path==CUT_CACHE or path.startswith(CUT_CACHE+"/") for path in delta):
        cut_proof = verify_cut_prefix_effect(home,before,after,delta,bounds.get("cut_input"))
        cache_paths.update(cut_proof["changed_files"])
        semantic[CUT_SUMMARY] = cut_proof["semantic"]
    for path, change in delta.items():
        if path in cache_paths:
            continue
        require(type(change) is dict and set(change) == {"operation", "before_sha256", "after_sha256",
                "before_bytes", "after_bytes", "payload_base64"}, "teardown raw delta fields differ")
        old, new = before.get(path, b""), after.get(path, b"")
        require(type(change["before_bytes"]) is int and type(change["after_bytes"]) is int
                and change["before_bytes"] == len(old) and change["after_bytes"] == len(new)
                and change["before_sha256"] == hashlib.sha256(old).hexdigest()
                and change["after_sha256"] == hashlib.sha256(new).hexdigest(), "teardown delta raw byte/hash binding differs")
        payload = base64.b64decode(change["payload_base64"], validate=True)
        require(base64.b64encode(payload).decode() == change["payload_base64"], "teardown delta base64 is noncanonical")
        if change["operation"] == "create_lock":
            require(path in locks and path not in before and after.get(path) == b"" and payload == b"",
                    "unreviewed or nonempty teardown lock")
            lock = private(home / path, directory=False)
            if path == waiter_lock:
                require(stat.S_IMODE(lock.lstat().st_mode) == 0o600,
                        "teardown waiter invocation lock must be private")
            semantic[path] = {"operation": "create_lock"}
        elif change["operation"] == "append":
            require(path in KINDS and new == old + payload, "unreviewed teardown ledger append")
            rows = frames(root, path, payload)
            semantic[path] = {"operation": "append", "frames": rows}
            for ordinal, row in enumerate(rows):
                identifier = row["id"]
                require(identifier not in generated and re.fullmatch(r"[a-z-]+-[0-9a-f]{32}", identifier),
                        "teardown generated identity is malformed or repeated")
                parsed = uuid.UUID(hex=identifier.rsplit("-", 1)[1])
                moment = datetime.fromtimestamp((parsed.int >> 80) / 1000, timezone.utc)
                require(parsed.version == 7 and parsed.variant == uuid.RFC_4122
                        and begin - timedelta(milliseconds=1) <= moment <= end,
                        "teardown generated identity is outside actual invocation")
                generated[identifier] = "<generated-" + row["kind"] + "-" + str(ordinal) + ">"
                clocks.add(row["timestamp"])
        elif change["operation"] == "replace":
            require(path == marker and path not in before and new == payload, "unreviewed teardown mutable state")
            value = json.loads(new)
            require((canonical(value) + "\n").encode() == new, "native teardown marker is not canonical JSON")
            semantic[path] = {"operation": "replace", "value": value}
        else:
            raise Unresolved("unreviewed teardown delta operation")
    require(set(KINDS) <= set(semantic) and marker in semantic, "completed teardown lacks a required durable effect")
    for stamp in clocks:
        require(type(stamp) is str and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", stamp)
                and begin - timedelta(milliseconds=1) <= datetime.fromisoformat(stamp.replace("Z", "+00:00")) <= end,
                "teardown generated clock is outside actual invocation")
    result = {"artifact": copy.deepcopy(artifact), "delta": semantic, "normalizations": []}
    report = artifact["evidence"]["report_note"]
    unknown = artifact["evidence"]["activity"]["native_unknown"]
    require(type(unknown) is list and all(type(row) is dict and row.get("receipt_ids") == [] for row in unknown),
            "named native UNKNOWN cannot be excluded from teardown comparison")
    count_token = "; unnamed native UNKNOWN " + str(len(unknown)) + " (reasons in receipt);"
    require(report.count(count_token) == 1, "native report count differs from its witnessed unnamed host cutoff")
    normalized_report = report.replace(artifact["evidence"]["started_receipt_id"], generated[artifact["evidence"]["started_receipt_id"]])
    normalized_report = normalized_report.replace(count_token,
        "; unnamed native UNKNOWN <excluded-unnamed-host-count> (reasons in receipt);", 1)
    def leaf(parts, replacement, reason):
        target = result
        for part in parts[:-1]:
            target = target[part]
        if target[parts[-1]] == replacement:
            return
        target[parts[-1]] = replacement
        result["normalizations"].append({"pointer": "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts),
                                         "reason": reason})
    def transform(value, parts):
        if type(value) is dict:
            for key, child in list(value.items()):
                if key == "activity":
                    require(child["exempt_ancestry"] and child["exempt_ancestry"][0] == child_pid,
                            "native ancestry does not begin with the actual collected entry child")
                    require(child["native_unknown"] == unknown
                            and all(type(row) is dict and row.get("receipt_ids") == [] for row in child["native_unknown"]),
                            "native activity does not retain the witnessed unnamed host cutoff")
                    leaf([*parts, key, "exempt_ancestry", 0], "<owned-collected-entry-child>",
                         "native first ancestry PID equals actual owned collected child; all other native scope/ancestry retained")
                    leaf([*parts, key, "native_unknown"], "<excluded-unnamed-host-observation>",
                         "full native completed/UID witness validated unnamed rows with empty receipt_ids; ruled host observation excluded only from comparison, raw retained")
                transform(child, [*parts, key])
        elif type(value) is list:
            for index, child in enumerate(list(value)):
                transform(child, [*parts, index])
        elif type(value) is str:
            if value in generated:
                leaf(parts, generated[value], "exact generated UUIDv7 identity and all native receipt references validated; raw retained")
            elif value in clocks and parts[-1] in {"timestamp", "paused_at", "observed_at"}:
                leaf(parts, "<native-invocation-clock>", "exact native generated clock equality and real invocation bounds validated; raw retained")
            elif value == report and parts[-1] in {"report_note", "note"}:
                leaf(parts, normalized_report,
                     "full native report prose validated; only generated started identity and exact derived unnamed-host count projected, all other prose and raw retained")
    transform(result["artifact"], ["artifact"])
    transform(result["delta"], ["delta"])
    return result
