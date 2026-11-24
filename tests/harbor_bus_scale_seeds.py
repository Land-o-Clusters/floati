"""Native integrity controls for private one-input bus-history seed pairs."""
from __future__ import annotations

# Package import installs the native test HOME before product imports below.
from tests import HOME_PIN_ROOT
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import seed_counts

from collections import defaultdict
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from floati.admin_registry import RegistryAdminBackend
from floati.events import EVENT_KINDS, EventLog, validate_event_records
from floati.framing import encode_frame
from floati.jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, read_records
from floati.records import validate_record
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot

try:
    from tests.bus_scale_seeds import make_seed_pair
except ModuleNotFoundError as error:
    if error.name != "tests.bus_scale_seeds":
        raise
    make_seed_pair = None


FAMILIES = {
    "events": ("events.jsonl", frozenset(EVENT_KINDS)),
    "registry": ("registry/entries.jsonl", frozenset(REGISTRY_KINDS)),
    "exhaustion": (
        "receipts/codex-wait-exhaustion/scale-worker.jsonl",
        frozenset({"codex_wait_exhaustion_receipt"}),
    ),
    "deliveries": (
        "receipts/deliveries/architect.jsonl", frozenset({"delivered_unpresented"}),
    ),
}


class BusScaleSeedTests(unittest.TestCase):
    _pair = None
    _counts = None

    def setUp(self):
        self.assertIsNotNone(
            make_seed_pair,
            "tests.bus_scale_seeds.make_seed_pair must produce native legal history seeds",
        )
        self.assertEqual(Path(HOME_PIN_ROOT), Path.home(),
                         "native test HOME must precede fixture/product work")
        cls = type(self)
        if cls._pair is None or cls._counts != seed_counts():
            temporary = tempfile.TemporaryDirectory(prefix="bus-scale-seeds-", dir=REAL_TEMP_ROOT)
            cls.addClassCleanup(temporary.cleanup)
            cls._base = Path(temporary.name).resolve()
            cls._pair = make_seed_pair(cls._base, counts=seed_counts())
            cls._counts = seed_counts()
            cls.addClassCleanup(setattr, cls, "_pair", None)
            cls._initial_files = {}
            cls._rows_cache = {}
            for family in FAMILIES:
                for arm in ("1", "10"):
                    home = Path(cls._pair[family][arm])
                    cls._initial_files[family, arm] = {
                        path.relative_to(home).as_posix(): path.read_bytes()
                        for path in home.rglob("*") if path.is_file()
                    }
        self.pair = cls._pair

    def home(self, family, arm):
        return Path(self.pair[family][arm])

    def root(self, family, arm):
        return FloatiRoot.open_direct_home(self.home(family, arm))

    def rows(self, family, arm):
        key = (family, arm)
        if key not in self._rows_cache:
            relative, kinds = FAMILIES[family]
            self._rows_cache[key] = read_records(self.root(family, arm), relative, allowed_kinds=kinds)
        return self._rows_cache[key]

    def test_contract_is_four_separate_private_ordinary_pairs(self):
        self.assertEqual(set(FAMILIES), set(self.pair))
        homes = []
        for family, (relative, _kinds) in FAMILIES.items():
            entry = self.pair[family]
            self.assertEqual(relative, entry["ledger_relative"])
            state = entry["selected_state"]
            self.assertEqual("scale-fleet", state["tenant_id"])
            self.assertEqual("scale-worker", state["node_id"])
            self.assertEqual("architect", state["architect_id"])
            for arm in ("1", "10"):
                home = self.home(family, arm)
                homes.append(home)
                self.assertTrue(home.is_absolute())
                self.assertEqual(home, home.resolve(strict=True))
                self.assertIn(self._base, home.parents)
                self.assertEqual("scale-fleet", home.name)
                self.assertEqual(os.getuid(), home.stat().st_uid)
                for path in home.rglob("*"):
                    self.assertFalse(path.is_symlink(), str(path))
                    self.assertTrue(path.is_file() or path.is_dir(), str(path))
                    if path.is_file():
                        self.assertEqual(1, path.stat().st_nlink, str(path))
        self.assertEqual(8, len(set(homes)))

    def test_counts_native_validation_and_canonical_frames(self):
        for family, (relative, kinds) in FAMILIES.items():
            for arm, count in zip(("1", "10"), seed_counts()):
                with self.subTest(family=family, arm=arm):
                    rows = self.rows(family, arm)
                    self.assertEqual(count, len(rows))
                    root = self.root(family, arm)
                    for row in rows:
                        validate_record(row, root.tenant_id, kinds, integrity=True)
                    raw = (root.tenant_home / relative).read_bytes()
                    self.assertEqual(raw, b"".join(encode_frame(row) for row in rows))
                    self.assertLessEqual(len(raw), MAX_LEDGER_BYTES)
                    self.assertLessEqual(len(rows), MAX_LEDGER_RECORDS)

    def test_one_history_prefix_and_increasing_actual_bytes(self):
        for family, (relative, _kinds) in FAMILIES.items():
            with self.subTest(family=family):
                small = (self.home(family, "1") / relative).read_bytes()
                large = (self.home(family, "10") / relative).read_bytes()
                self.assertTrue(large.startswith(small))
                self.assertGreater(len(large), len(small))
                self.assertEqual(seed_counts()[0], small.count(b"\n"))
                self.assertEqual(seed_counts()[1], large.count(b"\n"))

    def test_only_the_designated_ledger_changes_between_arms(self):
        for family, (relative, _kinds) in FAMILIES.items():
            with self.subTest(family=family):
                small = dict(self._initial_files[family, "1"])
                large = dict(self._initial_files[family, "10"])
                self.assertIn(relative, small)
                self.assertIn(relative, large)
                del small[relative]
                del large[relative]
                self.assertEqual(small, large)

    def test_final_two_active_assignments_match_declared_constant_state(self):
        for family in FAMILIES:
            declared = self.pair[family]["selected_state"]["assignments"]
            self.assertEqual({"scale-worker", "architect"}, set(declared))
            self.assertEqual({"role": "codex", "model": None, "state": "active"},
                             declared["scale-worker"])
            expected_model = "scale-model-a" if family == "registry" else None
            self.assertEqual({"role": "codex", "model": expected_model, "state": "active"},
                             declared["architect"])
            observed = []
            for arm in ("1", "10"):
                root = self.root(family, arm)
                registry = Registry(root)
                self.assertEqual(("architect", "scale-worker"), registry.active_node_ids())
                backend = RegistryAdminBackend(root)
                assignments = {}
                for node in declared:
                    current = backend.active_assignment(node)
                    assignments[node] = {field: current[field] for field in ("role", "model", "state")}
                self.assertEqual(declared, assignments)
                observed.append(assignments)
            self.assertEqual(observed[0], observed[1])

    def test_selected_builder_inbox_stays_empty_without_delivery_or_ack(self):
        for family in FAMILIES:
            for arm in ("1", "10"):
                root = self.root(family, arm)
                self.assertEqual([], EventLog(root).unacked_ids("scale-worker"))
                for relative in ("receipts/deliveries/scale-worker.jsonl", "receipts/acks/scale-worker.jsonl"):
                    path = root.tenant_home / relative
                    self.assertTrue(not path.exists() or path.read_bytes() == b"")

    def test_event_history_is_only_unique_unselected_native_envelopes(self):
        for arm, count in zip(("1", "10"), seed_counts()):
            rows = self.rows("events", arm)
            validate_event_records(rows)
            self.assertEqual({"message_envelope"}, {row["kind"] for row in rows})
            self.assertEqual({"architect"}, {row["recipient"] for row in rows})
            self.assertEqual({"scale-worker"}, {row["sender"] for row in rows})
            self.assertTrue(all(row["attempt_binding"] == "absent_legacy" for row in rows))
            self.assertTrue(all("worker_session_id" not in row and "reply_to" not in row for row in rows))
            self.assertEqual(count, len(EventLog(self.root("events", arm)).unacked_ids("architect")))

    def test_registry_history_preserves_complete_native_switch_pair_ancestry(self):
        for arm in ("1", "10"):
            rows = self.rows("registry", arm)
            self.assertEqual(["registry_entry", "registry_entry"], [row["kind"] for row in rows[:2]])
            self.assertEqual({"scale-worker", "architect"}, {row["node_id"] for row in rows[:2]})
            previous = next(row for row in rows[:2] if row["node_id"] == "architect")
            model = None
            for offset in range(2, len(rows), 2):
                replacement, receipt = rows[offset:offset + 2]
                self.assertEqual(["registry_entry", "provider_switch_receipt"],
                                 [replacement["kind"], receipt["kind"]])
                self.assertEqual("architect", replacement["node_id"])
                self.assertEqual("architect", receipt["node_id"])
                self.assertEqual(previous["id"], receipt["previous_registry_entry_id"])
                self.assertEqual(replacement["id"], receipt["registry_entry_id"])
                self.assertEqual(previous["role"], receipt["previous_harness"])
                self.assertEqual(model, receipt["previous_model"])
                self.assertEqual("codex", replacement["role"])
                self.assertEqual("active", replacement["state"])
                self.assertEqual("codex", receipt["harness"])
                wanted = "scale-model-a" if model != "scale-model-a" else "scale-model-b"
                self.assertEqual(wanted, receipt["model"])
                self.assertEqual(replacement["timestamp"], receipt["timestamp"])
                previous, model = replacement, wanted
            self.assertEqual("scale-model-a", model)

    def test_exhaustion_history_has_unique_native_sessions_and_correct_node(self):
        for arm, count in zip(("1", "10"), seed_counts()):
            rows = self.rows("exhaustion", arm)
            self.assertEqual({"codex_wait_exhaustion_receipt"}, {row["kind"] for row in rows})
            self.assertEqual({"scale-worker"}, {row["node_id"] for row in rows})
            self.assertEqual({"consent_reopened"}, {row["outcome"] for row in rows})
            self.assertEqual(1, len({row["waited_seconds"] for row in rows}))
            self.assertEqual(count, len({row["session_digest"] for row in rows}))

    def test_delivery_history_repeats_one_real_envelope_with_native_counts(self):
        for arm, count in zip(("1", "10"), seed_counts()):
            root = self.root("deliveries", arm)
            events = EventLog(root).event_records()
            validate_event_records(events)
            envelopes = [row for row in events if row["kind"] == "message_envelope"]
            self.assertEqual(1, len(envelopes))
            self.assertEqual("architect", envelopes[0]["recipient"])
            rows = self.rows("deliveries", arm)
            self.assertEqual({"delivered_unpresented"}, {row["kind"] for row in rows})
            for row in rows:
                self.assertIs(False, row["presenter_evidence"]["shown"])
                self.assertEqual("no_live_presenter_evidence", row["presenter_evidence"]["reason"])
                self.assertTrue(row["presenter_evidence"]["sources"])
            self.assertEqual({"architect"}, {row["recipient"] for row in rows})
            self.assertEqual([[envelopes[0]["id"]]] * count, [row["item_ids"] for row in rows])
            self.assertEqual(list(range(1, count + 1)), [row["presentation_count"] for row in rows])
            self.assertEqual([envelopes[0]["id"]], EventLog(root).unacked_ids("architect"))
            ack = root.tenant_home / "receipts/acks/architect.jsonl"
            self.assertTrue(not ack.exists() or ack.read_bytes() == b"")

    def test_history_ids_and_keys_are_unique_and_fixed_width_per_kind(self):
        for family in FAMILIES:
            rows = self.rows(family, "10")
            self.assertEqual(len(rows), len({row["id"] for row in rows}))
            grouped = defaultdict(list)
            for row in rows:
                grouped[row["kind"]].append(row)
            for kind, group in grouped.items():
                with self.subTest(family=family, kind=kind):
                    self.assertEqual(1, len({len(row["id"]) for row in group}))
                    if "idempotency_key" in group[0]:
                        keys = [row["idempotency_key"] for row in group]
                        self.assertEqual(len(keys), len(set(keys)))
                        self.assertEqual(1, len({len(key) for key in keys}))


