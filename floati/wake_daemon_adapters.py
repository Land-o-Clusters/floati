"""Exact, digest-bound Codex, Cursor, and `grok-build` wake daemon adapters."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from .codex_wait_contract import CodexWaitParticipant
from .errors import IntegrityFailure, ProtocolRefusal
from .root import FloatiRoot
from .wake_control import validate_session_id
from .wake_daemon_contract import (
    AdapterBindingStore,
    DaemonCoordinate,
)
from .wake_timeout_forensics import (
    forensics_relative_path,
    run_with_timeout_forensics,
)


CODEX_EXECUTABLE = Path("/opt/homebrew/bin/codex")
# Measured zcode wake surface (banked on lane/zc1-zcode-kit; not a git merge).
# The entry script is the product bytes the binding digests; node is the
# fixed interpreter that runs it.
ZCODE_NODE = Path("/opt/homebrew/bin/node")
ZCODE_ENTRY_SCRIPT = Path(
    "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"
)
_ADAPTER_VERSIONS = {"codex": "1", "cursor": "1", "grok-build": "1", "zcode": "1"}
_ADAPTER_CONTRACTS = {
    "codex": "floati:wake-daemon:codex:v1:queue --thread SESSION --message REASON",
    "cursor": "floati:wake-daemon:cursor:v1:--print --output-format json --single-turn --resume SESSION REASON",
    "grok-build": "floati:wake-daemon:grok-build:v1:-p REASON --output-format json --resume SESSION",
    "zcode": "floati:wake-daemon:zcode:v1:node ENTRY --json --no-color --resume SESSION --prompt REASON",
}
# WD-R5a (Am.1 5fc3f7d): every adapter DECLARES its resume-probe class. Each
# declared wake shape above consumes a turn, so all three declare
# costs_one_turn today; a turn_free declaration arrives only when an adapter
# grows a read-only liveness primitive. ABSENT IS UNDECLARED AND REFUSES -
# never silently none.
_ADAPTER_RESUME_PROBES = {
    "codex": "costs_one_turn",
    "cursor": "costs_one_turn",
    "grok-build": "costs_one_turn",
    "zcode": "costs_one_turn",
}
# WD-R5b: the bind-time probe is ONE resume against the named session,
# bounded by the adapter deadline machinery's own cap - no new constant is
# invented for it.
PROBE_REASON = (
    "[floati] bind-time resume probe: reply briefly to prove this session can wake"
)
PROBE_DEADLINE_SECONDS = 300


def _codex_executable_absent() -> ProtocolRefusal:
    path = str(CODEX_EXECUTABLE)
    return ProtocolRefusal(
        "wake_daemon_codex_executable_absent",
        f"the fixed Codex queue executable is absent at {path}",
        remedy=f"restore the reviewed Codex queue executable at {path}",
    )


def resume_probe_class(harness: str) -> str:
    """Return the adapter's declared resume_probe class, refusing if undeclared."""

    declared = _ADAPTER_RESUME_PROBES.get(harness) if isinstance(harness, str) else None
    if declared not in ("turn_free", "costs_one_turn", "none"):
        raise ProtocolRefusal(
            "wake_daemon_resume_probe_undeclared",
            "adapter declares no resume_probe class (turn_free | costs_one_turn | none); "
            "an undeclared adapter cannot be bound",
        )
    return declared


