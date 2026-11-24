"""Native unittest discovery, object selection, and lossless callback receipts.

This module deliberately keeps the discovered objects: load_tests adapters may
return classes from synthetic modules that cannot be imported by test ID.
"""

from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
import unittest


def _leaves(suite):
    if type(suite) is not unittest.TestSuite:
        raise ValueError("unsupported suite semantics: " + unittest.util.strclass(type(suite)))
    for child in suite:
        if isinstance(child, unittest.TestSuite):
            yield from _leaves(child)
        elif isinstance(child, unittest.TestCase):
            yield child
        else:
            raise ValueError("unsupported test object: " + repr(type(child)))


class _DiscoveryLoader(unittest.TestLoader):
    def __init__(self):
        super().__init__()
        self.origins = {}
        self.modules = {}

    def loadTestsFromModule(self, module, *args, **kwargs):
        self.modules[module.__name__] = module
        suite = super().loadTestsFromModule(module, *args, **kwargs)
        origin = (module.__name__, callable(getattr(module, "load_tests", None)))
        for test in _leaves(suite):
            # An outer load_tests adapter owns all its nested/synthetic leaves.
            self.origins[id(test)] = origin
        return suite


def _census(suite, origins=None, modules=None):
    origins, modules = origins or {}, modules or {}
    leaves = list(_leaves(suite))
    ids = [test.id() for test in leaves]
    if not ids:
        raise ValueError("empty native discovery population")
    if any(not isinstance(test_id, str) or not test_id for test_id in ids):
        raise ValueError("invalid test ID")
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate test IDs in native discovery")
    groups = {}
    membership = {}
    for test in leaves:
        cls = unittest.util.strclass(type(test))
        module_name = type(test).__module__
        origin, adapter = origins.get(id(test), (module_name, False))
        module = modules.get(module_name, sys.modules.get(module_name))
        coupled = module is not None and any(
            callable(getattr(module, name, None)) for name in ("setUpModule", "tearDownModule")
        )
        key = ("adapter:" + origin if adapter else "module:" + module_name if coupled
               else "class:" + origin + ":" + cls)
        unit = groups.setdefault(key, {"key": key, "ids": [], "classes": [], "serial_reason": None})
        unit["ids"].append(test.id())
        if cls not in unit["classes"]:
            unit["classes"].append(cls)
        membership[test.id()] = {"class": cls, "module": module_name, "origin": origin}
    # If adapters share a class or fixture-coupled module, join their units.
    # This also prevents a class from being split across different adapters.
    units = list(groups.values())
    owners = {}
    parent = list(range(len(units)))

    def root(index):
        while parent[index] != index:
            index = parent[index]
        return index

    for index, unit in enumerate(units):
        tokens = [("class", name) for name in unit["classes"]]
        for test_id in unit["ids"]:
            name = membership[test_id]["module"]
            module = modules.get(name, sys.modules.get(name))
            if module is not None and any(callable(getattr(module, f, None)) for f in ("setUpModule", "tearDownModule")):
                tokens.append(("module", name))
        for token in tokens:
            if token in owners:
                parent[root(index)] = root(owners[token])
            owners[token] = index
    merged = {}
    for index, unit in enumerate(units):
        target = merged.setdefault(root(index), {"key": units[root(index)]["key"], "ids": [], "classes": [], "serial_reason": None})
        target["ids"].extend(unit["ids"])
        target["classes"].extend(c for c in unit["classes"] if c not in target["classes"])
    order = {test_id: index for index, test_id in enumerate(ids)}
    for unit in merged.values():
        unit["ids"].sort(key=order.__getitem__)
    units = sorted(merged.values(), key=lambda unit: order[unit["ids"][0]])
    # Atomic membership alone is insufficient: filtering B from A/B/A changes
    # native fixture transitions. Close each unit's native-order interval over
    # all intervening units, including any overlapping intervals they extend.
    closed = []
    interval_end = -1
    for unit in units:
        first, last = order[unit["ids"][0]], order[unit["ids"][-1]]
        if closed and first <= interval_end:
            target = closed[-1]
            target["ids"].extend(unit["ids"])
            target["classes"].extend(c for c in unit["classes"] if c not in target["classes"])
            interval_end = max(interval_end, last)
        else:
            closed.append(unit)
            interval_end = last
    units = closed
    for unit in units:
        unit["ids"].sort(key=order.__getitem__)
    digest = hashlib.sha256(json.dumps(ids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    census = {"ids": ids, "units": units, "population_digest": digest, "membership": membership}
    suite._parallel_census = census
    return census


def discover_suite(root: Path):
    """Run canonical fresh-loader discovery while retaining origin ownership."""
    root = Path(root).resolve(strict=True)
    previous_cwd = Path.cwd()
    loader = _DiscoveryLoader()
    try:
        os.chdir(root)
        suite = loader.discover(start_dir=".", top_level_dir=".")
        if loader.errors:
            raise ValueError("native discovery errors:\n" + "\n".join(loader.errors))
        return suite, _census(suite, loader.origins, loader.modules)
    finally:
        os.chdir(previous_cwd)
        # Keep native discovery's import path and any deliberate additions by
        # tests/__init__.py (including the original user-site code exception).


def select_suite(suite: unittest.TestSuite, ids: list[str]):
    """Filter actual leaves, retaining native nesting/order and atomic units."""
    census = getattr(suite, "_parallel_census", None) or _census(suite)
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("selection is empty or contains duplicate IDs")
    wanted = set(ids)
    if not wanted <= set(census["ids"]):
        raise ValueError("selection contains foreign IDs")
    for unit in census["units"]:
        selected = wanted.intersection(unit["ids"])
        if selected and selected != set(unit["ids"]):
            raise ValueError("selection splits atomic unit: " + unit["key"])

    def clone(node):
        children = []
        for child in node:
            if isinstance(child, unittest.TestSuite):
                chosen = clone(child)
                if chosen.countTestCases():
                    children.append(chosen)
            elif child.id() in wanted:
                children.append(child)
        result = unittest.TestSuite(children)
        result._cleanup = node._cleanup
        return result

    # Check for unsupported suites even if they are outside this selection.
    list(_leaves(suite))
    selected = clone(suite)
    selected._parallel_census = {
        **census, "ids": [test_id for test_id in census["ids"] if test_id in wanted],
        "units": [unit for unit in census["units"] if wanted.intersection(unit["ids"])],
    }
    return selected


class _RecordingResult(unittest.TextTestResult):
    def __init__(self, stream, leaves, event_sink=None):
        super().__init__(stream, descriptions=True, verbosity=2)
        self.objects = {id(test): test for test in leaves}
        self.selected = [test.id() for test in leaves]
        self.members = {test.id(): (unittest.util.strclass(type(test)), type(test).__module__) for test in leaves}
        self.started_ids = []
        self.stopped_ids = []
        self.events = []
        self.start_times = {}
        self.test_durations = {}
        self.protocol_errors = []
        self.event_sink = event_sink
        self.sink_failed = False
        self.positions = {test_id: index for index, test_id in enumerate(self.selected)}
        self.frontier = 0
        self.active_setup = None

    def _emit(self, event):
        if self.event_sink is None or self.sink_failed:
            return
        try:
            # A consumer owns its journal; it must not mutate our receipt.
            self.event_sink(deepcopy(event))
        except BaseException as exc:
            self.sink_failed = True
            self.protocol_errors.append("event sink failed: " + type(exc).__name__ + ": " + str(exc))
            raise

    def _refuse(self, reason):
        # TestCase catches ordinary exceptions from a test body. Retain the
        # refusal even if that body catches it and then reports success.
        self.protocol_errors.append(reason)
        raise ValueError(reason)

    def _test_id(self, test):
        if id(test) not in self.objects:
            self._refuse("callback for an unknown test object: " + test.id())
        return test.id()

    def _event(self, kind, test, **extra):
        event = {"kind": kind, "test_id": self._test_id(test), **extra}
        self.events.append(event)
        self._emit(event)

    def _fixture(self, kind, test, **extra):
        if type(test) is not unittest.suite._ErrorHolder:
            return False
        fixture = test.id()
        match = re.fullmatch(r"(setUpClass|tearDownClass|setUpModule|tearDownModule) \((.+)\)", fixture)
        if not match:
            self._refuse("unknown native fixture callback: " + fixture)
        stage, owner = match.groups()
        field = 0 if stage.endswith("Class") else 1
        affected = [test_id for test_id in self.selected if self.members[test_id][field] == owner]
        if not affected:
            self._refuse("fixture callback outside selection: " + fixture)
        blocked = []
        if stage.startswith("setUp"):
            if self.active_setup is not None and self.active_setup[0] == fixture:
                # Setup and its failing cleanups produce separate native
                # callbacks for the same suppressed contiguous scope.
                blocked = self.active_setup[1][:]
            else:
                index = self.frontier
                if index >= len(self.selected) or self.members[self.selected[index]][field] != owner:
                    self._refuse("setup callback outside the next native scope: " + fixture)
                while index < len(self.selected) and self.members[self.selected[index]][field] == owner:
                    blocked.append(self.selected[index])
                    index += 1
                self.frontier = index
                self.active_setup = (fixture, blocked[:])
        else:
            self.active_setup = None
        event = {"kind": kind, "test_id": fixture, "fixture": fixture, "blocked_ids": blocked, **extra}
        self.events.append(event)
        self._emit(event)
        return True

    def startTest(self, test):
        test_id = self._test_id(test)
        self.started_ids.append(test_id)
        self.frontier = self.positions[test_id] + 1
        self.active_setup = None
        self.start_times[test_id] = time.monotonic()
        super().startTest(test)
        self._emit({"kind": "start", "test_id": test_id})

    def stopTest(self, test):
        test_id = self._test_id(test)
        self.stopped_ids.append(test_id)
        if test_id in self.start_times:
            self.test_durations[test_id] = time.monotonic() - self.start_times[test_id]
        super().stopTest(test)
        self._emit({"kind": "stop", "test_id": test_id})

    def addSuccess(self, test):
        self._event("success", test)
        super().addSuccess(test)

    def addFailure(self, test, err):
        self._event("failure", test)
        super().addFailure(test, err)

    def addError(self, test, err):
        if not self._fixture("fixture_error", test):
            self._event("error", test)
        super().addError(test, err)

    def addSkip(self, test, reason):
        if isinstance(test, unittest.case._SubTest):
            self._event("skip", test.test_case, subtest=test.id(), reason=reason)
        elif not self._fixture("fixture_skip", test, reason=reason):
            self._event("skip", test, reason=reason)
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self._event("expected_failure", test)
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self._event("unexpected_success", test)
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        kind = "subtest_success" if err is None else "subtest_failure" if issubclass(err[0], test.failureException) else "subtest_error"
        self._event(kind, test, subtest=subtest.id())
        super().addSubTest(test, subtest, err)


def run_suite(suite: unittest.TestSuite, assignment: dict, population_digest: str, stream, *, event_sink=None):
    """Run selected objects, optionally publishing callbacks before completion.

    The sink receives start/stop dictionaries and the same outcome dictionaries
    as the aggregate events list. The caller owns persistence and its footer.
    """
    leaves = list(_leaves(suite))
    selected = [test.id() for test in leaves]
    assigned = assignment.get("ids", [])
    if not selected or len(set(selected)) != len(selected) or Counter(selected) != Counter(assigned):
        raise ValueError("selected suite does not match assignment exactly")
    result = _RecordingResult(unittest.runner._WritelnDecorator(stream), leaves, event_sink)
    runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=lambda *args: result)
    start = time.monotonic()
    status, reason = "complete", None
    try:
        runner.run(suite)
    except KeyboardInterrupt:
        status, reason = "interrupted", "KeyboardInterrupt"
    except BaseException as exc:
        status, reason = "invalid", type(exc).__name__ + ": " + str(exc)
        traceback.print_exc(file=stream)
    blocked = {test_id for event in result.events for test_id in event.get("blocked_ids", [])}
    outcomes = {
        event["test_id"] for event in result.events
        if "fixture" not in event and event["kind"] != "subtest_success"
    }
    if result.protocol_errors:
        status, reason = "invalid", "; ".join(result.protocol_errors)
    if status == "complete" and (
        Counter(result.started_ids) != Counter(result.stopped_ids)
        or len(set(result.started_ids)) != len(result.started_ids)
        or set(selected) != set(result.started_ids) | blocked
        or not set(result.started_ids) <= outcomes
        or result.testsRun != len(result.started_ids)
    ):
        status, reason = "invalid", "native callbacks do not account for the complete selection"
    return {
        "key": assignment["key"], "population_digest": population_digest,
        "selected_ids": selected, "started_ids": result.started_ids,
        "stopped_ids": result.stopped_ids, "events": result.events,
        "tests_run": result.testsRun,
        "successful": status == "complete" and result.wasSuccessful(),
        "duration_seconds": time.monotonic() - start,
        "fixture_blocked_ids": [test_id for test_id in selected if test_id in blocked],
        "status": status, "incomplete_reason": reason,
        "test_durations": result.test_durations,
    }
