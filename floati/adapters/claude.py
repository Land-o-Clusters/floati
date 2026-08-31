"""Governed one-turn client for Claude Code print mode."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence

from ..storage_identity import EVIDENCE_DIRECTORY, refuse_legacy_workspace_artifacts
from ..workers import WorkerAdapterFailure
from .codex_live import (
    _WORKSPACE_PARENT,
    _open_private_file,
    _secure_directory,
    CodexAppServerAdapter,
)
from .headless_template import _PERMISSION_MARKERS


MAX_CLAUDE_OUTPUT_BYTES = 1024 * 1024
_DEFAULT_COMMAND = ("/opt/homebrew/bin/claude",)
_HEADLESS_ARGUMENTS = (
    "-p",
    "--input-format",
    "text",
    "--output-format",
    "json",
    "--permission-mode",
    "dontAsk",
    "--no-session-persistence",
    "--tools",
    "Read,Write,Edit",
)


@dataclass
class _ClaudeHandle:
    work_id: str
    workspace: Path
    filesystem_workspace: Path
    process: subprocess.Popen[bytes]
    stderr_path: Path
    deadline: float
    git_identity: tuple[int, int, int]


class ClaudeHeadlessAdapter(CodexAppServerAdapter):
    """Claude `-p` worker with explicit workspace and fail-closed tools."""

    name = "claude"
    cancel_mode = "native"
    requires_workspace = True

    def __init__(
        self,
        command: Sequence[str] = _DEFAULT_COMMAND,
        *,
        isolate_process_group: bool = True,
    ) -> None:
        if (
            not isinstance(command, (tuple, list))
            or not command
            or any(not isinstance(part, str) or not part for part in command)
            or not Path(command[0]).is_absolute()
        ):
            raise WorkerAdapterFailure("process_start_failed")
        super().__init__(
            command, isolate_process_group=isolate_process_group,
        )
        self._active_process: Optional[subprocess.Popen[bytes]] = None

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

        stderr_file = None
        filesystem_workspace = workspace
        try:
            filesystem_workspace = self._pin_prepared_workspace(
                workspace, workspace_descriptor,
            )
            refuse_legacy_workspace_artifacts(filesystem_workspace)
            deadline = time.monotonic() + float(deadline_seconds)
            self._initialize_repository(filesystem_workspace, deadline)
            git_identity = self._git_identity(filesystem_workspace)
            evidence_dir = filesystem_workspace / EVIDENCE_DIRECTORY
            _secure_directory(evidence_dir, create=True)
            stderr_path = evidence_dir / "claude.stderr"
            stderr_file = _open_private_file(stderr_path)
            process = subprocess.Popen(
                [
                    *self.command,
                    *_HEADLESS_ARGUMENTS,
                    "--",
                    str(item.get("title", "")),
                ],
                cwd=filesystem_workspace,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=False,
                bufsize=0,
                start_new_session=self.isolate_process_group,
            )
            self._active_process = process
            if (
                self.isolate_process_group
                and self._process_group_registrar is not None
            ):
                self._process_group_registrar(process.pid)
        except WorkerAdapterFailure:
            if stderr_file is not None:
                stderr_file.close()
            self._restore_prepared_workspace()
            raise
        except (OSError, ValueError) as exc:
            if stderr_file is not None:
                stderr_file.close()
            self._restore_prepared_workspace()
            raise WorkerAdapterFailure("process_start_failed") from exc
        except Exception:
            self._restore_prepared_workspace()
            raise
        finally:
            if stderr_file is not None and not stderr_file.closed:
                stderr_file.close()
        return _ClaudeHandle(
            str(item["id"]), workspace, filesystem_workspace,
            process, stderr_path, deadline, git_identity,
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
                not isinstance(handle, _ClaudeHandle)
                or handle.work_id != item.get("id")
                or handle.workspace != self._workspace(item)
            ):
                raise WorkerAdapterFailure("protocol_error")
            deadline = min(handle.deadline, time.monotonic() + float(deadline_seconds))
            payload = self._read_bounded(handle.process, deadline)
            return_code = self._wait(handle.process, deadline)
            output_path = (
                handle.filesystem_workspace / EVIDENCE_DIRECTORY / "claude-output.json"
            )
            with _open_private_file(output_path) as output:
                output.write(payload.decode("utf-8"))
            self._validate_result(payload, return_code, handle.stderr_path)
            return self._finalize(handle)
        finally:
            if isinstance(handle, _ClaudeHandle):
                self._close_process(handle.process)
            self._active_process = None
            self._restore_prepared_workspace()

    def cancel(self) -> None:
        process = self._active_process
        try:
            if process is not None:
                self._terminate(process)
                self._close_process(process)
                self._active_process = None
        finally:
            self._restore_prepared_workspace()

    @staticmethod
    def _read_bounded(process: subprocess.Popen[bytes], deadline: float) -> bytes:
        if process.stdout is None:
            raise WorkerAdapterFailure("process_died")
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                ClaudeHeadlessAdapter._terminate(process)
                raise WorkerAdapterFailure("process_timeout")
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                ClaudeHeadlessAdapter._terminate(process)
                raise WorkerAdapterFailure("process_timeout")
            try:
                chunk = os.read(process.stdout.fileno(), 65536)
            except OSError as exc:
                raise WorkerAdapterFailure("process_died") from exc
            if not chunk:
                return bytes(chunks)
            chunks.extend(chunk)
            if len(chunks) > MAX_CLAUDE_OUTPUT_BYTES:
                ClaudeHeadlessAdapter._terminate(process)
                raise WorkerAdapterFailure("protocol_error")

    @staticmethod
    def _wait(process: subprocess.Popen[bytes], deadline: float) -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            ClaudeHeadlessAdapter._terminate(process)
            raise WorkerAdapterFailure("process_timeout")
        try:
            return process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            ClaudeHeadlessAdapter._terminate(process)
            raise WorkerAdapterFailure("process_timeout") from exc

    @staticmethod
    def _validate_result(payload: bytes, return_code: int, stderr_path: Path) -> None:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerAdapterFailure("protocol_error") from exc
        if not isinstance(value, dict):
            raise WorkerAdapterFailure("protocol_error")
        result = value.get("result")
        combined = " ".join(
            (
                str(result or ""),
                stderr_path.read_text(encoding="utf-8", errors="replace"),
            )
        ).lower()
        if value.get("is_error") is True or return_code != 0:
            if any(marker in combined for marker in _PERMISSION_MARKERS):
                raise WorkerAdapterFailure("approval_required_unattended")
            raise WorkerAdapterFailure("turn_failed")
        if (
            return_code != 0
            or value.get("type") != "result"
            or value.get("subtype") != "success"
            or value.get("is_error") is not False
            or not isinstance(result, str)
            or not result
        ):
            raise WorkerAdapterFailure("protocol_error")

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=1)

    @staticmethod
    def _close_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            ClaudeHeadlessAdapter._terminate(process)
        if process.stdout is not None:
            process.stdout.close()
