"""Native private write-workload controls; never performance observations."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import base64
import copy
from tests.bus_scale_test_profile import clone_fixture
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from tests.bus_scale_test_profile import StableSourceFixture
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import tests
from floati import cli
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import full_scale_enabled, load_fixture_module, bounded_cli_parser, stable_fixture_sources

CASES = frozenset({
    "consume", "hold-start", "hold-release", "presence-report", "work-add",
    "work-claim", "work-complete", "inbox", "ack", "node-drain",
    "confluence-grant", "confluence-revoke", "confluence-status",
    "confluence-bundle", "intake-scan", "intake-show", "intake-preview",
})
HELPER = ROOT / "tests/fixtures/bus_scale_census/bus-scale-routine-writes-2026-10-15.py"


# Every native write case retains two independent default starts. The
# explicit profile additionally retains full census transport and archives.
_DEFAULT_CASES = CASES


def _fixture_identity(base):
    return tuple((path.relative_to(base).as_posix(), metadata.st_dev, metadata.st_ino,
                  metadata.st_mode, metadata.st_uid, metadata.st_gid, metadata.st_nlink,
                  metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
                 for path in [base, *sorted(base.rglob("*"))]
                 for metadata in [path.lstat()])


def load(path, name):
    return load_fixture_module(path, name)


def inventory(home):
    return {p.relative_to(home).as_posix(): p.read_bytes()
            for p in home.rglob("*") if p.is_file()}


def delta(before, after):
    from tests.fixtures.bus_scale_census.bus_scale_cursor_prefix_effects import raw_candidate_delta
    # Keep replacements and removals visible to the complete native proof;
    # fixed membership-directory and bucket bytes are not JSONL appends.
    return raw_candidate_delta(before, after)


class RoutineWriteTests(StableSourceFixture, unittest.TestCase):
    _completed_controls = {}

    @classmethod
    def tearDownClass(cls):
        for cleanup, _record, _identity in cls._completed_controls.values():
            cleanup()
        cls._completed_controls.clear()

    @contextmanager
    def completed_control(self, case, *, clones=False):
        """Share real completed work only while its native disk identity holds."""
        cache = type(self)._completed_controls
        source = tuple((str(path), hashlib.sha256(path.read_bytes()).hexdigest())
                       for path in (HELPER, ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py"))
        key = full_scale_enabled(), case, source
        entry = cache.get(key)
        if entry is not None and _fixture_identity(entry[1]["bank"]) != entry[2]:
            cache.pop(key)
            entry[0]()
            entry = None
        if entry is None:
            owner = type(self)()
            try:
                owner.setUp()
                helper = owner.helper()
                raw_events = (owner.home / "events.jsonl").read_bytes()
                context = helper.prepare(case, owner.home, owner.workspace, owner.bank)
                self.assertEqual(raw_events, (owner.home / "events.jsonl").read_bytes())
                json.dumps(context, allow_nan=False)
                entry_module = load(ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py",
                                    "routine_write_pair_entry")
                record = dict(bank=owner.bank, seed=owner.home, workspace=owner.workspace,
                              context=context, raw_events=raw_events)
                results, observations = [], []
                for mode in ("cpu", "diagnostic"):
                    parent = owner.bank / mode
                    parent.mkdir(mode=0o700)
                    home = parent / "scale-fleet"
                    shutil.copytree(owner.home, home)
                    observed = {}
                    argv = entry_module.arguments(case, home, owner.workspace, context)
                    result = owner.execute(helper, case, home, context, argv=argv, observation=observed)
                    observed["mode"] = mode
                    results.append(result[0])
                    observations.append(observed)
                    # The final independent start is the still-current genuine
                    # publication witness when both starts share an output path.
                    record.update(home=home, result=result, bounds=owner.cache_bounds[(case, home)],
                                  after=observed["after"])
                record.update(results=results, observations=observations)
                entry = owner.doCleanups, record, _fixture_identity(owner.bank)
                cache[key] = entry
            except BaseException:
                owner.doCleanups()
                raise
        try:
            yield copy.deepcopy(entry[1]) if full_scale_enabled() else clone_fixture(entry[1])
        finally:
            # Rewriting even the same bytes changes ctime. Discard a touched
            # witness instead of pretending its prior metadata was restored.
            if _fixture_identity(entry[1]["bank"]) != entry[2]:
                cache.pop(key, None)
                entry[0]()

    @contextmanager
    def prepared_control(self, case):
        if full_scale_enabled():
            helper = self.helper()
            context = helper.prepare(case, self.home, self.workspace, self.bank)
            yield helper, context, self.execute(helper, case, self.home, context)
            return
        with self.completed_control(case) as record:
            previous = self.bank, self.home, self.workspace, self.root, self.cache_bounds
            self.bank, self.home, self.workspace = record["bank"], record["home"], record["workspace"]
            if "publication_name" in record["context"]:
                # The native command publishes beside the actual completed
                # clone, rather than beside its unexecuted prepared seed.
                self.bank = self.home.parent
            self.root = FloatiRoot.open_direct_home(self.home, create=False)
            self.cache_bounds = {(case, self.home): record["bounds"]}
            try:
                yield self.helper(), record["context"], record["result"]
            finally:
                self.bank, self.home, self.workspace, self.root, self.cache_bounds = previous

    @stable_fixture_sources()
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bus-scale-routine-writes-", dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.bank = Path(temporary.name).resolve()
        self.home = self.bank / "scale-fleet"
        self.home.mkdir(mode=0o700)
        self.workspace = self.bank / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        for node in ("scale-worker", "architect"):
            Registry(self.root).register(node, "codex")
        self.message = EventLog(self.root).send("scale-worker", "architect", "floati", "a" * 40,
            "note.md", "same common-prefix native fixture", idempotency_key="common-message")["message"]
        self.cache_bounds = {}
        self.cold_inbox_homes = set()

    def helper(self):
        self.assertTrue(HELPER.is_file(), "assigned native write helper is absent")
        return load(HELPER, "routine_write_control_helper")

    def capture_inputs(self, case, home):
        bounds = {}
        root = FloatiRoot.open_direct_home(home)
        ledgers = {p.relative_to(home).as_posix(): p.read_bytes() for p in home.rglob("*.jsonl")}
        if case in {"consume", "work-complete", "inbox", "node-drain"}:
            from floati.inbox_prefix import warm as warm_inbox
            from floati.consumed_prefix import warm as warm_consumed
            if home not in self.cold_inbox_homes:
                self.assertIsNotNone(warm_inbox(root))
            if case in {"consume", "work-complete"}:
                self.assertIsNotNone(warm_consumed(root, "architect"))
        if case in {"ack", "inbox", "node-drain", "consume", "work-complete"}:
            from floati.cursor_prefix import warm
            from tests.fixtures.bus_scale_census.bus_scale_cursor_prefix_effects import (
                capture_cursor_prefix_input, capture_delivery_prefix_input)
            self.assertIsNotNone(warm(root, "architect"))
            before = inventory(home)
            bounds = {"before_raw_files": before,
                      "cursor_prefix_input": capture_cursor_prefix_input(home, before, node="architect")}
            if case in {"inbox", "node-drain"}:
                bounds["delivery_prefix_input"] = capture_delivery_prefix_input(home, before, node="architect")
        else:
            before = inventory(home)
        self.assertEqual(ledgers, {p.relative_to(home).as_posix(): p.read_bytes()
            for p in home.rglob("*.jsonl")}, "warming changed native source ledgers")
        if case in {"consume", "work-complete", "inbox", "node-drain"}:
            from tests.fixtures.bus_scale_census.bus_scale_inbox_prefix_effects import capture_inbox_prefix_input
            bounds["inbox_prefix_input"] = capture_inbox_prefix_input(home, before)
        if case in {"consume", "work-complete"}:
            from tests.fixtures.bus_scale_census.bus_scale_consumed_prefix_effects import capture_consumed_prefix_input
            bounds["consumed_prefix_input"] = capture_consumed_prefix_input(home, before, node="architect")
        from tests.fixtures.bus_scale_census.bus_scale_registry_prefix_effects import capture_registry_prefix_input
        bounds.update(before_raw_files=before,
                      registry_prefix_input=capture_registry_prefix_input(home,before))
        self.cache_bounds[(case, home)] = bounds
        return before, bounds

    def witness_inputs(self, bounds):
        return {key: value for key, value in bounds.items() if key != "before_raw_files"}

    @stable_fixture_sources()
    def execute(self, helper, case, home, context, *, argv=None, observation=None):
        before, bounds = self.capture_inputs(case, home)
        output, previous = io.StringIO(), Path.cwd()
        begin = datetime.now(timezone.utc)
        try:
            os.chdir(self.workspace)
            with redirect_stdout(output), bounded_cli_parser():
                native_argv = helper.arguments(case, home, self.workspace, context) if argv is None else argv
                code = cli.main(native_argv)
        finally:
            os.chdir(previous)
        end = datetime.now(timezone.utc)
        artifact = json.loads(output.getvalue())
        self.assertEqual((0, "ok"), (code, artifact["status"]), artifact)
        witness = helper.witness(case, home, before, artifact, context, {}, **self.witness_inputs(bounds))
        after = inventory(home)
        changes = delta(before, after)
        normalized = helper.normalize(case, changes, artifact, context,
            wall_begin=begin, wall_end=end, child_pid=os.getpid(), home=home, workspace=self.workspace, **bounds)
        if observation is not None:
            observation.update(home=home, before=before, after=after, begin=begin, end=end,
                code=code, artifact=artifact, delta=changes, normal=normalized, bounds=bounds,
                witness=witness, argv=native_argv)
        return normalized, before, changes, artifact, begin, end

    def test_current_entry_routes_every_assigned_case(self):
        entry = load(ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py", "routine_write_entry")
        if not full_scale_enabled():
            from tests.bus_scale_execution_paths import representative_cases
            for case in sorted(representative_cases(CASES)):
                with self.subTest(case=case), self.completed_control(case) as record:
                    try:
                        argv = entry.arguments(case, record["home"], record["workspace"], record["context"])
                    except ValueError as exc:
                        self.fail("native entry does not route " + case + ": " + str(exc))
                    self.assertIsInstance(argv, list)
            return
        helper = self.helper()
        for case in sorted(CASES):
            with self.subTest(case=case):
                place = self.bank / ("route-" + case)
                place.mkdir(mode=0o700)
                home = place / "scale-fleet"
                shutil.copytree(self.home, home)
                context = helper.prepare(case, home, self.workspace, place)
                try:
                    argv = entry.arguments(case, home, self.workspace, context)
                except ValueError as exc:
                    self.fail("native entry does not route " + case + ": " + str(exc))
                self.assertIsInstance(argv, list)

    def test_native_preconditions_preserve_scaled_events_and_complete_effects(self):
        helper = self.helper()
        self.assertEqual(CASES, helper.CASES)
        if not full_scale_enabled():
            for case in sorted(_DEFAULT_CASES):
                with self.subTest(case=case), self.completed_control(case) as record:
                    home, context = record["home"], record["context"]
                    normalized, before, raw_delta, artifact, begin, end = record["result"]
                    bounds = record["bounds"]
                    json.dumps(context, allow_nan=False)
                    self.assertEqual(record["raw_events"], (home / "events.jsonl").read_bytes())
                    self.assertEqual("ok", artifact["status"])
                    native = helper.witness(case, home, before, artifact, context, {}, **self.witness_inputs(bounds))
                    self.assertEqual((0, "ok", 1),
                        (native["exit_code"], native["status"], native["effect"]["count"]))
                    self.assertEqual({"artifact", "delta", "normalizations"}, set(normalized))
                    self.assertEqual(record["after"], inventory(home), "validation mutated native evidence")
                    bad = copy.deepcopy(artifact)
                    bad["status"] = "refused"
                    with self.assertRaises(ValueError):
                        helper.witness(case, home, before, bad, context, {}, **self.witness_inputs(bounds))
            return
        for case in sorted(CASES):
            with self.subTest(case=case):
                place = self.bank / case
                place.mkdir(mode=0o700)
                home = place / "scale-fleet"
                shutil.copytree(self.home, home)
                raw_events = (home / "events.jsonl").read_bytes()
                context = helper.prepare(case, home, self.workspace, place)
                json.dumps(context, allow_nan=False)
                self.assertEqual(raw_events, (home / "events.jsonl").read_bytes())
                before, bounds = self.capture_inputs(case, home)
                argv = helper.arguments(case, home, self.workspace, context)
                output, previous = io.StringIO(), Path.cwd()
                begin = datetime.now(timezone.utc)
                try:
                    os.chdir(self.workspace)
                    with redirect_stdout(output):
                        code = cli.main(argv)
                finally:
                    os.chdir(previous)
                end = datetime.now(timezone.utc)
                artifact = json.loads(output.getvalue())
                self.assertEqual(0, code, artifact)
                self.assertEqual("ok", artifact["status"])
                after = inventory(home)
                native = helper.witness(case, home, before, artifact, context, {}, **self.witness_inputs(bounds))
                self.assertEqual((0, "ok", 1),
                    (native["exit_code"], native["status"], native["effect"]["count"]))
                raw_delta = delta(before, after)
                normalized = helper.normalize(case, raw_delta, artifact, context,
                    wall_begin=begin, wall_end=end, child_pid=os.getpid(),
                    home=home, workspace=self.workspace, **bounds)
                self.assertEqual({"artifact", "delta", "normalizations"}, set(normalized))
                self.assertEqual(after, inventory(home), "validation mutated native evidence")
                bad = copy.deepcopy(artifact)
                bad["status"] = "refused"
                with self.assertRaises(ValueError):
                    helper.witness(case, home, before, bad, context, {}, **self.witness_inputs(bounds))

    def test_preview_uses_pinned_genuine_response_without_dispatch(self):
        helper = self.helper()
        context = helper.prepare("intake-preview", self.home, self.workspace, self.bank)
        path, _ = helper.github_fixture()
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), context["github_response_sha256"])
        repo = json.loads(raw)["url"].removeprefix("https://github.com/").removesuffix("/issues/3")
        self.assertEqual(f"github:{repo}#3", context["snapshot"]["record"]["source_id"])
        self.assertEqual(3, context["snapshot"]["payload"]["metadata"]["number"])

    def test_bundle_roster_preserves_native_cells_and_audits_only_bounded_ages(self):
        with self.prepared_control("confluence-bundle") as (helper, context, result):
            normalized, _, _, _, _, _ = result
            raw = json.loads((self.bank / "routine-bundle.json").read_bytes())
            actual = normalized["delta"]["@publication/routine-bundle.json"]["value"]
            self.assertEqual(["architect", "scale-worker"], [row["node_id"] for row in raw["roster"]])
            expected = copy.deepcopy(raw)
            for row in expected["roster"]:
                self.assertEqual("MEASURED", row["harness"]["stamp"])
                row["harness"]["age_seconds"] = "<native-roster-age>"
            expected["fleet_state"]["observed_at"] = "<native-fleet-observation-time>"
            self.assertEqual("none", raw["fleet_state"]["load_hold"]["state"])
            expected["snapshot_at"] = "<native-hold-snapshot-time>"
            hold = expected["fleet_state"]["load_hold"]
            hold["observed_at"] = "<native-hold-observation-time>"
            hold["valid_until"] = "<native-hold-freshness-deadline>"
            self.assertEqual(expected, actual)
            self.assertEqual(expected["snapshot_at"], normalized["artifact"]["evidence"]["snapshot_at"])
            audits = [row["pointer"] for row in normalized["normalizations"] if "/roster/" in row["pointer"]]
            self.assertEqual(2, len(audits))
            self.assertTrue(all(pointer.endswith("/harness/age_seconds") for pointer in audits))

    def test_bundle_roster_rejects_missing_cells_forged_values_and_out_of_invocation_ages(self):
        with self.prepared_control("confluence-bundle") as (helper, context, result):
            _, _, changes, artifact, begin, end = result
            bounds = self.cache_bounds[("confluence-bundle", self.home)]
            publication = self.bank / "routine-bundle.json"
            raw = publication.read_bytes()
            original = json.loads(raw)
            for poison in ("missing", "owner", "age", "unknown_age", "extra",
                           "fleet_clock", "archive_policy", "waiter_mode", "hold_state",
                           "hold_expiry", "snapshot", "stdout_snapshot"):
                with self.subTest(poison=poison):
                    document = copy.deepcopy(original)
                    stdout_artifact = artifact
                    if poison == "missing":
                        document.pop("roster")
                    elif poison == "owner":
                        document["roster"][0]["owner"]["value"] = "invented owner"
                    elif poison == "age":
                        document["roster"][0]["harness"]["age_seconds"] += 3600
                    elif poison == "unknown_age":
                        document["roster"][0]["owner"]["age_seconds"] = 0
                    elif poison == "extra":
                        document["roster"][0]["unreviewed"] = copy.deepcopy(document["roster"][0]["harness"])
                    elif poison == "fleet_clock":
                        document["fleet_state"]["observed_at"] = "2000-01-01T00:00:00.000Z"
                    elif poison == "archive_policy":
                        policy = document["fleet_state"]["archive_retention"]
                        policy["keep_forever"] = not policy["keep_forever"]
                    elif poison == "waiter_mode":
                        document["fleet_state"]["waiters"][0]["mode"]["reason"] = "invented"
                    elif poison == "hold_state":
                        document["fleet_state"]["load_hold"]["state"] = "active"
                    elif poison == "hold_expiry":
                        document["fleet_state"]["load_hold"]["valid_until"] = document["snapshot_at"]
                    elif poison == "snapshot":
                        document["snapshot_at"] = "2000-01-01T00:00:00.000Z"
                    else:
                        stdout_artifact = copy.deepcopy(artifact)
                        stdout_artifact["evidence"]["snapshot_at"] = "2000-01-01T00:00:00.000Z"
                    publication.write_text(json.dumps(document) + "\n")
                    try:
                        with self.assertRaises(ValueError):
                            helper.normalize("confluence-bundle", changes, stdout_artifact, context,
                                wall_begin=begin, wall_end=end, child_pid=os.getpid(),
                                home=self.home, workspace=self.workspace, **bounds)
                    finally:
                        publication.write_bytes(raw)

    def test_independent_clones_keep_complete_durable_semantics_equal(self):
        if not full_scale_enabled():
            from tests.bus_scale_execution_paths import representative_cases
            for case in sorted(representative_cases(CASES)):
                with self.subTest(case=case), self.completed_control(case, clones=True) as record:
                    results = record["results"]
                    self.assertEqual(results[0]["artifact"], results[1]["artifact"])
                    self.assertEqual(results[0]["delta"], results[1]["delta"])
            return
        helper = self.helper()
        for case in sorted(CASES):
            with self.subTest(case=case):
                place = self.bank / ("clones-" + case)
                place.mkdir(mode=0o700)
                seed = place / "scale-fleet"
                shutil.copytree(self.home, seed)
                context = helper.prepare(case, seed, self.workspace, place)
                results = []
                for mode in ("cpu", "diagnostic"):
                    parent = place / mode
                    parent.mkdir(mode=0o700)
                    home = parent / "scale-fleet"
                    shutil.copytree(seed, home)
                    results.append(self.execute(helper, case, home, context)[0])
                self.assertEqual(results[0]["artifact"], results[1]["artifact"])
                self.assertEqual(results[0]["delta"], results[1]["delta"])

    def test_partial_or_forged_consumption_evidence_cannot_qualify(self):
        with self.prepared_control("consume") as (helper, context, result):
            _, before, changes, artifact, begin, end = result
            malformed = copy.deepcopy(changes)
            malformed.pop("receipts/consumed/architect.jsonl")
            bounds = self.cache_bounds[("consume", self.home)]
            with self.assertRaises(ValueError):
                helper.normalize("consume", malformed, artifact, context, wall_begin=begin,
                    wall_end=end, child_pid=os.getpid(), home=self.home, workspace=self.workspace, **bounds)
            forged = copy.deepcopy(artifact)
            forged["evidence"]["ack_receipt_id"] = "ack-00000000000070008000000000000000"
            with self.assertRaises(ValueError):
                helper.witness("consume", self.home, before, forged, context, {},
                    **self.witness_inputs(bounds))
            (self.home / "unreviewed.json").write_text("{}")
            with self.assertRaises(ValueError):
                helper.witness("consume", self.home, before, artifact, context, {},
                    **self.witness_inputs(bounds))

    def test_full_drain_refuses_extra_selected_work_before_preparation(self):
        helper = self.helper()
        EventLog(self.root).send("scale-worker", "architect", "floati", "a" * 40,
            "note.md", "additional selected work", idempotency_key="second-message")
        before = inventory(self.home)
        for case in ("inbox", "node-drain"):
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, "UNRESOLVED.*exactly one"):
                helper.prepare(case, self.home, self.workspace, self.bank)
        self.assertEqual(before, inventory(self.home))

    def test_native_completion_appends_consumption_before_completion(self):
        helper = self.helper()
        context = helper.prepare("work-complete", self.home, self.workspace, self.bank)
        from floati import jsonl
        from floati.framing import decode_frames
        native_append = jsonl._append_frame
        observed = []

        def observe(path, raw):
            identity = native_append(path, raw)
            observed.extend(row["kind"] for row in decode_frames(raw))
            return identity

        # A passive delegated observer, not a fake append or success result.
        with patch.object(jsonl, "_append_frame", observe):
            self.execute(helper, "work-complete", self.home, context)
        self.assertEqual(["consumption_receipt", "work_transition"], observed)

    def test_native_normalizer_rejects_changed_raw_binding_and_clock_bounds(self):
        with self.prepared_control("hold-start") as (helper, context, result):
            _, _, changes, artifact, begin, end = result
            forged = copy.deepcopy(changes)
            forged["receipts/holds/architect.jsonl"]["after_sha256"] = "0" * 64
            with self.assertRaises(ValueError):
                helper.normalize("hold-start", forged, artifact, context, wall_begin=begin,
                    wall_end=end, child_pid=os.getpid(), home=self.home, workspace=self.workspace)
            with self.assertRaises(ValueError):
                helper.normalize("hold-start", changes, artifact, context,
                    wall_begin=begin.replace(year=begin.year + 1), wall_end=end.replace(year=end.year + 1),
                    child_pid=os.getpid(), home=self.home, workspace=self.workspace)

    def test_resealed_cold_inbox_member_must_match_native_pre_drain_projection(self):
        """A valid seal cannot authenticate invented event membership facts."""
        helper = self.helper()
        from floati import send_prefix
        from floati.inbox_prefix import InboxPrefix, recipient_key
        for case in ("inbox", "node-drain"):
            place = self.bank / ("cold-cache-" + case)
            place.mkdir(mode=0o700)
            home = place / "scale-fleet"
            shutil.copytree(self.home, home)
            root = FloatiRoot.open_direct_home(home, create=False)
            context = helper.prepare(case, home, self.workspace, place)
            self.cold_inbox_homes.add(home)
            _, before, _, artifact, begin, end = self.execute(helper, case, home, context)
            self.assertFalse(any(path.startswith(".inbox-prefix/") for path in before),
                "this control requires a genuinely cold exact native bank")
            payload = helper._native_inbox_payload(root, before)
            self.assertEqual({"recipient": "architect", "limit": 1000,
                "messages": [context["message"]], "truncated": False,
                "delivered_ids": [], "acked_ids": []}, payload)
            prefix = InboxPrefix(root)
            prefix.load()
            lookup = send_prefix._key("recipient", recipient_key("architect", 0))
            member = prefix.nodes / prefix.filename(lookup)
            original_raw = member.read_bytes()
            original = json.loads(original_raw)
            self.assertEqual(context["message"], original["payload"]["value"])
            mutations = {
                "recipient": "scale-worker",
                "note": "invented selected payload",
                "id": "msg-00000000000070008000000000000000",
                "worker_session_id": "foreign-worker",
            }
            for field, value in mutations.items():
                with self.subTest(case=case, field=field):
                    corrupt = dict(original["payload"]["value"], **{field: value})
                    member.write_bytes(send_prefix._bytes(prefix._entry(lookup, corrupt)))
                    # Preserve genuine source anchors and a valid member seal;
                    # complete raw commitments alone do not prove its value.
                    current = inventory(home)
                    bound_delta = delta(before, current)
                    try:
                        with self.assertRaises(ValueError):
                            helper.normalize(case, bound_delta, artifact, context,
                                wall_begin=begin, wall_end=end, child_pid=os.getpid(),
                                home=home, workspace=self.workspace, **self.cache_bounds[(case, home)])
                    finally:
                        self.assertEqual(current, inventory(home),
                                         "cache proof must remain physically read-only")
            member.write_bytes(original_raw)
            bounds = self.cache_bounds[(case, home)]
            for field in ("messages", "acknowledgment"):
                if case != "inbox":
                    continue
                with self.subTest(case=case, forged_stdout=field):
                    forged = copy.deepcopy(artifact)
                    forged["evidence"][field] = [] if field == "messages" else None
                    with self.assertRaises(ValueError):
                        helper.witness(case, home, before, forged, context, {},
                            **self.witness_inputs(bounds))


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(tests.HOME_PIN_ROOT)
