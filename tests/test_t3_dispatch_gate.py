"""T3 dispatch admission and receipts share a new hook surface.

The inbound-hold tests cover mail presentation, so this module drives Claude's
installed delegate hook and reuses their real controlling-terminal harness.
"""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from floati.mcp import run_cli_artifact
from floati.holds import HoldLedger
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_net_hold_inbound_1 import OperatorTerminal
from tests.test_overlap_radar import _commit, _git, _write
from tests.test_quiet_window import _FakeHost

REPOSITORY = Path(__file__).resolve().parents[1]
MATCHER = "mcp__t3-code__delegate_task"
RECEIPT_SCHEMA = REPOSITORY / "schemas/v1/t3-dispatch-receipt-record.schema.json"


class T3DispatchGateTests(OperatorTerminal, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True).path
        self.settings = self.base / "settings.json"
        self.original = (b'{ "hooks": {"PreToolUse": [{"matcher":"Other","hooks":[]}],'
                         b' "PostToolUse": [{"matcher":"OtherPost","hooks":[]}]}, "n":1e2 }\n')
        self.settings.write_bytes(self.original)
        self.policy = self.base / "gates.json"
        self.policy.write_text('[{"provider_instance":"account-a","model":"model-a"}]')
        self.runtime = self.base / "runtime"
        (self.runtime / "scripts").mkdir(parents=True)
        shutil.copy2(REPOSITORY / "scripts/floati", self.runtime / "scripts/floati")
        (self.runtime / "floati").symlink_to(REPOSITORY / "floati", target_is_directory=True)
        self.task = "Private task instructions. Read ~/briefs/property.md in full."

    def install(self):
        argv = ("hook", "install", "--harness", "claude", "--root", str(self.root),
                "--runtime", str(self.runtime), "--hooks", str(self.settings),
                "--t3-gates", str(self.policy))
        code, preview = run_cli_artifact(argv)
        self.assertEqual(0, code, preview)
        self.assertEqual(self.original, self.settings.read_bytes())
        token = preview["evidence"]["confirmation"]
        self.assertEqual(20, run_cli_artifact((*argv, "--confirm", "stale"))[0])
        self.assertEqual(self.original, self.settings.read_bytes())
        code, applied = run_cli_artifact((*argv, "--confirm", token))
        self.assertEqual(0, code, applied)
        self.assertIn(b'{"matcher":"Other","hooks":[]}', self.settings.read_bytes())
        self.assertIn(b'{"matcher":"OtherPost","hooks":[]}', self.settings.read_bytes())
        self.assertIn(b'"n":1e2', self.settings.read_bytes())
        hooks = json.loads(self.settings.read_bytes())["hooks"]["PreToolUse"]
        return shlex.split(next(h for h in hooks if h["matcher"] == MATCHER)["hooks"][0]["command"])

    def check(self, command, call, *, provider="account-a", model="model-a", options=None,
              title=None, role="implementation", base_ref="refs/heads/topic"):
        payload = {
            "hook_event_name": "PreToolUse", "tool_name": MATCHER, "tool_use_id": call,
            "tool_input": {"task": self.task, "title": title, "role": role, "isolation": "worktree",
                           "target": {"providerInstanceId": provider, "model": model,
                                      "options": options}},
        }
        if base_ref is not None:
            payload["tool_input"]["baseRef"] = base_ref
        return self.run_hook(command, payload, parent="parent-thread")

    def run_hook(self, command, payload, *, parent):
        run = subprocess.run(command, input=json.dumps(payload).encode(), capture_output=True,
                             env=dict(os.environ, T3CODE_THREAD_ID=parent), timeout=5)
        self.assertEqual(0, run.returncode, run.stderr)
        if not run.stdout:
            return {}
        return json.loads(run.stdout)

    def test_two_calls_in_one_turn_link_to_their_own_children(self):
        pre = self.install()
        groups = json.loads(self.settings.read_bytes())["hooks"]
        matching = [h for h in groups["PostToolUse"] if h["matcher"] == MATCHER]
        self.assertTrue(matching, "the installed delegate hook cannot record child IDs")
        post = shlex.split(matching[0]["hooks"][0]["command"])
        from floati.t3_dispatch import read_child_link

        # Redacted Claude probe shape. Both calls have identical session, prompt
        # and tool input. Only the per-call ID distinguishes these dispatches.
        parent = "mcp:launch:redacted-parent"
        common = {
            "session_id": "1342e067-b417-48f8-8168-81dc317f9153",
            "prompt_id": "1b3463a0-6e34-4906-a75f-82dbf4e72422",
            "transcript_path": "~/.claude/projects/redacted/session.jsonl",
            "cwd": str(self.base), "scratchpad_dir": "\x2fprivate/tmp/redacted/scratchpad",
            "permission_mode": "bypassPermissions", "effort": {"level": "high"},
            "hook_event_name": "PreToolUse", "tool_name": MATCHER,
            "tool_input": {"title": "probe-2", "task": self.task, "target": {
                "providerInstanceId": "codex", "model": "gpt-6.2-sol",
                "options": [{"id": "reasoningEffort", "value": "low"}],
            }},
            "mcp_server": {"name": "t3-code", "source": "dynamic"},
        }
        first, second = (dict(common, tool_use_id=call)
                         for call in ("toolu_01RhvPZdJJBi2fQUJkXbWznv", "toolu_redacted_2"))
        for call in (first, second):
            self.assertEqual({}, self.run_hook(pre, call, parent=parent))
        receipts = self.rows()
        self.assertEqual(2, len(receipts))
        keys = [receipt["dispatch_key"] for receipt in receipts]
        self.assertNotEqual(*keys)
        root = FloatiRoot.open_direct_home(self.root)
        self.assertEqual("post-hook-not-observed", read_child_link(root, keys[0]).status)

        suffixes = ["command%3Amcp%3Aredacted%3Adelegate-task%3A" + label
                    for label in ("first", "second")]
        for call, suffix in reversed(list(zip((first, second), suffixes))):
            response = {
                "taskId": "node:delegated-task:" + suffix,
                "childThreadId": "thread:delegated-task:" + suffix,
                "childNodeId": "node:delegated-task:" + suffix,
                "childRunId": "run:thread:redacted:ordinal:1",
                "status": "starting", "workState": "working", "providerInstanceId": "codex",
                "summary": "private response text",
            }
            payload = dict(call, hook_event_name="PostToolUse",
                           tool_response=json.dumps(response), duration_ms=32)
            for _ in range(2):  # A replay of the same hook leaves one observation.
                self.assertEqual({}, self.run_hook(post, payload, parent=parent))

        for key, suffix in zip(keys, suffixes):
            link = read_child_link(root, key)
            self.assertEqual("linked", link.status)
            self.assertEqual("node:delegated-task:" + suffix, link.task_id)
            self.assertEqual("thread:delegated-task:" + suffix, link.child_thread_id)
        text = (self.root / "receipts/t3-dispatches.jsonl").read_text()
        self.assertEqual(4, len(text.splitlines()))
        self.assertEqual(receipts, self.rows())
        self.assertNotIn(self.task, text)
        self.assertNotIn("private response text", text)

    def test_child_link_reports_only_the_observed_outcome(self):
        pre = self.install()
        groups = json.loads(self.settings.read_bytes())["hooks"]
        self.assertIn("PostToolUseFailure", groups, "a failed call cannot be distinguished from silence")
        from floati.t3_dispatch import read_child_link

        root = FloatiRoot.open_direct_home(self.root)
        cases = (
            ("PostToolUse", "private non-JSON response", "unparseable-response"),
            ("PostToolUse", {"taskId": "node:valid", "childThreadId": "thread:valid"},
             "unparseable-response"),
            ("PostToolUse", '{"taskId": "node:valid"}', "unparseable-response"),
            ("PostToolUseFailure", None, "call-failed"),
        )
        for number, (event, response, expected) in enumerate(cases):
            with self.subTest(event=event, response=response):
                call = f"toolu_{number}"
                self.check(pre, call, provider="codex")
                command = shlex.split(next(h for h in groups[event] if h["matcher"] == MATCHER)
                                      ["hooks"][0]["command"])
                payload = {"hook_event_name": event, "tool_name": MATCHER, "tool_use_id": call}
                if event == "PostToolUse":
                    payload["tool_response"] = response
                else:
                    payload["error"] = "private error text"
                key = self.rows()[-1]["dispatch_key"]
                self.assertEqual({}, self.run_hook(command, payload, parent="parent-thread"))
                result = read_child_link(root, key)
                self.assertEqual(expected, result.status)
                self.assertIsNone(result.task_id)
                self.assertIsNone(result.child_thread_id)
        text = (self.root / "receipts/t3-dispatches.jsonl").read_text()
        self.assertNotIn("private non-JSON response", text)
        self.assertNotIn("private error text", text)

    def rows(self):
        path = self.root / "receipts" / "t3-dispatches.jsonl"
        return [row for row in map(json.loads, path.read_text().splitlines())
                if row["kind"] == "t3_dispatch_receipt"]

    def grant(self, key, *, answer=None, count="1"):
        argv = [sys.executable, "-B", "-m", "floati.t3_gate", "grant", "--root", str(self.root),
                "--provider", "account-a", "--model", "model-a", "--expires-in", "120",
                "--idempotency-key", key]
        if count is not None:
            argv += ["--count", count]
        if answer is None:
            return self.without_terminal(argv, cwd=self.base)
        return self.at_terminal(argv, cwd=self.base, answer=answer)[:2]

    def test_project_hold_covers_only_load_for_the_declared_node_without_spending_a_grant(self):
        command = self.install()
        root = FloatiRoot.open_direct_home(self.root)
        Registry(root).register("builder-a", "codex")
        ledger = HoldLedger(root)
        hold = ledger.start("builder-a", "Release audit", "hold-a", "2030-01-01T00:00:00Z")
        self.assertEqual(0, self.grant("hold-control", answer=lambda code: code)[0])

        for call, title, provider, reason in (
            ("held", "[floati:node=builder-a] Build", "account-a", "t3_project_hold"),
            ("other", "[floati:node=builder-b] Build", "account-b", "t3_ungated"),
            ("measure", "[floati:node=builder-a] [floati:class=measurement] Audit",
             "account-b", "t3_ungated"),
            ("unbound", "Build", "account-b", "t3_ungated"),
            ("released", "[floati:node=builder-a] Build", "account-a", "t3_grant_consumed"),
        ):
            with self.subTest(call=call):
                if call == "released":
                    ledger.release("builder-a", "release-a", hold["id"])
                reply = self.check(command, call, title=title, provider=provider)
                receipt = self.rows()[-1]
                self.assertEqual(reason, receipt["reason"])
                validate_json_schema(receipt, RECEIPT_SCHEMA)
                if call == "held":
                    detail = reply["hookSpecificOutput"]["permissionDecisionReason"]
                    for value in (reason, hold["id"], hold["reason"], hold["expected_release"]):
                        self.assertIn(value, detail)
                    self.assertEqual(hold["id"], receipt["hold"]["id"])
                    self.assertIsNone(receipt["grant_id"])
                else:
                    self.assertNotIn("hookSpecificOutput", reply)

    def test_quiet_window_defers_load_even_when_no_existing_seat_needs_pausing(self):
        from floati import quiet_window

        command = self.install()
        root = FloatiRoot.open_direct_home(self.root)
        Registry(root).register("operator-a", "codex")
        native = quiet_window.NativeHost()
        pid = os.getpid()
        token = native.identity(pid)
        self.assertIsNotNone(token)
        wall = time.time()
        host = _FakeHost(quiet_window, lambda _: [])
        host.self_pid = lambda: pid
        host.identity = lambda caller: token if caller == pid else None
        host.wall = lambda: wall + host.clock
        wait = host.wait
        seen = {}

        def while_open(seconds):
            if not seen:
                seen["build"] = self.check(command, "quiet-build", provider="account-b",
                                           options=[{"id": "reasoningEffort", "value": "low"}])
                seen["measure"] = self.check(command, "quiet-measure", provider="account-b",
                                             options={"reasoningEffort": "low"},
                                             title="[floati:class=measurement] Audit")
                seen["unknown"] = self.check(command, "quiet-unknown", provider="account-b", role="general")
            return wait(seconds)

        host.wait = while_open
        parameters = quiet_window.QuietParameters(8, 30, 30, 30, None, ())
        window = quiet_window.QuietWindow(root, "operator-a", "quiet-a", parameters, host=host)
        self.assertEqual("released", window.run()["state"])
        seen["released"] = self.check(command, "quiet-released", provider="account-b")
        self.assertEqual([], host.signals)
        for case in ("measure", "unknown", "released"):
            self.assertNotIn("hookSpecificOutput", seen[case])
        self.assertEqual(["t3_quiet_window", "t3_ungated", "t3_ungated", "t3_ungated"],
                         [row["reason"] for row in self.rows()])
        receipt = self.rows()[0]
        validate_json_schema(receipt, RECEIPT_SCHEMA)
        hold = receipt["hold"]
        cap = datetime.fromisoformat(hold["expected_release"].replace("Z", "+00:00"))
        self.assertAlmostEqual(wall + 30, cap.timestamp(), places=2)
        detail = seen["build"]["hookSpecificOutput"]["permissionDecisionReason"]
        for value in ("t3_quiet_window", hold["id"], hold["expected_release"]):
            self.assertIn(value, detail)

    def test_shared_base_board_joins_children_and_shows_territory_and_ancestry(self):
        command = self.install()
        repo = self.base / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "topic")
        _write(repo, "shared.txt", "base")
        base = _commit(repo, "Base")
        bindings = []
        for index in range(3):
            workspace = self.base / f"child-{index}"
            _git(repo, "worktree", "add", "--detach", str(workspace), base if index < 2 else tip)
            if index < 2:
                _write(workspace, "shared.txt", f"child-{index}")
            if index == 0:
                tip = _commit(workspace, "Deliver producer tip")
            self.assertNotIn("hookSpecificOutput", self.check(
                command, f"child-{index}", provider="account-b", base_ref=None,
                title="[floati:base=refs/heads/topic] Build"))
            bindings.append({"dispatch_id": self.rows()[-1]["id"], "taskId": f"task-{index}",
                             "childThreadId": f"thread-{index}", "worktree": str(workspace)})
        binding_path = self.base / "bindings.json"
        binding_path.write_text(json.dumps(bindings))
        ledger = self.root / "receipts" / "t3-dispatches.jsonl"
        before = ledger.read_bytes()
        result = subprocess.run(
            [sys.executable, "-B", "-m", "floati.t3_gate", "board", "--root", str(self.root),
             "--repository", str(repo), "--base-ref", "refs/heads/topic", "--producer-tip", tip,
             "--bindings", str(binding_path)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        self.assertEqual(before, ledger.read_bytes())
        board = json.loads(result.stdout)["evidence"]
        children = {row["child_thread_id"]: row for row in board["children"]}
        self.assertEqual(set(children), {"thread-0", "thread-1", "thread-2"})
        self.assertEqual({"thread-0": True, "thread-1": False, "thread-2": True},
                         {key: row["contains_producer_tip"] for key, row in children.items()})
        self.assertTrue(all(row["territory"]["paths"] == ["shared.txt"] for row in children.values()))
        self.assertIn({"left": "thread-0", "right": "thread-1", "paths": ["shared.txt"],
                       "path_count": 1, "truncated": False}, board["overlaps"])

    def test_one_terminal_grant_covers_one_checked_call_and_receipts_omit_task_text(self):
        command = self.install()
        for call, options, expected in (
            ("before", {"effort": "low"}, "deny"),
            ("permitted", [{"id": "effort", "value": "low"}], None),
            ("permitted", [{"id": "effort", "value": "low"}], None),
            ("after", {}, "deny"),
        ):
            with self.subTest(call=call, expected=expected):
                result = self.check(command, call, options=options)
                decision = result.get("hookSpecificOutput", {})
                self.assertEqual(expected, decision.get("permissionDecision"))
                if expected:
                    self.assertIn("t3_grant_required", decision["permissionDecisionReason"])
                    self.assertIn("floati.t3_gate grant", decision["permissionDecisionReason"])
                if call == "before":
                    self.assertEqual(20, self.grant("no-terminal")[0])
                    self.assertEqual(20, self.grant("wrong-code", answer="wrong")[0])
                    self.assertEqual(0, self.grant("one", answer=lambda code: code)[0])
        rows = self.rows()
        self.assertEqual(["denied", "allowed", "denied"], [row["decision"] for row in rows])
        receipt = rows[1]
        self.assertEqual("parent-thread", receipt["parent_thread_id"])
        self.assertEqual("account-a", receipt["provider_instance"])
        self.assertEqual("model-a", receipt["requested_model"])
        self.assertEqual("implementation", receipt["role"])
        self.assertEqual("worktree", receipt["isolation"])
        self.assertEqual("refs/heads/topic", receipt["base_ref"])
        self.assertEqual("~/briefs/property.md", receipt["brief_path"])
        self.assertEqual(hashlib.sha256(self.task.encode()).hexdigest(), receipt["task_digest"])
        self.assertTrue(receipt["grant_id"])
        self.assertNotIn("Private task instructions", json.dumps(rows))

    def test_launcher_failure_refuses_gated_target_and_records_ungated_gap(self):
        command = self.install()
        self.assertNotIn("hookSpecificOutput", self.check(command, "inherited", provider=None))
        (self.runtime / "scripts/floati").unlink()
        denied = self.check(command, "gated")
        self.assertEqual("deny", denied["hookSpecificOutput"]["permissionDecision"])
        self.assertIn("t3_check_unavailable", denied["hookSpecificOutput"]["permissionDecisionReason"])
        with self.subTest("same provider, different model"):
            result = self.check(command, "other-model", model="model-b")
            self.assertNotIn("hookSpecificOutput", result)
        with self.subTest("other provider"):
            result = self.check(command, "other-account", provider="account-b")
            self.assertNotIn("hookSpecificOutput", result)
        rows = self.rows()
        self.assertEqual(["allowed_with_gap", "denied", "allowed_with_gap", "allowed_with_gap"],
                         [row["decision"] for row in rows])
        self.assertEqual("t3_target_unresolved", rows[0]["reason"])
        self.assertIsNone(rows[0]["provider_instance"])


    def test_timeout_and_total_runtime_loss_keep_the_gate_list_available(self):
        command = self.install()
        fifo = self.base / "blocked"
        os.mkfifo(fifo)
        launcher = self.runtime / "scripts/floati"
        launcher.write_text("#!/bin/sh\nread unused < " + shlex.quote(str(fifo)) + "\n")
        result = self.check(command, "timed-out", model=None)
        self.assertEqual("deny", result["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual("t3_check_unavailable", self.rows()[0]["reason"])

        shutil.rmtree(self.runtime)
        result = self.check(command, "runtime-absent", provider="account-b")
        self.assertNotIn("hookSpecificOutput", result)
        self.assertIn("t3_receipt_unavailable", result["systemMessage"])

    def test_expired_counted_grant_has_no_headroom_and_a_window_covers_multiple_calls(self):
        from floati.jsonl import append_record
        from floati.t3_dispatch import LEDGER, record
        from floati.t3_dispatch_records import GRANT_KIND

        command = self.install()
        root = FloatiRoot.open_direct_home(self.root)
        # Author an already expired fixture with the same governed record writer.
        prior = record(root, GRANT_KIND, "2000-01-01T00:00:00Z",
                       provider_instance="account-a", model="model-a", count=3,
                       expires_at="2000-01-02T00:00:00Z", idempotency_key="expired",
                       human_act={"channel": "controlling_terminal", "rendered_digest": "1" * 64})
        append_record(root, LEDGER, prior, allowed_kinds={GRANT_KIND})
        denied = self.check(command, "expired")
        self.assertEqual("deny", denied["hookSpecificOutput"]["permissionDecision"])
        code, artifact = self.grant("window", answer=lambda code: code, count=None)
        self.assertEqual(0, code, artifact)
        for call in ("window-first", "window-second"):
            with self.subTest(call=call):
                self.assertNotIn("hookSpecificOutput", self.check(command, call))
        self.assertEqual([None, artifact["evidence"]["id"], artifact["evidence"]["id"]],
                         [row["grant_id"] for row in self.rows()])