@dataclass(frozen=True)
class AdapterBinding:
    schema_version: int
    tenant_id: str
    node_id: str
    harness: str
    coordinate_digest: str
    session_id: str
    session_digest: str
    workspace: Path
    executable: Path
    executable_digest: str
    adapter_version: str
    adapter_digest: str
    binding_epoch: int
    resume_state: Optional[str] = None

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "AdapterBinding":
        try:
            return cls(
                schema_version=int(record["schema_version"]),
                tenant_id=str(record["tenant_id"]),
                node_id=str(record["node_id"]),
                harness=str(record["harness"]),
                coordinate_digest=str(record["coordinate_digest"]),
                session_id=str(record["session_id"]),
                session_digest=str(record["session_digest"]),
                workspace=Path(str(record["workspace"])),
                executable=Path(str(record["executable"])),
                executable_digest=str(record["executable_digest"]),
                adapter_version=str(record["adapter_version"]),
                adapter_digest=str(record["adapter_digest"]),
                binding_epoch=int(record["binding_epoch"]),
                resume_state=(
                    str(record["resume_state"])
                    if record.get("resume_state") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityFailure(
                "wake_daemon_binding_invalid", "adapter binding could not be projected"
            ) from exc


@dataclass(frozen=True)
class WakeAdapterResult:
    outcome: str
    reason_code: Optional[str]
    exit_code: Optional[int]
    output_digest: Optional[str]


Runner = Callable[
    [tuple[str, ...], Path, int], subprocess.CompletedProcess[str]
]


def adapter_contract_digest(harness: str) -> str:
    try:
        contract = _ADAPTER_CONTRACTS[harness]
    except (KeyError, TypeError) as exc:
        raise ProtocolRefusal(
            "wake_daemon_harness_unsupported",
            "wake daemon v1 supports only codex, cursor, grok-build, or zcode",
        ) from exc
    return hashlib.sha256(contract.encode("utf-8")).hexdigest()


def record_codex_daemon_binding(
    participant: CodexWaitParticipant,
    session_id: str,
    *,
    binding_epoch: Optional[int] = None,
) -> Mapping[str, object]:
    """Publish Codex testimony only from the trusted waiter participation path."""

    if not isinstance(participant, CodexWaitParticipant):
        raise ProtocolRefusal(
            "wake_daemon_codex_participant_invalid",
            "Codex daemon binding requires trusted waiter participation",
        )
    session = validate_session_id(session_id)
    try:
        executable = CODEX_EXECUTABLE.resolve(strict=True)
    except OSError as exc:
        raise _codex_executable_absent() from exc
    coordinate = DaemonCoordinate(
        participant.root, participant.binding.node_id, "codex"
    )
    epoch = time.time_ns() if binding_epoch is None else binding_epoch
    return AdapterBindingStore(participant.root).write(
        coordinate,
        session_id=session,
        workspace=participant.binding.workspace,
        executable=executable,
        adapter_version=_ADAPTER_VERSIONS["codex"],
        adapter_digest=adapter_contract_digest("codex"),
        binding_epoch=epoch,
    )


def _default_runner(
    argv: tuple[str, ...], cwd: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    return run_with_timeout_forensics(argv, cwd, timeout)


def _reason(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ProtocolRefusal(
            "wake_daemon_reason_invalid", "wake reason is empty, oversized, or unsafe"
        )
    return value


def _deadline(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 300:
        raise ProtocolRefusal(
            "wake_daemon_deadline_invalid", "adapter deadline is outside its bounds"
        )
    return value


class _BoundWakeAdapter:
    harness = ""

    def __init__(self, coordinate: DaemonCoordinate, *, runner: Optional[Runner] = None) -> None:
        if coordinate.harness != self.harness:
            raise ProtocolRefusal(
                "wake_daemon_harness_mismatch", "adapter and coordinate harness disagree"
            )
        self.coordinate = coordinate
        self.store = AdapterBindingStore(coordinate.root)
        self._runner = _default_runner if runner is None else runner
        self._timeout_forensics_key: Optional[str] = None

    def arm_timeout_forensics(self, attempt_key: str) -> None:
        self._timeout_forensics_key = attempt_key

    def exact_binding(self) -> AdapterBinding:
        record = self.store.read(self.coordinate)
        binding = AdapterBinding.from_record(record)
        expected_digest = adapter_contract_digest(self.harness)
        if (
            binding.adapter_version != _ADAPTER_VERSIONS[self.harness]
            or binding.adapter_digest != expected_digest
        ):
            raise ProtocolRefusal(
                "wake_daemon_adapter_digest_mismatch",
                "adapter binding does not name the installed v1 contract",
            )
        return binding

    def enumerate_bound_sessions(self) -> tuple[AdapterBinding, ...]:
        try:
            return (self.exact_binding(),)
        except (ProtocolRefusal, IntegrityFailure):
            return ()

    def observe_session(self, binding: object) -> str:
        self._require_current(binding)
        return "unknown"

    def _require_current(self, binding: object) -> AdapterBinding:
        current = self.exact_binding()
        supplied = (
            binding
            if isinstance(binding, AdapterBinding)
            else AdapterBinding.from_record(binding)
            if isinstance(binding, Mapping)
            else None
        )
        if supplied is None or supplied != current:
            raise ProtocolRefusal(
                "wake_daemon_session_mismatch",
                "wake request does not match the current exact session binding",
            )
        return current

    def _run(
        self, argv: tuple[str, ...], workspace: Path, deadline: int, session_id: str
    ) -> WakeAdapterResult:
        sidecar_path = None
        attempt_key = self._timeout_forensics_key
        if self._runner is _default_runner and isinstance(attempt_key, str):
            try:
                sidecar_path = self.coordinate.root.resolve_relative(
                    forensics_relative_path(attempt_key)
                )
            except (ValueError, OSError):
                sidecar_path = None
        try:
            if self._runner is _default_runner:
                result = run_with_timeout_forensics(
                    argv,
                    workspace,
                    deadline,
                    sidecar_path=sidecar_path,
                    attempt_key=attempt_key,
                )
            else:
                result = self._runner(argv, workspace, deadline)
        except subprocess.TimeoutExpired:
            return WakeAdapterResult("unknown", "wake_daemon_adapter_timeout", None, None)
        except OSError:
            return WakeAdapterResult("unknown", "wake_daemon_adapter_unavailable", None, None)
        output = result.stdout if isinstance(result.stdout, str) else ""
        digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
        if result.returncode != 0:
            return WakeAdapterResult(
                "refused", "wake_daemon_adapter_nonzero", result.returncode, digest
            )
        return self._successful_result(result.returncode, output, digest, session_id)

    def _successful_result(
        self,
        exit_code: int,
        output: str,
        digest: str,
        session_id: str,
    ) -> WakeAdapterResult:
        raise NotImplementedError

    def resume_argv(
        self, executable: Path, session_id: str, reason: str
    ) -> tuple[str, ...]:
        raise ProtocolRefusal(
            "wake_daemon_probe_unavailable",
            "adapter declares no resume shape the bind-time probe can drive",
        )

    def probe_resume(
        self,
        executable: Path,
        workspace: Path,
        session_id: str,
        reason: str,
        deadline: int,
    ) -> WakeAdapterResult:
        """WD-R5b: one bounded resume against the named session, judged by
        this adapter's own result validation - no persisted binding needed."""
        return self._run(
            self.resume_argv(executable, session_id, reason), workspace, deadline, session_id
        )


class CodexQueueWakeAdapter(_BoundWakeAdapter):
    harness = "codex"

    def request_wake(
        self, binding: object, reason: str, deadline_seconds: int
    ) -> WakeAdapterResult:
        current = self._require_current(binding)
        try:
            executable = CODEX_EXECUTABLE.resolve(strict=True)
        except OSError as exc:
            raise _codex_executable_absent() from exc
        if executable != current.executable:
            raise ProtocolRefusal(
                "wake_daemon_executable_digest_mismatch",
                "the fixed Codex executable no longer matches the binding",
            )
        wake_reason = _reason(reason)
        deadline = _deadline(deadline_seconds)
        argv = (
            str(CODEX_EXECUTABLE),
            "queue",
            "--thread",
            current.session_id,
            "--message",
            wake_reason,
        )
        return self._run(argv, current.workspace, deadline, current.session_id)

    def _successful_result(
        self,
        exit_code: int,
        output: str,
        digest: str,
        session_id: str,
    ) -> WakeAdapterResult:
        return WakeAdapterResult("woke", None, exit_code, digest)


class CursorResumeWakeAdapter(_BoundWakeAdapter):
    harness = "cursor"

    @staticmethod
    def resume_argv(
        executable: Path, session_id: str, reason: str
    ) -> tuple[str, ...]:
        return (
            str(executable),
            "--print",
            "--output-format",
            "json",
            "--single-turn",
            "--resume",
            session_id,
            reason,
        )

    def request_wake(
        self, binding: object, reason: str, deadline_seconds: int
    ) -> WakeAdapterResult:
        current = self._require_current(binding)
        wake_reason = _reason(reason)
        deadline = _deadline(deadline_seconds)
        argv = self.resume_argv(current.executable, current.session_id, wake_reason)
        return self._run(argv, current.workspace, deadline, current.session_id)

    def _successful_result(
        self,
        exit_code: int,
        output: str,
        digest: str,
        session_id: str,
    ) -> WakeAdapterResult:
        if not output.strip():
            return WakeAdapterResult(
                "unknown", "wake_daemon_cursor_output_empty", exit_code, digest
            )
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return WakeAdapterResult(
                "unknown", "wake_daemon_cursor_output_invalid", exit_code, digest
            )
        if (
            not isinstance(parsed, dict)
            or parsed.get("type") != "result"
            or parsed.get("subtype") != "success"
            or parsed.get("is_error") is not False
            or parsed.get("session_id") != session_id
        ):
            return WakeAdapterResult(
                "unknown", "wake_daemon_cursor_result_invalid", exit_code, digest
            )
        return WakeAdapterResult("woke", None, exit_code, digest)


class GrokBuildResumeWakeAdapter(_BoundWakeAdapter):
    harness = "grok-build"

    @staticmethod
    def resume_argv(
        executable: Path, session_id: str, reason: str
    ) -> tuple[str, ...]:
        return (
            str(executable),
            "-p",
            reason,
            "--output-format",
            "json",
            "--resume",
            session_id,
        )

    def request_wake(
        self, binding: object, reason: str, deadline_seconds: int
    ) -> WakeAdapterResult:
        current = self._require_current(binding)
        wake_reason = _reason(reason)
        deadline = _deadline(deadline_seconds)
        argv = self.resume_argv(current.executable, current.session_id, wake_reason)
        return self._run(argv, current.workspace, deadline, current.session_id)

    def _successful_result(
        self,
        exit_code: int,
        output: str,
        digest: str,
        session_id: str,
    ) -> WakeAdapterResult:
        if not output.strip():
            return WakeAdapterResult(
                "unknown", "wake_daemon_grok_output_empty", exit_code, digest
            )
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return WakeAdapterResult(
                "unknown", "wake_daemon_grok_output_invalid", exit_code, digest
            )
        if (
            not isinstance(parsed, dict)
            or parsed.get("sessionId") != session_id
            or parsed.get("stopReason") != "end_turn"
        ):
            return WakeAdapterResult(
                "unknown", "wake_daemon_grok_result_invalid", exit_code, digest
            )
        return WakeAdapterResult("woke", None, exit_code, digest)


class ZcodeResumeWakeAdapter(_BoundWakeAdapter):
    """Wake zcode by resuming the bound session headless.

    Argv is the measured K4 shape: `--json --no-color --resume SESSION --prompt REASON`.
    Success requires the artifact `sessionId` to name the bound session and a
    non-empty `response`. Empty / invalid / mismatched output emit the three
    zcode reason codes the ledger must record as themselves.
    """

    harness = "zcode"

    @staticmethod
    def resume_argv(
        executable: Path, session_id: str, reason: str
    ) -> tuple[str, ...]:
        try:
            node = ZCODE_NODE.resolve(strict=True)
        except OSError as exc:
            raise ProtocolRefusal(
                "wake_daemon_zcode_node_absent",
                f"the fixed zcode node interpreter is absent at {ZCODE_NODE}",
                remedy=(
                    "restore the reviewed zcode node interpreter at "
                    f"{ZCODE_NODE}"
                ),
            ) from exc
        return (
            str(node),
            str(executable),
            "--json",
            "--no-color",
            "--resume",
            session_id,
            "--prompt",
            reason,
        )

    def request_wake(
        self, binding: object, reason: str, deadline_seconds: int
    ) -> WakeAdapterResult:
        current = self._require_current(binding)
        try:
            entry = ZCODE_ENTRY_SCRIPT.resolve(strict=True)
        except OSError as exc:
            raise ProtocolRefusal(
                "wake_daemon_zcode_entry_absent",
                "the fixed zcode entry script is absent",
            ) from exc
        if entry != current.executable:
            raise ProtocolRefusal(
                "wake_daemon_zcode_entry_mismatch",
                "the binding does not name the pinned zcode entry script",
            )
        wake_reason = _reason(reason)
        deadline = _deadline(deadline_seconds)
        argv = self.resume_argv(current.executable, current.session_id, wake_reason)
        return self._run(argv, current.workspace, deadline, current.session_id)

    def _successful_result(
        self,
        exit_code: int,
        output: str,
        digest: str,
        session_id: str,
    ) -> WakeAdapterResult:
        if not output.strip():
            return WakeAdapterResult(
                "unknown", "wake_daemon_zcode_output_empty", exit_code, digest
            )
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return WakeAdapterResult(
                "unknown", "wake_daemon_zcode_output_invalid", exit_code, digest
            )
        if (
            not isinstance(parsed, dict)
            or parsed.get("sessionId") != session_id
            or not isinstance(parsed.get("response"), str)
            or not parsed["response"]
        ):
            return WakeAdapterResult(
                "unknown", "wake_daemon_zcode_result_invalid", exit_code, digest
            )
        return WakeAdapterResult("woke", None, exit_code, digest)


def wake_adapter_for(
    root: FloatiRoot,
    node_id: str,
    harness: str,
    *,
    runner: Optional[Runner] = None,
) -> _BoundWakeAdapter:
    coordinate = DaemonCoordinate(root, node_id, harness)
    if harness == "codex":
        return CodexQueueWakeAdapter(coordinate, runner=runner)
    if harness == "cursor":
        return CursorResumeWakeAdapter(coordinate, runner=runner)
    if harness == "grok-build":
        return GrokBuildResumeWakeAdapter(coordinate, runner=runner)
    if harness == "zcode":
        return ZcodeResumeWakeAdapter(coordinate, runner=runner)
    raise ProtocolRefusal(
        "wake_daemon_harness_unsupported",
        "wake daemon v1 supports only codex, cursor, grok-build, or zcode",
    )
