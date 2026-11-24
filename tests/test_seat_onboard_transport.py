"""Preparation must enforce its installed-transport prerequisite before writes."""
import json
import unittest
from pathlib import Path
from unittest import mock

from tests import test_seat_onboard as fixtures


class SeatOnboardTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SeatOnboardingTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_missing_selected_install_refuses_before_ownership_writes(self):
        fixture = self.fixture
        document = json.loads(fixture.profiles.read_bytes())
        missing = fixture.base / "absent-install"
        document["transports"]["fixture"]["install_root"] = str(missing)
        document["transports"]["fixture"]["manifest_path"] = str(missing / ".floati-install/manifest.v0.json")
        fixture.profiles.write_text(json.dumps(document))
        paths = (fixture.profiles, fixture.config, fixture.root.path / "registry/entries.jsonl")
        before = {path: path.read_bytes() for path in paths}
        code, output, errors = fixture.add()
        self.assertEqual(20, code, errors or output)
        self.assertIn("transport", json.loads(output)["evidence"]["detail"].lower())
        self.assertFalse(fixture.home.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in paths})
        self.assertFalse(any((fixture.root.path / "state/seat-onboarding").glob("*.json")))

    def test_ready_replay_rechecks_installed_transport_without_repair(self):
        fixture = self.fixture
        code, output, errors = fixture.add()
        self.assertEqual(0, code, errors or output)
        document = json.loads(fixture.profiles.read_bytes())
        install = Path(document["transports"]["fixture"]["install_root"])
        install.mkdir(exist_ok=True)
        client = install / "floati/cli.py"
        client.parent.mkdir(exist_ok=True)
        client.write_text("changed installed client\n")
        before = {path: path.read_bytes() for path in (fixture.profiles, fixture.config)}
        code, output, errors = fixture.add()
        self.assertEqual(20, code, errors or output)
        self.assertTrue(fixture.home.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_broken_transport_does_not_block_owned_seat_removal(self):
        fixture = self.fixture
        before = {path: path.read_bytes() for path in (fixture.profiles, fixture.config)}
        code, output, errors = fixture.add()
        self.assertEqual(0, code, errors or output)
        document = json.loads(fixture.profiles.read_bytes())
        Path(document["transports"]["fixture"]["manifest_path"]).unlink()
        code, output, errors = fixture.cli("seat", "remove", "--root", str(fixture.root.path),
            "--home", str(fixture.home))
        self.assertEqual(0, code, errors or output)
        self.assertFalse(fixture.home.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_transport_changed_during_preparation_cannot_publish_ready(self):
        from floati import seat_harness_config
        fixture = self.fixture
        apply = seat_harness_config.apply
        def apply_then_break(plan):
            result = apply(plan)
            document = json.loads(fixture.profiles.read_bytes())
            client = Path(document["transports"]["fixture"]["install_root"]) / "floati/cli.py"
            client.write_text("changed while preparing\n")
            return result
        with mock.patch.object(seat_harness_config, "apply", side_effect=apply_then_break):
            code, output, errors = fixture.add()
        self.assertEqual(20, code, errors or output)
        records = list((fixture.root.path / "state/seat-onboarding").glob("*.json"))
        self.assertEqual(1, len(records))
        self.assertEqual("failed", json.loads(records[0].read_bytes())["state"])
        code, output, errors = fixture.cli("seat", "remove", "--root", str(fixture.root.path),
            "--home", str(fixture.home))
        self.assertEqual(0, code, errors or output)
        self.assertFalse(fixture.home.exists())
