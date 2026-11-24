"""Governed client for the local pi LF-JSONL RPC process."""

from __future__ import annotations

import json
import os
import re
import select
import signal
import stat
import subprocess
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, IO, Dict, Optional, Sequence

from ..storage_identity import EVIDENCE_DIRECTORY, refuse_legacy_workspace_artifacts
from ..adhoc_records import NATIVE_BINDING_FIELDS, UUID
from ..errors import ProtocolRefusal
from ..native_thread_protocol import validate_native_event
from ..worker_bootstrap_protocol import validate_native_thread_context
from ..workers import WorkerAdapterFailure
from .native_io import BoundedNativeIO
from .codex_live import (
    _WORKSPACE_PARENT,
    _open_private_file,
    _secure_directory,
    CodexAppServerAdapter,
)


MAX_RPC_LINE_BYTES = 65536
_DEFAULT_COMMAND = ("/opt/homebrew/bin/pi", "--mode", "rpc", "--no-session")


class PiRpcSession:
    """One bounded pi RPC session using LF-only record framing."""

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
        self._stdout_buffer = bytearray()
        self._next_id = 1
        self._deadline: Optional[float] = None

    def start(self, *, deadline_seconds: float) -> None:
        if (
            not isinstance(deadline_seconds, (int, float))
            or isinstance(deadline_seconds, bool)
            or deadline_seconds <= 0
        ):
            raise WorkerAdapterFailure("protocol_error")
        refuse_legacy_workspace_artifacts(self.workspace)
        self._deadline = time.monotonic() + float(deadline_seconds)
        evidence_dir = self.workspace / EVIDENCE_DIRECTORY
        _secure_directory(evidence_dir, create=True)
        self.transcript = _open_private_file(evidence_dir / "transcript.jsonl")
        self.stderr = _open_private_file(evidence_dir / "pi.stderr")
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
            if self.isolate_process_group and self.process_group_registrar is not None:
                self.process_group_registrar(self.process.pid)
        except (OSError, ValueError) as exc:
            self.close()
            raise WorkerAdapterFailure("process_start_failed") from exc

    def run(self, prompt: str, *, deadline_seconds: float) -> None:
        self.start(deadline_seconds=deadline_seconds)
        try:
            self.run_turn(prompt)
        finally:
            self.close()

    def run_turn(self, prompt: str) -> None:
        if not isinstance(prompt, str) or not prompt or self._deadline is None:
            raise WorkerAdapterFailure("protocol_error")
        request_id = f"req-{self._next_id}"
        self._next_id += 1
        try:
            self._write({"id": request_id, "type": "prompt", "message": prompt})
            self._wait_for_response(request_id)
            self._wait_for_terminal_event()
        except WorkerAdapterFailure as failure:
            if failure.code == "process_timeout":
                self._abort()
            raise

    def close(self) -> None:
        process = self.process
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    self._signal(process, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        self._signal(process, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    process.wait(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
        if self.stderr is not None:
            self.stderr.close()
        if self.transcript is not None:
            self.transcript.close()
        self.process = None
        self.stderr = None
        self.transcript = None

    def _write(self, message: Dict[str, object]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise WorkerAdapterFailure("process_died")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        self._record("out", message)
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise WorkerAdapterFailure("process_died") from exc

    def _read(self) -> Dict[str, object]:
        process = self.process
        deadline = self._deadline
        if process is None or process.stdout is None or deadline is None:
            raise WorkerAdapterFailure("process_died")
        while b"\n" not in self._stdout_buffer:
            if len(self._stdout_buffer) > MAX_RPC_LINE_BYTES:
                raise WorkerAdapterFailure("protocol_error")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Deadline expiry does not prove that the provider emitted no
                # testimony: the parent may have been descheduled after the
                # pipe became readable. Drain only bytes already ready now;
                # an empty pipe remains a genuine timeout, while a complete
                # queued frame reaches the normal protocol parser.
                self._drain_ready_output(process)
                if b"\n" in self._stdout_buffer:
                    continue
                raise WorkerAdapterFailure("process_timeout")
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                self._drain_ready_output(process)
                if b"\n" in self._stdout_buffer:
                    continue
                raise WorkerAdapterFailure("process_timeout")
            self._read_stdout_chunk(process)
        line, _, remainder = self._stdout_buffer.partition(b"\n")
        self._stdout_buffer = bytearray(remainder)
        if len(line) > MAX_RPC_LINE_BYTES or not line:
            raise WorkerAdapterFailure("protocol_error")
        if line.endswith(b"\r"):
            line = line[:-1]
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerAdapterFailure("protocol_error") from exc
        if not isinstance(message, dict):
            raise WorkerAdapterFailure("protocol_error")
        self._record("in", message)
        return message

    def _read_stdout_chunk(self, process: subprocess.Popen[bytes]) -> None:
        try:
            chunk = os.read(process.stdout.fileno(), 65536)
        except OSError as exc:
            raise WorkerAdapterFailure("process_died") from exc
        if not chunk:
            raise WorkerAdapterFailure("process_died")
        self._stdout_buffer.extend(chunk)

    def _drain_ready_output(self, process: subprocess.Popen[bytes]) -> None:
        """Read only currently queued bytes after a deadline boundary."""
        while b"\n" not in self._stdout_buffer:
            if len(self._stdout_buffer) > MAX_RPC_LINE_BYTES:
                return
            ready, _, _ = select.select([process.stdout], [], [], 0)
            if not ready:
                return
            self._read_stdout_chunk(process)

    def _wait_for_response(self, request_id: str) -> None:
        while True:
            message = self._read()
            if message.get("type") != "response" or message.get("id") != request_id:
                continue
            if message.get("success") is not True:
                raise WorkerAdapterFailure("turn_failed")
            return

    def _wait_for_terminal_event(self) -> None:
        while True:
            message = self._read()
            event_type = message.get("type")
            if event_type in {"agent_end", "turn_end"}:
                return
            if event_type == "message_update":
                event = message.get("assistantMessageEvent")
                if isinstance(event, dict) and event.get("type") == "error":
                    raise WorkerAdapterFailure("turn_failed")
                continue

    def _abort(self) -> None:
        try:
            self._write({"type": "abort"})
        except WorkerAdapterFailure:
            pass

    def _record(self, direction: str, message: Dict[str, object]) -> None:
        if self.transcript is None:
            return
        self.transcript.write(
            json.dumps(
                {"direction": direction, "message": message},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        self.transcript.flush()

    def _signal(self, process: subprocess.Popen[bytes], signum: signal.Signals) -> None:
        if self.isolate_process_group:
            os.killpg(process.pid, signum)
        else:
            process.send_signal(signum)


class _NativePiSession(PiRpcSession):
    """Persistent Pi RPC with parent-owned identity and no child Git effects."""

    _EVENTS = frozenset({
        "agent_start", "agent_end", "agent_settled", "turn_start", "turn_end",
        "message_start", "message_update", "message_end", "queue_update",
        "tool_execution_start", "tool_execution_update", "tool_execution_end",
        "compaction_start", "compaction_end", "auto_retry_start", "auto_retry_end",
        "auto_compaction_start", "auto_compaction_end", "extension_error",
        "summarization_retry_scheduled", "summarization_retry_attempt_start",
        "summarization_retry_finished",
    })

    def __init__(self, *args, context, emit, **kwargs):
        super().__init__(*args, **kwargs)
        self.context = context
        self.emit = emit
        self.native_io = None
        self.coordinate = None
        self._directories = []
        self._settled = False
        self._assistant_failed = False
        self.usage = None
        self.usage_incomplete = False
        self.usage_turn_active = False

    @staticmethod
    def _directory(path, expected=None, *, private=False):
        descriptor = None
        try:
            if not path.is_absolute() or path.resolve(strict=True) != path:
                raise WorkerAdapterFailure("adhoc_binding_changed")
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            metadata = os.fstat(descriptor)
            current = path.lstat()
            identity = metadata.st_dev, metadata.st_ino
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) & (0o077 if private else 0o022)
                    or stat.S_ISLNK(current.st_mode)
                    or identity != (current.st_dev, current.st_ino)
                    or expected is not None and identity != expected):
                raise WorkerAdapterFailure("adhoc_binding_changed")
            return descriptor
        except BaseException as exc:
            if descriptor is not None:
                os.close(descriptor)
            if isinstance(exc, (OSError, ValueError, RuntimeError)):
                raise WorkerAdapterFailure("adhoc_binding_changed") from exc
            raise

    def _require_bindings(self):
        try:
            for path, descriptor, private in self._directories:
                metadata, current = os.fstat(descriptor), path.lstat()
                if (not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode)
                        or current.st_uid != os.getuid()
                        or stat.S_IMODE(current.st_mode) & (0o077 if private else 0o022)
                        or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)):
                    raise WorkerAdapterFailure("adhoc_binding_changed")
        except OSError as exc:
            raise WorkerAdapterFailure("adhoc_binding_changed") from exc

    def _saved_header(self, coordinate):
        """Read only the exact private file's bounded first JSONL frame."""
        path = Path(coordinate["session_file"])
        slot = Path(self.context["provider_state_binding"]["path"])
        descriptor = None
        parents = []
        try:
            if (not path.is_absolute() or path == slot or slot not in path.parents
                    or path.resolve(strict=False) != path):
                raise WorkerAdapterFailure("native_session_mismatch")
            root = next(fd for held, fd, _ in self._directories if held == slot)
            parent = root
            parts = path.relative_to(slot).parts
            for part in parts[:-1]:
                parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=parent)
                parents.append(parent)
                metadata = os.fstat(parent)
                if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise WorkerAdapterFailure("native_session_mismatch")
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
            metadata = os.fstat(descriptor)
            current = path.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                    or metadata.st_nlink != 1
                    or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)):
                raise WorkerAdapterFailure("native_session_mismatch")
            if metadata.st_size == 0:
                raise WorkerAdapterFailure("native_session_unmaterialized")
            frame = bytearray()
            while b"\n" not in frame:
                chunk = os.read(descriptor, min(4096, MAX_RPC_LINE_BYTES + 1 - len(frame)))
                if not chunk:
                    break
                frame.extend(chunk)
                if len(frame) > MAX_RPC_LINE_BYTES:
                    raise WorkerAdapterFailure("native_session_mismatch")

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate session header field")
                    result[key] = value
                return result

            header = json.loads(bytes(frame).split(b"\n", 1)[0].decode("utf-8"),
                                object_pairs_hook=unique)
            if (type(header) is not dict or header.get("type") != "session"
                    or type(header.get("version")) is not int or header["version"] != 3
                    or header.get("id") != coordinate["session_id"]
                    or header.get("cwd") != str(self.workspace)):
                raise WorkerAdapterFailure("native_session_mismatch")
            if (path.resolve(strict=True) != path
                    or (path.lstat().st_dev, path.lstat().st_ino) != (metadata.st_dev, metadata.st_ino)):
                raise WorkerAdapterFailure("native_session_mismatch")
        except FileNotFoundError as exc:
            raise WorkerAdapterFailure("native_session_unmaterialized") from exc
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            raise WorkerAdapterFailure("native_session_mismatch") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            for parent in reversed(parents):
                os.close(parent)

    def _event(self, phase):
        event = {"schema_version": 1, "event": phase,
            "binding": {field: deepcopy(self.context[field]) for field in NATIVE_BINDING_FIELDS},
            "provider_coordinate": deepcopy(self.coordinate)}
        if phase == "terminal":
            event["outcome"] = "success"
            if self.usage is not None and not self.usage_incomplete:
                event["usage"] = deepcopy(self.usage)
        return event

    def _check_record(self, record):
        kind = record.get("type")
        if kind == "extension_ui_request":
            raise WorkerAdapterFailure("approval_required_unattended")
        if kind == "response":
            return
        if type(kind) is not str or kind not in self._EVENTS:
            raise WorkerAdapterFailure("protocol_error")
        if kind == "extension_error":
            raise WorkerAdapterFailure("turn_failed")
        if kind == "message_update":
            event = record.get("assistantMessageEvent")
            if type(event) is dict and event.get("type") == "error":
                self._assistant_failed = True
        if kind == "message_end":
            message = record.get("message")
            if type(message) is dict and message.get("role") == "assistant":
                if self.usage_turn_active and "usage" in message:
                    from ..native_usage import add_pi_usage, provider_usage
                    self.usage = add_pi_usage(self.usage, provider_usage("pi", message["usage"]))
                elif self.usage_turn_active:
                    self.usage_incomplete = True
                # Pi retains failed attempts in session history and may retry
                # them before settlement. Only a later completed assistant
                # response can clear that provisional failure.
                reason = message.get("stopReason")
                if type(reason) is not str or reason not in {"stop", "length", "toolUse", "error", "aborted"}:
                    raise WorkerAdapterFailure("protocol_error")
                self._assistant_failed = reason in {"error", "aborted"}
        if kind == "auto_retry_end" and record.get("success") is False:
            self._assistant_failed = True
        if kind == "agent_settled":
            self._settled = True

    def _read(self):
        if self.native_io is None:
            raise WorkerAdapterFailure("process_died")
        record = self.native_io.read_record()
        if record is None:
            raise WorkerAdapterFailure("process_died")
        self._check_record(record)
        return record

    def _request(self, kind, **fields):
        request_id = "req-" + str(self._next_id)
        self._next_id += 1
        self._write({"id": request_id, "type": kind, **fields})
        while True:
            record = self._read()
            if record.get("type") != "response":
                continue
            if record.get("id") != request_id or record.get("command") != kind:
                raise WorkerAdapterFailure("protocol_error")
            if record.get("success") is not True:
                raise WorkerAdapterFailure("turn_failed")
            return record.get("data")

    def _state(self):
        state = self._request("get_state")
        if (type(state) is not dict or state.get("isStreaming") is not False
                or state.get("isCompacting") is not False
                or type(state.get("pendingMessageCount")) is not int
                or state["pendingMessageCount"] != 0):
            raise WorkerAdapterFailure("native_session_mismatch")
        coordinate = {"provider": "pi_local", "session_id": state.get("sessionId"),
                      "session_file": state.get("sessionFile")}
        # The event validator supplies the closed native ID/path bounds and
        # rejects substitutions on resume before the parent receives an event.
        validate_native_event(self.context, {**self._event("opened"),
            "provider_coordinate": coordinate}, adapter_kind="pi",
            opened_coordinate=None, terminal_seen=False)
        path = Path(coordinate["session_file"])
        slot = Path(self.context["provider_state_binding"]["path"])
        if path == slot or slot not in path.parents or path.resolve(strict=False) != path:
            raise WorkerAdapterFailure("native_session_mismatch")
        if self.coordinate is not None and coordinate != self.coordinate:
            raise WorkerAdapterFailure("native_session_mismatch")
        return coordinate

    def start(self, *, deadline_seconds):
        if (type(deadline_seconds) not in (int, float)
                or not 0 < deadline_seconds < float("inf")):
            raise WorkerAdapterFailure("protocol_error")
        self._deadline = time.monotonic() + float(deadline_seconds)
        slot = self.context["provider_state_binding"]
        try:
            for path, expected, private in ((self.workspace, None, False),
                    (Path(slot["path"]), (slot["dev"], slot["ino"]), True)):
                self._directories.append((path, self._directory(path, expected,
                                          private=private), private))
            if self.context["operation"] == "resume":
                self._saved_header(self.context["provider_coordinate"])
            self._require_bindings()
            environment = dict(os.environ)
            environment.update(HOME=slot["path"], PI_CODING_AGENT_DIR=slot["path"],
                               TMPDIR=slot["path"], TMP=slot["path"], TEMP=slot["path"])
            self.process = subprocess.Popen(self.command, cwd=self.workspace,
                env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=False, bufsize=0,
                start_new_session=self.isolate_process_group, umask=0o077)
            if self.isolate_process_group and self.process_group_registrar is not None:
                self.process_group_registrar(self.process.pid)
            self.native_io = BoundedNativeIO(self.process, deadline=self._deadline,
                                            bounds=self.context["bounds"])
            self.coordinate = self._state()
            if self.context["operation"] == "resume":
                self._saved_header(self.coordinate)
            self._require_bindings()
            event = self._event("opened")
            ack = self.emit(event)
            if (type(ack) is not dict or set(ack) != set(event) | {"opened_record_id"}
                    or ack.get("event") != "opened_ack"
                    or type(ack.get("schema_version")) is not int or ack["schema_version"] != 1
                    or ack.get("binding") != event["binding"]
                    or ack.get("provider_coordinate") != self.coordinate
                    or type(ack.get("opened_record_id")) is not str
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

    def run_turn(self, prompt):
        if (type(prompt) is not str or not prompt or len(prompt.encode("utf-8")) > 1_048_576):
            raise WorkerAdapterFailure("protocol_error")
        self._require_bindings()
        if self.context["operation"] == "resume":
            self._saved_header(self.coordinate)
        self._settled = False
        self._assistant_failed = False
        # Startup state may replay history before its correlated idle response.
        # Only final messages after this prompt belong to this dispatch's sum.
        self.usage = None
        self.usage_incomplete = False
        self.usage_turn_active = True
        prompt += "\nAuthenticated provider_coordinate: " + json.dumps(self.coordinate, sort_keys=True)
        self._request("prompt", message=prompt)
        while not self._settled:
            self._read()
        self._state()
        self._saved_header(self.coordinate)

    def finish(self):
        if self.process is None or self.native_io is None:
            raise WorkerAdapterFailure("process_died")
        if self.process.stdin is not None:
            self.process.stdin.close()

        def tail(record):
            self._check_record(record)
            # A settled idle terminal has no remaining provider activity.
            raise WorkerAdapterFailure("protocol_error")

        evidence = self.native_io.drain_to_exit(on_record=tail)
        if evidence["exit_code"] != 0:
            raise WorkerAdapterFailure("process_died")
        self._require_bindings()
        self._saved_header(self.coordinate)
        self.close()
        if self._assistant_failed:
            raise WorkerAdapterFailure("turn_failed")
        self.emit(self._event("terminal"))

    def close(self):
        process = self.process
        stderr = process.stderr if process is not None else None
        try:
            super().close()
            if process is not None and self.isolate_process_group:
                # Reap the direct child before quiescing its owned descendants.
                from .codex_live import AppServerSession
                AppServerSession._quiesce_process_group(process.pid)
        finally:
            if stderr is not None:
                stderr.close()
            for _, descriptor, _ in self._directories:
                os.close(descriptor)
            self._directories.clear()


@dataclass
class _PiHandle:
    work_id: str
    workspace: Path
    filesystem_workspace: Path
    session: PiRpcSession
    deadline: float
    git_identity: tuple[int, int, int]


class PiRpcAdapter(CodexAppServerAdapter):
    """Pi RPC worker with the same workspace and artifact boundary as Codex."""

    name = "pi"
    cancel_mode = "native"
    requires_workspace = True

    def __init__(
        self,
        command: Sequence[str] = _DEFAULT_COMMAND,
        *,
        isolate_process_group: bool = True,
    ) -> None:
        super().__init__(
            command, isolate_process_group=isolate_process_group,
        )
        self._active_session: Optional[PiRpcSession] = None

    def set_native_thread_context(self, context, emit):
        if self._native_context is not None or not callable(emit):
            raise WorkerAdapterFailure("protocol_error")
        if (type(context) is not dict or type(context.get("permission")) is not dict
                or context["permission"] not in (
                    {"mode": "tools_disabled"},
                    {"mode": "allowlisted_tools", "tools": ["read", "write", "edit"]})):
            raise WorkerAdapterFailure("native_permission_unsupported")
        try:
            context = validate_native_thread_context(context)
        except ProtocolRefusal as exc:
            raise WorkerAdapterFailure(exc.code) from exc
        coordinate = context["provider_coordinate"]
        if coordinate is not None and coordinate["provider"] != "pi_local":
            raise WorkerAdapterFailure("native_session_mismatch")
        if (context["workspace_binding"]["profile_id"] != context["profile_id"]
                or context["workspace_binding"]["node_lease_id"] != context["node_lease_id"]):
            raise WorkerAdapterFailure("adhoc_binding_changed")
        self._native_context, self._native_emitter = context, emit

    def _native_command(self):
        prefix = self.command[:1] if self.command == _DEFAULT_COMMAND else self.command
        if (not prefix or any(type(part) is not str or not part for part in prefix)
                or not Path(prefix[0]).is_absolute()):
            raise WorkerAdapterFailure("process_start_failed")
        forbidden = {"--mode", "--session", "--session-dir", "--session-id", "--no-session",
            "--resume", "--continue", "--fork", "-r", "-c", "--extension", "-e",
            "--skill", "--prompt-template", "--theme", "--use-theme", "--tools", "-t",
            "--no-tools", "-nt", "--no-builtin-tools", "-nbt", "--exclude-tools", "-xt",
            "--system-prompt", "--append-system-prompt", "--print", "-p", "--export"}
        if any(part.split("=", 1)[0] in forbidden or part.startswith("@") for part in prefix[1:]):
            raise WorkerAdapterFailure("native_permission_unsupported")
        slot = self._native_context["provider_state_binding"]["path"]
        command = [*prefix, "--mode", "rpc", "--session-dir", slot,
            "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes",
            "--no-context-files"]
        permission = self._native_context["permission"]
        if permission["mode"] == "tools_disabled":
            command.append("--no-tools")
        else:
            command.extend(("--tools", ",".join(permission["tools"])))
        if self._native_context["operation"] == "resume":
            command.extend(("--session", self._native_context["provider_coordinate"]["session_file"]))
        return command

    def spawn(self, item: Dict[str, object], *, deadline_seconds: float) -> object:
        if self._native_context is not None:
            workspace = self._native_workspace(item)
            prepared = self._prepared_workspace_identity
            self._prepared_workspace_identity = None
            if prepared is not None:
                if prepared[0] != str(workspace):
                    raise WorkerAdapterFailure("adhoc_binding_changed")
                descriptor = _NativePiSession._directory(workspace, prepared[1:])
                os.close(descriptor)
            session = _NativePiSession(self._native_command(), workspace,
                context=self._native_context, emit=self._native_emitter,
                isolate_process_group=self.isolate_process_group,
                process_group_registrar=self._process_group_registrar)
            self._active_session = session
            session.start(deadline_seconds=deadline_seconds)
            return _PiHandle(str(item["id"]), workspace, workspace,
                session, session._deadline, (0, 0, 0))
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
            session = PiRpcSession(
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
        return _PiHandle(
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
                if (not isinstance(handle, _PiHandle)
                        or handle.session is not self._active_session
                        or handle.work_id != item.get("id")
                        or handle.workspace != self._native_workspace(item)):
                    raise WorkerAdapterFailure("protocol_error")
                if (type(deadline_seconds) not in (int, float)
                        or not 0 < deadline_seconds < float("inf")):
                    raise WorkerAdapterFailure("protocol_error")
                handle.session.run_turn(item.get("native_prompt", item.get("title", "")))
                handle.session.finish()
                self.last_native_transport = handle.session.native_io.snapshot()
                return []
            except ProtocolRefusal as exc:
                raise WorkerAdapterFailure(exc.code) from exc
            finally:
                self.cancel()
        try:
            if (
                not isinstance(handle, _PiHandle)
                or handle.work_id != item.get("id")
                or handle.workspace != self._workspace(item)
            ):
                raise WorkerAdapterFailure("protocol_error")
            handle.session.run_turn(str(item.get("title", "")))
            return self._finalize(handle)
        finally:
            if isinstance(handle, _PiHandle):
                handle.session.close()
            self._active_session = None
            self._restore_prepared_workspace()

    def cancel(self) -> None:
        try:
            if self._active_session is not None:
                self._active_session.close()
                self._active_session = None
        finally:
            self._restore_prepared_workspace()
