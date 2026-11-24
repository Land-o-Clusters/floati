"""FLOATI-PROXY-AWARENESS-1: a loopback model proxy is typed doctor knowledge.

Awareness only: the finding names sources and reduced URLs, and never carries
key material. These tests plant harness configs under the fixture's patched
HOME and read doctor's artifact; nothing here touches a real user home.
"""
from __future__ import annotations

import tests  # Pin HOME before importing fixture or product code.

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_doctor as fixtures

LOOPBACK = "http://127.0.0.1:8317"
REMOTE = "https://api.z.ai/api/anthropic"
URL_ENV_VARS = ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "ZCODE_BASE_URL")


class ModelProxyAwarenessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DoctorContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = Path(os.environ["HOME"])
        self.home.mkdir(parents=True, exist_ok=True)
        # Deterministic baseline: the runner's own URL env must not leak in.
        env_patch = patch.dict(os.environ, {var: "" for var in URL_ENV_VARS})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def plant_claude(self, payload):
        settings = self.home / ".claude"
        settings.mkdir(parents=True, exist_ok=True)
        (settings / "settings.json").write_text(json.dumps(payload), encoding="utf-8")

    def plant_zcode(self, base_url, name="someproxy", extra=None):
        providers = {} if extra is None else dict(extra)
        providers[name] = {
            "kind": "anthropic",
            "options": {"baseURL": base_url, "apiKey": "sk-never-emit"},
        }
        config = self.home / ".zcode" / "cli"
        config.mkdir(parents=True, exist_ok=True)
        (config / "config.json").write_text(json.dumps({
            "provider": providers,
        }), encoding="utf-8")

    def rows(self, artifact):
        return [r for r in artifact["findings"] if r["code"] == "model_proxy_detected"]

    def artifact(self):
        try:
            return self.fixture.doctor(no_sandbox=True).artifact()
        except Exception as exc:  # noqa: BLE001 - the assertion IS the message
            self.fail(f"doctor did not produce an artifact: {exc!r}")

    def test_loopback_base_url_in_harness_config_is_a_typed_finding(self):
        """RED: today doctor says nothing when a harness points at a loopback proxy."""
        self.plant_claude({"env": {"ANTHROPIC_BASE_URL": LOOPBACK}})
        artifact, _rc = self.artifact()
        rows = self.rows(artifact)
        self.assertEqual(1, len(rows), "the loopback proxy must be one typed finding")
        self.assertEqual("info", rows[0]["severity"])
        self.assertEqual(1, rows[0]["proxy_awareness"]["classification"])
        self.assertIn("127.0.0.1:8317", rows[0]["detail"])

    def test_two_loopback_sources_classify_two(self):
        self.plant_claude({"env": {"ANTHROPIC_BASE_URL": LOOPBACK}})
        self.plant_zcode("http://localhost:8317")
        artifact, _rc = self.artifact()
        rows = self.rows(artifact)
        self.assertEqual(1, len(rows), "still exactly one typed line")
        self.assertEqual(2, rows[0]["proxy_awareness"]["classification"])

    def test_no_proxy_stays_silent(self):
        artifact, _rc = self.artifact()
        self.assertEqual([], self.rows(artifact))

    def test_remote_base_url_stays_silent(self):
        self.plant_claude({"env": {"ANTHROPIC_BASE_URL": REMOTE}})
        artifact, _rc = self.artifact()
        self.assertEqual([], self.rows(artifact))

    def test_key_material_never_reaches_the_artifact(self):
        self.plant_claude({"env": {
            "ANTHROPIC_BASE_URL": LOOPBACK,
            "ANTHROPIC_API_KEY": "sk-super-secret-value",
        }})
        self.plant_zcode(LOOPBACK)
        artifact, _rc = self.artifact()
        self.assertEqual(1, len(self.rows(artifact)))
        self.assertNotIn("sk-super-secret-value", json.dumps(artifact))
        self.assertNotIn("sk-never-emit", json.dumps(artifact))

    def test_planted_provider_name_never_reaches_the_artifact(self):
        """PRIORITY AMENDMENT RED (2026-10-11, OPUS-RULED): a secret planted
        AS the zcode provider NAME reaches the finding's source label today.
        No user-controlled config text reaches doctor output: the source
        label is built from a fixed vocabulary only (harness + config file
        kind + ordinal), never the provider key or any other free-text
        field."""
        token = "sk-secret-as-provider-name-9f2b7c"
        self.plant_zcode(LOOPBACK, name=token)
        artifact, _rc = self.artifact()
        rows = self.rows(artifact)
        self.assertEqual(1, len(rows), artifact)
        self.assertNotIn(token, json.dumps(artifact), rows)

    def test_source_labels_are_fixed_vocabulary_ordinals(self):
        """The label names the harness, the config file kind and the
        provider's sorted-key ordinal - here the ruling's own example, the
        SECOND provider - and never either provider's name."""
        quiet = {"kind": "anthropic", "options": {"baseURL": REMOTE}}
        self.plant_zcode(LOOPBACK, name="beta-name", extra={"alpha-name": quiet})
        artifact, _rc = self.artifact()
        rows = self.rows(artifact)
        self.assertEqual(1, len(rows), artifact)
        rendered = json.dumps(artifact)
        self.assertNotIn("alpha-name", rendered, rows)
        self.assertNotIn("beta-name", rendered, rows)
        sources = rows[0]["proxy_awareness"]["sources"]
        self.assertEqual(["zcode provider 2"], [s["source"] for s in sources], rows)

    def test_malformed_config_stays_readable_doctor(self):
        settings = self.home / ".claude"
        settings.mkdir(parents=True, exist_ok=True)
        (settings / "settings.json").write_text("{ not json", encoding="utf-8")
        artifact, _rc = self.artifact()
        self.assertEqual([], self.rows(artifact))


if __name__ == "__main__":
    unittest.main()