class FixtureSourceLifetimeTests(unittest.TestCase):
    def test_shared_source_dependency_is_stable_until_class_release_and_refuses_drift(self):
        from floati import send_prefix
        from tests.bus_scale_test_profile import stable_fixture_sources

        # This control specifies the bounded profile even when explicitly
        # selected by a caller that is retaining full-scale native controls.
        bounded = patch("tests.bus_scale_test_profile.full_scale_enabled", return_value=False)
        bounded.start()
        self.addCleanup(bounded.stop)
        cleanups = []

        class Owner:
            addClassCleanup = staticmethod(cleanups.append)

        current = ["first-source"]

        def source():
            return current[0]

        with patch.object(send_prefix, "_revision", source):
            with stable_fixture_sources(owner=Owner):
                self.assertEqual("first-source", send_prefix._revision())
            current[0] = "changed-source"
            # Borrowing the same class keeps one dependency. The eventual
            # release must reject drift instead of silently changing which
            # implementation its shared fixture claims to exercise.
            with stable_fixture_sources(owner=Owner):
                self.assertEqual("first-source", send_prefix._revision())

        replacement = lambda: "replacement-source"
        with patch.object(send_prefix, "_revision", replacement):
            with stable_fixture_sources(owner=Owner):
                self.assertEqual("replacement-source", send_prefix._revision(),
                    "a replaced dependency must not inherit the earlier callable's fixture")

        failures = []
        for cleanup in reversed(cleanups):
            try:
                cleanup()
            except AssertionError as error:
                failures.append(type(error))
        self.assertEqual([AssertionError], failures,
            "only the changed source dependency may fail its release")
        # A class cleanup and the receipt runner may release the same owner.
        for cleanup in cleanups:
            cleanup()


if __name__ == "__main__":
    unittest.main()
