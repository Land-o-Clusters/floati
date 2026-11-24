"""Native readiness clone coordinates must not hide other testimony."""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path
import unittest

from floati.mail_health import RecipientReadiness
from floati.registry import Registry
from floati.root import FloatiRoot
from tests import bus_scale_integration
from tests.temp_roots import REAL_TEMP_ROOT


class ReadinessEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.normalize = getattr(bus_scale_integration, "normalize_readiness", None)
        self.assertTrue(callable(self.normalize), "missing native readiness equivalence")
        scratch = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-")
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name).resolve()
        self.homes, self.artifacts = [], []
        for mode in ("cpu", "diagnostic"):
            home = self.base / mode / "scale-fleet"
            root = FloatiRoot.open_direct_home(home, create=True)
            Registry(root).register("scale-worker", "codex")
            self.homes.append(home)
            self.artifacts.append(RecipientReadiness.observe(root, "scale-worker",
                observed_at="2026-10-09T00:00:00.000Z").artifact())

    def test_complete_native_observation_has_equal_audited_clone_normal_form(self):
        saved = copy.deepcopy(self.artifacts)
        normal = [self.normalize(home, artifact) for home, artifact in zip(self.homes, self.artifacts)]
        self.assertEqual(normal[0]["artifact"], normal[1]["artifact"])
        for result in normal:
            self.assertEqual({"artifact", "normalizations"}, set(result))
            self.assertEqual({"/mechanism_evidence/0/sources/0/path",
                "/mechanism_evidence/2/sources/0/path"}, {row["pointer"] for row in result["normalizations"]})
        self.assertEqual(saved, self.artifacts)

    def test_foreign_source_and_native_semantic_changes_are_rejected(self):
        for defect in ("source", "reason", "clock", "record"):
            artifact = copy.deepcopy(self.artifacts[0])
            if defect == "source":
                artifact["mechanism_evidence"][0]["sources"][0]["path"] = str(self.homes[1] / "registry/entries.jsonl")
            elif defect == "record":
                artifact["mechanism_evidence"][0]["sources"][0]["record_id"] = "foreign"
            else:
                artifact["reason" if defect == "reason" else "observed_at"] = "foreign"
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                self.normalize(self.homes[0], artifact)

    def test_unknown_literal_testimony_is_preserved_or_refused(self):
        artifact = copy.deepcopy(self.artifacts[0])
        artifact["literal"] = {"path": str(self.homes[0]), "timestamp": "literal"}
        try:
            result = self.normalize(self.homes[0], artifact)
        except ValueError:
            return
        self.assertEqual(artifact["literal"], result["artifact"]["literal"])

    def test_normalization_does_not_change_fixture_bytes_or_modes(self):
        inventory = lambda: {str(path): (path.stat().st_mode, path.read_bytes())
            for home in self.homes for path in home.rglob("*") if path.is_file()}
        before = inventory()
        self.normalize(self.homes[0], self.artifacts[0])
        self.assertEqual(before, inventory())
