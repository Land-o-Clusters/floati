"""Strict, scratch-only first-finding census for the current parser.

This deliberately stops at the first measured RED. Its pending population is
not a performance verdict. Native history is produced by send/present/ack,
never by editing JSONL frames. The existing exhaustive workload adapters remain
the population authority. Complete-history reads use the ruled per-output
byte judgment for reads and CPU; ledger-lock hold remains absolute.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.bus_scale_io import IOProbe
from tests.bus_scale_metrics import compare_pairs, compare_output_proportional_pairs
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import stable_fixture_sources

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py"
L7_DEFERRALS = ROOT / "tests/fixtures/bus-scale-census/l7-deferrals.v1.json"
METRICS = ("cpu_seconds", "read_bytes", "ledger_hold_seconds")
ORIGINAL_REMAINING = ("status", "doctor", "readiness-read", "receipt-append",
                      "log-history", "receipts-history", "wait-idle", "wait-start")
EXPANDED_SPLIT_CLOCK = frozenset({"seat-teardown", "wake-daemon-serve", "wake-wait"})
SPLIT_CLOCK = EXPANDED_SPLIT_CLOCK | {"wait-start", "wait-idle"}


class UncollectedChild(RuntimeError):
    """Retain every input while an owned child may still access it."""


class PreparationDeadline(RuntimeError):
    """The instrument exhausted preparation, independently of command cost."""
    code = "preparation_deadline"


class MeasuredDeadline(RuntimeError):
    code = "measured_deadline"


class ChildCommandDeadline(RuntimeError):
    code = "child_command_deadline"


class ChildCollectionDeadline(UncollectedChild):
    code = "child_collection_deadline"


class ChildInterpreterMismatch(RuntimeError):
    code = "census_interpreter_mismatch"

    def __init__(self, preparation_cache_tag, child_cache_tag):
        super().__init__("Native child and preparation have different interpreter cache tags")
        self.evidence = {"code": self.code, "instrument_finding": True,
                         "preparation_cache_tag": preparation_cache_tag, "child_cache_tag": child_cache_tag}


def population(parser):
    from tests.bus_scale_workloads import scope_contract
    return scope_contract(parser)


def verdict_population(cases, mode):
    """Only an explicit owner-ruling entry can defer a parser-derived case."""
    if mode not in {"l7", "all-cases"}:
        raise ValueError("unknown census verdict mode")
    deferred = []
    if mode == "l7":
        rows = json.loads(L7_DEFERRALS.read_text())
        if type(rows) is not list:
            raise ValueError("L7 deferrals must be a list of explicit owner rulings")
        seen = set()
        for row in rows:
            if (type(row) is not dict or set(row) != {"case", "row", "owner_ruling_date"}
                    or any(type(value) is not str or not value for value in row.values())
                    or row["case"] not in cases or row["case"] in seen
                    or not row["row"].isascii() or not row["row"].isupper()
                    or any(not (part.isalnum() and part) for part in row["row"].split("-"))
                    or date.fromisoformat(row["owner_ruling_date"]).isoformat() != row["owner_ruling_date"]):
                raise ValueError("invalid or duplicate L7 deferral owner ruling")
            seen.add(row["case"])
            deferred.append(dict(row, status="deferred", code="l7_owner_deferral"))
    judged = sorted(set(cases) - {row["case"] for row in deferred})
    return {"mode": mode, "judged": {"count": len(judged), "cases": judged},
            "deferred": sorted(deferred, key=lambda row: row["case"])}


# Closed claimants, each bound to its actual CLI help. Current projections
# (including status) do not become complete-history commands by tagging them.
_OUTPUT_PROPORTIONAL = {
    "log-history": ("log", "Read canonical ledgers. Without --replay, return the complete retained message history."),
    "receipts-history": ("receipts", "Read delivery, acknowledgment, consumption, and denial histories as distinct evidence."),
    "confluence-bundle": ("confluence bundle", "Materialize the complete retained message and receipt history permitted by the selected bundle schema as a grant-stamped receipts-read bundle to one explicit absolute path."),
}


def case_disposition(case, *, disposition=None):
    from floati.helptext import help_for
    expected = "output_proportional" if case in _OUTPUT_PROPORTIONAL else "ordinary"
    chosen = expected if disposition is None else disposition
    if chosen not in {"ordinary", "output_proportional"}:
        raise ValueError("unknown census disposition")
    if chosen == "output_proportional":
        if case not in _OUTPUT_PROPORTIONAL:
            raise ValueError("output_proportional requires a documented complete-history contract")
        command, body = _OUTPUT_PROPORTIONAL[case]
        help_text = help_for([*command.split(), "--help"])
        if help_text is None or body not in " ".join(help_text.split()):
            raise ValueError("complete-history parser help has changed")
        return {"class": chosen, "command": command,
                "citation": command + " --help", "help_text": body}
    return {"class": chosen}


def compare_case_pairs(case, pairs, *, disposition=None):
    judgment = case_disposition(case, disposition=disposition)
    compared = (compare_output_proportional_pairs(pairs)
                if judgment["class"] == "output_proportional" else compare_pairs(pairs))
    return dict(compared, disposition=judgment)


def _expanded_emitted_bytes(case, seeds, place, collection):
    """Count captured stdout plus the independently measured declared file.

    Ruling 2026-10-25 section 1.2 amends the stdout-only guard: the one
    caller-supplied output file contributes only with cited --out help and
    the census's own exact size/hash proof, never a command-reported count.
    """
    from floati.helptext import help_for
    judgment = case_disposition(case)
    help_text = help_for([*judgment.get("command", "").split(), "--help"])
    if help_text is None or "--out" not in help_text.split():
        raise ValueError("emitted file bytes require --out in the cited help")
    if (case != "confluence-bundle" or seeds["context"].get("case") != case
            or seeds["context"].get("publication_name") != "routine-bundle.json"):
        raise ValueError("emitted bundle bytes require the exact declared publication")
    home, place = Path(seeds["prepared_home"]), Path(place)
    if not home.is_absolute() or not place.is_absolute():
        raise ValueError("emitted bundle coordinates must be absolute")
    publication = home.parent / "routine-bundle.json"
    rows = collection["diagnostic"]["auxiliary_post_state"]
    matches = [(index, row) for index, row in enumerate(rows) if row.get("root") == str(publication)]
    if len(matches) != 1:
        raise ValueError("emitted bundle archive lacks its exact native publication")
    index, row = matches[0]
    inventory = row["inventory"]
    entries = inventory.get("entries", {})
    metadata = entries.get(".", {})
    if (inventory.get("kind") != "file" or set(entries) != {"."}
            or metadata.get("kind") != "file" or type(metadata.get("bytes")) is not int
            or metadata["bytes"] <= 0):
        raise ValueError("emitted bundle archive is not a measured publication file")
    archived, stdout = place / "diagnostic/auxiliary" / str(index), place / "diagnostic/stdout.txt"
    try:
        if any(path.is_symlink() or path.resolve(strict=True) != path or not path.is_file()
               or path.stat().st_nlink != 1
               for path in (archived, stdout)):
            raise ValueError("emitted bundle archive is aliased or nonordinary")
        raw, output = archived.read_bytes(), stdout.read_bytes()
    except (OSError, RuntimeError) as error:
        raise ValueError("emitted bundle archive cannot be read safely") from error
    if (len(raw) != metadata["bytes"] or hashlib.sha256(raw).hexdigest() != metadata.get("sha256")
            or hashlib.sha256(output).hexdigest() != collection["diagnostic"].get("stdout_sha256")
            or json.loads(output).get("evidence", {}).get("out") != str(publication)):
        raise ValueError("emitted bundle bytes differ from their exact native proof")
    return len(output) + len(raw)


def longest_ledger_hold(holds):
    return max((row["seconds"] for row in holds
                if row["path"].endswith(".jsonl.lock")
                or row["path"] == "locks/records.lock"), default=0)


def _load(path, name):
    from tests.bus_scale_test_profile import load_fixture_module
    return load_fixture_module(path, name)


def _usage():
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


def _fixture_child(kind, mode, home, output):
    from floati.jsonl import _locked_path
    probe = IOProbe(home) if mode == "diagnostic" else None
    before = _usage()
    with probe if probe else nullcontext():
        with _locked_path(home / "events.jsonl.lock", exclusive=True,
                          relative=Path("events.jsonl.lock")):
            with (home / "events.jsonl").open("rb") as stream:
                data = stream.read() if kind == "linear" else stream.read(131072)
            # Real cumulative CPU work under the actual product lock path.
            for _ in range(3200):
                hashlib.sha256(data).digest()
    cpu = _usage() - before
    if probe:
        probe.assert_complete()
    output.write_text(json.dumps({"cpu_seconds": cpu,
        "read_bytes": sum(probe.read_bytes_by_path.values()) if probe else 0,
        "ledger_hold_seconds": longest_ledger_hold(probe.holds) if probe else 0,
        "reads": dict(probe.read_bytes_by_path) if probe else {},
        "holds": probe.holds if probe else []}) + "\n")


def _environment(home):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("GIT_", "FLOATI_", "PYTHON", "LD_", "DYLD_", "XDG_"))}
    environment.update(HOME=str(home), PATH="/usr/bin:/bin",
                       PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    return environment


def _collect(argv, place, *, workspace, home, deadline):
    owner = _load(ROOT / "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py", "strict_census_owner")
    if time.monotonic() >= deadline:
        raise RuntimeError("census exceeded its overall wall budget")
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with (place / "stdout.txt").open("xb") as stdout, (place / "stderr.txt").open("xb") as stderr:
        child = owner.launch_owned_child(argv, cwd=workspace, env=_environment(home), stdout=stdout, stderr=stderr)
        collected = owner.collect_owned_child(child,
            deadline_seconds=min(70, deadline - time.monotonic()), collection_seconds=5)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
    receipt = dict(collected, pid=child.pid, cpu_seconds=cpu)
    (place / "collection.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
    if not collected["child_collected"]:
        raise UncollectedChild("uncollected census child; retain scratch at " + str(place))
    if collected["status"] != "complete" or collected["exit_code"] not in (0, 35):
        raise RuntimeError("native census child failed: " + json.dumps(receipt)
                           + " " + (place / "stderr.txt").read_text()[-1500:])
    return receipt


def calibrate(kind, *, collector=None, fixture_bytes=131072):
    if kind not in {"linear", "constant"}:
        raise ValueError("unknown planted command")
    base = Path(tempfile.mkdtemp(prefix="bus-scale-calibration-", dir=REAL_TEMP_ROOT)).resolve()
    collected = True
    try:
        pairs = []
        for index in range(5):
            pair = {"order": [1, 10] if index % 2 == 0 else [10, 1]}
            for scale in pair["order"]:
                values = {}
                for mode in ("cpu", "diagnostic"):
                    place = base / str(index) / str(scale) / mode
                    place.mkdir(parents=True, mode=0o700)
                    home = place / "fixture"
                    home.mkdir(mode=0o700)
                    (home / "events.jsonl").write_bytes(b"x" * (fixture_bytes * scale))
                    output = place / "metrics.json"
                    receipt = (collector or _collect)(["/usr/bin/python3", "-B", "-m", "tests.bus_scale_census",
                        "fixture-child", kind, mode, str(home), str(output)], place,
                        workspace=ROOT, home=place, deadline=time.monotonic() + 30)
                    detail = json.loads(output.read_text())
                    if detail.get("home_removed") is not True:
                        raise RuntimeError("planted command did not remove its owned test HOME")
                    if mode == "cpu":
                        values["cpu_seconds"] = receipt["cpu_seconds"]
                    else:
                        values.update({key: detail[key] for key in METRICS if key != "cpu_seconds"})
                pair[str(scale)] = values
            pairs.append(pair)
        result = compare_pairs(pairs)
        result["pairs"] = pairs
        return result
    except UncollectedChild:
        collected = False
        raise
    finally:
        if collected:
            shutil.rmtree(base)


def _inventory(home):
    return {p.relative_to(home).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in home.rglob("*") if p.is_file()}


def _write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")


def _warm_fixture(home, *, registry=True, cursor_preflights=None, event_cut=False):
    """Rebuild a copied stat-bound checkpoint outside the measured child."""
    before = _inventory(home)
    try:
        module = importlib.import_module("floati.send_prefix")
    except ModuleNotFoundError as error:
        if error.name != "floati.send_prefix":
            raise
        return {"status": "unavailable", "outside_child_measurement": True,
                "inventory_before": before, "inventory_after": before}
    from floati.root import FloatiRoot
    root = FloatiRoot.open_direct_home(home)
    ledgers = {p.relative_to(home).as_posix(): p.read_bytes() for p in home.rglob("*.jsonl")}
    mail_before = {p.relative_to(home).as_posix(): p.read_bytes()
        for p in home.rglob("*") if p.is_file()}
    result = module.warm(root)
    if ledgers != {p.relative_to(home).as_posix(): p.read_bytes() for p in home.rglob("*.jsonl")}:
        raise RuntimeError("send checkpoint preflight changed authoritative ledgers")
    mail_warm_proof = None
    from floati.cut_prefix import cache_name as cut_cache_name, warm as warm_cut
    cut_bank = home / cut_cache_name()
    needs_cut = event_cut or cut_bank.exists() or cut_bank.is_symlink()
    cut_result = warm_cut(root) if needs_cut else None
    if needs_cut and cut_result is None:
        raise RuntimeError("native cut checkpoint preflight could not certify the fixture")
    if result is not None:
        # The source warmer is deliberately source-only for ordinary callers.
        # This explicit census preflight certifies every mail observation bank
        # outside the measured child, through each bank's native full reader.
        from floati.mail_summary import warm as warm_mail
        warm_mail(root)
        from tests.fixtures.bus_scale_census.bus_scale_prefix_effects import verify_warmed_clone
        mail_warm_proof = verify_warmed_clone(home, mail_before, full_mail=True,
            cursor_preflights=cursor_preflights)
    receipt_results = {}
    exhaustion = home / "receipts/codex-wait-exhaustion"
    if exhaustion.exists():
        from floati.receipt_prefix import warm as warm_receipts
        for ledger in sorted(exhaustion.glob("*.jsonl")):
            receipt = warm_receipts(root, ledger.stem)
            if receipt is None:
                raise RuntimeError("native receipt checkpoint preflight could not certify the fixture")
            receipt_results[ledger.relative_to(home).as_posix()] = receipt
    registry_result = None
    if registry:
        from floati.registry_prefix import warm as warm_registry
        registry_result = warm_registry(root)
        if registry_result is None:
            raise RuntimeError("native registry checkpoint preflight could not certify the fixture")
    inbox_result = None
    if (home / ".inbox-prefix").exists():
        from floati.inbox_prefix import warm as warm_inbox
        inbox_result = warm_inbox(root)
        if inbox_result is None:
            raise RuntimeError("native inbox checkpoint preflight could not certify the copied fixture")
    consumed_results = {}
    consumed = home / "receipts/consumed"
    if consumed.exists():
        from floati.consumed_prefix import warm as warm_consumed
        nodes = {path.stem if path.is_file() else path.name for path in consumed.iterdir()
            if path.is_dir() or path.suffix == ".jsonl"}
        for node in sorted(nodes):
            consumed_results[node] = warm_consumed(root, node)
    after_ledgers = {p.relative_to(home).as_posix(): p.read_bytes() for p in home.rglob("*.jsonl")}
    if ledgers != after_ledgers:
        raise RuntimeError("send checkpoint preflight changed authoritative ledgers")
    if result is None:
        raise RuntimeError("native send checkpoint preflight could not certify the fixture")
    result = json.loads(json.dumps(result, allow_nan=False))
    return {"status": "complete", "outside_child_measurement": True,
            "native_result": result, "receipt_results": receipt_results,
            "cut_result": cut_result,
            "full_mail_warm_proof": mail_warm_proof,
            "registry_result": registry_result,
            "inbox_result": inbox_result,"consumed_results":consumed_results,
            "inventory_before": before, "inventory_after": _inventory(home)}


def _freeze_native_seed(source, destination, receipt_path, *, event_cut=False):
    """Compact through native validation, copy, then bind the copied inode."""
    from floati.events import EVENT_KINDS
    from floati.root import FloatiRoot
    from floati.jsonl import _epoch_writer_guard, _lock_beside, _locked_path
    try:
        prefix_module = importlib.import_module("floati.send_prefix")
    except ModuleNotFoundError as error:
        if error.name != "floati.send_prefix":
            raise
        compaction = {"status": "unavailable", "outside_child_measurement": True}
    else:
        root = FloatiRoot.open_direct_home(source)
        before = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*.jsonl")}
        node_bank = source / ".send-prefix/nodes"
        old_nodes = len(list(node_bank.iterdir())) if node_bank.exists() else 0
        path = root.resolve_relative("events.jsonl")
        lock, coordinate = _lock_beside(path, "events.jsonl")
        with _epoch_writer_guard(root, "events.jsonl", path=path), _locked_path(
                lock, exclusive=True, relative=coordinate):
            prefix = prefix_module.Prefix(root, EVENT_KINDS)
            prefix.rebuild()
            prefix.load()
        after = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*.jsonl")}
        if before != after:
            raise RuntimeError("native source cache compaction changed authoritative ledgers")
        compaction = {"status": "complete", "outside_child_measurement": True,
                      "native_identity": prefix.payload["identity"], "native_count": prefix.payload["count"],
                      "nodes_before": old_nodes, "nodes_after": len(list(node_bank.iterdir()))}
    shutil.copytree(source, destination)
    if _inventory(source) != _inventory(destination):
        raise RuntimeError("native seed copy differs before inode checkpoint rebuild")
    preflight = _warm_fixture(destination, event_cut=event_cut)
    _write_json(receipt_path, {"source": str(source), "seed": str(destination),
        "outside_child_measurement": True, "source_compaction": compaction,
        "copied_seed_preflight": preflight, "frozen_inventory": _inventory(destination)})


@stable_fixture_sources()
def native_seeds(base, *, small_count=32, deadline, workspace=None, preparation_check=None):
    from floati.cursor import SparseCursor
    from floati.events import EventLog
    from floati.registry import Registry
    from floati.root import FloatiRoot
    home = base / "author" / "scale-fleet"
    home.mkdir(parents=True, mode=0o700)
    root = FloatiRoot.open_direct_home(home, create=True)
    for node in ("scale-worker", "architect"):
        Registry(root).register(node, "codex")
    if workspace is not None:
        from floati.codex_wait_contract import write_workspace_map
        write_workspace_map(home, workspace, "scale-worker")
    log, cursor = EventLog(root), SparseCursor(root)
    seeds = {}
    for index in range(10 * small_count):
        if preparation_check is not None:
            preparation_check("native_seed")
        elif time.monotonic() >= deadline:
            raise RuntimeError("native seed construction exceeded census wall budget")
        envelope = log.send("scale-worker", "architect", "floati", "a" * 40,
            "note.md", "native acknowledged scale history",
            idempotency_key="history-%08d" % index)["message"]
        messages, _delivery = log.present("architect")
        if [row["id"] for row in messages] != [envelope["id"]]:
            raise RuntimeError("native history presentation changed its selected message")
        cursor.ack("architect", [envelope["id"]], acting_session_id="scale-history")
        if index + 1 in (small_count, 10 * small_count):
            scale = "1" if index + 1 == small_count else "10"
            seed = base / ("seed-" + scale) / "scale-fleet"
            _freeze_native_seed(home, seed, base / ("seed-freeze-" + scale + ".json"))
            seeds[scale] = seed
    for seed in seeds.values():
        if EventLog(FloatiRoot.open_direct_home(seed)).unacked_ids("architect"):
            raise RuntimeError("history left selected mail unacknowledged")
    return seeds


@stable_fixture_sources()
def native_exhaustion_seeds(base, *, small_count=32, deadline):
    """Author native 1x/10x exhaustion receipts, not unrelated event history."""
    from floati.codex_wait_contract import CodexWaitReceiptLedger
    from floati.jsonl import MAX_LEDGER_RECORDS, read_records
    from floati.registry import Registry
    from floati.root import FloatiRoot
    if (type(small_count) is not int or small_count < 1
            or 10 * small_count + 1 > MAX_LEDGER_RECORDS):
        raise ValueError("native exhaustion history must retain room for its fresh selected append")
    base=Path(base)
    home=base/"author"/"scale-fleet"
    home.mkdir(parents=True,mode=0o700)
    root=FloatiRoot.open_direct_home(home,create=True)
    for node in ("scale-worker","architect"):
        Registry(root).register(node,"codex")
    plane=CodexWaitReceiptLedger(root)
    relative="receipts/codex-wait-exhaustion/scale-worker.jsonl"
    seeds={}
    authored=[]
    for index in range(10 * small_count):
        if time.monotonic() >= deadline:
            raise RuntimeError("native exhaustion seed construction exceeded census wall budget")
        session="bus-scale-native-census-history-%08d" % index
        row=plane.record_exhaustion(node_id="scale-worker",
            session_digest=hashlib.sha256(session.encode("utf-8")).hexdigest(),
            waited_seconds=2,idempotency_key="strict-native-exhaustion-%08d" % index)
        authored.append(row)
        if index + 1 in (small_count,10 * small_count):
            scale="1" if index + 1 == small_count else "10"
            destination=base/("seed-"+scale)/"scale-fleet"
            # The inode-bound receipt proof belongs to the author, not its
            # immutable copy. Native preflight certifies each measured clone.
            cache_name = ".receipt-prefix-" + hashlib.sha256(b"scale-worker").hexdigest()
            shutil.copytree(home,destination,ignore=shutil.ignore_patterns(cache_name))
            frozen=FloatiRoot.open_direct_home(destination)
            actual=read_records(frozen,relative,allowed_kinds=set(plane._KINDS))
            if actual != authored or any(row["idempotency_key"] == "census-receipt" for row in actual):
                raise RuntimeError("native exhaustion seed differs from its genuine authored prefix")
            seeds[scale]=destination
    small,large=(seeds[scale]/relative for scale in ("1","10"))
    if not large.read_bytes().startswith(small.read_bytes()):
        raise RuntimeError("native exhaustion seeds lost their exact shared prefix")
    return {**seeds,"history":{"producer":"CodexWaitReceiptLedger.record_exhaustion",
        "family":"wait-exhaustion","ledger_relative":relative,"node_id":"scale-worker",
        "counts":{"1":small_count,"10":10 * small_count},"waited_seconds":2,
        "selected_idempotency_key_reserved":"census-receipt",
        "scope":"native scale-worker exhaustion receipts; two-actor registry fixed; no event/delivery/ACK padding"}}


def original_family_coverage(case, seeds):
    """Qualify the current parser descriptor against actual primary bytes.

    A result about one family's scaled input cannot qualify a different family.
    This admission runs before any native measurement child is launched.
    """
    from floati.cli import _parser
    from floati.events import EVENT_KINDS, validate_event_records
    from floati.framing import decode_frames
    from floati.jsonl import _decode_path_records
    from floati.records import WAKE_HOLD_KINDS
    from floati.registry import REGISTRY_KINDS
    from floati.root import FloatiRoot
    if case not in (*ORIGINAL_REMAINING,"send"):
        raise ValueError("original family coverage requires an original native case")
    descriptor=population(_parser())["cases"][case]
    family=descriptor["family"]
    coordinates={
        "events":("events.jsonl",EVENT_KINDS),
        "registry":("registry/entries.jsonl",REGISTRY_KINDS),
        "wait-exhaustion":("receipts/codex-wait-exhaustion/scale-worker.jsonl",{"codex_wait_exhaustion_receipt"}),
        "deliveries":("receipts/deliveries/architect.jsonl",WAKE_HOLD_KINDS),
    }
    if family not in coordinates or set(seeds) != {"1","10"}:
        raise ValueError("native family coverage has an unsupported declared family or scale population")
    relative,kinds=coordinates[family]
    raw={}
    counts={}
    for scale in ("1","10"):
        root=FloatiRoot.open_direct_home(seeds[scale])
        path=root.resolve_relative(relative)
        if not path.is_file():
            raise RuntimeError("declared "+family+" family has no native scaled ledger: "+relative)
        raw[scale]=path.read_bytes()
        known=_decode_path_records(path,root.tenant_id,frozenset(kinds),raw[scale],
            unrecognized={} if family=="events" else None)
        if family=="events":
            validate_event_records(known)
        counts[scale]=len(decode_frames(raw[scale]))
    if (counts["1"] < 1 or counts["10"] != 10 * counts["1"]
            or not raw["10"].startswith(raw["1"])):
        raise RuntimeError("declared "+family+" family lacks an exact nonempty native 1x/10x shared prefix")
    return {"case":case,"command":descriptor["command"],"declared_family":family,
        "ledger_relative":relative,"physical_records":counts,
        "bytes":{scale:len(value) for scale,value in raw.items()},
        "sha256":{scale:hashlib.sha256(value).hexdigest() for scale,value in raw.items()},
        "shared_prefix_exact":True,"outside_child_measurement":True}


def _native_sample(case, seed, workspace, place, deadline):
    from floati.events import EventLog
    from floati.root import FloatiRoot
    producer = _load(ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py", "strict_effect_producer")
    values, evidence = {}, {}
    prior_events = EventLog(FloatiRoot.open_direct_home(seed)).event_records()
    for mode in ("cpu", "diagnostic"):
        child_place = place / mode
        child_place.mkdir(parents=True, mode=0o700)
        fixture = child_place / "scale-fleet"
        shutil.copytree(seed, fixture)
        before = _inventory(seed)
        if _inventory(fixture) != before:
            raise RuntimeError("measured clone differs from its frozen seed")
        preflight = _warm_fixture(fixture)
        _write_json(child_place / "preflight.json", preflight)
        output = child_place / "detail.json"
        collection = _collect(["/usr/bin/python3", "-B", str(ENTRY), mode, case,
            str(fixture), str(workspace), str(output), "owned-census-v1"], child_place,
            workspace=workspace, home=child_place, deadline=deadline)
        detail = json.loads(output.read_text())
        artifact = json.loads((child_place / "stdout.txt").read_text())
        native = producer.effect_witness(case, fixture, {"event_rows": prior_events},
            (child_place / "stdout.txt").read_text(), (child_place / "stderr.txt").read_text(),
            detail, workspace=workspace)
        if mode == "cpu":
            values["cpu_seconds"] = collection["cpu_seconds"]
        else:
            if detail["coverage"] != {"bytes": True, "locks": True}:
                raise RuntimeError("native diagnostic coverage incomplete")
            holds = [row for family in detail["hold_families"].values() for row in family["intervals"]]
            values.update(read_bytes=sum(detail["read_bytes_by_path"].values()),
                          ledger_hold_seconds=longest_ledger_hold(holds))
        evidence[mode] = {"collection": collection, "artifact": artifact,
                          "detail": detail, "seed_inventory": before, "effect": native,
                          "checkpoint_preflight": preflight,
                          "streams": {name: hashlib.sha256((child_place / name).read_bytes()).hexdigest()
                                      for name in ("stdout.txt", "stderr.txt", "detail.json")}}
        if _inventory(seed) != before:
            raise RuntimeError("native command changed its frozen seed")
    if evidence["cpu"]["effect"] != evidence["diagnostic"]["effect"]:
        raise RuntimeError("CPU and diagnostic children performed different native work")
    return values, evidence


def _source_witness():
    paths = sorted(set(ROOT.glob("floati/**/*.py")) | set(ROOT.glob("schemas/**/*.json"))
        | set(ROOT.glob("tests/bus_scale*.py")) | set(ROOT.glob("tests/fixtures/bus_scale_census/*.py"))

        | set(ROOT.glob("tests/fixtures/bus-scale-census/*.json")) | {
        Path(__file__), ROOT / "tests/harbor_bus_scale_instrument.py", ROOT / "tests/bus_scale_io.py",
        ROOT / "tests/bus_scale_metrics.py", ROOT / "tests/bus_scale_commands.py",
        ROOT / "tests/fixtures/bus_scale_census/bus_scale_prefix_effects.py",
        ROOT / "tests/__init__.py", ROOT / "tests/temp_roots.py", ROOT / "tests/banked_sha_fixture.py",
        ROOT / "tests/bus_scale_workloads.py", ENTRY,
        ROOT / "scripts/bus-scale-census.py",
        ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
        ROOT / "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py", ROOT / "bundle-manifest.v0.json"})
    return {"commit": subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "files": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}


def _case_report_summary(case, case_bank, evidence, *, native, source_commit, comparison):
    """Reference exact retained case testimony without copying its raw trees."""
    fields = ("native_complete", "scratch_removed", "source_unchanged", "phase",
              "pairs", "history", "seed_ledgers", "wall_seconds", "load1", "raw_archive_sha256")
    summary = {field: evidence[field] for field in fields if field in evidence}
    if case in EXPANDED_SPLIT_CLOCK:
        summary.update({key: evidence[key] for key in ("clock_accounting", "pair_clocks") if key in evidence})
    summary.update(case=case, comparison=comparison, native_measurement=native,
                   source_commit=evidence.get("source_before", {}).get("commit", source_commit))
    path = case_bank / "case.json"
    if path.is_file() and not path.is_symlink():
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        summary["case_evidence"] = {"path": str(path), "sha256": digest.hexdigest()}
    elif native:
        raise RuntimeError("complete native case lacks its retained case.json testimony")
    else:
        summary["case_evidence"] = None
    return summary


def remaining_census(bank, *, cases=None, case_runner=None, prior_dispositions=None,
                     verdict_mode="all-cases"):
    """Attempt every pending scenario; only complete native pairs are measured.

    This is an explicit continuation, separate from the send-first stopping
    rule. Independent setup/instrument errors remain pending and do not erase
    evidence from later cases. Injected runners are unit controls, never native
    measurement authority: the default runner owns preparation and validation.
    """
    from floati.cli import _parser
    bank = Path(bank)
    if (not bank.is_absolute() or bank.resolve(strict=True) != bank
            or Path(REAL_TEMP_ROOT) not in bank.parents or not bank.is_dir()
            or bank.stat().st_uid != os.getuid() or bank.stat().st_mode & 0o077):
        raise ValueError("remaining census bank must be canonical, private and owned temporary storage")
    if any(bank.iterdir()):
        raise ValueError("remaining census bank must be empty")
    scope = population(_parser())
    verdict = verdict_population(set(scope["cases"]) - {"send"}, verdict_mode)
    remaining = verdict["judged"]["cases"]
    population_order = [case for case in ORIGINAL_REMAINING if case in remaining]
    population_order += sorted(set(remaining) - set(population_order))
    if cases is not None and (type(cases) not in (list, tuple) or not cases
            or any(type(case) is not str or case not in remaining for case in cases)
            or len(cases) != len(set(cases))):
        raise ValueError("cases must select distinct known scenarios from the remaining parser population")
    order = list(population_order) if cases is None else [case for case in population_order if case in cases]
    unselected = [case for case in population_order if case not in order]
    if prior_dispositions is not None:
        if type(prior_dispositions) is not dict or set(prior_dispositions) != set(unselected):
            raise ValueError("prior dispositions must account for every explicitly unselected scenario")
        if any(type(row) is not dict or not isinstance(row.get("reason"), str)
               or not row["reason"] or not isinstance(row.get("evidence"), str) or not row["evidence"]
               for row in prior_dispositions.values()):
            raise ValueError("prior dispositions require an explicit reason and evidence coordinate")
        prior_dispositions = json.loads(json.dumps(prior_dispositions, allow_nan=False))
    runner = _remaining_case if case_runner is None else case_runner
    result = {"status": "incomplete", "scope": scope, "verdict": verdict,
              "attempted": [], "measured": [],
              "pending": list(population_order), "selected": list(order), "selected_pending": list(order),
              "not_attempted": list(population_order), "errors": {}, "findings": [], "cases": {},
              "selection": {"requested": None if cases is None else list(cases), "unselected": unselected,
                            "prior_dispositions": prior_dispositions or {}},
              "native_measurement": case_runner is None,
              "coverage": "native acknowledged event history; case-specific receipt scope is retained per case"}
    result["source_before"] = _source_witness()
    started = time.monotonic()
    unsafe_child = False
    try:
        for case in order:
            if unsafe_child:
                result["errors"][case] = {"type": "UncollectedChild",
                    "detail": "not launched while an earlier native child remains uncollected"}
                continue
            result["attempted"].append(case)
            result["not_attempted"].remove(case)
            case_bank = bank / case
            case_bank.mkdir(mode=0o700)
            try:
                evidence = runner(case, case_bank)
                if (type(evidence) is not dict or evidence.get("native_complete") is not True
                        or evidence.get("scratch_removed") is not True
                        or evidence.get("source_unchanged") is not True):
                    raise RuntimeError("case lacks complete native measurement, cleanup or source proof")
                comparison = compare_case_pairs(case, evidence["pairs"])
                result["cases"][case] = _case_report_summary(case, case_bank, evidence,
                    native=case_runner is None, source_commit=result["source_before"]["commit"],
                    comparison=comparison)
                del evidence
                result["measured"].append(case)
                result["pending"].remove(case)
                result["selected_pending"].remove(case)
                for metric, row in comparison["metrics"].items():
                    if row["classification"] != "pass":
                        result["findings"].append(dict(command=case, metric=metric, **row))
            except (Exception, SystemExit) as error:
                result["errors"][case] = {"type": type(error).__name__, "detail": str(error),
                                         "evidence_bank": str(case_bank)}
                if isinstance(error, ChildInterpreterMismatch):
                    result["errors"][case].update(error.evidence)
                if (case_bank / "case.json").is_file():
                    partial = json.loads((case_bank / "case.json").read_text())
                    result["errors"][case]["phase"] = partial.get("phase", "unknown")
                    if case in EXPANDED_SPLIT_CLOCK:
                        result["errors"][case].update(partial.get("error", {}))
                        for key in ("clock_accounting", "pair_clocks"):
                            if key in partial:
                                result["errors"][case][key] = partial[key]
                unsafe_child = isinstance(error, UncollectedChild) or bool(
                    getattr(error, "uncollected_child", False))
            _write_json(bank / "result.json", result)
            if case_runner is None:
                print("BUS_SCALE_CASE " + case + " " + case_disposition(case)["class"] + " "
                      + ("measured" if case in result["measured"] else "pending"), flush=True)
        if not result["pending"]:
            result["status"] = "red" if result["findings"] else "green"
        return result
    finally:
        result["wall_seconds"] = time.monotonic() - started
        result["source_after"] = _source_witness()
        result["source_unchanged"] = result["source_before"] == result["source_after"]
        result["selection_complete"] = not result["selected_pending"]
        if not result["source_unchanged"] or case_runner is not None:
            result["status"] = "incomplete"
        _write_json(bank / "result.json", result)


def _native_child(entry_args, cleanup_path):
    """Run the unchanged native entry and collect this child's pinned HOME."""
    from tests import HOME_PIN_ROOT
    home = Path(HOME_PIN_ROOT)
    original = sys.argv
    previous_environment = dict(os.environ)
    # Importing the test package pins HOME and carries its optional library
    # path. Reapply the sealed native launch shape after that import, before
    # the real entry installs its unchanged subprocess/I/O policy.
    native_environment = _environment(home)
    try:
        os.environ.clear()
        os.environ.update(native_environment)
        sys.argv = [str(ENTRY), *entry_args]
        runpy.run_path(str(ENTRY), run_name="__main__")
    finally:
        sys.argv = original
        try:
            if (home.parent != Path(REAL_TEMP_ROOT) or not home.name.startswith("suite-home-")
                    or home.is_symlink() or home.stat().st_uid != os.getuid()):
                raise RuntimeError("native child test HOME identity changed")
            shutil.rmtree(home)
            _write_json(Path(cleanup_path), {"native_home": str(home), "home_removed": not home.exists(),
                                            "cache_tag": sys.implementation.cache_tag})
        finally:
            os.environ.clear()
            os.environ.update(previous_environment)


