"""Opt-in projection observations; original tests and producers remain authoritative.

Imported only by the three closed check-harbor-parallel-r2 aliases. No cache
inspection, cache warming, exporter-global patching, or child instrumentation.
"""

import base64
from contextlib import ExitStack
import dataclasses
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ALIASES = {
    "projection-copy-cost": ("tests.test_copy_ledger", 8, 2),
    "projection-no-listener-cost": ("tests.test_no_listener_fence", 9, 1),
    "projection-fixture-cost": ("tests.test_fixture_placement", 3, 1),
}
RAW_METHOD = "tests.test_copy_ledger.CopyLedgerTests.test_the_ledger_pin_predicts_a_real_projection"
COPY_PIN = "tests.test_copy_ledger.CopyLedgerTests.test_generated_copy_ledger_matches_all_static_help_surfaces"
GIT_STEPS = (
    ["init", "-q", "--initial-branch=main"],
    ["config", "user.name", "fixture"],
    ["config", "user.email", "fixture@example.invalid"],
    ["add", "."], ["commit", "-q", "-m", "projection fixture"],
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def exception(exc):
    return {"type": type(exc).__module__ + "." + type(exc).__qualname__, "message": str(exc)}


def stream_value(value):
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return value


def file_row(path, relative):
    info = path.lstat()
    row = {"path": relative, "mode": stat.S_IMODE(info.st_mode)}
    if stat.S_ISREG(info.st_mode):
        data = path.read_bytes()
        row.update(type="file", bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    elif stat.S_ISDIR(info.st_mode):
        row["type"] = "directory"
    elif stat.S_ISLNK(info.st_mode):
        row.update(type="symlink", target=os.readlink(path))
    else:
        row["type"] = "other"
    return row


def selected_ids(suite):
    if isinstance(suite, unittest.TestSuite):
        return [item for test in suite for item in selected_ids(test)]
    return [suite.id()]


class ProjectionObserver:
    def __init__(self, root, output, alias, suite):
        self.root, self.output, self.alias = root, output, alias
        self.module = importlib.import_module(ALIASES[alias][0])
        self.inventory = importlib.import_module("tests.export_inventory")
        self.original_run = subprocess.run
        self.active_test = None
        self.stack = []
        self.active_originals = set()
        self.materialisations = []
        self.destinations = {}
        self.spans = []
        self.native = []
        self.cleanup = []
        self.methods = []
        self.errors = []
        self.bindings = []
        self.original_bindings = []
        self.external_outcomes = []
        self.selection = selected_ids(suite)

    def begin(self, kind, **metadata):
        row = {"id": len(self.spans), "kind": kind, "test_id": self.active_test,
               "parent": self.stack[-1]["id"] if self.stack else None,
               "immediate_children_ns": 0, **metadata}
        self.spans.append(row)
        self.stack.append(row)
        row["start_ns"] = time.monotonic_ns()
        return row

    def end(self, row, status, error=None):
        row["end_ns"] = time.monotonic_ns()
        row["elapsed_ns"] = row["end_ns"] - row["start_ns"]
        row["status"] = status
        if error is not None:
            row["exception"] = exception(error)
        assert self.stack.pop() is row
        row["self_ns"] = row["elapsed_ns"] - row["immediate_children_ns"]
        if self.stack:
            self.stack[-1]["immediate_children_ns"] += row["elapsed_ns"]

    def call(self, kind, original, args, kwargs, **metadata):
        row = self.begin(kind, **metadata)
        try:
            value = original(*args, **kwargs)
        except BaseException as exc:
            self.end(row, "raised", exc)
            raise
        else:
            self.end(row, "returned")
            return value, row

    def safe_metadata(self, operation):
        # Observation failures invalidate the sidecar, never replace a native
        # producer exception or prevent the original operation from running.
        try:
            return operation()
        except Exception as exc:
            self.errors.append(exception(exc))
            return None

    def git_read(self, arguments, root):
        value, row = self.call("diagnostic_git_read", self.original_run,
                               (["/usr/bin/git", *arguments],),
                               {"cwd": root, "capture_output": True, "check": True})
        row.update(argv=["/usr/bin/git", *arguments], cwd=str(root), returncode=value.returncode)
        return value.stdout

    def source_identity(self):
        row = self.begin("source_attestation")
        try:
            names = self.git_read(["ls-files", "-z", "--cached"], self.root).decode().split("\0")
            tracked = [file_row(self.root / name, name) for name in sorted(set(names) - {""})]
            untracked = self.git_read(["ls-files", "-z", "--others", "--exclude-standard"], self.root)
            python = [file_row(self.root / name, name) for name in sorted(
                name for name in untracked.decode().split("\0") if name.endswith(".py"))]
            result = {"head": self.git_read(["rev-parse", "HEAD"], self.root).decode().strip(),
                      "status_base64": base64.b64encode(self.git_read(
                          ["status", "--porcelain=v1", "-z", "--untracked-files=all"], self.root)).decode(),
                      "staged_entries_sha256": hashlib.sha256(self.git_read(
                          ["ls-files", "--stage", "-z"], self.root)).hexdigest(),
                      "tracked": tracked, "tracked_digest": digest(tracked),
                      "untracked_python": python}
        except BaseException as exc:
            self.end(row, "raised", exc)
            raise
        self.end(row, "returned")
        return result

    def tree_identity(self, destination):
        row = self.begin("tree_attestation", destination=str(destination))
        try:
            rows = [file_row(destination, ".")]
            for parent, directories, files in os.walk(destination, followlinks=False):
                directories[:] = sorted(name for name in directories if name != ".git")
                for name in sorted(directories + files):
                    path = Path(parent) / name
                    rows.append(file_row(path, path.relative_to(destination).as_posix()))
            rows.sort(key=lambda item: item["path"])
            index = destination / ".git" / "index"
            result = {"entries": rows, "digest": digest(rows),
                      "head": self.git_read(["rev-parse", "HEAD"], destination).decode().strip(),
                      "index": file_row(index, ".git/index")}
        except BaseException as exc:
            self.end(row, "raised", exc)
            raise
        self.end(row, "returned")
        return result

    def register(self, destination, kind):
        destination = Path(destination).absolute()
        fixtures = Path(os.environ["FLOATI_TEST_TEMP_ROOT"])
        if destination.resolve() != destination or fixtures not in destination.parents:
            raise ValueError("projection observation destination escaped the owned fixture root")
        key = str(destination)
        if key not in self.destinations:
            self.destinations[key] = {"path": key, "kind": kind, "test_id": self.active_test,
                                      "aggregates": {}}
        return self.destinations[key]

    def aggregate_call(self, destination, kind, original, args, kwargs, describe, input_bytes=0):
        bucket = destination["aggregates"].setdefault(kind, {
            "calls": 0, "returned": 0, "raised": 0, "elapsed_ns": 0,
            "parent_span": self.stack[-1]["id"] if self.stack else None,
            "input_bytes": 0, "output_bytes": 0, "changed_bytes_calls": 0, "exceptions": []})
        bucket["input_bytes"] += input_bytes
        began = time.monotonic_ns()
        try:
            value = original(*args, **kwargs)
        except BaseException as exc:
            elapsed = time.monotonic_ns() - began
            bucket["raised"] += 1
            bucket["exceptions"].append(exception(exc))
            raise
        else:
            elapsed = time.monotonic_ns() - began
            bucket["returned"] += 1
            self.safe_metadata(lambda: describe(bucket, value))
            return value
        finally:
            bucket["calls"] += 1
            bucket["elapsed_ns"] += elapsed
            if self.stack:
                self.stack[-1]["immediate_children_ns"] += elapsed

    def inventory_wrapper(self, name, original):
        def wrapped(*args, **kwargs):
            if original in self.active_originals:
                return original(*args, **kwargs)
            self.active_originals.add(original)
            try:
                root = kwargs.get("root", args[0] if args and name != "classify_inventory" else self.root)
                value, row = self.call(name, original, args, kwargs, root=str(root))
                # These original helpers return tuples; never consume their input iterator.
                self.safe_metadata(lambda: row.update(path_count=len(value), paths_digest=digest(value)))
                return value
            finally:
                self.active_originals.remove(original)
        return wrapped

    def materialise_wrapper(self, original):
        def wrapped(*args, **kwargs):
            destination = self.safe_metadata(lambda: self.register(
                args[0] if args else kwargs["destination"], "strict-adapted"))
            if destination is None:
                return original(*args, **kwargs)
            self.materialisations.append(destination)
            try:
                value, row = self.call("strict_materialisation", original, args, kwargs,
                                       destination=destination["path"])
                self.safe_metadata(lambda: row.update(path_count=len(value), paths_digest=digest(value)))
                return value
            finally:
                self.materialisations.pop()
        return wrapped

    def adapter_factory_wrapper(self, original):
        def wrapped(*args, **kwargs):
            adapter, _row = self.call("adapter_factory", original, args, kwargs)
            destination = self.materialisations[-1] if self.materialisations else None
            if destination is None:
                return adapter

            def delegated(relative, data):
                def describe(bucket, value):
                    bucket["output_bytes"] += len(value)
                    bucket["changed_bytes_calls"] += int(value != data)
                return self.aggregate_call(destination, "adapter_service", adapter,
                                           (relative, data), {}, describe, input_bytes=len(data))
            return delegated
        return wrapped

    def write_wrapper(self, original):
        def wrapped(path, data):
            if not self.materialisations:
                return original(path, data)
            destination = self.materialisations[-1]
            if Path(destination["path"]) not in Path(path).absolute().parents:
                return original(path, data)
            def describe(bucket, value):
                bucket["output_bytes"] += value
            return self.aggregate_call(destination, "target_write", original,
                                       (path, data), {}, describe, input_bytes=len(data))
        return wrapped

    def copy_wrapper(self, original):
        def wrapped(src, dst, *args, **kwargs):
            if self.active_test != RAW_METHOD:
                return original(src, dst, *args, **kwargs)
            def identify():
                relative = Path(src).relative_to(self.root)
                destination_path = Path(dst)
                for _part in relative.parts:
                    destination_path = destination_path.parent
                return self.register(destination_path, "raw")
            destination = self.safe_metadata(identify)
            if destination is None:
                return original(src, dst, *args, **kwargs)
            def describe(bucket, value):
                bucket["output_bytes"] += Path(dst).stat().st_size
                bucket["input_bytes"] += Path(src).stat().st_size
            return self.aggregate_call(destination, "raw_copy", original,
                                       (src, dst, *args), kwargs, describe)
        return wrapped

    def survey_wrapper(self, name, original):
        def wrapped(*args, **kwargs):
            roots = kwargs.get("root", args[0] if args and name not in ("_private",) else self.root)
            if name == "_private":
                roots = kwargs.get("root", args[1] if len(args) > 1 else self.root)
            root_list = roots if isinstance(roots, list) else [roots]
            absolute = [str(Path(root).absolute()) for root in root_list]
            value, row = self.call(name, original, args, kwargs, roots=absolute,
                                   root_scope=["product" if self.root == Path(root) or
                                               self.root in Path(root).parents else "planted"
                                               for root in absolute])
            def describe():
                if dataclasses.is_dataclass(value):
                    row["survey"] = dataclasses.asdict(value)
                elif name == "measure_network_surface":
                    row["cardinalities"] = [len(part) for part in value]
                    row["network_import_members"] = sum(len(part) for part in value[1].values())
                else:
                    row["count"] = len(value)
            self.safe_metadata(describe)
            return value
        return wrapped

    def run_wrapper(self, *args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        cwd = Path(kwargs.get("cwd") or Path.cwd()).absolute()
        destination = self.destinations.get(str(cwd))
        command = [str(item) for item in argv] if isinstance(argv, (list, tuple)) else argv
        kind = "other_native"
        if destination and isinstance(command, list) and command[:1] == ["/usr/bin/git"]:
            prefix = ["-c", "gc.auto=0", "-c", "gc.autoDetach=false"] if self.alias == "projection-no-listener-cost" else []
            if command[1:] in [prefix + step for step in GIT_STEPS]:
                kind = "setup_git"
        elif destination and isinstance(command, list) and command[:3] == [sys.executable, "-m", "unittest"]:
            kind = "fence_child"
        elif cwd == self.root and command == ["python3", "-m", "floati.copy"]:
            kind = "copy_generator"
        elif command == ["git", "ls-files", "-z", "--cached"]:
            kind = "inventory_native"
        before = self.safe_metadata(lambda: self.tree_identity(cwd)) if kind == "fence_child" else None
        row = self.begin(kind, argv=command, cwd=str(cwd))
        self.native.append(row)
        try:
            value = self.original_run(*args, **kwargs)
        except BaseException as exc:
            self.end(row, "raised", exc)
            if isinstance(exc, subprocess.CalledProcessError):
                row.update(returncode=exc.returncode, stdout=stream_value(exc.stdout), stderr=stream_value(exc.stderr))
            raise
        else:
            self.end(row, "returned")
            row.update(returncode=value.returncode, stdout=stream_value(value.stdout), stderr=stream_value(value.stderr))
            return value
        finally:
            if kind == "fence_child":
                after = self.safe_metadata(lambda: self.tree_identity(cwd))
                row.update(tree_before=before, tree_after=after,
                           tree_unchanged=before is not None and before == after)
                if before is not None and after is not None:
                    left = {item["path"]: item for item in before["entries"]}
                    right = {item["path"]: item for item in after["entries"]}
                    row["namespace_changes"] = {
                        "added": sorted(right.keys() - left.keys()),
                        "removed": sorted(left.keys() - right.keys()),
                        "changed": sorted(key for key in left.keys() & right.keys() if left[key] != right[key])}

    def cleanup_wrapper(self, original):
        def wrapped(temporary, *args, **kwargs):
            destinations = [row for row in self.destinations.values()
                            if Path(row["path"]).parent == Path(temporary.name)]
            if not destinations:
                return original(temporary, *args, **kwargs)
            row = self.begin("owned_cleanup", temporary=str(temporary.name),
                             destinations=[item["path"] for item in destinations])
            self.cleanup.append(row)
            try:
                value = original(temporary, *args, **kwargs)
            except BaseException as exc:
                self.end(row, "raised", exc)
                raise
            else:
                self.end(row, "returned")
                return value
            finally:
                row["temporary_absent"] = not os.path.lexists(temporary.name)
                row["destinations_absent"] = all(not os.path.lexists(item["path"]) for item in destinations)
        return wrapped

    def patches(self, stack):
        def patch(module, name, wrapper):
            original = getattr(module, name)
            self.original_bindings.append((module, name, original))
            self.bindings.append({"owner": getattr(module, "__name__", str(module)),
                                  "name": name, "original_id": id(original),
                                  "original_module": original.__module__,
                                  "original_qualname": original.__qualname__})
            stack.enter_context(mock.patch.object(module, name, wrapper))

        for name in ("tracked_files", "classify_inventory", "export_include_set"):
            original = getattr(self.inventory, name)
            wrapper = self.inventory_wrapper(name, original)
            for module in (self.inventory, self.module):
                if getattr(module, name, None) is original:
                    patch(module, name, wrapper)
        patch(self.module, "materialise_adapted_tree", self.materialise_wrapper(self.module.materialise_adapted_tree))
        patch(self.inventory, "_adapted_bytes_source", self.adapter_factory_wrapper(self.inventory._adapted_bytes_source))
        patch(Path, "write_bytes", self.write_wrapper(Path.write_bytes))
        patch(shutil, "copyfile", self.copy_wrapper(shutil.copyfile))
        patch(subprocess, "run", self.run_wrapper)
        patch(tempfile.TemporaryDirectory, "cleanup", self.cleanup_wrapper(tempfile.TemporaryDirectory.cleanup))
        for name in ("survey", "_private", "measure_network_surface", "_built_argv_egress_sites"):
            if hasattr(self.module, name):
                patch(self.module, name, self.survey_wrapper(name, getattr(self.module, name)))

    def result_class(self, base):
        observer = self
        class Result(base):
            def mark(self, test, outcome, **extra):
                if observer.active_test == test.id() and hasattr(self, "phase"):
                    self.phase.update(outcome=outcome, **extra)
                else:
                    observer.external_outcomes.append({"test_id": test.id(), "outcome": outcome, **extra})

            def startTest(self, test):
                super().startTest(test)
                observer.active_test = test.id()
                self.phase = observer.begin("outer_method")
                observer.methods.append(self.phase)

            def stopTest(self, test):
                try:
                    super().stopTest(test)
                finally:
                    observer.end(self.phase, "completed")
                    observer.active_test = None

            def addSuccess(self, test):
                super().addSuccess(test)
                self.mark(test, "success")

            def addError(self, test, err):
                super().addError(test, err)
                self.mark(test, "error")

            def addFailure(self, test, err):
                super().addFailure(test, err)
                self.mark(test, "failure")

            def addSkip(self, test, reason):
                super().addSkip(test, reason)
                self.mark(test, "skip", skip_reason=reason)
        return Result

    def validate(self, result, before, after):
        expected_methods, expected_trees = ALIASES[self.alias][1:]
        fences = [row for row in self.native if row["kind"] == "fence_child"]
        git = [row for row in self.native if row["kind"] == "setup_git"]
        expected_selectors = (["tests.test_fixture_placement"] if self.alias == "projection-fixture-cost"
                              else ["-v", *self.module.PROJECTED_FENCE_IDS] if self.alias == "projection-no-listener-cost"
                              else [COPY_PIN])
        expected_tests = 3 if self.alias == "projection-fixture-cost" else 6 if self.alias == "projection-no-listener-cost" else 1
        checks = {
            "source_identity_unchanged": before is not None and before == after,
            "observer_has_no_metadata_errors": not self.errors,
            "original_bindings_restored": all(getattr(module, name) is original
                                               for module, name, original in self.original_bindings),
            "exact_outer_count": result.testsRun == expected_methods == len(self.methods) == len(self.selection),
            "exact_outer_order": [row["test_id"] for row in self.methods] == self.selection,
            "outer_all_successful": result.wasSuccessful() and not result.skipped and all(row.get("outcome") == "success" for row in self.methods),
            "independent_destinations": len(self.destinations) == expected_trees,
            "raw_and_strict_preparations_retained": sorted(row["kind"] for row in self.destinations.values()) ==
                (["raw", "strict-adapted"] if self.alias == "projection-copy-cost" else ["strict-adapted"]),
            "setup_git_count": len(git) == expected_trees * 5,
            "setup_git_returns": all(row.get("returncode") == 0 and row["status"] == "returned" for row in git),
            "four_tree_share_of_fences": len(fences) == expected_trees,
            "fence_native_argv": all(row["argv"] == [sys.executable, "-m", "unittest", *expected_selectors] for row in fences),
            "fence_native_returns": all(row.get("returncode") == 0 and row["status"] == "returned" for row in fences),
            "fence_summaries": all(re.search(r"(?m)^Ran " + str(expected_tests) + r" tests? in ", row.get("stderr", "")) and
                                    re.search(r"(?m)^OK \(skipped=2\)$" if self.alias == "projection-fixture-cost" else r"(?m)^OK$", row.get("stderr", "")) for row in fences),
            "fence_trees_unchanged": all(row.get("tree_unchanged") for row in fences),
            "owned_cleanup": len(self.cleanup) == expected_trees and all(row["status"] == "returned" and row["temporary_absent"] and row["destinations_absent"] for row in self.cleanup),
            "copy_generator_retained": len([row for row in self.native if row["kind"] == "copy_generator" and row.get("returncode") == 0]) == (1 if self.alias == "projection-copy-cost" else 0),
        }
        for destination in self.destinations.values():
            key = destination["path"]
            prefix = ["-c", "gc.auto=0", "-c", "gc.autoDetach=false"] if self.alias == "projection-no-listener-cost" else []
            checks["git_order:" + key] = [row["argv"] for row in git if row["cwd"] == key] == [["/usr/bin/git", *prefix, *step] for step in GIT_STEPS]
            kinds = ("raw_copy",) if destination["kind"] == "raw" else ("adapter_service", "target_write")
            checks["delegated_file_work:" + key] = all(
                destination["aggregates"].get(kind, {}).get("calls", 0) > 0 and
                destination["aggregates"][kind]["raised"] == 0 for kind in kinds)
        if self.alias == "projection-fixture-cost":
            checks["two_source_surveys_and_private_checks"] = all(
                sum(row["kind"] == kind for row in self.spans) == 2 for kind in ("survey", "_private"))
        if self.alias == "projection-no-listener-cost":
            for index, row in enumerate(fences):
                observed = []
                for method, owner in re.findall(r"^(test_\w+) \(([^)]+)\)", row.get("stderr", ""), re.MULTILINE):
                    if owner.endswith("." + method):
                        owner = owner[:-(len(method) + 1)]
                    observed.append(owner + "." + method)
                checks["six_native_fence_ids:" + str(index)] = observed == list(self.module.PROJECTED_FENCE_IDS)
        return checks


def observe_projection(root, output, alias, suite, runner):
    observer = ProjectionObserver(root, output, alias, suite)
    before = observer.safe_metadata(observer.source_identity)
    original_resultclass = runner.resultclass
    result = None
    began = time.monotonic_ns()
    try:
        with ExitStack() as stack:
            observer.patches(stack)
            runner.resultclass = observer.result_class(original_resultclass)
            began = time.monotonic_ns()
            result = runner.run(suite)
            elapsed = time.monotonic_ns() - began
    finally:
        runner.resultclass = original_resultclass
        after = observer.safe_metadata(observer.source_identity)
        checks = observer.validate(result, before, after) if result is not None else {"runner_returned": False}
        document = {"schema_version": 1, "alias": alias, "selection": observer.selection,
                    "source_before": before, "source_after": after,
                    "class_run_ns": elapsed if result is not None else time.monotonic_ns() - began,
                    "method_sum_ns": sum(row.get("elapsed_ns", 0) for row in observer.methods),
                    "spans": observer.spans, "destinations": list(observer.destinations.values()),
                    "native": observer.native, "cleanup": observer.cleanup,
                    "metadata_errors": observer.errors, "bindings": observer.bindings,
                    "external_outcomes": observer.external_outcomes, "checks": checks,
                    "valid": all(checks.values()), "qualified": False,
                    "limits": ["Instrumentation overhead and extra attestations are included in class/method times.",
                               "Nested spans are inclusive; self time subtracts immediate spans and aggregate native call intervals only.",
                               "Residual materialisation includes source reads, directories, policy and bookkeeping; it is not pure copy or CPU time.",
                               "Adapter service includes hashing/memo lookup and adaptation when needed; no cache-miss count is claimed.",
                               "Fresh child internals remain opaque. Nonverbose argv and summaries are not per-ID callbacks.",
                               "Collected subprocess.run results and removed owned trees do not prove exhaustive descendant cleanup."]}
        (output / "projection-phases.json").write_text(json.dumps(document, indent=2) + "\n")
    return result, document["valid"]
