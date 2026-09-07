"""V7-PC — `node prep-clear`, the WIND-DOWN verb (LC-1c).

The two REDs the ruling names are `test_node_prep_clear_is_a_registered_verb`
and `test_a_dirty_tree_without_complement_is_refused`. The rest pin the
composition: the checkpoint envelope carries the full 40-hex PUSHED tip, the
wake claim is released through the primitives that already exist, and the
`prep_clear_receipt` names all three.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.codex_wait_contract import (
    WORKSPACE_MAP_RELATIVE,
    CodexWaitConsentLedger,
    CodexWaitSessionLedger,
    resolve_participant,
)
from floati.errors import ProtocolRefusal
from floati.jsonl import read_records
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = REPOSITORY_ROOT / "schemas/v1/prep-clear-receipt.schema.json"
SEAT = public_ids.builder("floati")
ARCHITECT = "architect-a"
SESSION = "session-018f7e9b3c137abc8def0123456789ab"


def _git(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """One fixed Git, one explicit tree, no ambient Git coordinates."""

    completed = subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "prep-clear-test",
            "GIT_AUTHOR_EMAIL": "prep-clear-test@example.invalid",
            "GIT_COMMITTER_NAME": "prep-clear-test",
            "GIT_COMMITTER_EMAIL": "prep-clear-test@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)}: {completed.stderr}")
    return completed


class PrepClearFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(dir=REAL_TEMP_ROOT))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        registry = Registry(self.root)
        registry.register(SEAT, "builder")
        registry.register(ARCHITECT, "architect")

        self.workspace = self.base / "workspace"
        self.remote = self.base / "remote.git"
        self.workspace.mkdir()
        _git(["init", "-q", "-b", "main", str(self.workspace)], self.base)
        _git(["init", "-q", "--bare", str(self.remote)], self.base)
        _git(["remote", "add", "origin", str(self.remote)], self.workspace)
        (self.workspace / "SEAT.md").write_text("the seat\n", encoding="utf-8")
        _git(["add", "SEAT.md"], self.workspace)
        _git(["commit", "-q", "-m", "seed the seat workspace"], self.workspace)
        _git(["push", "-q", "-u", "origin", "main"], self.workspace)
        self.pushed_tip = _git(["rev-parse", "HEAD"], self.workspace).stdout.strip()

        map_path = self.root.tenant_home / WORKSPACE_MAP_RELATIVE
        map_path.parent.mkdir(parents=True, exist_ok=True)
        map_path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "tenant_id": self.root.tenant_id,
                    "mappings": [
                        {"workspace": str(self.workspace), "node_id": SEAT}
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        participant = resolve_participant(self.root.tenant_home, self.workspace)
        assert participant is not None
        self.binding = participant.binding
        self.consent = CodexWaitConsentLedger(self.root).arm(
            self.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="prep-clear-consent",
        )
        self.claim = CodexWaitSessionLedger(self.root).arm(
            self.binding,
            self.consent,
            SESSION,
            idempotency_key="prep-clear-claim",
        )

    def clear(self, **overrides: object) -> dict:
        from floati.prep_clear import PrepClear

        arguments: dict = {
            "node": SEAT,
            "workspace": self.workspace,
            "session": SESSION,
            "repo": "floati",
            "doc": "docs/status/QUEUE-2026-09-01.md",
            "note": "seat winding down; V7-PC banked",
            "idempotency_key": "prep-clear-one",
        }
        arguments.update(overrides)
        return PrepClear(self.root).clear(**arguments)

    def dirty(self) -> None:
        (self.workspace / "SEAT.md").write_text("edited, uncommitted\n", encoding="utf-8")

    def unpushed(self) -> str:
        (self.workspace / "NOTES.md").write_text("banked nowhere\n", encoding="utf-8")
        _git(["add", "NOTES.md"], self.workspace)
        _git(["commit", "-q", "-m", "work this stop does not cover"], self.workspace)
        return _git(["rev-parse", "HEAD"], self.workspace).stdout.strip()

    def receipts(self) -> list[dict]:
        return read_records(
            self.root,
            Path("receipts/prep-clear") / f"{SEAT}.jsonl",
            allowed_kinds={"prep_clear_receipt"},
        )


class PrepClearVerbTests(PrepClearFixture):
    def test_node_prep_clear_is_a_registered_verb(self) -> None:
        """RED 1: `node prep-clear` is unrecognized."""

        from floati.cli import _parser

        parsed = _parser().parse_args(
            [
                "node", "prep-clear",
                "--root", str(self.root.tenant_home),
                "--as", SEAT,
                "--session", SESSION,
                "--workspace", str(self.workspace),
                "--repo", "floati",
                "--doc", "docs/status/QUEUE-2026-09-01.md",
                "--note", "seat winding down",
                "--idempotency-key", "prep-clear-one",
            ]
        )
        self.assertEqual("prep-clear", parsed.node_command)
        self.assertEqual(SEAT, parsed.actor)
        self.assertIsNone(parsed.complement)

    def test_a_dirty_tree_without_complement_is_refused(self) -> None:
        """RED 2: a stop order that does not name what it fails to cover."""

        self.dirty()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.clear()
        self.assertEqual("prep_clear_stop_incomplete", caught.exception.code)
        self.assertIn("--complement", str(caught.exception.remedy))
        self.assertEqual([], self.receipts())

    def test_unpushed_commits_without_complement_are_refused(self) -> None:
        unpushed_tip = self.unpushed()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.clear()
        self.assertEqual("prep_clear_stop_incomplete", caught.exception.code)
        self.assertIn("1", caught.exception.detail)
        self.assertNotEqual(self.pushed_tip, unpushed_tip)
        self.assertEqual([], self.receipts())

    def test_complement_is_recorded_verbatim(self) -> None:
        self.dirty()
        self.unpushed()
        complement = "does NOT cover the R16 perf repair or any Necromancy row"
        receipt = self.clear(complement=complement)
        self.assertEqual(complement, receipt["complement"])
        self.assertTrue(receipt["workspace_dirty"])
        self.assertEqual(1, receipt["unpushed_commit_count"])
        self.assertEqual([receipt], self.receipts())

    def test_a_clean_pushed_tree_needs_no_complement(self) -> None:
        receipt = self.clear()
        self.assertIsNone(receipt["complement"])
        self.assertFalse(receipt["workspace_dirty"])
        self.assertEqual(0, receipt["unpushed_commit_count"])
        self.assertEqual("prep_clear_receipt", receipt["kind"])
        self.assertEqual(1, receipt["schema_version"])


class PrepClearCompositionTests(PrepClearFixture):
    def test_the_envelope_carries_the_full_forty_hex_pushed_tip(self) -> None:
        from floati.events import EventLog

        receipt = self.clear()
        self.assertEqual(self.pushed_tip, receipt["pushed_tip"])
        self.assertEqual(40, len(receipt["pushed_tip"]))
        envelopes = [
            record
            for record in EventLog(self.root).event_records()
            if record.get("kind") == "message_envelope"
        ]
        self.assertEqual(1, len(envelopes))
        self.assertEqual(receipt["envelope_id"], envelopes[0]["id"])
        self.assertEqual(self.pushed_tip, envelopes[0]["sha"])
        self.assertEqual(ARCHITECT, envelopes[0]["recipient"])
        self.assertEqual(ARCHITECT, receipt["envelope_recipient"])
        self.assertEqual(SEAT, envelopes[0]["sender"])

    def test_an_unpushed_tip_never_reaches_the_envelope(self) -> None:
        """The tip the envelope names is the banked one, not HEAD."""

        unpushed_tip = self.unpushed()
        receipt = self.clear(complement="the NOTES.md row is not covered")
        self.assertEqual(self.pushed_tip, receipt["pushed_tip"])
        self.assertNotEqual(unpushed_tip, receipt["pushed_tip"])

    def test_the_release_names_the_claim_this_session_held(self) -> None:
        from floati.wake_control import WakeController

        receipt = self.clear()
        self.assertEqual(self.claim["id"], receipt["released_claim_receipt_id"])
        self.assertEqual("released", receipt["wake_release_outcome"])
        self.assertIsNotNone(receipt["wake_release_receipt_id"])
        self.assertEqual(
            "paused",
            WakeController(self.root).status(SEAT, SESSION)["state"],
        )

    def test_a_session_that_does_not_hold_the_claim_is_refused(self) -> None:
        other = "session-018f7e9b3c147abc8def0123456789ab"
        with self.assertRaises(ProtocolRefusal) as caught:
            self.clear(session=other)
        self.assertEqual("prep_clear_claim_not_held", caught.exception.code)
        self.assertIn(SESSION, caught.exception.detail)
        self.assertEqual([], self.receipts())

    def test_an_already_released_claim_reports_its_own_outcome(self) -> None:
        from floati.wake_control import WakeController

        WakeController(self.root).pause(SEAT, SESSION, idempotency_key="hand-pause")
        receipt = self.clear()
        self.assertEqual("already_released", receipt["wake_release_outcome"])
        self.assertIsNone(receipt["wake_release_receipt_id"])

    def test_repeating_the_key_returns_the_same_receipt_and_sends_once(self) -> None:
        from floati.events import EventLog

        first = self.clear()
        second = self.clear()
        self.assertEqual(first, second)
        self.assertEqual([first], self.receipts())
        envelopes = [
            record
            for record in EventLog(self.root).event_records()
            if record.get("kind") == "message_envelope"
        ]
        self.assertEqual(1, len(envelopes))


class PrepClearRecordTests(PrepClearFixture):
    def test_the_receipt_validates_against_its_closed_v1_schema(self) -> None:
        receipt = self.clear(complement=None)
        validate_json_schema(receipt, SCHEMA)
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(receipt), set(schema["required"]))

    def test_a_complemented_receipt_validates_against_the_same_schema(self) -> None:
        self.dirty()
        receipt = self.clear(complement="the perf repair is not covered")
        validate_json_schema(receipt, SCHEMA)

    def test_the_runtime_spec_and_the_schema_agree_on_the_field_set(self) -> None:
        from floati.records import _SPECS

        prefix, fields = _SPECS["prep_clear_receipt"]
        self.assertEqual("prep-clear-", prefix)
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(fields, frozenset(schema["required"]))


class PrepClearLooseningTests(unittest.TestCase):
    """A new kind is a LOOSENING: every prior record must still read.

    The corpus behind that law is the record factory's: one committed
    fixture or one schema-derived record per prior kind, with the
    underivable kinds pinned as typed absences. See
    `test_every_prior_kind_still_validates` for the walk.
    """

    def test_every_prior_kind_still_validates(self) -> None:
        """REC-FACTORY-1: one walk over EVERY prior kind, not a hand-picked five.

        This test replaces `test_five_hand_picked_prior_kinds_still_validate`,
        whose docstring claimed 56 of the 121 kinds had no JSON Schema — that
        count was v1-only; the governed schemas span v0 AND v1. The factory
        (tests/record_factory.py) derives one record per prior kind —
        committed corpus first, then the governed schemas, values synthesized
        from the schemas' own const/enum/pattern/bounds contracts — and this
        walk reads each through `validate_record`.

        Two absence classes are pinned in the factory, never faked: kinds
        with no governed schema at all (the pinned NO_SCHEMA_KINDS —
        exactly the V7-PC-F1 read's census), and kinds whose validator
        carries a value contract beyond the schema (the pinned
        VALIDATOR_CONTRACT_KINDS, each with its measured refusal code —
        computed digests, cross-field time ordering, cross-record refs,
        scope and state-machine grammar). The earlier node_lease
        "schema/runtime drift" is RETRACTED: schema and validator agree on
        exactly-one-of expires_at/predecessor_lease_id; the synthesizer
        resolves oneOf, so the kind derives and is refused only by its
        pinned workspace contract. A kind that becomes derivable or stops
        being derivable, and any synthesized record the validator refuses
        with an unpinned code, fails this walk — so a loosening that breaks
        any walking prior kind REDs here by name. No counts are typed here:
        the walk asserts the pinned dicts' partition of the vocabulary.
        """

        from floati.records import validate_record
        from tests import record_factory

        kinds = record_factory.prior_kinds()
        walked: list[str] = []
        no_schema: list[str] = []
        validator_contract: list[tuple[str, str]] = []
        for kind in kinds:
            try:
                record = record_factory.synthesize(kind)
            except record_factory.KindNotDerivable as absence:
                pinned = record_factory.NO_SCHEMA_KINDS.get(kind)
                self.assertIsNotNone(
                    pinned,
                    f"{kind} is not derivable and is NOT pinned as an absence: "
                    f"{absence.reason}",
                )
                no_schema.append(kind)
                continue
            with self.subTest(kind=kind):
                try:
                    validate_record(
                        record,
                        record["tenant_id"],
                        frozenset({kind}),
                        integrity=False,
                    )
                except ProtocolRefusal as refusal:
                    pinned_code = record_factory.VALIDATOR_CONTRACT_KINDS.get(kind)
                    if pinned_code == refusal.code:
                        validator_contract.append(kind)
                        continue
                    self.fail(
                        f"{kind} validates NO MORE (a loosening, or an unpinned "
                        f"validator contract): {refusal.code}"
                    )
            walked.append(kind)

        self.assertEqual(
            sorted(record_factory.NO_SCHEMA_KINDS),
            no_schema,
            "the no-schema absences must be exactly the pinned set",
        )
        self.assertEqual(
            sorted(record_factory.VALIDATOR_CONTRACT_KINDS),
            sorted(validator_contract),
            "the beyond-schema validator contracts must be exactly the pinned set",
        )
        self.assertEqual(len(kinds), len(walked) + len(no_schema) + len(validator_contract))

    def test_the_kind_vocabulary_only_grew(self) -> None:
        """Catches a new kind that was smuggled in by renaming an old one."""

        from floati.records import _SPECS

        fixtures = REPOSITORY_ROOT / "tests/fixtures/v7-pc"
        before = set(json.loads((fixtures / "kinds-before.json").read_text(encoding="utf-8")))
        self.assertTrue(before < set(_SPECS), "the record kind vocabulary must only grow")
        # kinds-before.json is frozen at this car's own base; the rest of the
        # composition added four more kinds after that base, each by its own
        # named car (SB-1 in train AT; HOOKS-PRE and ROLE-1 Am.1 in train AU;
        # ARCH-1 in train AV). LANES-1 adds its explicit workspace kind.
        self.assertEqual(
            {
                "prep_clear_receipt",
                "seat_board_receipt",
                "hook_burn_record",
                "role_template_write_receipt",
                "registry_role_transfer",
                "lane_workspace_record",
            },
            set(_SPECS) - before,
        )


class PrepClearProcessFenceTests(unittest.TestCase):
    def test_the_verb_resolves_git_from_a_fixed_candidate_list(self) -> None:
        """No PATH resolution: `shutil.which` may not appear in this module."""

        source = (REPOSITORY_ROOT / "floati/prep_clear.py").read_text(encoding="utf-8")
        self.assertNotIn("shutil.which", source)
        self.assertNotIn("import shutil", source)
        from floati.prep_clear import _GIT_CANDIDATES

        self.assertEqual(("/usr/bin/git", "/bin/git"), _GIT_CANDIDATES)

    def test_the_verb_reuses_the_rb_1_banked_sha_predicate(self) -> None:
        """RB-1 is the send-side fence; prep-clear may not grow a second one."""

        import floati.prep_clear as module
        from floati.cli import _require_banked_sha

        self.assertIs(_require_banked_sha, module._banked_sha_predicate())


if __name__ == "__main__":  # pragma: no cover - parity with the suite's modules
    unittest.main()
