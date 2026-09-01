"""Fixed one-shot GitHub CLI subprocess contract for issue intake."""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .copy import GH_AUTHENTICATION_REMEDY
from .errors import ProtocolRefusal
from .work import _now as _work_now


_FIXED_GH_ENVIRONMENT = {
    "GH_NO_UPDATE_NOTIFIER": "1",
    "GH_PAGER": "cat",
    "GH_PROMPT_DISABLED": "1",
    "HOME": "/var/empty",
    "LANG": "C",
    "LC_ALL": "C",
    "NO_COLOR": "1",
    "PAGER": "cat",
    "XDG_CONFIG_HOME": "/var/empty",
}
GH_ISSUE_FIELDS = (
    "number",
    "title",
    "body",
    "state",
    "labels",
    "author",
    "createdAt",
    "updatedAt",
    "url",
)
DEFAULT_GH_DEADLINE = 15.0
MAX_GH_DEADLINE = 60.0
MAX_GH_OUTPUT_BYTES = 1024 * 1024
_KNOWN_GITHUB_TOKEN = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{20,}"
)


def fixed_gh_environment(executable: str) -> dict[str, str]:
    """Return fixed GitHub CLI coordinates plus ambient token material only."""

    executable_path = Path(executable)
    search_directories = ["/usr/bin", "/bin"]
    if executable_path.is_absolute():
        search_directories.insert(0, str(executable_path.parent))
    environment = dict(_FIXED_GH_ENVIRONMENT)
    environment["PATH"] = os.pathsep.join(dict.fromkeys(search_directories))
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def fixed_gh_command(
    executable: str, owner: str, repo: str, number: int
) -> list[str]:
    """Bind one explicit executable to one exact issue-view invocation."""

    return [
        executable,
        "issue",
        "view",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        ",".join(GH_ISSUE_FIELDS),
    ]


