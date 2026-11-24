"""Governed stdio client for the local Codex app-server process."""

from __future__ import annotations

import json
from copy import deepcopy
import os
import select
import signal
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, IO, Dict, Optional, Sequence

from ..host_paths import deferred_worker_workspace_root
from ..git_process import GIT_MAINTENANCE_OPTIONS
from ..storage_identity import (
    EVIDENCE_DIRECTORY,
    LEGACY_ARTIFACT_PREFIX,
    refuse_legacy_workspace_artifacts,
)
from ..workers import WorkerAdapterFailure
from ..adhoc_records import CODEX_PERMISSION, NATIVE_BINDING_FIELDS
from ..errors import ProtocolRefusal
from ..worker_bootstrap_protocol import validate_native_thread_context
from .native_io import BoundedNativeIO


_APPROVAL_DENIALS = {
    "item/commandExecution/requestApproval": {"decision": "cancel"},
    "item/fileChange/requestApproval": {"decision": "cancel"},
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
}
_WORKSPACE_PARENT = deferred_worker_workspace_root()
_DEFAULT_COMMAND = ("/opt/homebrew/bin/codex", "app-server", "--stdio")
_SAFE_GIT_OPTIONS = (
    *GIT_MAINTENANCE_OPTIONS,
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "commit.gpgSign=false",
    "-c",
    "core.fsmonitor=false",
)


def _secure_directory(path: Path, *, create: bool) -> None:
    try:
        if create:
            path.mkdir(mode=0o700)
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise WorkerAdapterFailure("workspace_invalid")
        os.chmod(path, 0o700, follow_symlinks=False)
    except WorkerAdapterFailure:
        raise
    except OSError as exc:
        raise WorkerAdapterFailure("workspace_invalid") from exc


def _open_private_file(path: Path) -> IO[str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "w", encoding="utf-8")
    except OSError as exc:
        raise WorkerAdapterFailure("workspace_invalid") from exc


