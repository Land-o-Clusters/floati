"""Governed one-turn client for Claude Code print mode."""

from __future__ import annotations

import json
import os
import select
import signal
import stat
import subprocess
import sys
import time
from copy import deepcopy
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
from .native_io import BoundedNativeIO


MAX_CLAUDE_OUTPUT_BYTES = 1024 * 1024
_OAUTH_TOKEN_FILE = "claude-oauth-token"
_MAX_OAUTH_TOKEN_BYTES = 16 * 1024
_NATIVE_ENVIRONMENT_KEYS = frozenset({
    "PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM", "TZ",
    "PYTHONDONTWRITEBYTECODE",
})
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
# Claude's own write confinement, set explicitly on every ad hoc launch (the
# legacy fresh-workspace argv above is unchanged). Cited from Claude Code's CLI
# and settings references: `--restricted` (v2.1.248+) confines the built-in file
# tools to the working directories and loads only managed settings and
# `--settings`, so a row's own .claude/settings.json cannot widen the scope;
# `permissions.blockWritesOutsideWorkingDirectories` (v2.1.314+) makes Write,
# Edit and NotebookEdit refuse paths outside the working directories in every
# permission mode. A Claude older than v2.1.248 refuses the unknown flag, so the
# launch fails closed. Enforcement by an installed Claude is a live gate.
_NATIVE_CONFINEMENT_ARGUMENTS = (
    "--restricted",
    "--settings",
    json.dumps({"permissions": {"blockWritesOutsideWorkingDirectories": True}},
               separators=(",", ":")),
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


@dataclass
class _NativeClaudeHandle:
    work_id: str
    workspace: Path
    process: subprocess.Popen[bytes]
    deadline: float
    provider_descriptor: int
    provider_path: Path
    provider_identity: tuple[int, int]
    context: dict


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
        self._native_thread_context = None
        self._native_thread_emit = None
        self._native_provider_descriptor = None

    def set_native_thread_context(self, context: dict, emit) -> None:
        from ..errors import ProtocolRefusal
        from ..worker_bootstrap_protocol import validate_native_thread_context

        if self._native_thread_context is not None or not callable(emit):
            raise WorkerAdapterFailure("protocol_error")
        # No default permissions and no persistent launch until the closed
        # parent handoff has been checked. Legacy argv remains untouched.
        if (type(context) is not dict or type(context.get("permission")) is not dict
                or context["permission"] != {
                    "mode": "dontAsk", "tools": ["Read", "Write", "Edit"]}):
            raise WorkerAdapterFailure("native_permission_unsupported")
        try:
            context = validate_native_thread_context(context)
        except ProtocolRefusal as exc:
            raise WorkerAdapterFailure(exc.code) from exc
        coordinate = context["provider_coordinate"]
        if coordinate is not None and (coordinate["provider"] != "claude_local"
                                       or coordinate["session_file"] is not None):
            raise WorkerAdapterFailure("native_session_mismatch")
        self._native_thread_context = context
        self._native_thread_emit = emit

    @staticmethod
    def _native_directory(path: Path, expected=None) -> int:
        descriptor = None
        try:
            if not path.is_absolute() or path.resolve(strict=True) != path:
                raise WorkerAdapterFailure("adhoc_binding_changed")
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            metadata = os.fstat(descriptor)
            current = path.lstat()
            identity = (metadata.st_dev, metadata.st_ino)
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                    or not stat.S_ISDIR(current.st_mode)
                    or identity != (current.st_dev, current.st_ino)
                    or expected is not None and identity != expected):
                raise WorkerAdapterFailure("adhoc_binding_changed")
            return descriptor
        except (OSError, ValueError, RuntimeError) as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise WorkerAdapterFailure("adhoc_binding_changed") from exc

    @staticmethod
    def _native_oauth_token(provider_descriptor: int) -> Optional[str]:
        """Read an optional owner-supplied token through the pinned slot only."""
        descriptor = None
        try:
            try:
                descriptor = os.open(_OAUTH_TOKEN_FILE,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=provider_descriptor)
            except FileNotFoundError:
                return None
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or not 0 < before.st_size <= _MAX_OAUTH_TOKEN_BYTES):
                raise WorkerAdapterFailure("adhoc_binding_changed")
            data = bytearray()
            while len(data) <= _MAX_OAUTH_TOKEN_BYTES:
                chunk = os.read(descriptor, _MAX_OAUTH_TOKEN_BYTES + 1 - len(data))
                if not chunk:
                    break
                data.extend(chunk)
            after = os.fstat(descriptor)
            current = os.stat(_OAUTH_TOKEN_FILE, dir_fd=provider_descriptor, follow_symlinks=False)
            identity = lambda value: (value.st_dev, value.st_ino, value.st_mode,
                                      value.st_uid, value.st_nlink, value.st_size, value.st_mtime_ns)
            if (identity(before) != identity(after) or identity(after) != identity(current)
                    or len(data) != before.st_size):
                raise WorkerAdapterFailure("adhoc_binding_changed")
            token = data.decode("ascii").rstrip("\r\n")
            if not token or any(not 0x21 <= ord(character) <= 0x7e for character in token):
                raise WorkerAdapterFailure("adhoc_binding_changed")
            return token
        except (OSError, ValueError, UnicodeError) as exc:
            raise WorkerAdapterFailure("adhoc_binding_changed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _spawn_native(self, item, *, deadline_seconds):
        context = self._native_thread_context
        workspace = Path(context["workspace_binding"]["workspace"])
        if (item.get("id") != context["work_item_id"]
                or item.get("workspace") != str(workspace)):
            raise WorkerAdapterFailure("native_session_mismatch")
        if (type(deadline_seconds) not in (int, float)
                or not 0 < deadline_seconds < float("inf")):
            raise WorkerAdapterFailure("protocol_error")
        prompt = item.get("native_prompt", item.get("title", ""))
        if (type(prompt) is not str or not prompt
                or len(prompt.encode("utf-8")) > 1_048_576):
            raise WorkerAdapterFailure("protocol_error")
        prepared = self._prepared_workspace_identity
        self._prepared_workspace_identity = None
        expected = None
        if prepared is not None:
            if prepared[0] != str(workspace):
                raise WorkerAdapterFailure("adhoc_binding_changed")
            expected = prepared[1:]
        descriptor = self._native_directory(workspace, expected)
        slot = context["provider_state_binding"]
        provider_path = Path(slot["path"])
        provider_identity = (slot["dev"], slot["ino"])
        try:
            workspace_descriptor, descriptor = descriptor, None
            # The inherited lifecycle takes ownership even when pinning fails.
            filesystem_workspace = self._pin_prepared_workspace(workspace, workspace_descriptor)
            provider_descriptor = self._native_directory(provider_path, provider_identity)
            self._native_provider_descriptor = provider_descriptor
            # Retain only ordinary runtime input: provider credentials, token
            # descriptors and cloud selectors must not override the slot.
            environment = {name: value for name, value in os.environ.items()
                           if name in _NATIVE_ENVIRONMENT_KEYS}
            token = self._native_oauth_token(provider_descriptor)
            if token is None and sys.platform == "darwin":
                raise WorkerAdapterFailure("adhoc_binding_changed")
            # Pass the credential only to this child. Neither the controller
            # environment nor native context/evidence receives the value.
            if token is not None:
                environment["CLAUDE_CODE_OAUTH_TOKEN"] = token
            environment.update(HOME=str(provider_path), CLAUDE_CONFIG_DIR=str(provider_path),
                               TMPDIR=str(provider_path), TMP=str(provider_path), TEMP=str(provider_path))
            arguments = [part for part in _HEADLESS_ARGUMENTS
                         if part != "--no-session-persistence"]
            arguments.extend(_NATIVE_CONFINEMENT_ARGUMENTS)
            if context["operation"] == "resume":
                arguments.extend(("--resume", context["provider_coordinate"]["session_id"]))
            deadline = time.monotonic() + float(deadline_seconds)
            process = subprocess.Popen(
                [*self.command, *arguments, "--", prompt],
                cwd=filesystem_workspace, env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=False, bufsize=0, start_new_session=self.isolate_process_group,
            )
            self._active_process = process
            if self.isolate_process_group and self._process_group_registrar is not None:
                self._process_group_registrar(process.pid)
            return _NativeClaudeHandle(str(item["id"]), workspace, process, deadline,
                provider_descriptor, provider_path, provider_identity, deepcopy(context))
        except (OSError, ValueError) as exc:
            self.cancel()
            raise WorkerAdapterFailure("process_start_failed") from exc
        except Exception:
            self.cancel()
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def spawn(self, item: Dict[str, object], *, deadline_seconds: float) -> object:
        if self._native_thread_context is not None:
            return self._spawn_native(item, deadline_seconds=deadline_seconds)
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
        if isinstance(handle, _NativeClaudeHandle):
            return self._drive_native(handle, item, deadline_seconds=deadline_seconds)
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

    def _drive_native(self, handle, item, *, deadline_seconds):
        from ..adhoc_records import NATIVE_BINDING_FIELDS
        from ..errors import ProtocolRefusal
        from ..native_thread_protocol import validate_native_event

        try:
            if (handle.work_id != item.get("id") or item.get("workspace") != str(handle.workspace)
                    or handle.context != self._native_thread_context
                    or handle.process is not self._active_process):
                raise WorkerAdapterFailure("protocol_error")
            deadline = min(handle.deadline, time.monotonic() + float(deadline_seconds))
            transport = BoundedNativeIO(handle.process, deadline=deadline,
                                        bounds=handle.context["bounds"])
            try:
                payload = transport.read_terminal_json()
            except WorkerAdapterFailure as exc:
                if (exc.code == "protocol_error" and not transport.snapshot()["stdout_bytes"]
                        and handle.context["operation"] == "start"):
                    raise WorkerAdapterFailure("adhoc_spawn_unknown") from exc
                raise
            self.last_native_transport = transport.drain_to_exit()
            value = json.loads(payload.decode("utf-8"))
            if type(value) is not dict:
                raise WorkerAdapterFailure("protocol_error")
            if value.get("is_error") is True or handle.process.returncode != 0:
                result = str(value.get("result", "")).lower()
                code = ("approval_required_unattended"
                        if any(marker in result for marker in _PERMISSION_MARKERS) else "turn_failed")
                raise WorkerAdapterFailure(code)
            if (value.get("type") != "result" or value.get("subtype") != "success"
                    or value.get("is_error") is not False
                    or type(value.get("result")) is not str or not value["result"]):
                raise WorkerAdapterFailure("protocol_error")
            session_id = value.get("session_id")
            if type(session_id) is not str or not session_id:
                raise WorkerAdapterFailure("adhoc_spawn_unknown" if handle.context["operation"] == "start"
                                           else "native_session_mismatch")
            coordinate = {"provider": "claude_local", "session_id": session_id, "session_file": None}
            event = {"schema_version": 1, "event": "terminal", "outcome": "success",
                "binding": {field: deepcopy(handle.context[field]) for field in NATIVE_BINDING_FIELDS},
                "provider_coordinate": coordinate}
            if "usage" in value:
                from ..native_usage import provider_usage
                event["usage"] = provider_usage("claude", value["usage"])
            try:
                event = validate_native_event(handle.context, event, adapter_kind="claude",
                                             opened_coordinate=None, terminal_seen=False)
            except ProtocolRefusal as exc:
                raise WorkerAdapterFailure(exc.code) from exc
            # Provider state and row remain borrowed: no Git init, reset,
            # snapshot, raw transcript file, or child-owned finalization.
            descriptor = self._native_directory(handle.provider_path, handle.provider_identity)
            os.close(descriptor)
            self._require_workspace_descriptor_identity(handle.workspace,
                                                       self._prepared_workspace_descriptor)
            self._close_process(handle.process)
            self._native_thread_emit(event)
            return []
        finally:
            self.cancel()

    def cancel(self) -> None:
        process = self._active_process
        try:
            if process is not None:
                self._terminate(process)
                self._close_process(process)
                self._active_process = None
        finally:
            if self._native_provider_descriptor is not None:
                os.close(self._native_provider_descriptor)
                self._native_provider_descriptor = None
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
        if process.stderr is not None:
            process.stderr.close()
