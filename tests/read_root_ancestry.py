"""Explicit host regressions; select tests.read_root_ancestry with unittest.

These require an existing root-owned ancestor above PID 1. They belong
outside default discovery because ordinary GUI launches need not have one.
The child mode reads its real ancestry, independently of the product.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from floati.errors import ProtocolRefusal
from floati.codex_wait_liveness import process_start_epoch
from floati.waiter_invocation import _host_ancestry, _native_parent
from tests.test_wd_3 import _ps_start_epoch


class RootOwnedAncestryTests(unittest.TestCase):
    def test_root_owned_process_start_matches_independent_native_witness(self):
        candidates = subprocess.run(
            ["/bin/ps", "-U", "root", "-o", "pid="],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        for value in candidates:
            pid = int(value)
            if pid <= 1:
                continue
            try:
                truth = _ps_start_epoch(pid)
            except (subprocess.CalledProcessError, AssertionError):
                continue  # Discovery can race an exit; never invent a PID.
            measured = process_start_epoch(pid)
            if measured is None:
                continue
            self.assertGreater(measured, 0)
            self.assertLess(abs(measured - truth), 1.0)
            parent = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "ppid="],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            self.assertEqual(int(parent), _native_parent(pid))
            break
        else:
            raise ProtocolRefusal(
                "root_owned_process_fixture_unavailable",
                "no live root-owned PID other than 1 publishes a native start time",
                remedy="run this regression on a host with an existing root-owned process",
            )

    def test_child_retains_every_real_ancestor_including_root_owned_edge(self):
        # The child obtains its OWN ancestry. No mocked parent, injected PID,
        # sudo, login, provider, or modification of an existing process.
        child = subprocess.run(
            ["/usr/bin/python3", "-B", str(Path(__file__).resolve())],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(0, child.returncode, child.stderr)
        witness = json.loads(child.stdout)
        self.assertTrue(any(row["uid"] == 0 for row in witness["expected"]))
        self.assertEqual(len(witness["expected"]), len(witness["actual"]))
        for expected, actual in zip(witness["expected"], witness["actual"]):
            with self.subTest(pid=expected["pid"]):
                self.assertEqual(expected["pid"], actual["pid"])
                self.assertEqual(expected["parent_pid"], actual["parent_pid"])
                self.assertGreater(actual["process_start_epoch"], 0)
                self.assertLess(abs(expected["process_start_epoch"] - actual["process_start_epoch"]), 1.0)


def main():
    table = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,uid="],
        capture_output=True, text=True, check=True,
    )
    population = {int(pid): (int(parent), int(uid))
                  for pid, parent, uid in (row.split() for row in table.stdout.splitlines())}
    pid, expected, seen = os.getppid(), [], set()
    while pid > 1:
        if pid in seen or len(expected) >= 64:
            raise AssertionError("independent native ancestry is cyclic or exceeds 64 edges")
        seen.add(pid)
        parent, uid = population[pid]
        expected.append({"pid": pid, "parent_pid": parent, "uid": uid,
                         "process_start_epoch": _ps_start_epoch(pid)})
        pid = parent
    if not any(row["uid"] == 0 for row in expected):
        raise ProtocolRefusal(
            "root_owned_ancestor_fixture_unavailable",
            "this child's real ancestry has no root-owned process other than PID 1",
            remedy="run this regression beneath an existing root-owned login or ssh ancestor",
        )
    print(json.dumps({"expected": expected, "actual": _host_ancestry()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
