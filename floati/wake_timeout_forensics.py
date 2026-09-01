"""Photograph a capped wake child without making its continued output a pipe."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Mapping, Optional, Sequence


_SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SAMPLE = "/usr/bin/sample"
_LSOF = "/usr/sbin/lsof"


def _reap_abandoned(process: subprocess.Popen[str], attempt_directory: Path) -> None:
    """One daemon reaper owns the capped child until wait() and file cleanup finish."""

    try:
        process.wait()
    finally:
        shutil.rmtree(attempt_directory, ignore_errors=True)


def forensics_relative_path(attempt_key: str) -> Path:
    if not isinstance(attempt_key, str) or _SAFE_KEY.fullmatch(attempt_key) is None:
        raise ValueError("timeout forensics key must be a filesystem-safe attempt id")
    return Path("receipts/wake-timeout-forensics") / f"{attempt_key}.json"


def _bounded_cmd(argv: Sequence[str], timeout: int) -> tuple[str, str]:
    if not argv or not Path(argv[0]).is_file():
        return "unavailable", ""
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "failed", ""
    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    if len(stdout) > 256_000:
        stdout = stdout[:256_000]
    return ("ok" if completed.returncode == 0 else "failed"), stdout


def photograph_hung_child(*, pid: int, argv: Sequence[str], attempt_key: str) -> dict[str, object]:
    sample_status, sample_stdout = _bounded_cmd((_SAMPLE, str(pid), "2"), timeout=15)
    lsof_bin = _LSOF if Path(_LSOF).is_file() else (shutil.which("lsof") or "")
    lsof_status, lsof_stdout = _bounded_cmd(
        (lsof_bin, "-p", str(pid)) if lsof_bin else (),
        timeout=5,
    )
    blob = f"{sample_stdout}\n{lsof_stdout}".lower()
    blocked_call = None
    if "pipe" in blob or "fifo" in blob:
        blocked_call = "read" if ("read" in blob or re.search(r"\bpipe\b", blob)) else "pipe"
    elif re.search(r"\bread\b", blob):
        blocked_call = "read"
    return {
        "attempt_key": attempt_key,
        "pid": pid,
        "argv": [str(part) for part in argv],
        "sample_status": sample_status,
        "sample_stdout": sample_stdout,
        "lsof_status": lsof_status,
        "lsof_stdout": lsof_stdout,
        "blocked_call": blocked_call,
    }


def write_sidecar(destination: Path, row: Mapping[str, object]) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(dict(row), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def run_with_timeout_forensics(
    argv: Sequence[str],
    cwd: Path,
    timeout: int,
    *,
    sidecar_path: Optional[Path] = None,
    attempt_key: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    key = attempt_key if isinstance(attempt_key, str) and _SAFE_KEY.fullmatch(attempt_key) else "unkeyed"
    attempt_directory = Path(
        tempfile.mkdtemp(prefix=f".floati-wake-{key}-", dir=str(cwd))
    )
    stdout_path = attempt_directory / "stdout"
    stderr_path = attempt_directory / "stderr"
    stdout_sink = stdout_path.open("w", encoding="utf-8")
    stderr_sink = stderr_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            stdout=stdout_sink,
            stderr=stderr_sink,
            text=True,
            start_new_session=True,
        )
    except Exception:
        stdout_sink.close()
        stderr_sink.close()
        shutil.rmtree(attempt_directory, ignore_errors=True)
        raise
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        try:
            if sidecar_path is not None and process.pid:
                row = photograph_hung_child(pid=process.pid, argv=argv, attempt_key=key)
                write_sidecar(sidecar_path, row)
        except Exception:
            pass
        stdout_sink.close()
        stderr_sink.close()
        threading.Thread(
            target=_reap_abandoned,
            args=(process, attempt_directory),
            name=f"floati-wake-reaper-{process.pid}",
            daemon=True,
        ).start()
        raise error
    stdout_sink.close()
    stderr_sink.close()
    try:
        stdout = stdout_path.read_text(encoding="utf-8")
        stderr = stderr_path.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(attempt_directory, ignore_errors=True)
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)
