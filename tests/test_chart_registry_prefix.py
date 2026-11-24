"""Chart cost follows live membership, with sealed all-history activity."""
from __future__ import annotations

import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from floati import jsonl, registry_prefix, send_prefix
from floati.errors import IntegrityFailure
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.multi_bus_chart import MultiBusHarborChart
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from tests.test_multi_bus_chart import NOW
from tests.bus_scale_test_profile import full_scale_enabled


class ChartRegistryPrefixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.registry = Registry(self.root)
        self.row = self.registry.register("architect", "Architect")
        self.path = self.root.resolve_relative("registry/entries.jsonl")
        self.bank = self.root.tenant_home / ".registry-prefix"
        self.declared = self.base / "declared.json"
        self.declared.write_text(json.dumps({"schema_version": 0, "roots": [{
            "bus_id": "fixture", "root": str(self.root.path),
            "architect_node": "architect", "downstream": []}]}))

    def append(self, rows):
        jsonl.transact_records(self.root, "registry/entries.jsonl",
            lambda _prior: (None, rows), allowed_kinds=REGISTRY_KINDS)

    def row_at(self, node, timestamp, *, state="active"):
        return dict(self.row, id="registry-" + uuid7_hex(), node_id=node,
                    timestamp=timestamp, state=state)

    def chart(self, *, native=False):
        chart = MultiBusHarborChart(self.declared, now=NOW)
        if native:
            with patch.object(registry_prefix, "_open", return_value=None):
                return chart.to_json()
        return chart.to_json()

    def catalog(self):
        reader = getattr(registry_prefix, "read_catalog", None)
        self.assertTrue(callable(reader), "chart needs the ruled locked catalog reader")
        return reader(self.root)

    def test_fold_keeps_overwritten_backdated_and_tied_timestamp_text(self):
        later = "2099-10-25T00:00:00.000Z"
        tied = "2099-10-25T00:00:00Z"
        self.append([self.row_at("history", later), self.row_at("history", tied),
                     self.row_at("history", "2020-01-01T00:00:00Z", state="retired")])
        registry_prefix.warm(self.root)
        payload = json.loads((self.bank / "checkpoint.json").read_bytes())["payload"]
        self.assertEqual({"max_timestamp": tied}, payload.get("folds"),
                         "timestamp fold must span every row, including overwritten ones")
        self.assertEqual({"max_timestamp": "chart-time-text-v1"}, payload["binding"]["folds"])
        self.assertEqual("registry-prefix-v2", payload["binding"]["format"])
        prefix = registry_prefix.RegistryPrefix(self.root)
        prefix.load()
        self.assertEqual(["architect"], prefix.get("catalog", "active"))

    def test_random_backdated_histories_match_native_chart_byte_for_byte(self):
        generator = random.Random(271828)
        for _batch in range(5):
            self.append([self.row_at("node-" + str(generator.randrange(5)),
                "%04d-01-01T00:00:00.123Z" % generator.randrange(2020, 2100),
                state=generator.choice(("active", "retired"))) for _ in range(8)])
            expected = self.chart(native=True)
            self.assertEqual(expected, self.chart())
        self.assertTrue((self.bank / "checkpoint.json").is_file(),
                        "equivalence alone must not pass by replaying native history")

    def test_warm_chart_reads_zero_registry_bytes_after_constant_population_transitions(self):
        self.append([self.row_at("history", "2020-01-01T00:00:00Z")])
        registry_prefix.warm(self.root)
        for count in (2, 20):
            rows = []
            for _ in range(count):
                rows.extend([self.row_at("history", "2020-01-02T00:00:00Z", state="retired"),
                             self.row_at("history", "2020-01-01T00:00:00Z")])
            self.append(rows)
            expected = self.chart(native=True)
            native_open = Path.open
            def opening(path, mode="r", *args, **kwargs):
                if path == self.path and mode == "rb":
                    self.fail("warm chart replayed registry history")
                return native_open(path, mode, *args, **kwargs)
            with patch.object(Path, "open", opening):
                self.assertEqual(expected, self.chart())

    def test_old_frame_and_checkpoint_fold_perturbations_recover_native_truth(self):
        self.append([self.row_at("history", "2099-01-01T00:00:00Z"),
                     self.row_at("history", "2020-01-01T00:00:00Z", state="retired")])
        registry_prefix.warm(self.root)
        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(b"2099-01-01", b"2098-01-01", 1))
        self.assertEqual(self.chart(native=True), self.chart())
        checkpoint = self.bank / "checkpoint.json"
        value = json.loads(checkpoint.read_bytes())
        self.assertIn("folds", value["payload"], "fold must be inside the sealed checkpoint")
        value["payload"]["folds"]["max_timestamp"] = "2000-01-01T00:00:00Z"
        checkpoint.write_bytes(send_prefix._bytes(value))
        self.assertEqual(self.chart(native=True), self.chart())
        prefix = registry_prefix.RegistryPrefix(self.root)
        prefix.load()
        self.assertEqual("2098-01-01T00:00:00Z", prefix.payload["folds"]["max_timestamp"])

    def test_catalog_falls_back_for_a_symlinked_bank(self):
        outside = self.base / "foreign"
        outside.mkdir()
        self.bank.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.chart(native=True), self.chart())
        self.catalog()
        self.assertEqual([], list(outside.iterdir()))

    def test_foreign_device_bank_falls_back_before_creating_any_descendant(self):
        self.bank.mkdir(mode=0o700)
        target = self.bank.stat()
        expected = self.chart(native=True)
        native_fstat = os.fstat
        def foreign_fstat(descriptor):
            info = native_fstat(descriptor)
            if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                fields = list(info)
                fields[2] += 1
                return os.stat_result(fields)
            return info
        with patch("floati.key_prefix.os.fstat", foreign_fstat):
            self.assertEqual(expected, self.chart())
        self.assertEqual([], list(self.bank.iterdir()))

    def test_invalid_old_timestamp_preserves_native_typed_refusal(self):
        row = self.row_at("history", "2020-01-01T00:00:00Z")
        self.append([row])
        registry_prefix.warm(self.root)
        self.path.write_bytes(self.path.read_bytes().replace(
            encode_frame(row), encode_frame(dict(row, timestamp="invalid"))))
        for native in (True, False):
            with self.subTest(native=native):
                with self.assertRaises(IntegrityFailure) as caught:
                    self.chart(native=native)
                self.assertEqual("timestamp_invalid", caught.exception.code)

    def test_missing_or_undeclared_fold_keys_force_a_native_rebuild(self):
        import hashlib
        import hmac
        registry_prefix.warm(self.root)
        checkpoint = self.bank / "checkpoint.json"
        for folds in ({}, {"max_timestamp": self.row["timestamp"], "undeclared": None}):
            value = json.loads(checkpoint.read_bytes())
            value["payload"]["folds"] = folds
            value["seal"] = hmac.new((self.bank / "seal.key").read_bytes(),
                send_prefix._bytes(value["payload"]), hashlib.sha256).hexdigest()
            checkpoint.write_bytes(send_prefix._bytes(value))
            with self.assertRaises(send_prefix.CacheMiss):
                registry_prefix.RegistryPrefix(self.root).load()
            self.assertEqual(self.chart(native=True), self.chart())

    def test_timestamp_fold_advances_with_nonregistration_rows_in_the_same_seal(self):
        registry_prefix.warm(self.root)
        lease = {"schema_version": 0, "id": "lease-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": "2099-01-01T00:00:00Z",
            "kind": "node_lease", "node_id": "architect",
            "workspace": str(self.root.path / "nodes/architect"),
            "state": "active", "expires_at": "2099-01-02T00:00:00Z"}
        self.append([lease])
        prefix = registry_prefix.RegistryPrefix(self.root)
        prefix.load()
        self.assertEqual({"max_timestamp": lease["timestamp"]}, prefix.payload["folds"])
        self.assertEqual(self.path.stat().st_size, prefix.payload["offset"])
        self.assertEqual(2, prefix.payload["count"])
        self.assertEqual(self.chart(native=True), self.chart())

    def test_catalog_entry_bound_misses_toward_full_read_at_4883_maximal_ids(self):
        if not full_scale_enabled():
            self._bounded_catalog_entry_limit()
            return
        prefix = registry_prefix.RegistryPrefix(self.root)
        prefix.rebuild()
        key = send_prefix._key("catalog", "active")
        nodes = ["n%063d" % index for index in range(4883)]
        self.assertEqual(327626, len(prefix._entry_bytes(key, nodes[:4882])))
        with self.assertRaises(send_prefix.CacheMiss):
            prefix._entry_bytes(key, nodes)
        # This fleet is lawful under MAX_LEDGER_RECORDS. The real reader must
        # return the complete native catalog when its sealed entry cannot fit.
        self.append([self.row_at(node, "2020-01-01T00:00:00Z") for node in nodes])
        rows, maximum = self.catalog()
        self.assertIsNone(maximum, "an oversized catalog must take the full-read branch")
        self.assertEqual(4884, len(rows))
        self.assertEqual(self.chart(native=True), self.chart())

    def _bounded_catalog_entry_limit(self):
        # Same entry-size and full-read branches, with a small explicit native
        # byte budget. Vector 5 retains the real 327,680-byte/4,883-ID boundary.
        prefix = registry_prefix.RegistryPrefix(self.root)
        prefix.rebuild()
        key = send_prefix._key("catalog", "active")
        nodes = ["n%063d" % index for index in range(32)]
        under = prefix._entry_bytes(key, nodes[:-1])
        limit = (len(under) + 4) // 5
        rows = [self.row_at(node, "2020-01-01T00:00:00Z") for node in nodes]
        self.assertTrue(all(len(encode_frame(row)) - 1 <= limit for row in rows))
        with patch.object(jsonl, "MAX_RECORD_BYTES", limit):
            prefix = registry_prefix.RegistryPrefix(self.root)
            prefix.rebuild()
            self.assertEqual(len(under), len(prefix._entry_bytes(key, nodes[:-1])))
            with self.assertRaises(send_prefix.CacheMiss):
                prefix._entry_bytes(key, nodes)
            original, maximum = self.catalog()
            self.assertEqual([self.row], original)
            self.assertIsNotNone(maximum, "small valid catalog must use its sealed bank")
            self.append(rows)
            actual, maximum = self.catalog()
            self.assertIsNone(maximum, "oversized entry must fall back to complete native history")
            self.assertEqual(len(nodes) + 1, len(actual))
            self.assertEqual(self.chart(native=True), self.chart())