def _native_argv(entry_args, cleanup):
    # Preparation and observation must agree on the interpreter cache tag
    # that the product seals into the reader revision.
    return [sys.executable, "-B", str(Path(__file__).resolve()), "native-child",
            *map(str, entry_args), str(cleanup)]


class _ContinuationOwner:
    """Adapter for existing observers, with child HOME cleanup and receipts."""
    def __init__(self, deadline, *, measured_budget=None, started=None, case=None):
        self.deadline = deadline
        self.started = time.monotonic() if started is None else started
        self.measured_budget = deadline - self.started if measured_budget is None else measured_budget
        self.measured_seconds = 0.0
        self.preparation_budget = 1710.0
        self.preparation_phases = {}
        self._phase = "native_setup"
        self._phase_started = self.started
        self._children = {}
        self.case = case
        self.owner = _load(ROOT / "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py",
                           "remaining_census_owner")
        self.homes = {}
        self.cursor_preflights = ()

    @property
    def _split_clock(self):
        return self.case in SPLIT_CLOCK

    @property
    def preparation_seconds(self):
        now = time.monotonic()
        live = sum(now - started for started in self._children.values())
        return max(0.0, now - self.started - self.measured_seconds - live)

    def preparation_phase(self, phase, *, check=True):
        now = time.monotonic()
        if not self._children:
            self.preparation_phases[self._phase] = self.preparation_phases.get(self._phase, 0) + now - self._phase_started
        self._phase, self._phase_started = phase, now
        if check and self._split_clock and self.preparation_seconds > self.preparation_budget:
            raise PreparationDeadline("preparation_deadline: " + json.dumps(self.accounting(), sort_keys=True))

    def accounting(self):
        now = time.monotonic()
        measured = self.measured_seconds + sum(now - started for started in self._children.values())
        phases = dict(self.preparation_phases)
        if not self._children:
            phases[self._phase] = phases.get(self._phase, 0) + now - self._phase_started
        return {"measured_seconds": measured,
            "measured_budget_seconds": self.measured_budget,
            "preparation_seconds": max(0.0, now - self.started - measured),
            "preparation_cap_seconds": self.preparation_budget,
            "preparation_phases": phases}

    def launch_owned_child(self, argv, **kwargs):
        if argv[:3] != ["/usr/bin/python3", "-B", str(ENTRY)]:
            raise RuntimeError("continuation observer requested an unexpected child")
        self.case = argv[4]
        self.preparation_phase("child_launch")
        if self._split_clock:
            if self.measured_seconds >= self.measured_budget:
                error = MeasuredDeadline if self.case in EXPANDED_SPLIT_CLOCK else RuntimeError
                raise error("native case exceeded its measured child budget")
        elif time.monotonic() >= self.deadline:
            raise RuntimeError("native case exceeded its bounded wall budget")
        output = Path(kwargs["stdout"].name)
        cleanup = output.with_name(output.name + ".home-" + str(time.monotonic_ns()) + ".json")
        started = time.monotonic()
        child = self.owner.launch_owned_child(_native_argv(argv[3:], cleanup), **kwargs)
        self._children[child.pid] = started
        self.homes[child.pid] = cleanup
        return child

    def collect_owned_child(self, child, **kwargs):
        command_limit = kwargs["deadline_seconds"]
        remaining = (self.measured_budget - self.measured_seconds
            - (time.monotonic() - self._children[child.pid]) if self._split_clock
            else self.deadline - time.monotonic())
        kwargs["deadline_seconds"] = max(0.001, min(kwargs["deadline_seconds"],
                                                   remaining))
        receipt = self.owner.collect_owned_child(child, **kwargs)
        if receipt.get("child_collected") is not True:
            if self.case in EXPANDED_SPLIT_CLOCK:
                raise ChildCollectionDeadline("native child remains uncollected after the collection limit: "
                    + json.dumps(dict(pid=child.pid, collection_seconds=kwargs["collection_seconds"],
                                      receipt=receipt), sort_keys=True))
            raise UncollectedChild("native continuation child remains live: " + str(child.pid))
        elapsed = time.monotonic() - self._children.pop(child.pid)
        self.measured_seconds += elapsed
        self._phase, self._phase_started = "collection_validation", time.monotonic()
        cleanup = self.homes.pop(child.pid)
        if self.case in EXPANDED_SPLIT_CLOCK:
            details = dict(pid=child.pid, child_seconds=elapsed, command_limit_seconds=command_limit,
                           remaining_measured_seconds=remaining, home_cleanup_receipt=str(cleanup), receipt=receipt)
            if receipt.get("status") == "deadline":
                deadline = MeasuredDeadline if remaining <= command_limit else ChildCommandDeadline
                raise deadline("native child reached its deadline: " + json.dumps(details, sort_keys=True))
            if self.measured_seconds > self.measured_budget:
                raise MeasuredDeadline("native case exceeded its measured child budget: " + json.dumps(details, sort_keys=True))
        if not cleanup.exists():
            raise RuntimeError("collected native child omitted its HOME cleanup receipt")
        home = json.loads(cleanup.read_text())
        if home.get("home_removed") is not True or Path(home["native_home"]).exists():
            raise RuntimeError("native child did not remove its pinned HOME")
        preparation_cache_tag = sys.implementation.cache_tag
        child_cache_tag = home.get("cache_tag")
        if not isinstance(child_cache_tag, str) or child_cache_tag != preparation_cache_tag:
            raise ChildInterpreterMismatch(preparation_cache_tag, child_cache_tag)
        return dict(receipt, home_cleanup=home, home_cleanup_receipt=str(cleanup),
                    preparation_cache_tag=preparation_cache_tag, wall_seconds=elapsed)


