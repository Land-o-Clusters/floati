"""Strict native scale instrument controls and the current-source RED.

The product test stops at the first failing command. Later commands remain
unmeasured, never skipped or accepted by exemption.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from tests.bus_scale_test_profile import StableSourceFixture
from unittest.mock import patch

from floati.cli import _parser
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import full_scale_enabled

_BOUNDED_SUBJECT = None


class CensusInstrumentTests(unittest.TestCase):
    def test_collected_child_must_match_the_preparation_interpreter(self):
        from types import SimpleNamespace
        subject = importlib.import_module("tests.bus_scale_census")
        tag = sys.implementation.cache_tag

        for child_tag in (tag, tag + "-different"):
            with self.subTest(child_tag=child_tag), tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
                bank = Path(temporary)

                def launch(argv, **kwargs):
                    self.assertEqual(sys.executable, argv[0])
                    cleanup = Path(argv[-1])
                    with tempfile.TemporaryDirectory(prefix="suite-home-", dir=REAL_TEMP_ROOT) as home,\
                            patch("tests.HOME_PIN_ROOT", home), patch.object(subject.runpy, "run_path"):
                        subject._native_child(argv[4:-1], cleanup)
                    if child_tag != tag:
                        receipt = json.loads(cleanup.read_text())
                        receipt["cache_tag"] = child_tag
                        cleanup.write_text(json.dumps(receipt))
                    return SimpleNamespace(pid=123)

                delegate = SimpleNamespace(launch_owned_child=launch,
                    collect_owned_child=lambda child, **kwargs: {"child_collected": True, "exit_code": 0})

                def collect(case, place):
                    with patch.object(subject, "_load", return_value=delegate):
                        owner = subject._ContinuationOwner(time.monotonic() + 60)
                        child = owner.launch_owned_child(["/usr/bin/python3", "-B", str(subject.ENTRY),
                            "cpu", case, str(place), str(place), str(place / "detail.json"), "owned-census-v1"],
                            stdout=SimpleNamespace(name=str(place / "stdout")))
                        return owner.collect_owned_child(child, deadline_seconds=30, collection_seconds=5)

                if child_tag == tag:
                    receipt = collect("wake-daemon-serve", bank)
                    self.assertEqual(tag, receipt["home_cleanup"].get("cache_tag"))
                    self.assertEqual(tag, receipt.get("preparation_cache_tag"))
                else:
                    with patch.object(subject, "_source_witness", return_value={}):
                        result = subject.remaining_census(bank, cases=["wake-daemon-serve"], case_runner=collect)
                    error = result["errors"]["wake-daemon-serve"]
                    self.assertEqual("census_interpreter_mismatch", error.get("code"))
                    self.assertEqual((tag, child_tag), (error.get("preparation_cache_tag"), error.get("child_cache_tag")))
                    command = subject._load(subject.ROOT / "scripts/bus-scale-census.py", "interpreter_refusal_control")
                    self.assertEqual({"status": "refused", "evidence": {"code": error["code"],
                        "cases": {"wake-daemon-serve": error["code"]}}}, command.instrument_refusal(result))

    def test_nonconsumer_preparation_does_not_spend_budget_on_unused_cursor_proofs(self):
        """Nearest: initial seed and measured/preparation scripted-clock controls."""
        from contextlib import ExitStack
        from types import SimpleNamespace
        subject = importlib.import_module("tests.bus_scale_census")
        clock = [0.0]
        final_restoration = [False]

        class PreparedNativeInput(RuntimeError):
            pass

        def repository(base):
            path = base / "source"
            path.mkdir(mode=0o700)
            return path, "a" * 40

        def seeds(case, scratch, workspace, entry, deadline):
            result = {"history": {}, "context": {}, "prepared_home": scratch / "prepared"}
            for scale in ("1", "10", "prepared_home"):
                path = result[scale] if scale == "prepared_home" else scratch / scale
                path.mkdir(mode=0o700)
                (path / "events.jsonl").touch(mode=0o600)
                result[scale] = path
            return result

        def restore(*args):
            receipt = {"status": "restored"}
            subject._write_json(Path(args[5]), receipt)
            return receipt

        def warm(home, *, cursor_preflights=None, **kwargs):
            # Issuing an unused complete capability can consume the entire
            # ordinary-case budget; the real launch gate must still apply.
            clock[0] += 1
            if cursor_preflights is not None or final_restoration[0]:
                clock[0] += 600
            if cursor_preflights is not None:
                cursor_preflights.append(object())
            return {"status": "complete", "outside_child_measurement": True}

        def launch(*args, **kwargs):
            raise PreparedNativeInput("native input reached its unchanged launch gate")

        observer = SimpleNamespace(restore=restore)

        def observe(owner, case, pair, scale, workspace, place, entry):
            name = "final-restore.json" if final_restoration[0] else "cpu-restore.json"
            observer.restore(pair, pair[scale], {}, None, None, place / name)
            owner.launch_owned_child(["/usr/bin/python3", "-B", str(subject.ENTRY), "cpu", case,
                str(pair[scale]), str(workspace), str(place / "detail.json"), "owned-census-v1"],
                stdout=SimpleNamespace(name=str(place / "stdout.txt")))

        observer.expanded_observation = observe
        producer = SimpleNamespace(native_entry_module=lambda: SimpleNamespace(),
            expanded_adapter=lambda: observer)
        delegate = SimpleNamespace(launch_owned_child=launch)
        with tempfile.TemporaryDirectory(prefix="bus-scale-unused-preflight-clock-",
                dir=REAL_TEMP_ROOT) as temporary, ExitStack() as stack:
            base_bank = Path(temporary).resolve()
            stack.enter_context(patch("tests.bus_scale_test_profile.banked_send_repository", repository))
            stack.enter_context(patch.object(subject, "_expanded_native_seeds", side_effect=seeds))
            stack.enter_context(patch.object(subject, "_qualify_expanded_family"))
            stack.enter_context(patch.object(subject, "_warm_fixture", side_effect=warm))
            stack.enter_context(patch.object(subject, "_load", side_effect=lambda path, name:
                delegate if Path(path).name.startswith("run-") else producer))
            stack.enter_context(patch.object(subject, "_source_witness", return_value={}))
            stack.enter_context(patch.object(subject.time, "monotonic", side_effect=lambda: clock[0]))
            stack.enter_context(patch.object(subject.os, "getloadavg", return_value=(1, 1, 1)))
            for final in (False, True):
                with self.subTest(final_restoration=final):
                    bank = base_bank / str(final)
                    bank.mkdir(mode=0o700)
                    clock[0], final_restoration[0] = 0.0, final
                    observer.restore = restore
                    with self.assertRaises(RuntimeError) as caught:
                        subject._remaining_case("graph", bank)
                    self.assertIsInstance(caught.exception, PreparedNativeInput,
                        "unused preparation consumed the ordinary-case launch budget")

    def test_initial_wait_seed_preparation_uses_its_own_cap_before_measured_children(self):
        """Keep real seed deadline checks and the real continuation accounting."""
        from contextlib import ExitStack
        import shutil
        from types import SimpleNamespace
        subject = importlib.import_module("tests.bus_scale_census")
        for preparation in (600, 1711):
            with self.subTest(preparation=preparation), tempfile.TemporaryDirectory(
                    prefix="bus-scale-initial-wait-clock-", dir=REAL_TEMP_ROOT) as temporary:
                bank = Path(temporary).resolve()
                clock, next_pid, launched = [0.0], [0], []

                def repository(base):
                    path = base / "source"
                    path.mkdir(mode=0o700)
                    return path, "a" * 40

                class Events:
                    def __init__(self, root):
                        self.number = 0
                    def send(self, *args, **kwargs):
                        self.number += 1
                        if self.number == 1:
                            clock[0] += preparation
                        return {"message": {"id": str(self.number)}}
                    def present(self, node):
                        return [{"id": str(self.number)}], None
                    def unacked_ids(self, node):
                        return []

                def launch(argv, **kwargs):
                    next_pid[0] += 1
                    launched.append(argv)
                    Path(argv[-1]).write_text(json.dumps({
                        "native_home": str(bank / "removed-home"), "home_removed": True,
                        "cache_tag": sys.implementation.cache_tag}))
                    return SimpleNamespace(pid=next_pid[0])

                def collect(child, **kwargs):
                    clock[0] += 1
                    return {"child_collected": True, "status": "complete", "exit_code": 0}

                delegate = SimpleNamespace(launch_owned_child=launch, collect_owned_child=collect)
                producer = SimpleNamespace(native_entry_module=lambda: SimpleNamespace())

                def dependency(path, name):
                    return delegate if Path(path).name.startswith("run-") else producer

                def observe(case, seed, workspace, place, owner, producer):
                    place.mkdir(parents=True, mode=0o700)
                    for mode in ("cpu", "diagnostic"):
                        argv = ["/usr/bin/python3", "-B", str(subject.ENTRY), mode, case,
                            str(seed), str(workspace), str(place / "detail.json"), "owned-census-v1"]
                        child = owner.launch_owned_child(argv,
                            stdout=SimpleNamespace(name=str(place / (mode + "-stdout.txt"))))
                        owner.collect_owned_child(child, deadline_seconds=70, collection_seconds=5)
                    return dict(cpu_seconds=1, read_bytes=1, ledger_hold_seconds=1), {}

                # Native seed iteration/deadlines, pair ordering, child budget,
                # final accounting, and typed case evidence all stay real.
                with ExitStack() as stack:
                    for target, replacement in (
                            ("tests.bus_scale_test_profile.banked_send_repository", repository),
                            ("floati.events.EventLog", Events),
                            ("floati.registry.Registry", lambda root: SimpleNamespace(register=lambda *args: None)),
                            ("floati.cursor.SparseCursor", lambda root: SimpleNamespace(ack=lambda *args, **kwargs: None))):
                        stack.enter_context(patch(target, replacement))
                    stack.enter_context(patch("floati.root.FloatiRoot.open_direct_home", return_value=SimpleNamespace()))
                    stack.enter_context(patch.object(subject, "_load", side_effect=dependency))
                    stack.enter_context(patch.object(subject.time, "monotonic", side_effect=lambda: clock[0]))
                    stack.enter_context(patch.object(subject.os, "getloadavg", return_value=(1, 1, 1)))
                    stack.enter_context(patch.object(subject, "_source_witness", return_value={}))
                    stack.enter_context(patch.object(subject, "_freeze_native_seed",
                        side_effect=lambda source, destination, receipt: shutil.copytree(source, destination)))
                    stack.enter_context(patch.object(subject, "_prepare_wait_seeds"))
                    stack.enter_context(patch.object(subject, "original_family_coverage", return_value={}))
                    stack.enter_context(patch.object(subject, "_original_observation", side_effect=observe))
                    if preparation < 1710:
                        result = subject._remaining_case("wait-start", bank)
                        self.assertTrue(result["native_complete"])
                        self.assertEqual(40, result["clock_accounting"]["measured_seconds"])
                        self.assertEqual(preparation, result["clock_accounting"]["preparation_seconds"])
                    else:
                        with self.assertRaises(subject.PreparationDeadline):
                            subject._remaining_case("wait-start", bank)
                        result = json.loads((bank / "case.json").read_text())
                        self.assertEqual("preparation_deadline", result["error"]["code"])
                        self.assertTrue(result["error"]["instrument_finding"])
                        self.assertEqual([], launched)

    def test_preparation_does_not_spend_measured_budget_but_child_overrun_does(self):
        from types import SimpleNamespace
        subject = importlib.import_module("tests.bus_scale_census")
        clock, windows = [0.0], []
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary).resolve()
            def launch(argv, **kwargs):
                Path(argv[-1]).write_text(json.dumps({"native_home": str(base / "removed"),
                    "home_removed": True, "cache_tag": sys.implementation.cache_tag}))
                return SimpleNamespace(pid=123)
            def collect(child, **kwargs):
                windows.append(kwargs["deadline_seconds"])
                clock[0] += 10
                return {"child_collected": True, "status": "complete", "exit_code": 0}
            delegate = SimpleNamespace(launch_owned_child=launch, collect_owned_child=collect)
            with patch.object(subject, "_load", return_value=delegate), patch.object(
                    subject.time, "monotonic", side_effect=lambda: clock[0]):
                owner = subject._ContinuationOwner(570)
                clock[0] += 600  # Clone, warm, verify: no child has run.
                output = SimpleNamespace(name=str(base / "stdout.txt"))
                argv = ["/usr/bin/python3", "-B", str(subject.ENTRY), "cpu", "wait-start",
                    str(base), str(base), str(base / "detail.json"), "owned-census-v1"]
                child = owner.launch_owned_child(argv, stdout=output)
                owner.collect_owned_child(child, deadline_seconds=70, collection_seconds=5)
                self.assertEqual([70], windows, "preparation consumed the command's collection budget")
                self.assertEqual(10, owner.measured_seconds)
                self.assertEqual(600, owner.preparation_seconds)
                clock[0] += 600
                child = owner.launch_owned_child(argv, stdout=output)
                clock[0] += 560
                owner.collect_owned_child(child, deadline_seconds=70, collection_seconds=5)
                with self.assertRaisesRegex(RuntimeError, "measured.*budget"):
                    owner.launch_owned_child(argv, stdout=output)
                clock[0] = 0
                owner = subject._ContinuationOwner(570)
                clock[0] = 1711
                with self.assertRaisesRegex(RuntimeError, "preparation_deadline"):
                    owner.launch_owned_child(argv, stdout=output)
                self.assertEqual(0, owner.measured_seconds)
                self.assertEqual(1711, sum(owner.accounting()["preparation_phases"].values()))

    def test_expanded_case_pair_clocks_include_all_children_and_final_preparation(self):
        from contextlib import ExitStack, redirect_stderr
        from io import StringIO
        from types import SimpleNamespace
        subject = importlib.import_module("tests.bus_scale_census")
        for case in ("seat-teardown", "wake-daemon-serve", "wake-wait"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary, ExitStack() as stack:
                bank, clock, children = Path(temporary), [0.0], []
                progress = stack.enter_context(redirect_stderr(StringIO()))

                def repository(base):
                    path = base / "repo"
                    path.mkdir()
                    return path, "a" * 40

                def seeds(name, scratch, workspace, entry, deadline, **kwargs):
                    clock[0] += 600
                    pair = {"history": {}, "context": {}, "prepared_home": scratch / "prepared"}
                    for scale in ("1", "10"):
                        pair[scale] = scratch / scale
                        pair[scale].mkdir()
                    return pair

                def launch(argv, **kwargs):
                    children.append((argv[4], argv[5]))
                    Path(argv[-1]).write_text(json.dumps({"native_home": str(bank / "removed"),
                        "home_removed": True, "cache_tag": sys.implementation.cache_tag}))
                    return SimpleNamespace(pid=len(children))

                def collect(child, **kwargs):
                    clock[0] += 1
                    return {"child_collected": True, "status": "complete", "exit_code": 0}

                def observe(owner, case, seeds, scale, workspace, place, entry):
                    clock[0] += 2
                    for mode in ("cpu", "diagnostic"):
                        output = place / mode
                        output.mkdir()
                        argv = ["/usr/bin/python3", "-B", str(entry), mode, case, str(seeds[scale]),
                                str(workspace), str(output / "detail.json"), "owned-census-v1"]
                        child = owner.launch_owned_child(argv, stdout=SimpleNamespace(name=str(output / "stdout")))
                        owner.collect_owned_child(child, deadline_seconds=120, collection_seconds=10)
                    subject._write_json(output / "detail.json", {"hold_families": {}})
                    subject._write_json(place / "collection.json", {})
                    return {"native": {"exit_code": 0}, "child_collected": True,
                            "coverage": {"cpu": True, "bytes": True, "locks": True},
                            "metrics": {"cpu_seconds": 1, "bytes_read": 1}}

                original_open = subject.tarfile.open
                def archive(*args, **kwargs):
                    clock[0] += 5
                    return original_open(*args, **kwargs)

                delegate = SimpleNamespace(launch_owned_child=launch, collect_owned_child=collect)
                observer = SimpleNamespace(restore=None, expanded_observation=observe)
                producer = SimpleNamespace(native_entry_module=lambda: None, expanded_adapter=lambda: observer)
                stack.enter_context(patch("tests.bus_scale_test_profile.banked_send_repository", repository))
                stack.enter_context(patch.object(subject, "_expanded_native_seeds", seeds))
                stack.enter_context(patch.object(subject, "_qualify_expanded_family"))
                stack.enter_context(patch.object(subject, "_source_witness", return_value={}))
                stack.enter_context(patch.object(subject, "_load", side_effect=lambda path, name:
                    delegate if path.name.startswith("run-") else producer))
                stack.enter_context(patch.object(subject.time, "monotonic", side_effect=lambda: clock[0]))
                stack.enter_context(patch.object(subject.tarfile, "open", side_effect=archive))
                result = subject._remaining_case(case, bank)
                self.assertTrue(result["native_complete"])
                self.assertEqual([("cpu", case), ("diagnostic", case)] * 10, children)
                receipt = json.loads((bank / "case.json").read_text())
                clocks = [json.loads(line.split(" ", 1)[1]) for line in progress.getvalue().splitlines()
                          if line.startswith("BUS_SCALE_CLOCK ")]
                self.assertEqual(receipt["pair_clocks"], clocks[:-1])
                self.assertEqual(list(range(1, 6)), [row["pair"] for row in clocks[:-1]])
                for row in clocks[:-1]:
                    self.assertEqual((4, 4), (row["measured_seconds"], row["preparation_seconds"]))
                totals = receipt["clock_accounting"]
                self.assertEqual((20, 625), (totals["measured_seconds"], totals["preparation_seconds"]))
                for key in ("measured_seconds", "preparation_seconds"):
                    self.assertEqual(totals[key], clocks[-1][key])

    def test_expanded_budget_refusals_identify_preparation_measured_command_and_collection_clocks(self):
        from types import SimpleNamespace
        subject = importlib.import_module("tests.bus_scale_census")
        scenarios = (
            ("preparation_deadline", 1711, 0, 0, None),
            ("measured_deadline", 0, 568, 3, "deadline"),
            ("measured_deadline", 0, 568, 3, "complete"),
            ("child_command_deadline", 0, 0, 120, "deadline"),
            ("child_collection_deadline", 0, 0, 130, "uncollected"),
        )
        for code, prep, previous, elapsed, status in scenarios:
            with self.subTest(code=code, status=status), tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
                base, clock = Path(temporary), [0.0]
                def launch(argv, **kwargs):
                    Path(argv[-1]).write_text(json.dumps({"native_home": str(base / "removed"), "home_removed": True}))
                    return SimpleNamespace(pid=12)
                def collect(child, **kwargs):
                    self.assertEqual(10, kwargs["collection_seconds"])
                    self.assertEqual(min(120, 570 - previous), kwargs["deadline_seconds"])
                    clock[0] += elapsed
                    return {"child_collected": status != "uncollected", "status": status, "exit_code": 0}
                delegate = SimpleNamespace(launch_owned_child=launch, collect_owned_child=collect)
                with patch.object(subject, "_load", return_value=delegate), patch.object(
                        subject.time, "monotonic", side_effect=lambda: clock[0]):
                    owner = subject._ContinuationOwner(570)
                    owner.measured_seconds = previous
                    clock[0] = previous + prep
                    argv = ["/usr/bin/python3", "-B", str(subject.ENTRY), "cpu", "wake-wait",
                            str(base), str(base), str(base / "detail.json"), "owned-census-v1"]
                    with self.assertRaises(RuntimeError) as error:
                        child = owner.launch_owned_child(argv, stdout=SimpleNamespace(name=str(base / "stdout")))
                        owner.collect_owned_child(child, deadline_seconds=120, collection_seconds=10)
                    self.assertEqual(code, getattr(error.exception, "code", None))
                    self.assertEqual(code == "child_collection_deadline", bool(owner.homes))

    def calibration(self, kind):
        subject = self.subject()
        if full_scale_enabled():
            return subject.calibrate(kind)

        def collect(argv, place, **kwargs):
            # Exercise the actual child body on tiny bytes, including its
            # real product lock and diagnostic probe. Controlled metrics then
            # test five-pair classification without a tiny timing verdict.
            home, native_output = Path(argv[-2]), place / "native-control.json"
            with tempfile.TemporaryDirectory(prefix="bus-scale-calibration-home-",
                                             dir=REAL_TEMP_ROOT) as scratch_home:
                with patch.dict(os.environ, {"HOME": scratch_home}):
                    subject._fixture_child(kind, argv[-3], home, native_output)
            self.assertFalse(Path(scratch_home).exists())
            native = json.loads(native_output.read_text())
            self.assertGreater(native["cpu_seconds"], 0)
            if argv[-3] == "diagnostic":
                self.assertEqual((home / "events.jsonl").stat().st_size, native["read_bytes"])
                self.assertTrue(any(row["path"] == "events.jsonl.lock" for row in native["holds"]))
            scale = int(place.parent.name)
            value = scale if kind == "linear" else 1
            Path(argv[-1]).write_text(json.dumps(dict(home_removed=True,
                transport_profile="bounded_in_process", native_control=str(native_output),
                cpu_seconds=value, read_bytes=value, ledger_hold_seconds=value)))
            return {"cpu_seconds": value, "child_collected": True}

        return subject.calibrate(kind, collector=collect, fixture_bytes=16)

    def subject(self):
        try:
            module = importlib.import_module("tests.bus_scale_census")
            if full_scale_enabled():
                return module
            global _BOUNDED_SUBJECT
            if _BOUNDED_SUBJECT is None:
                from tests.bus_scale_bounded_transport import bounded_module
                spec = importlib.util.spec_from_file_location("bus_scale_default_instrument", module.__file__)
                _BOUNDED_SUBJECT = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(_BOUNDED_SUBJECT)
                _BOUNDED_SUBJECT = bounded_module(_BOUNDED_SUBJECT)
            return _BOUNDED_SUBJECT
        except ModuleNotFoundError as error:
            if error.name != "tests.bus_scale_census":
                raise
            self.fail("native scale instrument is missing")

    def test_planted_linear_command_is_red_on_all_three_measured_metrics(self):
        result = self.calibration("linear")
        print("BUS_SCALE_CALIBRATION linear " + json.dumps(result["metrics"], sort_keys=True), flush=True)
        for metric, row in result["metrics"].items():
            with self.subTest(metric=metric):
                self.assertEqual("red", row["classification"], row)
                self.assertGreater(row["ratio"], 1.5)

    def test_constant_command_is_green_on_all_three_measured_metrics(self):
        result = self.calibration("constant")
        print("BUS_SCALE_CALIBRATION constant " + json.dumps(result["metrics"], sort_keys=True), flush=True)
        for metric, row in result["metrics"].items():
            with self.subTest(metric=metric):
                self.assertEqual("pass", row["classification"], row)
                self.assertLessEqual(row["ratio"], 1.5)

    def test_new_parser_leaf_cannot_silently_leave_the_census(self):
        subject = self.subject()
        parser = _parser()
        scope = subject.population(parser)
        self.assertIn("seat-teardown", scope["cases"])
        self.assertIn("wait-idle", scope["cases"])
        action = next(a for a in parser._actions if hasattr(a, "choices") and isinstance(a.choices, dict))
        leaf = action.add_parser("scale-planted-command")
        leaf.set_defaults(handler=lambda args: None)
        with self.assertRaises(ValueError):
            subject.population(parser)

    def test_record_lock_is_included_in_longest_ledger_hold(self):
        self.assertEqual(7, self.subject().longest_ledger_hold([
            {"path": "events.jsonl.lock", "seconds": 2},
            {"path": "locks/records.lock", "seconds": 7},
            {"path": "receipts/wake-coordination/a/lane.lock", "seconds": 99},
        ]))


class CensusOutputProportionalTests(unittest.TestCase):
    """Measured publications normalize CPU and reads; holds stay absolute."""

    def pairs(self, *, read=1000, emitted=1000, cpu=1.0, hold=1.0):
        return [{"order": [1, 10] if index % 2 == 0 else [10, 1],
                 "1": {"cpu_seconds": 1.0, "read_bytes": 100,
                       "bytes_emitted": 100, "ledger_hold_seconds": 1.0},
                 "10": {"cpu_seconds": cpu, "read_bytes": read,
                        "bytes_emitted": emitted, "ledger_hold_seconds": hold}}
                for index in range(5)]

    def compare(self, case, pairs, **kwargs):
        subject = CensusInstrumentTests().subject()
        compare = getattr(subject, "compare_case_pairs", None)
        self.assertTrue(callable(compare), "missing contract-bound output_proportional judgment")
        return compare(case, pairs, **kwargs)

    def test_complete_history_bytes_are_judged_per_emitted_byte_in_all_five_pairs(self):
        result = self.compare("log-history", self.pairs())
        self.assertEqual("output_proportional", result["disposition"]["class"])
        self.assertEqual("log --help", result["disposition"]["citation"])
        self.assertIn("Read canonical ledgers", result["disposition"]["help_text"])
        row = result["metrics"]["read_bytes"]
        self.assertEqual("pass", row["classification"])
        self.assertEqual(10.0, row["raw_ratio"])
        self.assertEqual(1.0, row["ratio"])
        self.assertEqual([1.0] * 5, row["pair_ratios"])
        self.assertEqual({"1": 500, "10": 5000}, row["bytes_emitted"])

    def test_log_claimant_documents_complete_retained_output(self):
        from floati.helptext import help_for
        self.assertIn("complete retained message history", " ".join(help_for(["log", "--help"]).split()).lower())

    def test_output_growth_normalizes_cpu_but_never_ledger_hold(self):
        result = self.compare("receipts-history", self.pairs(cpu=2, hold=6.4))
        self.assertEqual("receipts --help", result["disposition"]["citation"])
        self.assertEqual("pass", result["metrics"]["cpu_seconds"]["classification"])
        self.assertEqual(2.0, result["metrics"]["cpu_seconds"]["raw_ratio"])
        self.assertEqual(0.2, result["metrics"]["cpu_seconds"]["ratio"])
        self.assertEqual([0.2] * 5, result["metrics"]["cpu_seconds"]["pair_ratios"])
        self.assertEqual("cpu_seconds / bytes_emitted", result["metrics"]["cpu_seconds"]["basis"])
        self.assertEqual("red", result["metrics"]["ledger_hold_seconds"]["classification"])
        self.assertEqual(6.4, result["metrics"]["ledger_hold_seconds"]["ratio"])
        self.assertEqual("pass", result["metrics"]["read_bytes"]["classification"])

    def test_cpu_growing_faster_than_stdout_still_red_at_exact_gate(self):
        for cpu, expected in ((15, "pass"), (15.01, "red")):
            with self.subTest(cpu=cpu):
                row = self.compare("log-history", self.pairs(cpu=cpu))["metrics"]["cpu_seconds"]
                self.assertEqual(expected, row["classification"])

    def test_complete_bundle_history_claim_is_documented_and_measured(self):
        subject = CensusInstrumentTests().subject()
        self.assertEqual("output_proportional", subject.case_disposition("confluence-bundle")["class"],
                         "the complete exported history needs its documented output-cost class")
        result = self.compare("confluence-bundle", self.pairs(cpu=2, hold=2))
        self.assertEqual("confluence bundle --help", result["disposition"]["citation"])
        self.assertEqual("pass", result["metrics"]["read_bytes"]["classification"])
        self.assertEqual("pass", result["metrics"]["cpu_seconds"]["classification"])
        self.assertEqual("red", result["metrics"]["ledger_hold_seconds"]["classification"])

    def test_bundle_emitted_count_adds_exact_measured_file_and_ignores_self_report(self):
        import hashlib
        subject = CensusInstrumentTests().subject()
        measure = getattr(subject, "_expanded_emitted_bytes", None)
        self.assertTrue(callable(measure), "complete bundle output requires captured stdout byte testimony")
        with tempfile.TemporaryDirectory(prefix="bus-scale-emitted-control-", dir=REAL_TEMP_ROOT) as directory:
            place = Path(directory).resolve()
            home = place / "prepared/scale-fleet"
            publication = home.parent / "routine-bundle.json"
            seeds = {"prepared_home": home, "context": {"case": "confluence-bundle", "publication_name": "routine-bundle.json"}}
            bank = place / "diagnostic"
            (bank / "auxiliary").mkdir(parents=True)
            raw = b'{"complete":"exported history"}\n'
            archived = bank / "auxiliary/0"
            archived.write_bytes(raw)
            stdout = json.dumps({"evidence": {"out": str(publication), "bytes_emitted": 10**12}}).encode() + b"\n"
            (bank / "stdout.txt").write_bytes(stdout)
            inventory = {"kind": "file", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            record = {"diagnostic": {"stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                      "auxiliary_post_state": [{"root": str(publication), "inventory":
                          {"kind": "file", "entries": {".": inventory}}}]}}
            self.assertEqual(len(stdout) + len(raw), measure("confluence-bundle", seeds, place, record))
            from floati.helptext import help_for
            help_text = help_for(["confluence", "bundle", "--help"])
            with patch("floati.helptext.help_for", return_value=help_text.replace("--out", "--destination")):
                with self.assertRaisesRegex(ValueError, "--out"):
                    measure("confluence-bundle", seeds, place, record)
            # Even identical bytes in an aliased file are not independent
            # testimony over the census's one declared publication.
            import os
            alias = place / "hardlink.json"
            os.link(archived, alias)
            with self.assertRaisesRegex(ValueError, "aliased"):
                measure("confluence-bundle", seeds, place, record)
            alias.unlink()
            for mutation in ("bytes", "hash", "root", "stdout", "symlink"):
                with self.subTest(mutation=mutation):
                    altered = json.loads(json.dumps(record))
                    if mutation == "bytes": altered["diagnostic"]["auxiliary_post_state"][0]["inventory"]["entries"]["."]["bytes"] += 1
                    if mutation == "hash": altered["diagnostic"]["auxiliary_post_state"][0]["inventory"]["entries"]["."]["sha256"] = "0" * 64
                    if mutation == "root": altered["diagnostic"]["auxiliary_post_state"][0]["root"] = str(place / "foreign.json")
                    if mutation == "stdout": (bank / "stdout.txt").write_bytes(stdout + b" " * 1000)
                    if mutation == "symlink":
                        target = place / "foreign.json"
                        target.write_bytes(raw)
                        archived.unlink()
                        archived.symlink_to(target)
                    with self.assertRaises(ValueError):
                        measure("confluence-bundle", seeds, place, altered)
                    if mutation == "stdout": (bank / "stdout.txt").write_bytes(stdout)

    def test_reads_growing_faster_than_output_still_red_at_exact_gate(self):
        for read, expected in ((1500, "pass"), (1501, "red")):
            with self.subTest(read=read):
                row = self.compare("log-history", self.pairs(read=read))["metrics"]["read_bytes"]
                self.assertEqual(expected, row["classification"])

    def test_ordinary_commands_cannot_claim_output_proportional(self):
        for case in ("send", "ack", "status"):
            with self.subTest(case=case):
                with self.assertRaisesRegex(ValueError, "complete.history"):
                    self.compare(case, self.pairs(), disposition="output_proportional")

    def test_missing_invalid_or_zero_output_cannot_qualify(self):
        for value in (None, 0, -1, True, 1.0, "100"):
            with self.subTest(value=value):
                pairs = self.pairs()
                if value is None:
                    del pairs[0]["10"]["bytes_emitted"]
                else:
                    pairs[0]["10"]["bytes_emitted"] = value
                with self.assertRaises(ValueError):
                    self.compare("log-history", pairs)

    def test_zero_stdout_at_baseline_cannot_claim_output_proportional(self):
        pairs = self.pairs()
        pairs[0]["1"]["bytes_emitted"] = 0
        with self.assertRaisesRegex(ValueError, "bytes_emitted"):
            self.compare("log-history", pairs)

    def test_parser_help_drift_refuses_the_history_claim(self):
        with patch.dict("floati.helptext.HELP", {"log": "Return only the current summary."}):
            with self.assertRaisesRegex(ValueError, "help"):
                self.compare("log-history", self.pairs())


class NativeSendCensusTests(StableSourceFixture, unittest.TestCase):
    def test_send_does_not_scale_with_native_acknowledged_history(self):
        subject = CensusInstrumentTests().subject()
        # Retain raw child results on RED for the architect; the helper owns
        # and removes only its private fixture roots after collecting children.
        bank = Path(tempfile.mkdtemp(prefix="bus-scale-current-result-", dir=REAL_TEMP_ROOT)).resolve()
        try:
            if full_scale_enabled():
                result = subject.first_finding(bank)
            else:
                samples = {}

                def bounded_sample(case, seed, workspace, place, deadline):
                    # Both small native scales still execute in both modes.
                    # Repeats test aggregation with controlled metrics; the
                    # process-scale timing verdict belongs to vector 5.
                    if seed not in samples:
                        _values, samples[seed] = subject._native_sample(
                            case, seed, workspace, place, deadline)
                    return dict(cpu_seconds=1, read_bytes=1, ledger_hold_seconds=1), samples[seed]

                result = subject.first_finding(bank, small_count=1, sample_runner=bounded_sample)
                self.assertFalse(result["native_measurement"])
        finally:
            print("BUS_SCALE_RESULT " + str(bank / "result.json"), flush=True)
        self.assertEqual([], result["findings"], json.dumps(result["findings"], sort_keys=True))
        self.assertTrue(all(row["classification"] == "pass"
                            for row in result["comparison"]["metrics"].values()))
        self.assertEqual(["send"], result["measured"])
        self.assertEqual(set(subject.population(_parser())["cases"]) - {"send"}, set(result["pending"]))
        self.assertEqual(69, len(result["pending"]))
        self.assertTrue(result["source_unchanged"])
        self.assertTrue(result["scratch_removed"])
        self.assertEqual("incomplete", result["status"], "a send-only pass cannot close the census")


class CensusContinuationTests(StableSourceFixture, unittest.TestCase):
    def test_native_case_reports_load_one_at_start_and_end_even_when_setup_fails(self):
        import shutil
        subject = CensusInstrumentTests().subject()
        for runner, result_name in ((subject._remaining_case, "case.json"),
                                    (subject.first_finding, "result.json")):
            with self.subTest(runner=runner.__name__), tempfile.TemporaryDirectory(
                    prefix="bus-scale-load-control-", dir=REAL_TEMP_ROOT) as temporary:
                bank = Path(temporary).resolve()
                with patch("tests.banked_sha_fixture.banked_send_repository",
                           side_effect=RuntimeError("planted setup failure")),\
                        patch.object(subject, "_source_witness", return_value={}),\
                        patch.object(subject.os, "getloadavg", side_effect=[(2.0, 3.0, 4.0), (5.0, 6.0, 7.0)]):
                    with self.assertRaisesRegex(RuntimeError, "planted setup failure"):
                        if runner is subject._remaining_case:
                            runner("ack", bank)
                        else:
                            runner(bank)
                result = json.loads((bank / result_name).read_text())
                if "retained_scratch" in result:
                    shutil.rmtree(result["retained_scratch"])
                self.assertEqual({"start": 2.0, "end": 5.0}, result.get("load1"))

    def test_parent_report_binds_retained_case_bytes_without_embedding_raw_trees(self):
        import hashlib
        subject = CensusInstrumentTests().subject()
        marker = "retained-raw-marker-" + "x" * 20000
        pairs = [{"order": [1, 10] if index % 2 == 0 else [10, 1],
                  "1": {"cpu_seconds": 1.0, "read_bytes": 10, "ledger_hold_seconds": 1.0},
                  "10": {"cpu_seconds": 1.0, "read_bytes": 10, "ledger_hold_seconds": 1.0}}
                 for index in range(5)]
        retained = {}

        def full_case(case, case_bank):
            evidence = {"case": case, "native_complete": True, "scratch_removed": True,
                        "source_unchanged": True, "phase": "complete", "pairs": pairs,
                        "history": {"producer": "unit control"}, "seed_ledgers": {"1": {}, "10": {}},
                        "wall_seconds": 0.1, "load1": {"start": 2.0, "end": 5.0},
                        "raw_archive_sha256": "a" * 64,
                        "source_before": {"commit": "control-source", "files": {"witness": marker}},
                        "source_after": {"commit": "control-source", "files": {"witness": marker}},
                        "raw": [{"payload": marker}], "seeds": {"1": {"payload": marker}}}
            # Deliberately retain bytes that cannot be recreated from the returned dict.
            complete = dict(evidence, retained_only="exact file bytes")
            retained["bytes"] = (json.dumps(complete, sort_keys=True, indent=1) + "\n\n").encode()
            retained["path"] = case_bank / "case.json"
            retained["path"].write_bytes(retained["bytes"])
            return evidence

        with tempfile.TemporaryDirectory(prefix="bus-scale-bounded-report-control-",
                                         dir=REAL_TEMP_ROOT) as temporary:
            bank = Path(temporary).resolve()
            result = subject.remaining_census(bank, cases=["ack"], case_runner=full_case)
            disk = json.loads((bank / "result.json").read_text())
            self.assertEqual(["ack"], result["measured"])
            self.assertEqual(result, disk)
            self.assertEqual(retained["bytes"], retained["path"].read_bytes())
            self.assertFalse(marker in json.dumps(result),
                             "parent report must reference retained case testimony, not embed raw trees")
            summary = result["cases"]["ack"]
            self.assertEqual({"path": str(retained["path"]),
                              "sha256": hashlib.sha256(retained["bytes"]).hexdigest()},
                             summary["case_evidence"])
            self.assertEqual("control-source", summary["source_commit"])
            self.assertFalse(result["native_measurement"])
            self.assertFalse(summary["native_measurement"])
            self.assertEqual(pairs, summary["pairs"])
            self.assertEqual({"start": 2.0, "end": 5.0}, summary.get("load1"))
            self.assertEqual(subject.compare_case_pairs("ack", pairs), summary["comparison"])
            for field in ("raw", "seeds", "source_before", "source_after"):
                self.assertNotIn(field, summary)
            self.assertNotIn(marker, (bank / "result.json").read_text())

    def test_setup_failures_leave_all_remaining_cases_pending_and_do_not_abort(self):
        subject = CensusInstrumentTests().subject()
        remaining = set(subject.population(_parser())["cases"]) - {"send"}
        self.assertEqual(69, len(remaining))
        continuation = getattr(subject, "remaining_census", None)
        self.assertTrue(callable(continuation), "strict remaining-case census is missing")
        visited = []
        with tempfile.TemporaryDirectory(prefix="bus-scale-continuation-control-",
                                         dir=REAL_TEMP_ROOT) as temporary:
            bank = Path(temporary).resolve()

            def failed_setup(case, case_bank):
                self.assertEqual(bank / case, case_bank)
                visited.append(case)
                raise RuntimeError("planted native setup failure")

            result = continuation(bank, case_runner=failed_setup)
            self.assertEqual("incomplete", result["status"])
            self.assertEqual(69, len(visited))
            self.assertEqual(remaining, set(visited))
            self.assertEqual(visited, result["attempted"])
            self.assertEqual([], result["measured"])
            self.assertEqual(remaining, set(result["pending"]))
            self.assertEqual(remaining, set(result["errors"]))
            for case, error in result["errors"].items():
                with self.subTest(case=case):
                    self.assertEqual("RuntimeError", error["type"])
                    self.assertEqual("planted native setup failure", error["detail"])
            self.assertEqual(result, json.loads((bank / "result.json").read_text()))

    def test_history_output_keeps_red_metrics_while_other_setups_remain_pending(self):
        subject = CensusInstrumentTests().subject()
        pairs = [{"order": [1, 10] if index % 2 == 0 else [10, 1],
                  "1": {"cpu_seconds": 1.0, "read_bytes": 10, "ledger_hold_seconds": 1.0},
                  "10": {"cpu_seconds": 3.0, "read_bytes": 30, "ledger_hold_seconds": 3.0}}
                 for index in range(5)]

        def one_history_case(case, _bank):
            if case != "log-history":
                raise RuntimeError("planted unqualified setup")
            for pair in pairs:
                for arm in ("1", "10"):
                    pair[arm]["bytes_emitted"] = 10
            return {"native_complete": True, "scratch_removed": True,
                    "source_unchanged": True, "pairs": pairs}

        with tempfile.TemporaryDirectory(prefix="bus-scale-continuation-control-",
                                         dir=REAL_TEMP_ROOT) as temporary:
            result = subject.remaining_census(Path(temporary).resolve(), case_runner=one_history_case)
        self.assertEqual(["log-history"], result["measured"])
        self.assertEqual(68, len(result["pending"]))
        self.assertEqual(68, len(result["errors"]))
        self.assertEqual({"cpu_seconds", "read_bytes", "ledger_hold_seconds"},
                         {row["metric"] for row in result["findings"]})
        self.assertTrue(all(row["classification"] == "red" for row in result["findings"]))
        self.assertFalse(result["native_measurement"], "a supplied unit runner is not native qualification")
        self.assertEqual("incomplete", result["status"])

    def test_checkpoint_preflight_cannot_change_authoritative_history(self):
        from floati.root import FloatiRoot
        subject = CensusInstrumentTests().subject()

        class MutatingCheckpoint:
            @staticmethod
            def warm(root):
                (root.tenant_home / "events.jsonl").write_bytes(b"changed authoritative history\n")
                return {"status": "ok"}

        with tempfile.TemporaryDirectory(prefix="bus-scale-continuation-control-",
                                         dir=REAL_TEMP_ROOT) as temporary:
            home = Path(temporary).resolve() / "scale-fleet"
            root = FloatiRoot.open_direct_home(home, create=True)
            (root.tenant_home / "events.jsonl").write_bytes(b"retained authoritative history\n")
            with patch.object(subject.importlib, "import_module", return_value=MutatingCheckpoint):
                with self.assertRaisesRegex(RuntimeError, "changed authoritative ledgers"):
                    subject._warm_fixture(home)


class CensusNativePrefaceTests(StableSourceFixture, unittest.TestCase):
    def test_unicode_native_intake_context_preserves_observer_canonical_bytes(self):
        from tests.bus_scale_test_profile import banked_send_repository
        subject = CensusInstrumentTests().subject()
        producer = subject._load(subject.ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                 "unicode_native_preface_producer")
        with tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-unicode-control-",
                                         dir=REAL_TEMP_ROOT) as temporary:
            scratch = Path(temporary).resolve()
            repository, _ = banked_send_repository(scratch)
            workspace = scratch / "workspace"
            repository.rename(workspace)
            workspace.chmod(0o700)
            pair = subject._expanded_native_seeds("intake-preview", scratch, workspace,
                producer.native_entry_module(), time.monotonic() + 30, small_count=1)
            expected = (producer.expanded_adapter().canonical(pair["context"]) + "\n").encode("utf-8")
            self.assertTrue(any(value > 127 for value in expected),
                            "native intake must exercise non-ASCII context")
            self.assertEqual(expected, (pair["prepared_home"].parent / "case-context.json").read_bytes(),
                             "native Unicode context must preserve the inherited canonical bytes")

    def test_wrapped_send_preserves_closed_native_environment_in_both_modes(self):
        import shutil
        from floati.events import EventLog
        from floati.root import FloatiRoot
        from tests.bus_scale_test_profile import banked_send_repository
        subject = CensusInstrumentTests().subject()
        bank = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-env-control-",
                                    dir=REAL_TEMP_ROOT)).resolve()
        print("BUS_SCALE_ENV_CONTROL " + str(bank), flush=True)
        repository, _ = banked_send_repository(bank)
        workspace = bank / "workspace"
        repository.rename(workspace)
        workspace.chmod(0o700)
        deadline = time.monotonic() + 60
        seeds = subject.native_seeds(bank, small_count=1, deadline=deadline)
        producer = subject._load(subject.ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                 "wrapped_send_native_environment")
        owner = subject._ContinuationOwner(deadline)
        effects = []
        for mode in ("cpu", "diagnostic"):
            place = bank / mode
            place.mkdir(mode=0o700)
            fixture = place / "scale-fleet"
            shutil.copytree(seeds["1"], fixture)
            subject._warm_fixture(fixture)
            prior = EventLog(FloatiRoot.open_direct_home(fixture)).event_records()
            output, error, detail = (place / name for name in ("stdout.txt", "stderr.txt", "detail.json"))
            with output.open("xb") as out, error.open("xb") as err:
                child = owner.launch_owned_child(["/usr/bin/python3", "-B", str(subject.ENTRY),
                    mode, "send", str(fixture), str(workspace), str(detail), "owned-census-v1"],
                    cwd=workspace, env=subject._environment(place), stdout=out, stderr=err)
                collected = owner.collect_owned_child(child, deadline_seconds=30, collection_seconds=5)
            self.assertTrue(collected["child_collected"])
            self.assertTrue(collected["home_cleanup"]["home_removed"])
            self.assertEqual(0, collected["exit_code"], error.read_text()[-2000:])
            effects.append(producer.effect_witness("send", fixture, {"event_rows": prior},
                output.read_text(), error.read_text(), json.loads(detail.read_text()), workspace=workspace))
        self.assertEqual(effects[0], effects[1], "wrapping cannot change the exact native send effect")
        self.assertEqual({}, owner.homes)

    def test_native_preface_context_matches_inherited_observer_canonical_contract(self):
        from tests.bus_scale_test_profile import banked_send_repository
        subject = CensusInstrumentTests().subject()
        producer = subject._load(subject.ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                 "native_preface_control_producer")
        entry = producer.native_entry_module()
        observer = producer.expanded_adapter()
        with tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-preface-control-",
                                         dir=REAL_TEMP_ROOT) as temporary:
            scratch = Path(temporary).resolve()
            repository, _ = banked_send_repository(scratch)
            workspace = scratch / "workspace"
            repository.rename(workspace)
            workspace.chmod(0o700)
            pair = subject._expanded_native_seeds("role-list", scratch, workspace, entry,
                                                  time.monotonic() + 30, small_count=1)
            context_path = pair["prepared_home"].parent / "case-context.json"
            self.assertEqual(observer.canonical(pair["context"]) + "\n", context_path.read_text(),
                             "native preface context must satisfy the real inherited observer's byte contract")


class CensusSeedQualificationTests(StableSourceFixture, unittest.TestCase):
    def test_teardown_cut_preflight_preserves_the_restored_bus_root_clock(self):
        import shutil
        from floati.cut_prefix import warm
        from floati.root import FloatiRoot
        from tests.bus_scale_test_profile import banked_send_repository
        from tests.fixtures.bus_scale_census.bus_scale_cut_prefix_effects import verify_warmed_cut_clone
        subject = CensusInstrumentTests().subject()
        producer = subject._load(subject.ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                 "teardown_seed_clock_producer")
        with tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-teardown-seed-", dir=REAL_TEMP_ROOT) as tmp:
            scratch = Path(tmp).resolve()
            repository, _ = banked_send_repository(scratch)
            workspace = scratch / "seat-home/repo"
            workspace.parent.mkdir(mode=0o700)
            repository.rename(workspace)
            workspace.chmod(0o700)
            pair = subject._expanded_native_seeds("seat-teardown", scratch, workspace,
                producer.native_entry_module(), float("inf"), small_count=1)
            for scale in ("1", "10"):
                with self.subTest(scale=scale):
                    clone = scratch / ("restored-" + scale) / pair[scale].name
                    shutil.copytree(pair[scale], clone)
                    # Separate the retained directory clock from the warmer's
                    # filesystem tick without sleeping or altering frozen input.
                    prior = clone.stat()
                    os.utime(clone, ns=(prior.st_atime_ns, prior.st_mtime_ns - 2_000_000_000))
                    restored_clock = clone.stat().st_mtime_ns
                    before = {p.relative_to(clone).as_posix(): p.read_bytes()
                              for p in clone.rglob("*") if p.is_file()}
                    self.assertIsNotNone(warm(FloatiRoot.open_direct_home(clone)))
                    verify_warmed_cut_clone(clone, before)
                    self.assertEqual(restored_clock, clone.stat().st_mtime_ns)

    def test_frozen_native_seed_checkpoints_bind_exact_inodes_and_reachable_bank(self):
        from floati.events import EVENT_KINDS
        from floati.root import FloatiRoot
        from floati.send_prefix import CacheMiss, Prefix, _FIELDS
        from tests.bus_scale_test_profile import banked_send_repository
        from tests.fixtures.bus_scale_census.bus_scale_prefix_effects import _bank_nodes
        subject = CensusInstrumentTests().subject()
        producer = subject._load(subject.ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                 "frozen_seed_control_producer")
        for constructor in ("original", "expanded"):
            with self.subTest(constructor=constructor):
                with tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-seed-control-",
                                                 dir=REAL_TEMP_ROOT) as temporary:
                    scratch = Path(temporary).resolve()
                    if constructor == "original":
                        pair = subject.native_seeds(scratch, small_count=1, deadline=time.monotonic() + 30)
                    else:
                        repository, _ = banked_send_repository(scratch)
                        workspace = scratch / "workspace"
                        repository.rename(workspace)
                        workspace.chmod(0o700)
                        pair = subject._expanded_native_seeds("role-list", scratch, workspace,
                            producer.native_entry_module(), time.monotonic() + 30, small_count=1)
                    for scale in ("1", "10"):
                        home = pair[scale]
                        before = subject._inventory(home)
                        prefix = Prefix(FloatiRoot.open_direct_home(home), EVENT_KINDS)
                        try:
                            prefix.load()
                        except CacheMiss:
                            self.fail("frozen " + constructor + " seed " + scale
                                      + " has a stale checkpoint instead of its exact copied inode binding")
                        identity = [getattr((home / "events.jsonl").stat(), field) for field in _FIELDS]
                        self.assertEqual(7, len(identity))
                        self.assertEqual(identity, prefix.payload["identity"])
                        reachable = set()

                        def visit(digest):
                            if digest is None or digest in reachable:
                                return
                            reachable.add(digest)
                            node = prefix._node(digest)
                            if "children" in node:
                                for child in node["children"].values():
                                    visit(child)
                            elif "prefix" in node and "value" in node:
                                reachable.add(node["value"])
                                prefix._node(node["value"])

                        visit(prefix.payload["trie"])
                        stored = set(_bank_nodes(prefix.bank.read_bytes()))
                        stored.update(path.name for path in prefix.nodes.iterdir())
                        self.assertEqual(reachable, stored,
                                         "frozen cache bank must not copy obsolete unreachable nodes")
                        self.assertEqual(before, subject._inventory(home), "checkpoint qualification must be read-only")

    def test_wait_preface_native_cursor_reads_do_not_create_new_ack_lock_paths(self):
        from floati.cursor import SparseCursor
        from floati.events import EventLog
        from floati.root import FloatiRoot
        from tests.bus_scale_test_profile import banked_send_repository
        subject = CensusInstrumentTests().subject()
        with tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-wait-control-",
                                         dir=REAL_TEMP_ROOT) as temporary:
            scratch = Path(temporary).resolve()
            repository, _ = banked_send_repository(scratch)
            workspace = scratch / "workspace"
            repository.rename(workspace)
            workspace.chmod(0o700)
            seeds = subject.native_seeds(scratch, small_count=1, deadline=time.monotonic() + 30)
            subject._prepare_wait_seeds(seeds, workspace)
            for scale, home in seeds.items():
                with self.subTest(scale=scale):
                    root = FloatiRoot.open_direct_home(home)
                    events = EventLog(root).records()
                    before = subject._inventory(home)
                    SparseCursor(root).state_for("scale-worker", events)
                    self.assertEqual(before, subject._inventory(home),
                                     "native wait preface must materialize cursor read locks before freezing")


class CensusExpandedHistoryBatchTests(StableSourceFixture, unittest.TestCase):
    def test_native_history_batches_keep_every_message_presented_and_acknowledged(self):
        from floati.cursor import SparseCursor
        from floati.events import EventLog
        from floati.jsonl import read_records
        from floati.records import WAKE_HOLD_KINDS
        from floati.root import FloatiRoot
        from tests.bus_scale_test_profile import banked_send_repository
        subject = CensusInstrumentTests().subject()
        producer = subject._load(subject.ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                 "native_history_batch_control_producer")
        entry = producer.native_entry_module()
        bank = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-history-batch-control-",
                                    dir=REAL_TEMP_ROOT)).resolve()
        print("BUS_SCALE_HISTORY_BATCH_CONTROL " + str(bank), flush=True)
        repository, _ = banked_send_repository(bank)
        workspace = bank / "workspace"
        repository.rename(workspace)
        workspace.chmod(0o700)
        observed = {"send": [], "present": [], "ack": []}
        real_send, real_present, real_ack = EventLog.send, EventLog.present, SparseCursor.ack

        def send(log, sender, recipient, *args, **kwargs):
            history = sender == recipient == "scale-history"
            if history and "selected_before" not in observed:
                observed["selected_before"] = {
                    node: log.unacked_ids(node) for node in ("scale-worker", "architect")}
            result = real_send(log, sender, recipient, *args, **kwargs)
            if history:
                observed["send"].append(result["message"]["id"])
            return result

        def present(log, recipient, *args, **kwargs):
            result = real_present(log, recipient, *args, **kwargs)
            if recipient == "scale-history":
                observed["present"].append([row["id"] for row in result[0]])
            return result

        def ack(cursor, recipient, item_ids, *args, **kwargs):
            result = real_ack(cursor, recipient, item_ids, *args, **kwargs)
            if recipient == "scale-history":
                observed["ack"].append(list(item_ids))
            return result

        small = 2 if full_scale_enabled() else 1
        with patch.object(EventLog, "send", new=send), patch.object(EventLog, "present", new=present),\
                patch.object(SparseCursor, "ack", new=ack):
            pair = subject._expanded_native_seeds("sent", bank, workspace, entry,
                                                  time.monotonic() + 60, small_count=small)
        self.assertEqual(10 * small, len(observed["send"]))
        self.assertEqual(10 * small, len(set(observed["send"])))
        frames = {}
        for scale, count in (("1", small), ("10", 10 * small)):
            with self.subTest(scale=scale):
                home = pair[scale]
                frozen = subject._inventory(home)
                root = FloatiRoot.open_direct_home(home)
                log, cursor = EventLog(root), SparseCursor(root)
                events = log.records()
                history = [row for row in events
                           if row["sender"] == row["recipient"] == "scale-history"]
                expected = observed["send"][:count]
                self.assertEqual(expected, [row["id"] for row in history])
                presented, acknowledged = cursor.state_for("scale-history", events)
                self.assertTrue(set(presented) <= set(expected))
                _presented, ackable = cursor.validate_deliveries("scale-history", events)
                self.assertEqual(set(expected), set(ackable))
                self.assertEqual(set(expected), set(acknowledged))
                self.assertEqual([], log.unacked_ids("scale-history"))
                self.assertEqual(observed["selected_before"],
                                 {node: log.unacked_ids(node) for node in observed["selected_before"]})
                deliveries = read_records(root, cursor._delivery_relative_path_for("scale-history"),
                                          allowed_kinds=set(WAKE_HOLD_KINDS))
                acknowledgments = read_records(root, cursor._relative_path_for("scale-history"),
                                               allowed_kinds={"ack_receipt"})
                self.assertEqual(expected, [item for row in deliveries for item in row["item_ids"]])
                self.assertEqual(expected, [item for row in acknowledgments for item in row["item_ids"]])
                frames[scale] = (len(deliveries), len(acknowledgments))
                self.assertEqual(frozen, subject._inventory(home))
        self.assertTrue((pair["10"] / "events.jsonl").read_bytes().startswith(
                        (pair["1"] / "events.jsonl").read_bytes()))
        expected_batches = [observed["send"][:small], observed["send"][small:]]
        self.assertEqual(expected_batches, observed["present"],
                         "expanded history must use real native small/remaining presentation batches")
        self.assertEqual(expected_batches, observed["ack"])
        self.assertEqual({"1": (1, 1), "10": (2, 2)}, frames)
        self.assertEqual({"1": [small], "10": [small, 9 * small]}, pair["history"]["presentation_batches"])
        self.assertEqual({"1": 1, "10": 2}, pair["history"]["receipt_frames"])


class CensusExpandedFamilyTests(StableSourceFixture, unittest.TestCase):
    def counts(self):
        small = 2 if full_scale_enabled() else 1
        return (("1", small), ("10", 10 * small))

    def native_pair(self, case):
        from floati.cursor import SparseCursor
        from floati.events import EventLog
        from floati.jsonl import read_records
        from floati.records import WAKE_HOLD_KINDS
        from floati.registry import Registry
        from floati.root import FloatiRoot
        from tests.bus_scale_test_profile import banked_send_repository
        subject = CensusInstrumentTests().subject()
        producer = subject._load(subject.ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                 "native_family_control_producer")
        entry = producer.native_entry_module()
        routine = entry.routine_module(case)
        real_prepare = routine.prepare
        captured = {}
        bank = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-family-control-",
                                    dir=REAL_TEMP_ROOT)).resolve()
        print("BUS_SCALE_FAMILY_CONTROL " + case + " " + str(bank), flush=True)
        repository, _ = banked_send_repository(bank)
        workspace = bank / "workspace"
        repository.rename(workspace)
        workspace.chmod(0o700)
        captured["workspace"] = workspace

        def prepare(*args, **kwargs):
            context = real_prepare(*args, **kwargs)
            root = FloatiRoot.open_direct_home(args[1])
            log, cursor = EventLog(root), SparseCursor(root)
            events = log.strict_event_records()
            captured["events"] = events
            captured["registry_rows"] = Registry(root).known_records()
            captured["selected"] = {}
            for node in Registry(root).active_node_ids():
                _shown, acknowledged = cursor.state_for(node,
                    [row for row in events if row["kind"] == "message_envelope"])
                captured["selected"][node] = {"registry": Registry(root).require_active(node),
                    "pending": log.unacked_ids(node), "acknowledged": set(acknowledged)}
            if case in {"ack", "consume"}:
                actor = context["actor"]
                self.assertEqual(routine.ACTOR, actor)
                self.assertEqual(actor, context["message"]["recipient"])
                captured["delivery_rows"] = read_records(root, cursor._delivery_relative_path_for(actor),
                                                         allowed_kinds=set(WAKE_HOLD_KINDS))
                captured["ack_rows"] = read_records(root, cursor._relative_path_for(actor),
                                                    allowed_kinds={"ack_receipt"})
            return context

        # Delegate the actual native preface; only observe its unscaled state.
        with patch.object(routine, "prepare", new=prepare),\
                patch.object(entry, "routine_module", return_value=routine):
            pair = subject._expanded_native_seeds(case, bank, workspace, entry,
                                                  time.monotonic() + 90, small_count=self.counts()[0][1])
        return subject, pair, captured

    def check_selected(self, root, captured):
        from floati.cursor import SparseCursor
        from floati.events import EventLog
        from floati.registry import Registry
        log, cursor = EventLog(root), SparseCursor(root)
        events = log.strict_event_records()
        by_id = {row["id"]: row for row in events}
        for row in captured["events"]:
            self.assertEqual(row, by_id[row["id"]])
        preface_ids = {row["id"] for row in captured["events"] if row["kind"] == "message_envelope"}
        for node, before in captured["selected"].items():
            self.assertEqual(before["registry"], Registry(root).require_active(node))
            self.assertEqual(before["pending"], log.unacked_ids(node))
            _shown, acknowledged = cursor.state_for(node,
                [row for row in events if row["kind"] == "message_envelope"])
            self.assertEqual(before["acknowledged"], set(acknowledged) & preface_ids)

    def test_registry_cases_grow_real_primary_frames_and_valid_member_roles(self):
        from floati.admin_registry import RegistryAdminBackend
        from floati.jsonl import read_records
        from floati.registry import REGISTRY_KINDS, Registry
        from floati.root import FloatiRoot
        for case in ("role-list", "node-boot", "node-explain", "node-teardown"):
            with self.subTest(case=case):
                subject, pair, captured = self.native_pair(case)
                needs_roles = case in {"node-boot", "node-explain", "node-teardown"}
                primary = "registry/entries.jsonl"
                raw = {}
                for scale, count in self.counts():
                    root = FloatiRoot.open_direct_home(pair[scale])
                    rows = read_records(root, primary, allowed_kinds=set(REGISTRY_KINDS))
                    members = [row for row in rows if row["kind"] == "registry_entry"
                               and row["node_id"].startswith("scale-history-member-")]
                    self.assertEqual(count, len(members), "declared registry family needs actual native memberships")
                    self.assertEqual(count, len({row["node_id"] for row in members}))
                    preface_count = len(captured["registry_rows"]) + 1 + int(needs_roles)
                    self.assertEqual(preface_count + count * (2 if needs_roles else 1), len(rows))
                    for row in members:
                        self.assertEqual(row, Registry(root).require_active(row["node_id"]))
                        if needs_roles:
                            self.assertEqual("builder", RegistryAdminBackend(root).role_record(row["node_id"])["template_role"])
                    self.check_selected(root, captured)
                    raw[scale] = (pair[scale] / primary).read_bytes()
                self.assertTrue(raw["10"].startswith(raw["1"]))
                self.assertEqual(primary, pair["ledger_relative"])
                proof = subject._qualify_expanded_family(case, pair)
                self.assertEqual("registry", proof["family"])
                self.assertEqual({scale: count * (2 if needs_roles else 1) for scale, count in self.counts()},
                                 {scale: row["history_frames"] for scale, row in proof["scales"].items()})

    def test_chart_grows_retire_register_history_with_identical_live_membership(self):
        from floati.jsonl import read_records
        from floati.registry import REGISTRY_KINDS, Registry
        from floati.root import FloatiRoot
        subject, pair, captured = self.native_pair("chart")
        live = tuple(sorted(set(captured["selected"]) | {"scale-history"}))
        raw = {}
        for scale, count in self.counts():
            root = FloatiRoot.open_direct_home(pair[scale])
            self.assertEqual(live, Registry(root).active_node_ids(),
                "chart must measure ledger length with a fixed live population")
            rows = read_records(root, pair["ledger_relative"], allowed_kinds=set(REGISTRY_KINDS))
            history = rows[pair["history"]["preface_frames"]:]
            self.assertEqual(2 * count, len(history))
            for retired, active in zip(history[::2], history[1::2]):
                self.assertEqual(("registry_entry", "scale-history", "retired"),
                                 (retired["kind"], retired["node_id"], retired["state"]))
                self.assertEqual(("registry_entry", "scale-history", "active"),
                                 (active["kind"], active["node_id"], active["state"]))
            self.check_selected(root, captured)
            raw[scale] = (pair[scale] / pair["ledger_relative"]).read_bytes()
        self.assertTrue(raw["10"].startswith(raw["1"]))
        proof = subject._qualify_expanded_family("chart", pair)
        self.assertEqual({scale: 2 * count for scale, count in self.counts()},
                         {scale: row["history_frames"] for scale, row in proof["scales"].items()})

    def test_chart_child_probe_reads_zero_registry_bytes_after_writer_advanced_transitions(self):
        import os
        from floati import registry_prefix
        from floati.registry import Registry
        from floati.root import FloatiRoot
        subject, pair, captured = self.native_pair("chart")
        root = FloatiRoot.open_direct_home(pair["prepared_home"])
        live = Registry(root).active_node_ids()
        registry_prefix.warm(root)
        before = json.loads((root.tenant_home / ".registry-prefix/checkpoint.json").read_bytes())["payload"]
        appended = 0
        for count in ((2, 20) if full_scale_enabled() else (1, 2)):
            begin_load = os.getloadavg()[0]
            for _ in range(count):
                subject._chart_registry_transition(root)
            appended += 2 * count
            self.assertEqual(live, Registry(root).active_node_ids())
            prefix = registry_prefix.RegistryPrefix(root)
            prefix.load()  # Only observe the writer's bank; never warm here.
            self.assertEqual(before["count"] + appended, prefix.payload["count"])
            self.assertNotEqual(before["head"], prefix.payload["head"])
            place = captured["workspace"].parent / ("writer-growth-probe-" + str(count))
            place.mkdir(mode=0o700)
            output = root.path.parent / ("writer-growth-" + str(count) + ".json")
            collection = subject._collect(["/usr/bin/python3", "-B", str(subject.ENTRY),
                "diagnostic", "chart", str(root.path), str(captured["workspace"]),
                str(output), "owned-census-v1"], place, workspace=captured["workspace"],
                home=place, deadline=time.monotonic() + 70)
            detail = json.loads(output.read_text())
            artifact = json.loads((place / "stdout.txt").read_text())
            self.assertTrue(collection["child_collected"])
            self.assertEqual(0, collection["exit_code"])
            self.assertEqual("ok", artifact["status"])
            self.assertEqual({"bytes": True, "locks": True}, detail["coverage"])
            measured = detail["read_bytes_by_path"].get("registry/entries.jsonl", 0)
            self.assertEqual(0, measured, "native child replayed history after writer certification")
            print("BUS_SCALE_CHART_WRITER_GROWTH " + json.dumps({
                "transitions": count, "registry_bytes_read": measured,
                "load1": {"start": begin_load, "end": os.getloadavg()[0]}}, sort_keys=True), flush=True)

    def test_ack_and_consume_grow_actor_delivery_and_ack_frames_without_acknowledging_selected_work(self):
        from floati.cursor import SparseCursor
        from floati.events import EventLog
        from floati.jsonl import read_records
        from floati.records import WAKE_HOLD_KINDS
        from floati.root import FloatiRoot
        for case in ("ack", "consume"):
            with self.subTest(case=case):
                subject, pair, captured = self.native_pair(case)
                actor = pair["context"]["actor"]
                raw = {}
                for scale, count in self.counts():
                    root = FloatiRoot.open_direct_home(pair[scale])
                    log, cursor = EventLog(root), SparseCursor(root)
                    primary = cursor._delivery_relative_path_for(actor).as_posix()
                    deliveries = read_records(root, primary, allowed_kinds=set(WAKE_HOLD_KINDS))
                    acknowledgments = read_records(root, cursor._relative_path_for(actor), allowed_kinds={"ack_receipt"})
                    self.assertEqual(count, len(deliveries) - len(captured["delivery_rows"]),
                                     "declared delivery family needs actual selected-actor receipt growth")
                    self.assertEqual(count, len(acknowledgments) - len(captured["ack_rows"]))
                    messages = [row for row in log.records()
                                if row["sender"] == "scale-history" and row["recipient"] == actor]
                    self.assertEqual(count, len(messages))
                    history_ids = {row["id"] for row in messages}
                    _presented, ackable = cursor.validate_deliveries(actor, log.records())
                    _presented, acknowledged = cursor.state_for(actor, log.records())
                    self.assertTrue(history_ids <= set(ackable))
                    self.assertTrue(history_ids <= set(acknowledged))
                    for row in acknowledgments[len(captured["ack_rows"]):]:
                        self.assertEqual(1, len(row["item_ids"]))
                        self.assertTrue(set(row["item_ids"]) <= history_ids)
                    self.check_selected(root, captured)
                    raw[scale] = (pair[scale] / primary).read_bytes()
                    self.assertEqual(primary, pair["ledger_relative"])
                self.assertTrue(raw["10"].startswith(raw["1"]))
                self.assertEqual("deliveries", subject._qualify_expanded_family(case, pair)["family"])

    def test_confluence_bundle_uses_a_genuinely_private_native_publication_parent(self):
        import os
        prior_umask = os.umask(0o022)
        try:
            subject, pair, captured = self.native_pair("confluence-bundle")
        finally:
            os.umask(prior_umask)
        producer = subject._load(subject.ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                 "native_family_bundle_parent_control_producer")
        routine = producer.native_entry_module().routine_module("confluence-bundle")
        argv = routine.arguments("confluence-bundle", pair["prepared_home"],
                                 captured["workspace"], pair["context"])
        publication = Path(argv[argv.index("--out") + 1])
        self.assertEqual(pair["prepared_home"].parent, publication.parent)
        self.assertEqual(0o700, publication.parent.stat().st_mode & 0o777)
        self.assertEqual(publication.parent.resolve(strict=True), publication.parent)
        self.assertFalse(publication.exists())

    def test_event_family_has_native_pair_frames_and_rejects_primary_family_drift(self):
        import copy
        from floati.events import EventLog
        from floati.root import FloatiRoot
        subject, pair, captured = self.native_pair("sent")
        for scale, count in self.counts():
            root = FloatiRoot.open_direct_home(pair[scale])
            rows = EventLog(root).strict_event_records()
            self.assertEqual(2 * count, len(rows) - len(captured["events"]))
            history_ids = {row["id"] for row in rows if row["kind"] == "message_envelope"
                           and row["sender"] == row["recipient"] == "scale-history"}
            observations = [row for row in rows if row["kind"] == "send_readiness_observation"
                            and row["envelope_id"] in history_ids]
            self.assertEqual(count, len(history_ids))
            self.assertEqual(count, len(observations))
            self.check_selected(root, captured)
        self.assertTrue((pair["10"] / "events.jsonl").read_bytes().startswith(
                        (pair["1"] / "events.jsonl").read_bytes()))
        qualifier = getattr(subject, "_qualify_expanded_family", None)
        self.assertTrue(callable(qualifier), "native EOF family guard is missing")
        proof = qualifier("sent", pair)
        self.assertEqual("events", proof["family"])
        self.assertEqual("events.jsonl", proof["primary"])
        bad = dict(pair, ledger_relative="registry/entries.jsonl")
        with self.assertRaisesRegex(RuntimeError, "primary family"):
            qualifier("sent", bad)
        drifted = copy.deepcopy(subject.population(_parser()))
        drifted["cases"]["sent"]["family"] = "registry"
        with patch.object(subject, "population", return_value=drifted):
            with self.assertRaisesRegex(RuntimeError, "primary family"):
                qualifier("sent", pair)


class CensusSelectionTests(StableSourceFixture, unittest.TestCase):
    def test_explicit_l7_and_all_case_plans_print_their_judged_population(self):
        import contextlib
        import io
        subject = CensusInstrumentTests().subject()
        command = subject._load(subject.ROOT / "scripts/bus-scale-census.py",
                                "bus_scale_verdict_plan")
        cases = set(subject.population(_parser())["cases"]) - {"send"}
        for mode in ("l7", "all-cases"):
            with self.subTest(mode=mode):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = command.main(["--dry-run", "--verdict", mode])
                self.assertEqual(0, code)
                plan = json.loads(output.getvalue())
                verdict = plan["verdict"]
                deferred = {row["case"] for row in verdict["deferred"]}
                self.assertEqual(3 if mode == "l7" else 0, len(deferred))
                self.assertEqual(cases - deferred, set(verdict["judged"]["cases"]))
                self.assertEqual(len(cases - deferred), verdict["judged"]["count"])
                self.assertEqual(cases - deferred, set(plan["pending"]))
                self.assertEqual(mode, verdict["mode"])
        self._explicit_command_controls(verdict_mode="l7")

    def test_l7_deferrals_are_typed_and_all_cases_still_judges_them(self):
        subject = CensusInstrumentTests().subject()
        expected = {
            "seat-teardown": "SEAT-TEARDOWN-PREFIX-1",
            "wake-daemon-serve": "WAKE-DAEMON-EPOCH-TOKEN-1",
            "wake-wait": "WAITER-COLD-SOURCE-1",
        }
        for mode in ("l7", "all-cases"):
            with self.subTest(mode=mode):
                result, visited = self._deferral_control(subject, mode)
                deferred = result["verdict"]["deferred"]
                self.assertEqual(expected if mode == "l7" else {},
                                 {row["case"]: row["row"] for row in deferred})
                for row in deferred:
                    self.assertEqual("deferred", row["status"])
                    self.assertEqual("l7_owner_deferral", row["code"])
                    self.assertEqual("2026-10-29", row["owner_ruling_date"])
                judged = result["verdict"]["judged"]
                self.assertEqual(sorted(visited), judged["cases"])
                self.assertEqual(len(visited), judged["count"])
                self.assertEqual(set(expected) if mode == "all-cases" else set(),
                                 {row["command"] for row in result["findings"]})
                self.assertEqual([], result["pending"])
                self.assertEqual(set(visited), set(result["measured"]))

    def test_l7_deleted_deferral_and_new_case_are_judged_at_existing_ratio(self):
        subject = CensusInstrumentTests().subject()
        rows = json.loads(subject.L7_DEFERRALS.read_text())
        for case in ("seat-teardown", "new-routine-case"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                    prefix="bus-scale-deferral-policy-", dir=REAL_TEMP_ROOT) as temporary:
                policy = Path(temporary) / "deferrals.json"
                policy.write_text(json.dumps([row for row in rows if row["case"] != case]))
                with patch.object(subject, "L7_DEFERRALS", policy):
                    result, visited = self._deferral_control(subject, "l7", extra_case=case)
                self.assertIn(case, visited)
                self.assertNotIn(case, [row["case"] for row in result["verdict"]["deferred"]])
                findings = [row for row in result["findings"] if row["command"] == case]
                self.assertEqual(set(subject.METRICS), {row["metric"] for row in findings})
                self.assertTrue(all(row["ratio"] == 2.0 for row in findings))

    def _deferral_control(self, subject, mode, *, extra_case=None):
        cases = {case: {} for case in ("status", "doctor", "seat-teardown",
                                      "wake-daemon-serve", "wake-wait")}
        if extra_case:
            cases[extra_case] = {}
        visited = []

        def admitted(case, _bank):
            visited.append(case)
            ratio = 1 if case in {"status", "doctor"} else 2
            pairs = [{"order": [1, 10] if index % 2 == 0 else [10, 1],
                      "1": {metric: 1 for metric in subject.METRICS},
                      "10": {metric: ratio for metric in subject.METRICS}}
                     for index in range(5)]
            return {"native_complete": True, "scratch_removed": True,
                    "source_unchanged": True, "pairs": pairs}

        with tempfile.TemporaryDirectory(prefix="bus-scale-deferral-control-",
                dir=REAL_TEMP_ROOT) as temporary, patch.object(subject, "population",
                return_value={"cases": cases}):
            result = subject.remaining_census(Path(temporary).resolve(), case_runner=admitted,
                                              verdict_mode=mode)
        return result, visited

    def test_explicit_plan_retains_send_calibration_and_full_scale_controls(self):
        import contextlib
        import io
        subject = CensusInstrumentTests().subject()
        command = subject._load(subject.ROOT / "scripts/bus-scale-census.py",
                                "bus_scale_paydown_plan")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = command.main(["--dry-run"])
        self.assertEqual(0, code)
        plan = json.loads(output.getvalue())
        self.assertEqual(["send"], plan["additional_cases"])
        self.assertEqual(["linear", "constant"], plan["calibration_controls"])
        self.assertTrue(plan["full_scale_controls"])
        self.assertFalse(any("_FailedTest" in name for name in plan["full_scale_controls"]))
        self.assertFalse(plan["native_measurement"])

    def _explicit_command_controls(self, *, verdict_mode="all-cases"):
        import contextlib
        import io
        subject = CensusInstrumentTests().subject()
        command = subject._load(subject.ROOT / "scripts/bus-scale-census.py",
                                "bus_scale_explicit_command_control")
        self.assertIn("scripts/bus-scale-census.py", subject._source_witness()["files"])
        verdict = subject.verdict_population(set(subject.population(_parser())["cases"]) - {"send"},
                                             verdict_mode)
        cases = verdict["judged"]["cases"]

        def invoke(arguments, *, controls_successful=True):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                with patch.object(command, "run_full_scale_controls",
                        return_value={"successful": controls_successful}) as full:
                    code = command.main([*arguments, "--verdict", verdict_mode])
            if arguments and arguments[0] == "--run" and code == 0:
                full.assert_called_once()
            return code, json.loads(output.getvalue())

        with patch.object(command.instrument, "remaining_census") as native:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as help_exit:
                command.main(["--help"])
            self.assertEqual(0, help_exit.exception.code)
            code, plan = invoke(["--dry-run"])
            self.assertEqual(0, code)
            self.assertEqual("BUS-SCALE-CENSUS-1", plan["row_id"])
            self.assertEqual(cases, sorted(plan["pending"]))
            self.assertEqual(verdict, plan["verdict"])
            for arguments in ([], ["--run"], ["--run", "--bank", "relative-bank"],
                              ["--measure", "no-such-case"], ["--measure", cases[0], cases[0]]):
                with self.subTest(arguments=arguments):
                    code, refusal = invoke(arguments)
                    self.assertEqual(20, code)
                    self.assertEqual("refused", refusal["status"])
                    self.assertTrue(refusal["evidence"]["code"])
            native.assert_not_called()

        with tempfile.TemporaryDirectory(prefix="bus-scale-command-control-", dir=REAL_TEMP_ROOT) as directory:
            bank = Path(directory).resolve()
            loop = bank / "loop"
            loop.symlink_to(loop.name)
            with patch.object(command.instrument, "remaining_census") as native:
                code, refusal = invoke(["--run", "--bank", str(loop)])
                self.assertEqual(20, code)
                self.assertEqual("census_bank_refused", refusal["evidence"]["code"])
                native.assert_not_called()
            loop.unlink()
            selected = [cases[0]]
            sample = {"selected": selected, "measured": selected, "errors": {}, "findings": [],
                      "cases": {}, "source_before": {"commit": "a" * 40}, "source_unchanged": True}
            for complete in (False, True):
                with self.subTest(selection_complete=complete), patch.object(command.instrument,
                        "remaining_census", return_value=dict(sample, selection_complete=complete)):
                    code, receipt = invoke(["--measure", *selected, "--bank", str(bank)])
                    self.assertEqual(0 if complete else 1, code)
                    self.assertEqual(selected, receipt["selected"])
                    self.assertEqual(complete, receipt["selection_complete"])
            green = {"status": "green", "pending": [], "measured": cases,
                     "findings": [], "errors": {}, "source_unchanged": True,
                     "native_measurement": True, "verdict": verdict}
            with patch.object(command.instrument, "remaining_census", return_value=green) as native:
                code, receipt = invoke(["--run", "--bank", str(bank)])
                native.assert_called_once_with(bank, verdict_mode=verdict_mode)
            self.assertEqual(0, code)
            self.assertEqual("BUS-SCALE-CENSUS-1", receipt["row_id"])
            self.assertEqual(green, {key: receipt[key] for key in green})
            self.assertEqual(receipt, json.loads((bank / "BUS-SCALE-CENSUS-1.json").read_text()))
            # A rerun may not overwrite the first receipt or launch a native child.
            with patch.object(command.instrument, "remaining_census") as native:
                code, refusal = invoke(["--run", "--bank", str(bank)])
                self.assertEqual(20, code)
                native.assert_not_called()
            (bank / "BUS-SCALE-CENSUS-1.json").unlink()
            with patch.object(command.instrument, "remaining_census", return_value=green):
                code, receipt = invoke(["--run", "--bank", str(bank)], controls_successful=False)
            self.assertEqual(1, code)
            self.assertEqual("incomplete", receipt["status"])
            self.assertFalse(receipt["full_scale_controls"]["successful"])
            (bank / "BUS-SCALE-CENSUS-1.json").unlink()
            for change in ({"status": "red", "findings": [{"command": "ack"}]},
                           {"pending": ["ack"]}, {"source_unchanged": False},
                           {"measured": cases[:-1]}, {"native_measurement": False},
                           {"verdict": {}}, {"errors": {"doctor": {"type": "RuntimeError"}}}):
                with self.subTest(change=change):
                    with patch.object(command.instrument, "remaining_census", return_value=dict(green, **change)):
                        code, receipt = invoke(["--run", "--bank", str(bank)])
                    self.assertEqual(1, code)
                    self.assertNotEqual("green", receipt["status"])
                    (bank / "BUS-SCALE-CENSUS-1.json").unlink()

    def test_explicit_subset_is_parser_bound_retains_unselected_and_cannot_close_population(self):
        self._explicit_command_controls()
        # Use the runner's actual retained discovery objects, including load_tests
        # adapters, rather than a list of the two native wrappers seen so far.
        import ast
        import inspect
        import textwrap
        from scripts.parallel_unittest import discover_suite, _leaves

        suite, census = discover_suite(Path(__file__).resolve().parents[1])
        native_ids = []
        for test in _leaves(suite):
            method = getattr(test, test._testMethodName)
            if "remaining_census" not in getattr(getattr(method, "__code__", None), "co_names", ()):
                continue
            tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, (ast.Name, ast.Attribute))
                        and getattr(node.func, "attr", getattr(node.func, "id", None)) == "remaining_census"
                        and not any(keyword.arg == "case_runner"
                                    and not (isinstance(keyword.value, ast.Constant)
                                             and keyword.value.value is None)
                                    for keyword in node.keywords)):
                    native_ids.append(test.id())
        self.assertEqual([], native_ids,
                         "default discovery performs the full native census: " + repr(native_ids))
        self.assertEqual(len(census["ids"]), suite.countTestCases())
        subject = CensusInstrumentTests().subject()
        remaining = set(subject.population(_parser())["cases"]) - {"send"}
        visited = []
        pairs = [{"order": [1, 10] if index % 2 == 0 else [10, 1],
                  "1": {"cpu_seconds": 1.0, "read_bytes": 10, "ledger_hold_seconds": 1.0},
                  "10": {"cpu_seconds": 1.0, "read_bytes": 10, "ledger_hold_seconds": 1.0}}
                 for index in range(5)]

        def admitted(case, _bank):
            visited.append(case)
            return {"native_complete": True, "scratch_removed": True,
                    "source_unchanged": True, "pairs": pairs}

        with tempfile.TemporaryDirectory(prefix="bus-scale-selection-control-", dir=REAL_TEMP_ROOT) as temporary:
            try:
                result = subject.remaining_census(Path(temporary).resolve(), cases=["ack"], case_runner=admitted)
            except TypeError as error:
                self.fail("strict continuation lacks explicit parser-bound cases selection: " + str(error))
        self.assertEqual(["ack"], visited)
        self.assertEqual(["ack"], result["selected"])
        self.assertEqual(["ack"], result["attempted"])
        self.assertEqual(["ack"], result["measured"])
        self.assertEqual(remaining - {"ack"}, set(result["not_attempted"]))
        self.assertEqual(remaining - {"ack"}, set(result["pending"]))
        self.assertEqual("incomplete", result["status"])
        for invalid in (["send"], ["unregistered-case"], ["ack", "ack"], []):
            with self.subTest(invalid=invalid):
                with tempfile.TemporaryDirectory(prefix="bus-scale-selection-control-",
                                                 dir=REAL_TEMP_ROOT) as temporary:
                    with self.assertRaises(ValueError):
                        subject.remaining_census(Path(temporary).resolve(), cases=invalid, case_runner=admitted)


class CensusNativeWaitTests(StableSourceFixture, unittest.TestCase):
    _bounded_wait_results = {}

    def test_fresh_waiter_uses_certified_mail_without_replaying_unrelated_history(self):
        from floati.codex_wait import run_stop_waiter
        from floati.codex_wait_contract import (CodexWaitConsentLedger,
            CodexWaitSessionLedger, resolve_participant, write_workspace_map)
        from floati.cursor import SparseCursor
        from floati.events import EventLog
        from floati.registry import Registry
        from floati.root import FloatiRoot
        from tests.bus_scale_io import IOProbe
        from tests.waiter_timing import scripted_readiness
        import io

        with tempfile.TemporaryDirectory(prefix="bus-scale-wait-prefix-", dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary).resolve()
            home, workspace = base / "fleet", base / "workspace"
            workspace.mkdir(mode=0o700)
            root = FloatiRoot.open_direct_home(home, create=True)
            for node in ("scale-worker", "architect"):
                Registry(root).register(node, "zcode")
            log = EventLog(root)
            for index in range(8):
                log.send("architect", "architect", "floati", "a" * 40, "note.md",
                    "unrelated acknowledged history", idempotency_key="wait-prefix-" + str(index))
            messages, _ = log.present("architect")
            SparseCursor(root).ack("architect", [row["id"] for row in messages],
                acting_session_id="history")
            log.present("scale-worker")
            write_workspace_map(home, workspace, "scale-worker")
            binding = resolve_participant(home, workspace, _strict=True).binding
            consent = CodexWaitConsentLedger(root).arm(binding, hook_timeout_seconds=10,
                wait_deadline_seconds=8, idempotency_key="wait-prefix-consent")
            CodexWaitSessionLedger(root).arm(binding, consent, "wait-prefix-session",
                idempotency_key="wait-prefix-session")
            with log.path.open("rb") as stream:
                age_anchor = stream.readline()
            clock = [0.0]
            def advance(seconds):
                clock[0] += seconds
            output, error = io.StringIO(), io.StringIO()
            with IOProbe(home) as probe:
                code = run_stop_waiter(bus_home=home,
                    hook_payload={"cwd": str(workspace), "session_id": "wait-prefix-session"},
                    stdout=output, stderr=error, monotonic=lambda: clock[0], sleep=advance,
                    wait_readable=scripted_readiness(advance),
                    wall_time=lambda: 1_790_000_000 + clock[0])
            probe.assert_complete()
            self.assertEqual(0, code, error.getvalue())
            self.assertIn("deadline exhausted", json.loads(output.getvalue())["reason"])
            self.assertLessEqual(probe.read_bytes_by_path.get("events.jsonl", 0), len(age_anchor),
                "a fresh empty-mail waiter replayed certified unrelated event history")

    def _cold_waiter_fixture(self):
        from floati.codex_wait_contract import (CodexWaitConsentLedger,
            CodexWaitSessionLedger, resolve_participant, write_workspace_map)
        from floati.events import EventLog
        from floati.registry import Registry
        from floati.root import FloatiRoot

        temporary = tempfile.TemporaryDirectory(prefix="bus-scale-wait-admission-", dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        home, workspace = base / "fleet", base / "workspace"
        workspace.mkdir(mode=0o700)
        root = FloatiRoot.open_direct_home(home, create=True)
        for node in ("scale-worker", "architect"):
            Registry(root).register(node, "zcode")
        log = EventLog(root)
        for index in range(2):
            log.send("architect", "architect", "floati", "a" * 40, "note.md",
                "unrelated retained history", idempotency_key="admission-history-" + str(index))
        log.present("scale-worker")
        write_workspace_map(home, workspace, "scale-worker")
        binding = resolve_participant(home, workspace, _strict=True).binding
        consent = CodexWaitConsentLedger(root).arm(binding, hook_timeout_seconds=10,
            wait_deadline_seconds=8, idempotency_key="admission-consent")
        CodexWaitSessionLedger(root).arm(binding, consent, "admission-session",
            idempotency_key="admission-session")
        return root, log, workspace

    def _run_cold_waiter(self, root, workspace):
        import io
        from floati.codex_wait import run_stop_waiter
        from tests.bus_scale_io import IOProbe
        from tests.waiter_timing import scripted_readiness

        clock, result = [0.0], {}
        def advance(seconds):
            clock[0] += seconds
        output, error = io.StringIO(), io.StringIO()
        with IOProbe(root.tenant_home) as probe:
            code = run_stop_waiter(bus_home=root.tenant_home,
                hook_payload={"cwd": str(workspace), "session_id": "admission-session"},
                stdout=output, stderr=error, monotonic=lambda: clock[0], sleep=advance,
                wait_readable=scripted_readiness(advance),
                wall_time=lambda: 1_790_000_000 + clock[0], _waiter_result=result)
        probe.assert_complete()
        self.assertEqual(0, code, error.getvalue())
        return result, json.loads(output.getvalue()), probe.read_bytes_by_path.get("events.jsonl", 0)

    def test_cold_waiter_uncertain_certificate_keeps_native_source_fallback(self):
        for uncertainty in ("missing", "stale", "corrupt"):
            with self.subTest(uncertainty=uncertainty):
                root, log, workspace = self._cold_waiter_fixture()
                checkpoint = root.tenant_home / ".inbox-prefix" / "checkpoint.json"
                if uncertainty == "missing":
                    checkpoint.unlink()
                elif uncertainty == "stale":
                    log.path.write_bytes(log.path.read_bytes())
                else:
                    checkpoint.write_bytes(b"{broken-certificate\n")
                with log.path.open("rb") as stream:
                    first_frame_bytes = len(stream.readline())
                result, output, event_bytes = self._run_cold_waiter(root, workspace)
                self.assertEqual("exhausted", result["kind"], result)
                self.assertIn("deadline exhausted", output["reason"])
                self.assertGreater(event_bytes, first_frame_bytes,
                    "an uncertain certificate was accepted as mailbox absence")

    def test_cold_waiter_nonempty_own_receipt_plane_keeps_native_refusal(self):
        from floati.cursor import SparseCursor

        for family in ("deliveries", "acks"):
            with self.subTest(family=family):
                root, _log, workspace = self._cold_waiter_fixture()
                cursor = SparseCursor(root)
                relative = (cursor._delivery_relative_path_for("scale-worker") if family == "deliveries"
                    else cursor._relative_path_for("scale-worker"))
                path = root.resolve_relative(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"{malformed-own-receipt\n")
                result, output, _event_bytes = self._run_cold_waiter(root, workspace)
                self.assertEqual("integrity_failure", result["kind"], result)
                self.assertEqual("consumption_state_unavailable", result["code"])
                self.assertIn("consumption_state_unavailable", output["reason"])

    def test_cold_waiter_keeps_ordinary_and_worker_mail_in_their_native_planes(self):
        for worker in (None, "worker-attempt"):
            with self.subTest(worker=worker):
                root, log, workspace = self._cold_waiter_fixture()
                message = log.send("architect", "scale-worker", "floati", "a" * 40,
                    "note.md", "selected mail", idempotency_key="selected-mail",
                    worker_session_id=worker)["message"]
                # Certify the new complete event source without presenting
                # this recipient's selected ordinary or worker mail.
                from floati.inbox_prefix import warm
                warm(root)
                with log.path.open("rb") as stream:
                    first_frame_bytes = len(stream.readline())
                result, output, event_bytes = self._run_cold_waiter(root, workspace)
                self.assertGreater(event_bytes, first_frame_bytes)
                if worker is None:
                    self.assertEqual("fresh_work", result["kind"], result)
                    self.assertEqual([message["id"]], result["item_ids"])
                    self.assertIn(message["id"], output["reason"])
                else:
                    self.assertEqual("exhausted", result["kind"], result)
                    messages, _receipt = log.present("scale-worker", worker_session_id=worker)
                    self.assertEqual([message["id"]], [row["id"] for row in messages])

    def test_cold_waiter_roll_head_retains_native_epoch_read(self):
        from datetime import datetime, timezone
        from floati import bus_epoch
        from floati.inbox_prefix import warm
        from floati.planes import AuthorityGrantStore

        root, _log, workspace = self._cold_waiter_fixture()
        AuthorityGrantStore(root).grant_exact("bus-epoch-roll", "architect", 1,
            datetime.now(timezone.utc))
        bus_epoch.roll_bus_epoch(root, actor="architect", idempotency_key="admission-roll")
        warm(root)
        result, output, event_bytes = self._run_cold_waiter(root, workspace)
        self.assertEqual("exhausted", result["kind"], result)
        self.assertIn("deadline exhausted", output["reason"])
        self.assertGreater(event_bytes, 0, "a roll head was admitted as an ordinary empty source")

    def bounded_wait(self, property_name):
        """Assert each property against one genuine completed waiter record."""
        import copy
        import hashlib
        from floati import codex_wait, codex_wait_contract, send_prefix
        from tests.waiter_timing import scripted_readiness
        subject = CensusInstrumentTests().subject()
        entry = subject._load(subject.ENTRY, "bounded_wait_phase_entry")
        paths=(Path(__file__).resolve(),Path(codex_wait.__file__).resolve(),
               Path(codex_wait_contract.__file__).resolve(),
               Path(scripted_readiness.__code__.co_filename).resolve(),Path(subject.ENTRY))
        key=(send_prefix._revision(),
             tuple((str(path),hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths),
             codex_wait.run_stop_waiter,codex_wait._run_stop_waiter,
             codex_wait._WaiterInputsWatch.changed,entry.observe_idle.__code__,
             codex_wait_contract.CodexWaitConsentLedger.arm,
             codex_wait_contract.CodexWaitSessionLedger.arm,scripted_readiness)
        cache=type(self)._bounded_wait_results
        record=None if full_scale_enabled() else cache.get(key)
        if record is None:
            record=self._run_bounded_wait(entry)
            self.assertEqual(0,record["code"],record["stderr"])
            self.assertIn("deadline exhausted",record["artifact"]["reason"])
            if not full_scale_enabled():
                cache[key]=copy.deepcopy(record)
        record=copy.deepcopy(record)
        self.assertEqual(0,record["code"],record["stderr"])
        self.assertIn("deadline exhausted",record["artifact"]["reason"])
        consent,clock,idle,startup=(record[field] for field in ("consent","clock","idle","startup"))
        if property_name == "consent":
            self.assertEqual(8, consent["wait_deadline_seconds"])
            self.assertGreaterEqual(clock, 8)
        elif property_name == "idle":
            self.assertGreater(idle["end_ns"], idle["begin_ns"])
            self.assertEqual(1, idle["native_sleep_seconds"])
        else:
            self.assertTrue(startup["authorized_reconciliation"])
            self.assertEqual(startup["endpoint_ns"], idle["begin_ns"])

    def _run_bounded_wait(self, entry):
        """Real waiter and passive phase observer, with scripted readiness.

        Virtual time proves phase ordering and expiry without claiming native
        duration/CPU measurements. Vector 5 retains the process measurements.
        """
        import io
        import sys
        from types import SimpleNamespace
        from floati import codex_wait
        from floati.codex_wait_contract import (CodexWaitConsentLedger,
            CodexWaitSessionLedger, resolve_participant, write_workspace_map)
        from floati.jsonl import _append_frame
        from floati.registry import Registry
        from floati.root import FloatiRoot
        from tests.waiter_timing import scripted_readiness
        with tempfile.TemporaryDirectory(prefix="bus-scale-bounded-wait-", dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary).resolve()
            home, workspace = base / "scale-fleet", base / "workspace"
            home.mkdir(mode=0o700)
            workspace.mkdir(mode=0o700)
            root = FloatiRoot.open_direct_home(home)
            _append_frame(root.resolve_relative("events.jsonl"), b"")
            Registry(root).register("scale-worker", "zcode")
            write_workspace_map(home, workspace, "scale-worker")
            binding = resolve_participant(home, workspace, _strict=True).binding
            consent = CodexWaitConsentLedger(root).arm(binding,
                hook_timeout_seconds=10, wait_deadline_seconds=8,
                idempotency_key="bounded-native-wait-consent")
            CodexWaitSessionLedger(root).arm(binding, consent, entry.SESSION,
                idempotency_key="bounded-native-wait-session")
            clock, changes, idle, startup = [0.0], [], {}, {}
            native_changed = codex_wait._WaiterInputsWatch.changed

            def advance(seconds):
                clock[0] += seconds

            def observe(watcher):
                frame = sys._getframe(1)
                self.assertIs(frame.f_code, codex_wait._run_stop_waiter.__code__)
                state = frame.f_locals
                return entry.observe_idle(native_changed, watcher, changes, idle, None,
                    reconciliation=state.get("idle_artifact"),
                    authorized=getattr(state.get("classification"), "state", None) == "authorized",
                    deadline_ns=int(state["deadline"] * 1e9),
                    poll_interval_seconds=state["poll_interval_seconds"],
                    previous_current_time_ns=int(state["current_time"] * 1e9) if "current_time" in state else None,
                    startup=startup)

            entry.time = SimpleNamespace(monotonic_ns=lambda: int(clock[0] * 1e9),
                                         process_time_ns=time.process_time_ns)
            output, error = io.StringIO(), io.StringIO()
            with patch.object(codex_wait._WaiterInputsWatch, "changed", observe):
                code = codex_wait.run_stop_waiter(bus_home=home,
                    hook_payload={"cwd": str(workspace), "session_id": entry.SESSION},
                    stdout=output, stderr=error, monotonic=lambda: clock[0],
                    sleep=advance, wait_readable=scripted_readiness(advance),
                    wall_time=lambda: 1_790_000_000 + clock[0])
            record=dict(code=code,stderr=error.getvalue(),artifact=json.loads(output.getvalue()),
                        consent=consent,clock=clock[0],idle=idle,startup=startup)
        self.assertFalse(Path(temporary).exists(),"completed bounded waiter left its owned fixture behind")
        return record

    def test_native_consent_retains_eight_second_window_and_real_idle_phase(self):
        if not full_scale_enabled():
            return self.bounded_wait("consent")
        from tests.bus_scale_test_profile import banked_send_repository
        from floati.jsonl import read_records
        from floati.root import FloatiRoot
        subject = CensusInstrumentTests().subject()
        bank = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-wait-window-control-",
                                    dir=REAL_TEMP_ROOT)).resolve()
        print("BUS_SCALE_WAIT_WINDOW_CONTROL " + str(bank), flush=True)
        repository, _ = banked_send_repository(bank)
        workspace = bank / "workspace"
        repository.rename(workspace)
        workspace.chmod(0o700)
        deadline = time.monotonic() + 65
        seeds = subject.native_seeds(bank, small_count=1, deadline=deadline)
        subject._prepare_wait_seeds(seeds, workspace)
        consent_windows = {}
        for scale in ("1", "10"):
            root = FloatiRoot.open_direct_home(seeds[scale])
            rows = read_records(root, Path("receipts/codex-wait-consent/scale-worker.jsonl"),
                                allowed_kinds={"codex_wait_consent_receipt"})
            self.assertEqual(1, len(rows))
            self.assertEqual(10, rows[0]["hook_timeout_seconds"])
            consent_windows[scale] = rows[0]["wait_deadline_seconds"]
        producer = subject._load(subject.ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                 "native_wait_window_control_producer")
        frozen = subject._inventory(seeds["10"])
        owner = subject._ContinuationOwner(deadline)
        values, raw = subject._original_observation("wait-idle", seeds["10"], workspace,
                                                    bank / "observation", owner, producer)
        self.assertEqual(frozen, subject._inventory(seeds["10"]))
        self.assertGreater(values["cpu_seconds"], 0)
        for mode in ("cpu", "diagnostic"):
            with self.subTest(mode=mode):
                self.assertEqual("unchanged_idle_tick", raw[mode]["effect"]["effect"]["kind"])
                detail = raw[mode]["detail"]
                self.assertTrue(detail["startup"]["authorized_reconciliation"])
                self.assertEqual(detail["startup"]["endpoint_ns"], detail["idle"]["begin_ns"])
                self.assertGreater(detail["idle"]["end_ns"], detail["idle"]["begin_ns"])
                self.assertTrue(raw[mode]["collection"]["child_collected"])
                self.assertTrue(raw[mode]["collection"]["home_cleanup"]["home_removed"])
        self.assertTrue(all(0 < window < 10 for window in consent_windows.values()))

    def test_actual_wait_idle_clone_has_complete_native_effect_and_phase(self):
        if not full_scale_enabled():
            return self.bounded_wait("idle")
        from tests.bus_scale_test_profile import banked_send_repository
        subject = CensusInstrumentTests().subject()
        bank = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-wait-entry-control-",
                                    dir=REAL_TEMP_ROOT)).resolve()
        print("BUS_SCALE_WAIT_CONTROL " + str(bank), flush=True)
        repository, _ = banked_send_repository(bank)
        workspace = bank / "workspace"
        repository.rename(workspace)
        workspace.chmod(0o700)
        deadline = time.monotonic() + 45
        seeds = subject.native_seeds(bank, small_count=1, deadline=deadline)
        subject._prepare_wait_seeds(seeds, workspace)
        producer = subject._load(subject.ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                 "native_wait_entry_control_producer")
        owner = subject._ContinuationOwner(deadline)
        try:
            values, raw = subject._original_observation("wait-idle", seeds["1"], workspace,
                                                       bank / "observation", owner, producer)
        except Exception as error:
            self.fail("native whole-wait effect/phase qualification failed: " + type(error).__name__ + ": " + str(error))
        self.assertGreater(values["cpu_seconds"], 0)
        self.assertEqual({"cpu", "diagnostic"}, set(raw))
        for mode, record in raw.items():
            with self.subTest(mode=mode):
                self.assertTrue(record["collection"]["child_collected"])
                self.assertTrue(record["collection"]["home_cleanup"]["home_removed"])
                self.assertEqual("unchanged_idle_tick", record["effect"]["effect"]["kind"])
                self.assertTrue(record["detail"]["startup"]["authorized_reconciliation"])

    def test_actual_wait_start_has_same_seed_independent_startup_and_idle_proof(self):
        if not full_scale_enabled():
            return self.bounded_wait("startup")
        from tests.bus_scale_test_profile import banked_send_repository
        subject = CensusInstrumentTests().subject()
        bank = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-wait-start-control-",
                                    dir=REAL_TEMP_ROOT)).resolve()
        print("BUS_SCALE_WAIT_START_CONTROL " + str(bank), flush=True)
        repository, _ = banked_send_repository(bank)
        workspace = bank / "workspace"
        repository.rename(workspace)
        workspace.chmod(0o700)
        deadline = time.monotonic() + 70
        seeds = subject.native_seeds(bank, small_count=1, deadline=deadline)
        subject._prepare_wait_seeds(seeds, workspace)
        producer = subject._load(subject.ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                 "native_wait_start_control_producer")
        owner = subject._ContinuationOwner(deadline)
        frozen = subject._inventory(seeds["1"])
        primary_values, primary = subject._original_observation("wait-start", seeds["1"], workspace,
                                                                bank / "primary", owner, producer)
        companion_values, companion = subject._original_observation("wait-idle", seeds["1"], workspace,
                                                                    bank / "companion", owner, producer)
        self.assertEqual(frozen, subject._inventory(seeds["1"]))
        self.assertGreater(primary_values["cpu_seconds"], 0)
        self.assertGreater(companion_values["cpu_seconds"], 0)
        for mode in ("cpu", "diagnostic"):
            with self.subTest(mode=mode):
                self.assertEqual(primary[mode]["seed_inventory"], companion[mode]["seed_inventory"])
                self.assertEqual("natural_deadline_exhausted", primary[mode]["effect"]["effect"]["kind"])
                detail = companion[mode]["detail"]
                self.assertTrue(detail["startup"]["authorized_reconciliation"])
                self.assertEqual(detail["startup"]["endpoint_ns"], detail["idle"]["begin_ns"])
                self.assertGreater(detail["idle"]["end_ns"], detail["idle"]["begin_ns"])
                self.assertTrue(primary[mode]["collection"]["child_collected"])
                self.assertTrue(companion[mode]["collection"]["child_collected"])



class CensusNativeAttentionTests(StableSourceFixture, unittest.TestCase):
    def observe_native_preface(self, case):
        import base64
        from datetime import datetime, timedelta
        import hashlib
        from tests.bus_scale_test_profile import banked_send_repository
        from tests.bus_scale_attention import attention_categories, require_attention_pair
        subject = CensusInstrumentTests().subject()
        producer = subject._load(subject.ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                                 "native_attention_control_producer")
        observer = producer.expanded_adapter()
        entry = producer.native_entry_module()
        bank = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-attention-control-",
                                    dir=REAL_TEMP_ROOT)).resolve()
        print("BUS_SCALE_ATTENTION_CONTROL " + case + " " + str(bank), flush=True)
        repository, _ = banked_send_repository(bank)
        workspace = bank / "workspace"
        repository.rename(workspace)
        workspace.chmod(0o700)
        deadline = time.monotonic() + 90
        if full_scale_enabled():
            pair = subject._expanded_native_seeds(case, bank, workspace, entry, deadline, small_count=1)
        else:
            # Retain native selected preparation and both real modes; native
            # history authoring and its tenfold population belong to vector 5.
            from tests.bus_scale_case_seeds import make_case_seed_pair
            seed_base = bank / "bounded-seeds"
            seed_base.mkdir(mode=0o700)
            pair = make_case_seed_pair(seed_base, "events", case, entry.routine_module(case),
                                       workspace, counts=(8, 16))
            (pair["prepared_home"].parent / "case-context.json").write_text(
                observer.canonical(pair["context"]) + "\n")
        pair["attention_time_policy"] = "native_preserved"
        context_path = pair["prepared_home"].parent / "case-context.json"
        context_raw = context_path.read_bytes()
        frozen = {scale: subject._inventory(pair[scale]) for scale in ("1", "10")}
        real_restore = observer.restore

        def restore_with_native_checkpoint(*args, **kwargs):
            receipt = real_restore(*args, **kwargs)
            receipt["checkpoint_preflight"] = subject._warm_fixture(pair["prepared_home"])
            receipt_path = Path(args[5] if len(args) > 5 else kwargs["receipt_path"])
            subject._write_json(receipt_path, receipt)
            return receipt

        observer.restore = restore_with_native_checkpoint
        owner = subject._ContinuationOwner(deadline)
        arms = {}
        for scale in ("1", "10"):
            place = bank / "observations" / scale
            place.mkdir(parents=True, mode=0o700)
            arms[scale + "x"] = observer.expanded_observation(
                owner, case, pair, scale, workspace, place, subject.ENTRY)
            receipt = json.loads((place / "collection.json").read_text())
            source_events = (pair[scale] / "events.jsonl").read_bytes()
            self.assertEqual("native_preserved", receipt["attention_time_policy"])
            for mode in ("cpu", "diagnostic"):
                with self.subTest(case=case, scale=scale, mode=mode):
                    row = receipt[mode]
                    stdout = (place / mode / "stdout.txt").read_bytes()
                    artifact = json.loads(stdout)
                    self.assertEqual(hashlib.sha256(stdout).hexdigest(), row["stdout_sha256"])
                    self.assertEqual(attention_categories(case, artifact), row["attention_categories"])
                    before_raw = json.loads((place / mode / "before-raw.json").read_text())
                    self.assertEqual(source_events, base64.b64decode(before_raw["events.jsonl"]))
                    self.assertEqual(source_events, (place / mode / "scale-fleet/events.jsonl").read_bytes())
                    proof = row["native_time_proof"]
                    self.assertEqual(hashlib.sha256(source_events).hexdigest(), proof["source_events_sha256"])
                    self.assertEqual(hashlib.sha256(context_raw).hexdigest(), proof["context_sha256"])
                    own = entry.routine_module(case)._projection_time(case, artifact["evidence"])
                    self.assertEqual(own.isoformat(), proof["native_own_clock"])
                    begin = datetime.fromisoformat(row["wall_begin"])
                    end = datetime.fromisoformat(row["wall_end"])
                    self.assertLessEqual(begin - timedelta(seconds=1), own)
                    self.assertLessEqual(own, end)
                    self.assertNotIn("clock_materialization", row)
                    self.assertTrue(row["collection"]["child_collected"])
                    self.assertTrue(row["collection"]["home_cleanup"]["home_removed"])
            for field in ("artifact", "delta"):
                self.assertEqual(receipt["cpu"]["equivalence"][field],
                                 receipt["diagnostic"]["equivalence"][field])
            self.assertEqual(frozen[scale], subject._inventory(pair[scale]))
            self.assertEqual(context_raw, context_path.read_bytes())
        require_attention_pair(case, arms["1x"], arms["10x"])
        self.assertEqual({scale: subject._inventory(pair[scale]) for scale in ("1", "10")}, frozen)

    def test_supervise_preserves_native_times_with_real_both_scale_mode_proofs(self):
        self.observe_native_preface("supervise")

    def test_watch_preserves_native_times_with_real_both_scale_mode_proofs(self):
        self.observe_native_preface("watch")


if __name__ == "__main__":
    unittest.main()
