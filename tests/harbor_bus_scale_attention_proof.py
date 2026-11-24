"""Closed attention admission controls, not measured scale evidence.

Literal samples below exercise the existing real first-pass consumer. Hash
syntax is not source authentication; the native adapter controls separately
require proof generation from complete witnessed artifacts and archived bytes.
"""
from __future__ import annotations

import copy
from tests.bus_scale_test_profile import clone_fixture
from functools import lru_cache
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.bus_scale_test_profile import StableSourceFixture

from floati.cli import _parser
from floati.events import EventLog
from floati.root import FloatiRoot
from tests import bus_scale_workloads as workloads
from tests.bus_scale_test_profile import banked_send_repository
from tests.bus_scale_case_seeds import FixtureSendCache, make_case_seed_pair
from tests.bus_scale_test_profile import seed_counts
from tests.temp_roots import REAL_TEMP_ROOT


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "tests/fixtures/bus_scale_census"
ATTENTION_CASES = ("supervise", "watch")
THRESHOLD_BASIS = "inherited ruled judgment; not a measured turn duration"
WAKE_REMEDY = ("declare one exact workspace and acting session for this node, then run floati seat board "
               "with --session and --take-over for that coordinate; after success present the verified takeover_report")
SOURCE_PATHS = ("tests/harbor_bus_scale_attention_proof.py", "tests/bus_scale_case_seeds.py",
                "tests/bus_scale_workloads.py", "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py",
                "tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py",
                "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py",
                "tests/fixtures/bus_scale_census/bus-scale-routine-reads-2026-10-15.py",
                "floati/supervisor.py", "floati/projection.py", "floati/wake_health.py")


def sources():
    paths = SOURCE_PATHS + (("tests/bus_scale_attention.py",)
                            if (ROOT / "tests/bus_scale_attention.py").is_file() else ())
    return {relative: {"bytes": (ROOT / relative).stat().st_size,
                       "sha256": hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()}
            for relative in paths}


class CollectedOwner:
    """Delegate native work unchanged and retain its exact collection results."""
    def __init__(self, delegate):
        self.delegate, self.pending, self.records = delegate, set(), []

    def launch_owned_child(self, argv, **kwargs):
        child = self.delegate.launch_owned_child(argv, **kwargs)
        self.pending.add(child.pid)
        return child

    def collect_owned_child(self, child, **kwargs):
        result = self.delegate.collect_owned_child(child, **kwargs)
        self.records.append({"child_pid": child.pid, "collection": clone_fixture(result)})
        if result.get("child_collected") is True:
            self.pending.remove(child.pid)
        return result


class AttentionSeedPreparation:
    def __init__(self, helper):
        self.helper = helper

    def prepare(self, case, home, workspace, bank):
        cache_owner = FixtureSendCache(Path(bank).parent, home)
        context = self.helper.prepare(case, home, workspace, bank)
        root = FloatiRoot.open_direct_home(home, create=False)
        own_send_ids = [row["id"] for row in EventLog(root).records()
                        if row["idempotency_key"] == "routine-read-common-prefix"]
        # Remove the entire disposable cache only after proving confinement
        # and the exact native sends since ownership capture.
        self.cache_cleanup_receipt = cache_owner.remove(own_send_ids)
        return context


@contextmanager
def owned_native_cases():
    base = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-attention-proof-", dir=REAL_TEMP_ROOT)).resolve()
    owners = []
    try:
        yield base, owners
    finally:
        if all(not owner.pending for owner in owners):
            shutil.rmtree(base)
        else:
            print("uncollected attention-proof fixture retained at original coordinate: " + str(base), flush=True)


@contextmanager
def retain_failed_case(bank, case, owner):
    initial = sources()
    head = subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    try:
        yield
    except Exception as error:
        if not owner.pending:
            retained = Path(tempfile.mkdtemp(prefix="bus-scale-attention-proof-red-", dir=REAL_TEMP_ROOT))
            if bank.is_dir():
                shutil.copytree(bank, retained / "case", copy_function=shutil.copy2)
            (retained / "source-bindings.json").write_text(json.dumps({
                "head": head, "initial": initial, "final": sources(), "case": case,
                "collections": owner.records, "error_type": type(error).__name__, "error": str(error),
                "original_coordinate": str(bank),
            }, sort_keys=True) + "\n")
            print("raw native attention-proof RED retained: " + str(retained), flush=True)
        raise


