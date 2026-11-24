"""Synthetic Trace2 grammar controls; these rows are never live testimony."""
from copy import deepcopy
import unittest

from tests.g1_trace_fixture import detached_witness


ROOT_SID = "20261008T000000.000000Z-H00000000-P00000064"
MAINTENANCE_SID = ROOT_SID + "/20261008T000000.000001Z-H00000000-P00000065"
GC_SID = MAINTENANCE_SID + "/20261008T000000.000002Z-H00000000-P00000066"
ROOT_ARGV = ["/usr/bin/git", "-c", "gc.auto=1", "-c", "gc.autoDetach=true",
    "commit", "-q", "-m", "gate trigger"]


def event(kind, sid, **fields):
    return {"event": kind, "sid": sid, **fields}


def trace(version):
    modern = version == "modern"
    fork = MAINTENANCE_SID if modern else GC_SID
    repack = fork + "/20261008T000000.000003Z-H00000000-P00000067"
    rows = [
        event("start", ROOT_SID, argv=ROOT_ARGV.copy()),
        event("cmd_name", ROOT_SID, name="commit"),
        event("child_start", ROOT_SID, child_id=0,
            argv=["git", "maintenance", "run", "--auto", "--detach"] if modern else
                ["git", "maintenance", "run", "--auto", "--no-quiet"]),
        event("start", MAINTENANCE_SID,
            argv=["git", "maintenance", "run", "--auto", "--detach"] if modern else
                ["git", "maintenance", "run", "--auto", "--no-quiet"]),
        event("cmd_name", MAINTENANCE_SID, name="maintenance"),
    ]
    if modern:
        rows.append(event("region_enter", fork, category="maintenance", label="detach", nesting=1))
    else:
        rows.extend([
            event("child_start", MAINTENANCE_SID, child_id=1, argv=["git", "gc", "--auto", "--quiet"]),
            event("start", GC_SID, argv=["git", "gc", "--auto", "--quiet"]),
            event("cmd_name", GC_SID, name="gc"),
        ])
    rows.extend([
        event("exit", fork, code=0, file="setup.c"),
        event("atexit", fork, code=0),
    ])
    if modern:
        rows.append(event("region_leave", fork, category="maintenance", label="detach", nesting=1))
    rows.extend([
        event("child_start", fork, child_id=2, argv=["git", "repack", "-d", "-l"]),
        event("start", repack, argv=["git", "repack", "-d", "-l"]),
        event("cmd_name", repack, name="repack"),
        event("exit", repack, code=0, file="git.c"),
        event("atexit", repack, code=0),
        event("child_exit", fork, child_id=2, pid=103, code=0),
        event("atexit", fork, code=0),
    ])
    if not modern:
        rows.extend([
            event("child_exit", MAINTENANCE_SID, child_id=1, pid=102, code=0),
            event("exit", MAINTENANCE_SID, code=0, file="git.c"),
            event("atexit", MAINTENANCE_SID, code=0),
        ])
    rows.extend([
        event("child_exit", ROOT_SID, child_id=0, pid=101, code=0),
        event("exit", ROOT_SID, code=0, file="git.c"),
        event("atexit", ROOT_SID, code=0),
    ])
    return rows, fork, repack


