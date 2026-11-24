"""Pure exact-population planning and native unittest receipt reconciliation.

These functions perform no discovery, launch, filesystem, or bus operations.
Every partition is proved independently of the balancing heuristic.
"""

from collections import Counter
import hashlib
import json
import math
import os
import re
from typing import Optional


_EVENT_KINDS = frozenset({
    "success", "failure", "error", "skip", "expected_failure",
    "unexpected_success", "subtest_success", "subtest_failure",
    "subtest_error", "fixture_error", "fixture_skip",
})
_FAILURE_KINDS = frozenset({
    "failure", "error", "unexpected_success", "subtest_failure",
    "subtest_error", "fixture_error",
})


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}: expected object")
    return value


def _string(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}: expected nonempty string")
    return value


def _strings(value, label, nonempty=False):
    if not isinstance(value, list):
        raise ValueError(f"{label}: expected list")
    for item in value:
        _string(item, label)
    if nonempty and not value:
        raise ValueError(f"empty {label}")
    return value


def _unique(values, label):
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {label}")


def _same_multiset(expected, actual, label):
    expected, actual = Counter(expected), Counter(actual)
    if actual - expected:
        foreign = set(actual) - set(expected)
        reason = "foreign" if foreign else "duplicate"
        raise ValueError(f"{reason} {label}")
    if expected - actual:
        raise ValueError(f"missing {label}")