def _prepare_wait_seeds(seeds, workspace):
    from floati.root import FloatiRoot
    from floati.codex_wait_contract import (write_workspace_map, resolve_participant,
        CodexWaitConsentLedger, CodexWaitSessionLedger)
    for home in seeds.values():
        root = FloatiRoot.open_direct_home(home)
        write_workspace_map(home, workspace, "scale-worker")
        participant = resolve_participant(home, workspace, _strict=True)
        consent = CodexWaitConsentLedger(root).arm(participant.binding,
            hook_timeout_seconds=10, wait_deadline_seconds=8, idempotency_key="strict-native-wait-consent")
        CodexWaitSessionLedger(root).arm(participant.binding, consent,
            "bus-scale-native-census", idempotency_key="strict-native-wait-session")


def _prepare_wait_clone(home):
    """Read the real waiter's source cut before capturing its measured state."""
    from floati.cursor import SparseCursor
    from floati.root import FloatiRoot
    from floati.wake_hold import WakeHoldController
    root = FloatiRoot.open_direct_home(home)
    before = _inventory(home)
    cursor = SparseCursor(root)
    events, _event_prefixes, _deliveries, _delivery_prefixes, _acks, _ack_prefixes = (
        WakeHoldController(root)._read("scale-worker", None))
    cursor.state_for("scale-worker", events)
    after = _inventory(home)
    allowed = {"events.jsonl.lock",
               str(cursor._delivery_relative_path_for("scale-worker")) + ".lock",
               str(cursor._relative_path_for("scale-worker")) + ".lock"}
    if any(after.get(path) != digest for path, digest in before.items()):
        raise RuntimeError("native wait source preparation changed a retained payload")
    added = set(after) - set(before)
    if not added <= allowed or any((home / path).read_bytes() != b"" for path in added):
        raise RuntimeError("native wait source preparation created an unexpected payload")
    return {"outside_child_measurement": True, "native_reader": "WakeHoldController._read; SparseCursor.state_for",
            "created_empty_source_locks": sorted(added), "inventory_before": before, "inventory_after": after}


