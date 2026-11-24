#!/usr/bin/env python3
"""Regenerate the LOADED-WALLCLOCK-1 round-2 source census, without importing tests.

    /usr/bin/python3 -B scripts/audit-loaded-wallclock-population.py --write
    /usr/bin/python3 -B scripts/audit-loaded-wallclock-population.py --check

Check is the default. Inputs and outputs are fixed repository-relative paths.
Only tests/**/*.py is scanned. Two existing evidence selector lists are read as
data. Source/input hashes identify the snapshot; no timestamp or Git invocation
makes otherwise identical output drift. This is a syntactic census with explicit
ownership limits, not unittest discovery or proof of timing independence.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path("docs/evidence")
OLD_INPUT = EVIDENCE / "loaded-wallclock-1-population-selectors-2026-10-25.json"
R2_INPUT = EVIDENCE / "loaded-wallclock-1-r2-candidates-2026-10-25.json"
INVENTORY = EVIDENCE / "loaded-wallclock-1-r2-tree-inventory-2026-10-25.json"
APPENDIX = EVIDENCE / "loaded-wallclock-1-r2-tree-audit-2026-10-25.md"
ORDERING = {
    "assertLess", "assertLessEqual", "assertGreater", "assertGreaterEqual",
    "assertAlmostEqual",
}
TEMPORAL = re.compile(
    r"elapsed|monotonic|perf_counter|process_time|deadline|duration|latency|"
    r"start_spread|waited_seconds|remaining|startup|timeout|"
    r"\.wait\(|\.poll\(|is_alive|ready", re.I,
)
REAL_CLOCK = re.compile(
    r"\btime\.(?:monotonic|time|perf_counter(?:_ns)?|process_time(?:_ns)?)\("
)
ELAPSED_ALIAS = re.compile(r"\belapsed\b|\bstart_spread\b|takeover\[0\]\[1\]")
WAIT_OR_IO_APIS = {
    "wait", "join", "communicate", "poll", "select", "recv", "recv_bytes",
    "read", "readline", "readinto", "get", "wait_until", "wait_for", "result",
    "run", "check_output", "check_call",
}
CORE_WAIT_OR_IO_APIS = {
    "wait", "join", "communicate", "poll", "select", "recv", "recv_bytes",
    "read", "readline", "readinto", "wait_until", "wait_for",
}
CLI_BUDGET_FLAGS = {"--deadline-seconds", "--wait-deadline-seconds", "--wait-seconds", "--timeout"}
CONTROLLED_ELAPSED_FILES = {
    "tests/test_codex_completed_teardown.py", "tests/test_wait_integrity_cadence.py",
    "tests/test_cursor_session_pause.py", "tests/test_cursor_followup_delivery_visible.py",
    "tests/test_f2_readiness_hold.py", "tests/test_waiter_idle_window.py",
}
SELECTOR = re.compile(r"tests(?:\.[A-Za-z_][A-Za-z_0-9]*){3,}\Z")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_text(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=True) + "\n"


def ordinary_path(relative: Path) -> Path:
    """Refuse symlinked data/output coordinates rather than following them."""
    current = ROOT
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlinked audit coordinate: " + relative.as_posix())
    return current


def selector_input(relative: Path) -> tuple[set[str], dict]:
    data = ordinary_path(relative).read_bytes()
    values = json.loads(data)
    if (not isinstance(values, list) or not values
            or any(not isinstance(value, str) or not SELECTOR.fullmatch(value) for value in values)
            or len(values) != len(set(values))):
        raise ValueError("invalid selector list: " + relative.as_posix())
    return set(values), {"path": relative.as_posix(), "sha256": digest(data)}


def source_text(lines: list[str], node: ast.AST) -> str:
    return "\n".join(lines[node.lineno - 1:node.end_lineno]).strip()


def duration_role(value, origin):
    """Describe supplied syntax; a dynamic expression is not known finite."""
    expression = ast.unparse(value)
    unknown = object()
    try:
        literal = ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError):
        literal = unknown
    if literal is None:
        return {
            "bound_role": "explicitly_untimed", "bound_expression": expression,
            "reason": origin + " is None: no finite timeout is supplied at this call.",
        }
    if type(literal) in (int, float):
        finite = type(literal) is int or math.isfinite(literal)
        if literal >= 0 and finite:
            return {
                "bound_role": "finite_timeout_literal", "bound_expression": expression,
                "reason": origin + " supplies a finite nonnegative duration literal. This is an API/watchdog budget, not evidence the property passed within it.",
            }
        return {
            "bound_role": "invalid_duration_literal", "bound_expression": expression,
            "reason": origin + " supplies negative/nonfinite duration data, typically an argument-validation control; it is not a finite waiting guarantee.",
        }
    if literal is not unknown:
        return {
            "bound_role": "invalid_duration_literal", "bound_expression": expression,
            "reason": origin + " supplies nonnumeric/bool literal data, typically an argument-validation control; it is not a duration guarantee.",
        }
    return {
        "bound_role": "dynamic_timeout_argument", "bound_expression": expression,
        "reason": origin + " supplies a dynamic expression; its value may be None, invalid or finite, so this census does not claim a finite timeout.",
    }


def wait_or_io_role(node, name, keywords, posix_select_calls):
    """Distinguish duration syntax, untimed gates, I/O data and unknown types."""
    api = ast.unparse(node.func)
    timeout_keys = [key for key in keywords
                    if key in ("timeout", "timeout_seconds") or key.endswith("_timeout_seconds")]
    if timeout_keys:
        key = timeout_keys[0]
        role = duration_role(keywords[key], "Explicit " + key + " keyword")
        role["api"] = api
        if len(timeout_keys) > 1:
            role["additional_timeout_keywords"] = {
                key: ast.unparse(keywords[key]) for key in timeout_keys[1:]
            }
        return role
    if "deadline" in keywords:
        return {
            "api": api, "bound_role": "absolute_deadline_argument",
            "bound_expression": ast.unparse(keywords["deadline"]),
            "reason": "An explicit deadline keyword supplies an absolute-budget input. Its clock domain, validity and remaining time are not inferred as a finite relative timeout.",
        }
    if any(isinstance(value, ast.Starred) for value in node.args) or any(
            keyword.arg is None for keyword in node.keywords):
        return {
            "api": api, "bound_role": "expanded_arguments_unresolved", "bound_expression": None,
            "reason": "Expanded positional/keyword arguments can supply hidden timeout or I/O parameters; neither a finite timeout nor an untimed gate is inferred.",
        }
    if api in posix_select_calls:
        if len(node.args) >= 4:
            role = duration_role(node.args[3], "POSIX select fourth positional argument")
            role["api"] = api
            return role
        if len(node.args) == 3:
            return {
                "api": api, "bound_role": "untimed_readiness_gate", "bound_expression": None,
                "reason": "POSIX select has three readiness-set arguments and no fourth timeout; this is an untimed readiness gate, not a finite-time wait.",
            }
        return {
            "api": api, "bound_role": "unresolved_select_signature", "bound_expression": None,
            "reason": "The recognized select call does not supply the ordinary three/four positional parameters; it may be an argument-validation fixture, so no timeout behavior is inferred.",
        }
    if name in ("recv", "recv_bytes", "read", "readline", "readinto"):
        return {
            "api": api, "bound_role": "io_without_explicit_timeout",
            "bound_expression": None,
            "positional_io_arguments": [ast.unparse(value) for value in node.args],
            "reason": "No timeout keyword is supplied. Positional socket/pipe/file read arguments are descriptors, byte counts, flags, maximum lengths or buffers; custom receiver signatures and previously configured stream/socket timeouts are unresolved. Positional I/O data is never treated as a duration here.",
        }
    if name == "join" and isinstance(node.func, ast.Attribute):
        receiver = node.func.value
        if isinstance(receiver, ast.Constant) and isinstance(receiver.value, (str, bytes)):
            return {
                "api": api, "bound_role": "non_waiting_string_join", "bound_expression": None,
                "reason": "Literal str/bytes join assembles data; its positional iterable is not a timeout or process/thread wait.",
            }
        if api in ("os.path.join", "posixpath.join", "ntpath.join"):
            return {
                "api": api, "bound_role": "non_waiting_path_join", "bound_expression": None,
                "reason": "This known path-joining spelling assembles path data rather than waiting on a thread/process.",
            }
    if not node.args or name in ("communicate", "run", "check_call", "check_output", "get", "result"):
        return {
            "api": api, "bound_role": "no_explicit_timeout", "bound_expression": None,
            "reason": "No timeout/deadline keyword is supplied; positional communicate/run data is not interpreted as timing. The receiver's default behavior is not claimed finite.",
        }
    return {
        "api": api, "bound_role": "unresolved_wait_signature", "bound_expression": None,
        "positional_arguments": [ast.unparse(value) for value in node.args],
        "reason": "A wait/readiness API name is present, but receiver/signature resolution is not performed. Positional arguments may be durations, deadlines, conditions or I/O sets; neither finite timeout nor unbounded blocking is inferred from their presence.",
    }


def source_ownership(tree: ast.Module, module: str) -> tuple[dict, set[str]]:
    """Expand local inheritance, self/cls helper calls and lifecycle consumers."""
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    methods = {
        name: {node.name: node for node in body.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name, body in classes.items()
    }

    @lru_cache(None)
    def resolved(name):
        found = {}
        for base in classes[name].bases:
            if isinstance(base, ast.Name) and base.id in classes:
                found.update(resolved(base.id))
        found.update(methods[name])
        return found

    owners = defaultdict(set)
    all_tests = set()
    for class_name in classes:
        members = resolved(class_name)
        tests = {name: module + "." + class_name + "." + name
                 for name in members if name.startswith("test_")}
        all_tests.update(tests.values())
        edges = {
            name: {node.func.attr for node in ast.walk(function)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and isinstance(node.func.value, ast.Name)
                   and node.func.value.id in ("self", "cls") and node.func.attr in members}
            for name, function in members.items()
        }
        lifecycle = [name for name in ("setUp", "tearDown", "setUpClass", "tearDownClass")
                     if name in members]
        for test_name, selector in tests.items():
            pending, visited = [test_name, *lifecycle], set()
            while pending:
                name = pending.pop()
                if name in visited:
                    continue
                visited.add(name)
                owners[id(members[name])].add(selector)
                pending.extend(edges[name] - visited)
    return owners, all_tests


class SourceScan(ast.NodeVisitor):
    def __init__(self, relative, source, ownership, tree):
        self.relative = relative.as_posix()
        self.module = ".".join(relative.with_suffix("").parts)
        self.lines = source.splitlines()
        self.ownership = ownership
        self.lexical = []
        self.functions = []
        self.method = None
        self.sites = []
        self.posix_select_calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for imported in node.names:
                    if imported.name == "select":
                        self.posix_select_calls.add((imported.asname or "select") + ".select")
            elif isinstance(node, ast.ImportFrom) and node.module == "select":
                for imported in node.names:
                    if imported.name == "select":
                        self.posix_select_calls.add(imported.asname or "select")

    def text(self, node):
        return source_text(self.lines, node)

    def visit_ClassDef(self, node):
        self.lexical.append(node.name)
        self.generic_visit(node)
        self.lexical.pop()

    def visit_FunctionDef(self, node):
        previous = self.method
        if not self.functions:
            self.method = node
        self.functions.append(node.name)
        self.lexical.append(node.name)
        self.generic_visit(node)
        self.lexical.pop()
        self.functions.pop()
        self.method = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def classification(self, node, category, expression):
        body = self.text(self.method) if self.method else ""
        if category == "ordering_assertion":
            real_duration = bool(REAL_CLOCK.search(expression) or ELAPSED_ALIAS.search(expression))
            if real_duration:
                if node.func.attr in ("assertGreater", "assertGreaterEqual"):
                    return "KEEP", "Elapsed lower bound; scheduling delays alone lengthen the interval and do not violate this lower bound."
                if self.relative in CONTROLLED_ELAPSED_FILES and ELAPSED_ALIAS.search(expression):
                    return "KEEP", "This fixture advances an injected elapsed clock or sums controlled sleeps; host scheduling does not advance the asserted clock."
                return "CANDIDATE", "Real elapsed value, including a helper-returned elapsed alias, or absolute deadline is compared to an upper bound; scheduling/startup can consume that allowance."
            if re.search(r"remaining", expression) and REAL_CLOCK.search(body) and "assertGreater" in expression:
                return "CANDIDATE", "Native setup must leave positive original-deadline residual; startup scheduling can exhaust it."
            if re.search(r"clock|elapsed|sleep|deadline|remaining|timeout", expression, re.I):
                return "KEEP", "Clock, budget, timeout-argument or recorded-evidence arithmetic; direct expression is not a host-time upper ceiling. Source owner retained for review."
            return "KEEP", "Structural/numeric contract comparison, not an elapsed host-time success ceiling."
        if category == "temporal_assertion":
            return "KEEP", "Typed outcome, readiness/evidence or controlled timing assertion; wait/I/O consumers and explicit bounds are separately enumerated."
        if category == "wait_or_io_api":
            return "KEEP", "Wait/readiness/I/O census site; bound_role and reason distinguish duration syntax, untimed gates, I/O data and unresolved signatures."
        if category == "clock_deadline_construction":
            return "KEEP", "This syntax constructs the absolute fixture deadline rather than asserting a sampled duration; native consumers and completion/readiness assertions are independently inventoried. Not a downstream load-independence claim."
        if category == "short_cli_budget":
            return "KEEP", "This syntax passes a product wait/deadline parameter; CLI result/readiness assertions and native subprocess watchdogs are inventoried separately. Budget input is not itself a sampled-latency assertion."
        if self.relative == "tests/test_codex_binding_stable_path.py":
            return "KEEP", "Two-second consent is exact binding/rebind identity metadata; wake runner is mocked and no timed native hold executes."
        if any(word in ".".join(self.lexical).lower() for word in (
                "install", "schema", "migration", "rollback", "arm_workspace",
                "_seed_control_state", "_plant_live_board_claim")):
            return "KEEP", "Install/schema/explicit authority metadata; no success-before-short-deadline native hold occurs at this matched site."
        return "KEEP", "This syntax configures a short consent/protocol budget; it is not an elapsed-time assertion. Native-success, controlled-clock, expected-timeout and metadata consumers remain explicit in callers and reviewed mechanisms; this KEEP does not certify native success before expiry."

    def add(self, node, category, expression=None, details=None):
        expression = expression or self.text(node)
        verdict, reason = self.classification(node, category, expression)
        row = {
            "path": self.relative, "line": node.lineno, "end_line": node.end_lineno,
            "category": category, "owner": self.module + "." + ".".join(self.lexical),
            "selectors": sorted(self.ownership[id(self.method)]) if self.method else [],
            "expression": expression, "verdict": verdict, "reason": reason,
        }
        if details:
            row.update(details)
        self.sites.append(row)

    def visit_Call(self, node):
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else node.func.id if isinstance(node.func, ast.Name) else "")
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        if name in ORDERING:
            self.add(node, "ordering_assertion")
        elif name.startswith("assert") and TEMPORAL.search(self.text(node)):
            self.add(node, "temporal_assertion")
        timeout = any(key in ("timeout", "timeout_seconds", "deadline")
                      or key.endswith("_timeout_seconds") for key in keywords)
        imported_select = ast.unparse(node.func) in self.posix_select_calls
        if imported_select or name in WAIT_OR_IO_APIS and (timeout or name in CORE_WAIT_OR_IO_APIS):
            self.add(node, "wait_or_io_api", details=wait_or_io_role(
                node, name, keywords, self.posix_select_calls))
        for key, value in keywords.items():
            if ("deadline_seconds" in key and isinstance(value, ast.Constant)
                    and type(value.value) in (int, float) and 0 <= value.value <= 2):
                self.add(node, "short_deadline_keyword",
                         key + "=" + ast.unparse(value) + "; " + self.text(node))
        self.generic_visit(node)

    def visit_BinOp(self, node):
        if (isinstance(node.op, ast.Add) and isinstance(node.left, ast.Call)
                and REAL_CLOCK.search(self.text(node.left)) and isinstance(node.right, ast.Constant)
                and type(node.right.value) in (int, float)):
            self.add(node, "clock_deadline_construction")
        self.generic_visit(node)

    def visit_List(self, node):
        for flag, value in zip(node.elts, node.elts[1:]):
            if (isinstance(flag, ast.Constant) and flag.value in CLI_BUDGET_FLAGS
                    and isinstance(value, ast.Constant) and isinstance(value.value, (str, int, float))):
                try:
                    number = float(value.value)
                except ValueError:
                    continue
                if 0 <= number <= 2:
                    self.add(node, "short_cli_budget")
        self.generic_visit(node)

    visit_Tuple = visit_List


def reviewed_findings(all_tests):
    """Historical source review at the requested base; not final-tip test verdicts."""
    findings = {
        "tests.test_watch.WatchTests.test_unbounded_watch_streams_initial_and_changed_artifacts_before_exit": ("CANDIDATE", "Fixed one-second native initial/change readability success windows conflate startup/publication scheduling with streaming."),
        "tests.test_orchestrate.FleetOrchestratorTests.test_three_controllers_drain_four_item_dag_with_full_receipt_chains": ("CANDIDATE", "Real controller start-spread ceiling is a wall-clock proxy for concurrency; active-controller readiness/barrier expresses the property directly."),
        "tests.test_orchestrate.FleetFaultDrillTests.test_three_controllers_drain_four_item_dag_with_full_receipt_chains": ("CANDIDATE", "Inherited real controller start-spread ceiling is a wall-clock proxy for concurrency."),
        "tests.test_workers.WorkerEffectPipeTests.test_effect_exec_partial_socket_frame_cannot_outlive_worker_deadline": ("CANDIDATE", "Receiver uses 0.05-second deadline but parent requires finished.wait(0.25); receiver/observer scheduling can change outcome."),
        "tests.test_effect_reconciliation_exec.EffectReconciliationExecTests.test_exec_cleanup_failure_precedes_and_chains_pending_base_exception": ("CANDIDATE", "Helper waits 0.1 second after terminate, then asserts collection within another second after kill; reaping observation can be descheduled."),
        "tests.test_gauntlet_recovery.RecoveryGauntletTests.test_deleted_root_under_live_watch_exits_typed_without_traceback": ("CANDIDATE", "Two-second initial native frame deadline; finite three-iteration child may also complete before parent deletion after readiness."),
        "tests.test_watch.WatchTests.test_watch_honours_sigint_when_parent_ignored_it": ("CANDIDATE", "Inherited-SIGINT behavior is checked only after native child exits within fixed five-second watchdog."),
        "tests.test_adhoc_result_io.AdhocResultReadTests.test_fifo_result_refuses_without_blocking": ("CANDIDATE", "Two-second communicate includes cold Python import/startup before FIFO validation; startup delay can mimic FIFO blocking."),
        "tests.test_pi_adapter.PiRpcSessionTests.test_session_distinguishes_malformed_response_and_timeout": ("CANDIDATE", "Cold malformed fixture must beat 0.1-second protocol budget or wrong process_timeout code replaces protocol_error."),
        "tests.test_worker_bootstrap_protocol.WorkerBootstrapProtocolTests.test_channel_round_trips_every_existing_worker_frame_over_real_socketpair": ("KEEP", "Complete data is written synchronously before poll(0.5), so producer startup/scheduling cannot delay readiness."),
        "tests.test_worker_bootstrap_protocol.WorkerBootstrapProtocolTests.test_channel_poll_eof_and_partial_frame_are_bounded_and_typed": ("KEEP", "EOF or partial data is installed synchronously before poll(0.5); immediate negative poll is intentional."),
        "tests.test_codex_binding_stable_path.CodexBindingStablePathTests.test_rebind_requires_current_consent": ("KEEP", "Short consent is identity metadata replaced before exact rebind refusal; no timed native hold executes."),
    }
    publication = sorted(selector for selector in all_tests
                         if selector.startswith("tests.test_codex_publication_surface."))
    for selector in publication:
        findings[selector] = ("CANDIDATE", "Imported separate-Stop fixture uses real two-second native consent; validation/startup or parent scheduling can exhaust it. Exact output/envelope/digest/receipt checks are the property.")
    for unit in ("AbruptBoundaryConvergenceTests", *(
            "AbruptOrdinalConvergenceShard%dTests" % index for index in range(4))):
        findings["tests.harbor_epoch_roll_crash.%s.test_abrupt_cases_converge" % unit] = (
            "CANDIDATE", "The shared native fork-server helper imposed an eight-second reply watchdog around cumulative mutation-probe work. Combined qualification and owned-load RED override its earlier generic watchdog KEEP; actual exit 91 and exact recovery, rather than elapsed latency, are the property.")
    return findings, publication


def build_inventory():
    old, old_identity = selector_input(OLD_INPUT)
    supplied, supplied_identity = selector_input(R2_INPUT)
    tests = ordinary_path(Path("tests"))
    entries = sorted(tests.rglob("*"))
    # pathlib does not recurse into directory symlinks; refuse them explicitly
    # rather than silently omit a purported portion of the census or follow it.
    if any(path.is_symlink() for path in entries):
        raise ValueError("test-tree symlink: audit refuses outside-source ambiguity")
    files, sites, all_tests = [], [], set()
    for path in (path for path in entries if path.suffix == ".py" and path.is_file()):
        relative = path.relative_to(ROOT)
        data = path.read_bytes()
        source = data.decode("utf-8")
        tree = ast.parse(source, filename=relative.as_posix())
        owners, selectors = source_ownership(tree, ".".join(relative.with_suffix("").parts))
        scan = SourceScan(relative, source, owners, tree)
        scan.visit(tree)
        files.append({"path": relative.as_posix(), "sha256": digest(data)})
        sites.extend(scan.sites)
        all_tests.update(selectors)
    findings, publication = reviewed_findings(all_tests)
    native_fixture_consumers = sorted(set(publication) | {
        selector for selector in all_tests
        if selector.startswith("tests.test_f2_codex_unacked_loop.CodexSeparateStopUnackedLoopTests.")
    })
    for site in sites:
        if (site["path"] == "tests/test_f2_codex_unacked_loop.py"
                and any("." + helper in site["owner"] for helper in (
                    "_native_hook", "_obstructed_hook", "launch", "collect",
                    "first_publication", "await_second_start", "release"))):
            site["external_fixture_consumers"] = native_fixture_consumers
        matches = [findings[key] for key in site["selectors"] if key in findings]
        if matches:
            verdict, reason = next((match for match in matches if match[0] == "CANDIDATE"), matches[0])
            site["reviewed_owner_basis"] = "historical source review at 82582381; not current-tip result"
            site["reviewed_owner_verdict"] = verdict
            site["reviewed_owner_reason"] = reason
    extra_comparisons = sorted({selector for site in sites if site["verdict"] == "CANDIDATE"
                                for selector in site["selectors"]} - old - supplied)
    extras = sorted(({key for key, value in findings.items() if value[0] == "CANDIDATE"}
                     | set(extra_comparisons)) - old - supplied)
    if set(extras) - all_tests:
        raise ValueError("extra selector no longer resolves statically: " + repr(sorted(set(extras) - all_tests)))
    identities = [old_identity, supplied_identity]
    source_identity = digest(json.dumps({"sources": files, "selector_inputs": identities},
                                       sort_keys=True, separators=(",", ":")).encode())
    counts = {
        "python_source_files": len(files),
        "static_test_selectors_including_local_inheritance": len(all_tests),
        "sites": len(sites),
        "site_categories": dict(sorted(Counter(site["category"] for site in sites).items())),
        "wait_or_io_bound_roles": dict(sorted(Counter(
            site["bound_role"] for site in sites if site["category"] == "wait_or_io_api").items())),
        "syntactic_site_verdicts": dict(sorted(Counter(site["verdict"] for site in sites).items())),
        "reviewed_selectors": len(findings), "old_selector_artifact": len(old),
        "supplied_round_2_selectors": len(supplied), "extra_selectors": len(extras),
        "sites_with_static_test_callers": sum(bool(site["selectors"]) for site in sites),
        "sites_without_static_test_callers": sum(not site["selectors"] for site in sites),
    }
    methodology = {
        "scope": "All tests/**/*.py parsed as source without imports, discovery or test execution. Only fixed repository-local selector inputs and evidence outputs are additionally read/written.",
        "ordering_assertions": "Every assertLess/assertLessEqual/assertGreater/assertGreaterEqual/assertAlmostEqual AST call, including structural comparisons, to expose denominator and false positives.",
        "temporal_assertions": "Other assert* calls matching elapsed/monotonic/perf_counter/process_time/deadline/duration/latency/start_spread/waited_seconds/remaining/startup/timeout/wait/poll/is_alive/ready in the complete source expression.",
        "wait_or_io_apis": "Core wait/join/communicate/poll/select/recv/recv_bytes/read/readline/readinto/wait_until/wait_for calls are included with or without arguments; get/result/run/check_output/check_call enter only with explicit timeout/deadline keywords. Recognized select imports include aliases. Per-site bound_role distinguishes duration syntax, untimed readiness, I/O arguments, data joins and unresolved signatures. Dynamic values and positional arguments alone are not claimed finite.",
        "short_budgets": "Literal numeric *deadline_seconds keywords and CLI deadline/wait/timeout flag values from zero through two; all time-clock-call plus numeric literal construction.",
        "ownership": "Lexical owner includes nested closures/classes. Caller selectors follow self/cls helper graph and setup/teardown, expanding local inheritance. Imported publication fixture dependencies are explicit. Empty selector arrays retain exact lexical owners and do not assert unused code.",
        "verdicts": "CANDIDATE is static timing sensitivity, not reproduced RED. KEEP gives structural, configuration, controlled-clock, lower-bound, already-ready-data or watchdog reason. Budget KEEP is not downstream load-independence. Reviewed owner records are historical base findings, not claims about a repaired tip. Parent overlays measured dispositions.",
        "limits": "Exhaustive for declared syntax predicates, not whole-program proof. Dynamic aliases, imported inheritance, arbitrary helper calls and inline child-code strings need source review. Static selector count is not discovery parity. Source/input SHA-256 values expose drift.",
        "determinism": "No timestamps, host identity, absolute workspace paths or Git HEAD are generated. Identical tests and selector input bytes produce identical artifacts.",
        "execution": "This generator does not execute tests, mutate Git, skip/retry tests or change timing tolerances.",
    }
    return {
        "schema_version": 1, "generator_revision": 2, "row": "LOADED-WALLCLOCK-1 round 2",
        "requested_base": "82582381", "source_identity_sha256": source_identity,
        "selector_inputs": identities, "counts": counts, "methodology": methodology,
        "source_files": files, "reviewed_selector_basis": "historical source review at requested base 82582381",
        "reviewed_selector_verdicts": [{"selector": key, "verdict": value[0], "reason": value[1]}
                                       for key, value in sorted(findings.items())],
        "extra_selectors": extras, "extra_explicit_comparison_selectors": extra_comparisons,
        "supplied_candidate_selectors": sorted(supplied), "sites": sites,
    }


def render_appendix(document):
    counts = document["counts"]
    lines = [
        "# LOADED-WALLCLOCK-1 round 2 tree audit — 2026-10-25", "",
        "Generated source census; this command runs no tests. Runtime evidence, fixes and final dispositions belong in the main population report.", "",
        "## Enumeration and denominator", "",
        "Scanned **%s Python files**, enumerated **%s source sites**, and identified **%s static test selectors**, including local inheritance. These are AST counts, not discovery/execution counts." % (
            counts["python_source_files"], counts["sites"], counts["static_test_selectors_including_local_inheritance"]), "",
        "| Category | Sites |", "| --- | ---: |",
    ]
    lines.extend("| `%s` | %s |" % pair for pair in counts["site_categories"].items())
    lines.extend([
        "", "The prior population contains %s selectors and the supplied round-2 list %s. JSON `extra_selectors` exports **%s runnable selectors** outside their union. Every site has exact path, line, lexical owner, expression, mapped callers and verdict/reason." % (
            counts["old_selector_artifact"], counts["supplied_round_2_selectors"], counts["extra_selectors"]), "",
        "All ordering/assertAlmostEqual calls are counted, including structural comparisons. Temporal/readiness assertions, wait/I/O calls, short consent/CLI budgets and clock constructions are independently enumerated. An API name does not establish a finite timeout: bound_role distinguishes duration literals, dynamic expressions, untimed readiness, I/O-size arguments, data joins and unresolved signatures. Self/cls helper calls and lifecycle consumers expand to local callers, including local inheritance. Imported publication-fixture consumers are explicit. Empty caller lists do not prove unused code.", "",
        "The census is exhaustive for the JSON methodology predicates, not a whole-program proof. Dynamic aliases, imported inheritance and inline child-code strings need inspection. Source-file and fixed selector-input hashes expose drift. Snapshot identity: `%s`." % document["source_identity_sha256"], "",
        "## Historical source findings", "",
        "These mechanisms were identified at requested base `82582381`; they are retained for provenance and do not assert that repaired current source still has the defect. Current exact sites/lines/hashes are generated independently in JSON. The main report records reproduced or fixed dispositions.", "",
        "| Selector | Base verdict and mechanism |", "| --- | --- |",
    ])
    lines.extend("| `%s` | %s: %s |" % (row["selector"], row["verdict"], row["reason"])
                 for row in document["reviewed_selector_verdicts"])
    lines.extend([
        "", "## Concrete KEEP reasons", "",
        "| Source/property | Reason |", "| --- | --- |",
        "| `test_codex_binding_stable_path.py` short consent | Exact binding/rebind metadata; mocked wake runner and no native timed hold. |",
        "| `test_worker_bootstrap_protocol.py` frame/EOF polls | Data or EOF is installed synchronously before poll; no producer startup race. |",
        "| `test_parallel_cost.py` internal/external brackets | Internal wall/CPU work is enclosed by external measurements of the same invocation, without a fixed completion ceiling. |",
        "| Presence/host observation timestamps | Actual before/after observations bracket the native timestamp rather than imposing a fixed short duration. |",
        "| Elapsed lower bounds | Descheduling alone lengthens the interval; upper-bound siblings remain separately inventoried. |",
        "| Query-work `spent` counters | Charged work units, not seconds. |",
        "| Injected-clock hold/cadence and historical replay values | Controlled clock/ledger arithmetic is independent of native scheduling. |", "",
        "Wait/readiness/I/O sites remain enumerated whether or not a timeout is supplied. POSIX select without its fourth timeout is untimed. Positional recv/recv_bytes byte lengths are not duration evidence; ambiguous custom signatures remain unresolved. Dynamic timeout values are not asserted finite. Fixture-watchdog KEEP does not certify immunity to scheduling delay; reproduced RED overrides it. Budget-construction KEEP describes that syntax role, not downstream load-independence. CANDIDATE means static sensitivity, not a reproduced failure or a passing/fixed claim.", "",
        "## Extra runnable selectors", "",
        "Machine-readable JSON field `extra_selectors` contains the following cases outside the prior/supplied union:", "",
    ])
    lines.extend("- `%s`" % selector for selector in document["extra_selectors"])
    lines.extend([
        "", "## Regeneration", "",
        "```text", "/usr/bin/python3 -B scripts/audit-loaded-wallclock-population.py --write",
        "/usr/bin/python3 -B scripts/audit-loaded-wallclock-population.py --check", "```", "",
        "Check is the default. The generator scans only `tests/**/*.py` and reads the fixed repository-local prior selector and round-2 candidate artifacts. It imports no tests and invokes no Git or subprocesses. Identical source/input bytes reproduce both outputs exactly; no timestamp or HEAD produces artificial drift. Symlinked input/output coordinates or test-tree entries refuse rather than escaping the declared scope.", "",
        "Run after final test edits, inspect candidate dispositions, then check at the frozen source. Runtime commands, counts, skips and owned contention cleanup belong in the main evidence report; the generator does not manufacture them.",
    ])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="regenerate the two fixed evidence outputs")
    mode.add_argument("--check", action="store_true", help="compare without writing (default)")
    args = parser.parse_args()
    document = build_inventory()
    rendered = {INVENTORY: json_text(document), APPENDIX: render_appendix(document)}
    changed = []
    for relative, wanted in rendered.items():
        path = ordinary_path(relative)
        if not path.is_file() or path.read_text(encoding="utf-8") != wanted:
            changed.append(relative.as_posix())
            if args.write:
                path.write_text(wanted, encoding="utf-8")
    print(json.dumps({
        "status": "written" if args.write and changed else "drift" if changed else "ok",
        "changed": changed, "source_identity_sha256": document["source_identity_sha256"],
        "counts": document["counts"],
    }, sort_keys=True))
    return 0 if args.write or not changed else 1


if __name__ == "__main__":
    raise SystemExit(main())
