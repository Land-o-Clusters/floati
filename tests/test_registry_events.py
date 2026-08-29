from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.jsonl import append_record, read_records
from floati.root import FloatiRoot

try:
    from floati.events import EventLog
    from floati.registry import Registry
except ModuleNotFoundError:
    EventLog = None
    Registry = None


def root_entries(root: FloatiRoot) -> dict[Path, tuple[str, bytes]]:
    return {
        path.relative_to(root.tenant_home): (
            "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file",
            b"" if path.is_symlink() or path.is_dir() else path.read_bytes(),
        )
        for path in root.tenant_home.rglob("*")
    }


class RegistryEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(Registry, "floati.registry must implement the registry contract")
        self.assertIsNotNone(EventLog, "floati.events must implement the event contract")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        self.registry = Registry(self.root)
        self.events = EventLog(self.root, self.registry)

    def test_registry_records_active_nodes_and_refuses_duplicates(self) -> None:
        entry = self.registry.register("sender", "worker")
        self.assertEqual("active", entry["state"])
        self.assertEqual("sender", self.registry.require_active("sender")["node_id"])
        before = self.registry.path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.registry.register("sender", "worker")
        self.assertEqual("registry_duplicate", caught.exception.code)
        self.assertEqual(before, self.registry.path.read_bytes())

    def test_terminal_unsafe_role_refuses_before_the_registry_ledger_exists(self) -> None:
        """Registry preflight must share durable role lexical safety, not create a lock first."""
        for role in ("bad\x1brole", "bad\u202erole"):
            with self.subTest(role=repr(role)):
                self.assertFalse(self.registry.path.parent.exists())
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.registry.register("sender", role)
                self.assertEqual("role_invalid", caught.exception.code)
                self.assertFalse(self.registry.path.parent.exists())

    def test_active_node_ids_projects_latest_history_as_an_immutable_sorted_tuple(self) -> None:
        """One snapshot projects the latest row for every node, not append order."""
        for node in ("zulu", "bravo", "alpha"):
            self.registry.register(node, "worker")
        self._append_registry_state("zulu", "retired")
        self._append_registry_state("bravo", "retired")
        self._append_registry_state("zulu", "active")

        roster = self.registry.active_node_ids()

        self.assertEqual(("alpha", "zulu"), roster)
        self.assertIsInstance(roster, tuple)
        with self.assertRaises(TypeError):
            roster[0] = "changed"  # type: ignore[index]

    def test_active_node_ids_returns_an_empty_tuple_when_the_ledger_is_absent(self) -> None:
        self.assertFalse(self.registry.path.exists())
        self.assertEqual((), self.registry.active_node_ids())
        self.assertFalse(self.registry.path.parent.exists())

    def test_registry_exposes_one_canonical_node_resolver(self) -> None:
        """Catches a merge retaining two APIs that can decide wake identity."""

        self.assertTrue(callable(getattr(self.registry, "resolve_node_id", None)))
        self.assertFalse(hasattr(self.registry, "canonical_active_node"))

    def test_valid_send_appends_once_and_is_idempotent_by_key(self) -> None:
        self.registry.register("sender", "worker")
        self.registry.register("recipient", "worker")
        first = self.events.send(
            "sender",
            "recipient",
            "slipway",
            "a" * 40,
            "docs/evidence/checkpoint.md",
            "HM-0.5 delivered",
            idempotency_key="job-123",
        )
        replay = self.events.send(
            "sender",
            "recipient",
            "slipway",
            "a" * 40,
            "docs/evidence/checkpoint.md",
            "HM-0.5 delivered",
            idempotency_key="job-123",
        )
        self.assertEqual(first, replay)
        records = read_records(self.root, "events.jsonl", allowed_kinds={"message_envelope"})
        self.assertEqual(1, len(records))
        self.assertEqual("message_envelope", records[0]["kind"])
        self.assertEqual("slipway", records[0]["repo"])
        self.assertEqual("a" * 40, records[0]["sha"])
        self.assertEqual("docs/evidence/checkpoint.md", records[0]["doc"])
        self.assertEqual("HM-0.5 delivered", records[0]["note"])
        self.assertNotIn("body", records[0])
        self.assertNotIn("wake_cause", records[0])

    def test_unregistered_delivery_spelling_refuses_before_minting_coordination_identity(self) -> None:
        """Catches delivery taking an alias lock before proving registry identity."""
        self.registry.register("recipient", "worker")
        alias_path = self.root.resolve_relative(
            "receipts/wake-coordination/recipient_alias"
        )

        with self.assertRaises(ProtocolRefusal) as caught:
            self.events.present("recipient_alias")

        self.assertEqual("unknown_node", caught.exception.code)
        self.assertFalse(alias_path.exists())

    def test_explicit_falsy_idempotency_keys_refuse_without_event_mutation(self) -> None:
        self.registry.register("sender", "worker")
        self.registry.register("recipient", "worker")
        for key in ("", False, 0):
            with self.subTest(key=key):
                before = self.events.path.read_bytes() if self.events.path.exists() else b""
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.events.send(
                        "sender", "recipient", "slipway", "a" * 40,
                        "docs/evidence/checkpoint.md", "HM-0.5 delivered",
                        idempotency_key=key,
                    )
                self.assertEqual("idempotency_key_invalid", caught.exception.code)
                after = self.events.path.read_bytes() if self.events.path.exists() else b""
                self.assertEqual(before, after)

    def test_all_payload_fields_participate_in_idempotency(self) -> None:
        for node in ("sender", "recipient", "other"):
            self.registry.register(node, "worker")
        reply_target = self.events.send(
            "recipient", "sender", "slipway", "f" * 40,
            "docs/evidence/reply-target.md", "target", idempotency_key="reply-target",
        )
        base = {
            "sender": "sender",
            "recipient": "recipient",
            "repo": "slipway",
            "sha": "a" * 40,
            "doc": "docs/evidence/checkpoint.md",
            "note": "HM-0.5 delivered",
        }
        mutations = {
            "sender": "other",
            "recipient": "other",
            "repo": "owner/slipway",
            "sha": "b" * 40,
            "doc": "docs/evidence/other.md",
            "note": "different note",
            "reply_to": reply_target["id"],
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                key = f"conflict-{field}"
                self.events.send(**base, idempotency_key=key)
                changed = dict(base)
                changed[field] = value
                before = self.events.path.read_bytes()
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.events.send(**changed, idempotency_key=key)
                self.assertEqual("idempotency_conflict", caught.exception.code)
                self.assertEqual(before, self.events.path.read_bytes())

    def test_reply_binding_requires_an_existing_reversed_message(self) -> None:
        for node in ("sender", "recipient", "other"):
            self.registry.register(node, "worker")
        first = self.events.send(
            "sender", "recipient", "slipway", "a" * 40,
            "docs/evidence/checkpoint.md", "notice", idempotency_key="first",
        )
        reply = self.events.send(
            "recipient", "sender", "slipway", "b" * 40,
            "docs/evidence/reply.md", "reply", reply_to=first["id"],
            idempotency_key="reply",
        )
        self.assertEqual(first["id"], reply["reply_to"])

        for reply_to, sender, recipient, code in (
            ("msg-018f0f23abcd71238000000000000000", "recipient", "sender", "reply_to_unknown"),
            (first["id"], "other", "sender", "reply_to_parties_mismatch"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.events.send(
                        sender, recipient, "slipway", "c" * 40,
                        "docs/evidence/reply.md", "reply", reply_to=reply_to,
                    )
                self.assertEqual(code, caught.exception.code)

    def test_malformed_git_notification_fields_refuse_without_event_mutation(self) -> None:
        self.registry.register("sender", "worker")
        self.registry.register("recipient", "worker")
        valid = {
            "sender": "sender",
            "recipient": "recipient",
            "repo": "slipway",
            "sha": "a" * 40,
            "doc": "docs/evidence/checkpoint.md",
            "note": "HM-0.5 delivered",
        }
        cases = (
            ("repo", "owner/repo/extra", "repo_invalid"),
            ("sha", "A" * 40, "sha_invalid"),
            ("doc", "docs/../secret.md", "doc_invalid"),
            ("note", "x" * 1025, "note_invalid"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                malformed = dict(valid)
                malformed[field] = value
                before = self.events.path.read_bytes() if self.events.path.exists() else b""
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.events.send(**malformed)
                self.assertEqual(code, caught.exception.code)
                after = self.events.path.read_bytes() if self.events.path.exists() else b""
                self.assertEqual(before, after)

    def _append_registry_state(self, node_id: str, state: str) -> None:
        append_record(
            self.root,
            "registry/entries.jsonl",
            {
                "schema_version": 0,
                "id": "registry-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": "2026-08-14T00:00:00.000Z",
                "kind": "registry_entry",
                "node_id": node_id,
                "role": "worker",
                "state": state,
            },
            allowed_kinds={"registry_entry"},
        )

    def _retire(self, node_id: str) -> None:
        self._append_registry_state(node_id, "retired")

    def _assert_invalid_registry_refuses_send_without_mutation(self, raw: bytes) -> None:
        self.registry.path.parent.mkdir(parents=True)
        self.registry.path.write_bytes(raw)
        before = root_entries(self.root)

        with self.assertRaises(IntegrityFailure):
            self.events.send(
                "sender", "recipient", "slipway", "a" * 40,
                "docs/evidence/checkpoint.md", "HM-0.5 delivered",
            )

        self.assertEqual(before, root_entries(self.root))
        self.assertFalse(self.events.path.exists())
        self.assertFalse(self.events.denial_path.exists())

    def test_malformed_registry_refuses_send_without_event_or_denial_append(self) -> None:
        self._assert_invalid_registry_refuses_send_without_mutation(b'{"not":"a registry entry"}\n')

    def test_truncated_registry_refuses_send_without_event_or_denial_append(self) -> None:
        self._assert_invalid_registry_refuses_send_without_mutation(b'{"schema_version":0')

    def _remove_registry_lock(self) -> None:
        lock_path = self.registry.path.with_name(self.registry.path.name + ".lock")
        lock_path.unlink()
        self.assertFalse(lock_path.exists())

    def test_unknown_sender_refuses_without_root_mutation_and_lists_active_roster(self) -> None:
        for node in ("zulu", "recipient", "retired", "alpha"):
            self.registry.register(node, "worker")
        self._retire("retired")
        self._remove_registry_lock()
        before = root_entries(self.root)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.events.send(
                "stranger", "recipient", "slipway", "a" * 40,
                "docs/evidence/checkpoint.md", "HM-0.5 delivered",
            )
        self.assertEqual("unknown_sender", caught.exception.code)
        detail = caught.exception.detail
        self.assertIn("registered active nodes: ", detail)
        self.assertEqual("alpha, recipient, zulu", detail.rsplit("registered active nodes: ", 1)[1])
        self.assertEqual(before, root_entries(self.root))

    def test_unknown_recipient_refuses_without_root_mutation_and_lists_active_roster(self) -> None:
        for node in ("zulu", "recipient", "retired", "alpha"):
            self.registry.register(node, "worker")
        self._retire("retired")
        self._remove_registry_lock()
        before = root_entries(self.root)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.events.send(
                "alpha", "stranger", "slipway", "a" * 40,
                "docs/evidence/checkpoint.md", "HM-0.5 delivered",
            )
        self.assertEqual("unknown_recipient", caught.exception.code)
        self.assertEqual(before, root_entries(self.root))
        detail = caught.exception.detail
        self.assertIn("registered active nodes: ", detail)
        self.assertEqual("alpha, recipient, zulu", detail.rsplit("registered active nodes: ", 1)[1])

    def test_retired_sender_refuses_without_root_mutation_and_lists_active_roster(self) -> None:
        for node in ("zulu", "recipient", "retired", "alpha"):
            self.registry.register(node, "worker")
        self._retire("retired")
        self._remove_registry_lock()
        before = root_entries(self.root)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.events.send(
                "retired", "recipient", "slipway", "a" * 40,
                "docs/evidence/checkpoint.md", "HM-0.5 delivered",
            )
        self.assertEqual("unknown_sender", caught.exception.code)
        detail = caught.exception.detail
        self.assertIn("registered active nodes: ", detail)
        self.assertEqual("alpha, recipient, zulu", detail.rsplit("registered active nodes: ", 1)[1])
        self.assertEqual(before, root_entries(self.root))

    def test_unknown_sender_with_absent_registry_refuses_without_root_mutation(self) -> None:
        before = root_entries(self.root)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.events.send(
                "stranger", "recipient", "slipway", "a" * 40,
                "docs/evidence/checkpoint.md", "HM-0.5 delivered",
            )
        self.assertEqual("unknown_sender", caught.exception.code)
        detail = caught.exception.detail
        self.assertIn("registered active nodes: ", detail)
        self.assertEqual("(none)", detail.rsplit("registered active nodes: ", 1)[1])
        self.assertEqual(before, root_entries(self.root))

    def test_unknown_sender_with_all_retired_nodes_refuses_without_root_mutation(self) -> None:
        self.registry.register("retired", "worker")
        self._retire("retired")
        self._remove_registry_lock()
        before = root_entries(self.root)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.events.send(
                "stranger", "retired", "slipway", "a" * 40,
                "docs/evidence/checkpoint.md", "HM-0.5 delivered",
            )
        self.assertEqual("unknown_sender", caught.exception.code)
        detail = caught.exception.detail
        self.assertIn("registered active nodes: ", detail)
        self.assertEqual("(none)", detail.rsplit("registered active nodes: ", 1)[1])
        self.assertEqual(before, root_entries(self.root))

    def test_registered_send_changes_root_and_appends_one_message_envelope(self) -> None:
        self.registry.register("sender", "worker")
        self.registry.register("recipient", "worker")
        before = root_entries(self.root)
        self.events.send(
            "sender", "recipient", "slipway", "a" * 40,
            "docs/evidence/checkpoint.md", "HM-0.5 delivered",
        )
        self.assertNotEqual(before, root_entries(self.root))
        events = read_records(self.root, "events.jsonl", allowed_kinds={"message_envelope"})
        self.assertEqual(1, len(events))
        self.assertEqual("message_envelope", events[0]["kind"])
        self.assertFalse(self.events.denial_path.exists())

    def test_registered_idempotency_conflict_records_durable_denial(self) -> None:
        self.registry.register("sender", "worker")
        self.registry.register("recipient", "worker")
        self.events.send(
            "sender", "recipient", "slipway", "a" * 40,
            "docs/evidence/checkpoint.md", "first", idempotency_key="conflict",
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self.events.send(
                "sender", "recipient", "slipway", "a" * 40,
                "docs/evidence/checkpoint.md", "second", idempotency_key="conflict",
            )
        self.assertEqual("idempotency_conflict", caught.exception.code)
        denials = read_records(self.root, "receipts/denials.jsonl", allowed_kinds={"denial_receipt"})
        self.assertEqual(1, len(denials))
        self.assertEqual("idempotency_conflict", denials[0]["reason_code"])

    def test_invalid_empty_claim_refuses_without_durable_denial_state(self) -> None:
        self.registry.register("recipient", "worker")
        with self.assertRaises(ProtocolRefusal) as caught:
            self.events.send(
                "", "recipient", "slipway", "a" * 40,
                "docs/evidence/checkpoint.md", "HM-0.5 delivered",
            )
        self.assertEqual("sender_invalid", caught.exception.code)
        self.assertFalse(self.root.resolve_relative("receipts/denials.jsonl").exists())

    def test_retract_fails_closed_on_a_prior_nonparty_retraction_before_append(self) -> None:
        """The retraction writer validates the complete ledger under its append lock."""
        for node in ("sender", "recipient"):
            self.registry.register(node, "worker")
        session = "worker-" + uuid7_hex()
        first = self.events.send(
            "sender", "recipient", "slipway", "a" * 40,
            "docs/evidence/first.md", "first",
            idempotency_key="writer-forged-first", worker_session_id=session,
        )
        second = self.events.send(
            "sender", "recipient", "slipway", "b" * 40,
            "docs/evidence/second.md", "second",
            idempotency_key="writer-forged-second", worker_session_id=session,
        )
        append_record(
            self.root,
            "events.jsonl",
            {
                "schema_version": 0,
                "id": "ret-" + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-08T12:00:00.000Z",
                "kind": "message_retracted",
                "retracted_message_id": first["id"],
                "worker_session_id": session,
                "reason": "stale_recipient",
                "author": "attacker",
            },
            allowed_kinds={"message_envelope", "message_retracted"},
        )
        before = self.events.path.read_bytes()
        with self.assertRaises(IntegrityFailure) as caught:
            self.events.retract(
                second["id"],
                worker_session_id=session,
                reason="sent_in_error",
                author="sender",
            )
        self.assertEqual("message_retraction_party_invalid", caught.exception.code)
        self.assertEqual(before, self.events.path.read_bytes())

    def test_retraction_of_held_work_is_visible_to_a_new_controller_evaluation(self) -> None:
        """Catches retraction racing outside the controller coordination boundary."""
        from floati.wake_hold import WakeHoldController

        for node in ("alice", "bob"):
            self.registry.register(node, "worker")
        session = "worker-018f7e9b3c137abc8def0123456789ab"
        item = self.events.send("alice", "bob", "slipway", "a" * 40, "docs/evidence/held.md", "held", idempotency_key="held", worker_session_id=session)
        WakeHoldController(self.root).evaluate("bob", idempotency_key="held-key", worker_session_id=session)
        self.events.retract(item["id"], worker_session_id=session, reason="sent_in_error", author="alice")
        artifact = WakeHoldController(self.root).evaluate("bob", idempotency_key="later-key", worker_session_id=session)
        self.assertEqual("caught_up", artifact["state"])

    def test_denial_receipt_has_a_strict_schema(self) -> None:
        path = Path("schemas/v0/denial-receipt.schema.json")
        self.assertTrue(path.is_file(), "denial evidence needs a versioned schema")
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("denial_receipt", schema["properties"]["kind"]["const"])
        self.assertTrue(
            {"attempt_id", "claimed_sender", "claimed_recipient", "reason_code"}.issubset(schema["required"])
        )


if __name__ == "__main__":
    unittest.main()
