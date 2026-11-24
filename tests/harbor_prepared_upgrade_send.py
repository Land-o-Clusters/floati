"""Explicit second arm of the installed upgrade measurement, with root preparation."""
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from tests import harbor_cold_upgrade_send as gate


class PreparedUpgradeTests(unittest.TestCase):
    def test_prepared_history(self):
        observe = gate.observed
        prepared = {}

        def observed(argv, *, cwd, environment):
            if argv[1:2] == ["send"] and argv[-1] == "after-upgrade-cold":
                root = Path(argv[argv.index("--root") + 1])
                command = [argv[0], "update", "prepare", "--root", str(root)]
                def run(options):
                    return observe(command + options, cwd=cwd, environment=environment)
                status, _ = run(["--status"])
                self.assertEqual("pending", status["state"])
                before = root.joinpath("events.jsonl").read_bytes()
                start = time.monotonic()
                cpu = 0
                steps = 0
                largest_step = {"frames": 0, "bytes": 0, "wall_seconds": 0, "child_cpu_seconds": 0}
                while status["state"] != "done":
                    previous = status
                    status, timing = run([])
                    size = status["validated_bytes"] - previous["validated_bytes"]
                    frames = status["frames"] - previous["frames"]
                    self.assertGreater(size, 0)
                    self.assertGreater(frames, 0)
                    self.assertLessEqual(size, 2 * 1024 * 1024 + 65536)
                    self.assertLessEqual(frames, 4096)
                    self.assertEqual("", timing["stderr_tail"])
                    self.assertIn(status["state"], {"in_progress", "done"})
                    measured = dict(frames=frames, bytes=size, wall_seconds=timing["wall_seconds"],
                                    child_cpu_seconds=timing["child_cpu_seconds"])
                    largest_step = {key: max(largest_step[key], value) for key, value in measured.items()}
                    cpu += timing["child_cpu_seconds"]
                    steps += 1
                elapsed = time.monotonic() - start
                self.assertEqual(before, root.joinpath("events.jsonl").read_bytes())
                bank = gate.bank_state(root)
                self.assertEqual((before.count(b"\n"), len(before)), (bank["frames"], bank["offset"]))
                prepared[root] = bank["generation"]
                gate.report("preparation", history_events=status["frames"], steps=steps,
                            wall_seconds=elapsed, child_cpu_seconds=cpu, bank_after=bank, largest_step=largest_step)
            result = observe(argv, cwd=cwd, environment=environment)
            if argv[1:2] == ["send"]:
                root = Path(argv[argv.index("--root") + 1])
                self.assertEqual(prepared[root], gate.bank_state(root)["generation"])
                self.assertEqual("", result[1]["stderr_tail"])
            return result

        with patch.object(gate, "observed", observed):
            gate.InstalledColdUpgradeSendTests().measure("bulk", (9998, 99980))
