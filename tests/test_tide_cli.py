from __future__ import annotations

import unittest

from floati.cli import _parser


class TideCliTests(unittest.TestCase):
    def test_context_policy_set_show_clear_are_public_and_exact(self) -> None:
        parser = _parser()
        set_args = parser.parse_args([
            "context", "policy", "set", "--root", "/tmp/fleet",
            "--node", "lane-a", "--metric", "context_fraction",
            "--threshold", "70%", "--action", "direct",
            "--idempotency-key", "set-key", "--json",
        ])
        self.assertEqual("set", set_args.policy_command)
        self.assertEqual("lane-a", set_args.node)
        self.assertTrue(callable(set_args.handler))
        for command in ("show", "clear"):
            argv = ["context", "policy", command, "--root", "/tmp/fleet", "--node", "lane-a", "--json"]
            if command == "clear":
                argv.extend(["--idempotency-key", "clear-key"])
            parsed = parser.parse_args(argv)
            self.assertEqual(command, parsed.policy_command)

    def test_new_policy_help_is_voice_passed_with_no_draft_stamp(self) -> None:
        parser = _parser()
        context = next(action for action in parser._actions if action.dest == "command").choices["context"]
        policy = next(action for action in context._actions if action.dest == "context_command").choices["policy"]
        leaf = next(action for action in policy._actions if action.dest == "policy_command").choices["set"]
        self.assertNotIn("DRAFT -", leaf.format_help())
        self.assertNotIn("DRAFT -", context.format_help())

    def test_context_reading_record_carries_exact_node_testimony_command(self) -> None:
        parsed = _parser().parse_args([
            "context", "reading", "record", "--root", "/tmp/fleet",
            "--as", "lane-a", "--metric", "self_reported_context_fraction",
            "--value", "75%", "--command", "/context",
            "--idempotency-key", "reading-key", "--json",
        ])
        self.assertEqual("lane-a", parsed.actor)
        self.assertEqual("/context", parsed.testimony_command)
        self.assertTrue(callable(parsed.handler))


if __name__ == "__main__":
    unittest.main()
