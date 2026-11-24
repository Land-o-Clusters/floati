"""Fresh-reader send prefix integrity and bounded admission properties."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import StableSourceFixture
from tests.bus_scale_test_profile import stable_fixture_sources


class SendPrefixTests(StableSourceFixture, unittest.TestCase):
    @stable_fixture_sources()
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(self.temp.name) / "fleet", create=True)
        for node in ("sender", "recipient"):
            Registry(self.root).register(node, "worker")
        self.log = EventLog(self.root)
        self.checkpoint = self.root.tenant_home / ".send-prefix" / "checkpoint.json"

    def send(self, key, *, reverse=False, reply_to=None):
        # A new EventLog for every call rules out an instance-only memo.
        parties = ("recipient", "sender") if reverse else ("sender", "recipient")
        return EventLog(self.root).send(*parties, "floati", "a" * 40,
            "note.md", "prefix fixture", idempotency_key=key, reply_to=reply_to)

    def warm(self):
        result = self.send("first")
        self.assertTrue(self.checkpoint.is_file(), "successful send must seal its verified prefix")
        return result

    def native_failure(self):
        try:
            self.log.event_records()
        except IntegrityFailure as error:
            return error.code
        self.fail("constructed corruption must fail the authoritative full reader")

    def assert_native_refusal(self):
        expected = self.native_failure()
        before = self.log.path.read_bytes()
        with self.assertRaises(IntegrityFailure) as caught:
            self.send("after-corruption")
        self.assertEqual(expected, caught.exception.code)
        self.assertEqual(before, self.log.path.read_bytes())

    def forbid_prefix_read(self):
        import floati.jsonl as jsonl
        native = jsonl._read_path_records
        def read(path, *args, **kwargs):
            if path == self.log.path:
                self.fail("fresh send re-read the already verified event prefix")
            return native(path, *args, **kwargs)
        return patch.object(jsonl, "_read_path_records", side_effect=read)

    def test_fresh_instance_send_does_not_read_the_verified_prefix(self):
        self.send("first")
        with self.forbid_prefix_read():
            second = self.send("second")
        self.assertEqual(["first", "second"], [r["idempotency_key"] for r in self.log.records()])
        self.assertEqual(second["recipient_readiness"], self.send("second")["recipient_readiness"])

    def test_fresh_send_collects_source_context_before_taking_the_event_lock(self):
        import fcntl
        from floati import jsonl, send_prefix
        self.warm()
        lock, _ = jsonl._lock_beside(self.log.path, "events.jsonl")
        native = send_prefix._revision
        observations = []
        def revision():
            with lock.open("rb") as stream:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.fail("source metadata collection must precede the event-ledger lock")
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            observations.append(True)
            return native()
        with patch.object(send_prefix, "_revision", side_effect=revision), self.forbid_prefix_read():
            result = self.send("outside-lock")
        self.assertTrue(observations, "a fresh operation must still measure its source context")
        self.assertEqual(result["message"], self.log.records()[-1])

    def test_fresh_send_syncs_durable_truth_without_flushing_the_growing_proof_bank(self):
        self.warm()
        home = self.checkpoint.parent
        nodes = home / "nodes"
        def identity(value):
            return value.st_dev, value.st_ino
        observed = []
        native = os.fsync
        def sync(descriptor):
            observed.append(identity(os.fstat(descriptor)))
            return native(descriptor)
        with patch.object(os, "fsync", side_effect=sync), self.forbid_prefix_read():
            result = self.send("durable-next")
        self.assertIn(identity(self.log.path.stat()), observed, "authoritative append must remain durable")
        self.assertIn(identity(self.checkpoint.stat()), observed, "atomic checkpoint bytes must remain durable")
        self.assertIn(identity(home.stat()), observed, "atomic checkpoint name must remain durable")
        self.assertNotIn(identity(nodes.stat()), observed,
                         "send must not flush a proof bank whose metadata grows with history")
        from floati.events import EVENT_KINDS
        from floati.send_prefix import Prefix
        prefix = Prefix(self.root, EVENT_KINDS)
        prefix.load()
        self.assertEqual(result["message"], prefix.get("id", result["message"]["id"]))

    def test_tampered_prefix_with_stale_checkpoint_keeps_native_refusal(self):
        self.warm()
        data = self.log.path.read_bytes().replace(b'"kind":"message_envelope"', b'"kind":"message_envelopX"', 1)
        self.log.path.write_bytes(data)
        self.assert_native_refusal()

    def test_truncation_with_stale_checkpoint_keeps_native_refusal(self):
        self.warm()
        self.log.path.write_bytes(self.log.path.read_bytes()[:-1])
        self.assert_native_refusal()

    def test_same_size_rewrite_with_restored_mtime_is_not_trusted(self):
        self.warm()
        before = self.log.path.stat()
        self.log.path.write_bytes(self.log.path.read_bytes().replace(b'"schema_version":0', b'"schema_version":9', 1))
        os.utime(self.log.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(before.st_size, self.log.path.stat().st_size)
        self.assertEqual(before.st_mtime_ns, self.log.path.stat().st_mtime_ns)
        self.assert_native_refusal()

    def test_missing_checkpoint_recovers_and_preserves_full_reader_semantics(self):
        first = self.warm()
        self.checkpoint.unlink()
        self.assertEqual(first, self.send("first"))
        self.assertTrue(self.checkpoint.is_file())
        from floati.framing import encode_frame
        from floati.ids import uuid7_hex
        from floati.send_prefix import event_references
        with self.log.path.open("ab") as stream:
            stream.write(encode_frame(dict(schema_version=3, kind="future_event_kind",
                id="future-" + uuid7_hex(), tenant_id=self.root.tenant_id,
                timestamp=first["message"]["timestamp"])))
        self.checkpoint.unlink()
        self.assertEqual(first, self.send("first"))
        second = self.send("after-future")
        for optimistic in (False, True):
            with self.subTest(optimistic=optimistic):
                rows, retracted, _proof = event_references(self.root,
                    [second["message"]["id"], first["message"]["id"]],
                    physical_order=True, optimistic=optimistic)
                self.assertEqual([first["message"], second["message"]], rows)
                self.assertEqual(set(), retracted)
        self.checkpoint.unlink()
        self.log.path.write_bytes(self.log.path.read_bytes()[:-1])
        self.assert_native_refusal()

    def test_interrupted_upgrade_resumes_across_a_send_and_replays_the_old_notice(self):
        from floati import send_prepare
        from contextlib import redirect_stderr
        from io import StringIO
        first = self.warm()
        self.checkpoint.unlink()
        status = lambda: send_prepare.prepare(self.root, status=True)
        self.assertEqual("pending", status()["state"])
        with patch.object(send_prepare, "STEP_FRAMES", 1):
            first_slice = send_prepare.prepare(self.root)
            self.assertEqual("in_progress", first_slice["state"])
            with redirect_stderr(StringIO()) as warning:
                second = self.send("during-upgrade")
            self.assertEqual("send_preparation_in_progress", json.loads(warning.getvalue())["code"])
            self.assertEqual(first_slice["validated_bytes"], status()["validated_bytes"])
            self.assertGreater(status()["source_bytes"], first_slice["source_bytes"])
            # A damaged absence filter must not hide an existing notice.
            bank = self.checkpoint.parent / "bank.bin"
            bank.write_bytes(bytes(bank.stat().st_size))
            before = self.log.path.read_bytes()
            with redirect_stderr(StringIO()):
                self.assertEqual(first, self.send("first"))
            self.assertEqual(before, self.log.path.read_bytes())
            frame_count = len(before.splitlines())
            while status()["frames"] < frame_count - 1:
                previous = status()
                current = send_prepare.prepare(self.root)
                self.assertGreater(current["validated_bytes"], previous["validated_bytes"])
            with patch.object(send_prepare.Prefix, "publish", side_effect=OSError("interrupted publication")):
                with self.assertRaises(ProtocolRefusal):
                    send_prepare.prepare(self.root)
            # A new process can finish the already durable cut between the
            # bank rename and checkpoint publication, even if it is a send.
            self.assertEqual(first, self.send("first"))
            self.assertEqual("done", status()["state"])
        self.assertEqual(first, self.send("first"))
        self.assertEqual(second, self.send("during-upgrade"))
        reply = self.send("after-preparation", reverse=True, reply_to=first["message"]["id"])
        self.assertEqual(first["message"]["id"], reply["message"]["reply_to"])
        # Losing a proved page must never turn a historical replay into an append.
        bank.write_bytes(b"damaged")
        before = self.log.path.read_bytes()
        self.assertEqual(first, self.send("first"))
        self.assertEqual(before, self.log.path.read_bytes())

    def test_preparation_recovers_damaged_draft_without_changing_events(self):
        from floati import send_prepare

        first = self.warm()
        self.send("second")
        self.checkpoint.unlink()
        before = self.log.path.read_bytes()
        with patch.object(send_prepare, "STEP_FRAMES", 1):
            self.assertEqual("in_progress", send_prepare.prepare(self.root)["state"])
        (self.checkpoint.parent / "preparing.bin").write_bytes(b"incomplete draft")
        result = send_prepare.prepare(self.root)
        self.assertEqual("done", result["state"])
        self.assertEqual(len(before), result["validated_bytes"])
        self.assertEqual(first, self.send("first"))
        self.assertEqual(before, self.log.path.read_bytes())

    def test_contended_prepare_status_keeps_legacy_root_read_only(self):
        import fcntl
        from floati import send_prepare

        root = FloatiRoot.open_direct_home(Path(self.temp.name) / "legacy", create=True)
        before = set(root.path.rglob("*"))
        flock = fcntl.flock
        descriptor = os.open(root.path, os.O_RDONLY | os.O_DIRECTORY)
        blocked = []
        try:
            flock(descriptor, fcntl.LOCK_SH)

            def release_on_contention(fd, operation):
                try:
                    return flock(fd, operation)
                except BlockingIOError:
                    blocked.append(True)
                    flock(descriptor, fcntl.LOCK_UN)
                    raise

            with patch.object(fcntl, "flock", side_effect=release_on_contention):
                self.assertEqual("pending", send_prepare.prepare(root, status=True)["state"])
        finally:
            os.close(descriptor)
        self.assertTrue(blocked)
        self.assertEqual(before, set(root.path.rglob("*")))

    def test_preparation_restarts_for_replaced_bytes_and_refuses_a_cross_slice_duplicate(self):
        from floati import send_prepare
        first = self.warm()["message"]
        self.checkpoint.unlink()
        with patch.object(send_prepare, "STEP_FRAMES", 1):
            self.assertEqual("in_progress", send_prepare.prepare(self.root)["state"])
            from floati.framing import encode_frame
            self.log.path.write_bytes(encode_frame(first) * 2)
            self.assertEqual("pending", send_prepare.prepare(self.root, status=True)["state"])
            self.assertEqual("in_progress", send_prepare.prepare(self.root)["state"])
            with self.assertRaises(IntegrityFailure) as caught:
                send_prepare.prepare(self.root)
            self.assertEqual("duplicate_record_id", caught.exception.code)
            self.assertFalse(self.checkpoint.exists())

    def test_stepwise_preparation_judges_launcher_testimony_as_the_full_reader_does(self):
        # Launcher testimony names an envelope that an earlier slice indexed,
        # so one-frame slices must reach the authoritative reader's verdict.
        from floati import send_prepare
        from floati.framing import encode_frame
        from floati.ids import uuid7_hex

        first = self.warm()["message"]
        ledger = self.log.path.read_bytes()
        native = uuid7_hex()

        def launcher():
            return {
                "schema_version": 1, "kind": "native_dispatch_launcher_record",
                "id": "native-dispatch-launcher-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id, "timestamp": first["timestamp"],
                "message_id": first["id"], "sender_node_id": first["sender"],
                "sender_provider": "codex",
                "sender_native_session_id": "-".join(
                    (native[:8], native[8:12], native[12:16], native[16:20], native[20:])),
                "session_claim_id": "codex-wait-session-" + uuid7_hex(),
            }

        def full_reader_verdict():
            try:
                self.log.event_records()
            except IntegrityFailure as error:
                return error.code
            return None

        def stepwise_verdict(frames):
            with patch.object(send_prepare, "STEP_FRAMES", 1):
                for _ in range(frames + 2):
                    try:
                        state = send_prepare.prepare(self.root)["state"]
                    except IntegrityFailure as error:
                        return error.code
                    if state == "done":
                        return None
            self.fail("one-frame preparation did not finish")

        for name, tail, verdict in (
            ("one launcher", [launcher()], None),
            ("a second launcher", [launcher(), launcher()], "native_dispatch_launcher_invalid"),
        ):
            with self.subTest(name):
                self.checkpoint.unlink(missing_ok=True)
                self.log.path.write_bytes(ledger + b"".join(encode_frame(row) for row in tail))
                self.assertEqual(verdict, full_reader_verdict())
                frames = len(self.log.path.read_bytes().splitlines())
                self.assertEqual(verdict, stepwise_verdict(frames))

    def test_checkpoint_from_replaced_epoch_cannot_hide_invalid_new_bytes(self):
        self.warm()
        # An epoch replaces the selected inode, even when its length matches.
        replacement = self.log.path.with_suffix(".replacement")
        replacement.write_bytes(self.log.path.read_bytes().replace(b'"schema_version":0', b'"schema_version":9', 1))
        replacement.replace(self.log.path)
        self.assert_native_refusal()

    def test_forged_checkpoint_identity_cannot_hide_prefix_corruption(self):
        self.warm()
        self.log.path.write_bytes(self.log.path.read_bytes()[:-1])
        checkpoint = json.loads(self.checkpoint.read_text())
        status = self.log.path.stat()
        checkpoint["payload"]["identity"] = [getattr(status, field) for field in
            ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")]
        self.checkpoint.write_text(json.dumps(checkpoint))
        self.assert_native_refusal()

    def test_missing_index_node_does_not_prove_idempotency_absence(self):
        first = self.warm()
        for node in (self.checkpoint.parent / "nodes").rglob("*"):
            if node.is_file():
                node.unlink()
        before = self.log.path.read_bytes()
        self.assertEqual(first, self.send("first"))
        self.assertEqual(before, self.log.path.read_bytes())

    def test_cold_recovery_keeps_fixture_file_cleanup_independent_of_history(self):
        from floati import send_prefix
        from floati.events import EVENT_KINDS
        from floati.framing import encode_frame
        from floati.ids import uuid7_hex
        first = self.warm()["message"]
        counts = []
        for count in (8, 80):
            with self.subTest(history=count):
                rows = [dict(first, id="msg-" + uuid7_hex(),
                    idempotency_key="cold-" + str(index)) for index in range(count)]
                self.log.path.write_bytes(b"".join(encode_frame(row) for row in rows))
                self.assertIsNotNone(send_prefix.warm(self.root))
                # A caller disposing of a cold fixture must not unlink one
                # cache file for every historical proof node.
                counts.append(sum(path.is_file() for path in self.checkpoint.parent.rglob("*")))
                proof = send_prefix.Prefix(self.root, EVENT_KINDS, read_only=True)
                proof.load()
                self.assertEqual(rows[0], proof.get("id", rows[0]["id"]))
                self.assertIsNone(proof.get("send", "never-sent"))
        self.assertEqual(counts[0], counts[1],
            "tenfold cold history grew the fixture's cache-file cleanup")

    def test_broken_cold_bank_cannot_prove_idempotency_absence(self):
        from floati import jsonl, send_prefix
        first = self.warm()
        self.checkpoint.unlink()
        self.assertIsNotNone(send_prefix.warm(self.root))
        bank = self.checkpoint.parent / "bank.bin"
        original, before = bank.read_bytes(), self.log.path.read_bytes()
        foreign = Path(self.temp.name) / "foreign-bank"
        foreign.write_bytes(original)
        foreign.chmod(0o600)
        header = len(b"FLOATI-SEND-BANK\x00\x01") + 8
        count = int.from_bytes(original[header - 8:header], "big")
        payload_start = header + count * 44
        for fault in ("missing", "header", "directory", "payload", "permissions", "symlink", "hardlink"):
            with self.subTest(fault=fault):
                bank.unlink(missing_ok=True)
                if fault == "symlink":
                    bank.symlink_to(foreign)
                elif fault == "hardlink":
                    os.link(foreign, bank)
                elif fault != "missing":
                    damaged = (b"!" + original[1:] if fault == "header" else
                        original[:header] + bytes(len(original) - header) if fault == "directory" else
                        original[:payload_start] + bytes(byte ^ 1 for byte in original[payload_start:])
                        if fault == "payload" else original)
                    bank.write_bytes(damaged)
                    bank.chmod(0o644 if fault == "permissions" else 0o600)
                # A failed rebuild must take the native reader, never turn
                # missing storage into a second append under the same key.
                with patch.object(send_prefix.Prefix, "rebuild", side_effect=OSError("unavailable")),\
                        patch.object(jsonl, "_read_path_records", wraps=jsonl._read_path_records) as native:
                    self.assertEqual(first, self.send("first"))
                self.assertTrue(any(call.args[0] == self.log.path for call in native.call_args_list),
                    "damaged bank did not fall back to native event validation")
                self.assertEqual(before, self.log.path.read_bytes())
                self.assertEqual(original, foreign.read_bytes())
        bank.unlink()
        bank.write_bytes(original)
        bank.chmod(0o600)

    def test_cold_bank_validates_the_exact_second_capture_before_publishing(self):
        from floati import send_prefix
        first = self.warm()
        self.checkpoint.unlink()
        native = Path.read_bytes
        original = native(self.log.path)
        def capture(path):
            raw = native(path)
            return raw.replace(b'"schema_version":0', b'"schema_version":9', 1) if path == self.log.path else raw
        with patch.object(Path, "read_bytes", new=capture), self.assertRaises(IntegrityFailure):
            send_prefix.warm(self.root)
        self.assertFalse(self.checkpoint.exists(), "invalid captured bytes received a source certificate")
        self.assertEqual(original, native(self.log.path))
        self.assertEqual(first, self.send("first"))

    def test_crash_before_checkpoint_recovers_the_appended_envelope_once(self):
        first = self.warm()
        stale = self.checkpoint.read_bytes()
        second = self.send("second")
        self.checkpoint.write_bytes(stale)
        before = self.log.path.read_bytes()
        self.assertEqual(second, self.send("second"))
        self.assertEqual(before, self.log.path.read_bytes())
        self.assertNotEqual(first["message"]["id"], second["message"]["id"])

    def test_historical_reply_and_replay_use_authenticated_selected_paths(self):
        first = self.warm()
        self.send("unrelated")
        with self.forbid_prefix_read():
            reply = self.send("reply", reverse=True, reply_to=first["message"]["id"])
            replay = self.send("reply", reverse=True, reply_to=first["message"]["id"])
        self.assertEqual(reply, replay)
        self.assertEqual(first["message"]["id"], reply["message"]["reply_to"])

    def test_checkpoint_from_another_root_is_rebuilt(self):
        self.warm()
        other_home = Path(self.temp.name) / "copied" / "fleet"
        shutil.copytree(self.root.tenant_home, other_home)
        other = FloatiRoot.open_direct_home(other_home)
        result = EventLog(other).send("sender", "recipient", "floati", "b" * 40,
            "other.md", "copy", idempotency_key="copy")
        self.assertEqual("b" * 40, result["message"]["sha"])
        self.assertNotEqual(json.loads(self.checkpoint.read_text())["payload"],
            json.loads((other_home / ".send-prefix/checkpoint.json").read_text())["payload"])

    def test_unindexed_bytes_after_append_are_never_sealed_as_verified(self):
        self.warm()
        import floati.jsonl as jsonl
        append = jsonl._append_frame
        def extra(path, encoded, **kwargs):
            result = append(path, encoded, **kwargs)
            with path.open("ab") as stream:
                stream.write(b"unvalidated\n")
            return result
        with patch.object(jsonl, "_append_frame", side_effect=extra):
            self.send("append-race")
        self.assert_native_refusal()

    def test_invalid_envelope_does_not_create_cache_or_lock_state(self):
        before = {p.relative_to(self.root.tenant_home): p.read_bytes()
            for p in self.root.tenant_home.rglob("*") if p.is_file()}
        with self.assertRaises(ProtocolRefusal):
            self.log.send("sender", "recipient", "floati", "bad-sha", "note.md", "bad")
        after = {p.relative_to(self.root.tenant_home): p.read_bytes()
            for p in self.root.tenant_home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_deeply_nested_hostile_checkpoint_is_a_cache_miss(self):
        first = self.warm()
        self.checkpoint.write_text("[" * 1500 + "0" + "]" * 1500)
        before = self.log.path.read_bytes()
        self.assertEqual(first, self.send("first"))
        self.assertEqual(before, self.log.path.read_bytes())

    def test_unwritable_recovery_of_missing_id_proof_uses_full_validation(self):
        self.warm()
        from floati.send_prefix import CacheMiss, Prefix
        get = Prefix.get
        def missing(prefix, domain, value):
            if domain == "id":
                raise CacheMiss()
            return get(prefix, domain, value)
        with patch.object(Prefix, "get", new=missing), patch.object(Prefix, "rebuild", side_effect=OSError("cache unavailable")):
            result = self.send("fallback")
        self.assertEqual("fallback", result["message"]["idempotency_key"])
        self.assertEqual(["first", "fallback"], [r["idempotency_key"] for r in self.log.records()])

    def test_changed_reader_limits_invalidate_prior_prefix_approval(self):
        self.warm()
        for field, limit in (("MAX_LEDGER_RECORDS", 1), ("MAX_LEDGER_BYTES", 1)):
            with self.subTest(field=field), patch("floati.jsonl." + field, limit):
                expected = self.native_failure()
                with self.assertRaises(IntegrityFailure) as caught:
                    self.send("first")
                self.assertEqual(expected, caught.exception.code)

    def test_absent_key_proof_does_not_read_historical_message_payload(self):
        first = self.warm()
        import floati.send_prefix as subject
        from floati.events import EVENT_KINDS
        target = subject._key("send", "first")
        others = [subject._key("id", row["id"]) for row in self.log.event_records()]
        length = next(n for n in range(1, len(target))
            if all(not key.startswith(target[:n]) for key in others))
        key = next("absent-" + str(n) for n in range(100000)
            if subject._key("send", "absent-" + str(n)).startswith(target[:length]))
        self.checkpoint.unlink()
        subject.warm(self.root)
        prefix = subject.open_prefix(self.root, EVENT_KINDS)
        native_read, native_descriptor_read, captured = subject._read, os.read, []
        def recording(path, maximum):
            data = native_read(path, maximum)
            captured.append(data)
            return data
        def recording_range(descriptor, size):
            data = native_descriptor_read(descriptor, size)
            captured.append(data)
            return data
        with patch.object(subject, "_read", side_effect=recording), patch.object(os, "read", side_effect=recording_range):
            self.assertIsNone(prefix.get("send", key))
        self.assertTrue(captured, "negative lookup did not expose its physical proof reads")
        self.assertNotIn(first["message"]["note"].encode(), b"".join(captured),
            "negative lookup read an unrelated historical message payload")

    def test_ack_resolves_selected_ids_without_replaying_unrelated_event_history(self):
        from floati.cursor import SparseCursor
        first = self.warm()
        self.log.present("recipient")
        for index in range(8):
            self.log.send("sender", "sender", "floati", "a" * 40, "other.md", "unrelated",
                idempotency_key="other-" + str(index))
        before = self.log.path.read_bytes()
        with self.forbid_prefix_read():
            receipt = SparseCursor(self.root).ack("recipient", [first["message"]["id"]],
                acting_session_id="prefix-ack")
        self.assertEqual([first["message"]["id"]], receipt["item_ids"])
        self.assertEqual(before, self.log.path.read_bytes())

    def test_ack_retains_retraction_refusal_from_verified_selected_paths(self):
        from floati.cursor import SparseCursor
        message = self.log.send("sender", "recipient", "floati", "a" * 40, "note.md", "retract",
            worker_session_id="worker-slot", idempotency_key="retracted")["message"]
        self.log.present("recipient", worker_session_id="worker-slot")
        self.log.retract(message["id"], worker_session_id="worker-slot", reason="sent_in_error", author="sender")
        self.send("after-retraction")
        with self.forbid_prefix_read(), self.assertRaises(ProtocolRefusal) as caught:
            SparseCursor(self.root).ack("recipient", [message["id"]],
                acting_session_id="prefix-ack", worker_session_id="worker-slot")
        self.assertEqual("ack_item_retracted", caught.exception.code)

    def test_recovery_validates_the_ledger_without_rewriting_matching_immutable_nodes(self):
        self.warm()
        from floati import jsonl, send_prefix
        # A recovery may remove unreachable old insertion paths. Compact the
        # source through native guarded recovery before comparing live nodes.
        self.checkpoint.unlink()
        self.assertIsNotNone(send_prefix.warm(self.root))
        copied = Path(self.temp.name) / "recovery" / "fleet"
        shutil.copytree(self.root.tenant_home, copied)
        bank = copied / ".send-prefix/bank.bin"
        before = send_prefix._identity(bank)
        native = jsonl._read_path_records
        with patch.object(jsonl, "_read_path_records", wraps=native) as validator:
            testimony = send_prefix.warm(FloatiRoot.open_direct_home(copied))
        self.assertTrue(any(call.args[0] == copied / "events.jsonl" for call in validator.call_args_list),
            "recovery reused a stale checkpoint instead of validating current ledger bytes")
        self.assertIsNotNone(testimony)
        self.assertIsNotNone(before, "recovery must publish its historical proofs")
        self.assertEqual(before, send_prefix._identity(bank), "recovery rewrote matching immutable bank bytes")

    def test_recovery_does_not_reuse_a_corrupt_content_addressed_node(self):
        first = self.warm()
        from floati import send_prefix
        # Recovery may prune obsolete insertion paths. Corrupt a live node,
        # using the same guarded compaction as the immutable-node control.
        self.checkpoint.unlink()
        self.assertIsNotNone(send_prefix.warm(self.root))
        copied = Path(self.temp.name) / "corrupt-recovery" / "fleet"
        shutil.copytree(self.root.tenant_home, copied)
        # Retired insertion paths may disappear on recovery. This envelope
        # remains authoritative, so its content-addressed payload must heal.
        expected = send_prefix._bytes(first["message"])
        target = copied / ".send-prefix/bank.bin"
        original = target.read_bytes()
        self.assertIn(expected, original)
        target.write_bytes(original.replace(expected, b"!" + expected[1:]))
        self.assertIsNotNone(send_prefix.warm(FloatiRoot.open_direct_home(copied)))
        self.assertEqual(original, target.read_bytes())
        self.assertEqual(first, EventLog(FloatiRoot.open_direct_home(copied)).send(
            "sender", "recipient", "floati", "a" * 40, "note.md", "prefix fixture", idempotency_key="first"))


    def assert_no_pending_send_nodes(self):
        staging = self.checkpoint.parent / "staging"
        if staging.exists():
            self.assertEqual([], list(staging.iterdir()), "native send retained operation-owned pending nodes")

    def test_native_send_prepares_private_nodes_outside_lock_and_publishes_truth_inside(self):
        import fcntl
        from floati import jsonl, send_prefix
        self.warm()
        lock, _ = jsonl._lock_beside(self.log.path, "events.jsonl")
        native_atomic, native_append = send_prefix._atomic, jsonl._append_frame
        node_writes, checkpoint_writes, appends = [], [], []
        appended = False

        def lock_held():
            with lock.open("rb") as stream:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    return False

        def atomic(path, data, **kwargs):
            path = Path(path)
            if path == self.checkpoint:
                checkpoint_writes.append((lock_held(), appended))
            elif len(path.name) == 64 and all(character in "0123456789abcdef" for character in path.name):
                node_writes.append((path, lock_held()))
            return native_atomic(path, data, **kwargs)

        def append(path, encoded, **kwargs):
            nonlocal appended
            if Path(path) == self.log.path:
                appends.append(lock_held())
                result = native_append(path, encoded, **kwargs)
                appended = True
                return result
            return native_append(path, encoded, **kwargs)

        with patch.object(send_prefix, "_atomic", side_effect=atomic),\
                patch.object(jsonl, "_append_frame", side_effect=append):
            result = self.send("private-staging")
        self.assertTrue(node_writes, "native notice must prepare its actual changed proof nodes")
        self.assertTrue(all(not held for _path, held in node_writes),
            "immutable proof bytes must be created outside the global event-ledger lock")
        self.assertTrue(all(self.checkpoint.parent / "staging" in path.parents for path, _held in node_writes),
            "uncommitted nodes must remain in the private staging bank")
        self.assertTrue(appends and all(appends), "authoritative append must retain the event-ledger lock")
        self.assertTrue(checkpoint_writes and all(held and durable for held, durable in checkpoint_writes),
            "checkpoint publication must follow durable append under the event-ledger lock")
        self.assertEqual(result["message"], self.log.records()[-1])
        self.assert_no_pending_send_nodes()

    def test_native_send_preparation_survives_same_identity_native_rebuild(self):
        from floati import events, send_prefix
        self.warm()
        native_transaction = events.transact_records
        rebuilt = False
        preserved = {}

        def rebuild_before_admission(*args, **kwargs):
            nonlocal rebuilt
            if not rebuilt:
                rebuilt = True
                before = send_prefix._identity(self.log.path)
                self.checkpoint.unlink()
                self.assertIsNotNone(send_prefix.warm(self.root))
                self.assertEqual(before, send_prefix._identity(self.log.path))
                preserved.update({path: send_prefix._identity(path)
                    for path in (self.checkpoint.parent / "nodes").iterdir()})
            return native_transaction(*args, **kwargs)

        with patch.object(events, "transact_records", side_effect=rebuild_before_admission):
            result = self.send("after-same-identity-rebuild")
        self.assertTrue(rebuilt)
        for path, identity in preserved.items():
            self.assertEqual(identity, send_prefix._identity(path),
                "admission rewrote a retained matching immutable proof node")
        before_replay = self.log.path.read_bytes()
        with self.forbid_prefix_read():
            self.assertEqual(result, self.send("after-same-identity-rebuild"))
        self.assertEqual(before_replay, self.log.path.read_bytes())
        self.assert_no_pending_send_nodes()

    def test_native_send_preparation_cannot_lose_a_competing_recipient_append(self):
        from floati import events
        self.warm()
        native_transaction = events.transact_records
        competing = []
        entered = False

        def competing_before_admission(*args, **kwargs):
            nonlocal entered
            if not entered:
                entered = True
                competing.append(EventLog(self.root).send("sender", "sender", "floati", "a" * 40,
                    "other.md", "competing native notice", idempotency_key="staging-competitor"))
            return native_transaction(*args, **kwargs)

        with patch.object(events, "transact_records", side_effect=competing_before_admission):
            result = self.send("after-staging-competitor")
        self.assertEqual(["first", "staging-competitor", "after-staging-competitor"],
            [row["idempotency_key"] for row in self.log.records()])
        before_replay = self.log.path.read_bytes()
        with self.forbid_prefix_read():
            self.assertEqual(result, self.send("after-staging-competitor"))
            replay = EventLog(self.root).send("sender", "sender", "floati", "a" * 40,
                "other.md", "competing native notice", idempotency_key="staging-competitor")
            self.assertEqual(competing[0], replay)
        self.assertEqual(before_replay, self.log.path.read_bytes(), "a stale prepared index hid the competing notice")
        self.assert_no_pending_send_nodes()

    def test_native_send_preparation_keeps_competing_key_conflict_authoritative(self):
        from floati import events
        self.warm()
        native_transaction = events.transact_records
        entered = False

        def conflict_before_admission(*args, **kwargs):
            nonlocal entered
            if not entered:
                entered = True
                EventLog(self.root).send("sender", "sender", "floati", "a" * 40,
                    "other.md", "earlier authoritative content", idempotency_key="staging-collision")
            return native_transaction(*args, **kwargs)

        with patch.object(events, "transact_records", side_effect=conflict_before_admission),\
                self.assertRaises(ProtocolRefusal) as caught:
            self.send("staging-collision")
        self.assertEqual("idempotency_conflict", caught.exception.code)
        matches = [row for row in self.log.records() if row["idempotency_key"] == "staging-collision"]
        self.assertEqual(1, len(matches))
        self.assertEqual("sender", matches[0]["recipient"])
        self.assertEqual("earlier authoritative content", matches[0]["note"])
        self.assert_no_pending_send_nodes()

    def test_native_send_preparation_rechecks_limits_before_authoritative_append(self):
        from floati import events, jsonl
        self.warm()
        before = self.log.path.read_bytes()
        limit = len(self.log.event_records()) + 1
        native_transaction = events.transact_records

        def limit_before_admission(*args, **kwargs):
            with patch.object(jsonl, "MAX_LEDGER_RECORDS", limit):
                return native_transaction(*args, **kwargs)

        with patch.object(events, "transact_records", side_effect=limit_before_admission),\
                self.assertRaises(ProtocolRefusal) as caught:
            self.send("staging-over-limit")
        self.assertEqual("ledger_record_limit", caught.exception.code)
        self.assertEqual(before, self.log.path.read_bytes())
        self.assert_no_pending_send_nodes()

    def test_native_send_preparation_does_not_publish_after_append_fsync_failure(self):
        import errno
        from floati.errors import DurabilityFailure
        self.warm()
        before, checkpoint = self.log.path.read_bytes(), self.checkpoint.read_bytes()
        nodes = {path.name: path.read_bytes() for path in (self.checkpoint.parent / "nodes").iterdir()}
        ledger = self.log.path.stat()
        native_sync = os.fsync
        failed = False

        def fail_ledger_sync(descriptor):
            nonlocal failed
            value = os.fstat(descriptor)
            if not failed and (value.st_dev, value.st_ino) == (ledger.st_dev, ledger.st_ino):
                failed = True
                raise OSError(errno.EIO, "injected authoritative append sync failure")
            return native_sync(descriptor)

        with patch.object(os, "fsync", side_effect=fail_ledger_sync), self.assertRaises(DurabilityFailure):
            self.send("staging-failed-fsync")
        self.assertTrue(failed, "control must reach the real authoritative append fsync")
        self.assertEqual(before, self.log.path.read_bytes())
        self.assertEqual(checkpoint, self.checkpoint.read_bytes())
        self.assertEqual(nodes, {path.name: path.read_bytes()
            for path in (self.checkpoint.parent / "nodes").iterdir()})
        self.assert_no_pending_send_nodes()

    def test_native_send_preparation_freezes_nested_readiness_before_admission(self):
        import copy
        from floati import events
        from floati.mail_health import RecipientReadiness
        self.warm()
        native_transaction, native_artifact = events.transact_records, RecipientReadiness.artifact
        captured, expected = [], []

        def artifact(observation):
            value = native_artifact(observation)
            captured.append(value)
            return value

        def mutate_before_admission(*args, **kwargs):
            self.assertTrue(captured, "control must observe the actual native readiness input")
            value = captured[-1]
            expected.append(copy.deepcopy(value))
            value["mechanism_evidence"][0]["sources"][0]["path"] = str(self.root.tenant_home / "mutated-after-preparation")
            return native_transaction(*args, **kwargs)

        with patch.object(RecipientReadiness, "artifact", new=artifact),\
                patch.object(events, "transact_records", side_effect=mutate_before_admission):
            result = self.send("staging-frozen-readiness")
        self.assertEqual(expected[0], result["recipient_readiness"],
            "nested caller mutation escaped the native preparation snapshot")
        readiness = next(row for row in self.log.event_records()
            if row["kind"] == "send_readiness_observation" and row["envelope_id"] == result["message"]["id"])
        self.assertEqual(expected[0], readiness["readiness"])
        before_replay = self.log.path.read_bytes()
        with self.forbid_prefix_read():
            self.assertEqual(result, self.send("staging-frozen-readiness"))
        self.assertEqual(before_replay, self.log.path.read_bytes())
        self.assert_no_pending_send_nodes()

    def test_constructed_send_stage_cannot_authorize_unvalidated_generic_append(self):
        import copy
        from floati import jsonl, send_prefix
        from floati.events import EVENT_KINDS
        self.warm()
        key = "constructed-stage"
        prior = send_prefix.selected_records(self.root, key, None)
        before, checkpoint = self.log.path.read_bytes(), self.checkpoint.read_bytes()
        invalid_batch = ({"kind": "message_envelope"},)

        def decide(_records):
            return "unissued stage result", invalid_batch

        stage = None
        stage_type = getattr(send_prefix, "_SendStage", None)
        if stage_type is not None:
            base = prior._prefix
            candidate = copy.copy(base)
            candidate.payload, candidate.memo = copy.deepcopy(base.payload), dict(base.memo)
            try:
                stage = stage_type(base, candidate, decide, "unissued stage result", invalid_batch,
                    (b'{"unvalidated":"constructed-stage-frame"}\n',), self.temp.name, {})
            except TypeError:
                # An opaque replacement may expose only a no-argument token.
                # Constructing that token must still confer no authority.
                stage = stage_type()

        with self.assertRaises(ProtocolRefusal):
            jsonl.transact_records(self.root, "events.jsonl", decide,
                allowed_kinds=EVENT_KINDS, skip_unknown_kinds=True,
                _send_selection=(key, None, prior, stage))
        self.assertEqual(before, self.log.path.read_bytes(), "constructed stage appended unvalidated bytes")
        self.assertEqual(checkpoint, self.checkpoint.read_bytes(), "constructed stage advanced approval")

    def test_issued_send_stage_attributes_cannot_replace_native_encoded_notice(self):
        from floati import events
        self.warm()
        before = self.log.path.read_bytes()
        native_transaction = events.transact_records

        def mutate_token_before_admission(*args, **kwargs):
            selection = kwargs.get("_send_selection", ())
            stage = selection[3] if len(selection) == 4 else None
            if stage is not None:
                try:
                    stage.encoded = (b'{"unvalidated":"mutated-issued-token"}\n',)
                    stage.batch = ({"kind": "message_envelope"},)
                except (AttributeError, TypeError):
                    pass  # An immutable opaque token rejects these assignments.
            return native_transaction(*args, **kwargs)

        with patch.object(events, "transact_records", side_effect=mutate_token_before_admission):
            result = self.send("issued-stage-input")
        appended = self.log.path.read_bytes()[len(before):]
        self.assertIn(result["message"]["id"].encode(), appended,
            "mutable stage attributes replaced the native notice with unvalidated bytes")
        self.assertNotIn(b"mutated-issued-token", appended)
        self.assertEqual(result["message"], self.log.records()[-1])
        before_replay = self.log.path.read_bytes()
        with self.forbid_prefix_read():
            self.assertEqual(result, self.send("issued-stage-input"))
        self.assertEqual(before_replay, self.log.path.read_bytes())
        self.assert_no_pending_send_nodes()

    def test_dependency_revision_error_falls_back_and_appends_one_native_notice(self):
        from floati import jsonl, send_prefix
        self.warm()
        before = self.log.path.read_bytes()
        native = jsonl._read_path_records
        with patch.object(send_prefix, "_revision", side_effect=OSError("dependency metadata unavailable")),\
                patch.object(jsonl, "_read_path_records", wraps=native) as validator:
            result = self.send("context-fallback")
            replay = self.send("context-fallback")
        self.assertTrue(any(call.args[0] == self.log.path for call in validator.call_args_list),
            "unavailable dependency metadata must retain authoritative full-prefix validation")
        self.assertEqual(result, replay)
        self.assertTrue(self.log.path.read_bytes().startswith(before))
        envelopes = self.log.records()
        self.assertEqual(["first", "context-fallback"], [row["idempotency_key"] for row in envelopes])
        self.assertEqual([result["message"]],
            [row for row in envelopes if row["idempotency_key"] == "context-fallback"],
            "metadata recovery must append exactly one native message envelope")

    def test_dependency_revision_error_keeps_native_ack_receipt_and_prefix_refusal(self):
        from floati import jsonl, send_prefix
        from floati.cursor import SparseCursor
        first = self.warm()
        self.log.present("recipient")
        self.log.send("sender", "sender", "floati", "a" * 40, "other.md", "unrelated history",
            idempotency_key="unrelated-context-history")
        events_before = self.log.path.read_bytes()
        ack_path = self.root.resolve_relative("receipts/acks/recipient.jsonl")
        native = jsonl._read_path_records
        with patch.object(send_prefix, "_revision", side_effect=OSError("dependency metadata unavailable")),\
                patch.object(jsonl, "_read_path_records", wraps=native) as validator:
            receipt = SparseCursor(self.root).ack("recipient", [first["message"]["id"]],
                acting_session_id="context-fallback-ack")
            replay = SparseCursor(self.root).ack("recipient", [first["message"]["id"]],
                acting_session_id="context-fallback-ack")
        self.assertTrue(any(call.args[0] == self.log.path for call in validator.call_args_list),
            "ACK metadata recovery must validate the complete authoritative event prefix")
        self.assertEqual(receipt, replay)
        self.assertEqual([first["message"]["id"]], receipt["item_ids"])
        self.assertEqual("recipient", receipt["recipient"])
        self.assertEqual("context-fallback-ack", receipt["acting_session_id"])
        self.assertEqual([receipt], jsonl.read_records(self.root, "receipts/acks/recipient.jsonl",
            allowed_kinds={"ack_receipt"}))
        self.assertEqual(events_before, self.log.path.read_bytes())

        # A prior ACK must not bypass complete event-prefix integrity merely
        # because dependency metadata and the optimization are unavailable.
        corrupt = events_before + b'{"unrelated":"malformed-event-frame"}\n'
        self.log.path.write_bytes(corrupt)
        expected = self.native_failure()
        ack_before = ack_path.read_bytes()
        with patch.object(send_prefix, "_revision", side_effect=OSError("dependency metadata unavailable")),\
                self.assertRaises(IntegrityFailure) as caught:
            SparseCursor(self.root).ack("recipient", [first["message"]["id"]],
                acting_session_id="context-fallback-ack")
        self.assertEqual(expected, caught.exception.code)
        self.assertEqual(corrupt, self.log.path.read_bytes())
        self.assertEqual(ack_before, ack_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
