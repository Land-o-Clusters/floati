"""Only operator-recorded person membership may select a group audience."""

import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.registry import Registry
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.root import resolve_command_root
from floati.tui import model_from_root
from floati.tui_render import render_plain_dump
from tests import test_net_ask_1
from tests import test_net_address_1
from tests.test_net_who_1 import _lane
from tests.schema_validation import validate_json_schema


class NetAddressPerson1Tests(unittest.TestCase):
    arguments = test_net_address_1.NetAddress1Tests.arguments
    preview = test_net_address_1.NetAddress1Tests.preview

    def setUp(self):
        self.fleet = test_net_ask_1.NetAsk1Tests()
        self.fleet.setUp()
        self.addCleanup(self.fleet.doCleanups)
        self.invoke_subprocess = self.fleet.invoke
        self.fleet.invoke = self.fleet.invoke_in_process
        self.root = resolve_command_root(str(self.fleet.fleet))

    def person(self, node, handle=None):
        args = ["node", "person", "clear" if handle is None else "set",
                "--root", str(self.fleet.fleet), "--node", node]
        if handle is not None:
            args += ["--person", handle]
        completed, artifact = self.fleet.invoke(*args)
        self.assertEqual(0, completed.returncode, artifact)
        return artifact["evidence"]

    def test_send_only_reaches_recorded_seats_not_matching_names_or_workspaces(self):
        """Inferring a person from a name, path, harness, or author widens mail."""
        for node in ("chris", "green", "retired"):
            Registry(self.root).register(node, "Codex")
        _lane(self.root, "chris", "chris", "chris", datetime.now(timezone.utc))
        subprocess.run(["/usr/bin/git", "config", "user.name", "chris"],
                       cwd=self.fleet.repository, env=self.fleet.environment(), check=True)
        self.person("blue", "chris")
        self.person("green", "chris")
        self.person("retired", "chris")
        Registry(self.root).retire("retired")
        arguments = self.arguments("person:chris")
        path, preview = self.preview(arguments)
        self.assertEqual(["blue", "green"], preview["recipients"])
        validate_json_schema(preview, Path("schemas/v1/recipient-preview.schema.json"))
        sent, artifact = self.invoke_subprocess(*arguments, "--recipient-preview", str(path))
        self.assertEqual(0, sent.returncode, artifact)
        messages = [row for row in self.fleet.events() if row["kind"] == "message_envelope"]
        self.assertEqual(["blue", "green"], [row["recipient"] for row in messages])
        self.assertTrue(all(row["address_preview"] == preview for row in messages))

    def test_changed_record_invalidates_preview_even_when_members_are_unchanged(self):
        """Binding only node ids misses an operator changing then restoring a person."""
        self.person("blue", "chris")
        args = self.arguments("person:chris")
        path, before = self.preview(args)
        self.person("blue", "sam")
        self.person("blue", "chris")
        completed, artifact = self.fleet.invoke(*args, "--recipient-preview", str(path))
        self.assertEqual(20, completed.returncode, artifact)
        self.assertEqual("address_preview_changed", artifact["evidence"]["code"])
        self.assertEqual([], self.fleet.events())
        _, after = self.preview(args)
        self.assertEqual(before["recipients"], after["recipients"])
        self.assertNotEqual(before["digest"], after["digest"])

    def test_clear_invalidates_send_and_ask_before_any_envelope(self):
        """An empty current person audience must invalidate a saved preview."""
        for command in ("send", "ask"):
            self.person("blue", "chris")
            args = self.arguments("person:chris", command)
            path, _ = self.preview(args)
            self.person("blue")
            completed, artifact = self.fleet.invoke(*args, "--recipient-preview", str(path))
            self.assertEqual(20, completed.returncode, artifact)
            self.assertEqual("address_preview_changed", artifact["evidence"]["code"])
        self.assertEqual([], self.fleet.events())

    def test_no_record_names_missing_source_and_no_match_names_handle(self):
        """Absent testimony differs from an explicitly empty person group."""
        self.person("blue", "sam")
        for clear in (False, True):
            if clear:
                self.person("blue")
            completed, artifact = self.fleet.invoke(*self.arguments("person:chris"), "--preview")
            self.assertEqual(20, completed.returncode, artifact)
            self.assertEqual("address_person_empty", artifact["evidence"]["code"])
            self.assertIn("chris", artifact["evidence"]["detail"])

    def test_invalid_handles_refuse_without_a_person_receipt(self):
        """Neither the operator verb nor group resolution may accept bad identifiers."""
        before = self.root.resolve_relative("registry/entries.jsonl").read_bytes()
        for handle in ("Chris", "../chris", "", "c" * 65):
            completed, artifact = self.fleet.invoke(
                "node", "person", "set", "--root", str(self.fleet.fleet),
                "--node", "blue", "--person", handle)
            self.assertEqual(20, completed.returncode, artifact)
            self.assertEqual("person_invalid", artifact["evidence"]["code"])
            completed, artifact = self.fleet.invoke(*self.arguments("person:" + handle), "--preview")
            self.assertEqual(20, completed.returncode, artifact)
            self.assertEqual("person_invalid", artifact["evidence"]["code"])
        self.assertEqual(before, self.root.resolve_relative("registry/entries.jsonl").read_bytes())
        self.assertFalse(self.root.resolve_relative("registry/persons.jsonl").exists())

    def test_latest_receipt_is_visible_on_board_and_clear_removes_it(self):
        """A stale owner cell must not survive reassignment or a recorded clear."""
        self.person("blue", "sam")
        receipt = self.person("blue", "chris")
        self.assertEqual("blue", receipt["node_id"])
        self.assertEqual("chris", receipt["person"])
        validate_json_schema(receipt, Path("schemas/v1/registry-person-record.schema.json"))
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        model = model_from_root(self.root, now)
        blue = next(row for row in model.nodes if row["node_id"] == "blue")
        self.assertEqual("chris", blue["roster"]["owner"]["value"])
        self.assertEqual("MEASURED", blue["roster"]["owner"]["stamp"])
        self.assertIn("chris", render_plain_dump(model, width=400))
        cleared = self.person("blue")
        self.assertIsNone(cleared["person"])
        model = model_from_root(self.root, now)
        blue = next(row for row in model.nodes if row["node_id"] == "blue")
        self.assertIsNone(blue["roster"]["owner"]["value"])
        self.assertNotIn("chris", render_plain_dump(model, width=400))

    def test_malformed_handle_keeps_identifier_refusal_with_a_saved_preview(self):
        """Preview validation must not hide a malformed person identifier."""
        self.person("blue", "chris")
        path, _ = self.preview(self.arguments("person:chris"))
        for command in ("send", "ask"):
            completed, artifact = self.fleet.invoke(
                *self.arguments("person:Chris", command), "--recipient-preview", str(path))
            self.assertEqual(20, completed.returncode, artifact)
            self.assertEqual("person_invalid", artifact["evidence"]["code"])
        self.assertEqual([], self.fleet.events())

    def test_unknown_or_retired_node_cannot_record_a_person(self):
        """Person records require the same registered active node as other edits."""
        Registry(self.root).retire("blue")
        before = self.root.resolve_relative("registry/entries.jsonl").read_bytes()
        for node in ("unknown", "blue"):
            for operation in ("set", "clear"):
                args = ["node", "person", operation, "--root", str(self.fleet.fleet), "--node", node]
                if operation == "set":
                    args += ["--person", "chris"]
                completed, artifact = self.fleet.invoke(*args)
                self.assertEqual(20, completed.returncode, artifact)
                self.assertEqual("unknown_node", artifact["evidence"]["code"])
        self.assertEqual(before, self.root.resolve_relative("registry/entries.jsonl").read_bytes())
        self.assertFalse(self.root.resolve_relative("registry/persons.jsonl").exists())

    def test_unrelated_person_population_cannot_oversize_a_single_recipient_send(self):
        """Copying the whole person journal into an envelope breaks bounded sends."""
        receipt = self.person("blue", "chris")
        # Construct retained testimony from 1,400 retired nodes. These records
        # remain a real source after retirement, but none is a current recipient.
        entries = self.root.resolve_relative("registry/entries.jsonl")
        persons = self.root.resolve_relative("registry/persons.jsonl")
        registration = Registry(self.root).require_active("blue")
        registry_frames = []
        person_frames = []
        for index in range(1400):
            node = f"departed-{index}"
            registry_frames.append(encode_frame(dict(
                registration, id="registry-" + uuid7_hex(), node_id=node, state="retired")))
            person_frames.append(encode_frame(dict(
                receipt, id="registry-person-" + uuid7_hex(), node_id=node, person="sam")))
        entries.write_bytes(entries.read_bytes() + b"".join(registry_frames))
        persons.write_bytes(persons.read_bytes() + b"".join(person_frames))
        args = self.arguments("person:chris")
        path, preview = self.preview(args)
        self.assertEqual(["blue"], preview["recipients"])
        completed, artifact = self.fleet.invoke(*args, "--recipient-preview", str(path))
        self.assertEqual(0, completed.returncode, artifact)
        self.assertEqual(["blue"], [row["recipient"] for row in self.fleet.events()
                                    if row["kind"] == "message_envelope"])
