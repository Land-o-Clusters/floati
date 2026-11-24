#!/usr/bin/python3
"""Reproduce the Linux nested-run data paths with the native host observer.

Only a private committed source copy is transformed. The real platform remains
unchanged for process ownership, signals and native cleanup. Staging and native
artifacts are retained so a failed reproduction remains inspectable.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


SOURCE = Path(__file__).resolve().parents[1]
MODULES = ("tests.harbor_parallel_launcher", "tests.harbor_parallel_qualification_supervisor")
NAMED = (
    "tests.harbor_parallel_launcher.ParallelLauncherIntegrationTests."
    "test_parallel_join_precedes_serial_and_workers_have_distinct_homes",
    *(
        "tests.harbor_parallel_qualification_supervisor.ParallelQualificationSupervisorTests." + name
        for name in (
            "test_actual_sigint_after_parallel_method_starts_is_interrupted_and_collected",
            "test_external_total_crossing_after_payload_publication_refuses_native_success_pair",
            "test_committed_wrong_source_cannot_authorize_supervisor_scratch_cleanup",
            "test_native_parallel_failure_retains_external_interval_without_qualification",
            "test_native_serial_failure_retains_committed_payload_without_supervisor_interrupt",
            "test_real_success_pair_without_ready_cannot_enter_early_failure_admission",
            "test_parallel_wall_deadline_is_a_typed_collected_refusal",
        )
    ),
)
TEMP_MODULES = (
    "scripts/parallel_isolation.py", "scripts/parallel_qualification.py",
    "scripts/parallel_supervisor.py", "tests/temp_roots.py",
)


def _ordinary_parent(path):
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        raise ValueError("output must be absolute and normalized")
    if os.path.lexists(path) or not path.parent.is_dir() or path.parent.resolve() != path.parent:
        raise ValueError("output must be absent with an existing ordinary parent")
    for component in (*reversed(path.parent.parents), path.parent):
        if component.is_symlink():
            raise ValueError("output must have ordinary directory ancestors")


def _git(root, environment, *arguments):
    return subprocess.check_output(
        ["/usr/bin/git", *arguments], cwd=root, env=environment,
        stderr=subprocess.STDOUT, text=True,
    ).strip()


def _linux_data_copy(relative, text):
    """Change only data-root branches, never the host's sys.platform value."""
    changes = []
    if sys.platform != "darwin":
        return text, changes
    if relative in TEMP_MODULES:
        # Every Darwin predicate in these four infrastructure modules selects
        # a temporary DATA root or the spelling/layout of its child paths.
        # In particular the stable Linux <temp> answer remains <temp>: no rewrite
        # substitutes tempfile.gettempdir() for a newly repaired stable root.
        for predicate in ('sys.platform == "darwin"', "sys.platform == 'darwin'"):
            count = text.count(predicate)
            if count:
                text = text.replace(predicate, "False")
                changes.append({"predicate": predicate, "count": count})
    elif relative == "scripts/test-parallel.py":
        # Do not rewrite check_work_parent's actual-host path policy.
        old = "Path('\x2fprivate/tmp' if sys.platform == 'darwin' else tempfile.gettempdir()).resolve()"
        count = text.count(old)
        if count:
            text = text.replace(old, "Path(tempfile.gettempdir()).resolve()")
            changes.append({"predicate": "launcher scratch data root", "count": count})
    return text, changes


