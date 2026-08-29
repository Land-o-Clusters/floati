"""Governed client for the local pi LF-JSONL RPC process."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, IO, Dict, Optional, Sequence

from ..storage_identity import EVIDENCE_DIRECTORY, refuse_legacy_workspace_artifacts
from ..workers import WorkerAdapterFailure
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

    def spawn(self, item: Dict[str, object], *, deadline_seconds: float) -> object:
        binding = self._prepared_workspace_identity
        self._prepared_workspace_identity = None
        workspace = self._workspace(item)
        workspace_descriptor = self._accept_prepared_workspace(workspace, binding)
        if workspace_descriptor is None:
            if workspace.exists():
                raise WorkerAdapterFailure("workspace_invalid")
            try:
                if not _WORKSPACE_PARENT.exists() and not _WORKSPACE_PARENT.is_symlink():
                    _WORKSPACE_PARENT.mkdir(parents=True, mode=0o700)
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
