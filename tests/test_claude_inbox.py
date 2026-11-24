"""Scratch socket transport; native relay tests own presentation semantics.

No existing module fits the outbound doorbell's state, framing and fallback:
test_claude_hook_relay exercises only the receiving publication boundary.
"""
import importlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati import fixture_ids
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from floati.cursor_presentations import CursorPresentationLedger
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.waiter_consent import arm_waiter_consent
from tests.temp_roots import REAL_TEMP_ROOT


class ScratchInbox:
    """The verified server accepts newline JSON and closes without a verdict."""
    def __init__(self, path):
        self.path = str(path)
        self.frames = []
        self.received = threading.Event()
        self.stopped = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(8)
        self.thread = threading.Thread(target=self.serve)
        self.thread.start()

    def serve(self):
        while True:
            connection, _ = self.listener.accept()
            with connection:
                data = b""
                while True:
                    fragment = connection.recv(4096)
                    if not fragment:
                        break
                    data += fragment
                if self.stopped.is_set():
                    return
                self.frames.append(data)
                self.received.set()

    def close(self):
        self.stopped.set()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
            wake.connect(self.path)
        self.thread.join(5)
        self.listener.close()
        if self.thread.is_alive():
            raise AssertionError("owned scratch socket did not exit")


class ClaudeInboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ci-", dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.node, self.sender = fixture_ids.builder("a"), fixture_ids.builder("b")
        Registry(self.root).register(self.node, "claude")
        Registry(self.root).register(self.sender, "claude")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        prior_cwd = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, prior_cwd)
        self.home = self.base / "home"
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        arm_waiter_consent(self.root.path, self.workspace, self.node, "claude", 10, 2)
        self.session = "claude-doorbell-scratch"
        self.participant = resolve_participant(self.root.path, self.workspace)
        self.consent = CodexWaitConsentLedger(self.root).require_armed(self.participant.binding)
        CodexWaitSessionLedger(self.root).arm(self.participant.binding, self.consent,
            self.session, idempotency_key="fixture-current")
        self.clock = [0.0]

    def implementation(self):
        self.assertIsNotNone(importlib.util.find_spec("floati.claude_inbox"),
            "Claude inbox doorbell transport is missing")
        return importlib.import_module("floati.claude_inbox")

    def send(self, key="one"):
        return EventLog(self.root).send(self.sender, self.node, "floati", "a" * 40,
            "docs/CONFLUENCE-v0.md", "private scratch mail", idempotency_key=key)["message"]

    def server(self):
        server = ScratchInbox(self.base / "inbox.sock")
        self.addCleanup(server.close)
        return server

    def capture(self, server=None, version="2.1.309", session=None):
        implementation = self.implementation()
        with mock.patch.dict(os.environ, {
                "CLAUDE_CODE_MESSAGING_SOCKET": server.path if server else str(self.base / "gone.sock")}),\
                mock.patch.object(implementation, "_installed_version", return_value=version):
            implementation.capture_session(self.root.path, self.node, self.workspace,
                session or self.session, {"hook_event_name": "SessionStart"})

    def notify(self, message, wait=None):
        def advance(seconds):
            self.clock[0] += seconds
            if wait:
                wait()
        return self.implementation().notify_send(self.root, message,
            clock=lambda: self.clock[0], wait=advance)

    def present(self):
        from floati.claude_hook_relay import run_relay
        output = io.StringIO()
        run_relay(bus_home=self.root.path, node=self.node,
            runtime=Path(__file__).resolve().parents[1],
            hook_payload={"cwd": str(self.workspace), "session_id": self.session,
                          "hook_event_name": "UserPromptSubmit"},
            stdout=output, clock=lambda: 100.0)
        return output.getvalue()

    def stop(self):
        from floati.codex_wait import run_stop_waiter
        from tests.waiter_timing import scripted_readiness
        output, clock = io.StringIO(), [0.0]
        advance = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        epoch = datetime.now(timezone.utc).timestamp()
        wall_time = lambda: epoch + clock[0]
        # The native hold and its delivery must share the virtual waiter's
        # elapsed time. Real CPU work cannot spend a synthetic hold budget.
        with mock.patch("floati.events.utc_now", side_effect=lambda:
                datetime.fromtimestamp(wall_time(), timezone.utc).isoformat(
                    timespec="milliseconds").replace("+00:00", "Z")):
            self.assertEqual(0, run_stop_waiter(bus_home=self.root.path,
                hook_payload={"cwd": str(self.workspace), "session_id": self.session,
                              "hook_event_name": "Stop"},
                stdout=output, stderr=io.StringIO(), monotonic=lambda: clock[0],
                wall_time=wall_time,
                wait_readable=scripted_readiness(advance, interval=1), sleep=advance))
        return output.getvalue()

    def test_doorbell_is_mail_free_and_only_native_flushed_receipt_confirms(self):
        message, server = self.send(), self.server()
        self.capture(server)
        output = []
        def flush():
            self.assertTrue(server.received.wait(5), "scratch doorbell not received")
            output.append(self.present())
        result = self.notify(message, flush)
        self.assertEqual("confirmed", result["status"], result)
        self.assertEqual(1, result["rings"])
        self.assertIn(message["id"], output[0])
        frame = server.frames[0]
        self.assertEqual(1, frame.count(b"\n"))
        body = json.loads(frame)
        self.assertEqual({"type", "message"}, set(body))
        self.assertEqual("user", body["type"])
        self.assertEqual({"role", "content"}, set(body["message"]))
        self.assertEqual("user", body["message"]["role"])
        for value in (message["id"], message["note"], self.sender, "token", "auth"):
            self.assertNotIn(value, frame.decode())
        receipts = CursorPresentationLedger(self.root).read(self.node)
        self.assertEqual(1, sum(row["phase"] == "flushed" and message["id"] in row["item_ids"]
                                for row in receipts))
        self.assertFalse(self.implementation().stop_path_required(self.root.path, self.node, self.session))
        self.assertEqual(receipts[-1]["id"], result["presentation_receipt_id"])
        observations = list((self.root.path / "state/claude-wait" / self.node / "inbox").glob("inbox-observation-*.json"))
        self.assertEqual(1, len(observations))
        self.assertEqual(result, json.loads(observations[0].read_text()))

    def test_eof_without_presentation_rerings_once_then_requires_stop(self):
        message, server = self.send(), self.server()
        self.capture(server)
        result = self.notify(message)
        self.assertEqual("claude_inbox_unconfirmed", result["code"])
        self.assertEqual(2, result["rings"])
        self.assertTrue(self.implementation().stop_path_required(self.root.path, self.node, self.session))
        self.assertEqual([], CursorPresentationLedger(self.root).read(self.node))

    def test_settings_files_hold_or_refuse_without_ringing_unknown_rings(self):
        for index, (layer, policy) in enumerate((("user", "hold"), ("project", "refuse"), ("local", "hold"))):
            with self.subTest(layer=layer, policy=policy):
                message = self.send(str(index))
                location = (self.home / ".claude/settings.json" if layer == "user" else
                    self.workspace / (".claude/settings.local.json" if layer == "local" else ".claude/settings.json"))
                location.parent.mkdir(exist_ok=True)
                location.write_text(json.dumps({"crossSessionInbound": policy}))
                self.capture()
                result = self.notify(message)
                self.assertEqual("claude_inbox_" + ("held" if policy == "hold" else "refused"), result["code"])
                self.assertEqual("readable_settings_files", result["policy_source"])
                self.assertEqual(0, result["rings"])
                location.unlink()
        server = self.server()
        self.capture(server)
        result = self.notify(message)
        self.assertEqual("claude_inbox_unconfirmed", result["code"])
        self.assertEqual(2, result["rings"])

    def test_missing_stale_version_and_windows_have_typed_stop_fallback(self):
        message = self.send()
        for version, platform, expected in (("2.1.309", "darwin", "claude_inbox_unavailable"),
                ("2.1.310", "darwin", "claude_inbox_unverified_version"),
                ("2.1.309", "win32", "claude_inbox_unavailable")):
            with self.subTest(version=version, platform=platform):
                self.capture(version=version)
                with mock.patch("floati.claude_inbox.sys.platform", platform):
                    result = self.notify(message)
                self.assertEqual(expected, result["code"])
                self.assertEqual(0, result["rings"])
                self.assertTrue(self.implementation().stop_path_required(self.root.path, self.node, self.session))

    def test_installed_client_version_is_read_with_scratch_home_and_unknown_is_unverified(self):
        implementation, server, message = self.implementation(), self.server(), self.send()
        executable = self.base / "scratch-claude"
        for output, expected in (("2.1.309 (Claude Code)", "claude_inbox_unconfirmed"),
                ("unknown", "claude_inbox_unverified_version")):
            with self.subTest(output=output):
                executable.write_text("#!/bin/sh\n"
                    "[ \"$1\" = \"--version\" ] || exit 1\n"
                    "[ -d \"$HOME\" ] || exit 2\n"
                    "[ ! -e \"$HOME/.claude\" ] || exit 3\n"
                    "printf '%s\\n' '" + output + "'\n")
                executable.chmod(0o700)
                with mock.patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": server.path,
                        "PATH": str(self.base)}),\
                        mock.patch.dict(os.environ, {"FLOATI_CLAUDE_EXECUTABLE": str(executable)}):
                    implementation.capture_session(self.root.path, self.node, self.workspace,
                        self.session, {"hook_event_name": "SessionStart"})
                self.assertEqual(expected, self.notify(message)["code"])
        # Even a usable client on PATH is absent without a declared executable.
        executable.write_text("#!/bin/sh\nprintf '2.1.309 (Claude Code)\\n'\n")
        (self.base / "claude").symlink_to(executable)
        for declared in ("", "scratch-claude", str(self.base / "claude"),
                         str(self.base / "missing-client")):
            with self.subTest(declared=declared), mock.patch.dict(os.environ, {
                    "CLAUDE_CODE_MESSAGING_SOCKET": server.path,
                    "FLOATI_CLAUDE_EXECUTABLE": declared, "PATH": str(self.base)}):
                implementation.capture_session(self.root.path, self.node, self.workspace,
                    self.session, {"hook_event_name": "SessionStart"})
                self.assertEqual("claude_inbox_unverified_version", self.notify(message)["code"])

    def test_stale_capture_and_paused_session_never_ring(self):
        from floati.wake_control import WakeController
        message, server = self.send(), self.server()
        self.capture(server, session="foreign-session")
        self.assertEqual("claude_inbox_unavailable", self.notify(message)["code"])
        self.capture(server)
        WakeController(self.root).pause(self.node, self.session, idempotency_key="fixture-pause")
        self.assertEqual("claude_inbox_unavailable", self.notify(message)["code"])
        self.assertEqual([], server.frames)

    def test_confirmed_old_message_does_not_confirm_new_mail_and_concurrent_notifies_deduplicate(self):
        first, second, server = self.send("first"), self.send("second"), self.server()
        self.capture(server)
        outputs = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda message: self.notify(message,
                lambda: outputs.append(self.present())), (first, second)))
        self.assertTrue(all(result["status"] == "confirmed" for result in results), results)
        self.assertEqual(1, sum(result["rings"] for result in results))
        self.assertEqual(1, len(outputs))
        self.assertIn(first["id"], outputs[0])
        self.assertIn(second["id"], outputs[0])

    def test_old_flushed_receipt_cannot_confirm_later_message(self):
        first, server = self.send("old"), self.server()
        self.capture(server)
        self.assertEqual("confirmed", self.notify(first, self.present)["status"])
        implementation = self.implementation()
        with mock.patch.object(implementation, "notify_send", return_value=None):
            second = self.send("new")
        self.assertEqual("claude_inbox_unconfirmed", self.notify(second)["code"])

    def test_native_send_releases_its_locks_before_waiting_for_prompt_receipt(self):
        implementation, server = self.implementation(), self.server()
        self.capture(server)
        notify = implementation.notify_send
        def after_append(root, message):
            def presentation():
                with ThreadPoolExecutor(max_workers=1) as executor:
                    self.assertIn(message["id"], executor.submit(self.present).result(timeout=5))
            return notify(root, message, clock=lambda: self.clock[0],
                wait=lambda seconds: (self.clock.__setitem__(0, self.clock[0] + seconds), presentation()))
        with mock.patch.object(implementation, "notify_send", side_effect=after_append):
            result = EventLog(self.root).send(self.sender, self.node, "floati", "a" * 40,
                "docs/CONFLUENCE-v0.md", "scratch integration", idempotency_key="integration")
        self.assertIn("claude_inbox", result, "native send does not trigger the doorbell after appending mail")
        self.assertEqual("confirmed", result["claude_inbox"]["status"])

    def test_capture_creates_only_private_owned_state_and_refuses_symlinked_state(self):
        from floati.errors import ProtocolRefusal
        implementation = self.implementation()
        legacy = self.root.path / "state/claude-wait" / self.node
        legacy.mkdir(mode=0o755, parents=True, exist_ok=True)
        legacy.chmod(0o755)
        self.capture()
        self.assertEqual(0o755, legacy.stat().st_mode & 0o777)
        state = legacy / "inbox"
        self.assertEqual(0o700, state.stat().st_mode & 0o777)
        files = [path for path in state.iterdir() if path.is_file()]
        self.assertTrue(files)
        for path in files:
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertNotIn("token", path.read_text())
        saved = state.with_name("inbox-saved")
        state.rename(saved)
        state.symlink_to(saved, target_is_directory=True)
        with self.assertRaises(ProtocolRefusal) as caught:
            implementation.capture_session(self.root.path, self.node, self.workspace,
                self.session, {"hook_event_name": "SessionStart"})
        self.assertEqual("claude_inbox_state_invalid", caught.exception.code)

    def test_missing_session_socket_records_typed_stop_fallback(self):
        implementation = self.implementation()
        with mock.patch.dict(os.environ, {}, clear=True):
            implementation.capture_session(self.root.path, self.node, self.workspace,
                self.session, {"hook_event_name": "SessionStart"})
        result = self.notify(self.send())
        self.assertIsNotNone(result, "a missing Claude inbox must name its Stop fallback")
        self.assertEqual("claude_inbox_unavailable", result["code"])
        self.assertEqual(0, result["rings"])

    def test_invalid_optional_inbox_state_cannot_block_native_stop_fallback(self):
        implementation = self.implementation()
        self.capture()
        directory = self.root.path / "state/claude-wait" / self.node / "inbox"
        for corruption in ("malformed", "permissions", "symlink"):
            with self.subTest(corruption=corruption):
                path = next(path for path in directory.glob("inbox-*.json")
                            if path.name != "inbox-latest.json")
                original = path.read_bytes()
                if corruption == "malformed":
                    path.write_text("{")
                elif corruption == "permissions":
                    path.chmod(0o644)
                else:
                    path.unlink()
                    path.symlink_to(directory / "absent")
                try:
                    self.assertTrue(implementation.stop_path_required(
                        self.root.path, self.node, self.session))
                finally:
                    path.unlink()
                    path.write_bytes(original)
                    path.chmod(0o600)

    def test_session_start_in_a_seat_subdirectory_confirms_its_native_presentation(self):
        implementation, server, message = self.implementation(), self.server(), self.send()
        child = self.workspace / "child"
        child.mkdir()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_MESSAGING_SOCKET": server.path}),\
                mock.patch.object(implementation, "_installed_version", return_value="2.1.309"):
            implementation.capture_session(self.root.path, self.node, child,
                self.session, {"hook_event_name": "SessionStart"})
        self.assertEqual("confirmed", self.notify(message, self.present)["status"])

    def test_every_typed_fallback_survives_into_native_relay_and_stop_delivery_receipts(self):
        from copy import deepcopy
        from floati.cursor import SparseCursor
        from floati.errors import ProtocolRefusal
        from floati.jsonl import read_records_snapshot
        from floati.records import validate_record
        from tests.schema_validation import validate_json_schema
        server = self.server()
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir()
        cases = (("held", "hold", "2.1.309", True),
                 ("refused", "refuse", "2.1.309", True),
                 ("unavailable", None, "2.1.309", False),
                 ("unverified_version", None, "2.1.310", True),
                 ("unconfirmed", None, "2.1.309", True))
        for route in (self.present, self.stop):
            for name, policy, version, usable in cases:
                with self.subTest(route=route.__name__, reason=name):
                    settings.write_text(json.dumps({"crossSessionInbound": policy}))
                    self.capture(server if usable else None, version=version)
                    # Native-send integration has its own property; inject the
                    # clock at notify below instead of paying real retry time.
                    with mock.patch("floati.claude_inbox.notify_send", return_value=None):
                        message = self.send(route.__name__ + name)
                    self.assertEqual("claude_inbox_" + name, self.notify(message)["code"])
                    self.assertIn(message["id"], route())
                    deliveries = read_records_snapshot(self.root,
                        SparseCursor(self.root)._delivery_relative_path_for(self.node),
                        allowed_kinds={"delivery_receipt", "delivered_unpresented", "wake_hold_receipt"})
                    receipt = next(row for row in deliveries if message["id"] in row["item_ids"])
                    self.assertEqual("delivery_receipt", receipt["kind"], receipt)
                    self.assertEqual({"acting_session_id": self.session,
                        "policy_source": "readable_settings_files",
                        "fallbacks": [{"item_id": message["id"], "code": "claude_inbox_" + name}]},
                        receipt.get("claude_inbox"))
                    validate_json_schema(receipt, Path(__file__).resolve().parents[1]
                        / "schemas/v1/delivery-receipt.schema.json")
        for corruption in ("foreign_item", "duplicate_item", "unknown_reason", "overclaimed_policy", "v0"):
            with self.subTest(corruption=corruption):
                bad = deepcopy(receipt)
                evidence = bad["claude_inbox"]
                if corruption == "foreign_item":
                    evidence["fallbacks"][0]["item_id"] = "msg-" + "a" * 12 + "7" + "a" * 3 + "8" + "a" * 15
                elif corruption == "duplicate_item":
                    evidence["fallbacks"] *= 2
                elif corruption == "unknown_reason":
                    evidence["fallbacks"][0]["code"] = "invented_acceptance"
                elif corruption == "overclaimed_policy":
                    evidence["policy_source"] = "resolved_receiver_policy"
                else:
                    bad["schema_version"] = 0
                with self.assertRaises(ProtocolRefusal):
                    validate_record(bad, self.root.tenant_id, {"delivery_receipt"}, integrity=False)

    def test_stop_racing_with_fallback_publication_keeps_the_typed_delivery_reason(self):
        from floati.cursor import SparseCursor
        from floati.jsonl import read_records_snapshot
        implementation, message = self.implementation(), self.send()
        self.capture()  # Independently unavailable, even before the sender writes its observation.
        save = implementation._save
        receipts = []
        def stop_before_observation_is_published(path, value):
            if path.name.startswith("inbox-observation-"):
                self.assertTrue(implementation.stop_path_required(self.root.path, self.node, self.session))
                self.assertIn(message["id"], self.stop())
                receipts.extend(read_records_snapshot(self.root,
                    SparseCursor(self.root)._delivery_relative_path_for(self.node),
                    allowed_kinds={"delivery_receipt", "delivered_unpresented", "wake_hold_receipt"}))
            return save(path, value)
        with mock.patch.object(implementation, "_save", side_effect=stop_before_observation_is_published):
            self.assertEqual("claude_inbox_unavailable", self.notify(message)["code"])
        self.assertEqual(1, len(receipts))
        self.assertEqual({"acting_session_id": self.session, "policy_source": "readable_settings_files",
            "fallbacks": [{"item_id": message["id"], "code": "claude_inbox_unavailable"}]},
            receipts[0].get("claude_inbox"))
