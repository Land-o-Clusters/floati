#!/usr/bin/env python3
"""Run selected Mac tests and preserve their logs and process evidence.

Run after the owner releases the hold. Pass both release marker paths and a
fresh directory created with mktemp using the supplied scratch prefix.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import signal
import subprocess
import time


def stop_owned(process):
    if process.poll() is None:
        process.terminate()
    try:
        return process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=10)


def test_group_exists(group):
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A group whose final members are exiting may not be signalable.
        # This is unknown, never evidence that the group has disappeared.
        return None
    return True


def stop_test_group(child):
    """The new session is exclusively the unittest command and its children."""
    group = child.pid
    survived_parent = child.poll() is not None and test_group_exists(group)
    sent = []
    ambiguous_probes = 0
    for requested in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        state = test_group_exists(group)
        if state is False:
            break
        if state is None:
            ambiguous_probes += 1
        try:
            os.killpg(group, requested)
            sent.append(requested.name)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            child.poll()  # Reap the direct child as well as signalling its group.
            state = test_group_exists(group)
            if state is False:
                break
            if state is None:
                ambiguous_probes += 1
            time.sleep(0.02)
        if state is None:
            raise RuntimeError(f"owned unittest group {group} remained unobservable after reaping")
    child.wait(timeout=10)
    if test_group_exists(group) is not False:
        raise RuntimeError(f"owned unittest process group {group} cleanup unproved")
    return {"group": group, "gone": True, "signals": sent,
            "survived_parent": survived_parent, "child_exit": child.returncode,
            "ambiguous_probes": ambiguous_probes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--scratch-prefix")
    parser.add_argument("--hold-markers", type=Path, nargs=2, metavar="PATH")
    parser.add_argument("--load", action="store_true")
    parser.add_argument("--repeat", type=int, choices=(1, 3, 5), default=1)
    parser.add_argument("--keep-going", action="store_true",
                        help="collect every selected baseline RED instead of failing fast")
    parser.add_argument("tests", nargs="+")
    args = parser.parse_args()
    scratch = args.scratch
    if scratch is None:
        parser.error("scratch_required: pass --scratch with a fresh directory")
    if not args.scratch_prefix:
        parser.error("scratch_prefix_required: pass the directory prefix with --scratch-prefix")
    if args.hold_markers is None:
        parser.error("hold_markers_required: pass both release marker paths with --hold-markers")
    scratch_parent = Path("\x2fprivate/tmp")
    if (scratch.parent != scratch_parent
            or not scratch.name.startswith(args.scratch_prefix)
            or scratch.is_symlink() or not scratch.is_dir()):
        parser.error(f"scratch_invalid: use a directory under {scratch_parent} with the supplied prefix")
    metadata = scratch.stat()
    if (metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700
            or any(scratch.iterdir())):
        parser.error("scratch must be fresh, empty, owned and mode 0700")
    if not all(marker.is_absolute() and marker.exists() for marker in args.hold_markers):
        parser.error("owner_hold_closed: run after the owner releases the hold; "
                     "both absolute release marker paths must exist")
    library = scratch / "Library"
    library.mkdir()
    (library / "Python").symlink_to(Path.home() / "Library" / "Python")
    environment = dict(os.environ, HOME=str(scratch), TMPDIR=str(scratch),
                       FLOATI_TEST_TEMP_ROOT=str(scratch),
                       FLOATI_TEST_SCRATCH_PREFIX=args.scratch_prefix,
                       PYTHONDONTWRITEBYTECODE="1")
    environment.pop("PYTHONUSERBASE", None)
    options = [] if args.keep_going else ["--failfast"]
    command = ["/usr/bin/nice", "-n", "10", "/usr/bin/python3", "-B",
               "-m", "unittest", "-v", *options, *args.tests * args.repeat]
    loads = []
    record = {"command": command, "scratch": str(scratch),
              "load_processes": [], "load_before": os.getloadavg(),
              "exit": 1, "outcome": "setup_incomplete"}
    began = time.monotonic()
    terminated = waiting = False

    def terminate(_number, _frame):
        nonlocal terminated, waiting
        terminated = True
        # Defer during Popen ownership transfer and during cleanup.
        if waiting:
            waiting = False
            raise KeyboardInterrupt

    previous_term = signal.signal(signal.SIGTERM, terminate)
    try:
        if args.load:
            for _ in range(os.cpu_count() or 1):
                if terminated:
                    break
                loads.append(subprocess.Popen(
                    ["/usr/bin/nice", "-n", "10", "/usr/bin/yes"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            record["load_processes"] = [p.pid for p in loads]
        record["load_at_start"] = os.getloadavg()
        with (scratch / "unittest.log").open("x") as output:
            if not terminated:
                child = subprocess.Popen(command, env=environment, stdout=output,
                                         stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    record["test_pid"] = child.pid
                    print(json.dumps({"test_pid": child.pid, "load_pids": record["load_processes"],
                                      "scratch": str(scratch)}), flush=True)
                    waiting = True
                    if not terminated:
                        record["exit"] = child.wait()
                        record["outcome"] = "completed"
                except KeyboardInterrupt:
                    record.update(exit=130, outcome="interrupted")
                finally:
                    waiting = False
                    try:
                        record["test_group_cleanup"] = stop_test_group(child)
                    except Exception as failure:
                        record.update(exit=1, outcome="cleanup_unproved",
                                      cleanup_error=repr(failure), test_group=child.pid)
                        raise
                    if record["exit"] == 0 and record["test_group_cleanup"]["survived_parent"]:
                        record.update(exit=1, outcome="unreaped_test_group")
    finally:
        try:
            record["elapsed_seconds"] = time.monotonic() - began
            record["load_at_end"] = os.getloadavg()
            for process in loads:
                if process.poll() is None:
                    process.terminate()
            record["load_exit_codes"] = [stop_owned(p) for p in loads]
            if terminated and record["outcome"] != "cleanup_unproved":
                record.update(exit=128 + signal.SIGTERM, outcome="interrupted")
            with (scratch / "summary.json").open("x") as output:
                output.write(json.dumps(record, indent=2) + "\n")
        finally:
            signal.signal(signal.SIGTERM, previous_term)
    print("\n".join((scratch / "unittest.log").read_text().splitlines()[-65:]))
    print(json.dumps(record, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
