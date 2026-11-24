#!/usr/bin/env python3
"""Observe one native ordinal 45 without changing the shipped eight-second test.

The sixty-second supervisor cap bounds this investigative measurement only.
Neither completion nor exit zero from this script qualifies the shipped test.
"""

import json
import os
from contextlib import nullcontext
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time

from fixture_finalizer import install


ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "docs/evidence/loaded-wallclock-1-r2-abrupt-diagnostic-2026-10-25.log"
MEASUREMENT_SECONDS = 60.0
SHIPPED_SECONDS = 8.0


# This observer delegates the native recorder before logging. Its last emitted
# ordinal is therefore 44 when the unmodified target recorder exits at 45.
# Writes go only to the child/server's owned stderr, outside the observed root.
OBSERVER_PREFIX = r'''
import json, os, time
from tests.test_bus_epoch_roll import _MutationEvent, _MutationProbe

_diagnostic_write = os.write
_diagnostic_init = _MutationProbe.__init__
_diagnostic_record = _MutationProbe._record

def _diagnostic_emit(value):
    _diagnostic_write(2, (json.dumps(value, sort_keys=True) + "\n").encode("utf-8"))

def _observed_probe_init(self, *args, **kwargs):
    _diagnostic_init(self, *args, **kwargs)
    self._diagnostic_wall = time.monotonic()
    self._diagnostic_cpu = time.process_time()
    _diagnostic_emit({"phase": "probe_started", "pid": os.getpid(),
                      "target_ordinal": self.abort_after})

def _observed_record(self, *args, **kwargs):
    previous = len(self.events)
    _diagnostic_record(self, *args, **kwargs)
    if len(self.events) != previous:
        event = self.events[-1]
        _diagnostic_emit({
            "phase": "mutation", "pid": os.getpid(),
            "ordinal": sum(isinstance(item, _MutationEvent) for item in self.events),
            "primitive": event.primitive, "target": event.target,
            "wall_seconds": time.monotonic() - self._diagnostic_wall,
            "cpu_seconds": time.process_time() - self._diagnostic_cpu,
        })

def _observed_waitpid(pid, options):
    _diagnostic_emit({"phase": "child_forked", "pid": pid})
    waited, status, usage = os.wait4(pid, options)
    _diagnostic_emit({
        "phase": "child_reaped", "pid": waited,
        "returncode": os.waitstatus_to_exitcode(status),
        "user_cpu_seconds": usage.ru_utime, "system_cpu_seconds": usage.ru_stime,
        "minor_faults": usage.ru_minflt, "major_faults": usage.ru_majflt,
        "input_blocks": usage.ru_inblock, "output_blocks": usage.ru_oublock,
    })
    return waited, status

_MutationProbe.__init__ = _observed_probe_init
_MutationProbe._record = _observed_record
os.waitpid = _observed_waitpid
'''


class MeasurementDeadline(Exception):
    pass


def records(path):
    if not path.exists():
        return []
    complete = path.read_bytes().split(b"\n")[:-1]
    return [json.loads(line) for line in complete if line]


