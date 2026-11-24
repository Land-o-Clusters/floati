"""Am6 population controls: routine work cannot inherit an outside-nine exclusion."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from floati.cli import _parser
from floati.command_contract import project_mcp_surface
from tests import bus_scale_commands as commands, bus_scale_workloads as workloads
from tests.projected_fixture_pins import expected_fixture_sha256


# BEGIN GENERATED BUS SCALE POPULATION
CURRENT_SOURCE = "1f2f40f68d7bb958b2927b9a699058b4ffa54bb0"
EXECUTABLE_COUNT = 151
ADMINISTRATION_COUNT = 58
# END GENERATED BUS SCALE POPULATION
REQUIRED_ROUTINE = frozenset([
    "describe",
    "overlap report",
    "lane open",
    "lane close",
    "sweep",
    "reclaim",
    "send",
    "verify",
    "inbox",
    "wake-evaluate",
    "wake-record",
    "ack",
    "sent",
    "log",
    "status",
    "effects",
    "effect show",
    "graph",
    "doctor",
    "watch",
    "wait",
    "receipts",
    "consume",
    "supervise",
    "presence report",
    "presence show",
    "sequencer status",
    "work add",
    "work claim",
    "work complete",
    "work show",
    "node drain",
    "node boot",
    "node teardown",
    "node explain",
    "node prep-clear",
    "node state-flush",
    "node prompts",
    "role list",
    "role show",
    "role validate",
    "quota show",
    "chart",
    "chart timings",
    "seat board",
    "seat teardown",
    "wake pause",
    "wake resume",
    "wake status",
    "wake arm",
    "wake wait",
    "wake daemon status",
    "context status",
    "context turnover",
    "context policy show",
    "context reading record",
    "hold start",
    "hold release",
    "wake-callback",
    "confluence grant",
    "confluence revoke",
    "confluence status",
    "confluence bundle",
    "intake scan",
    "intake show",
    "intake preview"
])
NONROUTINE = frozenset([
    "work notify",
    "hook relay",
    "ask",
    "replay",
    "confluence adopt",
    "confluence release",
    "effect reconcile",
    "effect compensate",
    "threads",
    "thread attach",
    "thread observe",
    "thread detach",
    "thread show",
    "plan",
    "waiter arm",
    "board",
    "orchestrate",
    "intake adopt",
    "intake dispatch",
    "worker run",
    "worker adhoc profile",
    "worker adhoc run",
    "worker adhoc show",
    "worker adhoc teardown",
    "mcp serve",
    "quota collect"
])


class BusScaleRefreshPopulationTests(unittest.TestCase):
    def test_every_proposed_routine_path_has_its_own_native_case(self):
        measured = [row["command"] for row in workloads._CASES.values()
                    if not row["command"].startswith("seam:")]
        self.assertEqual(REQUIRED_ROUTINE, set(measured))
        self.assertEqual(len(measured), len(set(measured)), "native command aliases cannot substitute for a missing workload")
        self.assertTrue({"consume", "reclaim", "hold start", "hold release", "inbox", "ack",
                         "seat board", "wake pause", "wake resume", "work add", "work claim",
                         "work complete", "work show"} <= set(measured))

    def test_no_current_path_receives_the_legacy_generic_exclusion(self):
        rows = commands.executable_contract(_parser())
        excluded = workloads._exclusions(rows, commands.classify_contract(rows))
        generic = [path for path, reason in excluded.items()
                   if "outside the nine" in reason.lower() or "outside the " in reason.lower()
                   and "scoped" in reason.lower()]
        self.assertEqual([], generic, "every nonroutine exclusion needs a path-specific seat-operation reason")

    def test_current_native_population_has_an_exact_measured_nonroutine_admin_partition(self):
        scope = workloads.scope_contract(_parser())
        # Binding a profile and declaring edit scope preserve the work-add case.
        work_add = scope["inventory"]["work add"]
        options = {option for arg in work_add["arguments"] for option in arg["option_strings"]}
        self.assertTrue({"--adhoc-profile", "--edits"} <= options)
        classified = commands.classify_contract(scope["inventory"])
        self.assertIn("work add", classified["routine"])
        self.assertEqual(["work-add"], [case for case, row in scope["cases"].items()
                                       if row["command"] == "work add"])
        self.assertNotIn("work add", scope["excluded"])
        changed = {**scope["inventory"], "work add": dict(work_add, public=False)}
        self.assertIn("work add", commands.classify_contract(changed)["unknown"])
        # The stopped-intent resolution grammar is frozen by its own amendment
        # and the command keeps its pending workload disposition.
        quiet = {option for arg in scope["inventory"]["quiet release"]["arguments"]
                 for option in arg["option_strings"]}
        self.assertTrue({"--resolve-pid", "--start-token"} <= quiet)
        self.assertTrue(scope["excluded"]["quiet release"].startswith("Native workload pending. "))
        amendment = json.loads(commands._TRAIN_AMENDMENT.read_bytes())
        pending = {" ".join(row["path"]) for row in amendment["commands"]} - {"work add"}
        self.assertEqual(EXECUTABLE_COUNT + 1 + len(pending), len(scope["inventory"]))
        admin = commands.classify_contract(scope["inventory"])["administration"]
        self.assertEqual(ADMINISTRATION_COUNT + 1, len(admin))
        self.assertIn("setup", admin)
        self.assertIn("seat retire", admin)
        self.assertEqual(NONROUTINE | pending | set(admin), set(scope["excluded"]))
        self.assertFalse(REQUIRED_ROUTINE & set(scope["excluded"]))
        self.assertEqual(set(scope["inventory"]), REQUIRED_ROUTINE | {"wake daemon serve"} | set(scope["excluded"]))

    def test_reviewed_fixture_is_current_and_historical_fixture_bytes_remain_exact(self):
        self.assertEqual(CURRENT_SOURCE, commands.BASELINE_SOURCE_SHA)
        historical = Path(__file__).parent / "fixtures/bus-scale-census/native-command-contract-f971841e.json"
        self.assertEqual(expected_fixture_sha256(historical),
                         hashlib.sha256(historical.read_bytes()).hexdigest())


class BusScaleAm7PopulationTests(unittest.TestCase):
    def test_per_wake_callback_has_an_actual_native_command_case(self):
        self.assertIn("wake-callback", {row["command"] for row in workloads._CASES.values()})

    def test_daemon_service_has_a_complete_one_iteration_native_serve_seam(self):
        self.assertIn("seam:floati.wake_daemon.WakeDaemon.serve:one_iteration",
                      {row["command"] for row in workloads._CASES.values()})

    def test_mcp_transport_exclusion_binds_every_exposed_tool_to_measured_implementation(self):
        parser = _parser()
        native_paths = {row["command"] for row in workloads._CASES.values()}
        tools = project_mcp_surface(parser)["tools"]
        def command(tool):
            return " ".join(tool["_meta"]["floati"]["commandPath"])

        pending = {command(tool) for tool in tools if command(tool) not in native_paths}
        self.assertEqual({
            "checkout-liveness", "dispatch show", "needs admission", "needs close",
            "needs file", "needs show", "quiet show", "quiet status", "work handoff show",
        }, pending)
        with self.assertRaisesRegex(ValueError, "served MCP tool has no measured native implementation"):
            workloads.mcp_tool_bindings(parser)
        # The previously covered tools still bind their actual native handlers.
        tools = [tool for tool in tools if command(tool) not in pending]
        with patch.object(workloads, "project_mcp_surface", return_value={"tools": tools}):
            bindings = workloads.mcp_tool_bindings(parser)
        self.assertEqual(CURRENT_SOURCE, bindings["source_sha"])
        self.assertEqual({tool["name"] for tool in tools}, {row["tool"] for row in bindings["tools"]})
        self.assertEqual(len(tools), len(bindings["tools"]))
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
        contracts = commands.executable_contract(parser)
        for row in bindings["tools"]:
            self.assertIn(row["command"], native_paths)
            self.assertEqual(row["command"], workloads._CASES[row["case"]]["command"])
            self.assertTrue(row["implementation"].startswith("floati."))
            self.assertTrue(row["implementation_signature"])
            self.assertEqual(hashlib.sha256(commands._signature(contracts[row["command"]]).encode()).hexdigest(),
                             row["contract_sha256"])
            schema = json.dumps(schemas[row["tool"]], sort_keys=True, separators=(",", ":"), allow_nan=False)
            self.assertEqual(hashlib.sha256(schema.encode()).hexdigest(), row["tool_schema_sha256"])
            source = Path(__file__).resolve().parents[1] / row["source_path"]
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), row["source_sha256"])


if __name__ == "__main__":
    unittest.main()