def load(filename, name):
    from tests.bus_scale_test_profile import load_fixture_module
    return load_fixture_module(EVIDENCE / filename, name)


def seat_category(*, pending=False, overdue=False):
    result = {"state": "not_overdue", "reason": "fresh_waiting_mail" if pending else "no_waiting_mail",
              "remedy": "", "hold_evidence_state": "readable" if pending else "not_read",
              "threshold_minutes": 15, "threshold_basis": THRESHOLD_BASIS, "detail": "",
              "nullness": {"oldest_waiting_at": not pending, "oldest_waiting_age_seconds": not pending,
                           "next_check_at": not pending, "last_hold_started_at": True}}
    if overdue:
        result.update(state="unknown", reason="overdue_without_hold_start",
            remedy="Look at architect's seat for a pending approval card or a stopped turn.",
            detail="architect: 1 undelivered, oldest waiting <waiting-duration>; last hold start none on record; "
                   "threshold 15m (inherited ruled judgment). Floati cannot tell a long-running turn from skipped Stop.")
        result["nullness"]["next_check_at"] = True
    return result


def literal_proof(case, scale):
    nodes = {}
    for node in ("architect", "scale-worker", "scale-history"):
        pending = node == "architect"
        wake = None
        if case == "watch":
            wake = {"state": "stale_claim_with_unread_mail" if pending else "healthy",
                    "breaker_state": "closed", "pause_state": "unknown", "waiter_exit_reason": None,
                    "documented_entrypoint_resolves": True, "remedy": WAKE_REMEDY, "read_finding": None,
                    "nullness": {"claim_session": True, "last_seen_session": True,
                                 "oldest_unread": not pending, "waiter_receipt_age_minutes": True}}
        nodes[node] = {"seat_attention": seat_category(pending=pending), "wake_health": wake}
    return {"schema_version": 1, "case": case, "nodes": nodes,
            "sources": {"cpu": ("1" if scale == 1 else "3") * 64,
                        "diagnostic": ("2" if scale == 1 else "4") * 64}}


def population():
    """Fresh mutable inputs copied from one privately shared literal population."""
    return clone_fixture(_population_template())


@lru_cache(maxsize=1)
def _population_template():
    scope = workloads.scope_contract(_parser())
    observations = {}
    # These are functional admission samples for all 70 required cases, not
    # assertions that unrelated commands ran or that these metrics were measured.
    for case, contract in scope["cases"].items():
        code, status, kind = workloads._NATIVE[case]
        native = {"exit_code": code, "status": status, "effect": {"kind": kind, "count": 1}}
        if case == "doctor":
            native["findings_sha256"] = "d" * 64
        arms = {}
        for scale in (1, 10):
            seed = hashlib.sha256((case + ":schema-seed:" + str(scale)).encode()).hexdigest()
            arm = {"records": 1000 * scale, "bytes": 100000 * scale,
                   "seed_sha256": seed, "clone_sha256": seed, "child_collected": True,
                   "raw_sha256": "a" * 64, "native": clone_fixture(native),
                   "coverage": {"cpu": True, "bytes": True, "locks": True},
                   "metrics": {"cpu_seconds": 1.0, "bytes_read": 100, "max_ledger_lock_seconds": 0.001}}
            if case in ATTENTION_CASES:
                arm["attention_proof"] = literal_proof(case, scale)
            arms[str(scale) + "x"] = arm
        observations[case] = {"family": contract["family"], "prefix_exact": True,
                              "selected_work_equal": True, "arms": arms}
    return scope, observations


