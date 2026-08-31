"""HeadlessProfileAdapter — the repaired roster template.

Every wiring discipline lives here ONCE (secure workspace, evidence
stderr, bounded read, permission-marker refusal, native cancel,
process-group isolation). Roster adapters are THIN: they name their
harness and declare a profile. Nothing else.

SURFACE HONESTY LAW (architect ruling msg-01a029d783ff): headless
argument spellings are CITED PER HARNESS or HONESTLY EMPTY. An argument
tuple that was never live-verified ships EMPTY — a probe that refuses on
absence is lawful; an invented spelling is not.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from ..storage_identity import EVIDENCE_DIRECTORY, refuse_legacy_workspace_artifacts
from ..workers import WorkerAdapterFailure
from .codex_live import (
    _WORKSPACE_PARENT,
    _open_private_file,
    _secure_directory,
    CodexAppServerAdapter,
)

MAX_PROFILE_OUTPUT_BYTES = 1024 * 1024
_PERMISSION_MARKERS = (
    "approval", "permission", "requires user", "not allowed",
)


@dataclass(frozen=True)
class HarnessProfile:
    """The per-harness declaration. Everything not live-verified is EMPTY.

    - name: closed-roster harness id
    - command: absolute path to the harness binary (probe target)
    - headless_arguments: CITED spellings, or () until live intake —
      an empty tuple is the honest state, not a placeholder
    - stderr_name: evidence filename for this harness's stderr capture
    - help_dump_signature: optional measured needles that identify a help/usage
      dump; None keeps today's marker scan (tolerant read, no forced migration)
    - prompt_form: how the work title is passed as the prompt. Default
      ("--",) — the Claude-Code idiom the template was written with.
      K4 finding (ZC1-K1 receipt, live): that idiom is NOT universal —
      zcode refuses `-- <title>` and its measured spelling is
      `--prompt <text>`. A member that has measured its prompt form
      declares it here; unmeasured members keep the default.
    """

    name: str
    command: Sequence[str]
    headless_arguments: Sequence[str] = ()
    stderr_name: str = "stderr"
    cited_source: Optional[str] = None
    help_dump_signature: Optional[Tuple[str, ...]] = None
    prompt_form: Sequence[str] = ("--",)

    def __post_init__(self) -> None:
        if not isinstance(self.command, (tuple, list)) or not self.command:
            raise WorkerAdapterFailure("process_start_failed")
        if any(not isinstance(part, str) or not part for part in self.command):
            raise WorkerAdapterFailure("process_start_failed")
        if not Path(self.command[0]).is_absolute():
            raise WorkerAdapterFailure("process_start_failed")
        if not isinstance(self.headless_arguments, tuple):
            raise WorkerAdapterFailure("process_start_failed")
        if any(not isinstance(a, str) or not a
               for a in self.headless_arguments):
            raise WorkerAdapterFailure("process_start_failed")
        if self.help_dump_signature is not None:
            if not isinstance(self.help_dump_signature, tuple):
                raise WorkerAdapterFailure("process_start_failed")
            if any(not isinstance(part, str) or not part
                   for part in self.help_dump_signature):
                raise WorkerAdapterFailure("process_start_failed")
        if not isinstance(self.prompt_form, tuple):
            raise WorkerAdapterFailure("process_start_failed")
        if any(not isinstance(f, str) or not f for f in self.prompt_form):
            raise WorkerAdapterFailure("process_start_failed")
        # Condition law: non-empty args REQUIRE a cited source (live intake).
        stripped_source = (self.cited_source or "").strip()
        if self.headless_arguments and not stripped_source:
            raise WorkerAdapterFailure("citation_missing")


@dataclass
class ProfileHandle:
    work_id: str
    workspace: Path
    filesystem_workspace: Path
    process: subprocess.Popen[bytes]
    stderr_path: Path
    deadline: float
    git_identity: tuple[int, int, int]


class HeadlessProfileAdapter(CodexAppServerAdapter):
    """Reference-contract adapter parameterized by a HarnessProfile.

    Subclasses declare ONLY their identity and profile; every wiring
    discipline is inherited from here, which is why a reference capability
    added tomorrow turns every roster adapter red via the runtime oracle.
    """

    cancel_mode = "native"
    requires_workspace = True
    #: CHECK-TWO flips this only after live intake verification
    #: (charter override 5).
    surface_verified = False

    def __init__(
        self,
        profile: HarnessProfile,
        *,
        isolate_process_group: bool = True,
    ) -> None:
        if not isinstance(profile, HarnessProfile):
            raise WorkerAdapterFailure("profile_declaration_invalid")
        self.profile = profile
        super().__init__(
            tuple(profile.command),
            isolate_process_group=isolate_process_group,
        )
        self._active_process: Optional[subprocess.Popen[bytes]] = None
        # Single-spelling law (binding msg-01a0282f72): the class attribute
        # and the profile name must agree or construction fails.
        if self.name != profile.name:
            raise WorkerAdapterFailure(
                "identity_mismatch: "
                + f"cls.name={self.name!r} != profile.name={profile.name!r}"
            )

    @classmethod
    def _default_profile(cls) -> HarnessProfile:
        raise NotImplementedError("subclass must bind its HarnessProfile")

    @classmethod
    def availability(
        cls, command: Sequence[str] = (), *,
        profile: Optional[HarnessProfile] = None,
    ) -> Dict[str, object]:
        """CHECK-TWO: typed live-surface probe. Reports binary presence
        as a filesystem fact; NEVER claims argument support."""
        resolved = tuple(command) if command else tuple(
            (profile or cls._default_profile()).command)
        binary = Path(resolved[0])
        return {
            "harness": (profile.name if profile
                        else cls._default_profile().name),
            "binary": str(binary),
            "present": binary.is_file(),
            "surface_verified": cls.surface_verified,
        }

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
            stderr_path = evidence_dir / self.profile.stderr_name
            stderr_file = _open_private_file(stderr_path)
            process = subprocess.Popen(
                [
                    *self.command,
                    *self.profile.headless_arguments,
                    *self._prompt_arguments(item),
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
            raise WorkerAdapterFailure("process_start_failed") from exc
        except Exception:
            self._restore_prepared_workspace()
            raise
        finally:
            if stderr_file is not None and not stderr_file.closed:
                stderr_file.close()
        return ProfileHandle(
            str(item["id"]), workspace, filesystem_workspace,
            process, stderr_path, deadline, git_identity,
        )

    def drive(
        self,
        handle: object,
        *,
        deadline_seconds: float,
    ) -> Dict[str, object]:
        assert isinstance(handle, ProfileHandle)
        deadline = min(
            handle.deadline,
            time.monotonic() + min(float(deadline_seconds), 3600.0),
        )
        output = self._read_bounded(handle.process, deadline)
        return_code = self._wait(handle.process, deadline)
        self._validate_result(output, return_code, handle.stderr_path)
        try:
            payload = json.loads(output.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"raw": output.decode("utf-8", errors="replace")}
        return {
            "work_id": handle.work_id,
            "payload": payload,
            "return_code": return_code,
        }

    def _prompt_arguments(self, item: Dict[str, object]) -> Sequence[str]:
        """The trailing prompt invocation: the profile's prompt form, then
        the work title. Default ("--",) reproduces the original inline
        argv exactly for every unmeasured member."""
        return (*self.profile.prompt_form, str(item.get("title", "")))

    def cancel(self) -> None:
        if self._active_process is None:
            return
        self._terminate(self._active_process)
        self._close_process(self._active_process)
        self._active_process = None

    def _read_bounded(self, process: subprocess.Popen[bytes],
                      deadline: float) -> bytes:
        collected = bytearray()
        while True:
            if len(collected) > MAX_PROFILE_OUTPUT_BYTES:
                raise WorkerAdapterFailure("output_limit_exceeded")
            if process.stdout is None:
                break
            piece = process.stdout.read(65536)
            if not piece:
                break
            collected.extend(piece)
            if time.monotonic() > deadline:
                raise WorkerAdapterFailure("deadline_exceeded")
        return bytes(collected)

    def _wait(self, process: subprocess.Popen[bytes], deadline: float) -> int:
        while True:
            code = process.poll()
            if code is not None:
                return code
            if time.monotonic() > deadline:
                raise WorkerAdapterFailure("deadline_exceeded")
            time.sleep(0.05)

    def _help_dump_present(self, lowered: str) -> bool:
        signature = self.profile.help_dump_signature
        if not signature:
            return False
        return all(needle.lower() in lowered for needle in signature)

    def _testimony_outside_help_dump(self, combined: str) -> str:
        signature = self.profile.help_dump_signature
        if not signature:
            return combined
        lowered = combined.lower()
        starts = [
            lowered.find(needle.lower())
            for needle in signature
            if needle.lower() in lowered
        ]
        if not starts:
            return combined
        return combined[: min(starts)]

    def _validate_result(self, payload: bytes, return_code: int,
                         stderr_path: Path) -> None:
        combined = ""
        try:
            combined = payload.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            pass
        try:
            combined += stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        lowered = combined.lower()
        dump_present = self._help_dump_present(lowered)
        scan = (
            self._testimony_outside_help_dump(combined).lower()
            if dump_present
            else lowered
        )
        if return_code != 0:
            for marker in _PERMISSION_MARKERS:
                if marker in scan:
                    raise WorkerAdapterFailure("permission_required")
            raise WorkerAdapterFailure(
                "process_failed", help_dump_present=dump_present
            )
        if any(marker in scan for marker in _PERMISSION_MARKERS):
            raise WorkerAdapterFailure("permission_required")

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        try:
            if self.isolate_process_group:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _close_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                if self.isolate_process_group:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
