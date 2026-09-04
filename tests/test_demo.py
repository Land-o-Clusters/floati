from __future__ import annotations

from tests.test_cli import LAUNCHER

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.errors import ProtocolRefusal


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class DemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "synthetic-fleet"

    def test_seeded_model_spans_plane_work_receipt_and_refusal_states(self) -> None:
        from floati.demo import build_demo_model, seed_demo

        root = seed_demo(self.home)
        model = build_demo_model(root)
        nodes = {node["node_id"]: node for node in model.nodes}

        self.assertEqual({"builder-review", "builder-app", "builder-core"}, set(nodes))
        self.assertEqual({"present", "silent", "expired"}, {node["liveness"] for node in nodes.values()})
        self.assertEqual({"active", "expired", "none"}, {node["authority"] for node in nodes.values()})
        self.assertEqual({"active", "expired", "none"}, {node["mutex"] for node in nodes.values()})
        self.assertEqual({"open", "claimed", "completed"}, {item["state"] for item in model.work_items})
        self.assertTrue(model.deliveries)
        self.assertTrue(model.acknowledgments)
        self.assertTrue(model.denials)
        self.assertTrue(any(node.get("visible_message_id") for node in model.nodes))
        self.assertEqual(
            {"claim", "driving", "degraded", "complete"},
            {worker["state"] for worker in model.workers},
        )

    def test_demo_refuses_nonempty_root_without_mutating_existing_content(self) -> None:
        from floati.demo import seed_demo

        self.home.mkdir()
        marker = self.home / "owner-data"
        marker.write_text("keep\n", encoding="utf-8")

        with self.assertRaises(ProtocolRefusal) as caught:
            seed_demo(self.home)

        self.assertEqual("demo_root_not_empty", caught.exception.code)
        self.assertEqual("keep\n", marker.read_text(encoding="utf-8"))

    def test_capture_is_deterministic_and_monochrome_is_fully_legible(self) -> None:
        from floati.demo import capture_demo

        first = capture_demo(color=False)
        second = capture_demo(color=False)
        color = capture_demo(color=True)

        self.assertEqual(first, second)
        self.assertNotIn("\x1b", first)
        self.assertIn("LIVE", first)
        self.assertIn("AUTH", first)
        self.assertIn("MUTEX", first)
        self.assertIn("\x1b[38;5;208m", color)

    def test_harbor_map_demo_uses_role_neutral_fictional_identifiers(self) -> None:
        """Catches the public demo reusing a private fleet or architect coordinate."""
        from floati.demo import harbor_map_demo_artifact

        artifact = harbor_map_demo_artifact(include_envelope=True)
        primary = artifact["buses"][0]

        self.assertEqual("demo-fleet", primary["bus_id"])
        self.assertEqual("demo-architect", primary["architect_node"])
        self.assertEqual("demo-architect", primary["nodes"][0]["id"])
        self.assertEqual("builder-core", primary["nodes"][1]["id"])
        self.assertEqual("demo-fleet", artifact["relationships"][0]["source"])
        self.assertEqual("demo-fleet", artifact["envelopes"][0]["source_bus"])

    def test_explicit_demo_capture_color_overrides_no_color_policy(self) -> None:
        """Catches NO_COLOR overriding an explicit color or monochrome capture flag."""
        from floati.demo import capture_demo

        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            color = capture_demo(color=True)
            monochrome = capture_demo(color=False)

        self.assertEqual(
            "3f66217becf2dad76d3556195ed2e16370eeb018b8456c091beb9b5405bcaecd",
            hashlib.sha256(color.encode("utf-8")).hexdigest(),
        )
        self.assertIn("\x1b[38;5;208m", color)
        self.assertNotIn("\x1b", monochrome)

    def test_make_demo_and_cli_demo_use_throwaway_plain_path_when_not_a_tty(self) -> None:
        make = subprocess.run(
            ["make", "demo"], cwd=REPOSITORY_ROOT,
            check=False, capture_output=True, text=True,
        )
        cli = subprocess.run(
            [str(LAUNCHER), "board", "--demo", "--no-animation"],
            cwd=REPOSITORY_ROOT, check=False, capture_output=True, text=True,
        )

        self.assertEqual(0, make.returncode, make.stderr)
        self.assertEqual(0, cli.returncode, cli.stderr)
        self.assertTrue(make.stdout.startswith("PLAIN DUMP\n"))
        self.assertTrue(cli.stdout.startswith("PLAIN DUMP\n"))
        self.assertIn("! DENIAL", cli.stdout)

    def test_demo_loader_reprojects_the_root_after_visible_ack(self) -> None:
        from floati.demo import demo_model_loader, seed_demo
        from floati.tui import BoardAction, acknowledge_visible

        root = seed_demo(self.home)
        loader = demo_model_loader(root)
        before = loader()
        node = next(node for node in before.nodes if node.get("visible_message_id"))
        prior_ack_count = len(before.acknowledgments)

        acknowledge_visible(
            root,
            BoardAction("ack", str(node["node_id"]), str(node["visible_message_id"])),
            acting_session_id="demo-test-session",
        )
        after = loader()

        refreshed = next(item for item in after.nodes if item["node_id"] == node["node_id"])
        self.assertIsNone(refreshed.get("visible_message_id"))
        self.assertEqual(prior_ack_count + 1, len(after.acknowledgments))


if __name__ == "__main__":
    unittest.main()
