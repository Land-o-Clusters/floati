"""Parent-owned dispatch journals and local, content-addressed brief bodies."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from contextlib import nullcontext
from pathlib import Path

from .bound_record import CLAIMED, OBSERVED, _git, file_path, freshness, observe_ref, resolve_repository, stamp_fact
from .bus_epoch import epoch_guard
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import (
    MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, _decode_bounded_frames, _durability_failure,
    _ensure_directory, transact,
)
from .records import _bounded_string, _git_sha, _record_ref, _sha256, validate_record, validate_unknown_record
from .registry import Registry
from .session_identity import IDENTIFIER_PATTERN, validate_identifier
from .work import _now, _timestamp


DIRECTORY = Path(".floati-dispatches")
KINDS = frozenset({"dispatch_revision", "dispatch_return", "dispatch_close"})
MAX_BRIEF_BYTES = 64 * 1024
MAX_REVISION = 2**63 - 1


def _refuse(code, detail, remedy=None):
    raise ProtocolRefusal(code, detail, remedy=remedy)


def _branch(branch, refuse):
    _bounded_string(branch, 1, 480, "branch", refuse)
    if branch.startswith(("-", "refs/")):
        refuse("dispatch_branch_invalid", "name the short delivery branch")


def _revision(value, refuse):
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        refuse("dispatch_revision_invalid", "name a positive brief revision")


def validate_dispatch_record(row, refuse):
    def fact(value, fields, stamp):
        if isinstance(value, dict) and "provenance" in value and value["provenance"] not in (CLAIMED, OBSERVED):
            refuse("dispatch_provenance_unsupported", "dispatch provenance is unsupported by this reader")
        if (not isinstance(value, dict) or set(value) != fields | {"provenance"}
                or value["provenance"] != stamp):
            refuse("dispatch_fact_invalid", "dispatch fact fields or provenance do not match the record kind")

    for field in ("actor", "dispatch_id"):
        _bounded_string(row[field], 1, 64, field, refuse)
        if not IDENTIFIER_PATTERN.fullmatch(row[field]):
            refuse("dispatch_identifier_invalid", "dispatch coordinates must use bounded identifiers")
    _bounded_string(row["idempotency_key"], 1, 128, "idempotency_key", refuse)
    _sha256(row["request_digest"], "request_digest", refuse)
    if row["supersedes"] is not None:
        _record_ref(row["supersedes"], "dispatch-revision-", "supersedes", refuse)

    kind = row["kind"]
    coordinates = row["coordinates"]
    if kind != "dispatch_return" and coordinates != []:
        refuse("dispatch_coordinates_invalid", "only dispatch returns carry coordinates")
    if kind == "dispatch_revision":
        _revision(row["revision"], refuse)
        _bounded_string(row["recipient"], 1, 64, "recipient", refuse)
        if not IDENTIFIER_PATTERN.fullmatch(row["recipient"]):
            refuse("dispatch_identifier_invalid", "dispatch coordinates must use bounded identifiers")
        _branch(row["branch"], refuse)
        brief = row["brief"]
        fact(brief, {"sha256", "bytes"}, OBSERVED)
        _sha256(brief["sha256"], "brief_sha256", refuse)
        if type(brief["bytes"]) is not int or not 1 <= brief["bytes"] <= MAX_BRIEF_BYTES:
            refuse("dispatch_brief_invalid", "brief size must be between one byte and 64 KiB")
        carrier = row["carrier"]
        if isinstance(carrier, dict) and carrier.get("kind") == "envelope":
            fact(carrier, {"kind", "envelope_id"}, OBSERVED)
            _record_ref(carrier["envelope_id"], "msg-", "envelope_id", refuse)
        else:
            fact(carrier, {"kind", "label"}, CLAIMED)
            if carrier["kind"] != "external":
                refuse("dispatch_carrier_invalid", "name an external carrier or a Floati envelope")
            _bounded_string(carrier["label"], 1, 160, "carrier_label", refuse)
        return

    if row["supersedes"] is not None:
        refuse("dispatch_supersedes_invalid", "only a brief revision can supersede a previous row")
    if kind == "dispatch_close":
        _record_ref(row["closes"], "dispatch-revision-", "closes", refuse)
        fact(row["reason"], {"text"}, CLAIMED)
        _bounded_string(row["reason"]["text"], 1, 512, "reason", refuse)
        return

    if not isinstance(coordinates, list) or len(coordinates) != 2:
        refuse("dispatch_coordinates_invalid", "a return binds the brief and delivery branch")
    brief, ref = coordinates
    if any(isinstance(coordinate, dict) and coordinate.get("kind") not in ("brief", "ref")
           for coordinate in coordinates):
        refuse("coordinate_kind_unsupported", "a dispatch return supports brief and branch coordinates")
    fact(brief, {"kind", "dispatch_id", "revision"}, CLAIMED)
    fact(ref, {"kind", "repository", "ref", "commit", "paths"}, OBSERVED)
    if brief["kind"] != "brief" or ref["kind"] != "ref":
        refuse("coordinate_kind_unsupported", "a dispatch return supports brief and branch coordinates")
    if brief["dispatch_id"] != row["dispatch_id"] or ref["paths"] != []:
        refuse("dispatch_coordinates_invalid", "a return binds the same dispatch and an unfiltered branch")
    if brief["revision"] is not None:
        _revision(brief["revision"], refuse)
    _bounded_string(ref["repository"], 1, 4096, "repository", refuse)
    if not Path(ref["repository"]).is_absolute():
        refuse("dispatch_coordinates_invalid", "the delivery repository must be absolute")
    _bounded_string(ref["ref"], 1, 512, "ref", refuse)
    if not ref["ref"].startswith("refs/heads/") or len(ref["ref"]) == len("refs/heads/"):
        refuse("dispatch_coordinates_invalid", "bind a delivery branch with its full Git ref")
    _git_sha(ref["commit"], refuse)
    fact(row["artifact"], {"path"}, CLAIMED)
    file_path(row["artifact"]["path"], refuse)
    if row["statement"] is not None:
        fact(row["statement"], {"text"}, CLAIMED)
        _bounded_string(row["statement"]["text"], 1, 512, "statement", refuse)


def _read_body(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _refuse("dispatch_brief_invalid", "supply an ordinary UTF-8 brief file")
        body = stream.read(MAX_BRIEF_BYTES + 1)
    if not 1 <= len(body) <= MAX_BRIEF_BYTES:
        _refuse("dispatch_brief_invalid", "brief size must be between one byte and 64 KiB")
    return body


def _journal_state(rows, dispatch_id):
    revisions, returns, keys = [], [], {}
    closed = None
    for row in rows:
        kind = row["kind"]
        head = revisions[-1] if revisions else None
        if (row["dispatch_id"] != dispatch_id or row["idempotency_key"] in keys or closed is not None
                or (head is not None and row["actor"] != head["actor"])):
            break
        if kind == "dispatch_revision":
            if (row["revision"] != len(revisions) + 1
                    or row["supersedes"] != (head["id"] if head else None)
                    or (head and row["recipient"] != head["recipient"])):
                break
            revisions.append(row)
        elif head is None:
            break
        elif kind == "dispatch_close":
            if row["closes"] != head["id"]:
                break
            closed = row
        else:
            brief, ref = row["coordinates"]
            target = brief["revision"] or len(revisions)
            if target > len(revisions) or ref["ref"] != "refs/heads/" + revisions[target - 1]["branch"]:
                break
            returns.append((row, len(revisions)))
        keys[row["idempotency_key"]] = row
    else:
        return revisions, returns, closed, keys
    raise IntegrityFailure("dispatch_journal_conflict", "dispatch journal coordinates disagree")


class DispatchRecords:
    def __init__(self, root):
        self.root = root

    def _path(self, relative):
        path = self.root.tenant_home / relative
        if self.root.resolve_relative(relative) != path:
            _refuse("dispatch_storage_redirected", "dispatch storage is redirected away from its local root",
                    "remove the redirect before using dispatch records")
        return path

    def _journal(self, dispatch_id):
        validate_identifier(dispatch_id, "dispatch_id")
        relative = DIRECTORY / (dispatch_id + ".jsonl")
        self._path(relative)
        return relative

    def _rows(self, dispatch_id, *, refused=None):
        path = self._path(self._journal(dispatch_id))
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                status = os.fstat(fd)
                if not stat.S_ISREG(status.st_mode):
                    raise IntegrityFailure("dispatch_journal_invalid", "dispatch journal must be an ordinary file")
                data = stream.read(min(MAX_LEDGER_BYTES + 1, status.st_size))
        except FileNotFoundError:
            return []
        except OSError as error:
            raise _durability_failure(error, path) from error
        frames = _decode_bounded_frames(path, data)
        if len(frames) > MAX_LEDGER_RECORDS:
            raise IntegrityFailure("ledger_record_limit", "dispatch journal exceeds the record limit")
        records, seen = [], set()
        for raw in frames:
            try:
                row = validate_record(raw, self.root.tenant_id, KINDS, integrity=True)
            except IntegrityFailure as error:
                if refused is None or error.code not in {
                    "schema_version_invalid", "coordinate_kind_unsupported", "dispatch_provenance_unsupported",
                }:
                    raise
                unknown = validate_unknown_record(raw, self.root.tenant_id)
                row = None
                refused.append({"record_id": unknown["id"], "code": error.code})
            record_id = raw["id"]
            if record_id in seen:
                raise IntegrityFailure("duplicate_record_id", "dispatch journal has a duplicate record ID")
            seen.add(record_id)
            if row is not None:
                records.append(row)
        return records

    def _store_body(self, digest, data):
        directory = self._path(DIRECTORY / "briefs")
        _ensure_directory(directory)
        destination = self._path(DIRECTORY / "briefs" / digest)
        try:
            with tempfile.NamedTemporaryFile(dir=directory) as staging:
                staging.write(data)
                staging.flush()
                os.fsync(staging.fileno())
                try:
                    os.link(staging.name, destination)
                except FileExistsError:
                    self._load_body({"sha256": digest, "bytes": len(data)})
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError as error:
            raise _durability_failure(error, destination) from error

    def _load_body(self, brief):
        digest = brief["sha256"]
        try:
            data = _read_body(self._path(DIRECTORY / "briefs" / digest))
            if len(data) == brief["bytes"] and hashlib.sha256(data).hexdigest() == digest:
                return data.decode("utf-8")
        except (OSError, UnicodeError, ProtocolRefusal):
            pass
        raise IntegrityFailure("dispatch_brief_unavailable", "stored brief bytes are missing or disagree with their receipt")

    def _write(self, dispatch_id, actor, key, kind, request, fields):
        journal = self._journal(dispatch_id)
        Registry(self.root).require_active(actor)
        _bounded_string(key, 1, 128, "idempotency_key", _refuse)
        encoded = json.dumps([kind, actor, request], ensure_ascii=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()
        directory = self._path(DIRECTORY)
        _ensure_directory(directory)

        def decide(rows):
            revisions, _, closed, keys = _journal_state(rows, dispatch_id)
            head = revisions[-1] if revisions else None
            if head is not None and head["actor"] != actor:
                _refuse("dispatch_actor_mismatch", "only the seat that sent the brief may write to its dispatch journal",
                        "record the return or correction as the dispatch owner")
            prior = keys.get(key)
            if prior is not None:
                if prior["request_digest"] != digest:
                    _refuse("idempotency_conflict", "the dispatch key already names a different request",
                            "use a new key for a new request")
                return prior, None
            if closed is not None:
                _refuse("dispatch_closed", "the dispatch is already closed", "use a new dispatch ID")
            if head is None and kind != "dispatch_revision":
                _refuse("dispatch_unknown", "the dispatch has no recorded brief", "record the initial dispatch first")
            row = {
                "schema_version": 1, "kind": kind, "id": kind.replace("_", "-") + "-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id, "timestamp": _timestamp(_now(None)),
                "actor": actor, "dispatch_id": dispatch_id, "idempotency_key": key,
                "request_digest": digest, "supersedes": None, "coordinates": [],
            }
            row.update(fields(revisions))
            validate_record(row, self.root.tenant_id, KINDS, integrity=False)
            return row, row

        return transact(self.root, journal, decide, allowed_kinds=KINDS)

    def record(self, dispatch_id, actor, key, recipient, branch, brief_file, *,
               external=None, envelope_id=None, supersedes=None):
        _branch(branch, _refuse)
        if branch == "HEAD":
            _refuse("dispatch_branch_invalid", "name a valid short Git branch")
        try:
            _git(self.root.tenant_home, "check-ref-format", "refs/heads/" + branch)
        except ProtocolRefusal as error:
            if error.code != "coordinate_git_failed":
                raise
            raise ProtocolRefusal("dispatch_branch_invalid", "name a valid short Git branch") from error
        validate_identifier(recipient, "recipient")
        if (external is None) == (envelope_id is None):
            _refuse("dispatch_carrier_invalid", "select exactly one dispatch carrier",
                    "name an external carrier or a Floati envelope")
        if external is not None:
            _bounded_string(external, 1, 160, "carrier_label", _refuse)
        if envelope_id is not None:
            _record_ref(envelope_id, "msg-", "envelope_id", _refuse)
        if supersedes is not None:
            _record_ref(supersedes, "dispatch-revision-", "supersedes", _refuse)
        try:
            data = _read_body(Path(brief_file))
            data.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise ProtocolRefusal("dispatch_brief_invalid", "the brief file is not readable UTF-8",
                                  remedy="supply an ordinary UTF-8 brief file") from error
        digest = hashlib.sha256(data).hexdigest()

        def fields(revisions):
            head = revisions[-1] if revisions else None
            if supersedes != (head["id"] if head else None):
                _refuse("supersedes_not_head", "a correction must name the current brief receipt",
                        "read dispatch show and supply the current receipt ID")
            if head and head["recipient"] != recipient:
                _refuse("dispatch_recipient_mismatch", "a correction must keep the original recipient",
                        "use a new dispatch ID for a different recipient")
            if envelope_id is None:
                carrier = stamp_fact(CLAIMED, kind="external", label=external)
            else:
                from .events import EventLog
                message = next((row for row in EventLog(self.root).records() if row["id"] == envelope_id), None)
                if message is None or (message["sender"], message["recipient"]) != (actor, recipient):
                    _refuse("dispatch_envelope_mismatch", "the envelope must name this sender and recipient",
                            "supply the exact receipt from the Floati send")
                carrier = stamp_fact(OBSERVED, kind="envelope", envelope_id=envelope_id)
            self._store_body(digest, data)
            return {"revision": len(revisions) + 1, "recipient": recipient, "branch": branch,
                    "brief": stamp_fact(OBSERVED, sha256=digest, bytes=len(data)), "carrier": carrier,
                    "supersedes": supersedes}

        # Envelope resolution takes the epoch lock before the dispatch journal.
        with epoch_guard(self.root) if envelope_id is not None else nullcontext():
            return self._write(dispatch_id, actor, key, "dispatch_revision",
                               [recipient, branch, digest, external, envelope_id, supersedes], fields)

    def record_return(self, dispatch_id, actor, key, revision, repository, ref, artifact, statement):
        if revision is not None:
            _revision(revision, _refuse)
        file_path(artifact, _refuse)
        if statement is not None:
            _bounded_string(statement, 1, 512, "statement", _refuse)
        _bounded_string(repository, 1, 4096, "repository", _refuse)
        _bounded_string(ref, 1, 512, "ref", _refuse)

        def fields(revisions):
            if revision is not None and revision > len(revisions):
                _refuse("coordinate_unresolved", "the return names a brief revision that was never recorded",
                        "read dispatch show and name a recorded revision")
            target = revisions[(revision or len(revisions)) - 1]
            if ref != "refs/heads/" + target["branch"]:
                _refuse("coordinate_unresolved", "the returned ref must match the delivery branch",
                        "supply the full Git ref for the branch named by the brief")
            repo = resolve_repository(repository)
            coordinate = observe_ref(repo, ref)
            if observe_ref(repo, ref) != coordinate:
                _refuse("checkout_changed", "the delivery branch changed during return capture",
                        "retry once the delivery branch is stable")
            return {
                "coordinates": [stamp_fact(CLAIMED, kind="brief", dispatch_id=dispatch_id, revision=revision),
                                coordinate],
                "artifact": stamp_fact(CLAIMED, path=artifact),
                "statement": None if statement is None else stamp_fact(CLAIMED, text=statement),
            }

        return self._write(dispatch_id, actor, key, "dispatch_return",
                           [revision, repository, ref, artifact, statement], fields)

    def close(self, dispatch_id, actor, key, reason):
        _bounded_string(reason, 1, 512, "reason", _refuse)
        return self._write(dispatch_id, actor, key, "dispatch_close", [reason], lambda revisions: {
            "closes": revisions[-1]["id"], "reason": stamp_fact(CLAIMED, text=reason),
        })

    def brief(self, dispatch_id, revision, actor):
        _revision(revision, _refuse)
        row = self.revision(dispatch_id, revision)
        if row["actor"] != actor:
            _refuse("dispatch_actor_mismatch", "only the sending seat can read the stored brief",
                    "read the brief as the seat that recorded it")
        return {"dispatch_id": dispatch_id, "revision": revision, "text": self._load_body(row["brief"])}

    def revision(self, dispatch_id, revision):
        revisions, _, _, _ = _journal_state(self._rows(dispatch_id), dispatch_id)
        if not 1 <= revision <= len(revisions):
            _refuse("dispatch_unknown", "the selected brief revision does not exist",
                    "read dispatch show and name a recorded revision")
        return revisions[revision - 1]

    def show(self, dispatch_id=None):
        projection = {"contract": "bound-record", "contract_version": 1,
                      "observed_at": _timestamp(_now(None)), "records": [], "roster": [], "refusals": []}
        if dispatch_id is None:
            directory = self._path(DIRECTORY)
            dispatch_ids = sorted(path.stem for path in directory.glob("*.jsonl"))
        else:
            dispatch_ids = [dispatch_id]
        for identifier in dispatch_ids:
            try:
                refused = []
                rows = self._rows(identifier, refused=refused)
                if refused:
                    projection["refusals"].extend({"dispatch_id": identifier, **row} for row in refused)
                    # A future row may change the head or close the chain.
                    projection["records"].extend({
                        "record": row,
                        "freshness": {"state": "unknown", "coordinates": [
                            {"state": "unknown", "cause": {"code": "dispatch_history_unsupported"}, "moved": []}
                            for _ in row["coordinates"]
                        ]},
                        "chain": {"head": None, "superseded_by": None},
                        "closed": None,
                    } for row in rows)
                    continue
                state = _journal_state(rows, identifier)
            except (DurabilityFailure, IntegrityFailure, ProtocolRefusal) as error:
                projection["refusals"].append({"dispatch_id": identifier, "code": error.code})
                continue
            if rows:
                records, roster = _project(rows, *state[:3])
                projection["records"].extend(records)
                projection["roster"].append(roster)
        return projection


def _project(rows, revisions, returns, closed):
    from .replay_history import project_asked

    head = revisions[-1]
    successors = {row["supersedes"]: row["id"] for row in revisions[1:]}
    ceilings = {row["id"]: ceiling for row, ceiling in returns}
    ref_cache = {}
    results = []
    states = []
    for row in rows:
        row_id = row["id"]
        if row_id in ceilings:
            brief, ref = row["coordinates"]
            revision = brief["revision"]
            before = revision or ceilings[row_id]
            current = head["revision"]
            moved = [{"what": "brief", "dispatch_id": row["dispatch_id"], "from": before, "to": current}]
            brief_state = "stale" if current > before else ("unbound" if revision is None else "current")
            ref_key = (ref["repository"], ref["ref"], ref["commit"])
            if ref_key not in ref_cache:
                ref_cache[ref_key] = freshness([ref])["coordinates"][0]
            ref_verdict = ref_cache[ref_key]
            candidates = {brief_state, ref_verdict["state"]}
            state = next((x for x in ("stale", "unknown", "unbound") if x in candidates), "current")
            states.append(state)
            verdict = {"state": state, "coordinates": [
                {"state": brief_state, "cause": None, "moved": moved if brief_state == "stale" else []},
                ref_verdict,
            ]}
        else:
            verdict = freshness([])
        successor = successors.get(row_id)
        results.append({"record": row, "freshness": verdict,
                        "chain": {"head": successor is None, "superseded_by": successor},
                        "closed": closed["id"] if closed and row_id == closed["closes"] else None})
    if closed:
        state = "closed"
    else:
        state = next((label for key, label in (
            ("current", "returned"), ("stale", "returned_stale"),
            ("unbound", "returned_unbound"), ("unknown", "returned"),
        ) if key in states), "out")
    return results, {field: head[field] for field in ("dispatch_id", "recipient", "branch", "revision")} | {
        "state": state, "head_id": head["id"], "asked": project_asked(head),
    }