class AttentionProofAdmissionTests(unittest.TestCase):
    def test_firstpass_accepts_complete_literal_categories_despite_distinct_source_hashes(self):
        scope, observations = population()
        for case in ATTENTION_CASES:
            one, ten = [observations[case]["arms"][scale]["attention_proof"] for scale in ("1x", "10x")]
            self.assertNotEqual(one["sources"], ten["sources"])
        workloads.require_observations(scope, observations)

    def test_full_matrix_accepts_categories_through_actual_classification_consumer(self):
        coordinator = load("census-bus-scale-2026-10-09.py", "attention_proof_matrix_controls")
        scope, observations = population()
        matrix = {}
        for family in ("events", "registry", "wait-exhaustion", "deliveries"):
            rows = clone_fixture(observations)
            for row in rows.values():
                row["family"] = family
            matrix[family] = [{"order": order, "observations": clone_fixture(rows)}
                              for order in ([1, 10], [10, 1])]
        result = coordinator.validate_matrix(scope, matrix, first_pass=True)
        self.assertEqual(4 * 70, len(result["comparisons"]))
        self.assertEqual([], result["ambiguous_observations"])

    def test_missing_target_proof_is_not_satisfied_by_the_other_attention_case(self):
        for case in ATTENTION_CASES:
            for scale in ("1x", "10x"):
                with self.subTest(case=case, scale=scale):
                    scope, observations = population()
                    del observations[case]["arms"][scale]["attention_proof"]
                    with self.assertRaisesRegex(ValueError, case + r".*categor.*missing"):
                        workloads.require_observations(scope, observations)

    def test_malformed_proof_fields_types_and_source_hashes_are_refused_as_category_errors(self):
        alterations = {
            "version-bool": lambda proof: proof.update(schema_version=True),
            "wrong-case": lambda proof: proof.update(case="status"),
            "extra-field": lambda proof: proof.update(unreviewed=True),
            "missing-sources": lambda proof: proof.pop("sources"),
            "source-extra-mode": lambda proof: proof["sources"].update(other="0" * 64),
            "source-uppercase": lambda proof: proof["sources"].update(cpu="A" * 64),
            "source-short": lambda proof: proof["sources"].update(diagnostic="0" * 63),
            "source-wrong-type": lambda proof: proof["sources"].update(cpu=1),
            "nodes-list": lambda proof: proof.update(nodes=[]),
            "nodes-empty": lambda proof: proof.update(nodes={}),
            "foreign-node-key": lambda proof: proof["nodes"].update({"../architect": proof["nodes"].pop("architect")}),
            "missing-node-field": lambda proof: proof["nodes"]["architect"].pop("seat_attention"),
            "extra-category-field": lambda proof: proof["nodes"]["architect"]["seat_attention"].update(extra=""),
            "missing-state": lambda proof: proof["nodes"]["architect"]["seat_attention"].pop("state"),
            "state-wrong-type": lambda proof: proof["nodes"]["architect"]["seat_attention"].update(state={}),
            "prose-wrong-type": lambda proof: proof["nodes"]["architect"]["seat_attention"].update(detail=None),
            "threshold-bool": lambda proof: proof["nodes"]["architect"]["seat_attention"].update(threshold_minutes=True),
            "nullness-number": lambda proof: proof["nodes"]["architect"]["seat_attention"]["nullness"].update(next_check_at=1),
            "missing-nullness": lambda proof: proof["nodes"]["architect"]["seat_attention"]["nullness"].pop("oldest_waiting_at"),
        }
        for case in ATTENTION_CASES:
            for label, change in alterations.items():
                with self.subTest(case=case, poison=label):
                    scope, observations = population()
                    change(observations[case]["arms"]["1x"]["attention_proof"])
                    with self.assertRaisesRegex(ValueError, case + r".*categor.*(malformed|invalid)"):
                        workloads.require_observations(scope, observations)

    def test_watch_requires_closed_nested_wake_health_and_supervise_requires_null(self):
        for case, label in (("watch", "absent"), ("watch", "extra"), ("watch", "bad-nullness"),
                            ("watch", "bad-boolean"), ("supervise", "unexpected")):
            with self.subTest(case=case, poison=label):
                scope, observations = population()
                node = observations[case]["arms"]["1x"]["attention_proof"]["nodes"]["architect"]
                if label == "absent": node["wake_health"] = None
                elif label == "extra": node["wake_health"]["extra"] = ""
                elif label == "bad-nullness": node["wake_health"]["nullness"]["oldest_unread"] = 0
                elif label == "bad-boolean": node["wake_health"]["documented_entrypoint_resolves"] = 1
                else: node["wake_health"] = {}
                with self.assertRaisesRegex(ValueError, case + r".*categor.*(malformed|invalid)"):
                    workloads.require_observations(scope, observations)

    def test_well_typed_node_category_nullness_and_prose_differences_are_mismatches(self):
        for case in ATTENTION_CASES:
            for label in ("missing-node", "overdue-category", "hold-category", "clock-nullness", "prose"):
                with self.subTest(case=case, difference=label):
                    scope, observations = population()
                    proof = observations[case]["arms"]["10x"]["attention_proof"]
                    category = proof["nodes"]["architect"]["seat_attention"]
                    if label == "missing-node": del proof["nodes"]["scale-history"]
                    elif label == "overdue-category": proof["nodes"]["architect"]["seat_attention"] = seat_category(pending=True, overdue=True)
                    elif label == "hold-category":
                        category.update(reason="hold_started_since_waiting_mail")
                        category["nullness"].update(next_check_at=True, last_hold_started_at=False)
                    elif label == "clock-nullness": category["nullness"]["last_hold_started_at"] = False
                    else: category["detail"] = "unreviewed native prose must not disappear"
                    with self.assertRaisesRegex(ValueError, case + r".*categor.*mismatch"):
                        workloads.require_observations(scope, observations)