def _expanded_family_contract(case, root, context):
    from floati.cli import _parser
    from floati.cursor import SparseCursor
    from tests.bus_scale_case_seeds import FAMILIES
    declaration = population(_parser())["cases"].get(case)
    if not isinstance(declaration, dict) or declaration.get("family") not in FAMILIES:
        raise RuntimeError("expanded case lacks its parser-bound primary family")
    family = declaration["family"]
    if family not in {"events", "registry", "deliveries"}:
        raise RuntimeError("expanded native producer does not implement declared family " + family)
    primary, kinds = FAMILIES[family]
    recipient = "scale-history"
    if family == "deliveries":
        recipient = context.get("actor")
        if not isinstance(recipient, str) or not recipient:
            raise RuntimeError("delivery history lacks its native preface actor")
        from floati.registry import Registry
        Registry(root).require_active(recipient)
        primary = SparseCursor(root)._delivery_relative_path_for(recipient).as_posix()
        if primary != FAMILIES[family][0]:
            raise RuntimeError("native actor delivery path differs from declared primary family")
        selected = context.get("message")
        if not isinstance(selected, dict) or selected.get("recipient") != recipient:
            raise RuntimeError("native delivery preface lacks its exact selected recipient")
    per_item = 2 if family == "events" or (
        family == "registry" and case in {"chart", "node-boot", "node-explain", "node-teardown"}) else 1
    return {"family": family, "primary": primary, "allowed_kinds": sorted(kinds),
            "frames_per_item": per_item, "recipient": recipient}