def validate_census(census: dict) -> None:
    """Refuse an empty, ambiguous, split, or internally damaged census."""
    _mapping(census, "census")
    ids = _strings(census.get("ids"), "population", nonempty=True)
    _unique(ids, "population ID")
    units = census.get("units")
    if not isinstance(units, list) or not units:
        raise ValueError("empty units")
    unit_keys, class_keys, members = [], [], []
    for unit in units:
        _mapping(unit, "unit")
        unit_keys.append(_string(unit.get("key"), "unit key"))
        members.extend(_strings(unit.get("ids"), "unit IDs", nonempty=True))
        classes = _strings(unit.get("classes"), "unit classes", nonempty=True)
        _unique(classes, "unit class")
        class_keys.extend(classes)
        if "serial_reason" not in unit:
            raise ValueError("missing serial reason")
        reason = unit["serial_reason"]
        if reason is not None:
            _string(reason, "serial reason")
    _unique(unit_keys, "unit key")
    if len(set(class_keys)) != len(class_keys):
        raise ValueError("split class across units")
    _same_multiset(ids, members, "unit ID")
    membership = _mapping(census.get("membership"), "membership")
    if set(membership) != set(ids):
        raise ValueError("membership must cover exactly the native population")
    for member in membership.values():
        _mapping(member, "membership entry")
        cls = _string(member.get("class"), "membership class")
        module = _string(member.get("module"), "membership module")
        _string(member.get("origin"), "membership origin")
        if not cls.startswith(module + "."):
            raise ValueError("membership class/module identity mismatch")
    for unit in units:
        actual_classes = {membership[test_id]["class"] for test_id in unit["ids"]}
        if actual_classes != set(unit["classes"]):
            raise ValueError("membership classes mismatch census unit")
    digest = hashlib.sha256(json.dumps(
        ids, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    if census.get("population_digest") != digest:
        raise ValueError("population digest mismatch in census")


def worker_limit() -> int:
    """Use the host's logical CPU count, conservatively falling back to one."""
    return max(1, os.cpu_count() or 1)


def validate_worker_count(workers: int) -> None:
    limit = worker_limit()
    if type(workers) is not int or not 1 <= workers <= limit:
        raise ValueError(f"parallel_workers_out_of_range: workers must be an integer between 1 and {limit}")


def make_plan(census: dict, workers: int, weights: Optional[dict] = None) -> dict:
    """Balance intact units deterministically, then prove the resulting plan."""
    validate_census(census)
    validate_worker_count(workers)
    units = census["units"]
    parallel = [unit for unit in units if unit["serial_reason"] is None]
    if parallel and workers > len(parallel):
        raise ValueError("workers would create an empty assignment")
    if weights is None:
        costs = {unit["key"]: len(unit["ids"]) for unit in units}
    else:
        _mapping(weights, "weights")
        if set(weights) != {unit["key"] for unit in units}:
            raise ValueError("weights must cover exactly all census units")
        for value in weights.values():
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value < 0):
                raise ValueError("weights must be finite nonnegative durations")
        costs = weights
    buckets = [[] for _ in range(workers)] if parallel else []
    loads = [0.0] * len(buckets)
    positions = {unit["key"]: i for i, unit in enumerate(units)}
    for unit in sorted(parallel, key=lambda unit: (-costs[unit["key"]], positions[unit["key"]])):
        # Occupancy breaks zero-duration ties without producing empty workers.
        index = min(range(workers), key=lambda i: (loads[i], len(buckets[i]), i))
        buckets[index].append(unit)
        loads[index] += costs[unit["key"]]
    assignments = []
    for i, bucket in enumerate(buckets):
        assignments.append(_assignment(census, f"worker-{i + 1}", "parallel", bucket))
    serial = [unit for unit in units if unit["serial_reason"] is not None]
    if serial:
        assignments.append(_assignment(census, "serial", "serial", serial))
    plan = {"population_digest": census["population_digest"], "assignments": assignments}
    validate_plan(census, plan)
    return plan


def _assignment(census, key, phase, units):
    selected = {test_id for unit in units for test_id in unit["ids"]}
    unit_set = {unit["key"] for unit in units}
    return {"key": key, "phase": phase,
            "ids": [test_id for test_id in census["ids"] if test_id in selected],
            "unit_keys": [unit["key"] for unit in census["units"] if unit["key"] in unit_set]}


def plan_estimates(plan: dict, costs: dict) -> dict:
    """Report advisory method costs; exclude the serial tail from balance."""
    estimates = {item['key']: sum(costs[key] for key in item['unit_keys'])
                 for item in plan['assignments']}
    parallel = [estimates[item['key']] for item in plan['assignments']
                if item['phase'] == 'parallel']
    maximum = max(parallel, default=0.0)
    mean = sum(parallel) / len(parallel) if parallel else 0.0
    return {'provenance': 'ESTIMATE', 'assignment_seconds': estimates,
            'serial_tail_seconds': sum(estimates[item['key']] for item in plan['assignments']
                                       if item['phase'] == 'serial'),
            'max_parallel_seconds': maximum, 'mean_parallel_seconds': mean,
            'parallel_max_mean_ratio': maximum / mean if mean else None}


def validate_plan(census: dict, plan: dict) -> None:
    """Prove IDs, units, ordering, worker cap, and exclusive phase before launch."""
    validate_census(census)
    _mapping(plan, "plan")
    if plan.get("population_digest") != census["population_digest"]:
        raise ValueError("population digest mismatch in plan")
    assignments = plan.get("assignments")
    if not isinstance(assignments, list) or not assignments:
        raise ValueError("empty assignments")
    keys, assigned_ids, assigned_units = [], [], []
    parallel_count = 0
    for assignment in assignments:
        _mapping(assignment, "assignment")
        key = _string(assignment.get("key"), "assignment key")
        keys.append(key)
        phase = assignment.get("phase")
        if phase == "parallel" and re.fullmatch(r"worker-[1-9][0-9]*", key):
            validate_worker_count(int(key.removeprefix("worker-")))
            parallel_count += 1
        elif phase != "serial" or key != "serial":
            raise ValueError("invalid assignment phase/key")
        ids = _strings(assignment.get("ids"), "assignment", nonempty=True)
        unit_keys = _strings(assignment.get("unit_keys"), "assignment units", nonempty=True)
        assigned_ids.extend(ids)
        assigned_units.extend(unit_keys)
    _unique(keys, "assignment key")
    if parallel_count:
        validate_worker_count(parallel_count)
    _same_multiset(census["ids"], assigned_ids, "assignment ID")
    _same_multiset([unit["key"] for unit in census["units"]], assigned_units, "unit")
    units = {unit["key"]: unit for unit in census["units"]}
    for assignment in assignments:
        member_ids = [test_id for key in assignment["unit_keys"] for test_id in units[key]["ids"]]
        if Counter(member_ids) != Counter(assignment["ids"]):
            raise ValueError("split unit or incorrect assignment unit membership")
        selected = set(assignment["ids"])
        if assignment["ids"] != [test_id for test_id in census["ids"] if test_id in selected]:
            raise ValueError("assignment violates native order")
        for key in assignment["unit_keys"]:
            serial = units[key]["serial_reason"] is not None
            if serial != (assignment["phase"] == "serial"):
                raise ValueError("serial unit phase mismatch")


def _fixture_scope(event, selected, membership):
    fixture = event.get("fixture")
    if fixture != event["test_id"] or not isinstance(fixture, str):
        raise ValueError("fixture identity mismatch")
    match = re.fullmatch(r"(setUpClass|tearDownClass|setUpModule|tearDownModule) \((.+)\)", fixture)
    if not match:
        raise ValueError("unsupported fixture event")
    callback, identity = match.groups()
    field = "class" if callback.endswith("Class") else "module"
    descendants = [test_id for test_id in selected if membership[test_id][field] == identity]
    if not descendants:
        raise ValueError("foreign fixture event")
    return callback, descendants


def _validate_result(assignment, result, digest, membership):
    _mapping(result, "result")
    if result.get("status") != "complete":
        raise ValueError("incomplete result status")
    if result.get("population_digest") != digest:
        raise ValueError("population digest mismatch in result")
    selected = _strings(result.get("selected_ids"), "selected IDs")
    if selected != assignment["ids"]:
        raise ValueError("selected IDs mismatch assignment")
    started = _strings(result.get("started_ids"), "started IDs")
    stopped = _strings(result.get("stopped_ids"), "stopped IDs")
    blocked = _strings(result.get("fixture_blocked_ids"), "fixture blocked IDs")
    _unique(started, "start ID")
    _unique(stopped, "stop ID")
    _unique(blocked, "fixture blocked ID")
    _same_multiset(started, stopped, "stop ID")
    _same_multiset(selected, started + blocked, "started/fixture blocked ID")
    started_set = set(started)
    if started != [test_id for test_id in selected if test_id in started_set] or stopped != started:
        raise ValueError("start/stop callback order mismatch native selection")
    tests_run = result.get("tests_run")
    if type(tests_run) is not int or tests_run != len(started):
        raise ValueError("native tests_run mismatch started IDs")
    duration = result.get("duration_seconds")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        raise ValueError("invalid duration_seconds")
    events = result.get("events")
    if not isinstance(events, list):
        raise ValueError("events must be a list")
    accounted, fixture_blocked, failing = set(), set(), False
    parent_outcomes, nonsuccess_subtests = {}, set()
    for event in events:
        _mapping(event, "event")
        kind = event.get("kind")
        if not isinstance(kind, str) or kind not in _EVENT_KINDS:
            raise ValueError("unknown event kind")
        test_id = _string(event.get("test_id"), "event test_id")
        if kind in ("skip", "fixture_skip") and not isinstance(event.get("reason"), str):
            raise ValueError("skip event requires reason")
        if kind.startswith("fixture_"):
            callback, descendants = _fixture_scope(event, selected, membership)
            event_blocked = _strings(event.get("blocked_ids"), "fixture event blocked IDs")
            _unique(event_blocked, "fixture event blocked ID")
            if callback.startswith("tearDown") and event_blocked:
                raise ValueError("teardown fixture cannot block IDs")
            if not set(event_blocked).issubset(descendants):
                raise ValueError("fixture blocked IDs outside fixture scope")
            fixture_blocked.update(event_blocked)
        else:
            if test_id not in started_set:
                raise ValueError("foreign or unstarted outcome event")
            if "fixture" in event or "blocked_ids" in event:
                raise ValueError("nonfixture event has fixture fields")
            if kind.startswith("subtest_"):
                _string(event.get("subtest"), "subtest event identity")
            elif "subtest" in event:
                if kind != "skip":
                    raise ValueError("invalid subtest event kind")
                _string(event["subtest"], "subtest skip identity")
            # A successful subtest alone is not a terminal parent outcome.
            if kind != "subtest_success":
                accounted.add(test_id)
            if "subtest" not in event:
                parent_outcomes.setdefault(test_id, []).append(kind)
            elif kind != "subtest_success":
                nonsuccess_subtests.add(test_id)
        failing = failing or kind in _FAILURE_KINDS
    _same_multiset(blocked, fixture_blocked, "fixture blocked event ID")
    if set(started) - accounted:
        raise ValueError("missing outcome for started ID")
    for test_id, outcomes in parent_outcomes.items():
        if "success" in outcomes and (len(outcomes) != 1 or test_id in nonsuccess_subtests):
            raise ValueError("duplicate or contradictory success outcome")
    if type(result.get("successful")) is not bool or result["successful"] != (not failing):
        raise ValueError("successful flag disagrees with native outcomes")


def reconcile(census: dict, plan: dict, results: list[dict]) -> dict:
    """Reconcile every scheduled native receipt without inventing test outcomes."""
    validate_plan(census, plan)
    if not isinstance(results, list):
        raise ValueError("results must be a list")
    keys = []
    for result in results:
        _mapping(result, "result")
        keys.append(_string(result.get("key"), "result key"))
    _unique(keys, "result key")
    _same_multiset([assignment["key"] for assignment in plan["assignments"]], keys, "result")
    by_key = {result["key"]: result for result in results}
    ordered = []
    for assignment in plan["assignments"]:
        result = by_key[assignment["key"]]
        _validate_result(assignment, result, census["population_digest"], census["membership"])
        ordered.append(result)
    aggregate = {
        "population_digest": census["population_digest"],
        "tests_run": sum(result["tests_run"] for result in ordered),
        "successful": all(result["successful"] for result in ordered),
    }
    for field in ("selected_ids", "started_ids", "stopped_ids", "fixture_blocked_ids", "events"):
        aggregate[field] = [item for result in ordered for item in result[field]]
    return aggregate


def compare_results(reference: dict, candidate: dict) -> dict:
    """Compare stable native signatures for optional same-source qualification."""
    _mapping(reference, "reference")
    _mapping(candidate, "candidate")
    differences = {}
    for field in ("population_digest", "tests_run", "successful"):
        if field not in reference or field not in candidate:
            raise ValueError(f"comparison missing {field}")
        if reference[field] != candidate[field]:
            differences[field] = {"reference": reference[field], "candidate": candidate[field]}
    for field in ("selected_ids", "started_ids", "stopped_ids", "fixture_blocked_ids"):
        left = Counter(_strings(reference.get(field), f"reference {field}"))
        right = Counter(_strings(candidate.get(field), f"candidate {field}"))
        if left != right:
            differences[field] = {"missing": list((left - right).elements()),
                                  "extra": list((right - left).elements())}
    signatures = []
    for result in (reference, candidate):
        events = result.get("events")
        if not isinstance(events, list):
            raise ValueError("comparison events must be a list")
        normalized = []
        for event in events:
            _mapping(event, "comparison event")
            kind = event.get("kind")
            if not isinstance(kind, str) or kind not in _EVENT_KINDS:
                raise ValueError("comparison unknown event kind")
            _string(event.get("test_id"), "comparison event test_id")
            if kind in ("skip", "fixture_skip") and not isinstance(event.get("reason"), str):
                raise ValueError("comparison skip event requires reason")
            signature = {key: event[key] for key in ("kind", "test_id", "fixture", "subtest", "reason") if key in event}
            if "blocked_ids" in event:
                signature["blocked_ids"] = sorted(_strings(event["blocked_ids"], "comparison blocked IDs"))
            normalized.append(json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        signatures.append(Counter(normalized))
    left, right = signatures
    if left != right:
        differences["events"] = {"missing": [json.loads(item) for item in (left - right).elements()],
                                  "extra": [json.loads(item) for item in (right - left).elements()]}
    return {"equivalent": not differences, "differences": differences}
