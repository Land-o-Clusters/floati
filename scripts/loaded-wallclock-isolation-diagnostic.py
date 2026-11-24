#!/usr/bin/env python3
"""Locate the existing isolation child's alarm without altering its policy."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from fixture_finalizer import install, run_child


def diagnose_child():
    import unittest
    import tests  # Pin HOME before product imports.
    from floati import worker_isolation as isolation

    state = {"phase": "before_activation", "getpath_calls": 0}
    origins = {}

    def report(event):
        pid = os.getpid()
        if pid not in origins:
            origins[pid] = (time.monotonic(), time.process_time())
        started, cpu_started = origins[pid]
        print(json.dumps(dict(state, event=event, pid=os.getpid(),
            wall_seconds=time.monotonic() - started,
            cpu_seconds=time.process_time() - cpu_started)), flush=True)

    real_signal = signal.signal
    def observed_signal(number, handler):
        if number == signal.SIGALRM:
            def alarm(signum, frame):
                report("existing_alarm")
                handler(signum, frame)
            return real_signal(number, alarm)
        return real_signal(number, handler)
    signal.signal = observed_signal

    close = isolation._close_tenant_descriptors
    def observed_close(home):
        state["phase"] = "descriptor_close"
        state["soft_nofile"] = isolation._resource.getrlimit(isolation._resource.RLIMIT_NOFILE)[0]
        report("enter")
        result = close(home)
        report("exit")
        return result
    isolation._close_tenant_descriptors = observed_close

    if sys.platform == "darwin":
        fcntl_call = isolation._fcntl.fcntl
        def observed_fcntl(descriptor, operation, *args):
            if operation == isolation._F_GETPATH:
                state["getpath_calls"] += 1
            return fcntl_call(descriptor, operation, *args)
        isolation._fcntl.fcntl = observed_fcntl
        apply = isolation._apply_macos
        def observed_apply(policy):
            state["phase"] = "macos_activation"
            report("enter")
            result = apply(policy)
            report("exit")
            return result
        isolation._apply_macos = observed_apply

    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_adhoc_worker.BorrowedIsolationTests."
        "test_direct_borrowed_policy_denies_foreign_writes_and_allows_exact_slot")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    if sys.argv[1:] == ["--child"]:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        return diagnose_child()
    children = []
    log = None
    try:
        for _ in range(8):
            children.append(subprocess.Popen(["/usr/bin/python3", "-c", "while True: pass"]))
        print(json.dumps({"load_pids": [child.pid for child in children]}), flush=True)
        install(temp_root="\x2fprivate/tmp")
        with tempfile.TemporaryDirectory(prefix="hw-home.", dir="\x2fprivate/tmp") as home:
            environment = dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE="1",
                PYTHONUSERBASE="\x2fUsers/penguinspecz/Library/Python/3.9")
            command = ["/usr/bin/python3", "-B", str(Path(__file__).resolve()), "--child"]
            log = Path(__file__).resolve().parents[1] / "docs/evidence/loaded-wallclock-1-r2-isolation-diagnostic-2026-10-25.log"
            with log.open("w") as output:
                output.write(json.dumps({"load_pids": [child.pid for child in children],
                    "HOME": home, "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONUSERBASE": environment["PYTHONUSERBASE"]}) + "\n")
                output.flush()
                result = run_child(command, env=environment, stdout=output, stderr=subprocess.STDOUT)
            print(log.read_text(), flush=True)
            return result.returncode
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