def _qualify_expanded_family(case, pair):
    """Read native EOFs and selected state before any measured child starts."""
    from floati.cursor import SparseCursor
    from floati.events import EventLog
    from floati.jsonl import read_records
    from floati.registry import Registry
    from floati.root import FloatiRoot
    first = FloatiRoot.open_direct_home(pair["1"])
    contract = _expanded_family_contract(case, first, pair["context"])
    history = pair["history"]
    if (pair.get("family") != contract["family"] or history.get("family") != contract["family"]
            or pair["ledger_relative"] != contract["primary"]
            or history.get("primary") != contract["primary"]
            or history.get("frames_per_item") != contract["frames_per_item"]):
        raise RuntimeError("native fixture differs from its declared primary family")
    counts = history["counts"]
    if (set(counts) != {"1", "10"} or type(counts["1"]) is not int or counts["1"] < 1
            or counts["10"] != 10 * counts["1"]):
        raise RuntimeError("native family lacks exact tenfold history items")
    primary_bytes = {}
    proof = dict(contract, scales={}, selected_work_equal=True, prefix_exact=True)
    for scale in ("1", "10"):
        home = pair[scale]
        root = FloatiRoot.open_direct_home(home)
        raw = (home / contract["primary"]).read_bytes()
        prefix = raw[:history["preface_bytes"]]
        if (len(prefix.splitlines()) != history["preface_frames"]
                or hashlib.sha256(prefix).hexdigest() != history["preface_sha256"]):
            raise RuntimeError("native primary preface changed before measurement")
        rows = read_records(root, contract["primary"], allowed_kinds=set(contract["allowed_kinds"]))
        if len(rows) != len(raw.splitlines()):
            raise RuntimeError("native physical frames differ from validated family records")
        delta = len(rows) - history["preface_frames"]
        if delta != counts[scale] * contract["frames_per_item"]:
            raise RuntimeError("native primary-family EOF has the wrong history delta")
        if case == "chart":
            live = history.get("live_members")
            if (not isinstance(live, list) or live != sorted(set(live))
                    or list(Registry(root).active_node_ids()) != live):
                raise RuntimeError("native chart history changed live membership")
        log, cursor = EventLog(root), SparseCursor(root)
        events = log.strict_event_records()
        event_path = home / "events.jsonl"
        event_raw = event_path.read_bytes() if event_path.exists() else b""
        if hashlib.sha256(event_raw[:history["preface_events_bytes"]]).hexdigest() != history["preface_events_sha256"]:
            raise RuntimeError("native selected event preface changed")
        for node, before in history["selected_before"].items():
            if Registry(root).require_active(node) != before["registry"]:
                raise RuntimeError("native history changed a selected registry member")
            if log.unacked_ids(node) != before["unacked"]:
                raise RuntimeError("native history changed selected pending work")
            _presented, acknowledged = cursor.state_for(node, [row for row in events if row["kind"] == "message_envelope"])
            if sorted(set(acknowledged) & set(history["preface_message_ids"])) != before["acknowledged"]:
                raise RuntimeError("native history acknowledged selected preface work")
        if contract["family"] != "registry":
            messages = [row for row in events if row["kind"] == "message_envelope"
                        and row["sender"] == "scale-history" and row["recipient"] == contract["recipient"]
                        and str(row.get("idempotency_key", "")).startswith("strict-history-")]
            if len(messages) != counts[scale]:
                raise RuntimeError("native family lost an authored history message")
            envelopes = [row for row in events if row["kind"] == "message_envelope"]
            _presented, ackable = cursor.validate_deliveries(contract["recipient"], envelopes)
            _presented, acknowledged = cursor.state_for(contract["recipient"], envelopes)
            ids = {row["id"] for row in messages}
            if not ids <= set(ackable) or not ids <= set(acknowledged):
                raise RuntimeError("native history is not fully delivered and acknowledged")
        primary_bytes[scale] = raw
        proof["scales"][scale] = {"items": counts[scale], "preface_frames": history["preface_frames"],
            "history_frames": delta, "eof_frames": len(rows), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}
    if not primary_bytes["10"].startswith(primary_bytes["1"]):
        raise RuntimeError("native primary-family histories lost their exact shared prefix")
    return proof


def _chart_registry_transition(root):
    """Append one ruled retire/re-register fixture transition, never a new member."""
    from floati.ids import uuid7_hex
    from floati.jsonl import transact_records
    from floati.registry import REGISTRY_KINDS, Registry, utc_now
    retired = Registry(root).retire("scale-history")
    # The public register verb deliberately refuses reused identities. This
    # fixture's ruled transition uses its native registration schema through
    # the ledger transaction, with an exact retired-predecessor guard.
    active = dict(retired, id="registry-" + uuid7_hex(), timestamp=utc_now(), state="active")

    def restore(rows):
        latest = next((row for row in reversed(rows)
            if row.get("kind") == "registry_entry" and row.get("node_id") == "scale-history"), None)
        if latest != retired or latest.get("state") != "retired":
            raise RuntimeError("chart fixture re-registration lost its retired predecessor")
        return active, [active]

    transact_records(root, "registry/entries.jsonl", restore, allowed_kinds=REGISTRY_KINDS)