def _stage(root, *, named_only):
    root.mkdir(mode=0o700)
    (root / "scripts").mkdir(mode=0o700)
    (root / "tests").mkdir(mode=0o700)
    paths = sorted({"scripts/test-parallel.py", "scripts/fixture_finalizer.py", *(
        str(path.relative_to(SOURCE)) for path in (SOURCE / "scripts").glob("parallel_*.py")
    ), "tests/__init__.py", "tests/temp_roots.py", "tests/harbor_parallel_launcher.py",
        "tests/harbor_parallel_qualification.py", "tests/harbor_parallel_qualification_supervisor.py"})
    copies = []
    for relative in paths:
        raw = (SOURCE / relative).read_bytes()
        text, changes = _linux_data_copy(relative, raw.decode())
        # Native runner and launcher fixture children use this same fixed
        # interpreter. Make bytecode suppression explicit in their argv too.
        if relative in ("scripts/test-parallel.py", "tests/harbor_parallel_launcher.py"):
            old = "[sys.executable, str(self.root / 'scripts/test-parallel.py')"
            count = text.count(old)
            if count:
                text = text.replace(old, "[sys.executable, '-B', str(self.root / 'scripts/test-parallel.py')")
                changes.append({"predicate": "explicit child bytecode suppression", "count": count})
        (root / relative).write_text(text)
        copies.append({"path": relative, "source_sha256": hashlib.sha256(raw).hexdigest(),
                       "staged_sha256": hashlib.sha256(text.encode()).hexdigest(), "changes": changes})
    # Keep the production package HOME pin. Suppress recursive discovery of
    # fixture support tests; explicit per-class adapters below own selection.
    with (root / "tests/__init__.py").open("a") as stream:
        stream.write("\n\ndef load_tests(loader, tests, pattern):\n    return tests\n")
    selected = []
    groups = []
    if named_only:
        for module in MODULES:
            names = [name for name in NAMED if name.startswith(module + ".")]
            groups.append(names)
            selected.extend(names)
    else:
        for module in MODULES:
            tree = ast.parse((root / (module.replace(".", "/") + ".py")).read_text())
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and any(
                        isinstance(base, ast.Attribute) and base.attr == "TestCase" for base in node.bases):
                    group = [module + "." + node.name]
                    groups.append(group)
                    selected.extend(module + "." + node.name + "." + member.name
                                    for member in node.body if isinstance(member, ast.FunctionDef)
                                    and member.name.startswith("test_"))
    for ordinal, names in enumerate(groups):
        (root / ("test_row_%02d.py" % ordinal)).write_text(
            "def load_tests(loader, tests, pattern):\n"
            "    return loader.loadTestsFromNames(%r)\n" % names)
    (root / "scripts/parallel_serial_classes.json").write_text("{}\n")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    for copied in copies:
        copied["staged_sha256"] = hashlib.sha256((root / copied["path"]).read_bytes()).hexdigest()
    return copies, selected, min(8, len(groups))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", choices=("nested", "long"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--named-only", action="store_true")
    args = parser.parse_args()
    _ordinary_parent(args.output)
    args.output.mkdir(mode=0o700)
    platform_root = Path("\x2fprivate/tmp" if sys.platform == "darwin" else "\x2ftmp").resolve(strict=True)
    scratch = Path(tempfile.mkdtemp(prefix="hf2-repro-", dir=platform_root))
    home = scratch / "home"
    home.mkdir(mode=0o700)
    temporary = scratch / "outer"
    temporary.mkdir(mode=0o700)
    minimum = 160 if args.condition == "long" else 80
    while len(os.fsencode(str(temporary))) < minimum:
        temporary = temporary / "worker-data-xxxxxxxx"
        temporary.mkdir(mode=0o700)
    environment = dict(os.environ, HOME=str(home), TMPDIR=str(temporary), TMP=str(temporary),
                       TEMP=str(temporary), PYTHONDONTWRITEBYTECODE="1")
    for key in ("FLOATI_TEST_TEMP_ROOT", "PYTHONHOME", "PYTHONPATH"):
        environment.pop(key, None)
    for key, name in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_CACHE_HOME", "cache")):
        directory = home / name
        directory.mkdir(mode=0o700)
        environment[key] = str(directory)
    root = scratch / "source"
    copies, selected, workers = _stage(root, named_only=args.named_only)
    _git(root, environment, "init", "-q")
    _git(root, environment, "add", ".")
    _git(root, environment, "-c", "user.name=Reproduction Fixture", "-c",
         "user.email=fixture@example.invalid", "commit", "-qm", "Record the private reproduction source.")
    head = _git(root, environment, "rev-parse", "HEAD")
    argv = ["/usr/bin/python3", "-B", str(root / "scripts/test-parallel.py"), "run",
            "--workers", str(workers), "--output", str(args.output / "native")]
    process = None
    cancelled = []

    def cancel(number, frame):
        if not cancelled:
            cancelled.append(number)
            if process is not None and process.poll() is None:
                process.send_signal(number)

    prior = {number: signal.signal(number, cancel) for number in (signal.SIGINT, signal.SIGTERM)}
    began = time.monotonic()
    try:
        with (args.output / "stdout.log").open("xb") as stdout, (args.output / "stderr.log").open("xb") as stderr:
            process = subprocess.Popen(argv, cwd=root, env=environment, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            if cancelled and process.poll() is None:
                process.send_signal(cancelled[0])
            code = process.wait()
    finally:
        # A signal only requests native cleanup. Collection of the retained
        # handle is required before restoring handlers or publishing evidence.
        if process is not None:
            process.wait()
        for number, handler in prior.items():
            signal.signal(number, handler)
    final_path = args.output / "native/final.json"
    final = json.loads(final_path.read_text()) if final_path.is_file() else None
    receipt = {"condition": args.condition, "host_platform": sys.platform,
               "linux_data_temp_emulated": sys.platform == "darwin", "named_only": args.named_only,
               "output": str(args.output), "scratch_root": str(scratch), "staged_source": str(root),
               "staged_commit": head, "copies": copies, "selected_ids": selected,
               "selected_count": len(selected), "workers": workers, "outer_tmpdir": str(temporary),
               "outer_tmpdir_bytes": len(os.fsencode(str(temporary))), "home": str(home),
               "argv": argv, "exit_code": code, "wall_seconds": time.monotonic() - began,
               "child_collected": process.returncode is not None, "staging_retained": True,
               "final_path": str(final_path) if final else None,
               "native_status": final.get("status") if final else "missing_final_artifact",
               "tests_run": final.get("aggregate", {}).get("tests_run") if final else None,
               "successful": bool(final and final.get("successful") and code == 0)}
    if cancelled:
        receipt["interruption"] = cancelled[0]
    (args.output / "reproduction.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
    print(json.dumps({key: receipt[key] for key in (
        "condition", "exit_code", "successful", "tests_run", "selected_count", "native_status",
        "output", "staged_commit", "outer_tmpdir_bytes", "child_collected")}, sort_keys=True))
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
