#!/usr/bin/env python3
"""Source-only census of bounded test waits, including embedded Python fixtures.

The JSON and Markdown outputs are deterministic snapshots, not test results.
No test module, fixture, subprocess, Git command or product code is executed.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import textwrap


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path("docs/evidence/loaded-wallclock-2-population-2026-10-26")
WAIT_APIS = {"wait", "join", "poll", "communicate", "select", "wait_for", "wait_until", "result", "get", "recv", "readline", "run", "check_output", "check_call", "settimeout"}
READINESS_APIS = {"Readiness", "wait_until", "wait_event", "wait_readable", "wait_poll", "wait_join", "wait_barrier", "wait_process", "wait_get", "wait_queue", "communicate"}
CLOCKS = {"monotonic", "time", "perf_counter"}
EMBEDDED_SYNTAX = re.compile(r"(?:\.(?:wait|join|poll|communicate|select|sleep)\s*\(|(?:monotonic|perf_counter|time)\(\)\s*\+)")
READINESS_NAMES = re.compile(r"ready|startup|started|entered|attempted|locked|checked|published|receiv|first_|observed|marker|prompt|pid|witness|frame", re.I)
RELEASE_NAMES = re.compile(r"release|resume|continue|permit|allow|start$|stop$|barrier", re.I)
PROCESS_NAMES = re.compile(r"process|child|proc$|peer$|leader|zombie|shell|server|follower|holder", re.I)

# Reviewed exclusions name the operation that consumes a deadline. These are
# intentionally narrow: an owner containing "deadline" does not turn its
# independent startup barrier into a product-deadline test.
PRODUCT_OPERATIONS = {
    ("tests/test_native_thread_protocol.py", "test_partial_eof_and_trickle_receive_do_not_reset_timeout"):
        "The partial-frame receiver is supplied an absolute transport deadline; trickle data must not reset it.",
    ("tests/test_native_thread_protocol.py", "test_deadline_send_backpressure_and_legacy_default"):
        "The native sender must enforce its supplied absolute deadline under socket backpressure.",
    ("tests/test_workers.py", "test_effect_exec_partial_socket_frame_never_counts_as_clean_final_drain"):
        "A bounded partial-frame final drain must be typed as failure, not clean EOF.",
    ("tests/test_workers.py", "test_effect_exec_partial_socket_frame_cannot_outlive_worker_deadline"):
        "The partial-frame effect receiver is given the worker deadline being enforced.",
    ("tests/test_pi_deadline_classification.py", "test_queued_malformed_frame_wins_over_an_expired_read_deadline"):
        "The supplied expired protocol deadline tests queued-data versus timeout precedence.",
}
REVIEWED_CONSTRUCTIONS = {
    ("tests/test_seat_teardown_zcode_host.py", "_process"):
        ("data / non-wait", "Constructs a synthetic process birth timestamp after fixture source settlement; no thread, process or readiness deadline consumes this value."),
    ("tests/test_codex_live_adapter.py", "test_git_nonzero_exit_carries_stderr_in_typed_failure_detail"):
        ("product API deadline", "Supplies the Git wrapper's execution deadline while testing nonzero-exit stderr testimony; no first-output barrier."),
    ("tests/test_codex_live_adapter.py", "test_git_timeout_carries_captured_stderr_in_typed_failure_detail"):
        ("product deadline", "Injected Git timeout must carry captured stderr in the typed product failure."),
    ("tests/test_codex_live_adapter.py", "test_git_start_failure_carries_os_error_in_typed_failure_detail"):
        ("product API deadline", "Supplies the Git operation deadline while testing its injected spawn error."),
    ("tests/test_gate_z2_of_fl_1.py", "_maintenance_children_under"):
        ("observer census watchdog", "Bounds a read-only per-PID maintenance census; it does not wait for an owned child or thread to start."),
    ("tests/test_gate_z2_of_fl_1.py", "_run_commit_and_watch"):
        ("observation sampling window", "Samples maintenance presence during a commit; post-commit sampling covers a commit that outlasts this fixed window."),
    ("tests/test_headless_worker_protocol.py", "test_background_writer_is_quiescent_before_artifact_commit"):
        ("absence / exclusion observation", "Deliberately gives an already-terminated background writer a chance to make a forbidden late mutation; correct behavior has no readiness event."),
    ("tests/test_native_io.py", "test_inherited_pipe_drain_uses_original_deadline"):
        ("completion / cleanup watchdog", "After the inherited-pipe deadline result, observes disappearance of the owned writer PID."),
    ("tests/test_seat_teardown_worker_provenance.py", "test_crashed_runner_anchor_keeps_orphan_managed_job_out_of_host_class"):
        ("completion / cleanup watchdog", "The finish cleanup observes the exact managed child's exit after releasing its owned input gate."),
    ("tests/test_workers.py", "test_effect_exec_clean_socket_eof_preserves_process_death_and_drain_semantics"):
        ("product API deadline / already queued data", "Supplies a final-drain deadline after data or EOF is installed synchronously; checks exact process-death/drain semantics."),
    ("tests/test_workers.py", "test_parent_rejects_noncanonical_landlock_backend_as_typed_refusal"):
        ("product API deadline / already queued data", "The fake backend frame is already sent before the parent receiver validates its typed refusal."),
    ("tests/test_workers.py", "test_effect_report_after_result_or_final_ack_is_protocol_error"):
        ("product API deadline / already queued data", "Frames are queued synchronously to test forbidden ordering after result/final ACK, not child startup."),
    ("tests/fixtures/latency_provider.py", "main"):
        ("fixture lifetime watchdog", "Caps the already-ready provider's service loop while it waits for the requested count of questions; first readiness is published before the loop."),
}

# A reviewed fragment must keep its exact file, lexical owner and source SHA.
# A changed or additional assembled snippet requires fresh consumer review.
REVIEWED_FRAGMENTS = {
    ("tests/test_effect_reconciliation_exec.py", "EffectReconciliationExecTests.test_exec_timeout_reaps_observer_and_kills_term_ignoring_descendant_group", "7945ef2c46b093625c6657530d72d9d87e89b7a4b864040e9794d117bca667a5"):
        ("fixture coordination watchdog", "FL-3's prepared observer waits on the parent's request-release marker after launching exactly one helper. The parent bounds PID readiness before releasing the marker; fixture_helper.wait then deliberately blocks that same helper during the unchanged 0.8-second product timeout. Parent-owned cleanup reaps the observer on every exit."),
    ("tests/test_adhoc_runtime.py", "AdhocRuntimeTests.fixture", "173382579388f6c6b00e6e59cf2de291288eb544b03151d293a19579a4e8bf4b"):
        ("fixture delay / assembled child code", "Assembled runtime wrapper starts an owned descendant with a four-second fixture lifetime; no bounded readiness loop in this fragment."),
    ("tests/test_adhoc_stage2_template.py", "AdhocStage2TemplateTests.test_real_background_provider_group_is_absent_after_deadline", "72d67f4ec0733654423986eb8fe6ad786e58f9fd7b23ab013928c882f6fefafe"):
        ("completion / cleanup watchdog", "Assembled supervisor snippet waits up to three seconds only to reap the descendant it has killed; its readiness gates are untimed and parent readiness is separately inventoried."),
    ("tests/test_gauntlet_crash.py", "EffectWorkerCrashTests.test_exec_child_timeout_and_crash_reap_provider_process_groups", "681f7fb6c80d943a96d0aacff4718b8e682977208f46335a8d658693192bcdb5"):
        ("readiness wait already generous", "Generated adapter snippet interpolates the host startup bound and emits ready_bound, elapsed and host_load before its readiness refusal; the exact fragment is retained in JSON."),
    ("tests/test_no_listener_fence.py", "WholeProductNoListenerFenceTests.test_planted_join_and_percent_sites_are_enumerated", "1832a61cfa28b53ba03e964a9f5c5ec494dbdd6180e5631e0a9e73433d3e1e2b"):
        ("documentation / non-wait", "Docstring discusses string-join census sensitivity; it is not executable child code."),
    ("tests/test_opencode_host_bridge.py", "OpenCodeHostBridgeTests.test_timeout_waits_for_owned_receiver_cleanup", "671a5df27a88f99cc2a0c8eb296a0096cb774b0bf1489d547dd9bff9bbd819e1"):
        ("fixture delay / assembled child code", "Assembled TERM-aware receiver intentionally sleeps after stdin to exercise owned receiver cleanup; no first-output success window."),
    ("tests/harbor_parallel_qualification_supervisor.py", "ParallelQualificationSupervisorTests.test_parallel_wall_deadline_is_a_typed_collected_refusal", "f229e4fe19cdb0f519af1ce7150ef628944e13939bfe6bb170a9882afa317e32"):
        ("fixture delay / assembled child code", "Generated numbered unittest class sleeps only after the serial receipt exists, constructing the parallel-run wall-deadline refusal; the supervisor deadline is the property under test."),
}
DIAGNOSTIC_HELPERS = {
    ("LoadedReadinessTests.test_timeout_names_the_budget_elapsed_and_load_for_each_gate", "wait_until", "0"),
    *(("LoadedReadinessTests.test_timeout_names_the_budget_elapsed_and_load_for_each_gate", api, "1")
      for api in ("wait_event", "wait_poll", "wait_join", "wait_barrier", "wait_process", "communicate")),
    ("LoadedReadinessTests.test_native_readiness_and_one_shared_frame_budget", "Readiness", "0.01"),
}
CURSOR_COMPLETION_OWNERS = {
    ("tests/test_cursor_wait_invocation.py", "CursorWaitInvocationTests.test_concurrent_duplicates_exit_while_the_original_keeps_holding", 2),
    ("tests/test_cursor_wait_invocation.py", "CursorWaitInvocationTests.test_codex_duplicate_also_exits_without_starting_another_hold", 2),
    ("tests/test_cursor_wait_invocation.py", "CursorWaitInvocationTests.test_three_sequential_mail_releases_leave_no_live_hook", 3),
    ("tests/test_cursor_wait_invocation.py", "CursorWaitInvocationTests.test_host_exit_retires_its_orphaned_hook_with_native_exit_receipt", 3),
    ("tests/test_cursor_wait_invocation_contract.py", "CursorWaitInvocationContractTests.test_host_exit_retires_hook_even_when_intermediate_parent_survives", 3),
}


def ordinary(relative: Path) -> Path:
    path = ROOT
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlinked census coordinate: " + relative.as_posix())
    return path


def number(node):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = number(node.operand)
        return None if value is None else -value if isinstance(node.op, ast.USub) else value
    return (node.value if isinstance(node, ast.Constant)
            and type(node.value) in (int, float) else None)


def name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return name(node.value) + "." + node.attr
    return ""


class Scan(ast.NodeVisitor):
    def __init__(self, relative, source, *, embedded_at=None, prefix=(), depth=0):
        self.relative, self.source = relative, source
        self.embedded_at, self.depth = embedded_at, depth
        self.owners = list(prefix)
        self.parents = []
        self.rows = []
        self.lines = source.splitlines(keepends=True)
        self.readiness_imports = {}

    def visit(self, node):
        self.parents.append(node)
        result = super().visit(node)
        self.parents.pop()
        return result

    def text(self, node):
        if not hasattr(node, "lineno") or not hasattr(node, "end_lineno"):
            return ast.dump(node)
        lines = self.lines[node.lineno - 1:node.end_lineno]
        if not lines:
            return ast.dump(node)
        if len(lines) == 1:
            return lines[0].encode()[node.col_offset:node.end_col_offset].decode()
        return (lines[0].encode()[node.col_offset:].decode() + "".join(lines[1:-1])
                + lines[-1].encode()[:node.end_col_offset].decode())

    def visit_ImportFrom(self, node):
        if node.module == "tests.readiness":
            self.readiness_imports.update((alias.asname or alias.name, alias.name) for alias in node.names)
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.owners.append(node.name)
        self.generic_visit(node)
        self.owners.pop()

    def visit_FunctionDef(self, node):
        self.owners.append(node.name)
        self.generic_visit(node)
        self.owners.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def add(self, node, kind, classification, reason, *, bound=None):
        row = {
            "path": self.relative.as_posix(),
            "line": self.embedded_at if self.embedded_at is not None else node.lineno,
            "column": node.col_offset,
            "owner": ".".join(self.owners) or "<module>",
            "kind": kind, "classification": classification, "reason": reason,
            "expression": self.text(node),
            "bound_expression": self.text(bound) if bound is not None else None,
        }
        if self.embedded_at is not None:
            row["embedded_line"] = node.lineno
        row["id"] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]
        self.rows.append(row)

    def negative_assertion(self):
        return any(isinstance(parent, ast.Call)
                   and name(parent.func).split(".")[-1] in {"assertFalse", "assertEqual"}
                   and (name(parent.func).endswith("assertFalse")
                        or any(isinstance(arg, (ast.List, ast.Tuple)) and not arg.elts
                               for arg in parent.args))
                   for parent in self.parents[:-1])

    def product_reason(self):
        return next((reason for (path, owner), reason in PRODUCT_OPERATIONS.items()
                     if path == self.relative.as_posix() and owner in self.owners), None)

    def reviewed_construction(self):
        return next((decision for (path, owner), decision in REVIEWED_CONSTRUCTIONS.items()
                     if path == self.relative.as_posix() and owner in self.owners), None)

    def visit_Call(self, node):
        api = name(node.func).split(".")[-1]
        receiver = name(node.func).rsplit(".", 1)[0]
        keywords = {part.arg: part.value for part in node.keywords if part.arg}
        bound = keywords.get("timeout") or keywords.get("seconds")
        if api == "select" and len(node.args) >= 4:
            bound = node.args[3]
        elif api in {"wait", "join", "poll"} and node.args and bound is None:
            bound = node.args[0]
        elif api in {"wait_for", "wait_until"}:
            bound = bound or keywords.get("deadline")
        elif api == "sleep" and node.args:
            bound = node.args[0]
        elif api == "settimeout" and node.args:
            bound = node.args[0]
        elif api == "Readiness" and len(node.args) >= 2:
            bound = node.args[1]

        helper = (name(node.func) in self.readiness_imports
                  or self.relative.as_posix() == "tests/readiness.py"
                  or bound is not None and "readiness.remaining()" in self.text(bound)
                  or api == "wait_until" and self.relative.name in {
                      "test_cursor_wait_invocation.py", "test_cursor_wait_invocation_contract.py"})
        canonical_api = self.readiness_imports.get(name(node.func), api)
        if helper and bound is None:
            timeout_position = 1 if canonical_api == "Readiness" else 3 if canonical_api == "wait_condition" else 2
            if len(node.args) > timeout_position:
                bound = node.args[timeout_position]
        owner = ".".join(self.owners)
        original_protocol_budget = (
            self.relative.name == "test_native_thread_protocol.py"
            and any(part in self.owners for part in ("launch_opened_child", "ready_opened_frame"))
            or self.relative.as_posix() == "tests/fixtures/adhoc-native/stream_harness.py" and canonical_api == "Readiness")
        if helper and original_protocol_budget:
            self.add(node, "shared protocol budget", "product deadline / default readiness budget",
                     "Explicit caller durations retain one original pre-spawn product deadline, including handshake and ACK/drain; omitted duration uses the generous 60-second success-control budget.", bound=bound)
        elif helper and self.relative.name == "test_loaded_wallclock_readiness.py" and bound is not None and (owner, canonical_api, self.text(bound)) in DIAGNOSTIC_HELPERS:
            self.add(node, "readiness helper", "controlled timeout diagnostic",
                     "Constructed immediate/short timeout or mocked nonready gate tests the helper's bound, elapsed and load testimony; it is not a native startup allowance.", bound=bound)
        elif helper and api == "wait_until" and (self.relative.as_posix(), owner, number(keywords.get("seconds"))) in CURSOR_COMPLETION_OWNERS:
            self.add(node, "fixture completion observation", "completion / exclusion observation",
                     "Explicit two/three-second control observes that an already-started duplicate or released/orphan hook exits without queueing; startup has its separate generous wait_started gate.", bound=bound)
        elif helper and (api in WAIT_APIS or api in READINESS_APIS or name(node.func) in self.readiness_imports):
            literal = number(bound)
            if literal is not None and literal < 30:
                category, reason = "readiness wait fixed", "Explicit short helper override reintroduces a startup scheduling window; exact diagnostic, completion or product controls need a reviewed exception."
            elif bound is not None and literal is None:
                category, reason = "readiness helper / dynamic bound", "Delegates to the attributed helper with a dynamic bound; the exact expression is retained rather than asserting its numeric generosity. Current caller/default and product exceptions are reviewed separately."
            else:
                category, reason = "readiness wait already generous", "Uses the shared 60-second default or an explicit literal of at least 30 seconds, with attributed failure text."
            self.add(node, "readiness helper", category, reason, bound=bound)
        elif api == "join" and (isinstance(getattr(node.func, "value", None), (ast.Constant, ast.JoinedStr)) or name(node.func) in {"os.path.join", "os.pathsep.join", "shlex.join"}):
            self.add(node, "join", "data / non-wait", "String join is data construction, not a scheduling wait.")
        elif api == "sleep":
            self.add(node, "sleep", "scheduler sleep / fixture delay",
                     "Scheduling delay; enclosing owner and adjacent readiness/deadline rows identify its role. It is not independently claimed to be a readiness barrier.", bound=bound)
        elif api in WAIT_APIS and (api not in {"get", "result", "recv", "readline", "run", "check_output", "check_call"} or bound is not None):
            if bound is None:
                self.add(node, api, "unbounded / unresolved wait",
                         "No syntactically explicit duration; custom signatures and data APIs require the displayed source context.")
            elif number(bound) == 0:
                self.add(node, api, "immediate observation",
                         "Zero-duration poll observes already available state and imposes no startup success window.", bound=bound)
            elif self.negative_assertion():
                self.add(node, api, "absence / exclusion observation",
                         "Negative assertion observes that an operation stays blocked or that no frame arrives; increasing this observation is not a startup-readiness repair.", bound=bound)
            elif api == "settimeout":
                self.add(node, api, "transport timeout configuration",
                         "Socket API timeout configuration; displayed owner determines whether it is a product protocol deadline or a fixture guard.", bound=bound)
            elif (self.relative.name == "test_seat_teardown_live_waiter.py"
                  and "test_dirty_data_refusal_does_not_retire_live_waiter" in self.owners
                  and api == "wait" and receiver == "job"):
                self.add(node, api, "completion / cleanup watchdog",
                         "Reaps the owned foreign-job fixture after terminate; waiter startup has a separate event gate.", bound=bound)
            elif api in {"communicate", "join", "get", "result", "run", "check_call", "check_output"} or (api == "wait" and (PROCESS_NAMES.search(receiver) or isinstance(getattr(node.func, "value", None), ast.Subscript) or "cleanup" in self.owners)):
                self.add(node, api, "completion / cleanup watchdog",
                         "Bounded process/thread collection, rather than first-output readiness; retained separately so cleanup limits cannot disappear from the population.", bound=bound)
            elif api == "select" and len(node.args) >= 3 and all(isinstance(arg, (ast.List, ast.Tuple)) and not arg.elts for arg in node.args[:3]):
                self.add(node, api, "scheduler sleep / fixture delay",
                         "Select with three empty descriptor sets is a deliberate scheduling delay, not readiness.", bound=bound)
            elif RELEASE_NAMES.search(receiver):
                self.add(node, api, "fixture coordination watchdog",
                         "Release/barrier fallback bounds fixture lifetime; it must remain compatible with the startup wait it supports.", bound=bound)
            elif self.product_reason() and api != "select":
                self.add(node, api, "product deadline", self.product_reason(), bound=bound)
            elif self.relative.name == "test_gauntlet_concurrency.py" and "test_timed_join_remains_bounded_during_serialized_signal_decision" in self.owners:
                self.add(node, api, "product deadline", "Timed join must finish while the serialized signal decision remains held; the bounded join itself is the product promise.", bound=bound)
            elif self.relative.name == "test_native_thread_protocol.py" and "observed_child_output" in self.owners:
                self.add(node, api, "product deadline", "Drain observation preserves the emitter's original protocol deadline plus one-second exit allowance; callers test deadline exhaustion and typed emitter failure.", bound=bound)
            elif self.relative.name == "test_orchestrate.py" and "_FaultAdapter" in self.owners:
                self.add(node, api, "product deadline", "Fixture waits for a fraction of the orchestrator-supplied worker deadline to construct the timeout outcome under test.", bound=bound)
            elif self.relative.name == "test_approval_suspension.py" and self.text(node).startswith("observer_tail_returned.wait"):
                self.add(node, api, "absence / exclusion observation", "Checks whether a transient observer tail escaped the retained rollback visibility guard.", bound=bound)
            elif self.relative.name == "test_native_thread_protocol.py" and self.text(node).startswith("finished.wait"):
                self.add(node, api, "absence / exclusion observation", "A competing append must remain excluded while the authenticated transition's guard is held.", bound=bound)
            elif self.relative.name == "test_watch.py" or "derived_" in self.text(bound) or "liveness_bound" in self.text(bound) or "readiness" in self.text(bound) or "QUIESCENT_DESCENDANT_DEADLINE_SECONDS" in self.text(bound):
                self.add(node, api, "readiness wait already generous",
                         "Uses a named derived/readiness bound; runtime timeout attribution is independently checked by the focused tests.", bound=bound)
            elif self.relative.name == "test_worker_bootstrap_protocol.py":
                self.add(node, api, "product deadline / already queued data",
                         "Protocol controls synchronously install bytes or EOF before poll; negative, boolean and nonfinite arguments test typed timeout validation.", bound=bound)
            elif self.relative.name == "test_thread_source.py" and "ready_select" in self.owners:
                self.add(node, api, "product deadline",
                         "Injected select preserves the source reader's supplied protocol timeout; its fixture handshake is staged separately.", bound=bound)
            elif self.relative.as_posix() == "tests/fixtures/codex-app-server/reference_harness.py" and "start_observed_descendant" in self.owners:
                self.add(node, api, "readiness wait already generous", "The local bound comes from the host-derived handshake derivation; the refusal reports bound, elapsed and host load.", bound=bound)
            elif self.relative.as_posix() == "tests/fixtures/adhoc-native/stream_harness.py" and api == "recv":
                self.add(node, api, "product protocol clock handshake", "Fixture-only clock translation uses the caller's explicit handshake budget before applying the parent-owned original protocol deadline.", bound=bound)
            elif self.relative.name == "test_native_thread_protocol.py" and api == "recv":
                self.add(node, api, "product protocol deadline", "Receives fixture clock/ACK or emitter protocol frames within the parent's original deadline; success startup uses the shared Readiness budget and explicit short cases test ACK/deadline behavior.", bound=bound)
            else:
                value = number(bound)
                generous = value is not None and value >= 30
                self.add(node, api, "readiness wait already generous" if generous else "readiness wait fixed",
                         "Positive bounded readiness/response observation; a static value below 30 seconds or unresolved local bound needs review. Dynamic expressions are not asserted to be constants.", bound=bound)
        elif api in {"readiness_wait", "wait_readable", "wait_ready", "wait_for_readiness"}:
            self.add(node, "readiness helper", "readiness wait already generous",
                     "Explicit shared readiness helper; inspect its implementation and timeout-attribution controls.", bound=bound)
        self.generic_visit(node)

    def visit_BinOp(self, node):
        if isinstance(node.op, ast.Add):
            clock = next((part for part in (node.left, node.right)
                          if isinstance(part, ast.Call)
                          and name(part.func).split(".")[-1] in CLOCKS and not part.args), None)
            if clock is not None:
                bound = node.right if clock is node.left else node.left
                context = " ".join(self.owners) + " " + self.text(node)
                parent = self.parents[-2] if len(self.parents) > 1 else None
                target = " ".join(name(item) for item in parent.targets) if isinstance(parent, ast.Assign) else ""
                product = self.product_reason()
                reviewed = self.reviewed_construction()
                if reviewed:
                    category, reason = reviewed
                elif product:
                    category, reason = "product deadline", product
                elif re.search(r"cleanup|reap|terminate|collect|assert_reaped", context + target, re.I):
                    category, reason = "completion / cleanup watchdog", "Bounds observing owned-process teardown, not first output."
                elif self.relative.name == "test_native_io.py" and "owned_child" in self.owners:
                    category, reason = "product deadline / default readiness budget", "Explicit caller durations remain the owned child's original deadline; omitted duration receives the generous success-control budget."
                elif "derived_" in self.text(bound) or "readiness" in self.text(bound) or "liveness_bound" in self.text(bound):
                    category, reason = "readiness wait already generous", "Deadline derives from a named host/readiness bound."
                elif number(bound) is not None and number(bound) >= 30:
                    category, reason = "readiness wait already generous", "At least 30-second observation backstop; bound/elapsed/load timeout text still needs runtime coverage."
                elif self.relative.name == "test_native_thread_protocol.py" and "observed_child_output" in self.owners:
                    category, reason = "product deadline", "Emitter drain is allowed only the original protocol deadline plus one-second exit allowance."
                elif self.relative.name == "test_watch.py":
                    category, reason = "readiness wait already generous", "Watch helper receives the derived liveness bound and retries once with a freshly measured host bound; its failure includes elapsed and loadavg."
                elif READINESS_NAMES.search(context + target):
                    category, reason = "readiness wait fixed", "Readiness/publication owner constructs a local wall-clock window; displayed bound may be a parameter and is not assumed constant."
                else:
                    category, reason = "deadline construction / consumer review", "Clock-plus-duration syntax alone does not identify a readiness barrier or a product promise; the exact lexical consumer is retained for review."
                self.add(node, "clock deadline", category, reason, bound=bound)
        self.generic_visit(node)

    def visit_Constant(self, node):
        if not isinstance(node.value, str) or not EMBEDDED_SYNTAX.search(node.value) or self.depth >= 3:
            return
        source = textwrap.dedent(node.value)
        try:
            tree = ast.parse(source)
        except SyntaxError:
            fragment_key = (self.relative.as_posix(), ".".join(self.owners), hashlib.sha256(self.text(node).encode()).hexdigest())
            category, reason = REVIEWED_FRAGMENTS.get(fragment_key, (
                "embedded fragment / consumer review", "Timing syntax appears in a non-standalone string fragment; concatenation, interpolation or documentation prevents claiming executable AST coverage."))
            self.add(node, "embedded timing fragment", category, reason)
            return
        child = Scan(self.relative, source, embedded_at=self.embedded_at or node.lineno,
                     prefix=(*self.owners, "<embedded>"), depth=self.depth + 1)
        child.visit(tree)
        self.rows.extend(child.rows)


def inventory():
    entries = sorted(ordinary(Path("tests")).rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise ValueError("symlinked test source: census refuses an ambiguous population")
    sources, rows = [], []
    for path in entries:
        if path.suffix != ".py" or not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        data = path.read_bytes()
        source = data.decode("utf-8")
        scan = Scan(relative, source)
        scan.visit(ast.parse(source, filename=relative.as_posix()))
        sources.append({"path": relative.as_posix(), "sha256": hashlib.sha256(data).hexdigest()})
        rows.extend(scan.rows)
    rows.sort(key=lambda row: (row["path"], row["line"], row.get("embedded_line", 0), row["kind"], row["id"]))
    return {
        "schema_version": 1, "row": "LOADED-WALLCLOCK-2", "sources": sources,
        "source_sha256": hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
        "counts": {"python_files": len(sources), "sites": len(rows),
                   "classifications": dict(sorted(Counter(row["classification"] for row in rows).items()))},
        "method": "AST scan of all tests/**/*.py, wait/join/poll/communicate/select/wait_for/wait_until; explicit timeout get/result/recv/readline/run/check_call/check_output; socket timeout configuration; shared Readiness constructors and imports; sleeps; and monotonic/time/perf_counter additions. Parse timing-bearing literal strings recursively as Python; retain non-standalone timing fragments explicitly.",
        "limits": [
            "Syntactic population, not whole-program proof: dynamic aliases, arbitrary helper names, generated code and nonliteral string assembly can hide behavior.",
            "A positive bounded poll may observe completion rather than startup. Conservative readiness candidates require consumer review; no candidate count is a reproduced failure count.",
            "Data joins, untimed calls, negative observations, coordination and cleanup watchdogs stay visible as supporting categories, rather than being called product deadlines.",
            "Lexical owners are exact source coordinates, not unittest discovery or expanded inherited caller selectors.",
            "Generous/derived classifications describe the bound; they do not alone prove that failure text includes bound, elapsed and os.getloadavg().",
        ],
        "sites": rows,
    }


def markdown(document):
    lines = ["# LOADED-WALLCLOCK-2 readiness population — 2026-10-26", "",
             "Generated by `scripts/audit-readiness-waits.py`; this is source evidence, not an execution receipt.", "",
             f"Scanned {document['counts']['python_files']} Python files and retained {document['counts']['sites']} sites. Snapshot SHA-256: `{document['source_sha256']}`.", "",
             "| Classification | Sites |", "| --- | ---: |"]
    lines += [f"| {category} | {count} |" for category, count in document["counts"]["classifications"].items()]
    lines += ["", document["method"], ""] + ["- " + limit for limit in document["limits"]]
    lines += ["", "Each row records the exact owner, bound and reason. Embedded line numbers refer to the parsed literal, anchored to its containing source line. Consumer-review categories preserve uncertainty; they must not be counted as cleared readiness waits.", "",
              "| Location / owner | Kind | Classification | Bound | Reason |", "| --- | --- | --- | --- | --- |"]
    for row in document["sites"]:
        location = f"{row['path']}:{row['line']}"
        if "embedded_line" in row:
            location += f" (embedded:{row['embedded_line']})"
        cells = [f"`{location}` `{row['owner']}`", row["kind"], row["classification"], row["bound_expression"] or "—", row["reason"]]
        lines.append("| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in cells) + " |")
    lines += ["", "Regenerate with `/usr/bin/python3 -B scripts/audit-readiness-waits.py --write`; verify identical source with `--check`. JSON retains each full expression and source hash.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--write", action="store_true")
    modes.add_argument("--check", action="store_true")
    args = parser.parse_args()
    document = inventory()
    outputs = {Path(str(OUTPUT) + ".json"): json.dumps(document, indent=2, sort_keys=True) + "\n",
               Path(str(OUTPUT) + ".md"): markdown(document)}
    changed = []
    for relative, content in outputs.items():
        path = ordinary(relative)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            changed.append(relative.as_posix())
            if args.write:
                path.write_text(content, encoding="utf-8")
    unresolved = [row["id"] for row in document["sites"]
                  if row["classification"] in {"readiness wait fixed", "deadline construction / consumer review", "embedded fragment / consumer review"}]
    status = ("written" if args.write and changed else "drift" if changed
              else "unresolved_readiness" if unresolved and not args.write else "ok")
    print(json.dumps({"status": status, "changed": changed,
                      "unresolved_readiness_ids": unresolved, "counts": document["counts"]}, sort_keys=True))
    return 0 if args.write or not changed and not unresolved else 1


if __name__ == "__main__":
    raise SystemExit(main())
