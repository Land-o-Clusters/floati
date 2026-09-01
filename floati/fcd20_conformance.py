"""Portable typed result core for the FCD 20 C1-C9 instrument."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import resource
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Mapping, Optional, Sequence, Tuple

from .errors import ProtocolRefusal
from .fleet_update import _explicit_executable
from . import host_paths
from .adapters.codex_live import _open_private_file, _secure_directory
from .workers import WorkerAdapterFailure


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROW_STATUSES = frozenset({"measured", "host_condition", "probe_failed"})
_NON_LINUX_CANNOT_SEE = "linux_measurements_from_a_non_linux_host"
_ALWAYS_CANNOT_SEE = (
    "provider_turn_or_authentication",
    "controlled_load_performance",
    "harnesses_outside_c1_c9",
)
_DEFAULT_PROBE_TIMEOUT = 5.0
_DEFAULT_OUTPUT_LIMIT = 65_536
_MAX_VERSION_BYTES = 512


@dataclass(frozen=True)
class RowSpec:
    row: str
    harness: str


@dataclass(frozen=True)
class ExecutableResolution:
    executable: Optional[Path]
    candidates: Tuple[Path, ...]


@dataclass(frozen=True)
class ProbeResult:
    exit_code: int
    timed_out: bool
    stdout_size: int
    stdout_sha256: str
    stderr_size: int
    stderr_sha256: str
    duration_ms: int
    version: Optional[str]


ROWS = tuple(
    RowSpec(f"C{index}", harness)
    for index, harness in enumerate(
        (
            "codex",
            "claude",
            "opencode",
            "cursor",
            "cline",
            "grok-build",
            "pi",
            "herdr",
            "t3",
        ),
        start=1,
    )
)


def _refuse(detail: str) -> None:
    raise ProtocolRefusal("fcd20_artifact_invalid", detail)


def _host_evidence() -> dict[str, object]:
    return {
        "platform": sys.platform,
        "machine": platform.machine(),
        "python_version": platform.python_version(),
    }


def _cannot_see(host: Mapping[str, object]) -> list[str]:
    cannot_see = list(_ALWAYS_CANNOT_SEE)
    if host.get("platform") != "linux":
        cannot_see.insert(0, _NON_LINUX_CANNOT_SEE)
    return cannot_see


def _declaration_flag(spec: RowSpec) -> str:
    return f"--{spec.harness}-executable"


def _declaration_code(spec: RowSpec, suffix: str) -> str:
    harness = spec.harness.replace("-", "_")
    return f"fcd20_{harness}_executable_{suffix}"


def _declaration_remedy(spec: RowSpec) -> str:
    return (
        f"pass {_declaration_flag(spec)} with one absolute canonical "
        "executable path"
    )


def validate_declarations(values: Mapping[str, object]) -> dict[str, Path]:
    """Validate only operator-declared C1-C9 executables; never search PATH."""

    if not isinstance(values, Mapping):
        raise ProtocolRefusal(
            "fcd20_declarations_invalid",
            "harness declarations must be one mapping",
        )
    specs = {spec.harness: spec for spec in ROWS}
    unknown = set(values) - set(specs)
    if unknown:
        raise ProtocolRefusal(
            "fcd20_declarations_invalid",
            "harness declarations contain an unknown row",
        )
    declarations: dict[str, Path] = {}
    for harness, value in values.items():
        if value is None:
            continue
        spec = specs[harness]
        code = _declaration_code(spec, "invalid")
        try:
            selected = _explicit_executable(value, code)
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                exc.code,
                exc.detail,
                remedy=_declaration_remedy(spec),
            ) from exc
        declarations[harness] = Path(selected)
    return declarations


def resolve_declared_executable(
    spec: RowSpec, declarations: Mapping[str, Path]
) -> ExecutableResolution:
    """Project one operator declaration into a candidate set of zero or one."""

    selected = declarations.get(spec.harness)
    if selected is None:
        return ExecutableResolution(None, ())
    return ExecutableResolution(selected, (selected,))


def _prepare_scratch_parent() -> Path:
    parent = host_paths.fcd20_scratch_parent()
    try:
        try:
            parent.lstat()
        except FileNotFoundError:
            _secure_directory(parent, create=True)
        else:
            _secure_directory(parent, create=False)
    except WorkerAdapterFailure as exc:
        raise ProtocolRefusal(
            "fcd20_probe_scratch_invalid",
            "derived FCD 20 scratch parent is not a private owned directory",
        ) from exc
    return parent


def _set_output_limit(limit: int) -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))


def _probe_environment() -> dict[str, str]:
    return {"LANG": "C", "LC_ALL": "C"}


def _version_field(stdout: bytes) -> Optional[str]:
    try:
        text = stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    lines = text.splitlines()
    if not lines:
        return None
    value = lines[0].strip()
    encoded = value.encode("utf-8")
    if (
        not value
        or len(encoded) > _MAX_VERSION_BYTES
        or any(not character.isprintable() for character in value)
    ):
        return None
    return value


def probe_version(
    spec: RowSpec,
    executable: Path,
    *,
    timeout: float = _DEFAULT_PROBE_TIMEOUT,
    output_limit: int = _DEFAULT_OUTPUT_LIMIT,
) -> ProbeResult:
    """Run one finite local version probe with bounded evidence capture."""

    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not 0.01 <= float(timeout) <= 60.0
        or not isinstance(output_limit, int)
        or isinstance(output_limit, bool)
        or not 128 <= output_limit <= 1_048_576
    ):
        raise ProtocolRefusal(
            "fcd20_probe_bounds_invalid",
            "probe timeout or output limit is outside the fixed bounds",
        )
    validated = Path(
        _explicit_executable(executable, "fcd20_executable_invalid")
    )
    parent = _prepare_scratch_parent()
    started = time.monotonic_ns()
    timed_out = False
    with TemporaryDirectory(prefix=f"{spec.row.lower()}-", dir=parent) as temporary:
        scratch = Path(temporary)
        stdout_path = scratch / "stdout.bin"
        stderr_path = scratch / "stderr.bin"
        stdout_file = _open_private_file(stdout_path)
        try:
            stderr_file = _open_private_file(stderr_path)
        except Exception:
            stdout_file.close()
            raise
        process: Optional[subprocess.Popen[bytes]] = None
        try:
            process = subprocess.Popen(
                (str(validated), "--version"),
                cwd=scratch,
                env=_probe_environment(),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
                preexec_fn=lambda: _set_output_limit(output_limit),
            )
            try:
                exit_code = process.wait(timeout=float(timeout))
            except subprocess.TimeoutExpired:
                timed_out = True
                process.terminate()
                try:
                    exit_code = process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    exit_code = process.wait(timeout=1.0)
        except OSError as exc:
            raise ProtocolRefusal(
                "fcd20_probe_start_failed",
                "version probe process could not be started",
            ) from exc
        finally:
            stdout_file.close()
            stderr_file.close()
        stdout = stdout_path.read_bytes()
        stderr = stderr_path.read_bytes()
        if len(stdout) >= output_limit or len(stderr) >= output_limit:
            raise ProtocolRefusal(
                "fcd20_probe_output_overflow",
                "version probe output reached its fixed byte ceiling",
            )
        elapsed_ns = time.monotonic_ns() - started
        return ProbeResult(
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_size=len(stdout),
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            stderr_size=len(stderr),
            stderr_sha256=hashlib.sha256(stderr).hexdigest(),
            duration_ms=max(0, (elapsed_ns + 999_999) // 1_000_000),
            version=_version_field(stdout),
        )


def _validate_resolution(
    value: object, spec: RowSpec
) -> ExecutableResolution:
    if not isinstance(value, ExecutableResolution):
        _refuse(f"{spec.row} executable resolution has an invalid type")
    if not isinstance(value.candidates, tuple):
        _refuse(f"{spec.row} executable candidates are not a tuple")
    for candidate in value.candidates:
        if not isinstance(candidate, Path) or not candidate.is_absolute():
            _refuse(f"{spec.row} executable candidate is not an absolute path")
    if value.executable is not None:
        if (
            not isinstance(value.executable, Path)
            or not value.executable.is_absolute()
            or value.executable not in value.candidates
        ):
            _refuse(f"{spec.row} selected executable is outside its candidates")
    return value


def _validate_probe(value: object, spec: RowSpec) -> ProbeResult:
    if not isinstance(value, ProbeResult):
        _refuse(f"{spec.row} probe result has an invalid type")
    if (
        not isinstance(value.exit_code, int)
        or isinstance(value.exit_code, bool)
        or not isinstance(value.timed_out, bool)
        or not isinstance(value.stdout_size, int)
        or isinstance(value.stdout_size, bool)
        or value.stdout_size < 0
        or not isinstance(value.stderr_size, int)
        or isinstance(value.stderr_size, bool)
        or value.stderr_size < 0
        or not isinstance(value.duration_ms, int)
        or isinstance(value.duration_ms, bool)
        or value.duration_ms < 0
        or not isinstance(value.stdout_sha256, str)
        or _SHA256.fullmatch(value.stdout_sha256) is None
        or not isinstance(value.stderr_sha256, str)
        or _SHA256.fullmatch(value.stderr_sha256) is None
        or (
            value.version is not None
            and (not isinstance(value.version, str) or not value.version)
        )
    ):
        _refuse(f"{spec.row} probe result fields are invalid")
    return value


def _host_condition(
    spec: RowSpec, resolution: ExecutableResolution
) -> dict[str, object]:
    return {
        "row": spec.row,
        "harness": spec.harness,
        "status": "host_condition",
        "evidence": {
            "code": _declaration_code(spec, "undeclared"),
            "detail": (
                f"the operator did not declare an executable for {spec.harness}"
            ),
            "paths": [str(path) for path in resolution.candidates],
            "remedy": _declaration_remedy(spec),
        },
    }


def _probe_failed(
    spec: RowSpec, path: Optional[Path], code: str, detail: str
) -> dict[str, object]:
    return {
        "row": spec.row,
        "harness": spec.harness,
        "status": "probe_failed",
        "evidence": {
            "code": code,
            "detail": detail,
            "path": None if path is None else str(path),
        },
    }


def _measured(
    spec: RowSpec, path: Path, result: ProbeResult
) -> dict[str, object]:
    evidence = asdict(result)
    evidence["path"] = str(path)
    return {
        "row": spec.row,
        "harness": spec.harness,
        "status": "measured",
        "evidence": evidence,
    }


def run(
    *,
    resolve: Optional[Callable[[RowSpec], ExecutableResolution]] = None,
    probe: Optional[Callable[[RowSpec, Path], ProbeResult]] = None,
    host: Callable[[], Mapping[str, object]] = _host_evidence,
    declarations: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    """Run all nine specifications and preserve one typed row per result."""

    if resolve is None:
        validated_declarations = validate_declarations(
            {} if declarations is None else declarations
        )
        selected_resolver = lambda spec: resolve_declared_executable(
            spec, validated_declarations
        )
    else:
        selected_resolver = resolve
    selected_probe = probe_version if probe is None else probe
    rows = []
    for spec in ROWS:
        try:
            resolution = _validate_resolution(selected_resolver(spec), spec)
        except ProtocolRefusal as exc:
            rows.append(
                _probe_failed(spec, None, exc.code, "executable resolution evidence is invalid")
            )
            continue
        except Exception as exc:
            rows.append(
                _probe_failed(
                    spec,
                    None,
                    "fcd20_probe_internal_failure",
                    f"executable resolver raised {type(exc).__name__}",
                )
            )
            continue

        selected = resolution.executable
        if selected is None:
            rows.append(_host_condition(spec, resolution))
            continue
        try:
            result = _validate_probe(selected_probe(spec, selected), spec)
        except ProtocolRefusal as exc:
            rows.append(
                _probe_failed(spec, selected, exc.code, "probe evidence is invalid")
            )
            continue
        except Exception as exc:
            rows.append(
                _probe_failed(
                    spec,
                    selected,
                    "fcd20_probe_internal_failure",
                    f"probe raised {type(exc).__name__}",
                )
            )
            continue
        if result.timed_out:
            rows.append(
                _probe_failed(
                    spec,
                    selected,
                    "fcd20_probe_timeout",
                    "the bounded executable probe reached its deadline",
                )
            )
            continue
        rows.append(_measured(spec, selected, result))

    statuses = {str(row["status"]) for row in rows}
    if "probe_failed" in statuses:
        status = "probe_failed"
    elif "host_condition" in statuses:
        status = "degraded"
    else:
        status = "ok"
    host_evidence = dict(host())
    artifact = {
        "artifact_version": 0,
        "command": "fcd20-conformance",
        "status": status,
        "evidence": {
            "host": host_evidence,
            "rows": rows,
            "cannot_see": _cannot_see(host_evidence),
        },
    }
    validate_artifact(artifact)
    return artifact


def _contains_skip(value: object) -> bool:
    if isinstance(value, dict):
        return "skip" in value or any(_contains_skip(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_skip(item) for item in value)
    return False


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _absolute_path_text(value: object) -> bool:
    return isinstance(value, str) and Path(value).is_absolute()


def _valid_measured_evidence(evidence: Mapping[str, object]) -> bool:
    version = evidence.get("version")
    return (
        isinstance(evidence.get("exit_code"), int)
        and not isinstance(evidence.get("exit_code"), bool)
        and evidence.get("timed_out") is False
        and isinstance(evidence.get("stdout_size"), int)
        and not isinstance(evidence.get("stdout_size"), bool)
        and 0 <= evidence["stdout_size"] <= 1_048_576
        and isinstance(evidence.get("stderr_size"), int)
        and not isinstance(evidence.get("stderr_size"), bool)
        and 0 <= evidence["stderr_size"] <= 1_048_576
        and isinstance(evidence.get("duration_ms"), int)
        and not isinstance(evidence.get("duration_ms"), bool)
        and evidence["duration_ms"] >= 0
        and isinstance(evidence.get("stdout_sha256"), str)
        and _SHA256.fullmatch(evidence["stdout_sha256"]) is not None
        and isinstance(evidence.get("stderr_sha256"), str)
        and _SHA256.fullmatch(evidence["stderr_sha256"]) is not None
        and (
            version is None
            or (
                _nonempty_text(version)
                and len(version.encode("utf-8")) <= _MAX_VERSION_BYTES
                and all(character.isprintable() for character in version)
            )
        )
        and _absolute_path_text(evidence.get("path"))
    )


def validate_artifact(artifact: object) -> None:
    """Fail closed when the instrument's own aggregate is inconsistent."""

    if not isinstance(artifact, dict) or set(artifact) != {
        "artifact_version",
        "command",
        "status",
        "evidence",
    }:
        _refuse("artifact fields are invalid")
    if (
        artifact.get("artifact_version") != 0
        or artifact.get("command") != "fcd20-conformance"
        or artifact.get("status") not in {"ok", "degraded", "probe_failed"}
        or _contains_skip(artifact)
    ):
        _refuse("artifact header is invalid")
    evidence = artifact.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {
        "host",
        "rows",
        "cannot_see",
    }:
        _refuse("artifact evidence fields are invalid")
    host = evidence.get("host")
    if (
        not isinstance(host, dict)
        or set(host) != {"platform", "machine", "python_version"}
        or any(not _nonempty_text(value) for value in host.values())
    ):
        _refuse("artifact host evidence is invalid")
    cannot_see = evidence.get("cannot_see")
    expected_cannot_see = _cannot_see(host)
    if (
        not isinstance(cannot_see, list)
        or len(cannot_see) != len(expected_cannot_see)
        or cannot_see != expected_cannot_see
    ):
        _refuse("artifact cannot-see evidence is invalid")
    rows = evidence.get("rows")
    if not isinstance(rows, list) or len(rows) != len(ROWS):
        _refuse("artifact rows are invalid")
    expected = [(spec.row, spec.harness) for spec in ROWS]
    actual = []
    statuses = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "row",
            "harness",
            "status",
            "evidence",
        }:
            _refuse("artifact row fields are invalid")
        status = row.get("status")
        if status not in _ROW_STATUSES or not isinstance(row.get("evidence"), dict):
            _refuse("artifact row status is invalid")
        actual.append((row.get("row"), row.get("harness")))
        statuses.append(status)
        row_evidence = row["evidence"]
        if status == "host_condition":
            if set(row_evidence) != {"code", "detail", "paths", "remedy"}:
                _refuse("host-condition evidence fields are invalid")
            paths = row_evidence.get("paths")
            if (
                row_evidence.get("code")
                != _declaration_code(ROWS[len(actual) - 1], "undeclared")
                or not _nonempty_text(row_evidence.get("detail"))
                or row_evidence.get("remedy")
                != _declaration_remedy(ROWS[len(actual) - 1])
                or paths != []
            ):
                _refuse("host-condition evidence values are invalid")
        elif status == "probe_failed":
            if (
                set(row_evidence) != {"code", "detail", "path"}
                or not _nonempty_text(row_evidence.get("code"))
                or not _nonempty_text(row_evidence.get("detail"))
                or (
                    row_evidence.get("path") is not None
                    and not _absolute_path_text(row_evidence.get("path"))
                )
            ):
                _refuse("probe-failed evidence fields are invalid")
        elif (
            set(row_evidence)
            != {
                "exit_code",
                "timed_out",
                "stdout_size",
                "stdout_sha256",
                "stderr_size",
                "stderr_sha256",
                "duration_ms",
                "version",
                "path",
            }
            or not _valid_measured_evidence(row_evidence)
        ):
            _refuse("measured evidence fields are invalid")
    if actual != expected:
        _refuse("artifact rows are missing, duplicated, or out of order")
    expected_status = (
        "probe_failed"
        if "probe_failed" in statuses
        else "degraded"
        if "host_condition" in statuses
        else "ok"
    )
    if artifact.get("status") != expected_status:
        _refuse("aggregate status does not match its rows")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ProtocolRefusal("arguments_invalid", message)