def main():
    with LOG.open("w", encoding="utf-8") as log:
        def emit(value):
            line = json.dumps(value, sort_keys=True)
            log.write(line + "\n")
            log.flush()
            print(line, flush=True)

        hogs = []
        server = None
        fixture = None
        sample_reaped = False
        sample_started = None
        checkpoint = False
        install(temp_root="\x2fprivate/tmp")
        home_bank = tempfile.TemporaryDirectory(prefix="hw-home.", dir="\x2fprivate/tmp")
        try:
            # Keep HOME alive through the owned-process cleanup below, also
            # when the investigative cap interrupts an active native child.
            with nullcontext(home_bank.name) as home:
                # Pin before importing unittest or any native test/fixture.
                os.environ["HOME"] = home
                os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
                sys.dont_write_bytecode = True
                if str(ROOT) not in sys.path:
                    sys.path.insert(0, str(ROOT))
                from tests.test_bus_epoch_roll import (
                    GovernedBusEpochRollTests, _ABRUPT_FORK_SERVER_PROGRAM,
                )

                emit({"diagnostic_only": True, "qualifies_shipped_test": False,
                      "measurement_cap_seconds": MEASUREMENT_SECONDS,
                      "shipped_reply_bound_seconds": SHIPPED_SECONDS,
                      "ordinal": 45, "HOME": home,
                      "PYTHONDONTWRITEBYTECODE": "1"})
                for _ in range(8):
                    hogs.append(subprocess.Popen(
                        ["/usr/bin/python3", "-c", "while True: pass"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL))
                emit({"load_pids": [child.pid for child in hogs]})

                fixture = GovernedBusEpochRollTests("runTest")
                fixture.setUp()
                bank = fixture.base / "abrupt-diagnostic"
                bank.mkdir()
                server_error = bank / "server.stderr"
                child_error = bank / "child.stderr"
                child_error.touch()
                contract_root = fixture._new_root(bank / "server-contract")
                cap_deadline = time.monotonic() + MEASUREMENT_SECONDS
                with server_error.open("wb") as errors:
                    server = subprocess.Popen(
                        ["/usr/bin/python3", "-B", "-c",
                         OBSERVER_PREFIX + _ABRUPT_FORK_SERVER_PROGRAM,
                         str(contract_root.path)],
                        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=errors, start_new_session=True)
                emit({"server_pid": server.pid})
                buffered = bytearray()

                def reply():
                    nonlocal checkpoint
                    assert server.stdout is not None
                    while b"\n" not in buffered:
                        now = time.monotonic()
                        if sample_started is not None and not checkpoint and (
                            now - sample_started >= SHIPPED_SECONDS
                        ):
                            progress = records(child_error)
                            mutations = [row for row in progress if row.get("phase") == "mutation"]
                            emit({"phase": "eight_second_observation",
                                  "sample_wall_seconds": now - sample_started,
                                  "last_mutation": mutations[-1] if mutations else None,
                                  "progress_records": len(progress),
                                  "server_running": server.poll() is None,
                                  "continues_same_measurement": True,
                                  "qualifies_shipped_test": False})
                            checkpoint = True
                        remaining = cap_deadline - now
                        if remaining <= 0:
                            raise MeasurementDeadline("diagnostic supervisor reached its sixty-second cap")
                        if sample_started is not None and not checkpoint:
                            remaining = min(remaining, max(0.0,
                                sample_started + SHIPPED_SECONDS - now))
                        ready, _, _ = select.select([server.stdout], [], [], remaining)
                        if not ready:
                            continue
                        chunk = os.read(server.stdout.fileno(), 65536)
                        if not chunk:
                            raise RuntimeError("native diagnostic server closed before its reply")
                        buffered.extend(chunk)
                    line, _, rest = buffered.partition(b"\n")
                    buffered[:] = rest
                    return json.loads(line)

                if reply() != {"ready": True}:
                    raise RuntimeError("native diagnostic server did not announce readiness")

                def measured_child(mode, root, arg):
                    nonlocal sample_started, sample_reaped
                    if (mode, arg) != ("ordinal", "45"):
                        raise AssertionError("diagnostic owns exactly ordinal 45")
                    assert server.stdin is not None
                    sample_started = time.monotonic()
                    request = {"mode": mode, "root": str(root.path), "arg": arg,
                               "stderr": str(child_error)}
                    server.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                    server.stdin.flush()
                    result = reply()
                    elapsed = time.monotonic() - sample_started
                    progress = records(child_error)
                    reaped = [row for row in records(server_error)
                              if row.get("phase") == "child_reaped"]
                    sample_reaped = bool(reaped)
                    emit({"phase": "native_completion", "sample_wall_seconds": elapsed,
                          "eight_second_checkpoint_reached": checkpoint,
                          "reply": result, "child_rusage": reaped,
                          "mutation_progress": progress,
                          "qualifies_shipped_test": False})
                    return result["returncode"], child_error.read_text(encoding="utf-8")

                # The original case constructs its original root and performs
                # every real classification, Doctor and recovered-tree check.
                fixture._abrupt_ordinal_case(measured_child, 45)
                emit({"native_ordinal_45_recovery_assertions": "passed",
                      "sample_child_reaped": sample_reaped,
                      "qualifies_shipped_test": False})
                return 0
        except BaseException as error:
            emit({"diagnostic_error": type(error).__name__, "detail": str(error),
                  "qualifies_shipped_test": False})
            return 1
        finally:
            try:
                if server is not None:
                    if server.poll() is None:
                        try:
                            os.killpg(server.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(server.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        server.wait()
                    if not sample_reaped:
                        # On a capped measurement the server may have exited
                        # before reaping its fork. Close its exact owned
                        # session as well; no unrelated process is selected.
                        try:
                            os.killpg(server.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    for stream in (server.stdin, server.stdout):
                        if stream is not None:
                            stream.close()
                    emit({"server_cleanup": {"pid": server.pid,
                          "returncode": server.returncode, "collected": True},
                          "sample_child_reaped": sample_reaped})
            finally:
                try:
                    if fixture is not None:
                        fixture.doCleanups()
                finally:
                    try:
                        for child in hogs:
                            if child.poll() is None:
                                child.terminate()
                        for child in hogs:
                            child.wait()
                        emit({"load_cleanup": [{"pid": child.pid, "returncode": child.returncode,
                              "reaped": child.poll() is not None} for child in hogs]})
                    finally:
                        home_bank.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
