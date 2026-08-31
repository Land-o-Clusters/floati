"""Hermetic, local-only reproduction of typed delivery claims."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import time
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

from .errors import ProtocolRefusal
from .events import EventLog
from .ids import uuid7_hex
from .jsonl import append_record
from .locks.cleanup import CleanupInspector
from .records import validate_record
from .registry import Registry, utc_now
from .root import FloatiRoot


_RAN_ANCHOR = re.compile(rb"(?m)^Ran ([0-9]+) tests? in [^\r\n]+\r?$")
_OK_ANCHOR = re.compile(rb"(?m)^OK(?: \([^\r\n]*\))?\r?$")
_FAILED_ANCHOR = re.compile(rb"(?m)^FAILED(?: \([^\r\n]*\))?\r?$")
_RECEIPT_KINDS = frozenset({"verification_receipt"})


class _Unrunnable(Exception):
    def __init__(self, code: str, remedy: str) -> None:
        super().__init__(code)
        self.code = code
        self.remedy = remedy


def load_claim_document(path_value: object) -> Dict[str, object]:
    """Load one bounded typed claim document without following an alias leaf."""

    if not isinstance(path_value, str) or not path_value:
        raise ProtocolRefusal("claim_path_invalid", "--claim requires one JSON file path")
    path = Path(path_value).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ProtocolRefusal(
            "claim_path_invalid", "delivery claim path must name one regular non-symlink file"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProtocolRefusal("claim_unreadable", "delivery claim file could not be read") from exc
    if not raw or len(raw) > 65_536:
        raise ProtocolRefusal("claim_size_invalid", "delivery claim file must contain at most 65536 bytes")
    def unique_object(pairs: Sequence[tuple[str, object]]) -> Dict[str, object]:
        value: Dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate claim key")
            value[key] = item
        return value

    def reject_constant(_value: str) -> object:
        raise ValueError("non-I-JSON numeric constant")

    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProtocolRefusal("claim_malformed", "delivery claim file is not strict JSON UTF-8") from exc
    if not isinstance(value, dict):
        raise ProtocolRefusal("claim_not_object", "delivery claim document must be a JSON object")
    return value


def parse_unittest_measurement(output: bytes, *, returncode: int) -> Dict[str, object]:
    """Measure anchors from the complete output while preserving direct status."""

    if not isinstance(output, bytes):
        raise ProtocolRefusal("test_output_unparseable", "test output must be retained as bytes")
    anchors = tuple(_RAN_ANCHOR.finditer(output))
    if len(anchors) != 1:
        raise ProtocolRefusal(
            "test_output_unparseable",
            "untruncated test output must contain exactly one Ran N tests anchor",
        )
    ran = int(anchors[0].group(1))
    ok = bool(_OK_ANCHOR.search(output))
    failed = bool(_FAILED_ANCHOR.search(output))
    if ok == failed:
        raise ProtocolRefusal(
            "test_output_unparseable",
            "untruncated test output must contain exactly one OK or FAILED anchor",
        )
    result = "OK" if returncode == 0 and ok else "FAILED"
    return {
        "ran": ran,
        "result": result,
        "returncode": returncode,
        "output_sha256": hashlib.sha256(output).hexdigest(),
    }


class DeliveryVerifier:
    """Verify one claim in a fresh exact-SHA worktree and append its receipt."""

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "delivery verification requires a writable root")
        self.root = root
        self.events = EventLog(root)
        self.registry = Registry(root)
        self.receipt_relative_path = Path("receipts/verifications.jsonl")

    @staticmethod
    def _environment() -> Dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": "/var/empty",
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        }

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Unrunnable(
                "deadline_exceeded", "increase the claim deadline and send a corrected claim"
            )
        return remaining

    def _git(
        self,
        repository: Path,
        *arguments: str,
        deadline: float,
        allowed: Sequence[int] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        argv = [
            "/usr/bin/git",
            "--no-optional-locks",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-C",
            os.fspath(repository),
            *arguments,
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                check=False,
                timeout=self._remaining(deadline),
                env=self._environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise _Unrunnable(
                "deadline_exceeded", "increase the claim deadline and send a corrected claim"
            ) from exc
        except OSError as exc:
            raise _Unrunnable("repository_invalid", "repair the named local repository") from exc
        if completed.returncode not in frozenset(allowed):
            raise _Unrunnable("checkout_failure", "repair the named repository worktree state")
        return completed

    def _claim(self, claim_id: object) -> Dict[str, object]:
        if not isinstance(claim_id, str):
            raise ProtocolRefusal("claim_id_invalid", "--claim must name one delivery claim id")
        claim = next(
            (
                row
                for row in self.events.event_records()
                if row.get("kind") == "delivery_claim" and row.get("id") == claim_id
            ),
            None,
        )
        if claim is None:
            raise ProtocolRefusal("claim_unknown", "the named delivery claim is absent")
        return claim

    @staticmethod
    def _repository(claim: Mapping[str, object]) -> Path:
        path = Path(str(claim["repo_path"]))
        try:
            if path.is_symlink() or path.resolve(strict=True) != path or not path.is_dir():
                raise OSError("repository identity is not canonical")
        except OSError as exc:
            raise _Unrunnable(
                "repository_invalid", "repair the canonical absolute repository path in the claim"
            ) from exc
        return path

    def _require_banked(self, repository: Path, sha: str, deadline: float) -> None:
        exists = self._git(
            repository,
            "cat-file",
            "-e",
            sha + "^{commit}",
            deadline=deadline,
            allowed=(0, 128),
        )
        if exists.returncode != 0:
            raise _Unrunnable(
                "sha_absent",
                f"run git fetch --all in {repository}, then send a corrected or newly banked claim",
            )
        banked = self._git(
            repository,
            "for-each-ref",
            "--format=%(refname)",
            "--contains",
            sha,
            "refs/remotes",
            deadline=deadline,
        )
        refs = tuple(line for line in banked.stdout.decode("utf-8", "strict").splitlines() if line)
        if not refs:
            raise _Unrunnable(
                "sha_unbanked",
                f"run git fetch --all in {repository} after the commit is pushed, then verify again",
            )
        if any(not ref.startswith("refs/remotes/") or ref.startswith("-") for ref in refs):
            raise _Unrunnable("repository_invalid", "repair malformed remote ref testimony")

    @staticmethod
    def _lock_tree(worktree: Path) -> Dict[Path, int]:
        modes: Dict[Path, int] = {}
        paths = [worktree, *worktree.rglob("*")]
        for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
            if path.is_symlink():
                continue
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
                modes[path] = mode
                path.chmod(mode & ~0o222)
            except OSError as exc:
                raise _Unrunnable(
                    "checkout_failure", "scratch worktree could not be made read-only"
                ) from exc
        return modes

    @staticmethod
    def _restore_tree(modes: Mapping[Path, int]) -> None:
        for path, mode in sorted(modes.items(), key=lambda item: len(item[0].parts)):
            if path.exists() and not path.is_symlink():
                path.chmod(mode)

    def _import_bank(
        self, worktree: Path, bank: object, deadline: float
    ) -> None:
        if bank == "discover":
            return
        modules = list(bank) if isinstance(bank, list) else []
        code = (
            "import importlib,sys;"
            "[importlib.import_module(name) for name in sys.argv[1:]]"
        )
        try:
            completed = subprocess.run(
                ["python3", "-c", code, *modules],
                cwd=worktree,
                capture_output=True,
                check=False,
                timeout=self._remaining(deadline),
                env=self._environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise _Unrunnable(
                "deadline_exceeded", "increase the claim deadline and send a corrected claim"
            ) from exc
        except OSError as exc:
            raise _Unrunnable(
                "bank_module_unresolved", "repair the claimed unittest module list"
            ) from exc
        if completed.returncode != 0:
            raise _Unrunnable(
                "bank_module_unresolved", "repair the claimed unittest module list"
            )

    @staticmethod
    def _runner(worktree: Path, bank: object) -> tuple[list[str], list[str]]:
        shipped = worktree / "scripts" / "test"
        arguments = ["discover"] if bank == "discover" else list(bank)
        if shipped.is_file() and os.access(shipped, os.X_OK):
            actual = [os.fspath(shipped), *arguments]
            return actual, ["scripts/test", *arguments]
        actual = ["python3", "-m", "unittest", *arguments]
        return actual, ["PYTHONDONTWRITEBYTECODE=1", *actual]

    def _run_bank(
        self, worktree: Path, bank: object, deadline: float
    ) -> tuple[Dict[str, object], list[str]]:
        actual, display = self._runner(worktree, bank)
        try:
            completed = subprocess.run(
                actual,
                cwd=worktree,
                capture_output=True,
                check=False,
                timeout=self._remaining(deadline),
                env=self._environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise _Unrunnable(
                "deadline_exceeded", "increase the claim deadline and send a corrected claim"
            ) from exc
        except OSError as exc:
            raise _Unrunnable("checkout_failure", "the claimed runner could not start") from exc
        output = completed.stdout + completed.stderr
        try:
            measured = parse_unittest_measurement(
                output, returncode=completed.returncode
            )
        except ProtocolRefusal as exc:
            raise _Unrunnable(exc.code, exc.detail) from exc
        return measured, display

    @staticmethod
    def _artifacts(
        worktree: Path, claimed: object
    ) -> list[Dict[str, object]]:
        measured: list[Dict[str, object]] = []
        for artifact in claimed if isinstance(claimed, list) else []:
            path = str(artifact["path"])
            candidate = worktree / path
            present = False
            digest: Optional[str] = None
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(worktree)
                if not candidate.is_symlink() and resolved.is_file():
                    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
                    present = True
            except (OSError, ValueError):
                pass
            measured.append({"path": path, "present": present, "sha256": digest})
        return measured

    @staticmethod
    def _matches(claim: Mapping[str, object], measurement: Mapping[str, object]) -> bool:
        declared = claim["declared"]
        measured_declared = measurement["declared"]
        if declared != measured_declared:
            return False
        expected = claim["artifacts"]
        actual = measurement["artifacts"]
        return len(expected) == len(actual) and all(
            measured.get("present") is True
            and measured.get("path") == claimed.get("path")
            and measured.get("sha256") == claimed.get("sha256")
            for claimed, measured in zip(expected, actual)
        )

    def _append_receipt(self, fields: Mapping[str, object]) -> Dict[str, object]:
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "verification-receipt-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "verification_receipt",
            **fields,
        }
        record = validate_record(
            record, self.root.tenant_id, _RECEIPT_KINDS, integrity=False
        )
        append_record(
            self.root,
            self.receipt_relative_path,
            record,
            allowed_kinds=_RECEIPT_KINDS,
        )
        return record

    def verify(self, verifier: object, claim_id: object) -> Dict[str, object]:
        actor = self.registry.resolve_node_id(
            verifier, field="verifier", unknown_code="unknown_verifier"
        )
        claim = self._claim(claim_id)
        started = time.monotonic()
        deadline = started + int(claim["deadline_seconds"])
        scratch = self.root.resolve_relative(
            Path("scratch/verifications") / str(claim["id"])
        )
        repository: Optional[Path] = None
        created = False
        destroyed = False
        modes: Dict[Path, int] = {}
        runner_argv: list[str] = []
        measurement: Optional[Dict[str, object]] = None
        output_sha256: Optional[str] = None
        outcome = "verification_unrunnable"
        reason_code: Optional[str] = None
        remedy = ""
        try:
            repository = self._repository(claim)
            self._require_banked(repository, str(claim["sha"]), deadline)
            if scratch.exists() or scratch.is_symlink():
                raise _Unrunnable(
                    "scratch_reuse_refused", "inspect and remove the stale verification scratch through governed cleanup"
                )
            scratch.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._git(
                repository,
                "worktree",
                "add",
                "--detach",
                os.fspath(scratch),
                str(claim["sha"]),
                deadline=deadline,
            )
            created = True
            modes = self._lock_tree(scratch)
            self._import_bank(scratch, claim["bank"], deadline)
            parsed, runner_argv = self._run_bank(scratch, claim["bank"], deadline)
            output_sha256 = str(parsed["output_sha256"])
            measurement = {
                "declared": {"ran": parsed["ran"], "result": parsed["result"]},
                "artifacts": self._artifacts(scratch, claim["artifacts"]),
                "returncode": parsed["returncode"],
                "output_sha256": parsed["output_sha256"],
            }
            outcome = (
                "verified_match"
                if self._matches(claim, measurement)
                else "verified_mismatch"
            )
        except _Unrunnable as exc:
            reason_code = exc.code
            remedy = exc.remedy
        finally:
            if created and repository is not None:
                try:
                    self._restore_tree(modes)
                    status_result = self._git(
                        scratch,
                        "status",
                        "--porcelain",
                        "--untracked-files=all",
                        deadline=max(deadline, time.monotonic() + 5.0),
                    )
                    if status_result.stdout:
                        raise _Unrunnable(
                            "checkout_dirty", "inspect the retained dirty verification scratch"
                        )
                    CleanupInspector(repository).require_eligible(scratch)
                    self._git(
                        repository,
                        "worktree",
                        "remove",
                        os.fspath(scratch),
                        deadline=max(deadline, time.monotonic() + 5.0),
                    )
                    destroyed = not scratch.exists()
                    if not destroyed:
                        raise _Unrunnable(
                            "cleanup_failed", "inspect the retained verification scratch"
                        )
                except _Unrunnable as exc:
                    outcome = "verification_unrunnable"
                    if exc.code == "checkout_dirty":
                        reason_code = exc.code
                        remedy = exc.remedy
                    else:
                        reason_code = "cleanup_failed"
                        remedy = "inspect the retained verification scratch and its current ref reachability"
                    destroyed = False
                except (ProtocolRefusal, OSError):
                    outcome = "verification_unrunnable"
                    reason_code = "cleanup_failed"
                    remedy = "inspect the retained verification scratch and its current ref reachability"
                    destroyed = False
        if outcome != "verification_unrunnable":
            reason_code = None
            remedy = ""
        fields: Dict[str, object] = {
            "claim_id": claim["id"],
            "verifier": actor,
            "outcome": outcome,
            "reason_code": reason_code,
            "remedy": remedy,
            "runner_argv": runner_argv,
            "python_version": platform.python_version(),
            "repo_path": claim["repo_path"],
            "wall_time_seconds": f"{time.monotonic() - started:.6f}",
            "unchecked_scope": (
                "none" if claim["bank"] == "discover" else "claimed_bank_only"
            ),
            "output_sha256": output_sha256,
            "claim": claim,
            "measurement": measurement,
            "scratch": {
                "path": os.fspath(scratch),
                "created": created,
                "destroyed": destroyed,
            },
        }
        return self._append_receipt(fields)
