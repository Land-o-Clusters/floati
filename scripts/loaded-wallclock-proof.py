#!/usr/bin/env python3
"""Run this row's serial unittest proof with owned, reaped contention children."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from fixture_finalizer import install, run_child


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--load", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--selection-file", type=Path)
    parser.add_argument("--label")
    parser.add_argument("--isolate", action="store_true",
                        help="run each selector's repetitions in its own serial process")
    parser.add_argument("--failfast", action="store_true")
    parser.add_argument("--soft-nofile", type=int, choices=[1048576],
                        help="reproduce the reviewed million-descriptor isolation condition")
    parser.add_argument("tests", nargs="*")
    args = parser.parse_args()
    selections = args.tests + (json.loads(args.selection_file.read_text()) if args.selection_file else [])
    if not selections or args.repeat < 1:
        parser.error("select tests and a positive repeat count")
    if args.label and not re.fullmatch(r"[a-z0-9-]+", args.label):
        parser.error("label must contain only lowercase letters, numbers and hyphens")
    children = []
    log = None
    limits = {}
    if args.soft_nofile is not None:
        import resource
        _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (args.soft_nofile, hard))
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        limits = {"soft_nofile": soft, "hard_nofile": hard}
        print(json.dumps(limits), flush=True)
    try:
        if args.load:
            for _ in range(8):
                children.append(subprocess.Popen(
                    ["/usr/bin/python3", "-c", "while True: pass"]))
            print(json.dumps({"load_pids": [child.pid for child in children]}), flush=True)
        install(temp_root="\x2fprivate/tmp")
        with tempfile.TemporaryDirectory(prefix="hw-home.", dir="\x2fprivate/tmp") as home:
            environment = dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE="1",
                PYTHONUSERBASE="\x2fUsers/penguinspecz/Library/Python/3.9")
            prefix = ["/usr/bin/python3", "-B", "-m", "unittest", "-v"]
            if args.failfast:
                prefix.append("--failfast")
            groups = [[selection] for selection in selections] if args.isolate else [selections]
            print(json.dumps({"command_prefix": prefix, "selectors": selections,
                "repeat": args.repeat, "HOME": home,
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUSERBASE": environment["PYTHONUSERBASE"],
                "isolated_selectors": args.isolate}), flush=True)
            root = Path(__file__).resolve().parent.parent
            if args.label:
                log = root / "docs/evidence" / ("loaded-wallclock-1-" + args.label + "-2026-10-25.log")
                print(json.dumps({"test_log": str(log)}), flush=True)
                with log.open("w") as output:
                    output.write(json.dumps({"load_pids": [child.pid for child in children],
                        "HOME": home, "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONUSERBASE": environment["PYTHONUSERBASE"], **limits}) + "\n")
                    statuses = []
                    for group in groups:
                        command = prefix + group * args.repeat
                        output.write(json.dumps({"command": command}) + "\n")
                        output.flush()
                        completed = run_child(command, env=environment, cwd=root,
                            stdout=output, stderr=subprocess.STDOUT)
                        statuses.append(completed.returncode)
                        print(json.dumps({"selectors": group, "test_exit_code": completed.returncode}), flush=True)
                print(log.read_text()[-16000:], flush=True)
            else:
                statuses = [run_child(prefix + group * args.repeat,
                    env=environment, cwd=root).returncode for group in groups]
            status = next((value for value in statuses if value), 0)
            print(json.dumps({"test_exit_code": status}), flush=True)
            return status
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            child.wait()
        cleanup = {"load_cleanup": [{"pid": child.pid,
            "returncode": child.returncode, "reaped": child.poll() is not None}
            for child in children]}
        print(json.dumps(cleanup), flush=True)
        if log is not None:
            with log.open("a") as output:
                output.write(json.dumps(cleanup) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
