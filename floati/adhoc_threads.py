"""Parent-owned native turn, explicit mail disposition, and receipt recovery.

The dispatch journal is the recovery boundary. A saved spawn reservation never
authorizes another spawn. Conversation storage is retained, never exported or
used as work, delivery, process-cleanup, or publication truth.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import unicodedata

from .adhoc_execution import prepare_adhoc_execution
from .adhoc_journal import AdhocJournal, AdhocJournalController, _blob, sender_request
from .adhoc_profiles import AdhocProfiles, current_time
from .adhoc_records import canonical_json, refuse
from .adhoc_workspace import BorrowedWorkspace
from .cursor import SparseCursor
from .bus_epoch import epoch_guard
from .errors import ProtocolRefusal
from .events import EventLog
from .git_process import fixed_git_command, fixed_git_environment
from .message_consumption import MessageConsumption
from .native_thread_protocol import NativeThreadProtocol
from .root import FloatiRoot, validate_identifier
from .wake_control import validate_session_id
from .work import WorkLog
from .worker_bootstrap_protocol import NATIVE_THREAD_BOUNDS
from .workers import WorkerReceipts, WorkerRunner
from .waiter_lock_retry import transient_lock_timeout


MAX_PROMPT_BYTES = 1_048_576
MAX_RESULT_BYTES = 1_048_576
_RESULT_FIELDS = {"schema_version", "dispatch_digest", "provider_coordinate", "summary", "inbox_dispositions"}
_DISPOSITION_FIELDS = {"message_id", "disposition", "reason", "result_paths"}


def _git(repository, *arguments, maximum=MAX_RESULT_BYTES):
    try:
        result = subprocess.run(fixed_git_command("/usr/bin/git", Path(repository), arguments),
            env=fixed_git_environment("/usr/bin/git"), capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProtocolRefusal("adhoc_binding_changed", "exact result Git inspection is unavailable") from exc
    if result.returncode or len(result.stdout) > maximum or len(result.stderr) > 65_536:
        refuse("adhoc_binding_changed", "exact result Git inspection failed or exceeded its bound")
    return result.stdout


def _relative(value):
    try:
        size = len(value.encode("utf-8", "strict")) if type(value) is str else 0
    except UnicodeError:
        refuse("adhoc_result_disposition_invalid", "result path is not valid UTF-8")
    if (type(value) is not str or not value or size > 4096
        or Path(value).is_absolute() or str(Path(value)) != value
        or any(part.casefold() in {"", ".", "..", ".git"} for part in value.split("/"))
        or any(unicodedata.category(character).startswith("C") for character in value)):
        refuse("adhoc_result_disposition_invalid", "result paths must be exact ordinary project-relative paths")
    return value


def _prepare_result_parent(workspace, relative):
    """Create only the declared result's parents beneath the held row inode."""
    parts = _relative(relative).split("/")
    descriptor = os.dup(workspace)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            try:
                if os.fstat(child).st_uid != os.geteuid():
                    refuse("adhoc_result_disposition_invalid", "result parent must be an owned ordinary directory")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        raise ProtocolRefusal("adhoc_result_disposition_invalid", "result parent is not an ordinary project path") from exc
    finally:
        os.close(descriptor)


