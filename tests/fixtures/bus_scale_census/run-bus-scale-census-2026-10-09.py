"""Closed private-HOME evidence runner for BUS-SCALE-CENSUS-1."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time

from tests.public_projection import HARBOR_POLICY_RELATIVE

ROOT = Path(__file__).resolve().parents[3]
SELF = Path(__file__).resolve()
TEMP = Path("\x2fprivate/tmp") if sys.platform == "darwin" else Path(tempfile.gettempdir()).resolve()
PREFIX = "bus-scale-census-2026-10-09-"
FENCES = (
    "tests.test_rem_2", "tests.test_source_scrub", "tests.test_name_sweep",
    "tests.test_manifest", "tests.test_h1_f1", "tests.test_skip_census_1",
)
MODULES = {"contract": ("tests.harbor_bus_scale_commands", "tests.harbor_bus_scale_io"),
           "runner": ("tests.harbor_bus_scale_runner",),
           "metrics": ("tests.harbor_bus_scale_metrics",),
           "workloads": ("tests.harbor_bus_scale_workloads",),
           "seeds": ("tests.harbor_bus_scale_seeds",),
           "matrix": ("tests.harbor_bus_scale_matrix",),
           "effects": ("tests.test_bus_scale_effects",),
           "durable-effects": ("tests.test_bus_scale_durable_effects",),
           "wait-effects": ("tests.test_bus_scale_wait_effects",),
           "producer": ("tests.harbor_bus_scale_producer",),
           "integration": ("tests.harbor_bus_scale_integration",),
           "read-equivalence": ("tests.test_bus_scale_read_equivalence",),
           "equivalence": ("tests.harbor_bus_scale_equivalence",),
           "entry": ("tests.harbor_bus_scale_entry",), "fences": FENCES}
CONTENT_PATHS = (
    "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py",
    "tests/fixtures/bus_scale_census/probe-bus-scale-waiter-2026-10-09.py",
    "tests/harbor_bus_scale_commands.py", "tests/harbor_bus_scale_io.py",
    "tests/harbor_bus_scale_runner.py", "tests/__init__.py",
    "tests/fixtures/bus-scale-census/native-command-contract-f971841e.json",
    "tests/fixtures/bus-scale-census/native-command-contract-d3c6260e.json",
    "tests/fixtures/bus-scale-census/native-command-contract-d66a94da.json",
    "tests/fixtures/bus-scale-census/native-codex-quota-2026-10-15.json",
    "scripts/regen_h1_f1_reader_maps.py",
)


def content_witness(root, relative_paths):
    result = {}
    root = Path(root).resolve(strict=True)
    for relative in relative_paths:
        path = root / relative
        if (Path(relative).is_absolute() or ".." in Path(relative).parts
                or path.resolve(strict=True) != path or not path.is_file()):
            raise ValueError("content witness requires an ordinary contained file: " + str(relative))
        raw = path.read_bytes()
        result[str(relative)] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return result


def source_population():
    # Bind untracked Python imports too, not merely the Git status text.
    if (ROOT / HARBOR_POLICY_RELATIVE).is_file():
        issue_paths = {
            "tests/fixtures/bus-scale-census/native-gh-issue-harbor-3-2026-10-15.json",
            "tests/fixtures/bus-scale-census/native-gh-issue-harbor-3-2026-10-15.provenance.json",
        }
    else:
        issue_paths = {"tests/fixtures/bus-scale-census/synthetic-gh-issue.json"}
    return tuple(sorted(set(CONTENT_PATHS) | issue_paths | {
        path.relative_to(ROOT).as_posix()
        for directory in ("floati", "tests", "docs/evidence")
        for path in (ROOT / directory).rglob("*.py")
    }))


def launch_owned_child(argv, *, cwd, env, stdout, stderr):
    return subprocess.Popen(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                            start_new_session=True)


def collect_owned_child(child, *, deadline_seconds, collection_seconds,
                        clock=time.monotonic, cancelled=lambda: False):
    if deadline_seconds <= 0 or collection_seconds <= 0:
        raise ValueError("child deadlines must be positive")
    deadline = clock() + deadline_seconds
    stop_at, reason, interrupted = None, "complete", False
    last_progress = clock()
    while True:
        if child.poll() is not None:
            code = child.wait(timeout=0)
            return dict(status=reason, exit_code=code, child_collected=True,
                        interrupted=interrupted)
        now = clock()
        if stop_at is None and (cancelled() or now >= deadline):
            reason = "cancelled" if now < deadline else "deadline"
            stop_at = now + collection_seconds
            interrupted = True
            child.send_signal(signal.SIGINT)
        if stop_at is not None and now >= stop_at:
            return dict(status="uncollected", exit_code=None, child_collected=False,
                        interrupted=interrupted)
        try:
            code = child.wait(timeout=min(0.25, (stop_at or deadline) - now))
            return dict(status=reason, exit_code=code, child_collected=True,
                        interrupted=interrupted)
        except subprocess.TimeoutExpired:
            if clock() - last_progress >= 20:
                print("owned child still running", flush=True)
                last_progress = clock()
        except KeyboardInterrupt:
            if stop_at is None:
                reason, interrupted = "cancelled", True
                stop_at = clock() + collection_seconds
                child.send_signal(signal.SIGINT)


def cleanup_owned_scratch(scratch, *, child_collected):
    if not child_collected:
        return False
    shutil.rmtree(scratch)
    return not scratch.exists()


def attempt_exit_code(record):
    if (record.get("status") != "complete" or record.get("interrupted")
            or any(record.get(field) is not True for field in
                   ("child_collected", "scratch_removed", "source_unchanged", "content_unchanged"))
            or record.get("native", {}).get("native_home_removed") is not True
            or record.get("helper_sha256") != record.get("helper_final_sha256")):
        return 1
    code = record.get("exit_code")
    return code if isinstance(code, int) else 1


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def action_deadline_seconds(action):
    if action == "census-followup":
        return 3600
    if action in {"census-smoke", "census-first-pass"}:
        return 7200
    if action in {*MODULES, "maps", "baseline", "parser-snapshot", "census-record", "census-check"}:
        return 1800
    raise ValueError("unknown child action")


def child(action, destination):
    sys.path.insert(0, str(ROOT))
    import tests
    import unittest
    result = {"action": action, "home_before_tests": tests.REAL_HOME_BEFORE_PIN,
              "native_home": tests.HOME_PIN_ROOT}
    code = 1
    uncollected = False
    try:
        if action == "parser-snapshot":
            from floati.cli import _parser
            from floati.command_contract import describe_parser
            provenance = subprocess.run(
                ["/usr/bin/git", "diff", "--exit-code", "f971841e48b008903277b86d089892bbde42f963", "--", "floati"],
                cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            contract = describe_parser(_parser())
            target = ROOT / "tests/fixtures/bus-scale-census/native-command-contract-f971841e.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x") as stream:
                stream.write(json.dumps(contract, sort_keys=True, indent=2) + "\n")
            result.update(snapshot=str(target.relative_to(ROOT)), snapshot_sha256=digest(target),
                          native_source="f971841e48b008903277b86d089892bbde42f963")
            code = 0
        elif action in {"maps", "baseline", "census-smoke", "census-first-pass", "census-record", "census-check", "census-followup"}:
            import runpy
            old = sys.argv
            try:
                sys.argv = (["scripts/regen_h1_f1_reader_maps.py", "--write"] if action == "maps"
                            else ["tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                  action.removeprefix("census-"), str(destination.parent)]
                            if action.startswith("census-")
                            else ["tests/fixtures/bus_scale_census/probe-bus-scale-waiter-2026-10-09.py", "run"])
                runpy.run_path(str(ROOT / sys.argv[0]), run_name="__main__")
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    raise
            finally:
                sys.argv = old
            code = 0
        else:
            suite = unittest.defaultTestLoader.loadTestsFromNames(MODULES[action])
            native = unittest.TextTestRunner(verbosity=2).run(suite)
            result.update(tests_run=native.testsRun, failures=len(native.failures),
                          errors=len(native.errors), skipped=len(native.skipped),
                          failure_ids=[test.id() for test, _ in native.failures],
                          error_ids=[test.id() for test, _ in native.errors])
            code = 0 if native.wasSuccessful() else 1
    except BaseException as exc:
        uncollected = bool(getattr(exc, "uncollected_child", False))
        result.update(error=type(exc).__name__ + ": " + str(exc),
                      uncollected_descendant=uncollected)
        raise
    finally:
        home = Path(tests.HOME_PIN_ROOT)
        try:
            if home.parent != TEMP or not home.name.startswith("suite-home-") or home.is_symlink():
                raise RuntimeError("native HOME identity is not the owned test pin")
            if uncollected:
                raise RuntimeError("native HOME retained for uncollected descendant")
            shutil.rmtree(home)
            result["native_home_removed"] = not home.exists()
        except Exception as exc:
            result["cleanup_error"] = type(exc).__name__ + ": " + str(exc)
            result["native_home_removed"] = False
            code = 1
        result["exit_code"] = code
        with destination.open("x") as stream:
            stream.write(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return code


def source(env):
    result = {}
    for name, args in (("head", ["rev-parse", "HEAD"]),
                       ("status", ["status", "--porcelain=v1"]),
                       ("diff", ["diff", "HEAD", "--"])):
        completed = subprocess.run(["/usr/bin/git", *args], cwd=ROOT, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        result[name] = (hashlib.sha256(completed.stdout).hexdigest() if name == "diff"
                        else completed.stdout.decode())
    return result


def bank(attempt):
    output = TEMP / (PREFIX + attempt)
    if output.is_symlink() or not output.is_dir():
        raise ValueError("missing ordinary attempt bank")
    receipt_path = output / "receipt.json"
    data = json.loads(receipt_path.read_text())
    raw = output / "terminal.txt"
    if not data.get("child_collected") or digest(raw) != data["terminal_sha256"]:
        raise ValueError("uncollected or changed attempt")
    destinations = []
    members = [(raw, ".txt"), (receipt_path, ".json")]
    census_path = output / "census.json"
    if census_path.exists():
        census = json.loads(census_path.read_text())
        archive = output / "census-raw.tar.gz"
        if (archive.is_symlink() or not archive.is_file()
                or digest(archive) != census["raw_archive_sha256"]
                or archive.stat().st_size != census["raw_archive_bytes"]):
            raise ValueError("census raw archive is absent or changed")
        members.extend(((census_path, "-census.json"), (archive, "-census-raw.tar.gz")))
    for path, suffix in members:
        if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("invalid raw bank member")
    # Check every existing member bound before publishing the first file.
    # Oversized archives remain private; this adds no new archive format.
    for path, suffix in members:
        destination = ROOT / "docs/evidence" / (PREFIX + attempt + suffix)
        with destination.open("xb") as stream:
            stream.write(path.read_bytes())
        destinations.append({"path": str(destination), "sha256": digest(destination)})
    print(json.dumps({"action": "bank", "attempt": attempt, "files": destinations}, sort_keys=True))
    return 0


def run(action, attempt):
    output = TEMP / (PREFIX + attempt)
    output.mkdir(mode=0o700)
    scratch = Path(tempfile.mkdtemp(prefix="bus-scale-runner-", dir=TEMP))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYTHON", "GIT_", "DYLD_", "LD_", "FLOATI_"))
           and key not in {"HOME", "TMPDIR", "TMP", "TEMP"} and not key.startswith("XDG_")}
    home, temporary = scratch / "home", scratch / "tmp"
    home.mkdir()
    temporary.mkdir()
    env.update(HOME=str(home), TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary),
               XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local/share"),
               XDG_CACHE_HOME=str(home / ".cache"), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    raw, native = output / "terminal.txt", output / "native.json"
    record = {"attempt": attempt, "action": action, "source_before": source(env),
              "content_before": content_witness(ROOT, source_population()),
              "helper_sha256": digest(SELF), "home_before_startup": str(home),
              "child_collected": False, "scratch_removed": False}
    process = None
    started = time.monotonic()
    try:
        with raw.open("xb") as stream:
            process = launch_owned_child(["/usr/bin/python3", "-B", str(SELF),
                                        "--child", action, str(native)],
                                       cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
            record["child_pid"] = process.pid
            record.update(collect_owned_child(process, deadline_seconds=action_deadline_seconds(action),
                                             collection_seconds=30))
    finally:
        record.update(elapsed_seconds=time.monotonic() - started,
                      source_after=source(env), helper_final_sha256=digest(SELF),
                      content_after=content_witness(ROOT, source_population()))
        record["scratch_removed"] = cleanup_owned_scratch(
            scratch, child_collected=record["child_collected"])
        record["source_unchanged"] = record["source_before"] == record["source_after"]
        record["content_unchanged"] = record["content_before"] == record["content_after"]
        if native.exists():
            record["native"] = json.loads(native.read_text())
        if raw.exists():
            record["terminal_sha256"] = digest(raw)
        (output / "receipt.json").write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    print(raw.read_text(errors="replace")[-7000:], end="")
    print(json.dumps({key: value for key, value in record.items()
                      if key not in {"content_before", "content_after"}}, sort_keys=True))
    return attempt_exit_code(record)


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        if sys.argv[2] not in {*MODULES, "maps", "baseline", "parser-snapshot", "census-smoke", "census-first-pass", "census-record", "census-check", "census-followup"}:
            raise ValueError("unknown child action")
        return child(sys.argv[2], Path(sys.argv[3]))
    if len(sys.argv) != 3 or sys.argv[1] not in {*MODULES, "maps", "baseline", "parser-snapshot", "bank", "census-smoke", "census-first-pass", "census-record", "census-check", "census-followup"}:
        raise ValueError("expected contract|fences|maps|baseline|parser-snapshot|bank ATTEMPT")
    action, attempt = sys.argv[1:]
    if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", attempt) is None:
        raise ValueError("invalid attempt label")
    return bank(attempt) if action == "bank" else run(action, attempt)


if __name__ == "__main__":
    raise SystemExit(main())