class AppServerSession:
    """One bounded newline-delimited JSON-RPC app-server session."""

    def __init__(
        self,
        command: Sequence[str],
        workspace: Path,
        *,
        isolate_process_group: bool = True,
        process_group_registrar: Optional[Callable[[int], None]] = None,
    ) -> None:
        if (
            not isinstance(command, (tuple, list))
            or not command
            or any(not isinstance(part, str) or not part for part in command)
            or not Path(command[0]).is_absolute()
        ):
            raise WorkerAdapterFailure("process_start_failed")
        self.command = tuple(command)
        self.workspace = Path(workspace)
        self.isolate_process_group = isolate_process_group
        self.process_group_registrar = process_group_registrar
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.transcript: Optional[IO[str]] = None
        self.stderr: Optional[IO[str]] = None
        self.thread_id: Optional[str] = None
        self.turn_id: Optional[str] = None
        self.next_id = 1
        self._stdout_buffer = bytearray()
        self.deadline: Optional[float] = None

    def run(self, prompt: str, *, deadline_seconds: float) -> None:
        self.start(deadline_seconds=deadline_seconds)
        try:
            self.run_turn(prompt)
        finally:
            self.close()

    def start(self, *, deadline_seconds: float) -> None:
        if (
            not isinstance(deadline_seconds, (int, float))
            or isinstance(deadline_seconds, bool)
            or deadline_seconds <= 0
        ):
            raise WorkerAdapterFailure("protocol_error")
        refuse_legacy_workspace_artifacts(self.workspace)
        self.deadline = time.monotonic() + float(deadline_seconds)
        evidence_dir = self.workspace / EVIDENCE_DIRECTORY
        _secure_directory(evidence_dir, create=True)
        self.transcript = _open_private_file(evidence_dir / "transcript.jsonl")
        self.stderr = _open_private_file(evidence_dir / "app-server.stderr")
        try:
            try:
                self.process = subprocess.Popen(
                    self.command,
                    cwd=self.workspace,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=self.stderr,
                    text=False,
                    bufsize=0,
                    start_new_session=self.isolate_process_group,
                )
                if (
                    self.isolate_process_group
                    and self.process_group_registrar is not None
                ):
                    self.process_group_registrar(self.process.pid)
            except (OSError, ValueError) as exc:
                raise WorkerAdapterFailure("process_start_failed") from exc

            initialize_id = self._request(
                "initialize",
                {"clientInfo": {"name": "floati-worker", "version": "0"}},
            )
            self._wait_for_response(initialize_id, self.deadline)
            self._notify("initialized")
            thread_id = self._request(
                "thread/start",
                {
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                    "cwd": str(self.workspace),
                    "ephemeral": True,
                    "sandbox": "workspace-write",
                },
            )
            thread_result = self._wait_for_response(thread_id, self.deadline)
            try:
                self.thread_id = str(thread_result["thread"]["id"])
            except (KeyError, TypeError) as exc:
                raise WorkerAdapterFailure("protocol_error") from exc
        except WorkerAdapterFailure as failure:
            if failure.code == "process_timeout":
                self._interrupt()
            self.close()
            raise

    def run_turn(self, prompt: str) -> None:
        if not isinstance(prompt, str) or not prompt or self.deadline is None:
            raise WorkerAdapterFailure("protocol_error")
        try:
            turn_request_id = self._request(
                "turn/start",
                {
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                    "cwd": str(self.workspace),
                    "input": [{"type": "text", "text": prompt}],
                    # Codex's own sandbox, set explicitly on every turn: the
                    # workspace is the only writable root and <temp> is excluded.
                    # TMPDIR stays writable; the native launch routes it into
                    # the provider slot (app-server v2 SandboxPolicy fields).
                    "sandboxPolicy": {
                        "type": "workspaceWrite",
                        "writableRoots": [str(self.workspace)],
                        "networkAccess": False,
                        "excludeSlashTmp": True,
                        "excludeTmpdirEnvVar": False,
                    },
                    "threadId": self.thread_id,
                },
            )
            turn_result = self._wait_for_response(turn_request_id, self.deadline)
            try:
                self.turn_id = str(turn_result["turn"]["id"])
            except (KeyError, TypeError) as exc:
                raise WorkerAdapterFailure("protocol_error") from exc
            self._wait_for_turn_completion(self.deadline)
        except WorkerAdapterFailure as failure:
            if failure.code == "process_timeout":
                self._interrupt()
            raise

    def close(self) -> None:
        self._shutdown()

    def _request(self, method: str, params: dict) -> int:
        request_id = self.next_id
        self.next_id += 1
        self._write({"id": request_id, "method": method, "params": params})
        return request_id

    def _notify(self, method: str) -> None:
        self._write({"method": method})

    def _write(self, message: dict) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise WorkerAdapterFailure("process_died")
        self._record("out", message)
        try:
            process.stdin.write(
                (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
            )
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise WorkerAdapterFailure("process_died") from exc

    def _read(self, deadline: float) -> dict:
        process = self.process
        if process is None or process.stdout is None:
            raise WorkerAdapterFailure("process_died")
        while b"\n" not in self._stdout_buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerAdapterFailure("process_timeout")
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                raise WorkerAdapterFailure("process_timeout")
            try:
                chunk = os.read(process.stdout.fileno(), 65536)
            except OSError as exc:
                raise WorkerAdapterFailure("process_died") from exc
            if not chunk:
                raise WorkerAdapterFailure("process_died")
            self._stdout_buffer.extend(chunk)
        line, _, remainder = self._stdout_buffer.partition(b"\n")
        self._stdout_buffer = bytearray(remainder)
        try:
            message = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise WorkerAdapterFailure("protocol_error") from exc
        if not isinstance(message, dict):
            raise WorkerAdapterFailure("protocol_error")
        self._record("in", message)
        return message

    def _wait_for_response(self, request_id: int, deadline: float) -> dict:
        while True:
            message = self._read(deadline)
            if "method" in message and "id" in message:
                self._refuse_server_request(message)
            if message.get("id") != request_id:
                continue
            if "error" in message or not isinstance(message.get("result"), dict):
                raise WorkerAdapterFailure("protocol_error")
            return message["result"]

    def _wait_for_turn_completion(self, deadline: float) -> None:
        while True:
            message = self._read(deadline)
            if "method" in message and "id" in message:
                self._refuse_server_request(message)
            if message.get("method") != "turn/completed":
                continue
            params = message.get("params")
            if not isinstance(params, dict) or params.get("threadId") != self.thread_id:
                raise WorkerAdapterFailure("protocol_error")
            turn = params.get("turn")
            if not isinstance(turn, dict) or turn.get("id") != self.turn_id:
                raise WorkerAdapterFailure("protocol_error")
            if turn.get("status") != "completed":
                raise WorkerAdapterFailure("turn_failed")
            return

    def _refuse_server_request(self, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if not isinstance(method, str) or request_id is None:
            raise WorkerAdapterFailure("protocol_error")
        if method in _APPROVAL_DENIALS:
            response = {"id": request_id, "result": _APPROVAL_DENIALS[method]}
        elif method == "item/tool/requestUserInput":
            response = {"id": request_id, "result": {"answers": {}}}
        elif method == "mcpServer/elicitation/request":
            response = {"id": request_id, "result": {"action": "cancel"}}
        elif method == "item/tool/call":
            response = {
                "id": request_id,
                "result": {"contentItems": [], "success": False},
            }
        else:
            response = {
                "id": request_id,
                "error": {"code": -32000, "message": "unattended worker refused request"},
            }
        self._write(response)
        self._interrupt()
        raise WorkerAdapterFailure("approval_required_unattended")

    def _interrupt(self) -> None:
        if self.thread_id is None or self.turn_id is None:
            return
        try:
            self._request(
                "turn/interrupt",
                {"threadId": self.thread_id, "turnId": self.turn_id},
            )
        except WorkerAdapterFailure:
            pass

    def _record(self, direction: str, message: dict) -> None:
        if self.transcript is None:
            return
        self.transcript.write(
            json.dumps(
                {"direction": direction, "message": message},
                separators=(",", ":"),
            )
            + "\n"
        )
        self.transcript.flush()

    def _shutdown(self) -> None:
        process = self.process
        if process is not None:
            process_group = process.pid if self.isolate_process_group else None
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    self._signal_process(process, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        self._signal_process(process, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    process.wait(timeout=1)
            if process_group is not None:
                self._quiesce_process_group(process_group)
            if process.stdout is not None:
                process.stdout.close()
        if self.stderr is not None:
            self.stderr.close()
        if self.transcript is not None:
            self.transcript.close()
        self.process = None
        self.stderr = None
        self.transcript = None

    def _signal_process(
        self, process: subprocess.Popen[bytes], signum: signal.Signals
    ) -> None:
        if self.isolate_process_group:
            os.killpg(process.pid, signum)
        else:
            process.send_signal(signum)

    @staticmethod
    def _quiesce_process_group(process_group: int) -> None:
        try:
            os.killpg(process_group, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                break
            time.sleep(0.01)
        try:
            os.killpg(process_group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class _NativeAppServerSession(AppServerSession):
    """Persistent app-server transport; row banking stays with the parent."""

    def __init__(self, *args, context, emit, **kwargs):
        super().__init__(*args, **kwargs)
        self.context = context
        self.emit = emit
        self.native_io = None
        self.coordinate = None
        self.workspace_identity = None
        self.transport_snapshot = None
        self.usage = None
        self.usage_turn_id = None
        self.usage_turn_active = False

    def _require_bindings(self):
        slot = self.context["provider_state_binding"]
        try:
            for path, expected in ((Path(slot["path"]), (slot["dev"], slot["ino"])),
                                   (self.workspace, self.workspace_identity)):
                metadata = path.lstat()
                if (stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or (metadata.st_dev, metadata.st_ino) != expected):
                    raise WorkerAdapterFailure("adhoc_binding_changed")
        except OSError as exc:
            raise WorkerAdapterFailure("adhoc_binding_changed") from exc

    def _event(self, phase):
        event = {"schema_version": 1, "event": phase,
                 "binding": {key: deepcopy(self.context[key]) for key in NATIVE_BINDING_FIELDS},
                 "provider_coordinate": deepcopy(self.coordinate)}
        if phase == "terminal":
            event["outcome"] = "success"
            if self.usage is not None:
                event["usage"] = deepcopy(self.usage)
        return event

    def start(self, *, deadline_seconds):
        if (type(deadline_seconds) not in (int, float) or deadline_seconds <= 0):
            raise WorkerAdapterFailure("protocol_error")
        self.deadline = time.monotonic() + deadline_seconds
        try:
            metadata = self.workspace.lstat()
            self.workspace_identity = (metadata.st_dev, metadata.st_ino)
            self._require_bindings()
            environment = dict(os.environ)
            slot = self.context["provider_state_binding"]["path"]
            environment.update(HOME=slot, CODEX_HOME=slot, TMPDIR=slot, TMP=slot, TEMP=slot)
            self.process = subprocess.Popen(self.command, cwd=self.workspace,
                env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=False, bufsize=0,
                start_new_session=self.isolate_process_group)
            if self.isolate_process_group and self.process_group_registrar is not None:
                self.process_group_registrar(self.process.pid)
            self.native_io = BoundedNativeIO(self.process, deadline=self.deadline,
                                            bounds=self.context["bounds"])
            request = self._request("initialize", {
                "clientInfo": {"name": "floati-worker", "version": "0"}})
            self._wait_for_response(request, self.deadline)
            self._notify("initialized")
            operation = self.context["operation"]
            params = {"approvalPolicy": "on-request", "approvalsReviewer": "user",
                      "cwd": str(self.workspace), "sandbox": "workspace-write"}
            if operation == "start":
                params["ephemeral"] = False
            else:
                params["threadId"] = self.context["provider_coordinate"]["session_id"]
            request = self._request("thread/" + operation, params)
            result = self._wait_for_response(request, self.deadline)
            thread = result.get("thread")
            if type(thread) is not dict or type(thread.get("id")) is not str:
                raise WorkerAdapterFailure("protocol_error")
            if thread.get("cwd") != str(self.workspace):
                raise WorkerAdapterFailure("native_session_mismatch")
            if thread.get("ephemeral") is not False:
                raise WorkerAdapterFailure("native_session_unmaterialized")
            self.thread_id = thread["id"]
            self.coordinate = {"provider": "codex_local", "session_id": self.thread_id,
                               "session_file": None}
            # Reuse the closed native-event validator, including UUIDv7 and
            # exact resume identity, before giving the parent any testimony.
            from ..native_thread_protocol import validate_native_event
            event = validate_native_event(self.context, self._event("opened"),
                adapter_kind="codex", opened_coordinate=None, terminal_seen=False)
            self._require_bindings()
            ack = self.emit(event)
            if (type(ack) is not dict or set(ack) != set(event) | {"opened_record_id"}
                or ack.get("event") != "opened_ack"
                or type(ack.get("schema_version")) is not int
                or ack.get("schema_version") != 1
                or ack.get("binding") != event["binding"]
                or ack.get("provider_coordinate") != self.coordinate):
                raise WorkerAdapterFailure("protocol_error")
            from ..adhoc_records import UUID
            import re
            if (type(ack["opened_record_id"]) is not str
                or re.fullmatch("adhoc-native-opened-" + UUID, ack["opened_record_id"]) is None):
                raise WorkerAdapterFailure("protocol_error")
            self._require_bindings()
        except ProtocolRefusal as exc:
            self.close()
            raise WorkerAdapterFailure(exc.code) from exc
        except (OSError, ValueError) as exc:
            self.close()
            raise WorkerAdapterFailure("process_start_failed") from exc
        except BaseException:
            self.close()
            raise

    def _read(self, deadline):
        if self.native_io is None:
            raise WorkerAdapterFailure("process_died")
        message = self.native_io.read_record()
        if message is None:
            raise WorkerAdapterFailure("process_died")
        if (("id" in message and type(message["id"]) not in (str, int))
            or ("method" in message and type(message["method"]) is not str)):
            raise WorkerAdapterFailure("protocol_error")
        self._observe_usage(message)
        return message

    def _observe_usage(self, message):
        if message.get("method") != "thread/tokenUsage/updated" or not self.usage_turn_active:
            return
        from ..native_usage import provider_usage
        params = message.get("params")
        if (type(params) is not dict or params.get("threadId") != self.thread_id
                or type(params.get("turnId")) is not str or not params["turnId"]):
            raise WorkerAdapterFailure("native_session_mismatch")
        if self.usage_turn_id is not None and params["turnId"] != self.usage_turn_id:
            raise WorkerAdapterFailure("native_session_mismatch")
        token_usage = params.get("tokenUsage")
        if type(token_usage) is not dict or "total" not in token_usage:
            raise WorkerAdapterFailure("protocol_error")
        self.usage = provider_usage("codex", token_usage["total"])
        self.usage_turn_id = params["turnId"]

    def _wait_for_response(self, request_id, deadline):
        result = super()._wait_for_response(request_id, deadline)
        if "turn" in result:
            turn = result["turn"]
            if type(turn) is not dict or type(turn.get("id")) is not str or not turn["id"]:
                raise WorkerAdapterFailure("protocol_error")
        return result

    def _refuse_server_request(self, message):
        if self.process is not None and self.process.stdin is not None and self.process.stdin.closed:
            # Tail requests cannot be answered after EOF, and still forbid
            # success even when the provider already announced completion.
            raise WorkerAdapterFailure("approval_required_unattended")
        super()._refuse_server_request(message)

    def run_turn(self, prompt):
        self._require_bindings()
        self.usage_turn_active = True
        prompt += "\nAuthenticated provider_coordinate: " + json.dumps(self.coordinate, sort_keys=True)
        super().run_turn(prompt)
        if self.usage_turn_id is not None and self.usage_turn_id != self.turn_id:
            raise WorkerAdapterFailure("native_session_mismatch")

    def finish(self):
        """A completed turn is provisional until every tail byte is bounded."""
        if self.process is None or self.native_io is None:
            raise WorkerAdapterFailure("process_died")
        if self.process.stdin is not None:
            self.process.stdin.close()

        def tail(message):
            self._observe_usage(message)
            if "method" in message and "id" in message:
                self._refuse_server_request(message)
            if message.get("method") == "turn/completed":
                raise WorkerAdapterFailure("protocol_error")

        evidence = self.native_io.drain_to_exit(on_record=tail)
        if evidence["exit_code"] != 0:
            raise WorkerAdapterFailure("process_died")
        if self.usage_turn_id is not None and self.usage_turn_id != self.turn_id:
            raise WorkerAdapterFailure("native_session_mismatch")
        self._require_bindings()
        self.close()
        self.transport_snapshot = deepcopy(evidence)
        self.emit(self._event("terminal"))

    def _shutdown(self):
        stderr = self.process.stderr if self.process is not None else None
        try:
            super()._shutdown()
        finally:
            if stderr is not None:
                stderr.close()


@dataclass
class _CodexHandle:
    work_id: str
    workspace: Path
    filesystem_workspace: Path
    session: AppServerSession
    deadline: float
    git_identity: tuple[int, int, int]


class CodexAppServerAdapter:
    """Live Codex worker boundary with explicit local workspace and Git evidence."""

    name = "codex"
    cancel_mode = "native"
    requires_workspace = True

    def __init__(
        self,
        command: Sequence[str] = _DEFAULT_COMMAND,
        *,
        isolate_process_group: bool = True,
    ) -> None:
        if type(isolate_process_group) is not bool:
            raise WorkerAdapterFailure("process_start_failed")
        self.command = tuple(command)
        self.isolate_process_group = isolate_process_group
        self._active_session: Optional[AppServerSession] = None
        self._process_group_registrar: Optional[Callable[[int], None]] = None
        self._spawn_context: Optional[Dict[str, object]] = None
        self._descendant_emitter: Optional[Callable[[object], None]] = None
        self._effect_context: Optional[Dict[str, object]] = None
        self._effect_emitter: Optional[Callable[[object], None]] = None
        self._prepared_workspace_identity: Optional[tuple[str, int, int]] = None
        self._prepared_workspace_descriptor: Optional[int] = None
        self._prior_directory_descriptor: Optional[int] = None
        self._prepared_workspace_lifecycle_consumed = False
        self._native_context = None
        self._native_emitter = None
        self.last_native_transport = None

    def set_native_thread_context(self, context, emit):
        if self._native_context is not None or not callable(emit):
            raise WorkerAdapterFailure("protocol_error")
        try:
            context = validate_native_thread_context(context)
        except ProtocolRefusal as exc:
            raise WorkerAdapterFailure(exc.code) from exc
        if context["permission"] != CODEX_PERMISSION:
            raise WorkerAdapterFailure("native_permission_unsupported")
        if (context["workspace_binding"]["profile_id"] != context["profile_id"]
            or context["workspace_binding"]["node_lease_id"] != context["node_lease_id"]):
            raise WorkerAdapterFailure("adhoc_binding_changed")
        coordinate = context["provider_coordinate"]
        if coordinate is not None and coordinate["provider"] != "codex_local":
            raise WorkerAdapterFailure("native_session_mismatch")
        self._native_context, self._native_emitter = context, emit

    def _native_workspace(self, item):
        context = self._native_context
        if (context is None or item.get("id") != context["work_item_id"]
            or item.get("adhoc_binding") != context["workspace_binding"]
            or item.get("workspace") != context["workspace_binding"]["workspace"]):
            raise WorkerAdapterFailure("adhoc_binding_changed")
        return Path(item["workspace"])

    def set_spawn_context(
        self,
        context: Dict[str, object],
        emit: Callable[[object], None],
    ) -> None:
        if (
            type(context) is not dict
            or not callable(emit)
            or self._spawn_context is not None
            or self._descendant_emitter is not None
        ):
            raise WorkerAdapterFailure("adapter_error")
        self._spawn_context = dict(context)
        self._descendant_emitter = emit

    def set_effect_context(
        self,
        context: Dict[str, object],
        emit: Callable[[object], None],
    ) -> None:
        if (
            type(context) is not dict
            or not callable(emit)
            or self._effect_context is not None
            or self._effect_emitter is not None
        ):
            raise WorkerAdapterFailure("adapter_error")
        self._effect_context = dict(context)
        self._effect_emitter = emit

    def set_prepared_workspace(
        self,
        path: str,
        device: int,
        inode: int,
    ) -> None:
        if (
            self._prepared_workspace_identity is not None
            or not isinstance(path, str)
            or not path
            or not isinstance(device, int)
            or isinstance(device, bool)
            or device < 0
            or not isinstance(inode, int)
            or isinstance(inode, bool)
            or inode <= 0
        ):
            raise WorkerAdapterFailure("workspace_invalid")
        self._prepared_workspace_identity = (path, device, inode)

    def _accept_prepared_workspace(
        self,
        workspace: Path,
        binding: Optional[tuple[str, int, int]],
    ) -> Optional[int]:
        if binding is None:
            return None
        path, device, inode = binding
        descriptor = -1
        try:
            prepared = Path(path)
            if (
                not prepared.is_absolute()
                or prepared != workspace
            ):
                raise WorkerAdapterFailure("workspace_invalid")
            nofollow = getattr(os, "O_NOFOLLOW", None)
            directory = getattr(os, "O_DIRECTORY", None)
            if nofollow is None or directory is None:
                raise WorkerAdapterFailure("workspace_invalid")
            descriptor = os.open(
                prepared, os.O_RDONLY | directory | nofollow,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or (metadata.st_dev, metadata.st_ino) != (device, inode)
            ):
                raise WorkerAdapterFailure("workspace_invalid")
            entries = os.listdir(descriptor)
            # The same on-disk prefix floati/storage_identity.py refuses, read
            # here with the OPPOSITE polarity: a non-empty prepared workspace
            # must carry the marker. Imported rather than repeated so the two
            # readers of one disk name cannot drift apart, and hex-built there
            # rather than spelled here. Pinned in
            # tests/test_retired_name_pins.py, at this site and at that one.
            if entries and not any(
                name.startswith(LEGACY_ARTIFACT_PREFIX) for name in entries
            ):
                raise WorkerAdapterFailure("workspace_invalid")
            path_metadata = prepared.lstat()
            if (
                stat.S_ISLNK(path_metadata.st_mode)
                or not stat.S_ISDIR(path_metadata.st_mode)
                or (path_metadata.st_dev, path_metadata.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                raise WorkerAdapterFailure("workspace_invalid")
        except WorkerAdapterFailure:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise WorkerAdapterFailure("workspace_invalid") from exc
        return descriptor

    @staticmethod
    def _require_workspace_descriptor_identity(
        workspace: Path,
        descriptor: int,
    ) -> None:
        try:
            descriptor_metadata = os.fstat(descriptor)
            path_metadata = workspace.lstat()
        except OSError as exc:
            raise WorkerAdapterFailure("workspace_invalid") from exc
        if (
            stat.S_ISLNK(path_metadata.st_mode)
            or not stat.S_ISDIR(path_metadata.st_mode)
            or (path_metadata.st_dev, path_metadata.st_ino)
            != (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
        ):
            raise WorkerAdapterFailure("workspace_invalid")

    def _pin_prepared_workspace(
        self,
        workspace: Path,
        workspace_descriptor: Optional[int],
    ) -> Path:
        if workspace_descriptor is None:
            return workspace
        if (
            self._prepared_workspace_lifecycle_consumed
            or self._prepared_workspace_descriptor is not None
            or self._prior_directory_descriptor is not None
        ):
            os.close(workspace_descriptor)
            raise WorkerAdapterFailure("workspace_invalid")
        directory = getattr(os, "O_DIRECTORY", None)
        if directory is None:
            os.close(workspace_descriptor)
            raise WorkerAdapterFailure("workspace_invalid")
        prior_directory = -1
        try:
            prior_directory = os.open(".", os.O_RDONLY | directory)
            os.fchdir(workspace_descriptor)
            self._prepared_workspace_lifecycle_consumed = True
            self._prepared_workspace_descriptor = workspace_descriptor
            self._prior_directory_descriptor = prior_directory
            return Path(".")
        except OSError as exc:
            if prior_directory >= 0:
                os.close(prior_directory)
            os.close(workspace_descriptor)
            raise WorkerAdapterFailure("workspace_invalid") from exc

    def _restore_prepared_workspace(self) -> None:
        workspace_descriptor = self._prepared_workspace_descriptor
        prior_directory = self._prior_directory_descriptor
        self._prepared_workspace_descriptor = None
        self._prior_directory_descriptor = None
        if workspace_descriptor is None and prior_directory is None:
            return
        try:
            if prior_directory is None:
                raise WorkerAdapterFailure("workspace_invalid")
            os.fchdir(prior_directory)
        except OSError as exc:
            raise WorkerAdapterFailure("workspace_invalid") from exc
        finally:
            if prior_directory is not None:
                os.close(prior_directory)
            if workspace_descriptor is not None:
                os.close(workspace_descriptor)

    def set_process_group_registrar(
        self, registrar: Callable[[int], None]
    ) -> None:
        self._process_group_registrar = registrar

    def spawn(self, item: Dict[str, object], *, deadline_seconds: float) -> object:
        if self._native_context is not None:
            workspace = self._native_workspace(item)
            session = _NativeAppServerSession(self.command, workspace,
                context=self._native_context, emit=self._native_emitter,
                isolate_process_group=self.isolate_process_group,
                process_group_registrar=self._process_group_registrar)
            self._active_session = session
            session.start(deadline_seconds=deadline_seconds)
            return _CodexHandle(str(item["id"]), workspace, workspace,
                                session, session.deadline, (0, 0, 0))
        binding = self._prepared_workspace_identity
        self._prepared_workspace_identity = None
        workspace = self._workspace(item)
        workspace_descriptor = self._accept_prepared_workspace(workspace, binding)
        if workspace_descriptor is None:
            if workspace.exists():
                raise WorkerAdapterFailure("workspace_invalid")
            try:
                if not _WORKSPACE_PARENT.exists() and not _WORKSPACE_PARENT.is_symlink():
                    _WORKSPACE_PARENT.mkdir(parents=True, mode=0o700, exist_ok=True)
                _secure_directory(_WORKSPACE_PARENT, create=False)
                _secure_directory(workspace, create=True)
            except OSError as exc:
                raise WorkerAdapterFailure("workspace_invalid") from exc
        filesystem_workspace = workspace
        try:
            filesystem_workspace = self._pin_prepared_workspace(
                workspace, workspace_descriptor,
            )
            refuse_legacy_workspace_artifacts(filesystem_workspace)
            deadline = time.monotonic() + float(deadline_seconds)
            self._initialize_repository(filesystem_workspace, deadline)
            git_identity = self._git_identity(filesystem_workspace)
            # The app-server remains in the worker adapter's process group so the
            # parent runner can reap it even if this adapter process dies abruptly.
            session = AppServerSession(
                self.command,
                filesystem_workspace,
                isolate_process_group=self.isolate_process_group,
                process_group_registrar=(
                    self._process_group_registrar
                    if self.isolate_process_group
                    else None
                ),
            )
            self._active_session = session
            session.start(deadline_seconds=self._remaining(deadline))
        except WorkerAdapterFailure:
            self._active_session = None
            self._restore_prepared_workspace()
            raise
        except OSError as exc:
            self._restore_prepared_workspace()
            raise WorkerAdapterFailure("git_finalize_failed") from exc
        except Exception:
            self._restore_prepared_workspace()
            raise
        return _CodexHandle(
            str(item["id"]), workspace, filesystem_workspace,
            session, deadline, git_identity,
        )

    def drive(
        self,
        handle: object,
        item: Dict[str, object],
        *,
        deadline_seconds: float,
    ) -> list[Dict[str, str]]:
        if self._native_context is not None:
            try:
                if (not isinstance(handle, _CodexHandle)
                    or handle.session is not self._active_session
                    or handle.work_id != item.get("id")
                    or handle.workspace != self._native_workspace(item)):
                    raise WorkerAdapterFailure("protocol_error")
                handle.session.run_turn(item.get("native_prompt", str(item.get("title", ""))))
                handle.session.finish()
                self.last_native_transport = deepcopy(handle.session.transport_snapshot)
                # Only BorrowedWorkspace in the authenticated parent may bank
                # declared result paths or advance the existing row branch.
                return []
            finally:
                if isinstance(handle, _CodexHandle):
                    handle.session.close()
                self._active_session = None
        try:
            if (
                not isinstance(handle, _CodexHandle)
                or handle.work_id != item.get("id")
                or handle.workspace != self._workspace(item)
            ):
                raise WorkerAdapterFailure("protocol_error")
            handle.session.run_turn(str(item.get("title", "")))
            # A completed turn can leave provider descendants running. Close
            # the owned session before inspecting or committing their workspace.
            handle.session.close()
            return self._finalize(handle)
        finally:
            try:
                if isinstance(handle, _CodexHandle):
                    handle.session.close()
            finally:
                self._active_session = None
                self._restore_prepared_workspace()

    def cancel(self) -> None:
        try:
            if self._active_session is not None:
                self._active_session.close()
                self._active_session = None
        finally:
            self._restore_prepared_workspace()

    @staticmethod
    def _workspace(item: Dict[str, object]) -> Path:
        work_id = item.get("id")
        workspace = item.get("workspace")
        if not isinstance(work_id, str) or not isinstance(workspace, str):
            raise WorkerAdapterFailure("workspace_mapping_missing")
        expected = _WORKSPACE_PARENT / work_id
        path = Path(workspace)
        if not path.is_absolute() or path != expected:
            raise WorkerAdapterFailure("workspace_invalid")
        return path

    def _finalize(self, handle: _CodexHandle) -> list[Dict[str, str]]:
        files: list[Path] = []
        try:
            self._reset_repository(handle)
            for root, directories, names in os.walk(handle.filesystem_workspace, followlinks=False):
                root_path = Path(root)
                kept: list[str] = []
                for name in directories:
                    candidate = root_path / name
                    if root_path == handle.filesystem_workspace and name in {".git", EVIDENCE_DIRECTORY}:
                        continue
                    if candidate.is_symlink():
                        raise WorkerAdapterFailure("artifact_ambiguous")
                    kept.append(name)
                directories[:] = kept
                for name in names:
                    candidate = root_path / name
                    if candidate.is_symlink() or not candidate.is_file():
                        raise WorkerAdapterFailure("artifact_ambiguous")
                    relative = candidate.relative_to(handle.filesystem_workspace)
                    if relative.parts[0] in {".git", EVIDENCE_DIRECTORY}:
                        continue
                    files.append(relative)
        except OSError as exc:
            raise WorkerAdapterFailure("artifact_ambiguous") from exc
        files.sort(key=lambda path: path.as_posix())
        if not files:
            raise WorkerAdapterFailure("artifact_missing")
        if len(files) > 32:
            raise WorkerAdapterFailure("artifact_ambiguous")
        try:
            self._git(
                handle.filesystem_workspace,
                handle.deadline,
                "--literal-pathspecs",
                "add",
                "--",
                *(path.as_posix() for path in files),
            )
            if self._git(handle.filesystem_workspace, handle.deadline, "ls-files", EVIDENCE_DIRECTORY):
                raise WorkerAdapterFailure("artifact_ambiguous")
            self._git(
                handle.filesystem_workspace,
                handle.deadline,
                "-c",
                "user.name=Floati Worker",
                "-c",
                "user.email=worker@floati.local",
                "commit",
                "--quiet",
                "-m",
                f"Complete {handle.work_id}",
            )
            sha = self._git(handle.filesystem_workspace, handle.deadline, "rev-parse", "HEAD")
            tree = self._git(
                handle.filesystem_workspace,
                handle.deadline,
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                "HEAD",
            )
            tracked = [entry for entry in tree.split("\0") if entry]
            expected = [path.as_posix() for path in files]
            if tracked != expected:
                raise WorkerAdapterFailure("artifact_ambiguous")
            if self._git(
                handle.filesystem_workspace,
                handle.deadline,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ):
                raise WorkerAdapterFailure("artifact_ambiguous")
        except WorkerAdapterFailure:
            raise
        return [
            {
                "repo": f"local/{handle.work_id}",
                "sha": sha,
                "doc": path.as_posix(),
            }
            for path in files
        ]

    def _initialize_repository(self, workspace: Path, deadline: float) -> None:
        self._git(
            workspace,
            deadline,
            "-c",
            "init.defaultBranch=main",
            "init",
            "--quiet",
            "--template=",
        )
        info = workspace / ".git" / "info"
        _secure_directory(info, create=True)
        with _open_private_file(info / "exclude") as exclude:
            exclude.write(f"{EVIDENCE_DIRECTORY}/\n")

    @staticmethod
    def _git_identity(workspace: Path) -> tuple[int, int, int]:
        try:
            metadata = (workspace / ".git").lstat()
        except OSError as exc:
            raise WorkerAdapterFailure("git_finalize_failed") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise WorkerAdapterFailure("git_finalize_failed")
        return metadata.st_dev, metadata.st_ino, metadata.st_uid

    def _reset_repository(self, handle: _CodexHandle) -> None:
        if self._git_identity(handle.filesystem_workspace) != handle.git_identity:
            raise WorkerAdapterFailure("git_finalize_failed")
        git_directory = handle.filesystem_workspace / ".git"
        try:
            shutil.rmtree(git_directory)
        except OSError as exc:
            raise WorkerAdapterFailure("git_finalize_failed") from exc
        if git_directory.exists() or git_directory.is_symlink():
            raise WorkerAdapterFailure("git_finalize_failed")
        self._initialize_repository(handle.filesystem_workspace, handle.deadline)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerAdapterFailure("process_timeout")
        return remaining

    @classmethod
    def _git(cls, workspace: Path, deadline: float, *arguments: str) -> str:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update(
            {
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        try:
            result = subprocess.run(
                ["git", *_SAFE_GIT_OPTIONS, *arguments],
                cwd=workspace,
                check=False,
                capture_output=True,
                env=environment,
                text=True,
                timeout=cls._remaining(deadline),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            stderr = getattr(exc, "stderr", None)
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            detail = stderr.rstrip("\n") if isinstance(stderr, str) else ""
            raise WorkerAdapterFailure(
                "git_finalize_failed", detail=detail or str(exc)
            ) from exc
        if result.returncode != 0:
            raise WorkerAdapterFailure(
                "git_finalize_failed",
                detail=result.stderr.rstrip("\n") or f"git exited {result.returncode}",
            )
        return result.stdout.rstrip("\n")