class DetachedTraceContractTests(unittest.TestCase):
    def reject_both(self, change):
        for version in ("modern", "legacy"):
            with self.subTest(version=version):
                rows, fork, repack = trace(version)
                change(rows, fork, repack)
                with self.assertRaises(ValueError):
                    detached_witness(rows)

    def test_complete_modern_native_detach_and_repack_lineage_is_accepted(self):
        rows, _, _ = trace("modern")
        before = deepcopy(rows)
        witness = detached_witness(rows)
        self.assertIs(type(witness), dict)
        self.assertTrue(witness)
        self.assertEqual(before, rows)

    def test_complete_legacy_gc_auto_fork_and_repack_lineage_is_accepted(self):
        rows, _, _ = trace("legacy")
        self.assertFalse(any(row["event"].startswith("region_") for row in rows))
        witness = detached_witness(rows)
        self.assertIs(type(witness), dict)
        self.assertTrue(witness)

    def test_spawn_only_rows_cannot_testify_to_detached_execution(self):
        self.reject_both(lambda rows, fork, repack:
            rows.__setitem__(slice(None), [row for row in rows
                if row["sid"] == ROOT_SID and row["event"] in {"start", "cmd_name", "child_start"}]))

    def test_root_must_be_the_exact_trigger_commit(self):
        for argv in (["git", "log", "-m", "gate trigger"],
                ["git", "commit", "-m", "gate seed"], ["git", "commit", "gate trigger"]):
            with self.subTest(argv=argv):
                self.reject_both(lambda rows, fork, repack: rows[0].update(argv=argv))

    def test_root_child_id_must_match_maintenance_launcher_exit(self):
        def change(rows, fork, repack):
            next(row for row in rows if row["sid"] == ROOT_SID and row["event"] == "child_exit")["child_id"] = 9
        self.reject_both(change)

    def test_maintenance_cannot_borrow_a_sid_without_the_root_child_start(self):
        self.reject_both(lambda rows, fork, repack:
            rows.__setitem__(slice(None), [row for row in rows
                if not (row["sid"] == ROOT_SID and row["event"] == "child_start")]))

    def test_maintenance_must_have_its_own_native_start(self):
        self.reject_both(lambda rows, fork, repack:
            rows.__setitem__(slice(None), [row for row in rows
                if not (row["sid"] == MAINTENANCE_SID and row["event"] == "start")]))

    def test_root_launcher_pid_must_match_maintenance_sid_hex_pid(self):
        def change(rows, fork, repack):
            next(row for row in rows if row["sid"] == ROOT_SID and row["event"] == "child_exit")["pid"] = 104
        self.reject_both(change)

    def test_maintenance_start_must_descend_from_the_trigger_root_sid(self):
        def change(rows, fork, repack):
            for row in rows:
                if row["sid"].startswith(MAINTENANCE_SID):
                    row["sid"] = row["sid"].replace(ROOT_SID, "20261008T000001.000000Z-H00000000-P00000068", 1)
        self.reject_both(change)

    def test_maintenance_command_name_cannot_be_borrowed_from_another_command(self):
        def change(rows, fork, repack):
            next(row for row in rows if row["sid"] == MAINTENANCE_SID and row["event"] == "cmd_name")["name"] = "status"
        self.reject_both(change)

    def test_failed_maintenance_launcher_is_not_a_successful_lineage_edge(self):
        def change(rows, fork, repack):
            next(row for row in rows if row["sid"] == ROOT_SID and row["event"] == "child_exit")["code"] = 1
        self.reject_both(change)

    def test_fork_exit_must_be_the_native_setup_exit(self):
        def change(rows, fork, repack):
            next(row for row in rows if row["sid"] == fork and row["event"] == "exit")["file"] = "git.c"
        self.reject_both(change)

    def test_fork_setup_exit_must_succeed(self):
        def change(rows, fork, repack):
            next(row for row in rows if row["sid"] == fork and row["event"] == "exit")["code"] = 1
        self.reject_both(change)

    def test_two_atexit_records_without_the_fork_setup_exit_are_insufficient(self):
        self.reject_both(lambda rows, fork, repack:
            rows.__setitem__(slice(None), [row for row in rows
                if not (row["sid"] == fork and row["event"] == "exit")]))

    def test_missing_second_fork_atexit_cannot_prove_two_process_lifetimes(self):
        def change(rows, fork, repack):
            rows.remove([row for row in rows if row["sid"] == fork and row["event"] == "atexit"][-1])
        self.reject_both(change)

    def test_both_fork_atexit_records_must_succeed(self):
        def change(rows, fork, repack):
            [row for row in rows if row["sid"] == fork and row["event"] == "atexit"][-1]["code"] = 1
        self.reject_both(change)

    def test_repack_must_have_a_real_descendant_start(self):
        self.reject_both(lambda rows, fork, repack:
            rows.__setitem__(slice(None), [row for row in rows
                if not (row["sid"] == repack and row["event"] == "start")]))

    def test_repack_must_have_its_own_native_command_name(self):
        def change(rows, fork, repack):
            next(row for row in rows if row["sid"] == repack and row["event"] == "cmd_name")["name"] = "status"
        self.reject_both(change)

    def test_repack_without_successful_atexit_is_not_completed_execution(self):
        def change(rows, fork, repack):
            next(row for row in rows if row["sid"] == repack and row["event"] == "atexit")["code"] = 1
        self.reject_both(change)

    def test_repack_missing_atexit_cannot_use_its_exit_alone(self):
        self.reject_both(lambda rows, fork, repack:
            rows.__setitem__(slice(None), [row for row in rows
                if not (row["sid"] == repack and row["event"] == "atexit")]))

    def test_repack_parent_must_collect_the_same_child_id(self):
        def change(rows, fork, repack):
            next(row for row in rows if row["sid"] == fork and row["event"] == "child_exit")["child_id"] = 9
        self.reject_both(change)

    def test_repack_without_its_parent_child_exit_is_not_a_joined_execution(self):
        self.reject_both(lambda rows, fork, repack:
            rows.__setitem__(slice(None), [row for row in rows
                if not (row["sid"] == fork and row["event"] == "child_exit")]))

    def test_repack_parent_must_collect_the_same_native_sid_pid(self):
        def change(rows, fork, repack):
            next(row for row in rows if row["sid"] == fork and row["event"] == "child_exit")["pid"] = 104
        self.reject_both(change)

    def test_failed_repack_child_exit_is_not_a_successful_execution_edge(self):
        def change(rows, fork, repack):
            next(row for row in rows if row["sid"] == fork and row["event"] == "child_exit")["code"] = 1
        self.reject_both(change)

    def test_unrelated_successful_repack_cannot_repair_missing_native_descendant(self):
        def change(rows, fork, repack):
            other = "20261008T000001.000000Z-H00000000-P00000068"
            for row in rows:
                if row["sid"] == repack:
                    row["sid"] = other
        self.reject_both(change)

    def test_modern_detach_requires_both_native_region_boundaries(self):
        for kind in ("region_enter", "region_leave"):
            with self.subTest(missing=kind):
                rows, fork, _ = trace("modern")
                rows = [row for row in rows if not (row["sid"] == fork and row["event"] == kind)]
                with self.assertRaises(ValueError):
                    detached_witness(rows)

    def test_legacy_gc_must_be_auto_with_its_own_matched_lineage_edge(self):
        for field in ("auto", "child_id", "pid", "cmd_name"):
            with self.subTest(field=field):
                rows, _, _ = trace("legacy")
                if field == "auto":
                    for row in rows:
                        if row["event"] == "start" and row["sid"] == GC_SID:
                            row["argv"] = ["git", "gc"]
                        if row["event"] == "child_start" and row["sid"] == MAINTENANCE_SID:
                            row["argv"] = ["git", "gc"]
                elif field == "cmd_name":
                    next(row for row in rows if row["sid"] == GC_SID and row["event"] == "cmd_name")["name"] = "status"
                else:
                    next(row for row in rows if row["sid"] == MAINTENANCE_SID and row["event"] == "child_exit")[field] = 9
                with self.assertRaises(ValueError):
                    detached_witness(rows)


if __name__ == "__main__":
    unittest.main()
