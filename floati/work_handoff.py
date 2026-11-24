"""Local evidence capture and read-only projection for a claimed work item."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from .bound_record import (
    CLAIMED, OBSERVED, file_path, freshness, observe_files, observe_ref,
    resolve_repository, stamp_fact, tracked_dirty,
)
from .consumption import ConsumptionLedger
from .errors import IntegrityFailure, ProtocolRefusal
from .fleet_update import _explicit_executable
from .ids import uuid7_hex
from .jsonl import _locked_path, read_records_snapshot, transact
from .records import _bounded_string, _contract_string_list, _record_ref, validate_record
from .registry import Registry
from .work import _now, _timestamp


KINDS = frozenset({"work_handoff_record"})
_RUN_TIMEOUT_SECONDS = 300
_CHILD_PATH = "/usr/bin:/bin"
_COUNT_FD_ENV ="FLOATI_HANDOFF_COUNT_FD"
_UNNITEST_PROGRAM = (
    "import os, sys; "
    f"count_fd = int(os.environ.pop({_COUNT_FD_ENV!r})); "
    "os.set_inheritable(count_fd, False); "
    "import unittest; sys.path.insert(0, os.getcwd()); "
    "run = unittest.main(module=None, argv=['unittest', '--', *sys.argv[1:]], exit=False); "
    "os.write(count_fd, str(run.result.testsRun).encode('ascii')); os.close(count_fd); "
    "sys.exit(not run.result.wasSuccessful())"
)


def _refuse(code, detail, remedy=None):
    raise ProtocolRefusal(code, detail, remedy=remedy)


def command_argv(argv, refuse):
    if not isinstance(argv, list) or not 1 <= len(argv) <= 32:
        refuse("handoff_command_invalid", "a recorded command needs 1 to 32 arguments")
    for argument in argv:
        _bounded_string(argument, 0, 1024, "command_argument", refuse)
    if not Path(argv[0]).is_absolute():
        refuse("handoff_command_invalid", "the command executable must be an absolute path")


def _require_same_checkout(repo, coordinates):
    if freshness(coordinates)["state"] != "current" or tracked_dirty(repo):
        _refuse("checkout_changed", "the tracked checkout changed during handoff capture")


def _child_environment(scratch):
    # Nothing is inherited: a recorded command runs with a scratch home and a fixed path.
    return {"HOME": scratch, "TMPDIR": scratch, "PATH": _CHILD_PATH, "LANG": "C", "LC_ALL": "C"}


def _observe_run(repo, argv, *, is_unittest=False):
    with tempfile.TemporaryFile() as count_output, tempfile.TemporaryDirectory() as scratch:
        environment = _child_environment(scratch)
        channel = {}
        if is_unittest:
            count_fd = count_output.fileno()
            channel = {"pass_fds": (count_fd,)}
            environment[_COUNT_FD_ENV] = str(count_fd)
        try:
            child = subprocess.Popen(
                argv, cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, env=environment, **channel,
            )
        except OSError as error:
            raise ProtocolRefusal("handoff_run_unavailable", "the handoff command could not start") from error
        try:
            exit_code = child.wait(timeout=_RUN_TIMEOUT_SECONDS)
        except BaseException as error:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            if isinstance(error, subprocess.TimeoutExpired):
                raise ProtocolRefusal("handoff_run_timeout", "the handoff command exceeded its time limit") from error
            raise
        count_output.seek(0)
        output = count_output.read(24).strip()
    count = int(output) if is_unittest and output.isdigit() and len(output) < 20 else None
    has_count = count is not None and count < 2**63
    source = "not_instrumented"
    if is_unittest:
        source = "unittest_result" if has_count else "unittest_result_unavailable"
    return stamp_fact(
        OBSERVED, argv=argv, exit_code=exit_code,
        test_count=count if has_count else None, test_count_source=source,
    )


class WorkHandoffs:
    def __init__(self, root):
        self.root = root
        self.work = ConsumptionLedger(root)

    @staticmethod
    def _path(item_id):
        _record_ref(item_id, "work-", "work_item_id", _refuse)
        return Path("receipts/work-handoffs") / f"{item_id}.jsonl"

    def _work_snapshot(self, item_id):
        rows = self.work.records()
        item = self.work.project(rows).get(item_id)
        if item is None:
            _refuse("work_unknown", "work item does not exist")
        transitions = {
            row["action"]: row for row in rows
            if row["kind"] == "work_transition" and row["work_item_id"] == item_id
        }
        return item, transitions

    @staticmethod
    def _require_holder(item, actor):
        if item["holder"] is None:
            _refuse("work_not_claimed", "claim the work item before recording a handoff")
        if item["holder"] != actor:
            _refuse("work_holder_mismatch", "only the work claim holder may record a handoff")

    def record(self, item_id, actor, repository, commands, selectors, artifacts, remaining, statements,
               key, supersedes=None):
        Registry(self.root).require_active(actor)
        item, _ = self._work_snapshot(item_id)
        self._require_holder(item, actor)
        _bounded_string(repository, 1, 4096, "repository", _refuse)
        _bounded_string(key, 1, 128, "idempotency_key", _refuse)
        if not Path(repository).is_absolute():
            _refuse("coordinate_unresolved", "name the absolute path to the top of a Git checkout")
        try:
            repo = Path(repository).resolve()
        except (OSError, RuntimeError) as error:
            raise ProtocolRefusal("coordinate_unresolved", "the bound checkout could not be read") from error
        if len(commands) + bool(selectors) > 8 or len(selectors) > 28:
            _refuse("handoff_command_invalid", "record up to eight commands and 28 unittest selectors")
        if len(artifacts) > 64 or len(set(artifacts)) != len(artifacts):
            _refuse("handoff_artifacts_invalid", "name up to 64 distinct artifact files")
        for value in artifacts:
            file_path(value, _refuse)
        for field, items in (("remaining", remaining), ("statements", statements)):
            _contract_string_list(items, field, 0, 16, _refuse)
            for value in items:
                _bounded_string(value, 1, 512, field, _refuse)
        for selector in selectors:
            _bounded_string(selector, 1, 512, "unittest_selector", _refuse)
            if selector.startswith("-"):
                _refuse("unittest_selector_invalid", "name a unittest selector without options")
        for argv in commands:
            command_argv(argv, _refuse)
        if supersedes is not None:
            _record_ref(supersedes, "work-handoff-", "supersedes", _refuse)
        requests = [str(repo), commands, selectors, sorted(artifacts), remaining, statements, supersedes]
        encoded = json.dumps(requests, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(encoded) > 48000:
            _refuse("handoff_request_too_large", "shorten the command arguments or remaining work")
        request_digest = hashlib.sha256(encoded).hexdigest()
        commands = list(commands)

        def previous_result(rows):
            self._validate_journal(rows, item_id)
            for prior in rows:
                if prior["idempotency_key"] == key:
                    if prior["request_digest"] != request_digest or prior["actor"] != actor:
                        _refuse("idempotency_conflict", "the handoff key already names a different request")
                    return prior
            head_id = rows[-1]["id"] if rows else None
            if supersedes is not None and supersedes != head_id:
                _refuse("supersedes_not_head", "the named handoff is no longer the head of this work item")

        path = self._path(item_id)
        # Serialize attempts for one request key without occupying the journal.
        # Other keys can append while this request observes its checkout.
        key_digest = hashlib.sha256(json.dumps(key, ensure_ascii=True).encode("ascii")).hexdigest()
        relative = Path("state/work-handoff-captures") / item_id / (key_digest + ".lock")
        with _locked_path(self.root.resolve_relative(relative), exclusive=True,
                          relative=relative, order_tracked=False):
            prior = transact(self.root, path, lambda rows: (previous_result(rows), None), allowed_kinds=KINDS)
            if prior is not None:
                return prior
            checkout = resolve_repository(str(repo))
            original_checkout = checkout.stat()
            coordinates = [observe_ref(checkout)]
            if tracked_dirty(checkout):
                _refuse("checkout_dirty", "record the handoff from a clean tracked checkout")
            for argv in commands:
                _explicit_executable(argv[0], "handoff_executable_invalid")
            runs = []
            for argv in commands:
                runs.append(_observe_run(checkout, argv))
                _require_same_checkout(checkout, coordinates)
            if selectors:
                python = _explicit_executable(Path(sys.executable).resolve(), "handoff_executable_invalid")
                argv = [python, "-I", "-B", "-c", _UNNITEST_PROGRAM, *selectors]
                runs.append(_observe_run(checkout, argv, is_unittest=True))
            if artifacts:
                coordinates.append(observe_files(checkout, artifacts))
            _require_same_checkout(checkout, coordinates)

            def decide(rows):
                prior = previous_result(rows)
                if prior is not None:
                    return prior, None
                Registry(self.root).require_active(actor)
                item, transitions = self._work_snapshot(item_id)
                self._require_holder(item, actor)
                try:
                    same_directory = os.path.samestat(checkout.lstat(), original_checkout)
                except OSError:
                    same_directory = False
                if not same_directory:
                    _refuse("checkout_changed", "the tracked checkout changed during handoff capture")
                completion = transitions.get("complete")
                row = {
                    "schema_version": 1,
                    "kind": "work_handoff_record",
                    "id": "work-handoff-" + uuid7_hex(),
                    "tenant_id": self.root.tenant_id,
                    "timestamp": _timestamp(_now(None)),
                    "work_item_id": item_id,
                    "actor": actor,
                    "work_receipts": stamp_fact(
                        OBSERVED, claim_id=transitions["claim"]["id"],
                        completion_id=completion["id"] if completion is not None else None,
                    ),
                    "coordinates": coordinates,
                    "supersedes": rows[-1]["id"] if rows else None,
                    "runs": runs,
                    "remaining": [stamp_fact(CLAIMED, text=value) for value in remaining],
                    "statements": [stamp_fact(CLAIMED, text=value) for value in statements],
                    "idempotency_key": key,
                    "request_digest": request_digest,
                }
                validate_record(row, self.root.tenant_id, KINDS, integrity=False)
                return row, row

            return transact(self.root, path, decide, allowed_kinds=KINDS)

    @staticmethod
    def _validate_journal(rows, item_id):
        keys = set()
        previous = None
        for row in rows:
            key = row["idempotency_key"]
            if row["work_item_id"] != item_id or key in keys or row["supersedes"] != previous:
                raise IntegrityFailure("handoff_journal_conflict", "handoff journal coordinates disagree")
            keys.add(key)
            previous = row["id"]

    def show(self, item_id, handoff_id=None):
        projection = {"contract": "bound-record", "contract_version": 1,
                      "observed_at": _timestamp(_now(None)), "records": []}
        # Every handoff appends after its work transitions. Read in reverse
        # order so a concurrent completion cannot appear to lack its receipt.
        rows = read_records_snapshot(self.root, self._path(item_id), allowed_kinds=KINDS)
        self._validate_journal(rows, item_id)
        item, transitions = self._work_snapshot(item_id)
        index = len(rows) - 1
        if handoff_id is not None:
            index = next((i for i, row in enumerate(rows) if row["id"] == handoff_id), -1)
            if index < 0:
                _refuse("handoff_unknown", "the work item has no handoff with that identifier")
        if not rows:
            return {**projection, "evidence_state": "none"}
        row = rows[index]
        receipts = row["work_receipts"]
        if (row["actor"] != item["holder"]
                or receipts["claim_id"] != transitions.get("claim", {}).get("id")
                or (receipts["completion_id"] is not None
                    and receipts["completion_id"] != transitions.get("complete", {}).get("id"))):
            raise IntegrityFailure("handoff_work_conflict", "handoff and work receipt coordinates disagree")
        state = "claimed" if row["statements"] else "none"
        if row["runs"] or len(row["coordinates"]) > 1:
            state = "recorded"
        next_id = rows[index + 1]["id"] if index + 1 < len(rows) else None
        projection["records"].append({
            "record": row,
            "freshness": freshness(row["coordinates"]),
            "chain": {"head": next_id is None, "superseded_by": next_id},
            "closed": None,
            "evidence_state": state,
        })
        return projection
