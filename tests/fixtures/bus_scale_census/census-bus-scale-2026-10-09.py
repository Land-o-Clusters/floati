"""Parser-bound scratch census. No live root, product repair or host install."""
from __future__ import annotations

import importlib.util as _fixture_proof_util
from pathlib import Path as _FixtureProofPath

_fixture_proof_specification = _fixture_proof_util.spec_from_file_location(
    "_bus_scale_fixture_proof_bootstrap", _FixtureProofPath(__file__).with_name("_proof_namespace.py"))
_fixture_proof_bootstrap = _fixture_proof_util.module_from_spec(_fixture_proof_specification)
_fixture_proof_specification.loader.exec_module(_fixture_proof_bootstrap)
_fixture_proof_bootstrap.bind()


import copy
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import stat
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tests.bus_scale_integration import normalize_status_cache, qualify_wait_start, normalize_readiness
from tests.bus_scale_workloads import _CASES
from tests.bus_scale_attention import ATTENTION_CASES, AttentionCategoryMismatch, require_attention_pair
from tests.bus_scale_native_refusal import (is_functional_refusal, validate_functional_refusal,
                                          bind_functional_refusal)
FAMILIES = ("events", "registry", "wait-exhaustion", "deliveries")
NATIVE_BINDINGS = {case: row["command"] for case, row in _CASES.items()}
NATIVE_BINDINGS.update({"wait-idle": "wait", "readiness-read": "send", "receipt-append": "wait",
                        "wake-daemon-serve": "wake daemon serve"})
TEMP = Path("\x2fprivate/tmp" if sys.platform == "darwin" else tempfile.gettempdir()).resolve()
SESSION = "bus-scale-native-census"
ORIGINAL_CASES = frozenset({"send", "doctor", "status", "wait-start", "wait-idle",
    "readiness-read", "receipt-append", "log-history", "receipts-history"})


def workload_seed(seeds, family, case, workspace, base, entry, cache, *, counts=(1000, 10000)):
    """Keep old inputs untouched; author each new native case preface once."""
    if family not in FAMILIES or case not in _CASES:
        raise ValueError("workload seed requires a closed native case and family")
    if case in ORIGINAL_CASES:
        return seeds[family], workspace
    key = (family, case, counts)
    if key not in cache:
        from tests.bus_scale_case_seeds import make_case_seed_pair
        bank = base / "expanded" / family / ("bus-scale-census-owned-" + case)
        bank.mkdir(mode=0o700, parents=True)
        if case == "seat-teardown":
            seat_home = bank / "seat-home"
            seat_home.mkdir(mode=0o700)
            native_workspace = seat_home / "repo"
        else:
            native_workspace = bank / "workspace"
        shutil.copytree(workspace, native_workspace)
        native_workspace.chmod(0o700)
        seed_base = bank / "seeds"
        seed_base.mkdir(mode=0o700)
        pair = make_case_seed_pair(seed_base, family, case, entry.routine_module(case), native_workspace,
                                   counts=counts)
        cache[key] = pair, native_workspace
    return cache[key]


