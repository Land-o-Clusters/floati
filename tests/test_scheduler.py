"""Focused Task 2 scheduler/dispatch compatibility controls."""

from __future__ import annotations

import unittest

from floati.errors import ProtocolRefusal
from tests.test_spawn_groups import _Task2Case


class SchedulerSpawnPolicyTests(unittest.TestCase):
    def test_explicit_spawn_policy_is_repeated_by_v1_dispatch_for_every_mode(self) -> None:
        for mode in ("disabled", "observed_only", "managed"):
            case = _Task2Case(self)
            policy = case.prepare_parent(mode=mode, start=False)
            with self.subTest(mode=mode):
                self.assertEqual("codex", case.dispatch["adapter"])
                self.assertEqual(
                    policy["id"], case.dispatch["attempt_spawn_policy_id"]
                )

    def test_exact_policy_retry_survives_dispatch_but_changed_retry_refuses(self) -> None:
        case = _Task2Case(self)
        policy = case.prepare_parent(mode="managed", start=False)
        exact = case.controller.bind_attempt_policy(
            case.run_id, case.parent, str(case.opened["attempt_id"]),
            str(case.snapshot["id"]), adapter="codex", subagents_mode="managed",
            max_children=2, max_depth=4,
            child_capability_ceiling=["review", "workspace_write"],
            spawn_budget_ceiling=[{"budget_id": "build", "amount": 2}],
            workspace_policies=["isolated_worktree", "patch_only"],
            now=case.now(20),
        )
        self.assertEqual(policy, exact)
        with self.assertRaises(ProtocolRefusal) as changed:
            case.controller.bind_attempt_policy(
                case.run_id, case.parent, str(case.opened["attempt_id"]),
                str(case.snapshot["id"]), adapter="other",
                subagents_mode="managed", max_children=2, max_depth=4,
                child_capability_ceiling=["review", "workspace_write"],
                spawn_budget_ceiling=[{"budget_id": "build", "amount": 2}],
                workspace_policies=["isolated_worktree", "patch_only"],
                now=case.now(21),
            )
        self.assertEqual("spawn_policy_input_divergent", changed.exception.code)


if __name__ == "__main__":
    unittest.main()