def _artifact_exit_code(artifact: Mapping[str, object]) -> int:
    if artifact["status"] == "probe_failed":
        return 30
    evidence = artifact["evidence"]
    if not any(row["status"] == "measured" for row in evidence["rows"]):
        return 32
    return 0


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    resolve: Optional[Callable[[RowSpec], ExecutableResolution]] = None,
    probe: Optional[Callable[[RowSpec, Path], ProbeResult]] = None,
    host: Callable[[], Mapping[str, object]] = _host_evidence,
) -> int:
    """Emit exactly one C1-C9 JSON artifact for the current host."""

    parser = _Parser(
        prog="python3 -m floati.fcd20_conformance",
        add_help=False,
    )
    for spec in ROWS:
        parser.add_argument(_declaration_flag(spec))
    try:
        args = parser.parse_args(argv)
        declared = {
            spec.harness: getattr(
                args, f"{spec.harness.replace('-', '_')}_executable"
            )
            for spec in ROWS
            if getattr(
                args, f"{spec.harness.replace('-', '_')}_executable"
            )
            is not None
        }
        artifact = run(
            resolve=resolve,
            probe=probe,
            host=host,
            declarations=declared,
        )
    except ProtocolRefusal as exc:
        refusal_evidence = {"code": exc.code, "detail": exc.detail}
        if exc.remedy is not None:
            refusal_evidence["remedy"] = exc.remedy
        artifact = {
            "artifact_version": 0,
            "command": "fcd20-conformance",
            "status": "refused",
            "evidence": refusal_evidence,
        }
        print(json.dumps(artifact, sort_keys=True, separators=(",", ":")))
        return 20
    print(json.dumps(artifact, sort_keys=True, separators=(",", ":")))
    return _artifact_exit_code(artifact)


if __name__ == "__main__":
    sys.exit(main())