def _validate_executable(executable: str) -> Path:
    path = Path(executable)
    if not path.is_absolute():
        raise ProtocolRefusal(
            "gh_executable_invalid",
            "GitHub CLI executable must be one explicit absolute executable regular file",
        )
    try:
        path.lstat()
    except FileNotFoundError as exc:
        raise ProtocolRefusal(
            "gh_executable_absent", f"GitHub CLI executable does not exist: {path}"
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise ProtocolRefusal(
            "gh_executable_invalid", f"GitHub CLI executable cannot be inspected: {path}"
        ) from exc
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProtocolRefusal(
            "gh_executable_invalid",
            "GitHub CLI executable must resolve to an executable regular file",
        ) from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ProtocolRefusal(
            "gh_executable_invalid",
            "GitHub CLI executable must resolve to an executable regular file",
        )
    return resolved


def _deadline(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= MAX_GH_DEADLINE
    ):
        raise ProtocolRefusal(
            "gh_deadline_invalid", f"GitHub CLI deadline must be in (0, {MAX_GH_DEADLINE}] seconds"
        )
    return float(value)


def _failure_detail(
    returncode: int, stderr: bytes, secret_values: tuple[str, ...]
) -> str:
    bounded = stderr[:512].decode("utf-8", errors="replace")
    redacted = bounded
    for secret in sorted(set(secret_values), key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    redacted = _KNOWN_GITHUB_TOKEN.sub("<redacted>", redacted)
    suffix = f": {redacted}" if redacted else ""
    return f"GitHub CLI invocation failed with exit {returncode}{suffix}"


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Stop the isolated GitHub CLI process group without an unbounded drain."""

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        if process.poll() is None:
            process.kill()
    for stream in (process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        # The group was already sent SIGKILL. Never replace the governed deadline
        # with an unbounded cleanup wait.
        pass


def _capture_process(
    process: subprocess.Popen[bytes], timeout: float
) -> tuple[bytes, bytes]:
    """Drain both pipes with a hard stdout cap and one end-to-end deadline."""

    if process.stdout is None or process.stderr is None:
        raise RuntimeError("capture pipes are required")
    selector = selectors.DefaultSelector()
    streams = {process.stdout.fileno(): "stdout", process.stderr.fileno(): "stderr"}
    stdout = bytearray()
    stderr = bytearray()
    expires_at = time.monotonic() + timeout
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = expires_at - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                raise ProtocolRefusal(
                    "gh_deadline_exceeded",
                    f"GitHub CLI exceeded the {timeout:g} second deadline",
                )
            events = selector.select(remaining)
            if not events:
                _terminate_process_group(process)
                raise ProtocolRefusal(
                    "gh_deadline_exceeded",
                    f"GitHub CLI exceeded the {timeout:g} second deadline",
                )
            for key, _ in events:
                try:
                    chunk = os.read(key.fd, 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if streams[key.fd] == "stdout":
                    if len(stdout) + len(chunk) > MAX_GH_OUTPUT_BYTES:
                        _terminate_process_group(process)
                        raise ProtocolRefusal(
                            "gh_output_too_large",
                            f"GitHub CLI output exceeds {MAX_GH_OUTPUT_BYTES} bytes",
                        )
                    stdout.extend(chunk)
                elif len(stderr) < 512:
                    stderr.extend(chunk[: 512 - len(stderr)])
        remaining = expires_at - time.monotonic()
        if remaining <= 0:
            _terminate_process_group(process)
            raise ProtocolRefusal(
                "gh_deadline_exceeded",
                f"GitHub CLI exceeded the {timeout:g} second deadline",
            )
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise ProtocolRefusal(
                "gh_deadline_exceeded",
                f"GitHub CLI exceeded the {timeout:g} second deadline",
            ) from exc
        return bytes(stdout), bytes(stderr)
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def read_github_issue(
    executable: str,
    owner: str,
    repo: str,
    number: int,
    *,
    deadline: float = DEFAULT_GH_DEADLINE,
    now: Optional[datetime] = None,
) -> tuple[dict[str, object], datetime]:
    """Run exactly one bounded issue read and return its exact metadata object."""

    path = _validate_executable(executable)
    timeout = _deadline(deadline)
    environment = fixed_gh_environment(str(path))
    try:
        process = subprocess.Popen(
            fixed_gh_command(str(path), owner, repo, number),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise ProtocolRefusal(
            "gh_invocation_failed", "GitHub CLI invocation could not start"
        ) from exc
    stdout, stderr = _capture_process(process, timeout)
    retrieved_at = _work_now(now)
    if process.returncode != 0:
        credential_names = ("GH_TOKEN", "GITHUB_TOKEN")
        if not any(name in environment for name in credential_names):
            raise ProtocolRefusal(
                "gh_authentication_absent",
                "GitHub refused the request and Floati sent no credential.",
                GH_AUTHENTICATION_REMEDY,
            )
        raise ProtocolRefusal(
            "gh_invocation_failed",
            _failure_detail(
                process.returncode,
                stderr,
                tuple(environment[name] for name in credential_names if name in environment),
            ),
        )
    try:
        metadata = json.loads(stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal(
            "gh_metadata_invalid", "GitHub CLI output is not one UTF-8 JSON object"
        ) from exc
    if not isinstance(metadata, dict):
        raise ProtocolRefusal(
            "gh_metadata_invalid", "GitHub CLI metadata must be one JSON object"
        )
    unexpected = sorted(set(metadata) - set(GH_ISSUE_FIELDS))
    if unexpected:
        raise ProtocolRefusal(
            "gh_metadata_unexpected_field",
            "GitHub CLI metadata contains an unexpected field",
        )
    if set(metadata) != set(GH_ISSUE_FIELDS):
        raise ProtocolRefusal(
            "gh_metadata_invalid", "GitHub CLI metadata is missing a requested field"
        )
    if (
        not isinstance(metadata.get("number"), int)
        or isinstance(metadata.get("number"), bool)
        or metadata.get("number") != number
        or not isinstance(metadata.get("title"), str)
        or not isinstance(metadata.get("body"), str)
    ):
        raise ProtocolRefusal(
            "gh_metadata_invalid", "GitHub CLI metadata does not match the requested issue"
        )
    return metadata, retrieved_at
