"""Task1 exact dispatch admission, replay and conversation privacy."""
from __future__ import annotations

from tests.readiness import wait_queue, wait_event
import io
import hashlib
from copy import deepcopy
from contextlib import ExitStack, contextmanager
import json
import multiprocessing
import os
import stat
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from floati import bus_epoch
from floati.cursor import SparseCursor
from floati.epoch_carry import build_carry_plan, carry_dependency_witness
from floati.errors import ProtocolRefusal
from floati.framing import decode_frames, encode_frame
from floati.events import EventLog
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.records import validate_record
from floati.root import FloatiRoot
from floati.support_bundle import create_support_bundle, derive_collectors
from floati.thread_observations import ThreadObserver
from floati.work import WorkLog
from tests.fixtures.adhoc_stage1 import AdhocFixture, declared_api
from tests.temp_roots import REAL_TEMP_ROOT


_PROFILE_ID = "adhoc-profile-0199e000000070008000000000000001"


def _fixture_digest(value, *, domain=b""):
    """Independent test-owned canonical JSON preimage, never a product builder."""
    return hashlib.sha256(domain + json.dumps(value, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _literal_profile_record():
    row = {
        "schema_version": 1,
        "id": _PROFILE_ID,
        "tenant_id": "fleet",
        "timestamp": "2026-10-16T12:00:00.000Z",
        "kind": "adhoc_profile_record",
        "node_id": "temporary",
        "node_lease_id": "lease-0199e000000070008000000000000002",
        "adapter": "codex",
        "lane_opening_id": "lane-workspace-0199e000000070008000000000000003",
        "repository": "fixture",
        "result_recipient": "coordinator",
        "node_home": "\x2fprivate/tmp/task1-record-fixture/fleet/nodes/temporary",
        "seat_digest": "1111111111111111111111111111111111111111111111111111111111111111",
        "workspace": "\x2fprivate/tmp/task1-record-fixture/lanes/temporary/work/adhoc-one",
        "git_common_dir": "\x2fprivate/tmp/task1-record-fixture/repository/.git",
        "git_admin_dir": "\x2fprivate/tmp/task1-record-fixture/repository/.git/worktrees/adhoc-one",
        "branch": "codex/lane/temporary/adhoc-one",
        "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "lease_expires_at": "2026-10-16T13:00:00.000Z",
        "provider_state": "provider-state/" + _PROFILE_ID,
        "filesystem_bindings": {
            "root": {"dev": 1, "ino": 101},
            "node_home": {"dev": 1, "ino": 102},
            "seat_file": {"dev": 1, "ino": 103},
            "workspace": {"dev": 1, "ino": 104},
            "git_common_dir": {"dev": 1, "ino": 105},
            "git_admin_dir": {"dev": 1, "ino": 106},
            "provider_state": {"dev": 1, "ino": 107},
        },
        "idempotency_key": "profile-one",
    }
    row["digest"] = _fixture_digest(row, domain=b"floati-adhoc-profile-v1\0")
    return row


def _literal_admission_record():
    profile = _literal_profile_record()
    dispatch = {
        "schema_version": 1,
        "profile_id": _PROFILE_ID,
        "profile_digest": profile["digest"],
        "node_id": "temporary",
        "node_lease_id": "lease-0199e000000070008000000000000002",
        "lane_opening_id": "lane-workspace-0199e000000070008000000000000003",
        "work_item_id": "work-0199e000000070008000000000000004",
        "authority_subject": "adhoc-work",
        "authority_epoch": 1,
        "operation": "start",
        "permission": {"mode": "on_request_deny_unattended", "approval_policy": "on-request",
                       "reviewer": "user", "sandbox": "workspace-write", "network": False},
        "requested_duration": 120,
        "result_document": "docs/evidence/result.json",
        "result_recipient": "coordinator",
    }
    canonical = (json.dumps(dispatch, sort_keys=True, separators=(",", ":")) + "\n").encode()
    raw_blob = (json.dumps(dispatch, indent=2) + "\n").encode()
    digest = hashlib.sha256(canonical).hexdigest()
    return {
        "schema_version": 1,
        "id": "adhoc-journal-0199e000000070008000000000000005",
        "tenant_id": "fleet",
        "timestamp": "2026-10-16T12:00:01.000Z",
        "kind": "adhoc_dispatch_record",
        "dispatch_id": "adhoc-dispatch-0199e000000070008000000000000006",
        "profile_id": _PROFILE_ID,
        "profile_digest": profile["digest"],
        "envelope_id": "msg-0199e000000070008000000000000007",
        "dispatch_digest": digest,
        "dispatch": dispatch,
        "actor": "temporary",
        "acting_session_id": "fixture-parent",
        "idempotency_key": "dispatch-one",
        "phase": "admitted",
        "evidence": {"repository": "fixture", "sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                     "doc": "docs/dispatch/dispatch-one.json",
                     "blob_sha256": hashlib.sha256(raw_blob).hexdigest()},
    }


class AdhocRecordBoundaryTests(unittest.TestCase):
    """Existing record validator must admit the closed new family, then refuse malformed rows."""
    kinds = frozenset({"adhoc_profile_record", "adhoc_dispatch_record"})

    def lawful(self, row):
        try:
            validated = validate_record(deepcopy(row), "fleet", self.kinds, integrity=False)
        except ProtocolRefusal as exc:
            self.fail("existing record boundary rejected lawful " + row["kind"] + ": " + exc.code)
        self.assertEqual(row, validated)

    def test_profile_record_rejects_unknown_fields_and_foreign_provider_slot(self):
        """Catches permissive extension fields or a profile writing another profile's private slot."""
        row = _literal_profile_record()
        self.lawful(row)  # Refusing every new-family row must not satisfy the negatives.
        for changes in ({"executable": "/untrusted/provider"},
                        {"provider_state": "provider-state/adhoc-profile-0199e000000070008000000000000009"},
                        {"schema_version": True}, {"adapter": "arbitrary"}):
            with self.subTest(changes=changes):
                changed = dict(row, **changes)
                changed["digest"] = _fixture_digest({key: value for key, value in changed.items()
                    if key != "digest"}, domain=b"floati-adhoc-profile-v1\0")
                with self.assertRaises(ProtocolRefusal):
                    validate_record(changed, "fleet", self.kinds, integrity=False)

    def test_admission_record_rejects_unknown_dispatch_fields_and_coordinate_mismatch(self):
        """Catches validating only the outer envelope while ignoring closed authority fields."""
        row = _literal_admission_record()
        self.assertNotEqual(row["dispatch_digest"], row["evidence"]["blob_sha256"],
                            "positive control: canonical identity differs from valid pretty Git bytes")
        self.lawful(row)
        mutations = (
            {"profile_id": "adhoc-profile-0199e000000070008000000000000009"},
            {"authority_epoch": True},
            {"executable": "/untrusted/provider"},
            {"operation": "latest"},
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                changed = deepcopy(row)
                changed["dispatch"].update(changes)
                raw = (json.dumps(changed["dispatch"], sort_keys=True,
                    separators=(",", ":")) + "\n").encode()
                changed["dispatch_digest"] = hashlib.sha256(raw).hexdigest()
                with self.assertRaises(ProtocolRefusal):
                    validate_record(changed, "fleet", self.kinds, integrity=False)

    def test_profile_record_requires_exact_nonboolean_descriptor_identity_bindings(self):
        """Catches missing, partial, extended or bool-shaped descriptor identity testimony."""
        row = _literal_profile_record()
        self.lawful(row)
        invalid_maps = []
        missing = deepcopy(row["filesystem_bindings"])
        del missing["provider_state"]
        invalid_maps.append(missing)
        extended = deepcopy(row["filesystem_bindings"])
        extended["foreign"] = {"dev": 1, "ino": 108}
        invalid_maps.append(extended)
        for identity in ({"dev": True, "ino": 107}, {"dev": 1, "ino": True},
                         {"dev": -1, "ino": 107}, {"dev": 1, "ino": 0},
                         {"dev": 1}, {"dev": 1, "ino": 107, "path": "/foreign"}):
            changed = deepcopy(row["filesystem_bindings"])
            changed["provider_state"] = identity
            invalid_maps.append(changed)
        for bindings in invalid_maps:
            with self.subTest(bindings=bindings):
                changed = dict(row, filesystem_bindings=bindings)
                changed["digest"] = _fixture_digest({key: value for key, value in changed.items()
                    if key != "digest"}, domain=b"floati-adhoc-profile-v1\0")
                with self.assertRaises(ProtocolRefusal):
                    validate_record(changed, "fleet", self.kinds, integrity=False)
        missing_field = dict(row)
        del missing_field["filesystem_bindings"]
        missing_field["digest"] = _fixture_digest({key: value for key, value in missing_field.items()
            if key != "digest"}, domain=b"floati-adhoc-profile-v1\0")
        with self.assertRaises(ProtocolRefusal):
            validate_record(missing_field, "fleet", self.kinds, integrity=False)


def _admit_in_parent_process(root_path, profile_id, envelope_id, dispatch, key, start, results):
    """One independent parent admission; no provider/worker child is launched."""
    from floati.adhoc_journal import AdhocJournal
    try:
        journal = AdhocJournal(FloatiRoot.open_direct_home(root_path))
        results.put(("ready", os.getpid()))
        if not wait_event(start, 'adhoc_journal: start synchronization'):
            results.put(("error", "admission start barrier timed out"))
            return
        admitted = journal.admit(profile_id, envelope_id, dispatch, actor="temporary",
            acting_session_id="fixture-parent", idempotency_key=key)
        results.put(("admitted", os.getpid(), admitted))
    except Exception as exc:
        results.put(("error", type(exc).__name__, getattr(exc, "code", str(exc))))


class AdhocJournalTests(AdhocFixture, unittest.TestCase):
    def setUp(self):
        declared_api(self, "floati.adhoc_profiles", "AdhocProfiles")
        declared_api(self, "floati.adhoc_journal", "AdhocJournal")
        super().setUp()

    @contextmanager
    def advanced_clock(self, observed):
        """Advance real time consumers only; all registry/authority/receipt data stays real."""
        from floati.adhoc_profiles import current_time as original_current_time

        class RegistryClock(datetime):
            @classmethod
            def now(cls, tz=None):
                instant = cls.fromisoformat(observed.isoformat())
                return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

        def profile_clock(now=None):
            return original_current_time(observed if now is None else now)

        with mock.patch("floati.registry.datetime", RegistryClock),\
                mock.patch("floati.adhoc_profiles.current_time", side_effect=profile_clock),\
                mock.patch("floati.adhoc_journal.current_time", side_effect=profile_clock):
            yield

    def test_exact_git_blob_admits_once_and_replays_without_later_mail(self):
        """Catches re-reading HEAD or presenting later arrivals during exact replay."""
        profile = self.profile()
        dispatch = self.dispatch(profile)
        envelope = self.envelope(dispatch)
        journal = self.journal()
        first = self.admit(journal, profile, envelope, dispatch)
        self.assertEqual("admitted", first["phase"])
        late = self.envelope(dispatch, name="later-mail")
        before = self.tree_bytes()
        replay = self.admit(self.journal(), profile, envelope, dispatch)
        self.assertEqual(first, replay)
        self.assertEqual(before, self.tree_bytes())
        self.assertIn(late["id"], self.events.unacked_ids("temporary"))
        self.assert_no_provider_effects()

    def test_pretty_git_dispatch_json_admits_without_a_compact_format_requirement(self):
        """Catches conflating exact Git-byte authentication with canonical parsed identity."""
        profile = self.profile()
        dispatch = self.dispatch(profile)
        pretty = json.dumps(dispatch, indent=2) + "\n"
        canonical = json.dumps(dispatch, sort_keys=True, separators=(",", ":")) + "\n"
        self.assertNotEqual(hashlib.sha256(pretty.encode()).hexdigest(),
                            hashlib.sha256(canonical.encode()).hexdigest())
        envelope = self.envelope(dispatch, raw=pretty, name="pretty-dispatch")
        admitted = self.admit(self.journal(), profile, envelope, dispatch)
        self.assertEqual("admitted", admitted["phase"])
        self.assertEqual(admitted, self.admit(self.journal(), profile, envelope, dispatch))

    def test_changed_blob_reusing_key_refuses(self):
        """Catches reusing one dispatch key for a second genuine coordinator blob."""
        profile = self.profile()
        dispatch = self.dispatch(profile)
        envelope = self.envelope(dispatch)
        journal = self.journal()
        first = self.admit(journal, profile, envelope, dispatch)
        changed = self.dispatch(profile, requested_duration=121)
        other = self.envelope(changed, name="changed-blob")
        before = self.tree_bytes()
        self.refuses("adhoc_dispatch_key_conflict", lambda:
            self.admit(journal, profile, other, changed))
        self.assertEqual(before, self.tree_bytes())
        self.assertEqual(first, self.admit(self.journal(), profile, envelope, dispatch))

    def test_two_keys_have_one_owner_through_recovery(self):
        """Catches releasing active ownership when a parent journal instance is lost."""
        profile = self.profile()
        dispatch = self.dispatch(profile)
        envelope = self.envelope(dispatch)
        first = self.admit(self.journal(), profile, envelope, dispatch, key="owner-one")
        second_envelope = self.envelope(dispatch, name="owner-two")
        recovered = self.journal()
        before = self.tree_bytes()
        self.refuses("adhoc_dispatch_busy", lambda:
            self.admit(recovered, profile, second_envelope, dispatch, key="owner-two"))
        self.assertEqual(before, self.tree_bytes())
        self.assertEqual(first, self.admit(recovered, profile, envelope, dispatch, key="owner-one"))
        self.assertEqual("admitted", recovered.show(first["dispatch_id"])["phase"])

    def test_same_key_admission_race_replays_one_owner_in_distinct_parent_processes(self):
        """Catches checking an idempotency key outside the durable admission lock."""
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("independent-parent fork race requires the supported POSIX host")
        profile = self.profile()
        dispatch = self.dispatch(profile)
        envelope = self.envelope(dispatch)
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        parents = [context.Process(target=_admit_in_parent_process, args=(
            str(self.root.path), profile["id"], envelope["id"], dispatch, "racing-same-key", start, results))
            for _ in range(2)]

        def cleanup():
            start.set()
            for process in parents:
                if process.pid is not None:
                    process.join(10)
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
            results.close()
            results.join_thread()

        self.addCleanup(cleanup)
        for process in parents:
            process.start()
        ready = [wait_queue(results, 'adhoc_journal: results frame'), wait_queue(results, 'adhoc_journal: results frame')]
        self.assertEqual(["ready", "ready"], [row[0] for row in ready], ready)
        self.assertEqual(2, len({row[1] for row in ready}))
        start.set()
        outcomes = [wait_queue(results, 'adhoc_journal: results frame'), wait_queue(results, 'adhoc_journal: results frame')]
        self.assertEqual(["admitted", "admitted"], [row[0] for row in outcomes], outcomes)
        self.assertEqual(outcomes[0][2], outcomes[1][2])
        for process in parents:
            process.join(10)
            self.assertEqual(0, process.exitcode)
        admitted = outcomes[0][2]
        before = self.tree_bytes()
        replay = self.admit(self.journal(), profile, envelope, dispatch, key="racing-same-key")
        self.assertEqual(admitted, replay)
        self.assertEqual(before, self.tree_bytes())
        self.refuses("adhoc_dispatch_busy", lambda:
            self.admit(self.journal(), profile, envelope, dispatch, key="race-second-owner"))

    def test_simultaneous_two_keys_admit_exactly_one_owner_and_one_busy(self):
        """Catches reserving the provider/row slot outside the cross-process lock."""
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("independent-parent fork race requires the supported POSIX host")
        profile = self.profile()
        dispatch = self.dispatch(profile)
        envelope = self.envelope(dispatch)
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        parents = [context.Process(target=_admit_in_parent_process, args=(
            str(self.root.path), profile["id"], envelope["id"], dispatch, key, start, results))
            for key in ("racing-owner-one", "racing-owner-two")]

        def cleanup():
            start.set()
            for process in parents:
                if process.pid is not None:
                    process.join(10)
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
            results.close()
            results.join_thread()

        self.addCleanup(cleanup)
        for process in parents:
            process.start()
        ready = [wait_queue(results, 'adhoc_journal: results frame'), wait_queue(results, 'adhoc_journal: results frame')]
        self.assertEqual(["ready", "ready"], [row[0] for row in ready], ready)
        self.assertEqual(2, len({row[1] for row in ready}))
        start.set()
        outcomes = [wait_queue(results, 'adhoc_journal: results frame'), wait_queue(results, 'adhoc_journal: results frame')]
        admitted = [row for row in outcomes if row[0] == "admitted"]
        refused = [row for row in outcomes if row[0] == "error"]
        self.assertEqual(1, len(admitted), outcomes)
        self.assertEqual([("error", "ProtocolRefusal", "adhoc_dispatch_busy")], refused)
        for process in parents:
            process.join(10)
            self.assertEqual(0, process.exitcode)
        self.assertEqual("admitted", self.journal().show(admitted[0][2]["dispatch_id"])["phase"])
        self.assert_no_provider_effects()

    def test_distinct_profiles_cannot_admit_two_owners_of_same_physical_row(self):
        """Catches using only profile ID as the exclusivity key for one borrowed worktree."""
        first = self.profile()
        second = self.profiles().create("temporary", "codex", self.opened["record"]["id"],
            "fixture", "coordinator", idempotency_key="second-profile", now=self.now)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["filesystem_bindings"]["workspace"], second["filesystem_bindings"]["workspace"])
        dispatch_one, dispatch_two = self.dispatch(first), self.dispatch(second)
        envelope_one = self.envelope(dispatch_one)
        envelope_two = self.envelope(dispatch_two, name="second-profile-dispatch")
        self.admit(self.journal(), first, envelope_one, dispatch_one)
        before = self.tree_bytes()
        self.refuses("adhoc_dispatch_busy", lambda:
            self.admit(self.journal(), second, envelope_two, dispatch_two, key="second-profile-owner"))
        self.assertEqual(before, self.tree_bytes())

    def test_replaced_lease_cannot_admit_an_existing_profile(self):
        """Catches a successor lease adopting a prior lease's profile/provider slot."""
        profile = self.profile()
        dispatch = self.dispatch(profile)
        envelope = self.envelope(dispatch)
        self.add_lease("temporary")
        journal = self.journal()
        before = self.tree_bytes()
        self.refuses("adhoc_lease_invalid", lambda:
            self.admit(journal, profile, envelope, dispatch))
        self.assertEqual(before, self.tree_bytes())

    def test_same_path_provider_slot_replacement_refuses_exact_dispatch_replay(self):
        """Catches an old admission silently adopting a different inode's provider history."""
        profile = self.profile()
        dispatch = self.dispatch(profile)
        envelope = self.envelope(dispatch)
        self.admit(self.journal(), profile, envelope, dispatch)
        self.replace_provider_slot_at_same_path(profile)
        recovered = self.journal()
        before = self.tree_bytes()
        self.refuses("adhoc_binding_changed", lambda:
            self.admit(recovered, profile, envelope, dispatch))
        self.assertEqual(before, self.tree_bytes())

    def test_argument_cannot_override_the_banked_dispatch_blob(self):
        """Only the declared coordinator's exact Git dispatch bytes authenticate admission."""
        profile = self.profile()
        dispatch = self.dispatch(profile)
        encoded = json.dumps(dispatch, sort_keys=True, separators=(",", ":"))
        cases = (
            ("supplied", {}, self.dispatch(profile,
                result_document="docs/evidence/substituted.json")),
            ("note", {"raw": "{}\n", "note": json.dumps(dispatch)}, dispatch),
            ("duplicate", {"raw": encoded[:-1] + ',"node_id":"foreign"}\n'}, dispatch),
            ("unknown", {"raw": encoded[:-1] + ',"executable":"/untrusted/provider"}\n'}, dispatch),
            ("coordinator", {"sender": "foreign"}, dispatch),
        )
        journal = self.journal()
        for name, envelope_arguments, supplied in cases:
            with self.subTest(name=name):
                envelope = self.envelope(dispatch, name=name, **envelope_arguments)
                before = self.tree_bytes()
                self.refuses("adhoc_dispatch_authentication_failed", lambda:
                    self.admit(journal, profile, envelope, supplied, key=name))
                self.assertEqual(before, self.tree_bytes())

    def test_journal_writer_requires_exact_reciprocal_owner_and_rejects_second_owner(self):
        """Catches foreign-ledger, subclass and competing writer capability issuance."""
        writer_type = declared_api(self, "floati.adhoc_journal", "AdhocJournalController")
        journal = self.journal()
        owner = writer_type(journal)
        # Construction establishes the sole owner; only that exact object may
        # obtain the ledger's private capability again.
        capability = journal._capability_for(owner)
        self.assertIs(capability, journal._capability_for(owner))
        before = self.tree_bytes()
        with self.assertRaises(ProtocolRefusal):
            journal._capability_for(object())
        with self.assertRaises(ProtocolRefusal):
            journal._append(object(), "adhoc-dispatch-0199e000000070008000000000000001",
                            "provider_opened", {"provider": "codex", "session_id": "forged", "session_file": None})
        foreign = writer_type(self.journal())
        with self.assertRaises(ProtocolRefusal):
            journal._capability_for(foreign)
        with self.assertRaises(ProtocolRefusal):
            writer_type(journal)
        class ChildWriter(writer_type):
            pass
        with self.assertRaises(ProtocolRefusal):
            ChildWriter(self.journal())
        self.assertEqual(before, self.tree_bytes())

    def test_roll_preserves_active_profile_and_saved_native_references(self):
        """Catches dropping active references already ACKed out of ordinary carry seeds."""
        profile = self.profile()
        dispatch = self.dispatch(profile)
        envelope = self.envelope(dispatch)
        admitted = self.admit(self.journal(), profile, envelope, dispatch)
        before = self.journal().show(admitted["dispatch_id"])
        rows, delivery = self.events.present("temporary", now=self.now)
        self.assertEqual([envelope["id"]], [row["id"] for row in rows])
        self.assertIsNotNone(delivery)
        ack = SparseCursor(self.root).ack("temporary", [envelope["id"]],
            acting_session_id="fixture-parent", now=self.now)
        self.assertEqual([], self.events.unacked_ids("temporary"),
                         "negative control requires the dispatch to leave ordinary unread seeds")
        AuthorityGrantStore(self.root).grant_exact("bus-epoch-roll", "coordinator", 1, self.now)

        def planner(root, old):
            plan = build_carry_plan(root, old)
            return plan.live_payloads, {"trigger": "ack", "policy": {"max_bytes": 1,
                "max_age_days": 7, "source": "operator_declared"},
                "observed_bytes": next(len(raw) for name, _plane, raw in old if name == "events.jsonl"),
                "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "epoch_started_at": None, "due_reasons": ["bytes"]}

        bus_epoch.roll_automatic_epoch(self.root, actor="coordinator", planner=planner)
        # Inspect actual live native frames before any projection/replay can use
        # archived history. All three required references must be live-carried.
        live_coordinates = (
            ("events.jsonl", envelope["id"]),
            ("receipts/deliveries/temporary.jsonl", delivery["id"]),
            ("receipts/acks/temporary.jsonl", ack["id"]),
        )
        for relative, native_id in live_coordinates:
            path = self.root.path / relative
            self.assertTrue(path.is_file(), relative)
            self.assertIn(native_id, [row["id"] for row in decode_frames(path.read_bytes())],
                          "active dependency must survive in live frames, without archive fallback")
        self.assertEqual(profile, self.profiles().get(profile["id"]))
        self.assertEqual(before, self.journal().show(admitted["dispatch_id"]))
        self.assertEqual(admitted, self.admit(self.journal(), profile, envelope, dispatch))
        self.refuses("adhoc_dispatch_busy", lambda:
            self.admit(self.journal(), profile, envelope, dispatch, key="post-roll-racer"))

    def test_manual_whole_epoch_roll_cannot_orphan_active_admission_dependencies(self):
        """Catches receipt-only manual replacement silently dropping an active owner's evidence."""
        profile = self.profile()
        dispatch = self.dispatch(profile)
        envelope = self.envelope(dispatch)
        admitted = self.admit(self.journal(), profile, envelope, dispatch)
        AuthorityGrantStore(self.root).grant_exact("bus-epoch-roll", "coordinator", 1, self.now)
        before = self.tree_bytes()
        self.refuses("adhoc_dispatch_busy", lambda:
            bus_epoch.roll_bus_epoch(self.root, actor="coordinator", idempotency_key="manual-active-owner"))
        self.assertEqual(before, self.tree_bytes())
        self.assertEqual(admitted, self.admit(self.journal(), profile, envelope, dispatch))

    def test_expired_exact_lease_keeps_historical_replay_and_acked_automatic_carry(self):
        """Catches fresh-operation lease authorization leaking into historical ownership reads."""
        profile = self.profile()
        dispatch = self.dispatch(profile)
        envelope = self.envelope(dispatch)
        admitted = self.admit(self.journal(), profile, envelope, dispatch)
        rows, delivery = self.events.present("temporary", now=self.now)
        self.assertEqual([envelope["id"]], [row["id"] for row in rows])
        self.assertIsNotNone(delivery)
        ack = SparseCursor(self.root).ack("temporary", [envelope["id"]],
            acting_session_id="fixture-parent", now=self.now)
        self.assertEqual([], self.events.unacked_ids("temporary"),
                         "ACKed dependency must not survive merely as an unread seed")
        expired = self.now + timedelta(hours=1, seconds=1)

        with self.advanced_clock(expired):
            # Positive clock control: the original real lease is expired under
            # Registry's default-clock route, which the old lane actor used.
            lease = self.registry.node_lease_state("temporary")
            self.assertEqual(self.lease["id"], lease["node_lease_id"])
            self.assertEqual("expired", lease["state"])
            self.refuses("node_lease_expired", lambda:
                self.registry.require_protocol_lease("temporary", act="fixture fresh-operation control"))
            before = self.tree_bytes()
            replay = self.journal().admit(profile["id"], envelope["id"], dispatch, actor="temporary",
                acting_session_id="fixture-parent", idempotency_key="dispatch-one")
            self.assertEqual(admitted, replay)
            self.assertEqual(before, self.tree_bytes())
            self.refuses("adhoc_lease_invalid", lambda:
                self.journal().admit(profile["id"], envelope["id"], dispatch, actor="temporary",
                    acting_session_id="fixture-parent", idempotency_key="expired-fresh-key"))
            self.assertEqual(before, self.tree_bytes())

            def planner(root, old):
                plan = build_carry_plan(root, old)
                return plan.live_payloads, {"trigger": "ack", "policy": {"max_bytes": 1,
                    "max_age_days": 7, "source": "operator_declared"},
                    "observed_bytes": next(len(raw) for name, _plane, raw in old if name == "events.jsonl"),
                    "observed_at": expired.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "epoch_started_at": None, "due_reasons": ["bytes"]}

            bus_epoch.roll_automatic_epoch(self.root, actor="coordinator", planner=planner)
            # Check actual live frames before any archive/projection fallback.
            for relative, native_id in (
                ("events.jsonl", envelope["id"]),
                ("receipts/deliveries/temporary.jsonl", delivery["id"]),
                ("receipts/acks/temporary.jsonl", ack["id"]),
            ):
                self.assertIn(native_id, [row["id"] for row in
                    decode_frames((self.root.path / relative).read_bytes())])
            self.assertEqual([admitted], self.journal().records(), "expiry never releases the active owner")
            self.assertEqual(admitted, self.journal().admit(profile["id"], envelope["id"], dispatch,
                actor="temporary", acting_session_id="fixture-parent", idempotency_key="dispatch-one"))
            after_roll = self.tree_bytes()
            self.refuses("adhoc_lease_invalid", lambda:
                self.journal().admit(profile["id"], envelope["id"], dispatch, actor="temporary",
                    acting_session_id="fixture-parent", idempotency_key="expired-post-roll-key"))
            self.assertEqual(after_roll, self.tree_bytes())

    def test_expired_historical_recovery_still_refuses_a_successor_lease(self):
        """Catches read-only recovery adopting a replacement lease's private slot/row authority."""
        profile = self.profile()
        dispatch = self.dispatch(profile)
        envelope = self.envelope(dispatch)
        admitted = self.admit(self.journal(), profile, envelope, dispatch)
        expired = self.now + timedelta(hours=1, seconds=1)
        successor = self.add_lease("temporary", expires=expired + timedelta(hours=1))
        with self.advanced_clock(expired):
            replacement = self.registry.node_lease_state("temporary")
            self.assertEqual(successor["id"], replacement["node_lease_id"])
            self.assertEqual("active", replacement["state"])
            before = self.tree_bytes()
            self.refuses("adhoc_lease_invalid", lambda:
                self.journal().admit(profile["id"], envelope["id"], dispatch, actor="temporary",
                    acting_session_id="fixture-parent", idempotency_key="dispatch-one"))
            self.assertEqual(before, self.tree_bytes())
            self.assertEqual([admitted], self.journal().records())


class AdhocProviderStatePrivacyTests(unittest.TestCase):
    """Baseline-exercisable privacy controls; no ad-hoc API imports."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="adhoc-provider-privacy-", dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        # A private TMPDIR may itself have a tmp-prefixed basename on Linux.
        # Retain that component on every host so support export exercises the
        # same strict identity fence instead of relying on the host temp name.
        self.root = FloatiRoot.open_direct_home(
            self.base / "tmp-provider-fixture" / "fleet", create=True)
        self.private = self.root.path / "provider-state"
        self.slot = self.private / "adhoc-profile-0199e000000070008000000000000001"
        self.slot.mkdir(parents=True, mode=0o700)
        self.slot.chmod(0o700)
        self.canary = b"TASK1-CONVERSATION-CANARY-8ea07ce3"
        self.transcript = self.slot / "native-transcript.jsonl"
        self.transcript.write_bytes(b'{"text":"' + self.canary + b'"}\n')
        self.transcript.chmod(0o600)
        for node in ("coordinator", "temporary"):
            Registry(self.root).register(node, "Codex")

    def collectors(self):
        return derive_collectors(self.root, Path(__file__).resolve().parents[1], 240)

    def observe_private_recursion(self, action):
        original = os.scandir
        entered = []

        def inspect(path):
            if not isinstance(path, int):
                coordinate = Path(path)
                if coordinate == self.private or self.private in coordinate.parents:
                    entered.append(str(coordinate))
            return original(path)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("os.scandir", side_effect=inspect))
            # Python 3.9 caches the original scandir on pathlib's accessor.
            # Delegate both real entry points; newer pathlib uses global os.
            accessor = getattr(self.root.path, "_accessor", None)
            if accessor is not None:
                stack.enter_context(mock.patch.object(accessor, "scandir", side_effect=inspect))
            result = action()
        return result, entered

    def record_private_recursion(self, action):
        result, entered = self.observe_private_recursion(action)
        self.assertEqual([], entered, "epoch/export truth must prune conversation storage before recursion")
        return result

    def test_recursion_observer_detects_actual_private_path_traversal(self):
        """Positive control prevents a cached filesystem accessor hiding traversal."""
        found, entered = self.observe_private_recursion(lambda: list(self.private.rglob("*")))
        self.assertIn(self.transcript, found)
        self.assertTrue(entered, "real traversal of private history must be observed")

    def test_support_bundle_never_registers_provider_transcript_collector(self):
        """Catches exporting provider JSONL as if it were a Floati receipt plane."""
        names = [name for name, _collector in self.collectors()]
        self.assertIn("ledger-registry-entries.jsonl", names, "positive control: real bus ledger remains exportable")
        self.assertEqual([], [name for name in names if "provider-state" in name])

    def test_support_collector_enumeration_prunes_provider_state_before_recursing(self):
        """Catches filtering transcript collectors only after inspecting history directories."""
        self.record_private_recursion(self.collectors)

    def test_epoch_dependency_witness_never_enumerates_provider_history(self):
        """Catches depending on private transcript membership for bus epoch truth."""
        witness = self.record_private_recursion(lambda: carry_dependency_witness(self.root))
        self.assertIsNotNone(witness, "unavailable witness must not mask private traversal")
        self.assertFalse(any("native-transcript.jsonl" in str(row) for row in witness))

    def test_roll_never_archives_carries_or_exports_provider_state_bytes(self):
        """Canary negative control across actual carry, staging, archive and support export."""
        now = datetime.now(timezone.utc)
        envelope = EventLog(self.root).send("coordinator", "temporary", "fixture", "a" * 40,
            "docs/evidence/dispatch.json", "coordinate-only roll fixture", idempotency_key="privacy", now=now)
        item = WorkLog(self.root).add("retained native reference", "temporary", [], now=now)
        native_id = "0199e000-0000-7000-8000-000000000001"
        attachment = ThreadObserver(self.root).register_work_item(
            item["id"], native_id, "temporary", now=now)
        AuthorityGrantStore(self.root).grant_exact("bus-epoch-roll", "coordinator", 1, now)
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            selected = bus_epoch._preflight_selected_members(self.root)
            payloads = bus_epoch._read_selected_epoch_payloads(self.root, selected)
            plan = build_carry_plan(self.root, payloads)
        for relative, _plane, raw in plan.live_payloads:
            self.assertFalse(relative.startswith("provider-state/"))
            self.assertNotIn(self.canary, raw)
        observed_staging = []

        def inspect_staging(_boundary):
            for path in self.root.path.rglob("*"):
                if path.is_file() and self.private not in path.parents:
                    data = path.read_bytes()
                    self.assertNotIn(self.canary, data, str(path))
                    observed_staging.append(path)

        result = bus_epoch.roll_bus_epoch(self.root, actor="coordinator",
            idempotency_key="privacy-roll", fault=inspect_staging)
        self.assertTrue(observed_staging, "positive control: real roll boundaries were observed")
        archive = Path(result["receipt"]["archive_path"])
        for path in archive.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.canary, path.read_bytes())
                self.assertNotIn("provider-state", path.relative_to(archive).parts)
        out = self.base / "support.tar.gz"
        # Materialize actual derived LIVE-ledger collectors. Rolled archive
        # names exceed the existing collector-name bound independently of
        # provider privacy; archive bytes were fully checked above.
        collectors = self.collectors()
        self.assertEqual([], [name for name, _collector in collectors if "provider-state" in name],
                         "private collector must be excluded before native name validation/materialization")
        ledger_collectors = tuple((name, collector) for name, collector in collectors
                                  if name.startswith("ledger-") and not name.startswith("ledger-archive-"))
        self.assertTrue({"ledger-registry-entries.jsonl", "ledger-events.jsonl"}.issubset(
            {name for name, _collector in ledger_collectors}),
            "positive control: genuine live registry/events collectors must be selected")
        create_support_bundle(root=self.root, source=Path(__file__).resolve().parents[1],
            out=out, lines=240, yes=True, stream=io.StringIO(), input_stream=io.StringIO(),
            collectors=ledger_collectors)
        sections = {}
        with tarfile.open(out, "r:gz") as bundle:
            for member in bundle.getmembers():
                if member.isfile():
                    sections[member.name] = bundle.extractfile(member).read()
                    self.assertNotIn(self.canary, sections[member.name])
                    self.assertNotIn("provider-state", member.name)
        registry_section = json.loads(sections["collectors/ledger-registry-entries.jsonl.json"])
        self.assertEqual("ok", registry_section["status"])
        self.assertEqual({"coordinator", "temporary"}, {row["node_id"]
            for row in registry_section["records"] if row["kind"] == "registry_entry"})
        events_section = json.loads(sections["collectors/ledger-events.jsonl.json"])
        self.assertEqual("ok", events_section["status"])
        self.assertEqual([result["receipt"]["id"]], [row["id"] for row in events_section["records"]])
        self.assertEqual("bus_epoch_roll_receipt", events_section["records"][0]["kind"])
        self.assertEqual("coordinator", events_section["records"][0]["actor"])
        self.assertEqual("privacy-roll", events_section["records"][0]["idempotency_key"])
        self.assertEqual(b'{"text":"' + self.canary + b'"}\n', self.transcript.read_bytes())
        self.assertEqual(0o700, stat.S_IMODE(self.slot.stat().st_mode))
        self.assertIn(envelope["id"].encode(), (archive / "events.jsonl").read_bytes())
        retained = ThreadObserver(self.root).ledger.project().attachment(attachment["id"])["attachment"]
        self.assertEqual(native_id, retained["provider_thread_id"])
        self.assertEqual("codex_local", retained["provider"])


class AdhocSealedLedgerTests(AdhocFixture, unittest.TestCase):
    def native_pair(self):
        profile = self.profile()
        dispatch = self.dispatch(profile)
        envelope = self.envelope(dispatch)
        journal = self.journal()
        admitted = self.admit(journal, profile, envelope, dispatch)
        return profile, admitted, journal

    def test_generic_record_and_raw_frame_routes_cannot_mint_adhoc_truth_anywhere(self):
        """Catches exact-family and foreign-ledger bypasses of native authentication."""
        from floati.adhoc_records import PROFILE_RELATIVE, JOURNAL_RELATIVE
        from floati.jsonl import append_record, transact, transact_records, transact_exact_frame, _append_frame
        profile, admitted, journal = self.native_pair()
        self.assertEqual([profile], self.profiles().records())
        self.assertEqual([admitted], journal.records())
        self.assertEqual("adhoc_profile_record", profile["kind"])
        self.assertEqual("adhoc_dispatch_record", admitted["kind"])
        self.assertEqual(profile["id"], admitted["profile_id"])
        native_before = {relative: (self.root.path / relative).read_bytes()
                         for relative in (PROFILE_RELATIVE, JOURNAL_RELATIVE)}
        for row, native in ((profile, PROFILE_RELATIVE), (admitted, JOURNAL_RELATIVE)):
            kinds = {row["kind"]}
            foreign = "scratch/foreign-" + row["kind"] + ".jsonl"
            for relative in (native, foreign):
                routes = (
                    lambda: append_record(self.root, relative, row, allowed_kinds=kinds),
                    lambda: transact(self.root, relative, lambda _rows: (row, row), allowed_kinds=kinds),
                    lambda: transact_records(self.root, relative, lambda _rows: (row, [row]), allowed_kinds=kinds),
                    lambda: transact_exact_frame(self.root, relative, lambda _rows, _lines: (row, row), allowed_kinds=kinds),
                    lambda: _append_frame(self.root.path / relative, encode_frame(row)),
                )
                for index, route in enumerate(routes):
                    with self.subTest(kind=row["kind"], relative=str(relative), route=index):
                        self.refuses("adhoc_dispatch_authentication_failed", route)
                        self.assertEqual(native_before, {name: (self.root.path / name).read_bytes()
                            for name in (PROFILE_RELATIVE, JOURNAL_RELATIVE)})
                self.assertFalse((self.root.path / foreign).exists())
        # A generic historical kind cannot use the protected family either.
        registry = self.registry.require_active("temporary")
        self.refuses("adhoc_dispatch_authentication_failed", lambda:
            append_record(self.root, "adhoc/foreign.jsonl", registry, allowed_kinds={"registry_entry"}))
        self.refuses("adhoc_dispatch_authentication_failed", lambda:
            _append_frame(self.root.path / "adhoc/foreign.jsonl", encode_frame(registry)))
        self.assertFalse((self.root.path / "adhoc/foreign.jsonl").exists())
        self.assertEqual([admitted], journal.records())

    def test_raw_native_transaction_and_internal_owner_routes_refuse_foreign_callers(self):
        """Catches calling a private native route without its real create/admit checks."""
        from floati.jsonl import _transact_adhoc_records
        profile, admitted, journal = self.native_pair()
        before = self.tree_bytes()
        for route in (
            lambda: _transact_adhoc_records(self.root, lambda _rows: (profile, profile)),
            lambda: self.profiles()._transact_profile(lambda _rows: (profile, profile)),
            lambda: journal._transact_admission(lambda _rows: (admitted, admitted)),
        ):
            self.refuses("adhoc_dispatch_authentication_failed", route)
            self.assertEqual(before, self.tree_bytes())


if __name__ == "__main__":
    unittest.main()
