"""A quiet permission request is a factual observation, never a card state."""
from tests.readiness import wait_barrier, wait_event, wait_join
from datetime import timedelta
import errno
import importlib
import json
import threading
import unittest
from unittest.mock import patch

from floati.events import EventLog
from tests import test_seat_execution_host_tool_native as native_fixture


class HostPermissionObservationsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.HostToolCallbackNativeTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.now = self.fixture.now

    def observer(self):
        try:
            return importlib.import_module("floati.host_observations")
        except ModuleNotFoundError:
            self.fail("native permission observation producer is absent")

    def payload(self):
        payload = self.fixture.payload()
        payload["hook_event_name"] = "PermissionRequest"
        del payload["tool_use_id"]
        del payload["tool_response"]
        return payload

    def observe(self, payload=None, at=None):
        return self.observer().observe_codex_host(self.root.path,
            self.payload() if payload is None else payload,
            now=self.now if at is None else at, launch=False)

    def publish(self, occurrence, seconds):
        return self.observer()._publish_due(occurrence.controller,
            occurrence.capability, occurrence.observation,
            now=self.now + timedelta(seconds=seconds))

    def test_899_seconds_is_silent_and_900_posts_one_factual_atomic_outcome(self):
        occurrence = self.observe()
        self.assertIsNotNone(occurrence)
        self.assertIsNone(self.publish(occurrence, 899))
        result = self.publish(occurrence, 900)
        self.assertIsNotNone(result)
        rows = EventLog(self.root).event_records()
        facts = [row for row in rows if row["kind"] == "seat_execution"]
        self.assertEqual(1, len(facts), "observation must not invent running testimony")
        self.assertEqual("permission_quiet", facts[0]["event"])
        self.assertIsNone(facts[0]["command"])
        self.assertIsNone(facts[0]["code"])
        self.assertIsNone(facts[0]["detail"])
        envelopes = [row for row in rows if row["kind"] == "message_envelope"]
        self.assertEqual(1, len(envelopes))
        self.assertEqual(facts[0]["envelope_id"], envelopes[0]["id"])
        self.assertEqual(1, sum(row["kind"] == "send_readiness_observation" for row in rows))
        self.assertIn("permission requested at ", envelopes[0]["note"])
        self.assertIn("for Bash; no later host event recorded for this session", envelopes[0]["note"])
        self.assertNotIn("STALLED", envelopes[0]["note"])
        replay = self.publish(occurrence, 901)
        self.assertIsNotNone(replay)
        self.assertEqual(result["execution"]["id"], replay["execution"]["id"])
        self.assertEqual(result["execution"]["envelope_id"], replay["execution"]["envelope_id"])
        self.assertEqual(1, sum(row["kind"] == "message_envelope"
            for row in EventLog(self.root).event_records()))

    def test_successful_tool_in_another_turn_clears_before_refusal_filtering(self):
        occurrence = self.observe()
        payload = self.fixture.payload(0, "success")
        payload["turn_id"] = "a-later-turn"
        self.observe(payload, self.now + timedelta(seconds=2))
        self.assertIsNone(self.publish(occurrence, 900))
        self.assertEqual([], EventLog(self.root).event_records())

    def test_later_event_clears_a_published_fact_without_rewriting_history(self):
        occurrence = self.observe()
        self.publish(occurrence, 900)
        projection = self.observer().host_approval_projection(self.root, "builder")
        self.assertEqual("FACT", projection["state"])
        payload = self.fixture.payload(0, "success")
        self.observe(payload, self.now + timedelta(seconds=901))
        projection = self.observer().host_approval_projection(self.root, "builder")
        self.assertEqual("UNKNOWN", projection["state"])
        self.assertEqual("later_host_event_observed", projection["reason"])
        self.assertEqual(1, sum(row["kind"] == "message_envelope"
            for row in EventLog(self.root).event_records()))

    def test_unclaimed_foreign_session_cannot_clear_or_publish(self):
        occurrence = self.observe()
        payload = self.fixture.payload(0, "success")
        payload["session_id"] = "another-session"
        self.assertIsNone(self.observe(payload, self.now + timedelta(seconds=2)))
        self.assertIsNotNone(self.publish(occurrence, 900))

    def test_no_car_and_forged_selector_do_not_create_observation_authority(self):
        self.fixture.work.complete(self.fixture.item["id"], "builder",
            [native_fixture.BINDING], now=self.now)
        self.assertIsNone(self.observe())
        payload = self.payload()
        payload["node_id"] = "builder"
        payload["authenticated"] = True
        self.assertIsNone(self.observe(payload))
        self.assertEqual([], EventLog(self.root).event_records())

    def test_interrupt_posts_supported_fact_and_clears_old_permission_observation(self):
        permission = self.observe()
        payload = self.payload()
        payload["hook_event_name"] = "Interrupt"
        del payload["tool_name"]
        del payload["tool_input"]
        self.observe(payload, self.now + timedelta(seconds=2))
        self.assertIsNone(self.publish(permission, 900))
        rows = EventLog(self.root).event_records()
        facts = [row for row in rows if row["kind"] == "seat_execution"]
        self.assertEqual(1, len(facts))
        self.assertEqual("interrupted", facts[0]["event"])
        self.assertIsNone(facts[0]["command"])
        self.assertIsNone(facts[0]["code"])
        self.assertIsNone(facts[0]["detail"])
        envelopes = [row for row in rows if row["kind"] == "message_envelope"]
        self.assertEqual(1, len(envelopes))
        self.assertIn("turn interrupted at ", envelopes[0]["note"])
        self.assertNotIn("STALLED", envelopes[0]["note"])

    def test_concurrent_equal_requests_at_one_timestamp_allocate_distinct_sequences(self):
        self.observer()
        barrier, results, errors = threading.Barrier(2), [], []
        def observe_once():
            try:
                wait_barrier(barrier, "test_host_permission_observations barrier")
                results.append(self.observe())
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=observe_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            wait_join(thread, "test_host_permission_observations thread")
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual([1, 2], sorted(row.observation["sequence"] for row in results))
        results.sort(key=lambda row: row.observation["sequence"])
        self.assertIsNone(self.publish(results[0], 900))
        self.assertIsNotNone(self.publish(results[1], 900))
        self.assertEqual(1, sum(row["kind"] == "message_envelope"
            for row in EventLog(self.root).event_records()))

    def test_publication_guard_orders_later_invalidation_after_the_atomic_outcome(self):
        occurrence = self.observe()
        entered, release, cleared = threading.Event(), threading.Event(), threading.Event()
        results, errors = [], []
        callback = occurrence.controller._callback_already_guarded
        def hold_native_callback(*args, **kwargs):
            entered.set()
            if not wait_event(release, "test_host_permission_observations release"):
                raise TimeoutError("fixture callback release was lost")
            return callback(*args, **kwargs)
        def publish():
            try:
                results.append(self.publish(occurrence, 900))
            except BaseException as exc:
                errors.append(exc)
        def clear():
            try:
                self.observe(self.fixture.payload(0, "later success"), self.now + timedelta(seconds=901))
                cleared.set()
            except BaseException as exc:
                errors.append(exc)
        publisher, invalidator = threading.Thread(target=publish), threading.Thread(target=clear)
        with patch.object(occurrence.controller, "_callback_already_guarded", side_effect=hold_native_callback):
            try:
                publisher.start()
                self.assertTrue(wait_event(entered, "test_host_permission_observations entered"))
                invalidator.start()
                self.assertFalse(cleared.wait(0.05), "invalidation escaped the retained publication guard")
            finally:
                release.set()
                wait_join(publisher, "test_host_permission_observations publisher")
                if invalidator.ident is not None:
                    wait_join(invalidator, "test_host_permission_observations invalidator")
        self.assertFalse(publisher.is_alive() or invalidator.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, len(results))
        self.assertIsNotNone(results[0])
        self.assertTrue(cleared.is_set())
        self.assertEqual("UNKNOWN", self.observer().host_approval_projection(self.root, "builder")["state"])
        self.assertEqual(1, sum(row["kind"] == "message_envelope"
            for row in EventLog(self.root).event_records()))

    def test_pause_after_observation_prevents_publication(self):
        from floati.codex_wait_contract import CodexWaitSessionLedger, resolve_participant
        from floati.wake_control import WakeController
        for index, invalidator in enumerate(("pause", "new_session")):
            with self.subTest(case=invalidator):
                if index:
                    self.doCleanups()
                    self.setUp()
                occurrence = self.observe()
                if invalidator == "pause":
                    WakeController(self.root).pause("builder", self.payload()["session_id"],
                        idempotency_key="fixture-host-pause")
                else:
                    participant = resolve_participant(self.root.path, self.fixture.workspace)
                    CodexWaitSessionLedger(self.root).arm(participant.binding, self.fixture.consent,
                        "new-native-session", idempotency_key="fixture-host-new-session", take_over=True)
                self.assertIsNone(self.publish(occurrence, 900))
                self.assertEqual("UNKNOWN", self.observer().host_approval_projection(self.root, "builder")["state"])
                self.assertEqual([], EventLog(self.root).event_records())

    def test_current_fact_drops_after_car_completion_while_historical_mail_remains(self):
        from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant
        for index, invalidator in enumerate(("car_completion", "consent_replacement")):
            with self.subTest(case=invalidator):
                if index:
                    self.doCleanups()
                    self.setUp()
                occurrence = self.observe()
                self.assertIsNotNone(self.publish(occurrence, 900))
                if invalidator == "car_completion":
                    self.fixture.work.complete(self.fixture.item["id"], "builder",
                        [native_fixture.BINDING], now=self.now + timedelta(seconds=901))
                else:
                    participant = resolve_participant(self.root.path, self.fixture.workspace)
                    CodexWaitConsentLedger(self.root).arm(participant.binding,
                        hook_timeout_seconds=3, wait_deadline_seconds=1,
                        idempotency_key="fixture-host-new-consent")
                self.assertEqual("UNKNOWN", self.observer().host_approval_projection(self.root, "builder")["state"])
                self.assertEqual(1, sum(row["kind"] == "message_envelope"
                    for row in EventLog(self.root).event_records()))

    def test_native_canonical_subdirectory_resolves_the_claimed_workspace_root(self):
        nested = self.fixture.workspace / "nested"
        nested.mkdir()
        payload = dict(self.payload(), cwd=str(nested))
        occurrence = self.observe(payload)
        self.assertIsNotNone(occurrence, "native activity inside the mapped workspace lost its root claim")
        self.assertIsNotNone(self.publish(occurrence, 900))

    def test_cached_tool_turn_or_event_cannot_relabel_an_accepted_fact(self):
        occurrence = self.observe()
        self.assertIsNotNone(self.publish(occurrence, 900))
        path = self.observer()._state_path(self.root, "builder")
        original = json.loads(path.read_text())
        for change in ({"tool_name": "Read"}, {"turn_id": "forged-later-turn"},
                       {"event": "Interrupt", "tool_name": None}):
            with self.subTest(change=change):
                try:
                    path.write_text(json.dumps({**original, **change}))
                    self.assertEqual("UNKNOWN",
                        self.observer().host_approval_projection(self.root, "builder")["state"],
                        "cache metadata relabeled durable source evidence")
                finally:
                    path.write_text(json.dumps(original))
        self.assertEqual(1, sum(row["kind"] == "message_envelope"
            for row in EventLog(self.root).event_records()))

    def test_unhashable_native_metadata_is_silent_and_cannot_clear(self):
        occurrence = self.observe()
        for change in ({"hook_event_name": []}, {"permission_mode": {}},
                       {"hook_event_name": ["PermissionRequest"]}):
            with self.subTest(change=change):
                self.assertIsNone(self.observe({**self.payload(), **change}))
        self.assertIsNotNone(self.publish(occurrence, 900))


    def test_new_second_car_invalidates_admitted_and_published_host_occurrences(self):
        occurrence = self.observe()
        self.assertIsNotNone(self.publish(occurrence, 900))
        second = self.fixture.work.add("second claimed car", "builder", [native_fixture.BINDING],
            now=self.now, car_binding={"row": "second-car", "repository": "floati", "branch": "codex/second"})
        self.fixture.work.claim(second["id"], "builder", "work-claims", self.fixture.grant["epoch"], now=self.now)
        self.assertIsNone(self.publish(occurrence, 901))
        self.assertEqual("UNKNOWN", self.observer().host_approval_projection(self.root, "builder")["state"])
        self.assertIsNone(self.observe(at=self.now + timedelta(seconds=2)))
        self.fixture.work.complete(second["id"], "builder", [native_fixture.BINDING], now=self.now)
        self.assertIsNone(self.publish(occurrence, 903), "later authenticated activity survived ambiguity resolution")
        self.assertEqual("UNKNOWN", self.observer().host_approval_projection(self.root, "builder")["state"])
        self.assertEqual(1, sum(row["kind"] == "message_envelope" for row in EventLog(self.root).event_records()))

    def test_failed_later_event_write_invalidates_pending_and_published_occurrences(self):
        for published in (False, True):
            with self.subTest(published=published):
                occurrence = self.observe()
                self.assertIsNotNone(occurrence)
                if published:
                    self.assertIsNotNone(self.publish(occurrence, 900))
                with patch.object(self.observer(), "_write_state", side_effect=OSError("fixture invalidation persistence failed")):
                    self.assertIsNone(self.observe(self.fixture.payload(0, "later activity"),
                        self.now + timedelta(seconds=901)))
                self.assertIsNone(self.publish(occurrence, 902))
                self.assertEqual("UNKNOWN", self.observer().host_approval_projection(self.root, "builder")["state"])
                # A poisoned missing state is deliberately not reinitialized.
                # Use a fresh fixture for the other branch of this control.
                if not published:
                    self.doCleanups()
                    self.setUp()

    def test_replacement_and_poison_failure_cannot_preserve_old_testimony(self):
        # Seat31 §5 amends this named control: the FACT is about recorded
        # activity, and a failing local store yields typed UNKNOWN + errno.
        import os
        occurrence = self.observe()
        self.assertIsNotNone(self.publish(occurrence, 900))
        path = self.observer()._state_path(self.root, "builder")
        original = path.read_bytes()
        native_unlink, native_open = os.unlink, os.open
        def refuse_poison(path, *args, **kwargs):
            if str(path).endswith("latest.json"):
                raise OSError(errno.ENOSPC, "fixture prior observation cannot be removed")
            return native_unlink(path, *args, **kwargs)
        def unwritable_observation_directory(path, flags, *args, **kwargs):
            if flags & os.O_CREAT and kwargs.get("dir_fd") is not None:
                raise OSError(errno.ENOSPC, "fixture observation directory is not writable")
            return native_open(path, flags, *args, **kwargs)
        with patch("os.open", side_effect=unwritable_observation_directory):
            with patch.object(self.observer(), "_write_state",
                    side_effect=OSError(errno.ENOSPC, "fixture replacement failed")),\
                    patch("os.unlink", side_effect=refuse_poison):
                self.assertIsNone(self.observe(self.fixture.payload(0, "later activity"),
                    self.now + timedelta(seconds=901)))
            self.assertEqual(original, path.read_bytes(), "old bytes must remain readable in this control")
            self.assertIsNone(self.publish(occurrence, 902))
            projection = self.observer().host_approval_projection(self.root, "builder")
            self.assertEqual("UNKNOWN", projection["state"])
            self.assertIn("storage_not_writable", projection["reason"])
            self.assertEqual(errno.ENOSPC, projection["errno"])
        # The lost event is not reconstructed after storage recovers. The
        # current FACT and its exact note claim only what Floati recorded.
        self.assertEqual("FACT", self.observer().host_approval_projection(self.root, "builder")["state"])
        envelopes = [row for row in EventLog(self.root).event_records() if row["kind"] == "message_envelope"]
        self.assertEqual(1, len(envelopes))
        self.assertIn("for Bash; no later host event recorded for this session", envelopes[0]["note"])


if __name__ == "__main__":
    unittest.main()