def expanded_adapter():
    specification = importlib.util.spec_from_file_location("census_expanded_observer",
        ROOT / "tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def native_entry_module():
    specification = importlib.util.spec_from_file_location("census_preparation_entry",
        ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def followup_selection():
    """Am3's closed population; companions qualify phases, never add verdicts."""
    return {"observations": [dict(family=family, case=case, metric=metric) for family, case, metric in (
        ("registry", "readiness-read", "cpu_seconds"),
        ("registry", "receipt-append", "ledger_hold_seconds"),
        ("registry", "log-history", "ledger_hold_seconds"),
        ("deliveries", "wait-start", "ledger_hold_seconds"),
        ("deliveries", "status", "cpu_seconds"))],
        "qualification_companions": [dict(family="deliveries", case="wait-idle", for_case="wait-start")],
        "pairs": 5, "deadline_seconds": 3600}


def validate_followup_selection(selection):
    from tests.bus_scale_commands import _signature
    if _signature(selection) != _signature(followup_selection()):
        raise ValueError("follow-up requires the exact five ruled observations and phase companion")


def followup_admission(record):
    """Classify only finished coordinates, retaining incomplete raw testimony."""
    from tests.bus_scale_metrics import compare_pairs
    validate_followup_selection(record["selection"])
    targets = {(row["family"], row["case"], row["metric"])
               for row in record["selection"]["observations"]}
    result = dict(status="incomplete", code="followup_incomplete",
                  observations=copy.deepcopy(record.get("observations", [])))
    status = record.get("status")
    if record.get("child_collected") is False:
        result["code"] = "followup_child_uncollected"
    elif status in {"deadline", "cancelled"}:
        result["code"] = "followup_deadline" if status == "deadline" else "followup_cancelled"
    seen, comparisons = set(), []
    for row in result["observations"]:
        if type(row) is not dict or set(row) != {"family", "case", "metric", "pairs"}:
            raise ValueError("follow-up coordinate fields are invalid")
        key = (row["family"], row["case"], row["metric"])
        if key not in targets or key in seen:
            raise ValueError("follow-up coordinate is unruled or duplicated")
        seen.add(key)
        supplied = row["pairs"]
        if type(supplied) is not list or len(supplied) > 5:
            raise ValueError("follow-up coordinate has an invalid pair count")
        if len(supplied) != 5:
            continue
        if row["case"] == "wait-start":
            companions = record.get("qualification_companions", [])
            if len(companions) != 1 or len(companions[0].get("pairs", [])) != 5:
                continue
            for phase in companions[0]["pairs"]:
                proof = qualify_wait_start(phase["primary_row"], phase["idle_row"], phase["details"])
                if proof != phase["proof"]:
                    raise ValueError("retained phase proof differs from its native qualification")
        metric = compare_pairs(supplied, history_output=row["case"] in {
            "log-history", "receipts-history"})["metrics"][row["metric"]]
        comparisons.append(dict(family=row["family"], case=row["case"], metric=row["metric"], **metric))
    if comparisons:
        result["comparisons"] = comparisons
    if (len(comparisons) == len(targets) and record.get("child_collected") is True
            and status == "complete"):
        result.update(status="followup_complete", code="followup_complete")
    return result


def _selected_pair(scope, case, row):
    from tests.bus_scale_workloads import _require_arm
    if (set(row) != {"family", "prefix_exact", "selected_work_equal", "arms"}
            or row["family"] not in FAMILIES or row["prefix_exact"] is not True
            or row["selected_work_equal"] is not True or set(row["arms"]) != {"1x", "10x"}):
        raise ValueError("selected follow-up pair lacks complete native evidence")
    one, ten = (_require_arm(case, row["arms"][scale]) for scale in ("1x", "10x"))
    if ten["records"] != 10 * one["records"] or ten["bytes"] <= one["bytes"]:
        raise ValueError("selected follow-up pair is not exact tenfold history growth")
    require_attention_pair(case, one, ten)


def _metric_pair(index, row):
    return {"order": [1, 10] if index % 2 == 0 else [10, 1], **{
        scale: {"cpu_seconds": row["arms"][scale + "x"]["metrics"]["cpu_seconds"],
                "read_bytes": row["arms"][scale + "x"]["metrics"]["bytes_read"],
                "ledger_hold_seconds": row["arms"][scale + "x"]["metrics"]["max_ledger_lock_seconds"]}
        for scale in ("1", "10")}}


def _run_followup(record, scope, seeds, workspace, base, owner):
    from tests.bus_scale_workloads import _require_scope
    _require_scope(scope)
    selection = followup_selection()
    validate_followup_selection(selection)
    record.update(selection=selection, observations=[], qualification_companions=[],
                  native_observations=[], child_collected=True)
    for target in selection["observations"]:
        family, case = target["family"], target["case"]
        selected = dict(target, pairs=[])
        record["observations"].append(selected)
        companion = None
        if case == "wait-start":
            companion = dict(selection["qualification_companions"][0], pairs=[])
            record["qualification_companions"].append(companion)
        seed, relative = seeds[family], seeds[family]["ledger_relative"]
        if not (seed["10"] / relative).read_bytes().startswith((seed["1"] / relative).read_bytes()):
            raise ValueError("follow-up seed does not preserve its exact prefix")
        for index in range(selection["pairs"]):
            order, rows = ([1, 10] if index % 2 == 0 else [10, 1]), {}
            for selected_case in (("wait-idle", case) if companion is not None else (case,)):
                row = dict(family=family, prefix_exact=True, selected_work_equal=False, arms={})
                rows[selected_case] = row
                record["native_observations"].append(dict(
                    family=family, case=selected_case, pair_index=index,
                    qualification_only=selected_case == "wait-idle", observation=row))
                for scale in order:
                    place = base / "runs" / family / str(index) / selected_case / str(scale)
                    place.mkdir(parents=True)
                    print("followup " + family + " " + selected_case + " pair=" + str(index)
                          + " scale=" + str(scale), flush=True)
                    row["arms"][str(scale) + "x"] = observation(
                        owner, selected_case, seed[str(scale)], relative, workspace, place)
                row["selected_work_equal"] = all(arm["child_collected"] and all(arm["coverage"].values())
                                                   for arm in row["arms"].values())
                _selected_pair(scope, selected_case, row)
            if companion is not None:
                details = {str(scale) + "x": {mode: json.loads((base / "runs" / family / str(index)
                    / "wait-idle" / str(scale) / mode / "detail.json").read_text())
                    for mode in ("cpu", "diagnostic")} for scale in order}
                proof = qualify_wait_start(rows[case], rows["wait-idle"], details)
                companion["pairs"].append(dict(primary_row=rows[case], idle_row=rows["wait-idle"],
                                                details=details, proof=proof))
            selected["pairs"].append(_metric_pair(index, rows[case]))
    record["status"] = "complete"
    record.update(followup_admission(record))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_matrix(scope, matrix, *, first_pass=False):
    from tests.bus_scale_workloads import _require_scope, require_observations
    from tests.bus_scale_metrics import compare_pairs, compare_first_pass, derive_red_map
    if type(first_pass) is not bool:
        raise ValueError("first_pass must be boolean")
    count = 2 if first_pass else 5
    comparison = compare_first_pass if first_pass else compare_pairs
    if type(matrix) is not dict or set(matrix) != set(FAMILIES):
        raise ValueError("census requires the exact four-family matrix")
    _require_scope(scope)
    cases = set(scope["cases"])
    # Reject incomplete shape across the entire matrix before validating any
    # arm. A later malformed family cannot be repaired by earlier valid work.
    for family in FAMILIES:
        pairs = matrix[family]
        if type(pairs) is not list or len(pairs) != count:
            raise ValueError("each family requires " + str(count) + " complete balanced pairs")
        for index, pair in enumerate(pairs):
            if (type(pair) is not dict or set(pair) != {"order", "observations"}
                    or pair["order"] != ([1, 10] if index % 2 == 0 else [10, 1])):
                raise ValueError("matrix pair is incomplete or not balanced")
            if type(pair["observations"]) is not dict or set(pair["observations"]) != cases:
                raise ValueError("workload observations has missing or extra fields")
    comparisons, maximum, ambiguous, functional_reds = [], {}, [], []
    rank = {"info": 0, "pass": 1, "ambiguous": 2, "red": 3}
    for family in FAMILIES:
        pairs = matrix[family]
        selected_scope = dict(scope, cases={case: dict(row, family=family)
                                           for case, row in scope["cases"].items()})
        for pair in pairs:
            require_observations(selected_scope, pair["observations"], allow_ruled_refusals=True)
        for case in scope["cases"]:
            failures = []
            for index, pair in enumerate(pairs):
                for scale, arm in pair["observations"][case]["arms"].items():
                    if is_functional_refusal(arm):
                        validate_functional_refusal(case, family, scale, arm)
                        failures.append(dict(pair_index=index, scale=scale, raw_sha256=arm["raw_sha256"]))
            if failures:
                functional_reds.append(dict(case=case, family=family, native_command=NATIVE_BINDINGS[case],
                    classification="red", row="F2-SEAT-TEARDOWN-SCALE-1", failed_cells=failures,
                    ruling=dict(sha="9f9187b7a17a63e40438262b81ea261fff7e5fc2",
                                doc="docs/evidence/gate-opus-2026-10-15-seat30.md", section=11)))
                continue
            observations = []
            for pair in pairs:
                arms = pair["observations"][case]["arms"]
                observations.append({"order": pair["order"], **{
                    scale: {"cpu_seconds": arms[scale + "x"]["metrics"]["cpu_seconds"],
                            "read_bytes": arms[scale + "x"]["metrics"]["bytes_read"],
                            "ledger_hold_seconds": arms[scale + "x"]["metrics"]["max_ledger_lock_seconds"]}
                    for scale in ("1", "10")}})
            result = comparison(observations, history_output=scope["cases"][case]["history_output"])
            command = NATIVE_BINDINGS[case]
            comparisons.append(dict(case=case, family=family, native_command=command, **result))
            for metric, row in result["metrics"].items():
                key = command, metric
                if row["classification"] == "ambiguous":
                    ambiguous.append(dict(case=case, family=family, subcommand=command, metric=metric, **row))
                if (key not in maximum or (rank[row["classification"]], row["ratio"])
                        > (rank[maximum[key]["classification"]], maximum[key]["ratio"])):
                    maximum[key] = dict(row, governing_case=case, governing_family=family)
    contracts = {path: scope["inventory"][path] for path in set(NATIVE_BINDINGS.values())}
    flat = [dict(subcommand=path, metric=metric, ratio=row["ratio"], pair_ratios=row["pair_ratios"],
                 classification=row["classification"], contract=contracts[path])
            for (path, metric), row in sorted(maximum.items())]
    reds = derive_red_map([row for row in flat if row["classification"] != "ambiguous"], contracts)
    return {"comparisons": comparisons, "native_metric_rows": flat, "functional_reds": functional_reds,
            "governing_observations": [dict(subcommand=path, metric=metric, **row)
                                      for (path, metric), row in sorted(maximum.items())],
            "ambiguous_observations": ambiguous,
            "red_map": [dict(subcommand=path, metric=metric,
                             ratio=maximum[(path, metric)]["ratio"], max_pair_ratio=ratio)
                        for (path, metric), ratio in sorted(reds.items())]}


def files(home):
    return {relative: {"sha256": sha(raw), "bytes": len(raw),
                       "mode": stat.S_IMODE((home / relative).stat().st_mode)}
            for relative, raw in raw_files(home).items()}


def raw_files(home):
    result = {}
    for path in sorted(home.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or path.resolve(strict=True) != path:
            raise RuntimeError("nonordinary native fixture inventory: " + str(path))
        result[path.relative_to(home).as_posix()] = path.read_bytes()
    return result


def write_collection(path, receipt):
    raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(raw)
    return sha(path.read_bytes())


def native_delta(case, before, after, *, home=None, receipt_prefix_input=None, prefix_input=None,
                 cut_input=None):
    receipt_cache = None
    receipt_candidate = None
    cut_candidate, cut_changed = None, set()
    if case == "send":
        from _bus_scale_fixture_proofs.bus_scale_cut_prefix_effects import (
            CACHE as CUT_CACHE, raw_candidate_delta, verify_cut_prefix_effect)
        changed = {path for path in set(before) | set(after)
            if (path == CUT_CACHE or path.startswith(CUT_CACHE + "/"))
            and before.get(path) != after.get(path)}
        if changed or cut_input is not None:
            if home is None or cut_input is None:
                raise RuntimeError("native cut cache delta requires complete pre-child proof")
            cut_candidate = raw_candidate_delta(before, after)
            proof = verify_cut_prefix_effect(home, before, after, cut_candidate, cut_input)
            cut_changed = set(proof["changed_files"])
            if cut_changed != changed:
                raise RuntimeError("native cut cache delta population differs from complete proof")
    if case in {"receipt-append", "wait-start", "wait-idle"}:
        from _bus_scale_fixture_proofs.bus_scale_receipt_prefix_effects import cache_name
        receipt_cache = cache_name()
    append = {
        "send": {"events.jsonl", "receipts/timings/send.jsonl"},
        "doctor": {"receipts/timings/doctor.jsonl"},
        "receipt-append": {"receipts/codex-wait-exhaustion/scale-worker.jsonl"},
    }.get(case, set())
    replace = set()
    if case in {"wait-start", "wait-idle"}:
        append = {"receipts/codex-wait-exhaustion/scale-worker.jsonl",
                  "receipts/wake-waiter-exit/scale-worker.jsonl",
                  "receipts/stop-holds/scale-worker.jsonl",
                  "receipts/cursor-presentations/scale-worker.jsonl"}
        replace = {"state/codex-wait/scale-worker/holder.json",
                   "state/codex-wait/scale-worker/breaker.json",
                   "state/cursor-wait/scale-worker/presentation-history.v1.json",
                   "state/wake-daemon/adapters/scale-worker/codex.json",
                   "state/waiter-observations/scale-worker.json"}
        # Main records the complete native invocation through its final flush.
        # Admit only this fixture's exact session coordinate; the wait decoder
        # separately proves its process, hold pair and complete state fields.
        invocation = "state/waiter-invocations/scale-worker/" + sha(
            b"bus-scale-native-census")
        replace.add(invocation + ".json")
    if case == "status":
        replace = {".floati-snapshots/v0/status-" + sha(b"default") + ".json"}
    result = {}
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path, b""), after.get(path)
        if path in before and new == old:
            continue
        if new is None:
            raise RuntimeError("unexpected removed native payload: " + path)
        if path in append:
            if not new.startswith(old):
                raise RuntimeError("native append rewrote its prior prefix: " + path)
            operation, payload = "append", new[len(old):]
        elif path in replace:
            operation, payload = "replace", new
        elif case == "send" and path.startswith("state/wake-hints/"):
            # Encode the actual bytes; the closed native routing and metadata
            # proof below must admit every hint before this delta can escape.
            operation, payload = "append", new[len(old):]
        elif path in cut_changed:
            result[path] = cut_candidate[path]
            continue  # Only the independent exact cut proof admits these files.
        elif receipt_cache is not None and path.startswith(receipt_cache + "/"):
            if home is None or receipt_prefix_input is None:
                raise RuntimeError("native receipt cache delta requires complete pre-child proof")
            from _bus_scale_fixture_proofs.bus_scale_receipt_prefix_effects import raw_candidate_delta
            if receipt_candidate is None:
                receipt_candidate = raw_candidate_delta(before, after)
            result[path] = receipt_candidate[path]
            continue  # The complete native map/delta proof below admits it.
        elif case == "send" and path.startswith(".send-prefix/"):
            if home is None or prefix_input is None:
                raise RuntimeError("native send cache delta requires complete pre-child proof")
            operation = "replace" if path.endswith(".json") else "append"
            if operation == "append" and not new.startswith(old):
                raise RuntimeError("native send cache node rewrote retained bytes: " + path)
            payload = new if operation == "replace" else new[len(old):]
        elif path not in before and not new and (
                path.endswith(".jsonl.lock") and path[:-5] in after
                or path.startswith("receipts/wake-coordination/") and path.endswith("/lane.lock")
                or case in {"wait-start", "wait-idle"}
                and path in {"state/wake-control/scale-worker.lock", invocation + ".lock"}):
            operation, payload = "create_lock", b""
        else:
            raise RuntimeError("unexpected changed native payload: " + path)
        result[path] = dict(operation=operation, before_sha256=sha(old), after_sha256=sha(new),
            before_bytes=len(old), after_bytes=len(new), payload_base64=base64.b64encode(payload).decode())
    if receipt_cache is not None and any(path.startswith(receipt_cache + "/") for path in result):
        from _bus_scale_fixture_proofs.bus_scale_receipt_prefix_effects import verify_receipt_prefix_effect
        verify_receipt_prefix_effect(home, before, after, result, receipt_prefix_input)
    if case == "send" and any(path.startswith(".send-prefix/") for path in result):
        from _bus_scale_fixture_proofs.bus_scale_prefix_effects import verify_prefix_effect
        verify_prefix_effect(home, before, after, result, prefix_input)
    if case == "send":
        from _bus_scale_fixture_proofs.bus_scale_wake_hint_effects import verify_wake_hint_effects
        from floati.events import EVENT_KINDS
        from floati.root import FloatiRoot
        if home is None and any(path.startswith("state/wake-hints/") for path in result):
            raise RuntimeError("native send hint requires its exact home")
        if home is not None:
            verify_wake_hint_effects(FloatiRoot.open_direct_home(Path(home)), before, after,
                                     result, ledger_kinds={"events.jsonl": EVENT_KINDS})
    return result


def owner_module():
    specification = importlib.util.spec_from_file_location("bus_scale_census_owner",
        ROOT / "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class UncollectedChild(RuntimeError):
    uncollected_child = True


class IncompleteObservation(RuntimeError):
    def __init__(self, detail, status):
        super().__init__(detail)
        self.collection_status = status


def prepare(seeds, workspace):
    from floati.root import FloatiRoot
    from floati.events import EventLog
    from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
    for pair in seeds.values():
        home = pair["1"]
        before = files(home)
        mapping = home / "codex-wait/workspaces.v0.json"
        mapping.parent.mkdir(exist_ok=True)
        mapping.write_text(json.dumps({"schema_version": 0, "tenant_id": "scale-fleet",
            "mappings": [{"workspace": str(workspace), "node_id": "scale-worker"}]},
            sort_keys=True, separators=(",", ":")) + "\n")
        root = FloatiRoot.open_direct_home(home)
        events = EventLog(root).records()
        if not events:
            EventLog(root).send("scale-worker", "architect", "floati", "a" * 40,
                "docs/evidence/bus-scale-seed.md", "bounded scale fixture",
                idempotency_key="census-fixed-history")
            events = EventLog(root).records()
        from floati.jsonl import read_records
        deliveries = read_records(root, "receipts/deliveries/architect.jsonl",
                                  allowed_kinds={"delivery_receipt", "delivered_unpresented"})
        if not deliveries:
            EventLog(root)._write_delivery_receipt("architect", [events[0]])
        participant = resolve_participant(home, workspace)
        consent = CodexWaitConsentLedger(root).arm(participant.binding,
            hook_timeout_seconds=70, wait_deadline_seconds=60, idempotency_key="scale-consent")
        CodexWaitSessionLedger(root).arm(participant.binding, consent, SESSION,
            idempotency_key="scale-session")
        after = files(home)
        for relative in after:
            if after[relative] != before.get(relative):
                if relative == pair["ledger_relative"]:
                    raise RuntimeError("wait preparation changed the scaled ledger")
                destination = pair["10"] / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(home / relative, destination)
        small, large = files(home), files(pair["10"])
        small.pop(pair["ledger_relative"])
        large.pop(pair["ledger_relative"])
        if small != large:
            raise RuntimeError("seed arms differ outside their one declared ledger")


def effect_witness(case, home, before, stdout, stderr, detail, *, workspace=None):
    from floati.root import FloatiRoot
    from floati.events import EventLog
    from floati.jsonl import read_records
    from floati.codex_wait_contract import CodexWaitReceiptLedger
    root = FloatiRoot.open_direct_home(home)
    if case == "send":
        artifact = json.loads(stdout)
        if artifact["status"] != "ok":
            raise RuntimeError("native send refused: " + stdout[-1000:])
        message = artifact["evidence"]["message"]
        matches = [row for row in EventLog(root).event_records()
                   if row.get("idempotency_key") == "census-send" and row["kind"] == "message_envelope"]
        if len(matches) != 1 or matches[0] != message:
            raise RuntimeError("native send has no exact appended envelope")
        if workspace is None:
            raise RuntimeError("native send requires its owned Git workspace witness")
        entry_spec = importlib.util.spec_from_file_location("census_send_effect_input",
            ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py")
        entry = importlib.util.module_from_spec(entry_spec)
        entry_spec.loader.exec_module(entry)
        expected = dict(sender="scale-worker", recipient="architect", repo="floati",
                        sha=entry.workspace_send_sha(workspace),
                        doc="note.md", note="fixed selected census send")
        if any(message.get(key) != value for key, value in expected.items()):
            raise RuntimeError("native send changed its fixed authored work")
        companions = [row for row in EventLog(root).event_records()
            if row["kind"] == "send_readiness_observation" and row["envelope_id"] == message["id"]]
        if (len(companions) != 1 or companions[0]["readiness"] != artifact["evidence"]["recipient_readiness"]
                or companions[0]["timestamp"] != message["timestamp"]):
            raise RuntimeError("native send readiness companion does not bind the exact envelope")
        events = EventLog(root).event_records()
        prior = before["event_rows"]
        if events != prior + [message, companions[0]]:
            raise RuntimeError("native send suffix is not exactly its two expected events")
        return dict(exit_code=0, status="ok", effect=dict(kind="message_envelope_appended", count=1))
    if case in {"wait-start", "wait-idle"}:
        decision = json.loads(stdout)
        if (decision != {"decision": "block", "reason": "(floati: wait deadline exhausted; end this turn to re-arm)"}
                or stderr.strip()):
            raise RuntimeError("native wait did not reach its natural deadline: " + stdout[-1000:] + stderr[-1000:])
        rows = read_records(root, "receipts/codex-wait-exhaustion/scale-worker.jsonl",
                            allowed_kinds=set(CodexWaitReceiptLedger._KINDS))
        if (len(rows) != before["exhaustion_count"] + 1
                or rows[:-1] != before.get("exhaustion_rows", rows[:-1])
                or rows[-1]["session_digest"] != sha(SESSION.encode())
                or rows[-1]["node_id"] != "scale-worker" or rows[-1]["outcome"] != "consent_reopened"):
            raise RuntimeError("wait exhaustion append was not observed")
        from floati.stop_hold import StopHoldLedger
        holds = StopHoldLedger(root).read("scale-worker")
        exits = read_records(root, "receipts/wake-waiter-exit/scale-worker.jsonl",
                            allowed_kinds={"wake_waiter_exit_receipt"})
        if (len(holds) != 2 or [row["phase"] for row in holds] != ["start", "exit"]
                or any(row["acting_session_id"] != SESSION for row in holds)
                or len(exits) != 1 or exits[0]["reason_code"] != "exhausted"
                or exits[0]["session_digest"] != rows[-1]["session_digest"]
                or exits[0]["waited_seconds"] != rows[-1]["waited_seconds"]):
            raise RuntimeError("native waiter exit/hold/session effect is incomplete")
        if case == "wait-idle" and "end_ns" not in detail["idle"]:
            raise RuntimeError("unchanged idle tick was not observed")
        return dict(exit_code=0, status="ok", effect=dict(kind=("unchanged_idle_tick"
            if case == "wait-idle" else "natural_deadline_exhausted"), count=1))
    if case == "receipt-append":
        artifact = json.loads(stdout)
        rows = read_records(root, "receipts/codex-wait-exhaustion/scale-worker.jsonl",
                            allowed_kinds=set(CodexWaitReceiptLedger._KINDS))
        if (len(rows) != before["exhaustion_count"] + 1
                or rows[:-1] != before.get("exhaustion_rows", rows[:-1])
                or rows[-1] != artifact or artifact["outcome"] != "consent_reopened"
                or artifact["node_id"] != "scale-worker" or artifact["session_digest"] != sha(SESSION.encode())
                or artifact["waited_seconds"] != 2 or artifact["idempotency_key"] != "census-receipt"):
            raise RuntimeError("receipt append is not the observed native effect")
        return dict(exit_code=0, status="consent_reopened",
                    effect=dict(kind="exhaustion_receipt_appended", count=1))
    if case == "log-history":
        artifact = json.loads(stdout)
        if artifact["status"] != "ok" or artifact["evidence"]["messages"] != EventLog(root).records():
            raise RuntimeError("full native log did not render its exact authored history")
        return dict(exit_code=0, status="ok", effect=dict(kind="history_returned", count=1))
    artifact = json.loads(stdout)
    if case == "readiness-read":
        if artifact["state"] != "unknown" or artifact["checked_mechanisms"] != ["lease", "stop_hook", "seat_board"]:
            raise RuntimeError("private readiness fixture outcome changed")
        return dict(exit_code=0, status="unknown", effect=dict(kind="readiness_observed", count=1))
    status = artifact["status"]
    expected = "degraded" if case == "doctor" else "ok"
    if status != expected:
        raise RuntimeError("native read outcome changed: " + stdout[-1000:])
    tags = {"status": "status_returned", "doctor": "doctor_returned", "receipts-history": "history_returned"}
    witness = dict(exit_code=35 if case == "doctor" else 0, status=status,
                   effect=dict(kind=tags[case], count=1))
    if case == "doctor":
        witness["findings_sha256"] = sha(json.dumps(artifact["evidence"]["findings"], sort_keys=True).encode())
    if case == "status":
        from datetime import datetime
        from floati.projection import FleetProjection
        evidence = artifact["evidence"]
        if (artifact.get("command") != "status" or evidence.get("root") != str(home)
                or evidence.get("tenant_id") != "scale-fleet"
                or evidence.get("kind") != "fleet_status"
                or sorted(node["node_id"] for node in evidence["nodes"]) != ["architect", "scale-worker"]):
            raise RuntimeError("native status omitted its exact fixture evidence")
        now = datetime.fromisoformat(evidence["nodes"][0]["wake_health"]["recent_sessions"]["window_end"].replace("Z", "+00:00"))
        snapshot, mode = FleetProjection(root)._snapshot(now)
        from floati.installer_shadow import observe_installer_shadow
        expected_evidence = dict(status_schema_version=1, kind="fleet_status", root=str(home),
            tenant_id="scale-fleet", mode=mode, installer_shadow=observe_installer_shadow(None), **snapshot)
        if {key: value for key, value in evidence.items() if key != "scope"} != expected_evidence:
            raise RuntimeError("native status differs from its full fixed-time native projection")
    if case == "receipts-history":
        from floati.projection import FleetProjection
        if artifact["evidence"] != FleetProjection(root).receipts("architect") or not artifact["evidence"]["deliveries"]:
            raise RuntimeError("full receipt dump omitted or changed its exact native history")
    return witness


def observation(owner, case, seed, relative, workspace, place):
    from floati.root import FloatiRoot
    from floati.jsonl import _decode_path_records
    from floati.codex_wait_contract import CodexWaitReceiptLedger
    from floati.events import EVENT_KINDS
    expected = 35 if case == "doctor" else 0
    records, outputs = {}, {}
    for mode in ("cpu", "diagnostic"):
        home = place / mode / "scale-fleet"
        shutil.copytree(seed, home)
        if files(home) != files(seed):
            raise RuntimeError("working root is not byte-identical to its seed")
        frozen = raw_files(home)
        preflight = None
        if case == "send":
            from floati.send_prefix import warm
            from floati.cut_prefix import warm as warm_cut
            from _bus_scale_fixture_proofs.bus_scale_prefix_effects import verify_warmed_clone
            root = FloatiRoot.open_direct_home(home)
            warmed = warm(root)
            if warmed is None:
                raise RuntimeError("native send checkpoint preflight could not certify the clone")
            cut_warmed = warm_cut(root)
            if cut_warmed is None:
                raise RuntimeError("native cut checkpoint preflight could not certify the clone")
            preflight = {"outside_child_measurement": True, "native_warm": warmed,
                         "native_cut_warm": cut_warmed,
                         "complete_clone_proof": verify_warmed_clone(home, frozen)}
        elif case in {"receipt-append", "wait-start", "wait-idle"}:
            from floati.receipt_prefix import warm
            from _bus_scale_fixture_proofs.bus_scale_receipt_prefix_effects import cache_name, ledger_relative
            warmed = warm(FloatiRoot.open_direct_home(home), "scale-worker")
            if warmed is None:
                raise RuntimeError("native receipt checkpoint preflight could not certify the clone")
            warmed_raw = raw_files(home)
            name, lock = cache_name(), ledger_relative() + ".lock"
            old_plain = {p: raw for p, raw in frozen.items() if not p.startswith(name + "/")}
            new_plain = {p: raw for p, raw in warmed_raw.items() if not p.startswith(name + "/")}
            if lock not in old_plain and new_plain.get(lock) == b"":
                old_plain[lock] = b""
            if old_plain != new_plain:
                raise RuntimeError("receipt warming changed authoritative clone bytes or unrelated paths")
            preflight = {"outside_child_measurement": True, "native_warm": warmed,
                         "authoritative_seed_bytes_unchanged": True}
        if preflight is not None:
            preflight["seed_inventory"] = files(seed)
            preflight["warmed_inventory"] = files(home)
            write_collection(place / (mode + "-preflight.json"), preflight)
        raw = (home / relative).read_bytes()
        raw_before, inventory_before = raw_files(home), files(home)
        receipt_input, prefix_input, cut_input = None, None, None
        if case == "send":
            from _bus_scale_fixture_proofs.bus_scale_prefix_effects import capture_prefix_input
            from _bus_scale_fixture_proofs.bus_scale_cut_prefix_effects import capture_cut_prefix_input
            prefix_input = capture_prefix_input(home, raw_before)
            cut_input = capture_cut_prefix_input(home, raw_before)
        elif case in {"receipt-append", "wait-start", "wait-idle"}:
            from _bus_scale_fixture_proofs.bus_scale_receipt_prefix_effects import capture_receipt_prefix_input
            receipt_input = capture_receipt_prefix_input(home, raw_before)
        exhaustion = "receipts/codex-wait-exhaustion/scale-worker.jsonl"
        prior = _decode_path_records(home / exhaustion, "scale-fleet",
            set(CodexWaitReceiptLedger._KINDS), raw_before.get(exhaustion, b""))
        before = {"exhaustion_count": len(prior), "exhaustion_rows": prior,
            "event_rows": _decode_path_records(home / "events.jsonl", "scale-fleet",
                set(EVENT_KINDS), raw_before.get("events.jsonl", b""))}
        output, error, detail_path = (home.parent / name for name in ("stdout.txt", "stderr.txt", "detail.json"))
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "FLOATI_", "PYTHON", "LD_", "DYLD_"))}
        env.update(PATH="/usr/bin:/bin", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        wall_begin = datetime.now(timezone.utc)
        with output.open("xb") as out_stream, error.open("xb") as err_stream:
            child = owner.launch_owned_child(["/usr/bin/python3", "-B",
                str(ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py"),
                mode, case, str(home), str(workspace), str(detail_path), "owned-census-v1"],
                cwd=workspace, env=env, stdout=out_stream, stderr=err_stream)
            collected = owner.collect_owned_child(child, deadline_seconds=120, collection_seconds=10)
        wall_end = datetime.now(timezone.utc)
        usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        # Preserve primary collection and CPU even if a later diagnostic or
        # semantic check fails. Partial data is retained, never qualified.
        records[mode] = dict(collection=collected, child_pid=child.pid,
            wall_begin=wall_begin.isoformat(), wall_end=wall_end.isoformat(),
            cpu_seconds=(usage_after.ru_utime + usage_after.ru_stime
                - usage_before.ru_utime - usage_before.ru_stime),
            stdout_sha256=sha(output.read_bytes()), stderr_sha256=sha(error.read_bytes()))
        write_collection(place / (mode + "-collection.json"), records[mode])
        if not collected["child_collected"]:
            raise UncollectedChild("native census child remains live at " + str(place))
        stdout, stderr = output.read_text(), error.read_text()
        if collected["status"] != "complete" or collected["exit_code"] != expected or not detail_path.exists():
            raise IncompleteObservation("native census entry incomplete: " + json.dumps(records[mode])
                                        + " " + stderr[-1500:], collected["status"])
        detail = json.loads(detail_path.read_text())
        inventory_after, raw_after = files(home), raw_files(home)
        for path in set(inventory_before) & set(inventory_after):
            if inventory_before[path]["mode"] != inventory_after[path]["mode"]:
                raise RuntimeError("native operation changed a retained fixture mode: " + path)
        delta = native_delta(case, raw_before, raw_after, home=home,
                             receipt_prefix_input=receipt_input, prefix_input=prefix_input,
                             cut_input=cut_input)
        write_collection(place / (mode + "-native-delta.json"), dict(
            inventory_before=inventory_before, inventory_after=inventory_after, native_delta=delta))
        witness = effect_witness(case, home, before, stdout, stderr, detail, workspace=workspace)
        cache_payload = None
        if case == "status":
            from tests.bus_scale_effects import validate_status_cache
            cache_payload = validate_status_cache(home, json.loads(stdout))
        if case in {"send", "doctor"}:
            from floati.timings import argv_digest, outcome_for_status, refusal_code_for
            relative_timing = "receipts/timings/" + case + ".jsonl"
            old_timing = _decode_path_records(home / relative_timing, "scale-fleet", {"timing_receipt"},
                raw_before.get(relative_timing, b""))
            new_timing = _decode_path_records(home / relative_timing, "scale-fleet", {"timing_receipt"},
                raw_after.get(relative_timing, b""))
            if (len(new_timing) != len(old_timing) + 1 or new_timing[:-1] != old_timing
                    or new_timing[-1]["command"] != case
                    or new_timing[-1]["outcome"] != outcome_for_status(witness["status"])
                    or new_timing[-1]["argv_digest"] != argv_digest(detail["argv"], home)
                    or new_timing[-1]["refusal_code"] != refusal_code_for(
                        outcome_for_status(witness["status"]), json.loads(stdout)["evidence"])):
                raise RuntimeError("native timing append does not bind the exact invocation")
        if files(home) != inventory_after:
            raise RuntimeError("parent native validation changed the measured fixture")
        outputs[mode] = dict(native=witness, detail=detail, artifact=json.loads(stdout),
            home=home, wall_begin=wall_begin, wall_end=wall_end, cache_payload=cache_payload,
            prefix_input=prefix_input, receipt_prefix_input=receipt_input, cut_input=cut_input,
            before_raw_files=raw_before)
        records[mode].update(cpu_seconds=(usage_after.ru_utime + usage_after.ru_stime
            - usage_before.ru_utime - usage_before.ru_stime),
            stdout_sha256=sha(output.read_bytes()), stderr_sha256=sha(error.read_bytes()),
            detail_sha256=sha(detail_path.read_bytes()), inventory_before=inventory_before,
            inventory_after=inventory_after, native_delta=delta, native_effect=witness)
        if mode == "cpu":
            raw_seed = raw
    if case in {"send", "receipt-append", "doctor", "wait-start", "wait-idle"}:
        from tests.bus_scale_effects import normalize_durable_delta, normalize_timing_delta
        decoder = normalize_timing_delta if case == "doctor" else normalize_durable_delta
        for mode in ("cpu", "diagnostic"):
            observed = outputs[mode]
            semantic = decoder(case, records[mode]["native_delta"], observed["artifact"],
                wall_begin=observed["wall_begin"], wall_end=observed["wall_end"],
                child_pid=records[mode]["child_pid"], home=observed["home"], workspace=workspace,
                child_cpu_seconds=records[mode]["cpu_seconds"],
                **({"prefix_input": observed["prefix_input"], "before_raw_files": observed["before_raw_files"]}
                   if observed["prefix_input"] is not None else {}),
                **({"cut_input": observed["cut_input"]} if observed["cut_input"] is not None else {}),
                **({"receipt_prefix_input": observed["receipt_prefix_input"], "before_raw_files": observed["before_raw_files"]}
                   if observed["receipt_prefix_input"] is not None else {}))
            records[mode]["durable_equivalence"] = semantic
            write_collection(place / (mode + "-durable-equivalence.json"), semantic)
            if files(observed["home"]) != records[mode]["inventory_after"]:
                raise RuntimeError("durable normalization changed the measured fixture")
        if records["cpu"]["durable_equivalence"]["delta"] != records["diagnostic"]["durable_equivalence"]["delta"]:
            raise RuntimeError("diagnostics changed complete native durable testimony")
        if case != "doctor" and records["cpu"]["durable_equivalence"]["artifact"] != records["diagnostic"]["durable_equivalence"]["artifact"]:
            raise RuntimeError("diagnostics changed complete native durable stdout")
    if case in {"readiness-read", "log-history", "receipts-history"}:
        artifacts = []
        for mode in ("cpu", "diagnostic"):
            if case == "readiness-read":
                normalized = normalize_readiness(outputs[mode]["home"], outputs[mode]["artifact"])
                records[mode]["artifact_equivalence"] = normalized
                artifacts.append(normalized["artifact"])
                if files(outputs[mode]["home"]) != records[mode]["inventory_after"]:
                    raise RuntimeError("readiness normalization changed the measured fixture")
                continue
            artifact = copy.deepcopy(outputs[mode]["artifact"])
            audit = []
            scope = artifact.get("evidence", {}).get("scope")
            if scope is not None:
                if scope.get("root") != str(outputs[mode]["home"]):
                    raise RuntimeError("native history scope names a foreign fixture")
                scope["root"] = "<bus-scale-fixture>"
                audit.append({"pointer": "/evidence/scope/root", "reason": "exact arm clone coordinate"})
            artifacts.append(artifact)
            records[mode]["artifact_equivalence"] = dict(artifact=artifact, normalizations=audit)
        if artifacts[0] != artifacts[1]:
            raise RuntimeError("diagnostics changed complete native read stdout")
    if case in {"doctor", "status"}:
        from tests.bus_scale_equivalence import normalize_artifact
        original = outputs["cpu"]["artifact"]["evidence"]
        clock = (next(row["wake_health"]["recent_sessions"]["window_end"]
                      for row in original["findings"] if row["code"] == "wake_health")
                 if case == "doctor" else original["nodes"][0]["wake_health"]["recent_sessions"]["window_end"])
        canonical = datetime.fromisoformat(clock.replace("Z", "+00:00"))
        for mode in ("cpu", "diagnostic"):
            observed = outputs[mode]
            normalized = normalize_artifact(case, observed["artifact"], observed["home"], workspace,
                observed["wall_begin"], observed["wall_end"], canonical)
            write_collection(place / (mode + "-artifact-equivalence.json"), normalized)
            observed["normalized_artifact"] = normalized["artifact"]
            records[mode]["artifact_equivalence"] = normalized
            if case == "status":
                relative_cache = ".floati-snapshots/v0/status-" + sha(b"default") + ".json"
                cache = json.loads((observed["home"] / relative_cache).read_bytes())
                projection = normalize_status_cache(cache, observed["artifact"], normalized)
                records[mode]["cache_equivalence"] = projection["cache"]
                records[mode]["cache_normalizations"] = projection["normalizations"]
            if case == "doctor":
                observed["native"]["findings_sha256"] = sha(json.dumps(
                    normalized["artifact"]["evidence"]["findings"], sort_keys=True).encode())
        if outputs["cpu"]["normalized_artifact"] != outputs["diagnostic"]["normalized_artifact"]:
            raise RuntimeError("diagnostics changed the complete normalized native artifact")
        if case == "status" and records["cpu"]["cache_equivalence"] != records["diagnostic"]["cache_equivalence"]:
            raise RuntimeError("diagnostics changed the complete validated status cache")
        for mode in ("cpu", "diagnostic"):
            if files(outputs[mode]["home"]) != records[mode]["inventory_after"]:
                raise RuntimeError("artifact normalization changed the measured fixture")
    delta_shapes = [{path: {"operation": row["operation"],
                           "mode": records[mode]["inventory_after"][path]["mode"]}
                     for path, row in records[mode]["native_delta"].items()}
                    for mode in ("cpu", "diagnostic")]
    if delta_shapes[0] != delta_shapes[1]:
        raise RuntimeError("diagnostics changed native effect paths, kinds or modes")
    if outputs["cpu"]["native"] != outputs["diagnostic"]["native"]:
        raise RuntimeError("diagnostics changed the native outcome")
    diagnostic = outputs["diagnostic"]["detail"]
    cpu = records["cpu"]["cpu_seconds"]
    read_bytes = sum(diagnostic["read_bytes_by_path"].values())
    hold = diagnostic["hold_families"]["ledger"]["maximum_seconds"]
    if case == "wait-idle":
        cpu_idle = outputs["cpu"]["detail"]["idle"]
        cpu = (cpu_idle["cpu_end_ns"] - cpu_idle["cpu_begin_ns"]) / 1e9
        read_bytes, hold = diagnostic["idle"]["bytes_read"], diagnostic["idle"]["ledger_hold_seconds"]
    receipt = dict(records, cpu_boundary=("delegated observed unchanged idle tick" if case == "wait-idle"
        else "whole native entry child including bootstrap, imports, native command and cleanup"))
    raw_sha = write_collection(place / "collection.json", receipt)
    return {"records": raw_seed.count(b"\n"), "bytes": len(raw_seed),
            "seed_sha256": sha(raw_seed), "clone_sha256": sha(raw_seed), "child_collected": True,
            "raw_sha256": raw_sha,
            "native": outputs["cpu"]["native"], "coverage": {"cpu": True, "bytes": True, "locks": True},
            "metrics": {"cpu_seconds": cpu, "bytes_read": read_bytes, "max_ledger_lock_seconds": hold}}


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {"smoke", "first-pass", "record", "check", "followup"}:
        raise ValueError("expected smoke|first-pass|record|check|followup OWNED_OUTPUT")
    mode, output_arg = sys.argv[1:]
    output = Path(output_arg)
    if (output.parent != TEMP or not output.name.startswith("bus-scale-census-2026-10-09-")
            or output.resolve(strict=True) != output):
        raise ValueError("census output must be the closed runner's exclusive bank")
    from tests import HOME_PIN_ROOT
    from tests.bus_scale_seeds import make_seed_pair
    from tests.bus_scale_workloads import scope_contract, require_observations
    from floati.cli import _parser
    if Path.home() != Path(HOME_PIN_ROOT):
        raise RuntimeError("private native HOME must be installed before producer startup")
    base = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-", dir=TEMP))
    record = {"mode": mode, "status": "incomplete", "matrix": {}, "scratch_removed": False}
    collected = True
    try:
        workspace = base / "workspace"
        from tests.banked_sha_fixture import banked_send_repository
        repository, _send_sha = banked_send_repository(base)
        repository.rename(workspace)
        workspace.chmod(0o700)
        (base / "seeds").mkdir(mode=0o700)
        seeds = make_seed_pair(base / "seeds")
        seeds["wait-exhaustion"] = seeds.pop("exhaustion")
        prepare(seeds, workspace)
        scope, owner = scope_contract(_parser()), owner_module()
        from tests.bus_scale_workloads import mcp_tool_bindings
        record["scope"] = scope
        record["mcp_tool_bindings"] = mcp_tool_bindings(_parser())
        expanded_seeds, entry = {}, native_entry_module()
        if mode == "followup":
            _run_followup(record, scope, seeds, workspace, base, owner)
        for family in (() if mode == "followup" else FAMILIES):
            seed = seeds[family]
            relative = seed["ledger_relative"]
            small, large = (seed[scale] / relative for scale in ("1", "10"))
            if not large.read_bytes().startswith(small.read_bytes()):
                raise RuntimeError("history is not an exact one-prefix growth")
            pairs = record["matrix"][family] = []
            for index in range(1 if mode == "smoke" else 2 if mode == "first-pass" else 5):
                pair = {"order": [1, 10] if index % 2 == 0 else [10, 1], "observations": {}}
                pairs.append(pair)
                for case in sorted(scope["cases"], key=lambda case: (case.startswith("wait-"), case)):
                    case_seed, case_workspace = workload_seed(
                        seeds, family, case, workspace, base, entry, expanded_seeds)
                    # Retry only the typed residual attention mismatch, once,
                    # inside the unchanged outer run deadline. Both scales
                    # keep the same balanced order and distinct raw banks.
                    for attempt in range(2):
                        row = pair["observations"][case] = dict(family=family, prefix_exact=True,
                            selected_work_equal=False, arms={})
                        raw_coordinates = {}
                        try:
                            for scale in pair["order"]:
                                place = base / "runs" / family / str(index) / case
                                if attempt:
                                    place = place / "category-retry-1"
                                place = place / str(scale)
                                place.mkdir(parents=True)
                                raw_coordinates[str(scale) + "x"] = place.relative_to(base).as_posix()
                                print("census " + family + " " + case + " pair=" + str(index)
                                      + " scale=" + str(scale) + " attempt=" + str(attempt), flush=True)
                                row["arms"][str(scale) + "x"] = (
                                    observation(owner, case, case_seed[str(scale)], case_seed["ledger_relative"],
                                                case_workspace, place) if case in ORIGINAL_CASES else
                                    expanded_adapter().expanded_observation(owner, case, case_seed, str(scale),
                                        case_workspace, place, ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py"))
                                if is_functional_refusal(row["arms"][str(scale) + "x"]):
                                    if mode == "smoke":
                                        raise ValueError("calibration requires completed native work; ruled refusal retained for classified census")
                                    bind_functional_refusal(case, family, str(scale) + "x",
                                        row["arms"][str(scale) + "x"], place,
                                        ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py")
                            from tests.bus_scale_workloads import _require_arm
                            one = _require_arm(case, row["arms"]["1x"])
                            refused = is_functional_refusal(row["arms"]["10x"])
                            ten = (validate_functional_refusal(case, family, "10x", row["arms"]["10x"])
                                   if refused else _require_arm(case, row["arms"]["10x"]))
                            if ten["records"] != 10 * one["records"] or ten["bytes"] <= one["bytes"]:
                                raise ValueError(case + " pair is not exact tenfold history growth")
                            if not refused:
                                require_attention_pair(case, one, ten)
                            # Complete arm syntax/coverage and exact categories
                            # precede the selected-work assertion and admission.
                            row["selected_work_equal"] = not refused
                            break
                        except AttentionCategoryMismatch as error:
                            if case not in ATTENTION_CASES:
                                raise
                            # An arm-local mismatch may have archived both modes
                            # without returning an arm. Preserve its coordinate
                            # and every returned arm; never fabricate metrics.
                            del pair["observations"][case]
                            record.setdefault("invalid_pairs", []).append(dict(
                                family=family, case=case, pair_index=index, attempt=attempt,
                                order=list(pair["order"]), code=error.code, detail=str(error),
                                raw_coordinates=raw_coordinates, observation=copy.deepcopy(row)))
                            if attempt == 1:
                                raise
                    if case == "wait-start":
                        # Whole-deadline receipts alone do not prove completed
                        # startup reconciliation. Bind both primary arms to
                        # independently observed idle children on the SAME
                        # family/scale seed, with their retained startup proof.
                        sibling = pair["observations"].get("wait-idle")
                        details_by_scale = {}
                        for scale in pair["order"]:
                            idle_place = base / "runs" / family / str(index) / "wait-idle" / str(scale)
                            details_by_scale[str(scale) + "x"] = {}
                            for arm_mode in ("cpu", "diagnostic"):
                                details_by_scale[str(scale) + "x"][arm_mode] = json.loads(
                                    (idle_place / arm_mode / "detail.json").read_text())
                        record.setdefault("phase_qualification", {}).setdefault(family, {})[str(index)] = (
                            qualify_wait_start(row, sibling, details_by_scale))
                selected_scope = copy.deepcopy(scope)
                for case_row in selected_scope["cases"].values():
                    case_row["family"] = family
                require_observations(selected_scope, pair["observations"], allow_ruled_refusals=True)
        if mode == "followup":
            pass
        elif mode == "smoke":
            record["status"] = "calibration_complete"
        else:
            record.update(validate_matrix(scope, record["matrix"], first_pass=mode == "first-pass"),
                status="first_pass_complete" if mode == "first-pass" else "instrument_complete")
            if record["functional_reds"]:
                record["status"] += "_with_functional_reds"
            if mode == "check":
                from tests.bus_scale_metrics import require_red_nonexpansion
                baseline = json.loads((ROOT / "tests/fixtures/bus-scale-census/known-red-f971841e.json").read_text())
                decode = lambda rows: {(r["subcommand"], r["metric"]): r.get("max_pair_ratio", r["ratio"])
                                       for r in rows}
                require_red_nonexpansion(decode(baseline["red_map"]), decode(record["red_map"]))
                record["nonexpansion"] = "numeric_pass_with_functional_reds" if record["functional_reds"] else "pass"
    except BaseException as exc:
        collected = not bool(getattr(exc, "uncollected_child", False))
        record.update(error=type(exc).__name__ + ": " + str(exc), child_collected=collected)
        if mode == "followup" and "selection" in record:
            status = getattr(exc, "collection_status", None)
            record["status"] = ("deadline" if isinstance(exc, KeyboardInterrupt) else status or "incomplete")
            record.update(followup_admission(record))
        raise
    finally:
        if collected:
            archive = output / "census-raw.tar.gz"
            with tarfile.open(archive, "x:gz") as stream:
                # Preserve complete seeds and raw native/diagnostic testimony;
                # working roots are reproducible copies with validated effects.
                if (base / "seeds").exists():
                    stream.add(base / "seeds", arcname="seeds", recursive=True)
                if (base / "expanded").exists():
                    stream.add(base / "expanded", arcname="expanded", recursive=True)
                for path in sorted((base / "runs").rglob("*")) if (base / "runs").exists() else ():
                    parts = path.relative_to(base / "runs").parts
                    if path.is_file() and ("scale-fleet" not in parts or
                                          len(parts) > 2 and parts[2] not in ORIGINAL_CASES):
                        stream.add(path, arcname=path.relative_to(base).as_posix(), recursive=False)
            record.update(raw_archive_sha256=sha(archive.read_bytes()), raw_archive_bytes=archive.stat().st_size)
            shutil.rmtree(base)
            record["scratch_removed"] = not base.exists()
        with (output / "census.json").open("x") as stream:
            stream.write(json.dumps(record, sort_keys=True, indent=2) + "\n")
        print(json.dumps({key: value for key, value in record.items() if key not in {"matrix", "scope", "comparisons", "native_metric_rows"}}, sort_keys=True), flush=True)
    return 0 if record["status"] in {"calibration_complete", "first_pass_complete", "instrument_complete", "followup_complete",
        "first_pass_complete_with_functional_reds", "instrument_complete_with_functional_reds"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