@stable_fixture_sources()
def _expanded_native_seeds(case, scratch, workspace, entry, deadline, *, small_count=32, preparation_check=None):
    """Native preface and real unselected history; never publish padded frames."""
    from floati.cursor import SparseCursor
    from floati.events import EventLog
    from floati.registry import Registry
    from floati.root import FloatiRoot
    if case == "wake-callback":
        root = FloatiRoot.open(scratch / "callback-root", "scale-fleet")
        prepared = root.tenant_home
        prepared.chmod(0o700)
    else:
        prepared = scratch / "prepared" / "scale-fleet"
        prepared.parent.mkdir(mode=0o700)
        prepared.mkdir(mode=0o700)
        root = FloatiRoot.open_direct_home(prepared, create=True)
    for node in ("scale-worker", "architect"):
        Registry(root).register(node, "codex")
    bank = scratch / "native-preparation"
    bank.mkdir(mode=0o700)
    context = entry.routine_module(case).prepare(case, prepared, workspace, bank)
    if type(context) is not dict:
        raise RuntimeError("native scenario preface did not retain JSON context")
    # The inherited observer compares the exact retained context bytes.
    (prepared.parent / "case-context.json").write_text(
        json.dumps(context, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False, allow_nan=False) + "\n")
    log, cursor = EventLog(root), SparseCursor(root)
    preface_events = log.strict_event_records()
    preface_ids = {row["id"] for row in preface_events if row["kind"] == "message_envelope"}
    selected_before = {}
    for node in Registry(root).active_node_ids():
        _presented, acknowledged = cursor.state_for(node,
            [row for row in preface_events if row["kind"] == "message_envelope"])
        selected_before[node] = {"registry": Registry(root).require_active(node),
            "unacked": log.unacked_ids(node), "acknowledged": sorted(set(acknowledged) & preface_ids)}
    Registry(root).register("scale-history", "codex")
    needs_history_roles = case in {"node-boot", "node-explain", "node-teardown"}
    if needs_history_roles:
        from tests.bus_scale_case_seeds import _history_role
        _history_role(root)
    contract = _expanded_family_contract(case, root, context)
    family, recipient = contract["family"], contract["recipient"]
    primary = root.resolve_relative(contract["primary"])
    preface_raw = primary.read_bytes() if primary.exists() else b""
    event_path = prepared / "events.jsonl"
    event_raw = event_path.read_bytes() if event_path.exists() else b""
    history = {"producer": "native registry registration" if family == "registry" else "native send/present/ack",
        "family": family, "primary": contract["primary"], "frames_per_item": contract["frames_per_item"],
        "sender": "scale-history", "recipient": recipient,
        "preface_frames": len(preface_raw.splitlines()), "preface_bytes": len(preface_raw),
        "preface_sha256": hashlib.sha256(preface_raw).hexdigest(),
        "preface_event_frames": len(event_raw.splitlines()), "preface_events_bytes": len(event_raw),
        "preface_events_sha256": hashlib.sha256(event_raw).hexdigest(),
        "preface_message_ids": sorted(preface_ids), "selected_before": selected_before,
        "counts": {"1": small_count, "10": 10 * small_count},
        "scope": "parser-declared primary-family physical history; original selected pending work and ACKs preserved"}
    if family == "events":
        history.update(producer="native send/batched-present/batched-ack",
            presentation_batches={"1": [small_count], "10": [small_count, 9 * small_count]},
            receipt_frames={"1": 1, "10": 2})
    elif family == "deliveries":
        history.update(presentation_batches={"1": [1] * small_count, "10": [1] * (10 * small_count)},
                       receipt_frames={"1": small_count, "10": 10 * small_count})
    if case == "chart":
        history.update(producer="native registry retirement and validated re-registration transaction",
            live_members=list(Registry(root).active_node_ids()),
            scope="registry history through retire/re-register transitions; live membership and original selected work preserved")
    result = {"context": context, "prepared_home": prepared, "preparation_bank": bank,
              "family": family, "ledger_relative": contract["primary"], "history": history}
    sha = entry.workspace_send_sha(workspace) if family != "registry" else None
    batch_ids = []
    for index in range(10 * small_count):
        if preparation_check is not None:
            preparation_check("native_seed")
        elif time.monotonic() >= deadline:
            raise RuntimeError("native per-case history construction exceeded wall budget")
        if family == "registry":
            if case == "chart":
                _chart_registry_transition(root)
            else:
                node = "scale-history-member-%08d" % index
                Registry(root).register(node, "codex")
                if needs_history_roles:
                    _history_role(root, node)
        else:
            message = log.send("scale-history", recipient, "floati", sha, "note.md",
                "unselected native acknowledged history", idempotency_key="strict-history-%08d" % index)["message"]
            batch_ids.append(message["id"])
            if family == "deliveries" or index + 1 in (small_count, 10 * small_count):
                messages, _ = log.present(recipient)
                returned = {row["id"] for row in messages}
                if (not set(batch_ids) <= returned or not returned - set(batch_ids) <= preface_ids
                        or any(row["recipient"] != recipient for row in messages)):
                    raise RuntimeError("native history presentation changed selected work")
                # Preserve every original selected ACK boundary: acknowledge history IDs only.
                cursor.ack(recipient, batch_ids, acting_session_id="strict-history")
                batch_ids = []
        if index + 1 in (small_count, 10 * small_count):
            if {node: log.unacked_ids(node) for node in selected_before} != {
                    node: row["unacked"] for node, row in selected_before.items()}:
                raise RuntimeError("native history changed selected pending work")
            scale = "1" if index + 1 == small_count else "10"
            home = scratch / ("seed-" + scale) / "scale-fleet"
            # Teardown rebinds this bank after every restore. Create and certify
            # it before freezing the root mtime compared across measured modes.
            _freeze_native_seed(prepared, home, scratch / ("seed-freeze-" + scale + ".json"),
                                event_cut=case == "seat-teardown")
            result[scale] = home
    history["family_proof"] = _qualify_expanded_family(case, result)
    return result


def _strict_holds(detail, *, begin=None, end=None):
    holds = [row for family in detail["hold_families"].values() for row in family["intervals"]]
    if begin is None:
        return holds
    result = []
    for row in holds:
        overlap = min(row["release_call_after_ns"], end) - max(row["acquire_call_after_ns"], begin)
        if overlap > 0:
            result.append(dict(row, seconds=overlap / 1e9))
    return result