def _read_result(workspace, relative):
    """Read one ordinary inode through no-follow parent descriptors."""
    parts = _relative(relative).split("/")
    descriptor = (os.dup(workspace) if type(workspace) is int else
                  os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            before = os.fstat(child)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or not 0 < before.st_size <= MAX_RESULT_BYTES):
                refuse("adhoc_result_disposition_invalid", "result must be a bounded owned ordinary file")
            data = bytearray()
            while len(data) <= MAX_RESULT_BYTES:
                chunk = os.read(child, min(65_536, MAX_RESULT_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            after = os.fstat(child)
            if (len(data) != before.st_size or len(data) > MAX_RESULT_BYTES
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                refuse("adhoc_result_disposition_invalid", "result changed during its bounded read")
            return bytes(data)
        finally:
            os.close(child)
    except OSError as exc:
        raise ProtocolRefusal("adhoc_result_disposition_invalid", "result or parent is not an ordinary project path") from exc
    finally:
        os.close(descriptor)


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _text(value, maximum):
    if type(value) is not str:
        return False
    try:
        return bool(value.strip()) and len(value.encode("utf-8", "strict")) <= maximum
    except UnicodeError:
        return False


def _parse_result(raw, admission, coordinate, message_ids):
    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ProtocolRefusal("adhoc_result_disposition_invalid", "result must be bounded strict unique-key JSON") from exc
    if (type(value) is not dict or set(value) != _RESULT_FIELDS
        or type(value["schema_version"]) is not int or value["schema_version"] != 1
        or value["dispatch_digest"] != admission["dispatch_digest"]
        or value["provider_coordinate"] != coordinate or not _text(value["summary"], 4096)):
        refuse("adhoc_result_disposition_invalid", "result identity or closed version-one fields differ")
    dispositions = value["inbox_dispositions"]
    if type(dispositions) is not list or len(dispositions) != len(message_ids):
        refuse("adhoc_result_disposition_invalid", "result must disposition the complete frozen startup batch")
    seen, paths = set(), set()
    for entry in dispositions:
        if (type(entry) is not dict or set(entry) != _DISPOSITION_FIELDS
            or type(entry["message_id"]) is not str or entry["message_id"] not in message_ids
            or entry["message_id"] in seen or type(entry["disposition"]) is not str
            or entry["disposition"] not in {"acted", "unacted"} or not _text(entry["reason"], 1024)
            or type(entry["result_paths"]) is not list or len(entry["result_paths"]) > 1000):
            refuse("adhoc_result_disposition_invalid", "result includes missing, repeated, foreign, or invalid dispositions")
        selected = [_relative(path) for path in entry["result_paths"]]
        if len(set(selected)) != len(selected):
            refuse("adhoc_result_disposition_invalid", "result path references must be unique per disposition")
        seen.add(entry["message_id"])
        paths.update(selected)
    if seen != set(message_ids) or len(paths | {admission["dispatch"]["result_document"]}) > 32:
        refuse("adhoc_result_disposition_invalid", "result disposition paths exceed the exact parent banking bound")
    return value, sorted(paths)


def provider_state_size(root, profile):
    """Share Doctor's metadata-only, inode-pinned provider-history census."""
    from .projection import provider_state_size as inspect_size
    return inspect_size(root, profile)["value"]


def _inspect_closed_successor(lanes, profile, opening):
    """Prove any path occupant is a separate native opening; never mutate it."""
    active = [row for row in lanes._open_records(profile["node_id"])
              if row["path"] == opening["path"]]
    path = Path(profile["workspace"])
    if not path.exists() and not path.is_symlink():
        if active:
            refuse("adhoc_teardown_blocked", "a recorded successor has no verifiable native row binding")
        return None
    if (len(active) != 1 or active[0]["id"] == opening["id"]
        or active[0]["repo"] != profile["repository"]):
        refuse("adhoc_teardown_blocked", "the former row path is occupied without a distinct native successor proof")
    declared, repositories = lanes._declarations()
    binding = lanes._owned_binding(active[0], declared, repositories)
    if Path(binding["workspace"]) != path:
        refuse("adhoc_teardown_blocked", "the native successor belongs to a different row coordinate")
    return active[0]["id"]


class AdhocThreadController:
    def __init__(self, root, *, clock=lambda: datetime.now(timezone.utc)):
        if type(root) is not FloatiRoot or not callable(clock):
            refuse("root_required", "ad-hoc controller requires a validated root and clock")
        self.root = root
        self.clock = clock
        self.journal = AdhocJournal(root)
        self.writer = AdhocJournalController(self.journal)
        self.profiles = AdhocProfiles(root)
        self.events = EventLog(root)
        self.work = WorkLog(root)
        self._observed = None
        self._execution_owner = None
        self._workspace_owner = None

    def _now(self):
        current = current_time(self.clock())
        # A backward UTC observation cannot revive this controller's lease.
        if self._observed is None or current > self._observed:
            self._observed = current
        return self._observed

    def _record(self, dispatch_id, phase, evidence):
        caller = sys._getframe(1)
        if caller.f_code not in _LIFECYCLE_PRODUCER_CODES or caller.f_locals.get("self") is not self:
            refuse("adhoc_dispatch_authentication_failed", "lifecycle records require their exact native controller producer")
        # Keep the sealed writer's direct producer frame intact on every try.
        deadline = time.monotonic() + 30
        while True:
            try:
                return self.writer.append(dispatch_id, phase, evidence, now=self._now())
            except ProtocolRefusal as exc:
                if not transient_lock_timeout(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    @staticmethod
    def _parent_operation(operation):
        """Wait for another sender's parent transaction without relaunching.

        Only native lock-acquisition timeouts qualify. Every selected operation
        is a read or an existing idempotent parent receipt/recovery operation;
        neither WorkerRunner.run_exact nor any provider command is retried.
        """
        deadline = time.monotonic() + 30
        while True:
            try:
                return operation()
            except ProtocolRefusal as exc:
                if not transient_lock_timeout(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def _prepare_execution(self, profile, admission):
        return prepare_adhoc_execution(self.root, profile, admission, self._now)

    def _history(self, dispatch_id):
        return [row for row in self.journal.records() if row["dispatch_id"] == dispatch_id
                and row["kind"] != "adhoc_dispatch_record"]

    @staticmethod
    def _phase(rows, phase, stage=None):
        return next((row for row in reversed(rows) if row["phase"] == phase
                     and (stage is None or row["evidence"].get("stage") == stage)), None)

    def _profile(self, profile_id):
        selected = [row for row in self.profiles.records() if row["id"] == profile_id]
        if len(selected) != 1:
            refuse("adhoc_binding_changed", "dispatch profile is not one exact durable binding")
        return selected[0]

    def _guard(self, admission, *, clean=False):
        dispatch = admission["dispatch"]
        current = self._now()
        try:
            with self.profiles.validated(admission["profile_id"], now=current,
                                        require_clean=clean) as (profile, binding):
                self.work._require_authority(dispatch["authority_subject"], admission["actor"],
                    dispatch["authority_epoch"], current)
                return deepcopy(profile), deepcopy(binding)
        except ProtocolRefusal as exc:
            if exc.code in {"adhoc_lease_invalid", "authority_inactive", "node_lease_expired"}:
                raise ProtocolRefusal("adhoc_lease_ended", "the exact dispatch lease or authority has ended") from exc
            raise

    def _admission(self, profile_id, envelope_id, actor, session, key):
        prior = [row for row in self.journal.records() if row["kind"] == "adhoc_dispatch_record"
                 and row["idempotency_key"] == key]
        if prior:
            admitted = prior[0]
            if any(admitted[field] != value for field, value in
                   (("profile_id", profile_id), ("envelope_id", envelope_id),
                    ("actor", actor), ("acting_session_id", session))):
                refuse("adhoc_dispatch_key_conflict", "dispatch key names another immutable controller invocation")
            return admitted
        with self.profiles.validated(profile_id, now=self._now(), require_clean=True) as (_profile, binding):
            envelopes = [row for row in self.events.event_records()
                if row["kind"] == "message_envelope" and row["id"] == envelope_id]
            if len(envelopes) != 1:
                refuse("adhoc_dispatch_authentication_failed", "dispatch requires one exact coordinator envelope")
            envelope = envelopes[0]
            dispatch, _digest = _blob(binding["repository_path"], envelope["sha"], envelope["doc"])
            self.journal._authenticate(_profile, binding, envelope_id, dispatch, actor)
            self.journal._require_work(_profile, binding, dispatch, actor, self._now())
        from .adhoc_launch import require_adhoc_launch
        require_adhoc_launch(self.root, _profile, dispatch, now=self._now())
        return self.writer.admit(profile_id, envelope_id, dispatch, actor=actor,
            acting_session_id=session, idempotency_key=key, now=self._now())

    def _prompt(self, admission, messages, *, presence=""):
        original = [row for row in self.work.consumption.records()
                    if row["kind"] == "work_item" and row["id"] == admission["dispatch"]["work_item_id"]]
        if len(original) != 1:
            refuse("adhoc_dispatch_authentication_failed", "startup requires its exact immutable work item")
        payload = {"schema_version": 1, "dispatch_id": admission["dispatch_id"],
            "dispatch_digest": admission["dispatch_digest"],
            "work_item_id": admission["dispatch"]["work_item_id"],
            "work_item": {"id": original[0]["id"], "title": original[0]["title"],
                "artifact_bindings": deepcopy(original[0]["artifact_bindings"]), "needs": list(original[0].get("needs", []))},
            "result_document": admission["dispatch"]["result_document"], "messages": messages,
            "result_contract": {"schema_version": 1, "dispatch_digest": admission["dispatch_digest"],
                "provider_coordinate": "the authenticated observed native coordinate",
                "summary": "a nonempty summary of at most 4096 UTF-8 bytes",
                "inbox_dispositions": "one exact message_id, acted or unacted disposition, reason, and result_paths per presented message"}}
        if presence:
            payload["fleet_presence"] = presence
        request = sender_request(self.root, admission["dispatch"])
        if request is not None:
            payload["peer_request"] = {"request_id": request["id"], "sender_node": request["sender"],
                "relationship": "peer agent request; authority remains the coordinator's authenticated dispatch"}
        if self._profile(admission["profile_id"])["adapter"] == "claude":
            payload["result_contract"]["provider_coordinate"] = None
            payload["result_identity_instruction"] = (
                "Write provider_coordinate as null. The parent binds the authenticated Claude terminal coordinate after freezing the exact draft.")
        encoded = canonical_json(payload)
        if len(encoded) > MAX_PROMPT_BYTES:
            refuse("native_protocol_limit", "serialized startup prompt exceeds its fixed one-MiB budget")
        return encoded.decode("utf-8")

    def _startup(self, admission, rows):
        with epoch_guard(self.root, exclusive=True):
            self._guard(admission)
            return self._startup_guarded(admission, rows)

    def _startup_guarded(self, admission, rows):
        saved = self._phase(rows, "inbox_presented")
        if saved is None:
            node = admission["actor"]
            pending = set(self.events.unacked_ids(node))
            candidates = [row for row in self.events.event_records()
                if row["kind"] == "message_envelope" and row["id"] in pending
                and row.get("worker_session_id") is None]
            request = sender_request(self.root, admission["dispatch"])
            if request is not None:
                exact = {request["id"], admission["envelope_id"]}
                candidates = [row for row in candidates if row["id"] in exact]
                if {row["id"] for row in candidates} != exact:
                    refuse("adhoc_sender_binding_changed", "served request and dispatch must remain pending until their frozen presentation")
            if len(candidates) > 1000:
                refuse("native_protocol_limit", "startup node mailbox exceeds the fixed 1000-message bound")
            from .fleet_presence import presence_line
            line = presence_line(self.root, node)
            # Budget before a delivery append; a refused prompt never ACKs.
            self._prompt(admission, candidates, presence=line)
            if request is None:
                messages, delivery = self.events.present(node, limit=1000,
                    worker_session_id=None, now=self._now())
            else:
                # The enclosing exclusive epoch guard freezes this exact batch.
                # Reuse the native delivery producer without presenting any
                # other sender's mail to this conversation.
                from .wake_hold import wake_coordination_guard
                with wake_coordination_guard(self.root, node, worker_session_id=None):
                    self.events.registry.require_protocol_lease(node, now=self._now(), act="served request presentation")
                    messages = candidates
                    delivery = self.events._write_delivery_receipt(node, messages, worker_session_id=None)
            prompt = self._prompt(admission, messages, presence=line)
            saved = self._record(admission["dispatch_id"], "inbox_presented", {
                "message_ids": [row["id"] for row in messages],
                "presentation_ids": [] if delivery is None else [delivery["id"]],
                "session_id": admission["acting_session_id"],
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                **({"fleet_presence": line} if line else {})})
            return prompt, saved
        frames = {row["id"]: row for row in self.events.event_records()
                  if row["kind"] == "message_envelope"}
        try:
            messages = [frames[identifier] for identifier in saved["evidence"]["message_ids"]]
        except KeyError as exc:
            raise ProtocolRefusal("adhoc_binding_changed", "saved startup mail requires its exact retained envelope references") from exc
        prompt = self._prompt(admission, messages, presence=saved["evidence"].get("fleet_presence", ""))
        if hashlib.sha256(prompt.encode()).hexdigest() != saved["evidence"]["prompt_sha256"]:
            refuse("adhoc_binding_changed", "saved startup prompt differs from its exact frozen envelope batch")
        return prompt, saved

    def _coordinate(self, profile_id, *, exclude=None):
        selected = None
        for row in self.journal.records():
            if row["profile_id"] != profile_id or row["dispatch_id"] == exclude:
                continue
            if row["phase"] == "provider_opened":
                selected = row["evidence"]["provider_coordinate"]
            elif row["phase"] == "provider_terminal":
                selected = row["evidence"]["event"]["provider_coordinate"]
        return deepcopy(selected)

    def _context(self, admission, profile, effect):
        dispatch = admission["dispatch"]
        coordinate = (self._coordinate(profile["id"], exclude=admission["dispatch_id"])
                      if dispatch["operation"] == "resume" else None)
        if dispatch["operation"] == "resume" and coordinate is None:
            refuse("native_session_unmaterialized", "resume requires a saved authenticated provider coordinate")
        return {"schema_version": 1, "profile_id": profile["id"], "profile_digest": profile["digest"],
            "dispatch_id": admission["dispatch_id"], "dispatch_digest": admission["dispatch_digest"],
            "node_id": profile["node_id"], "node_lease_id": profile["node_lease_id"],
            "work_item_id": dispatch["work_item_id"], "operation": dispatch["operation"],
            "provider_coordinate": coordinate,
            "workspace_binding": {"profile_id": profile["id"], "lane_opening_id": profile["lane_opening_id"],
                "node_lease_id": profile["node_lease_id"], "workspace": profile["workspace"]},
            "provider_state_binding": {"path": str(self.root.tenant_home / profile["provider_state"]),
                **profile["filesystem_bindings"]["provider_state"]},
            "permission": deepcopy(dispatch["permission"]), "bounds": dict(NATIVE_THREAD_BOUNDS),
            "effect_binding": deepcopy(effect)}

    def _run_native(self, admission, profile, prompt, rows):
        if self._phase(rows, "spawn_reserved") is not None:
            refuse("adhoc_spawn_unknown", "a saved launch reservation never authorizes another provider launch")
        runner, effect = self._parent_operation(lambda: self._prepare_execution(profile, admission))
        if type(runner) is not WorkerRunner or runner.root is not self.root:
            refuse("adhoc_dispatch_authentication_failed", "native execution requires its exact root-bound worker owner")
        self._execution_owner = runner
        context = self._parent_operation(lambda: self._context(admission, profile, effect))
        workspace = BorrowedWorkspace(self.root, context["workspace_binding"])
        pin = self._parent_operation(workspace.pin)
        self._workspace_owner = workspace
        try:
            _prepare_result_parent(workspace._fds["workspace"], admission["dispatch"]["result_document"])
            protocol = self._parent_operation(lambda: NativeThreadProtocol(context, adapter_kind=profile["adapter"],
                journal_controller=self.writer, effect_controller=runner.effect_controller, now=self._now()))
            # This append is an atomic one-time reservation. A competing writer
            # refusal escapes without altering the winner's lifecycle.
            self._record(admission["dispatch_id"], "spawn_reserved", {"native_context": context,
                "workspace_pin": self._workspace_checkpoint(workspace)})
            try:
                receipt = runner.run_exact(profile["node_id"], profile["adapter"],
                    admission["dispatch"]["work_item_id"], native_thread_context=context,
                    on_native_event=protocol.accept, startup_prompt=prompt, now=None)
            except ProtocolRefusal as exc:
                audit = deepcopy(getattr(runner, "last_process_audit", {}))
                code = "adhoc_lease_ended" if exc.code in {"adhoc_lease_ended", "adhoc_lease_invalid", "authority_inactive"} else exc.code
                self._record(admission["dispatch_id"], "failed", {"code": code, "cleanup": audit})
                raise ProtocolRefusal(code, exc.detail) from exc
            audit = deepcopy(runner.last_process_audit)
            terminal = next((event for event in reversed(getattr(runner, "last_native_events", []))
                             if event["event"] == "terminal"), None)
            if terminal is not None:
                self._record(admission["dispatch_id"], "provider_terminal", {"event": terminal})
            if not audit or audit.get("alive_after_cleanup") or audit.get("isolation_backend") is None:
                self._record(admission["dispatch_id"], "unknown", {
                    "code": "adhoc_physical_cleanup_unknown", "cleanup": audit})
                refuse("adhoc_physical_cleanup_unknown", "native process cleanup has not been proved")
            if receipt["transition"] != "bind_artifact" or terminal is None or terminal.get("outcome") != "success":
                code = getattr(runner, "last_native_failure_code", None) or receipt.get("outcome_code") or "adhoc_spawn_unknown"
                self._record(admission["dispatch_id"], "failed", {"code": code, "cleanup": audit})
                refuse(code, "native turn failed without successful work completion")
            self._record(admission["dispatch_id"], "process_closed", {
                "stage": "cleanup", "worker_receipt_id": receipt["id"], "cleanup": audit,
                "artifacts": deepcopy(receipt["artifact_bindings"]),
                "transport": deepcopy(runner.last_native_transport), "bounds": dict(NATIVE_THREAD_BOUNDS)})
            self._parent_operation(lambda: self._guard(admission))
            from .thread_observations import ThreadObserver
            self._parent_operation(lambda: ThreadObserver(self.root).register_adhoc(
                admission["dispatch_id"], profile["node_id"], now=self._now()))
            return self._parent_operation(lambda: self._save_result(
                admission, profile, terminal["provider_coordinate"], workspace, pin))
        finally:
            workspace.close()

    def _save_result(self, admission, profile, coordinate, workspace, pin):
        with epoch_guard(self.root, exclusive=True):
            self._guard(admission)
            return self._save_result_guarded(admission, profile, coordinate, workspace, pin)

    @staticmethod
    def _workspace_checkpoint(workspace):
        return {"expected_head": workspace._pin["head"],
            "index_sha256": hashlib.sha256(workspace._pin["index"]).hexdigest()}

    @staticmethod
    def _verify_intent_files(workspace, files):
        for entry in files:
            mode, data = workspace._result(entry["path"])
            if mode != entry["mode"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                refuse("adhoc_result_disposition_invalid", "live result paths differ from the immutable parent banking intent")

    def _save_result_guarded(self, admission, profile, coordinate, workspace, pin):
        dispatch_id = admission["dispatch_id"]
        self._workspace_owner = workspace
        rows = self._history(dispatch_id)
        saved = self._phase(rows, "process_closed", "result")
        if saved is not None:
            return deepcopy(saved["evidence"]["result"])
        intent = self._phase(rows, "process_closed", "banking_intent")
        if intent is not None and "identity_binding" in intent["evidence"]:
            from .adhoc_result_identity import bind_claude_result_identity
            bind_claude_result_identity(self.root, dispatch_id, workspace)
        pin = workspace.recover_pin(dispatch_id)
        if intent is None:
            startup = self._phase(rows, "inbox_presented")
            relative = admission["dispatch"]["result_document"]
            raw = _read_result(workspace._fds["workspace"], relative)
            identity_binding = None
            if profile["adapter"] == "claude":
                try:
                    draft = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                except (ValueError, UnicodeError, RecursionError, OverflowError):
                    draft = None
                if type(draft) is dict and draft.get("provider_coordinate", object()) is None:
                    from .adhoc_result_identity import prepare_claude_result_identity
                    raw, identity_binding = prepare_claude_result_identity(workspace, relative, raw, coordinate)
            parsed, paths = _parse_result(raw, admission, coordinate, startup["evidence"]["message_ids"])
            paths = sorted(set([relative, *paths]))
            files = []
            for path in paths:
                mode, data = workspace._result(path)
                if identity_binding is not None and path == relative:
                    data = raw
                files.append({"path": path, "mode": mode, "sha256": hashlib.sha256(data).hexdigest()})
            candidate = {"doc": relative, "blob_sha256": hashlib.sha256(raw).hexdigest(),
                "provider_coordinate": parsed["provider_coordinate"], "summary": parsed["summary"],
                "inbox_dispositions": parsed["inbox_dispositions"]}
            evidence = {"stage": "banking_intent",
                "workspace_pin": self._workspace_checkpoint(workspace),
                "result_candidate": candidate, "files": files}
            if identity_binding is not None:
                evidence["identity_binding"] = identity_binding
            intent = self._record(dispatch_id, "process_closed", evidence)
            if identity_binding is not None:
                from .adhoc_result_identity import bind_claude_result_identity
                bind_claude_result_identity(self.root, dispatch_id, workspace)
        candidate = intent["evidence"]["result_candidate"]
        files = intent["evidence"]["files"]
        pin = workspace.recover_pin(dispatch_id)
        self._verify_intent_files(workspace, files)
        self._guard(admission)
        sha = pin["completed_sha"]
        if sha is None:
            def before_commit():
                self._guard(admission)
                self._verify_intent_files(workspace, files)
            artifacts = workspace.finalize([entry["path"] for entry in files], pin["expected_head"],
                before_commit=before_commit, expected_files=files)
            sha = artifacts[0]["sha"]
        saved = {"sha": sha, **deepcopy(candidate)}
        self._record(dispatch_id, "process_closed", {"stage": "result", "result": saved})
        return saved

    def _saved_result(self, admission, rows):
        saved = self._phase(rows, "process_closed", "result")
        cleanup = self._phase(rows, "process_closed", "cleanup")
        terminal = self._phase(rows, "provider_terminal")
        if cleanup is not None and terminal is not None:
            # Re-enter only parent finalization after proved cleanup. No native
            # prompt or executable is involved in this recovery branch.
            with epoch_guard(self.root, exclusive=True):
                profile, _binding = self._guard(admission)
                from .thread_observations import ThreadObserver
                ThreadObserver(self.root).register_adhoc(admission["dispatch_id"],
                    profile["node_id"], now=self._now())
                if saved is not None:
                    return saved["evidence"]["result"]
                context = self._phase(rows, "spawn_reserved")["evidence"]["native_context"]
                workspace = BorrowedWorkspace(self.root, context["workspace_binding"])
                self._workspace_owner = workspace
                try:
                    intent = self._phase(rows, "process_closed", "banking_intent")
                    if intent is not None and "identity_binding" in intent["evidence"]:
                        from .adhoc_result_identity import bind_claude_result_identity
                        bind_claude_result_identity(self.root, admission["dispatch_id"], workspace)
                    pin = workspace.recover_pin(admission["dispatch_id"])
                    return self._save_result(admission, profile,
                        terminal["evidence"]["event"]["provider_coordinate"], workspace, pin)
                finally:
                    workspace.close()
        return None

    def _validate_banked(self, admission, result):
        profile, binding = self._guard(admission)
        repository = binding["repository_path"]
        relative, sha = result["doc"], result["sha"]
        if not _git(repository, "branch", "-r", "--contains", sha).strip():
            refuse("adhoc_result_unbanked", "saved result commit is not contained by a remote-tracking reference")
        reservation = self._phase(self._history(admission["dispatch_id"]), "spawn_reserved")
        workspace = BorrowedWorkspace(self.root, reservation["evidence"]["native_context"]["workspace_binding"])
        try:
            proof = workspace.recover_pin(admission["dispatch_id"])
            if proof["completed_sha"] != sha:
                refuse("adhoc_binding_changed", "publication requires the exact saved parent result commit")
        finally:
            workspace.close()
        size = _git(repository, "cat-file", "-s", sha + ":" + relative, maximum=64)
        try:
            if not 0 < int(size.strip()) <= MAX_RESULT_BYTES:
                raise ValueError()
        except ValueError:
            refuse("adhoc_result_disposition_invalid", "banked result blob exceeds its fixed bound")
        raw = _git(repository, "cat-file", "blob", sha + ":" + relative)
        if hashlib.sha256(raw).hexdigest() != result["blob_sha256"]:
            refuse("adhoc_result_disposition_invalid", "saved result digest differs from its exact banked blob")
        startup = self._phase(self._history(admission["dispatch_id"]), "inbox_presented")
        parsed, paths = _parse_result(raw, admission, result["provider_coordinate"], startup["evidence"]["message_ids"])
        if any(parsed[field] != result[field] for field in ("provider_coordinate", "summary", "inbox_dispositions")):
            refuse("adhoc_result_disposition_invalid", "saved result dispositions differ from the exact banked blob")
        for path in dict.fromkeys([relative, *paths]):
            tree = _git(repository, "ls-tree", sha, "--", path)
            if not tree.startswith((b"100644 blob ", b"100755 blob ")) or tree.count(b"\n") != 1:
                refuse("adhoc_result_disposition_invalid", "result references an absent or nonordinary banked path")
        return profile, [{"repo": profile["repository"], "sha": sha, "doc": path}
            for path in dict.fromkeys([relative, *paths])]

    def _finish(self, admission, result):
        # No provider runs while this short cross-ledger parent transaction
        # excludes lease replacement, epoch roll, or a competing teardown.
        with epoch_guard(self.root, exclusive=True):
            return self._finish_guarded(admission, result)

    def _settle_index(self, admission):
        dispatch_id = admission["dispatch_id"]
        rows = self._history(dispatch_id)
        if self._phase(rows, "process_closed", "index_settled") is not None:
            return
        reservation = self._phase(rows, "spawn_reserved")["evidence"]
        workspace = BorrowedWorkspace(self.root, reservation["native_context"]["workspace_binding"])
        self._workspace_owner = workspace
        try:
            intent = self._phase(rows, "process_closed", "index_settlement_intent")
            if intent is None:
                evidence = workspace.prepare_index_settlement(dispatch_id)
                intent = self._record(dispatch_id, "process_closed", evidence)
            completed = workspace.apply_index_settlement(dispatch_id, intent["evidence"])
            self._record(dispatch_id, "process_closed", completed)
        finally:
            workspace.close()

    def _finish_guarded(self, admission, result):
        dispatch_id = admission["dispatch_id"]
        profile, artifacts = self._validate_banked(admission, result)
        self._settle_index(admission)
        rows = self._history(dispatch_id)
        acted = [entry["message_id"] for entry in result["inbox_dispositions"] if entry["disposition"] == "acted"]
        session = result["provider_coordinate"]["session_id"]
        ack = self._phase(rows, "process_closed", "ack")
        if ack is None:
            self._guard(admission)
            receipts = [] if not acted else [SparseCursor(self.root).ack(profile["node_id"], acted,
                acting_session_id=session, worker_session_id=None, now=self._now())]
            ack = self._record(dispatch_id, "process_closed", {"stage": "ack", "ack_ids": [row["id"] for row in receipts]})
        consume = self._phase(rows, "process_closed", "consume")
        if consume is None:
            self._guard(admission)
            ack_ids = ack["evidence"]["ack_ids"]
            cursor = SparseCursor(self.root)
            from .jsonl import read_records_snapshot
            native_acks = read_records_snapshot(self.root, cursor._relative_path_for(profile["node_id"]),
                allowed_kinds={"ack_receipt"})
            exact_acks = {row["id"]: row for row in native_acks if row["id"] in ack_ids}
            if len(exact_acks) != len(ack_ids):
                refuse("adhoc_binding_changed", "saved native ACK references are unavailable")
            ack_for_message = {identity: row["id"] for row in exact_acks.values()
                               for identity in row["item_ids"] if identity in acted}
            receipts = [] if not acted else MessageConsumption(self.root).consume_many(profile["node_id"],
                acted, session, work_item_id=admission["dispatch"]["work_item_id"],
                ack_receipt_ids=ack_for_message, node_lease_id=profile["node_lease_id"], now=self._now())
            consume = self._record(dispatch_id, "process_closed", {"stage": "consume", "consumption_ids": [row["id"] for row in receipts]})
        completed = self._phase(rows, "work_completed")
        if completed is None:
            self._guard(admission)
            item_id = admission["dispatch"]["work_item_id"]
            item = self.work.show(item_id)[0]
            if item["state"] == "completed":
                transitions = [row for row in self.work.consumption.records() if row["kind"] == "work_transition"
                    and row["work_item_id"] == item_id and row["action"] == "complete"]
                if len(transitions) != 1 or transitions[0]["actor"] != profile["node_id"] or transitions[0]["artifact_bindings"] != artifacts:
                    refuse("adhoc_binding_changed", "completed work differs from the exact saved result disposition")
                transition = transitions[0]
            else:
                transition = self.work.complete(item_id, profile["node_id"], artifacts, now=self._now())
            completed = self._record(dispatch_id, "work_completed", {"transition_id": transition["id"],
                "ack_ids": ack["evidence"]["ack_ids"], "consumption_ids": consume["evidence"]["consumption_ids"],
                "result": deepcopy(result)})
        self._close_served_attempt(admission, rows)
        if self._phase(rows, "result_published") is None:
            self._guard(admission)
            envelope = self.events.send(profile["node_id"], profile["result_recipient"], profile["repository"],
                result["sha"], result["doc"], dispatch_id,
                reply_to=admission["dispatch"].get("request_id", admission["envelope_id"]),
                idempotency_key="adhoc-result-" + dispatch_id, now=self._now())
            self._record(dispatch_id, "result_published", {"envelope_id": envelope["id"]})
        return self.show(dispatch_id)

    def _close_served_attempt(self, admission, rows):
        """Release the exact native attempt only after parent work completion.

        A saved conversation cannot resume if its previous attempt still owns
        the workspace/concurrency key. Native cleanup alone is insufficient:
        join the banked result and work receipt before closing that attempt.
        """
        from .adhoc_execution import _ordinary_once
        from .effects import EffectLedger
        from .ids import uuid7_hex
        from .adhoc_profiles import timestamp
        from .runtruth import RunLedger
        from .scheduler import RunScheduler
        reservation = self._phase(rows, "spawn_reserved")["evidence"]
        effect = reservation["native_context"]["effect_binding"]
        ledger = RunLedger(self.root)
        run = ledger.project().run(effect["run_id"])
        attempt_id = effect["attempt_id"]
        worker = self._phase(rows, "process_closed", "cleanup")["evidence"]["worker_receipt_id"]
        produced = _ordinary_once(ledger, effect["run_id"], self._now(), "result_produced",
            "run-result-produced-", item_id=effect["item_id"], attempt_id=attempt_id,
            dispatch_decision_id=run["dispatches"][attempt_id]["id"], worker_receipt_ids=[worker])
        accepted = run["accepted"].get(effect["item_id"])
        if accepted is None:
            effects = EffectLedger(self.root).project().acceptance_evidence(effect["run_id"], attempt_id)
            record = {"schema_version": 1 if effects.operation_ids else 0,
                "id": "run-result-accepted-" + uuid7_hex(), "kind": "result_accepted",
                "tenant_id": self.root.tenant_id, "timestamp": timestamp(self._now()),
                "run_id": effect["run_id"], "item_id": effect["item_id"], "attempt_id": attempt_id,
                "predecessor_result_id": produced["id"], "acceptance_mode": "accepted_unverified",
                "acceptance_receipt_id": None, "worker_receipt_ids": [worker]}
            if effects.operation_ids:
                record.update(effect_operation_ids=list(effects.operation_ids),
                    effect_ledger_high_watermark=effects.high_watermark,
                    effect_evidence_digest=effects.evidence_digest)
            ledger.append(record)
        elif (accepted["attempt_id"] != attempt_id or accepted["predecessor_result_id"] != produced["id"]
              or accepted["worker_receipt_ids"] != [worker]):
            refuse("adhoc_binding_changed", "saved native acceptance differs from its exact cleanup and result")
        RunScheduler(ledger).terminal_attempt(effect["run_id"], effect["item_id"], attempt_id,
            "completed", None, "completed", "idempotent", now=self._now())

    def run(self, profile_id, envelope_id, *, actor, acting_session_id, idempotency_key):
        validate_identifier(actor, "actor")
        validate_identifier(idempotency_key, "idempotency_key")
        session = validate_session_id(acting_session_id)
        admission = self._parent_operation(lambda: self._admission(profile_id, envelope_id, actor, session, idempotency_key))
        rows = self._parent_operation(lambda: self._history(admission["dispatch_id"]))
        if self._phase(rows, "result_published") is not None:
            return self.show(admission["dispatch_id"])
        failed = self._phase(rows, "failed") or self._phase(rows, "unknown")
        if failed is not None:
            refuse(failed["evidence"]["code"], "saved native dispatch is terminal and cannot launch again")
        if self._phase(rows, "spawn_reserved") is None:
            from .adhoc_launch import require_adhoc_launch
            require_adhoc_launch(self.root, self._profile(profile_id), admission, now=self._now())
        try:
            profile, _binding = self._parent_operation(lambda: self._guard(admission))
            prompt, _startup = self._parent_operation(lambda: self._startup(admission, rows))
            result = self._parent_operation(lambda: self._saved_result(admission, rows))
            if result is None:
                result = self._run_native(admission, profile, prompt, rows)
            return self._parent_operation(lambda: self._finish(admission, result))
        except ProtocolRefusal as exc:
            if exc.code == "adhoc_lease_ended":
                history = self._history(admission["dispatch_id"])
                if self._phase(history, "failed") is None and self._phase(history, "spawn_reserved") is not None:
                    closed = self._phase(history, "process_closed", "cleanup")
                    cleanup = (deepcopy(self._execution_owner.last_process_audit) if closed is None and self._execution_owner is not None
                               else closed["evidence"]["cleanup"] if closed is not None
                               else {"adapter_pid": None, "registered_process_groups": [], "alive_after_cleanup": [], "isolation_backend": None})
                    self._record(admission["dispatch_id"], "failed", {"code": exc.code, "cleanup": cleanup})
            raise

    def show(self, dispatch_id):
        from .native_usage import usage_observation
        admission = self.journal.show(dispatch_id)
        rows = self._history(dispatch_id)
        profile = self._profile(admission["profile_id"])
        terminal = self._phase(rows, "provider_terminal")
        opened = self._phase(rows, "provider_opened")
        failed = self._phase(rows, "failed") or self._phase(rows, "unknown")
        closed = self._phase(rows, "process_closed", "cleanup")
        teardown = self._phase(rows, "teardown_completed")
        reason = None if failed is None else failed["evidence"]["code"]
        if reason is None and self._phase(rows, "spawn_reserved") is not None and closed is None and terminal is None:
            reason = "adhoc_spawn_unknown"
        return {"dispatch_id": dispatch_id, "profile_id": admission["profile_id"],
            "usage": usage_observation(None if terminal is None else terminal["evidence"]["event"]),
            "phase": rows[-1]["phase"] if rows else "admitted", "reason_code": reason,
            "provider_coordinate": (terminal["evidence"]["event"]["provider_coordinate"] if terminal
                else opened["evidence"]["provider_coordinate"] if opened else None),
            "cleanup": (failed["evidence"]["cleanup"] if failed else closed["evidence"]["cleanup"] if closed else None),
            "provider_state_bytes": provider_state_size(self.root, profile),
            "history_state": "inactive" if teardown else "active", "admission": deepcopy(admission),
            "phases": deepcopy(rows)}

    def teardown(self, dispatch_id, *, actor, acting_session_id, idempotency_key):
        with epoch_guard(self.root, exclusive=True):
            return self._teardown_guarded(dispatch_id, actor=actor, acting_session_id=acting_session_id,
                idempotency_key=idempotency_key)

    def _recover_closed_teardown(self, admission, rows):
        """Receipt recovery records old ownership, never a new resource act."""
        profile = self._profile(admission["profile_id"])
        actor = admission["actor"]
        lanes = self.profiles.lanes
        native = lanes._records(actor)
        closing = [row for row in native if row["state"] == "closed"
                   and row["opened_record_id"] == profile["lane_opening_id"]]
        if not closing:
            return None
        # Validate the complete native opening/closing history before selecting
        # one resource incarnation from it.
        lanes._open_records(actor)
        opening = [row for row in native if row["state"] == "open" and row["id"] == profile["lane_opening_id"]]
        if len(closing) != 1 or len(opening) != 1:
            refuse("adhoc_teardown_blocked", "historical teardown requires one exact native opening and closing")
        closing, opening = closing[0], opening[0]
        if (closing["closed_by"] != actor or closing["why"] is not None
            or opening["node_id"] != actor or opening["repo"] != profile["repository"]
            or opening["branch"] != profile["branch"] or opening["base_sha"] != profile["base_sha"]
            or any(closing[field] != opening[field] for field in
                   ("node_id", "repo", "branch", "base_sha", "path", "row", "opened_at"))):
            refuse("adhoc_teardown_blocked", "historical teardown cannot adopt a foreign or forced native close")
        _inspect_closed_successor(lanes, profile, opening)
        leases = [row for row in self.profiles.registry.known_records()
                  if row["kind"] == "node_lease" and row["node_id"] == actor]
        old = [row for row in leases if row["id"] == profile["node_lease_id"]]
        if (len(old) != 1 or old[0]["workspace"] != profile["node_home"]
            or old[0]["expires_at"] != profile["lease_expires_at"]):
            refuse("adhoc_teardown_blocked", "historical teardown lacks the exact recorded thread lease")
        closed = self._phase(rows, "process_closed", "cleanup")
        result = self._phase(rows, "process_closed", "result")
        completed = self._phase(rows, "work_completed")
        published = self._phase(rows, "result_published")
        ack = self._phase(rows, "process_closed", "ack")
        consume = self._phase(rows, "process_closed", "consume")
        if (any(row is None for row in (closed, result, completed, published, ack, consume))
            or closed["evidence"]["cleanup"]["alive_after_cleanup"]
            or closed["evidence"]["cleanup"]["adapter_pid"] is None
            or closed["evidence"]["cleanup"]["isolation_backend"] is None):
            refuse("adhoc_teardown_blocked", "historical teardown lacks saved physical cleanup and parent disposition")
        result = result["evidence"]["result"]
        work_id = admission["dispatch"]["work_item_id"]
        coordinate = result["provider_coordinate"]
        session = coordinate["session_id"]
        def exact(source, identity):
            selected = [row for row in source if row["id"] == identity]
            if len(selected) != 1:
                refuse("adhoc_teardown_blocked", "historical teardown lacks an exact native disposition reference")
            return selected[0]
        worker = exact(WorkerReceipts(self.root).records(), closed["evidence"]["worker_receipt_id"])
        if (worker["node_id"] != actor or worker["work_item_id"] != work_id
            or worker["adapter"] != profile["adapter"] or worker["transition"] != "bind_artifact"):
            refuse("adhoc_teardown_blocked", "historical physical cleanup belongs to another worker")
        from .jsonl import read_records_snapshot
        cursor = SparseCursor(self.root)
        ack_rows = read_records_snapshot(self.root, cursor._relative_path_for(actor), allowed_kinds={"ack_receipt"})
        acted = [item["message_id"] for item in result["inbox_dispositions"] if item["disposition"] == "acted"]
        seen = []
        ack_for_message = {}
        for identity in ack["evidence"]["ack_ids"]:
            receipt = exact(ack_rows, identity)
            if (receipt["recipient"] != actor or receipt["acting_session_id"] != session
                or receipt["node_lease_id"] != profile["node_lease_id"]):
                refuse("adhoc_teardown_blocked", "historical acknowledgment belongs to another provider or lease")
            seen.extend(receipt["item_ids"])
            ack_for_message.update({item: identity for item in receipt["item_ids"]})
        if len(seen) != len(set(seen)) or set(seen) != set(acted):
            refuse("adhoc_teardown_blocked", "historical acknowledgments differ from the saved acted subset")
        seen = []
        for identity in consume["evidence"]["consumption_ids"]:
            receipt = exact(MessageConsumption(self.root).records(actor), identity)
            if (receipt["recipient"] != actor or receipt["acting_session_id"] != session
                or receipt["work_item_id"] != work_id or receipt["worker_session_id"] is not None
                or receipt["ack_receipt_id"] != ack_for_message.get(receipt["message_id"])):
                refuse("adhoc_teardown_blocked", "historical consumption differs from its exact saved acknowledgment")
            seen.append(receipt["message_id"])
        if len(seen) != len(set(seen)) or set(seen) != set(acted):
            refuse("adhoc_teardown_blocked", "historical consumption differs from the saved acted subset")
        # These native joins are read-only and do not require the removed row.
        for row in (completed, published):
            self.journal._authenticate_lifecycle(row, rows, admission, self, self.journal.records())
        size = provider_state_size(self.root, profile)
        if size is None:
            refuse("adhoc_teardown_blocked", "historical provider history size is unknown")
        evidence = {"resource_dispositions": {"workspace": "closed",
            "lane_opening_id": profile["lane_opening_id"], "lane_closing_id": closing["id"],
            "node_lease_id": profile["node_lease_id"], "provider_state": "retained", "process": "closed",
            "work": "completed", "mail": "unknown_at_native_close", "result": "published"},
            "provider_state_bytes": size, "history_state": "inactive",
            "live_login": {"status": "unmeasured", "credential_opt_in": "required_separately"},
            "provenance": {"kind": "native_closing_recovery", "lane_opening_id": opening["id"],
                "lane_closing_id": closing["id"], "observed_node_lease_id": leases[-1]["id"],
                "worker_receipt_id": worker["id"], "work_transition_id": completed["evidence"]["transition_id"],
                "result_envelope_id": published["evidence"]["envelope_id"], "acted_message_ids": acted,
                "ack_receipt_ids": ack["evidence"]["ack_ids"],
                "consumption_receipt_ids": consume["evidence"]["consumption_ids"]}}
        self._record(admission["dispatch_id"], "teardown_completed", evidence)
        return evidence

    def _teardown_guarded(self, dispatch_id, *, actor, acting_session_id, idempotency_key):
        validate_identifier(actor, "actor")
        validate_identifier(idempotency_key, "idempotency_key")
        session = validate_session_id(acting_session_id)
        admission = self.journal.show(dispatch_id)
        if admission["actor"] != actor or admission["acting_session_id"] != session:
            refuse("adhoc_teardown_blocked", "teardown requires the exact admitted actor and parent session")
        rows = self._history(dispatch_id)
        saved = self._phase(rows, "teardown_completed")
        if saved is not None:
            return deepcopy(saved["evidence"])
        try:
            historical = self._recover_closed_teardown(admission, rows)
            if historical is not None:
                return historical
            profile, binding = self._guard(admission)
            closed = self._phase(rows, "process_closed", "cleanup")
            published = self._phase(rows, "result_published")
            result = self._phase(rows, "process_closed", "result")
            if (closed is None or published is None or result is None
                or closed["evidence"]["cleanup"].get("alive_after_cleanup")
                or self.work.show(admission["dispatch"]["work_item_id"])[0]["state"] != "completed"
                or any(item["state"] != "completed" and (item["owner"] == actor or item.get("holder") == actor)
                       for item in self.work.show()) or self.events.unacked_ids(actor)):
                refuse("adhoc_teardown_blocked", "outstanding work, mail, publication, or process evidence retains the row")
            self._validate_banked(admission, result["evidence"]["result"])
            size = provider_state_size(self.root, profile)
            if size is None:
                refuse("adhoc_teardown_blocked", "provider history size is unknown; retain the exact private slot")
            # Existing native close owns every destructive predicate and the
            # durable closing receipt. It never force-cleans an ad-hoc row.
            closing = self.profiles.lanes.close(actor, binding["row"])
            evidence = {"resource_dispositions": {"workspace": "closed",
                "lane_opening_id": profile["lane_opening_id"], "node_lease_id": profile["node_lease_id"],
                "lane_closing_id": closing["record"]["id"], "provider_state": "retained",
                "process": "closed", "work": "completed", "mail": "caught_up", "result": "published"},
                "provider_state_bytes": size, "history_state": "inactive",
                "live_login": {"status": "unmeasured", "credential_opt_in": "required_separately"}}
            self._record(dispatch_id, "teardown_completed", evidence)
            return evidence
        except ProtocolRefusal as exc:
            if exc.code == "adhoc_teardown_blocked":
                raise
            raise ProtocolRefusal("adhoc_teardown_blocked", "exact owned clean, banked teardown predicates are not satisfied",
                remedy="retain the owned row and receipts; resolve the named work, mail, banking, or cleanup predicate before teardown") from exc


_ADHOC_THREAD_RECORD_CODE = AdhocThreadController._record.__code__
_LIFECYCLE_PRODUCER_CODES = frozenset(method.__code__ for method in (
    AdhocThreadController._startup_guarded, AdhocThreadController._run_native,
    AdhocThreadController._save_result_guarded, AdhocThreadController._finish_guarded,
    AdhocThreadController.run, AdhocThreadController._teardown_guarded,
    AdhocThreadController._recover_closed_teardown,
    AdhocThreadController._settle_index,
))
