"""Photograph a hung wake-adapter child before kill. Sidecar is keyed by attempt id."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Mapping, Optional, Sequence


_SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SAMPLE = "/usr/bin/sample"
_LSOF = "/usr/sbin/lsof"


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
    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            key = attempt_key or "unkeyed"
            if sidecar_path is not None and process.pid:
                row = photograph_hung_child(pid=process.pid, argv=argv, attempt_key=key)
                write_sidecar(sidecar_path, row)
        except Exception:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass
        try:
            process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        raise
    return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)
