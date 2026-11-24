"""Private native prerequisites and complete effects for routine census writes.

This module never imports the test package or changes HOME. Preparation happens
once before cloned measured invocations. It never appends to events/registry or
installs a live hook. No provider mutation is performed, including for preview.
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
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid
from urllib.parse import urlsplit

from floati.errors import FloatiError
from floati.framing import decode_frames, encode_frame
from floati.jsonl import MAX_LEDGER_BYTES
from floati.records import validate_record
from floati.root import FloatiRoot
from tests.public_projection import HARBOR_POLICY_RELATIVE

CASES = frozenset({"consume", "hold-start", "hold-release", "presence-report",
    "work-add", "work-claim", "work-complete", "inbox", "ack", "node-drain",
    "confluence-grant", "confluence-revoke", "confluence-status", "confluence-bundle",
    "intake-scan", "intake-show", "intake-preview"})
EFFECTS = {"consume": "consumption_receipt_appended", "hold-start": "hold_started",
    "hold-release": "hold_released", "presence-report": "presence_report_appended",
    "work-add": "work_item_added", "work-claim": "work_item_claimed",
    "work-complete": "consumed_work_completed", "inbox": "messages_presented",
    "ack": "acknowledgment_appended", "node-drain": "node_mail_drained",
    "confluence-grant": "confluence_grant_appended", "confluence-revoke": "confluence_revocation_appended",
    "confluence-status": "confluence_grants_returned", "confluence-bundle": "confluence_bundle_written",
    "intake-scan": "intake_scan_returned", "intake-show": "intake_snapshots_returned",
    "intake-preview": "intake_preview_returned"}
SESSION = "bus-scale-routine-write"
ACTOR = "architect"
CONSUMER = "census-routine-reader"
TITLE = "bounded native census work"
REASON = "bounded native census hold"
BODY = "Read-only native census preview; no dispatch."
SOURCE = Path(__file__).resolve().parents[3]
WORK = "work/items.jsonl"
DELIVERY = "receipts/deliveries/architect.jsonl"
ACK = "receipts/acks/architect.jsonl"
CONSUMED = "receipts/consumed/architect.jsonl"
HOLD = "receipts/holds/architect.jsonl"
GRANTS = "confluence/grants.jsonl"
PRESENCE = "liveness-presence/architect.jsonl"
TIMINGS = {"inbox": "receipts/timings/inbox.jsonl", "ack": "receipts/timings/ack.jsonl"}
KINDS = {WORK: {"work_item", "work_transition"}, DELIVERY: {"delivery_receipt", "delivered_unpresented"},
    ACK: {"ack_receipt"}, CONSUMED: {"consumption_receipt"}, HOLD: {"hold_receipt"},
    GRANTS: {"confluence_grant"}, PRESENCE: {"liveness_presence"}}
APPENDS = {"consume": {CONSUMED}, "hold-start": {HOLD}, "hold-release": {HOLD},
    "presence-report": {PRESENCE}, "work-add": {WORK}, "work-claim": {WORK},
    "work-complete": {WORK, CONSUMED}, "inbox": {DELIVERY, ACK, TIMINGS["inbox"]},
    "ack": {ACK, TIMINGS["ack"]}, "node-drain": {DELIVERY, ACK},
    "confluence-grant": {GRANTS}, "confluence-revoke": {GRANTS},
    "confluence-status": set(), "confluence-bundle": set(), "intake-scan": set(),
    "intake-show": set(), "intake-preview": set()}
for path in TIMINGS.values():
    KINDS[path] = {"timing_receipt"}


def _json(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def github_fixture():
    if (SOURCE / HARBOR_POLICY_RELATIVE).is_file():
        path = SOURCE / "tests/fixtures/bus-scale-census/native-gh-issue-harbor-3-2026-10-15.json"
        digest = "cac0ada053d0745b82afcd00358105d1c7d1071f37f25a1afe722a6bef95b70f"
    else:
        path = SOURCE / "tests/fixtures/bus-scale-census/synthetic-gh-issue.json"
        digest = "7c65986a0380dd7d8652e05890f36f5795737bc89551c2d4ef09ec5d676eb216"
    return path, digest


def _equal(actual, expected, label):
    if _json(actual) != _json(expected):
        raise ValueError(label + " differs from complete native testimony")


def _private(path):
    path = Path(path)
    temp = Path("\x2fprivate/tmp" if os.sys.platform == "darwin" else "\x2ftmp")
    if (not path.is_absolute() or temp not in path.parents or path.resolve(strict=True) != path
            or not stat.S_ISDIR(path.lstat().st_mode) or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077):
        raise ValueError("routine fixture requires an owned canonical private temporary directory")
    return path


def _files(home):
    result = {}
    for path in home.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("routine fixture contains a nonordinary file")
        if info.st_size > MAX_LEDGER_BYTES:
            raise ValueError("routine fixture file exceeds native bound")
        result[path.relative_to(home).as_posix()] = path.read_bytes()
    return result


def _rows(root, path, data=None):
    raw = (root.path / path).read_bytes() if data is None and (root.path / path).exists() else data or b""
    rows = decode_frames(raw)
    if b"".join(encode_frame(row) for row in rows) != raw:
        raise ValueError("routine ledger is not canonically framed")
    for row in rows:
        validate_record(row, root.tenant_id, frozenset(KINDS.get(path, {row["kind"]})), integrity=True)
    return rows


def _context(case, home, context):
    if case not in CASES or type(context) is not dict or context.get("case") != case:
        raise ValueError("routine case/context mismatch")
    _json(context)
    return FloatiRoot.open_direct_home(_private(home), create=False)


def prepare(case, home, workspace, bank):
    """Native case-local prerequisites; caller copies sidecars identically.

    Multiple unACKed envelopes cannot be reduced by adding scale-dependent ACK
    histories. Such seeds remain explicitly unresolved for full-drain cases.
    """
    if case not in CASES:
        raise ValueError("unsupported routine write case")
    home, workspace, bank = _private(home), _private(workspace), _private(bank)
    root = FloatiRoot.open_direct_home(home, create=False)
    from floati.events import EventLog
    from floati.registry import Registry
    Registry(root).require_active(ACTOR)
    if not (home / "events.jsonl").exists():
        reference = workspace / ".git/refs/remotes/origin/main"
        if (not reference.is_file() or reference.resolve(strict=True) != reference
                or re.fullmatch(r"[0-9a-f]{40}", reference.read_text().strip()) is None):
            raise ValueError("UNRESOLVED: native common preface needs the owned banked Git workspace")
        # Architect-approved new-case preface only, before scaled padding.
        # Existing old-case seed bytes are never rewritten or extended here.
        EventLog(root).send("scale-worker", ACTOR, "floati", reference.read_text().strip(),
            "note.md", "bounded selected native routine work", idempotency_key="routine-selected-preface")
    protected = {path: (home / path).read_bytes() for path in ("events.jsonl", "registry/entries.jsonl")}
    context = {"case": case, "bank": str(bank), "session": SESSION, "actor": ACTOR}
    events = EventLog(root).records()
    selected = [row for row in events if row["recipient"] == ACTOR and row.get("worker_session_id") is None]
    if case in {"consume", "work-complete", "ack", "inbox", "node-drain"}:
        if not selected:
            raise ValueError("UNRESOLVED: selected common-prefix architect envelope absent")
        context["message"] = selected[0]
        if case in {"inbox", "node-drain"} and EventLog(root).unacked_ids(ACTOR) != [selected[0]["id"]]:
            raise ValueError("UNRESOLVED: native drain needs exactly one common-prefix pending envelope")
        # Native delivery factory retains real delivered_unpresented evidence
        # for the absent presenter. Full inbox/drain creates its own delivery
        # and cold cache inside the measured invocation instead.
        if case not in {"inbox", "node-drain"}:
            EventLog(root)._write_delivery_receipt(ACTOR, [selected[0]])
        if case in {"consume", "work-complete"}:
            from floati.cursor import SparseCursor
            context["ack"] = SparseCursor(root).ack(ACTOR, [selected[0]["id"]], acting_session_id="census-prepared-seen")
    if case in {"work-claim", "work-complete"}:
        from floati.planes import AuthorityGrantStore
        from floati.work import WorkLog
        now = datetime.now(timezone.utc)
        grant = AuthorityGrantStore(root).claim("census-work", ACTOR, 3600, 3600, now)
        work = WorkLog(root)
        context["authority"] = grant
        context["work"] = work.add(TITLE, ACTOR, [], now=now)
        if case == "work-complete":
            context["claim"] = work.claim(context["work"]["id"], ACTOR, "census-work", grant["epoch"], now=now)
    if case == "hold-release":
        from floati.holds import HoldLedger
        context["hold"] = HoldLedger(root).start(ACTOR, REASON, "census-prepared-hold")
    if case in {"confluence-revoke", "confluence-status", "confluence-bundle"}:
        from floati.confluence import ConfluenceGrantLedger
        context["grant"] = ConfluenceGrantLedger(root).grant(CONSUMER, "census-prepared-grant")
    if case == "confluence-bundle":
        context["publication_name"] = "routine-bundle.json"
    if case in {"intake-scan", "intake-show"}:
        directory = bank / "intake-source"
        directory.mkdir(mode=0o700)
        (directory / "task.md").write_text("# Native census task\n\nBounded local intake.\n", encoding="utf-8")
        context["directory"] = str(directory)
        if case == "intake-show":
            from floati.intake import adopt_local, show_snapshots
            adopted = adopt_local(root, directory, "task.md", owner=ACTOR)
            context["snapshot"] = show_snapshots(root, adopted["snapshot_id"])[0]
    if case == "intake-preview":
        path, digest = github_fixture()
        raw = path.read_bytes() if path.is_file() else b""
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("UNRESOLVED: pinned GitHub issue response absent or digest changed")
        metadata = json.loads(raw)
        url = urlsplit(metadata["url"])
        match = re.fullmatch(r"/([^/]+)/([^/]+)/issues/3", url.path)
        if metadata["number"] != 3 or url.scheme != "https" or url.netloc != "github.com" or match is None:
            raise ValueError("UNRESOLVED: pinned GitHub response coordinate mismatch")
        source_owner, repository = match.groups()
        selector = ["issue", "view", "3", "--repo", f"{source_owner}/{repository}", "--json",
                    "number,title,body,state,labels,author,createdAt,updatedAt,url"]
        replay = bank / "retained-gh-read"
        # Replay the exact pinned bytes. The child receives no account credentials.
        script = ("#!/usr/bin/python3\nimport base64,os,sys\n"
                  "if sys.argv[1:] != " + repr(selector) + ": sys.exit(64)\n"
                  "os.write(1,base64.b64decode(" + repr(base64.b64encode(raw).decode()) + "))\n")
        with replay.open("x") as stream:
            stream.write(script)
        replay.chmod(0o700)
        from floati.intake import adopt_github, show_snapshots
        credentials = {name: os.environ.pop(name) for name in ("GH_TOKEN", "GITHUB_TOKEN") if name in os.environ}
        try:
            adopted = adopt_github(root, source_owner, repository, 3, str(replay), owner=ACTOR)
        finally:
            os.environ.update(credentials)
        context["github_response_sha256"] = digest
        context["snapshot"] = show_snapshots(root, adopted["snapshot_id"])[0]
        from floati.policy import RepositoryPolicy
        policy = home / "FLOATI.toml"
        if not policy.exists():
            RepositoryPolicy.load(SOURCE / "FLOATI.toml")
            with policy.open("xb") as stream:
                stream.write((SOURCE / "FLOATI.toml").read_bytes())
        context["policy_sha256"] = hashlib.sha256(policy.read_bytes()).hexdigest()
        RepositoryPolicy.load(policy)
    for path, raw in protected.items():
        if (home / path).read_bytes() != raw:
            raise ValueError("routine preparation changed a primary scaled ledger")
    _json(context)
    return context


def arguments(case, home, workspace, context):
    _context(case, home, context)
    common = ["--root", str(home)]
    key = "census-" + case
    if case == "consume":
        return ["consume", context["message"]["id"], *common, "--as", ACTOR, "--session", SESSION, "--idempotency-key", key]
    if case.startswith("hold-"):
        return ["hold", case[5:], *common, "--as", ACTOR, "--idempotency-key", key,
                *( ["--reason", REASON] if case == "hold-start" else ["--hold", context["hold"]["id"]])]
    if case == "presence-report":
        return ["presence", "report", *common, "--as", ACTOR, "--ttl-seconds", "60"]
    if case == "work-add":
        return ["work", "add", *common, "--owner", ACTOR, "--title", TITLE]
    if case == "work-claim":
        return ["work", "claim", *common, "--id", context["work"]["id"], "--as", ACTOR,
                "--authority-subject", "census-work", "--authority-epoch", str(context["authority"]["epoch"])]
    if case == "work-complete":
        return ["work", "complete", *common, "--id", context["work"]["id"], "--as", ACTOR,
                "--consume", context["message"]["id"], "--session", SESSION]
    if case == "inbox":
        return ["inbox", *common, "--as", ACTOR, "--session", SESSION]
    if case == "ack":
        return ["ack", *common, "--as", ACTOR, "--id", context["message"]["id"], "--session", SESSION]
    if case == "node-drain":
        return ["node", "drain", *common, "--node", ACTOR, "--session", SESSION]
    if case.startswith("confluence-"):
        action = case.removeprefix("confluence-")
        argv = ["confluence", action, *common]
        if action != "status":
            argv.extend(["--consumer", CONSUMER])
        if action in {"grant", "revoke"}:
            argv.extend(["--idempotency-key", key])
        if action == "bundle":
            out = _private(Path(home).parent) / context["publication_name"]
            argv.extend(["--out", str(out), "--schema-version", "2"])
        return argv
    if case == "intake-scan":
        return ["intake", "scan", *common, "--from", context["directory"]]
    if case == "intake-show":
        return ["intake", "show", *common, "--id", context["snapshot"]["record"]["id"]]
    if case == "intake-preview":
        return ["intake", "preview", *common, "--snapshot", context["snapshot"]["record"]["id"], "--operation", "comment", "--body", BODY]
    raise ValueError("unsupported routine case")


def _native_inbox_payload(root, before):
    """Reproduce the complete native pre-drain payload from retained bytes.

    Delivery and ACK have already advanced when the parent validates this
    result. Replay just those two native reader inputs from retained prefix
    bytes; never rewind a file, invoke presentation, or refresh a cache.
    """
    from floati import cursor as cursor_module
    from floati.events import EVENT_KINDS, EventLog, validate_event_records
    from floati.jsonl import _decode_path_records
    from floati.records import WAKE_HOLD_KINDS

    log = EventLog(root)
    events_raw = before.get("events.jsonl")
    if not isinstance(events_raw, bytes) or (root.path / "events.jsonl").read_bytes() != events_raw:
        raise ValueError("cold inbox source differs from retained pre-drain events")
    events = _decode_path_records(root.path / "events.jsonl", root.tenant_id, frozenset(EVENT_KINDS), events_raw)
    validate_event_records(events)
    native_reader = cursor_module.read_records
    native_sealed_reader = cursor_module.read_records_sealed_snapshot

    def retained_records(requested_root, relative, *, allowed_kinds, **options):
        relative = Path(relative).as_posix()
        expected_kinds = {DELIVERY: frozenset(WAKE_HOLD_KINDS), ACK: frozenset({"ack_receipt"})}
        if (requested_root is not root or relative not in expected_kinds or options
                or frozenset(allowed_kinds) != expected_kinds[relative]):
            raise ValueError("native cold inbox replay requested an unreviewed reader coordinate")
        prior = before.get(relative, b"")
        if not isinstance(prior, bytes):
            raise ValueError("cold inbox replay requires retained raw receipt bytes")
        current = (root.path / relative).read_bytes() if (root.path / relative).exists() else b""
        if not current.startswith(prior):
            raise ValueError("cold inbox receipt prefix differs from retained native bytes")
        return _decode_path_records(root.path / relative, root.tenant_id, expected_kinds[relative], prior)

    # This is a closed historical read adapter after child collection. The
    # native cursor checks and native payload builder execute unchanged.
    cursor_module.read_records = retained_records
    cursor_module.read_records_sealed_snapshot = retained_records
    try:
        _messages, expected_payload = log._present_from_frames(events, ACTOR, 1000)
    finally:
        cursor_module.read_records = native_reader
        cursor_module.read_records_sealed_snapshot = native_sealed_reader
    return expected_payload


def _keyprefix_effects(home, before, after, delta, *, inbox_prefix_input=None,
                       consumed_prefix_input=None):
    """Prove only the exact changed artifact named by each closed contract."""
    from _bus_scale_fixture_proofs import bus_scale_inbox_prefix_effects as inbox
    from _bus_scale_fixture_proofs import bus_scale_consumed_prefix_effects as consumed
    results = []
    for name, captured, verify, summary, tag, options in (
        (inbox.CACHE, inbox_prefix_input, inbox.verify_inbox_prefix_effect,
            inbox.SUMMARY, inbox.TAG, {}),
        (consumed.cache_name(CONSUMED), consumed_prefix_input,
            consumed.verify_consumed_prefix_effect, consumed.SUMMARY, consumed.TAG,
            {"node": ACTOR, "worker_session_id": None}),
    ):
        if any(type(path) is str and (path == name or path.startswith(name + "/"))
               for path in delta):
            if before is None:
                raise ValueError("native prefix equivalence requires exact pre-child raw bytes")
            proof = verify(home, before, after, delta, captured, **options)
            results.append((summary, tag, proof))
    return results


def _hint_effects(case, root, before, after, delta):
    from _bus_scale_fixture_proofs.bus_scale_wake_hint_effects import verify_wake_hint_effects
    return verify_wake_hint_effects(root, before, after, delta,
        ledger_kinds={path: KINDS[path] for path in APPENDS[case]})


def _effects(case, root, before, *, cursor_prefix_input=None, delivery_prefix_input=None,
             registry_prefix_input=None, inbox_prefix_input=None, consumed_prefix_input=None):
    after = _files(root.path)
    expected = APPENDS[case]
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    proved = set()
    from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import raw_candidate_delta
    proved.update(_hint_effects(case, root, before, after, raw_candidate_delta(before, after)))
    for _summary, _tag, proof in _keyprefix_effects(root.path, before, after,
            raw_candidate_delta(before, after), inbox_prefix_input=inbox_prefix_input,
            consumed_prefix_input=consumed_prefix_input):
        proved.update(proof["changed_files"])
    if any((path==".registry-prefix" or path.startswith(".registry-prefix/")) for path in changed):
        from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import verify_registry_prefix_effect
        from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import raw_candidate_delta
        proof = verify_registry_prefix_effect(root.path,before,after,
            raw_candidate_delta(before,after),registry_prefix_input)
        proved.update(proof["changed_files"])
    if case in {"ack", "inbox", "node-drain"} and cursor_prefix_input is not None:
        from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import verify_cursor_prefix_effect, raw_candidate_delta
        proof = verify_cursor_prefix_effect(root.path, before, after,
            raw_candidate_delta(before, after), cursor_prefix_input, node=ACTOR,
            delivery_input=delivery_prefix_input)
        proved.update(proof["changed_files"])
        if "combined_delivery_effect" in proof:
            proved.update(proof["combined_delivery_effect"]["changed_files"])
    locks = {p + ".lock" for p in expected} | {"work/car-acceptance.lock", "receipts/wake-coordination/architect/lane.lock"}
    if changed - proved - expected - locks:
        raise ValueError("routine invocation changed unreviewed paths: " + repr(sorted(changed - proved - expected - locks)))
    appended = {}
    for path in changed - proved:
        old, new = before.get(path, b""), after.get(path)
        if new is None:
            raise ValueError("routine invocation removed native evidence")
        if path in locks:
            if old or new or path in before:
                raise ValueError("routine lock change is not an empty new lock")
        else:
            if not new.startswith(old):
                raise ValueError("routine append rewrote its prefix")
            _rows(root, path, new)
            rows = _rows(root, path, new[len(old):])
            if not rows:
                raise ValueError("routine required append is empty")
            appended[path] = rows
    if set(appended) != expected:
        raise ValueError("routine invocation did not append every expected ledger")
    return appended, after


def _one(appended, path):
    if len(appended[path]) != 1:
        raise ValueError("routine selected work must append exactly one " + path)
    return appended[path][0]


def _expected(row, **fields):
    for field, value in fields.items():
        _equal(row.get(field), value, "native " + field)


def witness(case, home, before, artifact, context, detail, *, cursor_prefix_input=None, delivery_prefix_input=None,
            registry_prefix_input=None, inbox_prefix_input=None, consumed_prefix_input=None):
    """Require actual complete native effects; a refusal is never work."""
    try:
        root = _context(case, home, context)
        command = arguments(case, home, home, context)[0]
        if (type(artifact) is not dict or type(artifact.get("artifact_version")) is not int
                or artifact["artifact_version"] != 0 or artifact.get("command") != command
                or artifact.get("status") != "ok" or type(artifact.get("evidence")) is not dict):
            raise ValueError("routine command lacks a successful native artifact")
        _json(artifact)
        appended, after = _effects(case, root, before, cursor_prefix_input=cursor_prefix_input,
            delivery_prefix_input=delivery_prefix_input, registry_prefix_input=registry_prefix_input,
            inbox_prefix_input=inbox_prefix_input, consumed_prefix_input=consumed_prefix_input)
        evidence = artifact["evidence"]
        if case in {"consume", "work-complete"}:
            consumed = _one(appended, CONSUMED)
            _expected(consumed, recipient=ACTOR, message_id=context["message"]["id"], worker_session_id=None,
                      acting_session_id=SESSION, ack_receipt_id=context["ack"]["id"],
                      source="explicit" if case == "consume" else "work_complete",
                      work_item_id=None if case == "consume" else context["work"]["id"],
                      idempotency_key="census-consume" if case == "consume" else context["message"]["id"])
            if context["ack"] not in _rows(root, ACK, before.get(ACK, b"")):
                raise ValueError("consume has no retained pre-invocation explicit ACK")
            if case == "consume":
                _equal(evidence, consumed, "consume stdout")
        if case in {"hold-start", "hold-release"}:
            row = _one(appended, HOLD)
            released = case == "hold-release"
            _expected(row, node_id=ACTOR, reason=None if released else REASON,
                      expected_release=None, phase="released" if released else "held",
                      predecessor_receipt_id=context["hold"]["id"] if released else None,
                      idempotency_key="census-" + case)
            if released:
                _equal(row["started_at"], context["hold"]["started_at"], "released hold start")
                _equal(row["released_at"], row["timestamp"], "released hold clock")
                if context["hold"] not in _rows(root, HOLD, before.get(HOLD, b"")):
                    raise ValueError("hold release has no retained open predecessor")
            else:
                _equal(row["started_at"], row["timestamp"], "hold start clock")
                _equal(row["released_at"], None, "hold terminal absence")
            _equal(evidence, row, "hold stdout")
        if case == "presence-report":
            row = _one(appended, PRESENCE)
            _expected(row, node_id=ACTOR, state="present")
            _equal(row["timestamp"], row["observed_at"], "presence observation")
            if _time(row["expires_at"]) - _time(row["observed_at"]) != timedelta(seconds=60):
                raise ValueError("presence TTL differs from authored 60 seconds")
            _equal(evidence, row, "presence stdout")
        if case in {"work-add", "work-claim", "work-complete"}:
            row = _one(appended, WORK)
            if case == "work-add":
                _expected(row, title=TITLE, owner=ACTOR, artifact_bindings=[])
            else:
                _expected(row, work_item_id=context["work"]["id"], actor=ACTOR,
                          action="claim" if case == "work-claim" else "complete",
                          authority_subject="census-work", authority_epoch=context["authority"]["epoch"], artifact_bindings=[])
                prior = _rows(root, WORK, before.get(WORK, b""))
                if context["work"] not in prior:
                    raise ValueError("work transition has no retained work item")
                from floati.consumption import ConsumptionLedger
                state = ConsumptionLedger(root).project(prior)[context["work"]["id"]]
                if state["state"] != ("open" if case == "work-claim" else "claimed"):
                    raise ValueError("work transition has the wrong predecessor state")
                if case == "work-complete":
                    _equal(_one(appended, CONSUMED)["timestamp"], row["timestamp"], "consumption/completion native clock")
            findings = []
            if case in {"work-add", "work-complete"}:
                findings = [{
                    "code": "work_add_unnotified" if case == "work-add" else "work_complete_unnotified",
                    "missing": ["dispatcher", "artifact"],
                    "missing_flags": ["--as", "--repo", "--sha", "--doc"],
                    "remedy": "author notification inputs with --as --repo --sha --doc on work add",
                }]
            _equal(evidence, {**row, "findings": findings}, "work stdout")
        if case in {"ack", "inbox", "node-drain"}:
            ack = _one(appended, ACK)
            _expected(ack, recipient=ACTOR, item_ids=[context["message"]["id"]], acting_session_id=SESSION)
            if case == "ack":
                _equal(evidence, ack, "ack stdout")
            else:
                payload = _native_inbox_payload(root, before)
                _equal(payload["messages"], [context["message"]],
                    "complete native pre-drain selected inbox payload")
                _equal(payload["recipient"], ACTOR, "native pre-drain recipient")
                _equal(payload["limit"], 1000, "native pre-drain limit")
                _equal(payload["truncated"], False, "native pre-drain truncation")
                delivery = _one(appended, DELIVERY)
                _expected(delivery, recipient=ACTOR, item_ids=[context["message"]["id"]])
                prior = _rows(root, DELIVERY, before.get(DELIVERY, b""))
                _equal(delivery["presentation_count"], len(prior) + 1, "native presentation count")
                if case == "inbox":
                    from floati.colleague_frame import framed_messages
                    _equal(evidence["messages"], framed_messages([context["message"]]), "complete inbox selected envelope and colleague frame")
                    _equal(evidence["receipt"], delivery, "inbox delivery stdout")
                    _equal(evidence["acknowledgment"], ack, "inbox ACK stdout")
                    _expected(evidence, used_node=ACTOR, used_root=str(home), workspace_identity="absent")
                else:
                    _equal(evidence, dict(node_id=ACTOR, state="active", registered=True,
                        acked_ids=[context["message"]["id"]], acknowledgment_ids=[ack["id"]], acting_session_id=SESSION), "complete node drain")
        if case in TIMINGS:
            row = _one(appended, TIMINGS[case])
            from floati.timings import argv_digest
            _expected(row, command=case, node_id=ACTOR, outcome="ok", refusal_code=None,
                      argv_digest=argv_digest(arguments(case, home, home, context), root.path))
        if case in {"confluence-grant", "confluence-revoke"}:
            row = _one(appended, GRANTS)
            revoked = case == "confluence-revoke"
            _expected(row, consumer=CONSUMER, state="revoked" if revoked else "granted",
                      predecessor_receipt_id=context["grant"]["id"] if revoked else None,
                      idempotency_key="census-" + case)
            expected = dict(grant_id=row["id"], consumer=CONSUMER, state=row["state"])
            if revoked:
                if context["grant"] not in _rows(root, GRANTS, before.get(GRANTS, b"")):
                    raise ValueError("revoke has no retained active grant")
                expected["predecessor_receipt_id"] = context["grant"]["id"]
            _equal(evidence, expected, "grant stdout")
        if case == "confluence-status":
            rows = _rows(root, GRANTS)
            _equal(evidence, {"grants": [{"grant_id": r["id"], "consumer": r["consumer"], "state": r["state"], "timestamp": r["timestamp"]} for r in rows]}, "complete grants status")
            if context["grant"] not in rows:
                raise ValueError("status omitted prepared grant")
        if case == "confluence-bundle":
            document = _bundle(root, context)
            _equal(evidence, dict(grant_id=context["grant"]["id"], consumer=CONSUMER,
                out=str(root.path.parent / context["publication_name"]), entries=len(document["entries"]), snapshot_at=document["snapshot_at"]), "bundle publication stdout")
        if case == "intake-scan":
            _equal(evidence, dict(root=str(home), directory=context["directory"], verdicts=[dict(path="task.md", verdict="eligible", code=None, detail="eligible")]), "complete intake scan")
            _equal(Path(context["directory"]).joinpath("task.md").read_text(), "# Native census task\n\nBounded local intake.\n", "scanned source bytes")
        if case == "intake-show":
            from floati.intake import show_snapshots
            expected = show_snapshots(root, context["snapshot"]["record"]["id"])
            _equal(expected, [context["snapshot"]], "retained immutable local snapshot")
            _equal(evidence, {"snapshots": expected}, "complete intake show")
        if case == "intake-preview":
            from floati.intake import preview_github_mutation, show_snapshots
            _equal(context["github_response_sha256"], github_fixture()[1], "pinned GitHub response digest")
            _equal(hashlib.sha256((root.path / "FLOATI.toml").read_bytes()).hexdigest(), context["policy_sha256"], "retained preview policy")
            _equal(show_snapshots(root, context["snapshot"]["record"]["id"]), [context["snapshot"]], "retained GitHub adoption")
            expected = preview_github_mutation(root, context["snapshot"]["record"]["id"], "comment", {"body": BODY})
            _equal(evidence, expected, "complete nondispatching GitHub preview")
            _equal(evidence["will_dispatch"], False, "preview does not dispatch")
        return {"exit_code": 0, "status": "ok", "effect": {"kind": EFFECTS[case], "count": 1}}
    except (FloatiError, OSError, KeyError, TypeError, UnicodeError, OverflowError) as exc:
        raise ValueError("native routine effect proof unavailable: " + str(exc)) from exc


def _bundle(root, context, *, wall_begin=None, wall_end=None):
    bank = _private(root.path.parent)
    path = bank / context["publication_name"]
    if path != bank / "routine-bundle.json" or path.resolve(strict=True) != path or path.stat().st_uid != os.getuid():
        raise ValueError("bundle publication is outside its exact private bank")
    raw = path.read_bytes()
    if len(raw) > MAX_LEDGER_BYTES:
        raise ValueError("bundle publication exceeds native bound")
    document = json.loads(raw)
    from floati.confluence import _bundle_sources
    entries = []
    excluded = 0
    for relative, kinds in _bundle_sources(root, 2):
        for ordinal, row in enumerate(decode_frames((root.path / relative).read_bytes() if (root.path / relative).exists() else b""), 1):
            validate_record(row, root.tenant_id, kinds, integrity=True)
            if row["kind"] == "wake_hold_receipt":
                excluded += 1
            elif relative != "events.jsonl" or row["kind"] in {"message_envelope", "message_retracted"}:
                entries.append(dict(source=relative, source_ordinal=ordinal, record=row))
    entries.sort(key=lambda e: (e["record"]["timestamp"], e["record"]["id"], e["source"], e["source_ordinal"]))
    entries = [dict(sequence=n, **entry) for n, entry in enumerate(entries, 1)]
    expected = dict(schema_version=2, kind="receipts_read_bundle", tenant_id=root.tenant_id,
        snapshot_at=max((e["record"]["timestamp"] for e in entries), default=context["grant"]["timestamp"]),
        grant_id=context["grant"]["id"], entries=entries, excluded_wake_hold_receipt_count=excluded)
    # Main's v2 roster carries native ages at one common publication instant.
    # Intersect every measured age's one-second interval, then rerun the full
    # native projection at that instant. Never drop ages or accept each cell
    # at a different time. The normalizer supplies the actual child bounds.
    from floati.roster import project_roster
    from floati.confluence_fleet_state import project_fleet_state
    if (wall_begin is None) != (wall_end is None):
        raise ValueError("bundle roster requires both invocation bounds")
    lower = _time(context["grant"]["timestamp"]) if wall_begin is None else wall_begin
    upper = datetime.now(timezone.utc) if wall_end is None else wall_end
    if (not isinstance(lower, datetime) or not isinstance(upper, datetime)
            or lower.tzinfo is None or upper.tzinfo is None or lower > upper):
        raise ValueError("bundle roster invocation bounds are invalid")
    fleet_state = document.get("fleet_state")
    if type(fleet_state) is not dict or type(fleet_state.get("waiters")) is not list:
        raise ValueError("bundle fleet state is absent or malformed")
    published = _time(fleet_state.get("observed_at"))
    lower = max(lower, published)
    roster = document.get("roster")
    if type(roster) is not list:
        raise ValueError("bundle roster is absent or malformed")
    ceilings = [published + timedelta(milliseconds=1)]
    for row in roster + fleet_state["waiters"]:
        if type(row) is not dict:
            raise ValueError("bundle roster row is malformed")
        for name, cell in row.items():
            if name == "node_id":
                continue
            if type(cell) is not dict:
                raise ValueError("bundle roster cell is malformed")
            if cell.get("stamp") == "MEASURED":
                age = cell.get("age_seconds")
                if type(age) is not int or age < 0:
                    raise ValueError("bundle roster age is not a native nonnegative integer")
                observed = cell.get("observed_at")
                if type(observed) is not str:
                    raise ValueError("bundle measured cell has no observation clock")
                moment = datetime.fromisoformat(observed.replace("Z", "+00:00"))
                if moment.utcoffset() is None:
                    raise ValueError("bundle measured cell has no UTC offset")
                start = moment + timedelta(seconds=age)
                lower = max(lower, start)
                ceilings.append(start + timedelta(seconds=1))
    if lower > upper or any(lower >= ceiling for ceiling in ceilings):
        raise ValueError("bundle roster ages do not share one native invocation instant")
    expected["roster"] = project_roster(root, lower)
    expected["fleet_state"] = project_fleet_state(root, lower)
    _equal(document, expected, "every published bundle source/ordinal/record")
    return document


def bundle_clock_leaves(document):
    """List the clock leaves allowed to vary after complete bundle proof."""
    for ordinal, row in enumerate(document["roster"]):
        for name, cell in row.items():
            if name != "node_id" and cell["stamp"] == "MEASURED":
                yield ["roster", ordinal, name, "age_seconds"], cell["age_seconds"], "<native-roster-age>"
    fleet_state = document["fleet_state"]
    yield ["fleet_state", "observed_at"], fleet_state["observed_at"], "<native-fleet-observation-time>"
    for ordinal, row in enumerate(fleet_state["waiters"]):
        for name in ("mode", "last_wake"):
            cell = row[name]
            if cell["stamp"] == "MEASURED":
                yield ["fleet_state", "waiters", ordinal, name, "age_seconds"], cell["age_seconds"], "<native-waiter-age>"


def _time(value):
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value):
        raise ValueError("native routine clock is not UTC milliseconds")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def normalize(case, delta, artifact, context, *, wall_begin, wall_end, child_pid, home, workspace,
              child_cpu_seconds=None, cursor_prefix_input=None, delivery_prefix_input=None, before_raw_files=None,
              registry_prefix_input=None, inbox_prefix_input=None, consumed_prefix_input=None):
    """Validate raw/current bytes before changing explicit generated leaves."""
    try:
        root = _context(case, home, context)
        if type(delta) is not dict:
            raise ValueError("routine raw delta is not an object")
        hint_semantic = _hint_effects(case, root, before_raw_files, _files(root.path), delta)
        delta = {path: row for path, row in delta.items() if path not in hint_semantic}
        keyprefix_proofs = _keyprefix_effects(home, before_raw_files, _files(root.path), delta,
            inbox_prefix_input=inbox_prefix_input, consumed_prefix_input=consumed_prefix_input)
        keyprefix_changed = {path for _summary, _tag, proof in keyprefix_proofs
            for path in proof["changed_files"]}
        delta = {path: row for path, row in delta.items() if path not in keyprefix_changed}
        registry_proof = None
        if any(type(path) is str and (path==".registry-prefix" or path.startswith(".registry-prefix/")) for path in delta):
            from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import SUMMARY as REGISTRY_SUMMARY, verify_registry_prefix_effect
            if before_raw_files is None:
                raise ValueError("registry cache equivalence requires exact pre-child raw bytes")
            registry_proof = verify_registry_prefix_effect(home,before_raw_files,_files(root.path),
                delta,registry_prefix_input)
            delta = {path:row for path,row in delta.items() if path not in registry_proof["changed_files"]}
        cursor_proof = None
        if case in {"ack", "inbox", "node-drain"} and cursor_prefix_input is not None:
            from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import verify_cursor_prefix_effect, SUMMARY, SUMMARY_DELIVERY
            if before_raw_files is None:
                raise ValueError("ACK cache equivalence requires exact pre-child raw bytes")
            cursor_proof = verify_cursor_prefix_effect(home, before_raw_files, _files(root.path),
                delta, cursor_prefix_input, node=ACTOR, delivery_input=delivery_prefix_input)
            proved = set(cursor_proof["changed_files"])
            if "combined_delivery_effect" in cursor_proof:
                proved.update(cursor_proof["combined_delivery_effect"]["changed_files"])
            delta = {path: row for path, row in delta.items() if path not in proved}
        if (type(delta) is not dict or not isinstance(wall_begin, datetime) or not isinstance(wall_end, datetime)
                or wall_begin.tzinfo is None or wall_end.tzinfo is None or wall_begin > wall_end
                or type(child_pid) is not int or child_pid <= 0):
            raise ValueError("routine normalization lacks complete invocation bounds")
        if child_cpu_seconds is not None and (type(child_cpu_seconds) not in (int, float)
                or not math.isfinite(child_cpu_seconds) or child_cpu_seconds < 0):
            raise ValueError("retained child CPU is not a finite nonnegative measurement")
        after, before, semantic = _files(root.path), {}, {}
        before.update(after)
        semantic.update(hint_semantic)
        for path in hint_semantic:
            before.pop(path, None)
        for path, change in delta.items():
            if type(change) is not dict or set(change) != {"operation", "before_sha256", "after_sha256", "before_bytes", "after_bytes", "payload_base64"}:
                raise ValueError("routine raw delta shape is incomplete")
            for field in ("before_bytes", "after_bytes"):
                if type(change[field]) is not int or not 0 <= change[field] <= MAX_LEDGER_BYTES:
                    raise ValueError("routine delta length is not a bounded integer")
            raw = after.get(path)
            payload = base64.b64decode(change["payload_base64"], validate=True)
            if raw is None or len(raw) != change["after_bytes"] or hashlib.sha256(raw).hexdigest() != change["after_sha256"]:
                raise ValueError("routine delta does not bind current bytes")
            count = change["before_bytes"]
            if change["operation"] == "replace":
                if count or change["before_sha256"] != hashlib.sha256(b"").hexdigest() or payload != raw:
                    raise ValueError("routine replacement must be an absent cold cache")
                before.pop(path, None)
                semantic[path] = {"operation": "replace", "value": json.loads(raw)}
            else:
                if raw[count:] != payload or hashlib.sha256(raw[:count]).hexdigest() != change["before_sha256"]:
                    raise ValueError("routine delta prefix/suffix differs")
                if count:
                    before[path] = raw[:count]
                else:
                    before.pop(path, None)
                if change["operation"] == "create_lock":
                    if raw or payload:
                        raise ValueError("routine new lock must be empty")
                    semantic[path] = {"operation": "create_lock"}
                elif change["operation"] == "append":
                    semantic[path] = {"operation": "append", "frames": decode_frames(payload)}
                else:
                    raise ValueError("unsupported routine delta operation")
        if (cursor_proof is not None or registry_proof is not None or keyprefix_proofs
                or inbox_prefix_input is not None or consumed_prefix_input is not None):
            if before_raw_files is None:
                raise ValueError("native prefix reconstruction requires exact pre-child bytes")
            proved_cache = set(registry_proof["changed_files"]) if registry_proof is not None else set()
            proved_cache.update(keyprefix_changed)
            if cursor_proof is not None:
                proved_cache.update(set(cursor_proof["cache_before"]) | set(cursor_proof["cache_after"]))
            if cursor_proof is not None and "combined_delivery_effect" in cursor_proof:
                delivery_proof = cursor_proof["combined_delivery_effect"]
                proved_cache.update(set(delivery_proof["cache_before"]) | set(delivery_proof["cache_after"]))
            for path in set(before) | set(before_raw_files):
                if path not in proved_cache and before.get(path) != before_raw_files.get(path):
                    raise ValueError("reconstructed native input differs from exact retained bytes")
            before = before_raw_files
        witness(case, home, before, artifact, context, {}, cursor_prefix_input=cursor_prefix_input,
            delivery_prefix_input=delivery_prefix_input, registry_prefix_input=registry_prefix_input,
            inbox_prefix_input=inbox_prefix_input, consumed_prefix_input=consumed_prefix_input)
        result = {"artifact": copy.deepcopy(artifact), "delta": copy.deepcopy(semantic), "normalizations": []}
        for summary, tag, _proof in keyprefix_proofs:
            result["delta"][summary] = dict(tag)
        if registry_proof is not None:
            result["delta"][REGISTRY_SUMMARY] = {"operation":"native_cache_rebuild","kind":"complete_native_registry_prefix"}
        if cursor_proof is not None:
            result["delta"][SUMMARY] = cursor_proof["semantic"]
            if "combined_delivery_effect" in cursor_proof:
                result["delta"][SUMMARY_DELIVERY] = cursor_proof["combined_delivery_effect"]["semantic"]
        generated = {}
        receipt_transforms = []
        for path, change in semantic.items():
            for ordinal, row in enumerate(change.get("frames", [])):
                moment = _time(row["timestamp"])
                if not wall_begin - timedelta(milliseconds=1) <= moment <= wall_end:
                    raise ValueError("generated receipt clock outside measured invocation")
                identifier = row["id"]
                suffix = identifier.rsplit("-", 1)[-1]
                parsed = uuid.UUID(hex=suffix)
                if parsed.version != 7 or parsed.variant != uuid.RFC_4122:
                    raise ValueError("generated receipt ID is not native UUIDv7")
                born = datetime.fromtimestamp((parsed.int >> 80) / 1000, timezone.utc)
                if not wall_begin - timedelta(milliseconds=1) <= born <= wall_end:
                    raise ValueError("generated receipt identity outside measured invocation")
                generated[identifier] = "<generated-" + row["kind"] + "-" + str(ordinal) + ">"
                fields = {"id": generated[identifier], "timestamp": "<invocation-time>"}
                if path == PRESENCE:
                    fields.update(observed_at="<invocation-time>", expires_at="<invocation-time-plus-60s>")
                if path == HOLD:
                    fields["released_at" if case == "hold-release" else "started_at"] = "<invocation-time>"
                if path in TIMINGS.values():
                    start = _time(row["started_at"])
                    if not wall_begin - timedelta(milliseconds=1) <= start <= moment:
                        raise ValueError("timing start is outside native invocation")
                    for field in ("cpu_seconds", "wall_seconds"):
                        value = row[field]
                        if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or value != round(value, 3):
                            raise ValueError("timing duration is not native finite milliseconds")
                    if row["wall_seconds"] > (wall_end - wall_begin).total_seconds() + .001:
                        raise ValueError("timing exceeds retained wall duration")
                    if child_cpu_seconds is not None and row["cpu_seconds"] > child_cpu_seconds + .001:
                        raise ValueError("timing exceeds retained collected child CPU")
                    fields["started_at"] = "<invocation-start>"
                receipt_transforms.append((path, ordinal, row, fields))
        if case == "confluence-bundle":
            result["delta"]["@publication/routine-bundle.json"] = {"operation": "publish",
                "value": _bundle(root, context, wall_begin=wall_begin, wall_end=wall_end)}
        original = copy.deepcopy(result)

        def leaf(parts, expected, replacement):
            target = result
            for part in parts[:-1]:
                target = target[part]
            _equal(target[parts[-1]], expected, "audited native scalar")
            if expected == replacement:
                raise ValueError("routine audited leaf must change")
            target[parts[-1]] = replacement
            pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)
            result["normalizations"].append(dict(pointer=pointer,
                reason="complete native routine effect, exact source binding and invocation bounds validated"))

        for path, ordinal, row, fields in receipt_transforms:
            targets = [["delta", path, "frames", ordinal]]
            if ((path == CONSUMED and case == "consume") or path == HOLD or path == PRESENCE
                    or path == WORK or path == ACK and case == "ack"):
                targets.append(["artifact", "evidence"])
            if case == "inbox" and path in {ACK, DELIVERY}:
                targets.append(["artifact", "evidence", "acknowledgment" if path == ACK else "receipt"])
            for target in targets:
                for field, replacement in fields.items():
                    leaf([*target, field], row[field], replacement)
        if case in {"confluence-grant", "confluence-revoke"}:
            identifier = artifact["evidence"]["grant_id"]
            leaf(["artifact", "evidence", "grant_id"], identifier, generated[identifier])
        if case == "node-drain":
            identifier = artifact["evidence"]["acknowledgment_ids"][0]
            leaf(["artifact", "evidence", "acknowledgment_ids", 0], identifier, generated[identifier])
        if case == "inbox":
            leaf(["artifact", "evidence", "used_root"], str(home), "<bus-scale-fixture>")
            leaf(["artifact", "evidence", "scope", "root"], str(home), "<bus-scale-fixture>")
        if case == "intake-scan":
            leaf(["artifact", "evidence", "root"], str(home), "<bus-scale-fixture>")
            leaf(["artifact", "evidence", "directory"], context["directory"], "<routine-intake-source>")
        if case == "confluence-bundle":
            leaf(["artifact", "evidence", "out"], str(root.path.parent / context["publication_name"]), "<routine-publication>/bundle.json")
            publication = result["delta"]["@publication/routine-bundle.json"]["value"]
            for parts, expected, replacement in bundle_clock_leaves(publication):
                leaf(["delta", "@publication/routine-bundle.json", "value", *parts], expected, replacement)
        for path, change in result["delta"].items():
            if path.startswith(".floati-snapshots/") and change["operation"] == "replace":
                from floati.snapshot import _checksum
                envelope = change["value"]
                leaf(["delta", path, "value", "root"], str(home), "<bus-scale-fixture>")
                checksum = _checksum(envelope)
                if checksum != envelope["checksum"]:
                    envelope["checksum"] = checksum
                    result["normalizations"].append(dict(pointer="/delta/" + path.replace("/", "~1") + "/value/checksum",
                        reason="native complete snapshot checksum follows only its validated exact root-coordinate transform"))
        # Native timing values stay in raw evidence. Only their already-bound
        # explicit leaves receive placeholders, never fields of other records.
        for path in TIMINGS.values():
            if path in result["delta"]:
                row = result["delta"][path]["frames"][0]
                for field in ("cpu_seconds", "wall_seconds", "argv_digest"):
                    row[field] = "<native-" + field + ">"
                    result["normalizations"].append(dict(pointer="/delta/" + path.replace("/", "~1") + "/frames/0/" + field,
                        reason="native timing argv and bounded measured durations validated; raw measurements retained"))
        if after != _files(root.path):
            raise ValueError("routine evidence changed during normalization")
        restored, seen = copy.deepcopy(result), set()
        for audit in result["normalizations"]:
            pointer = audit["pointer"]
            if pointer in seen:
                raise ValueError("routine leaf audit repeats a coordinate")
            seen.add(pointer)
            old, target = original, restored
            parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
            for part in parts[:-1]:
                old = old[int(part)] if isinstance(old, list) else old[part]
                target = target[int(part)] if isinstance(target, list) else target[part]
            key = int(parts[-1]) if isinstance(target, list) else parts[-1]
            if isinstance(old[key], (dict, list)) or _json(old[key]) == _json(target[key]):
                raise ValueError("routine audit must change one validated scalar leaf")
            target[key] = copy.deepcopy(old[key])
        restored["normalizations"] = []
        _equal(restored, original, "complete explicit leaf-audit roundtrip")
        _json(result)
        return result
    except (FloatiError, OSError, KeyError, TypeError, UnicodeError, OverflowError) as exc:
        raise ValueError("native routine normalization unavailable: " + str(exc)) from exc