def _original_observation(case, seed, workspace, place, owner, producer):
    """Existing native witnesses, with preflight before the measured boundary."""
    from floati.root import FloatiRoot
    from floati.events import EventLog
    from floati.jsonl import read_records
    from tests.bus_scale_effects import (normalize_durable_delta, normalize_timing_delta,
                                         validate_status_cache)
    from tests.bus_scale_integration import normalize_readiness, normalize_status_cache, _phase
    from tests.bus_scale_equivalence import normalize_artifact
    records, observed = {}, {}
    seed_before = _inventory(seed)
    for mode in ("cpu", "diagnostic"):
        owner.preparation_phase("clone")
        child_place = place / mode
        child_place.mkdir(parents=True, mode=0o700)
        home = child_place / "scale-fleet"
        shutil.copytree(seed, home)
        if _inventory(home) != seed_before:
            raise RuntimeError("native continuation clone differs from its frozen seed")
        owner.preparation_phase("warm_and_verify")
        preflight = _warm_fixture(home)
        root = FloatiRoot.open_direct_home(home)
        if case in {"wait-start", "wait-idle"}:
            owner.preparation_phase("wait_preparation")
            preflight["native_wait_sources"] = _prepare_wait_clone(home)
        owner.preparation_phase("before_inventory")
        _write_json(child_place / "preflight.json", preflight)
        exhaustion = read_records(root, "receipts/codex-wait-exhaustion/scale-worker.jsonl",
                                  allowed_kinds={"codex_wait_exhaustion_receipt"})
        wait_budget = None
        if case in {"wait-start", "wait-idle"}:
            consent = read_records(root, "receipts/codex-wait-consent/scale-worker.jsonl",
                allowed_kinds={"codex_wait_consent_receipt"})
            if len(consent) != 1:
                raise RuntimeError("native wait fixture requires its exact single consent")
            wait_budget = consent[0]["wait_deadline_seconds"] + 2
        before = {"event_rows": EventLog(root).event_records(), "exhaustion_rows": exhaustion,
                  "exhaustion_count": len(exhaustion)}
        raw_before, inventory_before = producer.raw_files(home), producer.files(home)
        receipt_input = None
        if case in {"receipt-append", "wait-start", "wait-idle"}:
            from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import capture_receipt_prefix_input
            receipt_input = capture_receipt_prefix_input(home, raw_before)
        output, error, detail_path = (child_place / name for name in ("stdout.txt", "stderr.txt", "detail.json"))
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        wall_begin = datetime.now(timezone.utc)
        with output.open("xb") as out, error.open("xb") as err:
            child = owner.launch_owned_child(["/usr/bin/python3", "-B", str(ENTRY), mode, case,
                str(home), str(workspace), str(detail_path), "owned-census-v1"],
                cwd=workspace, env=_environment(child_place), stdout=out, stderr=err)
            collection = owner.collect_owned_child(child, deadline_seconds=70, collection_seconds=5)
        wall_end = datetime.now(timezone.utc)
        owner.preparation_phase("effect_and_equivalence_proofs")
        usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = usage_after.ru_utime + usage_after.ru_stime - usage_before.ru_utime - usage_before.ru_stime
        record = {"collection": collection, "pid": child.pid, "cpu_seconds": cpu,
                  "wall_begin": wall_begin.isoformat(), "wall_end": wall_end.isoformat(),
                  "preflight": preflight, "seed_inventory": seed_before}
        _write_json(child_place / "collection.json", record)
        expected = 35 if case == "doctor" else 0
        if collection["status"] != "complete" or collection["exit_code"] != expected or not detail_path.exists():
            raise RuntimeError("native continuation child failed: " + json.dumps(collection)
                               + " " + error.read_text()[-1500:])
        if wait_budget is not None and collection["wall_seconds"] > wait_budget:
            raise RuntimeError("wait child exceeded wait_deadline_seconds + 2 s startup allowance: "
                + str(collection["wall_seconds"]))
        detail = json.loads(detail_path.read_text())
        artifact = json.loads(output.read_text())
        inventory_after = producer.files(home)
        if any(inventory_before[p]["mode"] != inventory_after[p]["mode"]
               for p in set(inventory_before) & set(inventory_after)):
            raise RuntimeError("native command changed a retained fixture mode")
        delta = producer.native_delta(case, raw_before, producer.raw_files(home),
            home=home, receipt_prefix_input=receipt_input)
        receipt_proof = None
        if receipt_input is not None:
            from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import verify_receipt_prefix_effect
            receipt_proof = verify_receipt_prefix_effect(home, raw_before,
                producer.raw_files(home), delta, receipt_input)
        native = producer.effect_witness(case, home, before, output.read_text(), error.read_text(),
                                         detail, workspace=workspace)
        normalized = None
        if case in {"doctor", "receipt-append", "wait-start", "wait-idle"}:
            decoder = normalize_timing_delta if case == "doctor" else normalize_durable_delta
            normalized = decoder(case, delta, artifact, wall_begin=wall_begin, wall_end=wall_end,
                child_pid=child.pid, home=home, workspace=workspace, child_cpu_seconds=cpu,
                **({"receipt_prefix_input": receipt_input, "before_raw_files": raw_before}
                   if receipt_input is not None else {}))
        if case == "readiness-read":
            normalized = normalize_readiness(home, artifact)
        if case in {"log-history", "receipts-history"}:
            normalized = json.loads(json.dumps(artifact))
            scope = normalized.get("evidence", {}).get("scope")
            if scope is not None:
                if scope.get("root") != str(home):
                    raise RuntimeError("native history scope points outside its fixture")
                scope["root"] = "<bus-scale-fixture>"
            normalized = {"artifact": normalized, "delta": delta}
        if case == "status":
            validate_status_cache(home, artifact)
        if case == "wait-idle":
            _phase(detail, mode)
            if mode == "diagnostic":
                endpoint = detail["startup"]["endpoint_ns"]
                if any(row["acquire_call_after_ns"] < endpoint < row["release_call_after_ns"]
                       for row in _strict_holds(detail)
                       if row["path"].endswith(".jsonl.lock") or row["path"] == "locks/records.lock"):
                    raise RuntimeError("startup record/ledger hold crosses the observed endpoint")
        record.update(detail=detail, artifact=artifact, effect=native, native_delta=delta,
                      receipt_prefix_effect=receipt_proof,
                      inventory_before=inventory_before, inventory_after=inventory_after,
                      equivalence=normalized)
        records[mode] = record
        observed[mode] = (home, wall_begin, wall_end)
        if producer.files(home) != inventory_after or _inventory(seed) != seed_before:
            raise RuntimeError("native effect validation changed a measured fixture or frozen seed")
    if case in {"doctor", "status"}:
        source = records["cpu"]["artifact"]["evidence"]
        clock = (next(row["wake_health"]["recent_sessions"]["window_end"] for row in source["findings"]
                      if row["code"] == "wake_health") if case == "doctor"
                 else source["nodes"][0]["wake_health"]["recent_sessions"]["window_end"])
        canonical = datetime.fromisoformat(clock.replace("Z", "+00:00"))
        for mode, record in records.items():
            home, begin, end = observed[mode]
            normalized = normalize_artifact(case, record["artifact"], home, workspace, begin, end, canonical)
            if case == "status":
                cache_path = home / (".floati-snapshots/v0/status-" + producer.sha(b"default") + ".json")
                record["cache_equivalence"] = normalize_status_cache(
                    json.loads(cache_path.read_text()), record["artifact"], normalized)
            else:
                record["effect"]["findings_sha256"] = producer.sha(json.dumps(
                    normalized["artifact"]["evidence"]["findings"], sort_keys=True).encode())
            record["artifact_equivalence"] = normalized
    for field in ("effect", "cache_equivalence", "artifact_equivalence"):
        if records["cpu"].get(field) != records["diagnostic"].get(field):
            # Normalization audit values retain different raw clocks and paths;
            # compare the proved semantic payload, retaining both full audits.
            if field == "effect":
                raise RuntimeError("diagnostics changed the native effect")
            key = "cache" if field == "cache_equivalence" else "artifact"
            if records["cpu"][field][key] != records["diagnostic"][field][key]:
                raise RuntimeError("diagnostics changed complete native " + field)
    for field in ("artifact", "delta"):
        one, two = records["cpu"]["equivalence"], records["diagnostic"]["equivalence"]
        if one is not None and two is not None and one.get(field) != two.get(field):
            if not (case == "doctor" and field == "artifact"):
                raise RuntimeError("diagnostics changed normalized native " + field)
    shapes = []
    for record in records.values():
        proof = record.get("receipt_prefix_effect")
        proved = set(proof["changed_files"]) if proof is not None else set()
        shape = {path: {"operation": row["operation"], "mode": record["inventory_after"][path]["mode"]}
                 for path, row in record["native_delta"].items() if path not in proved}
        if proof is not None:
            from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import SUMMARY
            # Node names depend on genuine generated receipt IDs. Complete
            # byte/map/insertion/private-mode proof precedes this projection.
            shape[SUMMARY] = proof["semantic"]
        shapes.append(shape)
    if shapes[0] != shapes[1]:
        raise RuntimeError("diagnostics changed native effect paths or modes")
    diagnostic = records["diagnostic"]["detail"]
    if diagnostic.get("coverage") != {"bytes": True, "locks": True}:
        raise RuntimeError("native diagnostic coverage is incomplete")
    values = {"cpu_seconds": records["cpu"]["cpu_seconds"],
              "read_bytes": sum(diagnostic["read_bytes_by_path"].values()),
              "ledger_hold_seconds": longest_ledger_hold(_strict_holds(diagnostic))}
    if case in _OUTPUT_PROPORTIONAL:
        # The captured native stdout is the complete artifact whose semantics
        # were checked in both modes above. Measure bytes, not JSON character
        # count or a reconstructed/normalized payload.
        values["bytes_emitted"] = (place / "diagnostic/stdout.txt").stat().st_size
    if case == "wait-idle":
        idle, cpu_idle = diagnostic["idle"], records["cpu"]["detail"]["idle"]
        values.update(cpu_seconds=(cpu_idle["cpu_end_ns"] - cpu_idle["cpu_begin_ns"]) / 1e9,
            read_bytes=idle["bytes_read"], ledger_hold_seconds=longest_ledger_hold(
                _strict_holds(diagnostic, begin=idle["begin_ns"], end=idle["end_ns"])))
    for mode, record in records.items():
        home = observed[mode][0]
        if producer.files(home) != record["inventory_after"] or _inventory(seed) != seed_before:
            raise RuntimeError("equivalence validation changed native testimony")
        _write_json(place / mode / "collection.json", record)
    return values, records


