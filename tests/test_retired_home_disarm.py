"""F2-RETIRED-HOME-DISARM-1: moving or retiring a seat disarms the hooks it leaves behind.

Board Am.5's measurement: the 09-22 re-home left the retired home's
``.cursor/hooks.json`` armed as the node for four days, and the zombie woke
on every envelope. At the fix, ``seat migrate`` and node retirement find
every hook config naming the node on this bus root outside the new home and
disarm it with a receipt — never deleting it. Every fixture here is a
scratch directory; nothing reaches a real harness home.
"""
import json
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as ids
from floati.cursor_wait import format_cursor_stop_command
from floati.jsonl import read_records_snapshot
from floati.records import validate_record
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


RECEIPT_KIND = "retired_home_disarm_receipt"


class RetiredHomeDisarmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bus_home = self.base / "demo-fleet"
        self.root = FloatiRoot.open_direct_home(self.bus_home, create=True)
        self.node = ids.builder("floati")
        Registry(self.root).register(self.node, "worker")
        Registry(self.root).register("architect", "architect")
        self.workspace = self.base / "new-home"
        self.workspace.mkdir()
        self.old_home = self.base / "retired-home"
        (self.old_home / ".cursor").mkdir(parents=True)
        self.zombie_command = format_cursor_stop_command(
            runtime=self.base / "runtime",
            root=self.root.path,
            node=self.node,
            deadline_seconds=10,
        )
        self.foreign_command = "/usr/bin/true  # some other project's hook"
        self.write_cursor_hooks(
            self.old_home,
            [
                {"command": self.zombie_command, "timeout": 30, "loop_limit": 8},
                {"command": self.foreign_command, "timeout": 5},
            ],
        )
        self.write_map(
            [
                {"workspace": str(self.workspace), "node_id": self.node},
                {"workspace": str(self.old_home), "node_id": self.node},
            ]
        )

    def write_map(self, mappings: list[dict[str, str]]) -> None:
        path = self.bus_home / "codex-wait" / "workspaces.v0.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "tenant_id": self.bus_home.name,
                    "mappings": mappings,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    def write_cursor_hooks(self, home: Path, stop_entries: list[dict]) -> None:
        path = home / ".cursor" / "hooks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"version": 1, "hooks": {"stop": stop_entries}}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )

    def read_cursor_stops(self, home: Path) -> list[dict]:
        path = home / ".cursor" / "hooks.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        return document["hooks"]["stop"]

    def disarm_rows(self) -> list[dict]:
        return read_records_snapshot(
            self.root,
            "receipts/retired-home-disarm/" + self.node + ".jsonl",
            allowed_kinds={RECEIPT_KIND},
        )

    def cli(self, *argv: str) -> tuple[int, str]:
        import contextlib
        import io
        from floati.cli import main

        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                code = main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
        return code, output.getvalue()

    def migrate(self, key: str) -> tuple[int, str]:
        return self.cli(
            "seat", "migrate",
            "--root", str(self.root.path),
            "--as", self.node,
            "--workspace", str(self.workspace),
            "--idempotency-key", key,
            "--coordinator", "architect",
        )

    def test_migrate_disarms_the_hook_config_outside_the_new_home(self) -> None:
        code, output = self.migrate("disarm-migrate-1")
        self.assertEqual(0, code, output)

        # The old config is DISARMED, never deleted: the file and the
        # foreign hook survive, the entry naming this node is gone.
        self.assertTrue(
            (self.old_home / ".cursor" / "hooks.json").is_file(),
            "the retired home's hooks.json must never be deleted")
        stops = self.read_cursor_stops(self.old_home)
        self.assertEqual([self.foreign_command], [row["command"] for row in stops])

        # The act is receipted on this bus root, against the node.
        rows = self.disarm_rows()
        self.assertTrue(rows, "the disarm must carry its own receipt")
        row = rows[-1]
        self.assertEqual(RECEIPT_KIND, row["kind"])
        self.assertEqual(self.node, row["node_id"])
        disarmed = row["disarmed"]
        self.assertEqual(1, len(disarmed))
        self.assertEqual(
            str(self.old_home / ".cursor" / "hooks.json"), disarmed[0]["config"])
        self.assertEqual(1, disarmed[0]["removed"])
        validate_record(row, row["tenant_id"], {RECEIPT_KIND}, integrity=True)

    def test_migrate_leaves_the_new_home_armed(self) -> None:
        self.write_cursor_hooks(
            self.workspace,
            [{"command": self.zombie_command, "timeout": 30, "loop_limit": 8}],
        )
        code, output = self.migrate("disarm-migrate-2")
        self.assertEqual(0, code, output)
        stops = self.read_cursor_stops(self.workspace)
        self.assertEqual(1, len(stops), "the new home's own hook stays armed")
        self.assertEqual(self.zombie_command, stops[0]["command"])
        for row in self.disarm_rows():
            for entry in row["disarmed"]:
                self.assertNotEqual(
                    str(self.workspace / ".cursor" / "hooks.json"),
                    entry["config"],
                    "the new home is never disarmed by its own migration",
                )

    def test_retire_disarms_every_hook_config_the_node_left_behind(self) -> None:
        zcode_hooks = self.old_home / ".zcode"
        zcode_hooks.mkdir(parents=True)
        (zcode_hooks / "settings.json").write_text(
            json.dumps({
                "hooks": {"Stop": [{
                    "hooks": [{
                        "command": "python3 hooks/stop-hook-bridge.py",
                        "env": {
                            "HOOK_ROOT": str(self.root.path),
                            "HOOK_NODE": self.node,
                        },
                    }],
                }]},
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        code, output = self.cli(
            "retire", "--root", str(self.root.path), self.node,
        )
        self.assertEqual(0, code, output)

        self.assertTrue((self.old_home / ".cursor" / "hooks.json").is_file())
        self.assertEqual(
            [self.foreign_command],
            [row["command"] for row in self.read_cursor_stops(self.old_home)],
        )
        zcode_document = json.loads(
            (zcode_hooks / "settings.json").read_text(encoding="utf-8"))
        zombie_hooks = zcode_document["hooks"]["Stop"][0]["hooks"]
        self.assertEqual(
            [], zombie_hooks,
            "the zcode harness equivalent naming the node must disarm too")
        rows = self.disarm_rows()
        self.assertTrue(rows, "the retirement disarm must be receipted")
        configs = {entry["config"] for entry in rows[-1]["disarmed"]}
        self.assertEqual(
            {
                str(self.old_home / ".cursor" / "hooks.json"),
                str(zcode_hooks / "settings.json"),
            },
            configs,
        )

    def test_a_node_with_no_recorded_homes_still_receipts_the_sweep(self) -> None:
        self.write_map([])
        code, output = self.cli(
            "retire", "--root", str(self.root.path), self.node,
        )
        self.assertEqual(0, code, output)
        rows = self.disarm_rows()
        self.assertTrue(rows, "the sweep is receipted even when it finds nothing")
        self.assertEqual([], rows[-1]["disarmed"])
        self.assertEqual([], rows[-1]["homes_examined"])

    def test_node_retire_disarms_the_old_home_too(self) -> None:
        """The admin `node retire` verb performs the same disarm."""

        code, output = self.cli(
            "node", "retire",
            "--root", str(self.root.path),
            "--as", "architect",
            "--node", self.node,
        )
        self.assertEqual(0, code, output)
        self.assertTrue((self.old_home / ".cursor" / "hooks.json").is_file())
        self.assertEqual(
            [self.foreign_command],
            [row["command"] for row in self.read_cursor_stops(self.old_home)],
        )
        rows = self.disarm_rows()
        self.assertTrue(rows, "the admin retirement disarm must be receipted")
        self.assertEqual(1, rows[-1]["disarmed"][0]["removed"])


if __name__ == "__main__":
    unittest.main()