class AttentionProofNativeBindingTests(StableSourceFixture, unittest.TestCase):
    def test_real_expanded_observer_retains_all_native_nodes_and_exact_stdout_bindings(self):
        adapter = load("bus-scale-expanded-observation-2026-10-15.py", "attention_proof_native_adapter")
        delegate = load("run-bus-scale-census-2026-10-09.py", "attention_proof_native_owner")
        helper = load("bus-scale-routine-reads-2026-10-15.py", "attention_proof_native_reads")
        entry = EVIDENCE / "bus-scale-native-entry-2026-10-09.py"
        with owned_native_cases() as (base, owners):
            for case in ATTENTION_CASES:
                bank = base / ("bus-scale-census-owned-" + case)
                owner = CollectedOwner(delegate)
                owners.append(owner)
                with self.subTest(case=case), retain_failed_case(bank, case, owner):
                    bank.mkdir(mode=0o700)
                    workspace, _sha = banked_send_repository(bank)
                    workspace.rename(bank / "workspace")
                    workspace = bank / "workspace"
                    workspace.chmod(0o700)
                    seed_base, place = bank / "seeds", bank / "observation"
                    seed_base.mkdir(mode=0o700)
                    place.mkdir(mode=0o700)
                    pair = make_case_seed_pair(seed_base, "registry", case, AttentionSeedPreparation(helper),
                                               workspace, counts=seed_counts())
                    arm = adapter.expanded_observation(owner, case, pair, "1", workspace, place, entry)
                    self.assertIn("attention_proof", arm,
                                  "real observed attention arms need inspectable witnessed categories")
                    raw_collection = (place / "collection.json").read_bytes()
                    self.assertEqual(hashlib.sha256(raw_collection).hexdigest(), arm["raw_sha256"])
                    collection = json.loads(raw_collection)
                    self.assertEqual(arm["attention_proof"], collection["attention_proof"])
                    for mode in ("cpu", "diagnostic"):
                        raw = (place / mode / "stdout.txt").read_bytes()
                        actual = json.loads(raw)["evidence"]
                        if case == "watch": actual = actual["delta"]["snapshot"]
                        self.assertEqual({row["node_id"] for row in actual["nodes"]},
                                         set(arm["attention_proof"]["nodes"]))
                        self.assertEqual(hashlib.sha256(raw).hexdigest(), arm["attention_proof"]["sources"][mode])
                        self.assertEqual(arm["attention_proof"]["nodes"], collection[mode]["attention_categories"])
                        for row in actual["nodes"]:
                            native = row["seat_attention"]
                            proof = arm["attention_proof"]["nodes"][row["node_id"]]["seat_attention"]
                            for field in ("state", "reason", "remedy", "hold_evidence_state", "threshold_minutes", "threshold_basis"):
                                self.assertEqual(native[field], proof[field])
                            for field in proof["nullness"]:
                                self.assertIs(native[field] is None, proof["nullness"][field])
                            # These fresh native fixtures have no prose age
                            # token, so the complete text must remain literal.
                            self.assertEqual(native["detail"], proof["detail"])
                            wake_proof = arm["attention_proof"]["nodes"][row["node_id"]]["wake_health"]
                            if case == "supervise":
                                self.assertIsNone(wake_proof)
                            else:
                                wake = row["wake_health"]
                                for field in ("state", "breaker_state", "pause_state", "waiter_exit_reason",
                                              "documented_entrypoint_resolves", "remedy"):
                                    self.assertEqual(wake[field], wake_proof[field])
                                self.assertEqual(wake.get("read_finding"), wake_proof["read_finding"])
                                for field in wake_proof["nullness"]:
                                    self.assertIs(wake[field] is None, wake_proof["nullness"][field])