def _remaining_case(case, bank):
    """Qualify one native case, retaining incomplete raw evidence on failure."""
    from tests.bus_scale_test_profile import banked_send_repository
    started = time.monotonic()
    start_load1 = os.getloadavg()[0]
    deadline = started + 570
    scratch = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-" + case + "-",
                                    dir=REAL_TEMP_ROOT)).resolve()
    producer = _load(ROOT / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                     "remaining_native_producer")
    entry = producer.native_entry_module()
    owner = _ContinuationOwner(deadline, measured_budget=570, started=started, case=case)
    result = {"case": case, "native_complete": False, "scratch_removed": False,
              "source_before": _source_witness(), "pairs": [], "raw": [], "phase": "native_setup",
              "load1": {"start": start_load1}}
    report_clocks = case in EXPANDED_SPLIT_CLOCK
    if report_clocks:
        result["pair_clocks"] = []
    uncollected = False
    try:
        repository, _ = banked_send_repository(scratch)
        workspace = scratch / "workspace"
        if case == "seat-teardown":
            seat_home = scratch / "seat-home"
            seat_home.mkdir(mode=0o700)
            workspace = seat_home / "repo"
        repository.rename(workspace)
        workspace.chmod(0o700)
        if case in ORIGINAL_REMAINING:
            if case == "receipt-append":
                receipt_pair = native_exhaustion_seeds(scratch, deadline=deadline)
                seeds = {scale: receipt_pair[scale] for scale in ("1", "10")}
                result["history"] = receipt_pair["history"]
            else:
                seed_bounds = ({"preparation_check": owner.preparation_phase} if owner._split_clock else {})
                seeds = native_seeds(scratch, deadline=deadline,
                                     workspace=workspace if case == "doctor" else None, **seed_bounds)
                if case.startswith("wait-"):
                    owner.preparation_phase("wait_seed_binding")
                    _prepare_wait_seeds(seeds, workspace)
                result["history"] = {"producer": "native send/present/ack", "sender": "scale-worker",
                    "recipient": "architect", "counts": {"1": 32, "10": 320},
                    "scope": "global event history and architect delivery/ACK ledgers"}
            result["family_coverage"] = original_family_coverage(case, seeds)
            seed_homes = seeds
            observer = None
        else:
            seed_bounds = {"preparation_check": owner.preparation_phase} if report_clocks else {}
            seeds = _expanded_native_seeds(case, scratch, workspace, entry, deadline, **seed_bounds)
            if report_clocks:
                owner.preparation_phase("seed_validation")
            _qualify_expanded_family(case, seeds)
            result["history"] = seeds["history"]
            result["native_preface"] = seeds["context"]
            seed_homes = {scale: seeds[scale] for scale in ("1", "10")}
            if case in {"supervise", "watch"}:
                seeds["attention_time_policy"] = "native_preserved"
            observer = producer.expanded_adapter()
            restore = observer.restore

            def restore_with_checkpoint(*args, **kwargs):
                owner.cursor_preflights = ()
                receipt = restore(*args, **kwargs)
                receipt_path = Path(args[5] if len(args) > 5 else kwargs["receipt_path"])
                if receipt_path.name == "final-restore.json":
                    return receipt
                cursor_preflights = ([] if case == "seat-teardown" or case in
                    getattr(observer, "CURSOR_PREPARE_CASES", ()) else None)
                receipt["checkpoint_preflight"] = _warm_fixture(Path(seeds["prepared_home"]),
                    cursor_preflights=cursor_preflights,
                    event_cut=case in {"wake-evaluate", "wake-wait", "wake-daemon-serve"})
                owner.cursor_preflights = tuple(cursor_preflights or ())
                # The rebuild follows exact restore and precedes the observer's
                # before-state capture and collected-child CPU boundary.
                _write_json(receipt_path, receipt)
                return receipt

            observer.restore = restore_with_checkpoint
        result["seeds"] = {scale: _inventory(home) for scale, home in seed_homes.items()}
        result["seed_ledgers"] = {scale: {path.relative_to(home).as_posix(): {
            "bytes": path.stat().st_size, "frames": len(path.read_bytes().splitlines())}
            for path in home.rglob("*.jsonl")} for scale, home in seed_homes.items()}
        result["phase"] = "measurement"
        for index in range(5):
            if report_clocks:
                owner.preparation_phase("pair_preparation")
                pair_start = owner.accounting()
            pair = {"order": [1, 10] if index % 2 == 0 else [10, 1]}
            detail = {}
            for scale in pair["order"]:
                place = scratch / "measure" / str(index) / str(scale)
                if observer is None:
                    values, raw = _original_observation(case, seeds[str(scale)], workspace,
                                                         place, owner, producer)
                    if case == "wait-start":
                        companion_values, companion_raw = _original_observation("wait-idle", seeds[str(scale)],
                            workspace, scratch / "phase-companion" / str(index) / str(scale), owner, producer)
                        raw["phase_companion"] = {"case": "wait-idle", "same_frozen_seed": True,
                            "metrics": companion_values, "raw": companion_raw}
                else:
                    place.mkdir(parents=True, mode=0o700)
                    arm = observer.expanded_observation(owner, case, seeds, str(scale), workspace, place, ENTRY)
                    if arm.get("native", {}).get("exit_code") != 0 or arm.get("child_collected") is not True:
                        raise RuntimeError("strict census requires completed native work; refusal is not measurement")
                    if arm.get("coverage") != {"cpu": True, "bytes": True, "locks": True}:
                        raise RuntimeError("expanded native metric coverage incomplete")
                    diagnostic = json.loads((place / "diagnostic/detail.json").read_text())
                    values = {"cpu_seconds": arm["metrics"]["cpu_seconds"],
                        "read_bytes": arm["metrics"]["bytes_read"],
                        "ledger_hold_seconds": longest_ledger_hold(_strict_holds(diagnostic))}
                    collection = json.loads((place / "collection.json").read_text())
                    if case in _OUTPUT_PROPORTIONAL:
                        values["bytes_emitted"] = _expanded_emitted_bytes(case, seeds, place, collection)
                    raw = {"arm": arm, "strict_metrics": values,
                           "collection": collection}
                pair[str(scale)], detail[str(scale)] = values, raw
            result["pairs"].append(pair)
            result["raw"].append(detail)
            if report_clocks:
                accounting = owner.accounting()
                clocks = {"case": case, "pair": index + 1, "order": pair["order"]}
                for key in ("measured_seconds", "preparation_seconds"):
                    clocks[key] = accounting[key] - pair_start[key]
                result["pair_clocks"].append(clocks)
                result["clock_accounting"] = accounting
                print("BUS_SCALE_CLOCK " + json.dumps(clocks, sort_keys=True), file=sys.stderr, flush=True)
            _write_json(bank / "case.json", result)
        result["phase"] = "comparison"
        owner.preparation_phase("comparison")
        result["comparison"] = compare_case_pairs(case, result["pairs"])
        result["native_complete"] = True
        result["phase"] = "complete"
        return result
    except BaseException as error:
        uncollected = (bool(owner.homes) or isinstance(error, UncollectedChild)
                       or bool(getattr(error, "uncollected_child", False)))
        result["error"] = {"type": type(error).__name__, "detail": str(error), "phase": result["phase"]}
        if isinstance(error, (PreparationDeadline, MeasuredDeadline, ChildCommandDeadline, ChildCollectionDeadline)):
            result["error"].update(code=error.code, instrument_finding=isinstance(error, PreparationDeadline))
        if isinstance(error, ChildInterpreterMismatch):
            result["error"].update(error.evidence)
        if uncollected and not isinstance(error, UncollectedChild):
            raise UncollectedChild("native case retains an uncollected child at " + str(scratch)) from error
        raise
    finally:
        # A possibly live child retains all inputs. Collected failures are
        # archived as incomplete evidence before private roots are removed.
        if not uncollected:
            owner.preparation_phase("archive_and_cleanup", check=False)
            archive = bank / "raw.tar.gz"
            with tarfile.open(archive, "x:gz") as stream:
                stream.add(scratch, arcname="scratch")
            result["raw_archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            shutil.rmtree(scratch)
            result["scratch_removed"] = not scratch.exists()
        else:
            result["retained_scratch"] = str(scratch)
        result["load1"]["end"] = os.getloadavg()[0]
        result["source_after"] = _source_witness()
        result["source_unchanged"] = result["source_before"] == result["source_after"]
        if not result["source_unchanged"]:
            result["native_complete"] = False
        result["wall_seconds"] = time.monotonic() - started
        accounting = owner.accounting()
        result["clock_accounting"] = accounting
        if owner._split_clock and accounting["preparation_seconds"] > owner.preparation_budget:
            result["native_complete"] = False
            if "error" not in result:
                result["error"] = {"type": "PreparationDeadline", "code": "preparation_deadline",
                    "instrument_finding": True, "phase": "archive_and_cleanup", "detail": accounting}
        if report_clocks:
            print("BUS_SCALE_CLOCK " + json.dumps(dict(accounting, case=case, phase="total"),
                  sort_keys=True), file=sys.stderr, flush=True)
        _write_json(bank / "case.json", result)


def first_finding(bank, *, small_count=32, sample_runner=None):
    """Measure send first; stop and preserve a structural RED before cures.

    Remaining adapters are intentionally not executed after that stop. This
    function cannot report GREEN until their native construction is qualified.
    """
    from floati.cli import _parser
    from tests.bus_scale_test_profile import banked_send_repository
    started = time.monotonic()
    start_load1 = os.getloadavg()[0]
    deadline = started + 570
    scope = population(_parser())
    result = {"status": "incomplete", "scope": scope, "findings": [],
              "measured": [], "pending": list(scope["cases"]), "scratch_removed": False,
              "load1": {"start": start_load1}}
    result["source_before"] = _source_witness()
    scratch = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-", dir=REAL_TEMP_ROOT)).resolve()
    try:
        repository, _sha = banked_send_repository(scratch)
        workspace = scratch / "workspace"
        repository.rename(workspace)
        workspace.chmod(0o700)
        seeds = native_seeds(scratch, small_count=small_count, deadline=deadline)
        result["seeds"] = {scale: _inventory(seed) for scale, seed in seeds.items()}
        result["seed_ledgers"] = {scale: {
            p.relative_to(seed).as_posix(): {"bytes": p.stat().st_size,
                "frames": len(p.read_bytes().splitlines())}
            for p in seed.rglob("*.jsonl")}
            for scale, seed in seeds.items()}
        pairs, raw = [], []
        for index in range(5):
            pair = {"order": [1, 10] if index % 2 == 0 else [10, 1]}
            detail = {}
            for scale in pair["order"]:
                pair[str(scale)], detail[str(scale)] = (sample_runner or _native_sample)("send", seeds[str(scale)], workspace,
                    scratch / "measure" / str(index) / str(scale), deadline)
            pairs.append(pair)
            raw.append(detail)
        comparison = compare_case_pairs("send", pairs)
        result.update(measured=["send"], pairs=pairs, raw=raw, comparison=comparison,
                      native_measurement=sample_runner is None)
        result["pending"].remove("send")
        for metric, row in comparison["metrics"].items():
            if row["classification"] != "pass":
                result["findings"].append(dict(command="send", metric=metric, **row))
        result["status"] = "red" if result["findings"] else "incomplete"
        print("BUS_SCALE_CASE send ordinary measured", flush=True)
        return result
    finally:
        # All expected child paths have been collected before returning. An
        # unexpected failure retains scratch for investigation.
        if "comparison" in result:
            archive = bank / "raw.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                output.add(scratch, arcname="scratch")
            result["raw_archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            shutil.rmtree(scratch)
            result["scratch_removed"] = not scratch.exists()
        else:
            result["retained_scratch"] = str(scratch)
        result["wall_seconds"] = time.monotonic() - started
        result["load1"]["end"] = os.getloadavg()[0]
        result["source_after"] = _source_witness()
        result["source_unchanged"] = result["source_before"] == result["source_after"]
        if not result["source_unchanged"]:
            result["status"] = "incomplete"
        (bank / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    if len(sys.argv) == 9 and sys.argv[1] == "native-child":
        _native_child(sys.argv[2:8], sys.argv[8])
    elif len(sys.argv) == 6 and sys.argv[1] == "fixture-child":
        from tests import HOME_PIN_ROOT
        home = Path(HOME_PIN_ROOT)
        try:
            _fixture_child(sys.argv[2], sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5]))
        finally:
            if (home.parent != Path(REAL_TEMP_ROOT) or not home.name.startswith("suite-home-")
                    or home.is_symlink() or home.stat().st_uid != os.getuid()):
                raise RuntimeError("planted command test HOME identity changed")
            shutil.rmtree(home)
        output = Path(sys.argv[5])
        result = json.loads(output.read_text())
        result["home_removed"] = not home.exists()
        output.write_text(json.dumps(result, sort_keys=True) + "\n")
    else:
        raise SystemExit("expected fixture-child KIND MODE ROOT OUTPUT or native-child MODE CASE ROOT WORKSPACE RESULT MARKER CLEANUP")
