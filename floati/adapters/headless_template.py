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

import ctypes
import json
import os
import select
import signal
import stat
import subprocess
import sys
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
    AppServerSession,
    CodexAppServerAdapter,
)

MAX_PROFILE_OUTPUT_BYTES = 1024 * 1024
_PERMISSION_MARKERS = (
    "approval", "permission", "requires user", "not allowed",
)


class _BsdShortInfo(ctypes.Structure):
    # sys/proc_info.h: proc_bsdshortinfo (64 bytes, no pointer-sized fields).
    _fields_ = [(name, ctypes.c_uint32) for name in ("pid", "ppid", "pgid", "status")]
    _fields_ += [("comm", ctypes.c_char * 16)]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        "flags", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]


def _darwin_group_terminal(process_group: int, *, allow_absent: bool) -> bool:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.c_int]
    library.proc_listpids.restype = ctypes.c_int
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                    ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int

    def members():
        values = (ctypes.c_int * 4096)()
        ctypes.set_errno(0)
        size = library.proc_listpids(2, process_group, values, ctypes.sizeof(values))
        # PROC_PGRP_ONLY includes live processes AND zombies. A full buffer
        # cannot prove completeness; zero plus an errno cannot prove absence.
        if (size < 0 or size >= ctypes.sizeof(values) or size % ctypes.sizeof(ctypes.c_int)
                or (size == 0 and ctypes.get_errno())):
            raise OSError("process group membership unreadable or truncated")
        result = list(values[:size // ctypes.sizeof(ctypes.c_int)])
        if any(pid <= 0 for pid in result) or len(set(result)) != len(result):
            raise OSError("process group membership malformed")
        return set(result)

    before = members()
    for pid in before:
        info = _BsdShortInfo()
        # SHORTBSDINFO does not require same-user credentials; arg=1 includes
        # zombies. A credential transition therefore cannot masquerade as exit.
        size = library.proc_pidinfo(pid, 13, 1, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info) or info.pid != pid or info.pgid != process_group:
            raise OSError("process group member state unproved")
        if info.status != 5:  # SZOMB; exiting/runnable/stopped are still owned.
            return False
    # A live member can fork then exit during state reads. Require membership
    # to remain unchanged AFTER all sampled members have become terminal.
    return members() == before and (bool(before) or allow_absent)


def _linux_group_terminal(process_group: int, *, allow_absent: bool) -> bool:
    # A hidden procfs view cannot testify that no live foreign-UID member exists.
    # An ancestor-mounted procfs also reports PGIDs in the wrong PID namespace.
    # NStgid starts in the procfs mount's namespace and lists each inner one.
    # Use self-only testimony: PID 1's namespace link can require ptrace rights.
    namespace_rows = [line.split()[1:] for line in Path("/proc/self/status").read_text().splitlines()
                      if line.split() and line.split()[0] == "NStgid:"]
    if (len(namespace_rows) != 1 or len(namespace_rows[0]) != 1
            or not namespace_rows[0][0].isdecimal() or int(namespace_rows[0][0]) != os.getpid()):
        raise OSError("process group requires its own PID namespace procfs")
    mounts = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    visible = False
    for fields in mounts:
        if len(fields) < 10 or fields[4] != "/proc":
            continue
        separator = fields.index("-")
        options = fields[5].split(",") + fields[separator + 3].split(",")
        visible = (fields[3] == "/" and fields[separator + 1] == "proc"
                   and all(not option.startswith("hidepid=") or option == "hidepid=0"
                           for option in options))
    if not visible:
        raise OSError("process group requires an unfiltered procfs view")

    def snapshot():
        result = {}
        entries = [entry for entry in Path("/proc").iterdir() if entry.name.isdecimal()]
        if len(entries) >= 65536:
            raise OSError("process group census exceeded bound")
        for entry in entries:
            try:
                raw = (entry / "stat").read_text()
            except (FileNotFoundError, ProcessLookupError):
                continue
            prefix, separator, suffix = raw.rpartition(")")
            fields = suffix.split()
            if (not separator or int(prefix.split("(", 1)[0]) != int(entry.name)
                    or len(fields) < 20):
                raise OSError("process group member stat malformed")
            if int(fields[2]) == process_group:
                result[int(entry.name)] = (fields[0], int(fields[19]))
        return result

    before = snapshot()
    if any(state != "Z" for state, _birth in before.values()):
        return False
    return snapshot() == before and (bool(before) or allow_absent)


def _group_terminal(process_group: int, *, allow_absent: bool) -> bool:
    try:
        if sys.platform == "darwin":
            return _darwin_group_terminal(process_group, allow_absent=allow_absent)
        if sys.platform == "linux":
            return _linux_group_terminal(process_group, allow_absent=allow_absent)
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    # Unknown platforms, unavailable APIs, partial reads and membership churn
    # retain ownership under the caller's existing typed process_timeout.
    return False


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
        self._stderr_capture = None
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
                env=self._launch_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                start_new_session=self.isolate_process_group,
            )
            self._active_process = process
            self._stderr_capture = stderr_file
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
            if (stderr_file is not None and not stderr_file.closed
                    and stderr_file is not self._stderr_capture):
                stderr_file.close()
        return ProfileHandle(
            str(item["id"]), workspace, filesystem_workspace,
            process, stderr_path, deadline, git_identity,
        )

    def drive(
        self,
        handle: object,
        item: Optional[Dict[str, object]] = None,
        *,
        deadline_seconds: float,
    ) -> Dict[str, object] | list[Dict[str, str]]:
        """Bind worker artifacts, or return the legacy no-item probe report."""
        try:
            if (
                not isinstance(handle, ProfileHandle)
                or (item is not None and (
                    handle.work_id != item.get("id")
                    or handle.workspace != self._workspace(item)
                ))
            ):
                raise WorkerAdapterFailure("protocol_error")
            deadline = min(
                handle.deadline,
                time.monotonic() + min(float(deadline_seconds), 3600.0),
            )
            output = self._read_bounded(handle.process, deadline)
            return_code = self._wait(handle.process, deadline)
            self._validate_result(output, return_code, handle.stderr_path)
            if item is not None:
                # The worker consumes committed file coordinates, not the
                # harness's diagnostic stdout. Exclude evidence as usual.
                self.cancel()
                return self._finalize(handle)
            try:
                payload = json.loads(output.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {"raw": output.decode("utf-8", errors="replace")}
            return {
                "work_id": handle.work_id,
                "payload": payload,
                "return_code": return_code,
            }
        finally:
            try:
                self.cancel()
            finally:
                self._restore_prepared_workspace()

    def _launch_environment(self) -> Optional[Dict[str, str]]:
        """Inherit the environment unless a harness declares a launch seam."""
        return None

    def _prompt_arguments(self, item: Dict[str, object]) -> Sequence[str]:
        """The trailing prompt invocation: the profile's prompt form, then
        the work title. Default ("--",) reproduces the original inline
        argv exactly for every unmeasured member."""
        return (*self.profile.prompt_form, str(item.get("title", "")))

    def cancel(self) -> None:
        process = self._active_process
        if process is None:
            return
        closed = False
        try:
            self._terminate(process)
            self._close_process(process)
            if self.isolate_process_group:
                AppServerSession._quiesce_process_group(process.pid)
                self._require_group_exit(process.pid)
            closed = True
        finally:
            for name in ("stdout", "stderr"):
                stream = getattr(process, name, None)
                if stream is not None:
                    stream.close()
            if self._stderr_capture is not None:
                self._stderr_capture.close()
                self._stderr_capture = None
            # A reaped leader does not prove that its descendants are gone.
            if closed:
                self._active_process = None

    @staticmethod
    def _require_group_exit(process_group: int) -> None:
        """Prove no executable group member remains after the final group KILL."""
        deadline = time.monotonic() + 0.5
        while True:
            denied = False
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                # Darwin skips zombies and returns EPERM when no signalable
                # member remains. EPERM can ALSO mean a genuinely live member.
                denied = True
            except OSError as exc:
                raise WorkerAdapterFailure("process_timeout") from exc
            if _group_terminal(process_group, allow_absent=denied):
                return
            if time.monotonic() >= deadline:
                raise WorkerAdapterFailure("process_timeout")
            time.sleep(0.01)

    def _read_bounded(self, process: subprocess.Popen[bytes],
                      deadline: float) -> bytes:
        collected = bytearray()
        streams = {stream.fileno(): name for name in ("stdout", "stderr")
                   if (stream := getattr(process, name, None)) is not None}
        counts = {"stdout": 0, "stderr": 0}
        try:
            while streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerAdapterFailure("deadline_exceeded")
                try:
                    ready, _, _ = select.select(list(streams), [], [], remaining)
                except InterruptedError:
                    continue
                if not ready:
                    raise WorkerAdapterFailure("deadline_exceeded")
                for descriptor in ready:
                    try:
                        piece = os.read(descriptor, 8192)
                    except InterruptedError:
                        continue
                    if not piece:
                        del streams[descriptor]
                        continue
                    name = streams[descriptor]
                    available = max(0, MAX_PROFILE_OUTPUT_BYTES - counts[name])
                    counts[name] += len(piece)
                    # Retain at most the declared limit even on the refusing
                    # chunk. Arbitrary diagnostic stdout remains a valid probe.
                    if name == "stdout":
                        collected.extend(piece[:available])
                    elif self._stderr_capture is not None:
                        self._stderr_capture.buffer.write(piece[:available])
                    if counts[name] > MAX_PROFILE_OUTPUT_BYTES:
                        raise WorkerAdapterFailure("output_limit_exceeded")
        except (OSError, ValueError) as exc:
            raise WorkerAdapterFailure("process_failed") from exc
        finally:
            if self._stderr_capture is not None:
                self._stderr_capture.flush()
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
            flags = os.O_RDONLY | os.O_NONBLOCK
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(stderr_path, flags)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise WorkerAdapterFailure("workspace_invalid")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    diagnostic = stream.read(MAX_PROFILE_OUTPUT_BYTES + 1)
            finally:
                os.close(descriptor)
            if len(diagnostic) > MAX_PROFILE_OUTPUT_BYTES:
                raise WorkerAdapterFailure("output_limit_exceeded")
            combined += diagnostic.decode("utf-8", errors="replace")
        except OSError as exc:
            raise WorkerAdapterFailure("workspace_invalid") from exc
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
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired as exc:
                raise WorkerAdapterFailure("process_timeout") from exc
