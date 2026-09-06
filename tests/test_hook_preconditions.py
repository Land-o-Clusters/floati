"""RED-first measurements for the three hooks preconditions.

The 2026-08-30 hooks ruling (`docs/rulings/2026-09-01-the-two-rows-i-was-
holding-for-a-word.md` §1) holds hooks OFF until three things are *measured*:

1. a behavioural burn record for hook execution;
2. confinement at some layer, retested per release;
3. MEASURED proof a hook session cannot write its own trust config.

One test per clause. Nothing here enables a hook, installs one, or grants
trust; every measurement runs against a scratch bus root and a scratch
Codex home. Clause 1 is measurable only for a *harness-executed* invocation:
a `live_codex_stop` burn requires trusting a Stop hook in a live harness,
which is the act the ruling forbids, so that source stays a typed absence
carried in the record's own vocabulary rather than faked here.
"""

from __future__ import annotations

import importlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


SCHEMA = Path("schemas/v1/hook-burn-record.schema.json")
REPO_ROOT = Path(__file__).resolve().parents[1]

try:
    hook_preconditions = importlib.import_module("floati.hook_preconditions")
except ModuleNotFoundError:  # RED before the mechanism exists.
    hook_preconditions = None


def _require_module():
    if hook_preconditions is None:
        raise AssertionError(
            "floati.hook_preconditions is absent: no hooks precondition is measured"
        )
    return hook_preconditions


class HookPreconditionFixture(unittest.TestCase):
    """One armed Codex waiter participant and one scratch Codex home."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.tenant_id = "demo-fleet"
        self.bus_home = self.base / self.tenant_id
        self.root = FloatiRoot.open_direct_home(self.bus_home, create=True)
        Registry(self.root).register(public_ids.builder("floati"), "worker")
        Registry(self.root).register("architect", "architect")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.write_map(
            [
                {
                    "workspace": str(self.workspace),
                    "node_id": public_ids.builder("floati"),
                }
            ]
        )

        from floati.codex_wait_contract import (
            CodexWaitConsentLedger,
            resolve_participant,
        )

        participant = resolve_participant(self.bus_home, self.workspace)
        assert participant is not None
        self.participant = participant
        CodexWaitConsentLedger(self.root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="hook-precondition-consent",
        )

        # A scratch Codex home: the hook file the waiter is named in, and the
        # trust config that decides whether that hook is armed.
        self.codex_home = self.base / "codex-home"
        self.codex_home.mkdir()
        self.hooks_path = self.codex_home / "hooks.json"
        self.hook_block = {
            "hooks": [
                {
                    "type": "command",
                    "command": (
                        "/usr/bin/python3 /opt/floati-wake/"
                        + "0" * 64
                        + "/scripts/floati-codex-wait"
                    ),
                    "timeout": 10,
                }
            ]
        }
        self.hooks_path.write_text(
            json.dumps({"hooks": {"Stop": [self.hook_block]}}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.trust_config = self.codex_home / "config.toml"
        self.trust_config.write_text(
            '[hooks.state."'
            + str(self.hooks_path)
            + ':stop:0:0"]\nenabled = true\ntrusted_hash = "'
            + "b" * 64
            + '"\n',
            encoding="utf-8",
        )

    def write_map(self, mappings: list[dict[str, str]]) -> None:
        path = self.bus_home / "codex-wait" / "workspaces.v0.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "tenant_id": self.tenant_id,
                    "mappings": mappings,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    def tree_bytes(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def burn_one_invocation(self) -> dict[str, object]:
        """Execute the hook entry point once and return the observed effects."""

        from floati.codex_wait import run_stop_waiter

        EventLog(self.root).send(
            "architect",
            public_ids.builder("floati"),
            "floati",
            "a" * 40,
            "docs/evidence/ping.md",
            "live ping",
            idempotency_key="hook-precondition-ping",
        )
        before = self.tree_bytes(self.bus_home)
        stdout = io.StringIO()
        stderr = io.StringIO()
        status = run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload={
                "cwd": str(self.workspace),
                "session_id": "hook-precondition-session",
            },
            stdout=stdout,
            stderr=stderr,
        )
        after = self.tree_bytes(self.bus_home)
        return _require_module().observe_hook_burn(
            before=before,
            after=after,
            exit_status=status,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
        )

    def burn_record(self, observation: dict[str, object], **overrides: object) -> dict:
        module = _require_module()
        record = module.hook_burn_record(
            tenant_id=self.tenant_id,
            record_id="hook-burn-record-" + uuid7_hex(),
            timestamp="2026-09-05T04:00:00.000Z",
            release_id=module.package_release_id(REPO_ROOT),
            hook_command_sha256=module.hook_command_identity(self.hook_block),
            hook_group_index=0,
            hooks_path_shape=module.path_shape(
                self.hooks_path, host_root=self.codex_home
            ),
            invocation_source="harness_executed",
            observation=observation,
        )
        record.update(overrides)
        return record


class HookBurnRecordTests(HookPreconditionFixture):
    def test_a_burn_record_is_witnessed_behaviour_bound_to_the_hook_that_ran(
        self,
    ) -> None:
        """CLAUSE 1. A burn record states what one hook invocation was OBSERVED
        to do — derived from the before/after bus bytes, the exit status and the
        captured streams — and the kind refuses a record with no capture to
        witness it, a host-prefixed path shape, or an unknown effect class."""

        module = _require_module()
        observation = self.burn_one_invocation()

        # The effects are DERIVED from the run, not declared: this invocation
        # really blocked the Stop and really appended to the wake ledger.
        self.assertIn("stop_decision_blocked", observation["observed_effects"])
        self.assertIn("bus_path_created", observation["observed_effects"])
        self.assertEqual(0, observation["observed_exit_code"])
        self.assertRegex(str(observation["capture_sha256"]), r"^[0-9a-f]{64}$")

        record = self.burn_record(observation)
        validate_json_schema(record, SCHEMA)
        self.assertEqual(
            record, module.validate_hook_burn(record, self.tenant_id)
        )
        self.assertEqual("harness_executed", record["invocation_source"])

        # The path shape keeps the shape and loses the host.
        self.assertNotIn(str(self.codex_home), record["hooks_path_shape"])
        self.assertTrue(record["hooks_path_shape"].startswith("<codex-home>/"))

        # The hook identity is Codex's own normalized trust hash for the exact
        # Stop group, so a record cannot be re-used for a different hook.
        from floati.codex_hook_trust import codex_hook_current_hash

        self.assertEqual(
            codex_hook_current_hash(self.hook_block), record["hook_command_sha256"]
        )

        unwitnessed = self.burn_record(
            observation, capture_sha256=None, unknown_fields=["capture_sha256"]
        )
        with self.assertRaises(ProtocolRefusal) as unwitnessed_refusal:
            module.validate_hook_burn(unwitnessed, self.tenant_id)
        self.assertEqual(
            "hook_burn_unwitnessed", unwitnessed_refusal.exception.code
        )

        hostful = self.burn_record(
            observation, hooks_path_shape=str(self.hooks_path)
        )
        with self.assertRaises(ProtocolRefusal) as hostful_refusal:
            module.validate_hook_burn(hostful, self.tenant_id)
        self.assertEqual(
            "hook_burn_path_shape_hostful", hostful_refusal.exception.code
        )

        invented = self.burn_record(
            observation, observed_effects=["hook_was_probably_fine"]
        )
        with self.assertRaises(ProtocolRefusal) as invented_refusal:
            module.validate_hook_burn(invented, self.tenant_id)
        self.assertEqual("hook_burn_effects_invalid", invented_refusal.exception.code)

        # The old-record fixture: a burn measured against an earlier release is
        # a valid record and a stale precondition.
        old = self.burn_record(observation, release_id="0.0.1")
        self.assertEqual(old, module.validate_hook_burn(old, self.tenant_id))
        with self.assertRaises(ProtocolRefusal) as stale:
            module.require_burn_current(old, repo_root=REPO_ROOT)
        self.assertEqual("hook_burn_release_stale", stale.exception.code)
        self.assertEqual(
            module.package_release_id(REPO_ROOT),
            module.require_burn_current(record, repo_root=REPO_ROOT),
        )


class HookConfinementTests(HookPreconditionFixture):
    def test_confinement_is_retested_against_the_release_the_package_spells(
        self,
    ) -> None:
        """CLAUSE 2. The confinement battery runs now, and its result carries the
        release identifier READ FROM THE PACKAGE, so an attestation measured
        against any other release is refused as stale instead of reused."""

        module = _require_module()

        # Derived, never a literal: a package spelling 9.9.9 must produce 9.9.9.
        other_package = self.base / "other-package"
        (other_package / "floati").mkdir(parents=True)
        (other_package / "floati" / "__init__.py").write_text(
            '"""Floati protocol core."""\n\n__version__ = "9.9.9"\n',
            encoding="utf-8",
        )
        self.assertEqual("9.9.9", module.package_release_id(other_package))

        elsewhere = module.confinement_retest(
            root=self.root, repo_root=other_package
        )
        self.assertEqual("9.9.9", elsewhere.release_id)

        current = module.confinement_retest(root=self.root, repo_root=REPO_ROOT)
        self.assertEqual(module.package_release_id(REPO_ROOT), current.release_id)

        # The battery really ran: every escape name was refused by containment,
        # and nothing landed outside the tenant home. The count is pinned and
        # the shapes are named, so an enumeration cannot quietly shrink to one
        # easy case and keep passing.
        self.assertEqual(5, len(module.CONFINEMENT_ESCAPE_NAMES))
        for shape in ("absolute", "parent_traversal", "symlink_component"):
            self.assertIn(shape, module.CONFINEMENT_ESCAPE_NAMES)
        self.assertEqual(
            set(module.CONFINEMENT_ESCAPE_NAMES),
            {attempt.name for attempt in current.attempts},
        )
        for attempt in current.attempts:
            with self.subTest(escape=attempt.name):
                self.assertEqual("path_not_contained", attempt.refusal_code)
                self.assertFalse(attempt.wrote)
        self.assertEqual((), current.leaked)

        self.assertEqual(
            module.package_release_id(REPO_ROOT),
            module.require_confinement_current(current, repo_root=REPO_ROOT),
        )
        with self.assertRaises(ProtocolRefusal) as stale:
            module.require_confinement_current(elsewhere, repo_root=REPO_ROOT)
        self.assertEqual("hook_confinement_release_stale", stale.exception.code)
        self.assertIn("9.9.9", stale.exception.detail)


class HookTrustConfigWriteTests(HookPreconditionFixture):
    def test_hook_session_privilege_cannot_write_the_codex_trust_config(
        self,
    ) -> None:
        """CLAUSE 3. A writer holding exactly the hook session's authority is
        made to ATTEMPT the write of the Codex trust config under every name it
        can spell; each attempt refuses `path_not_contained`, the whole Codex
        home stays byte-identical, and a live hook invocation touches none of
        it."""

        module = _require_module()
        before = self.tree_bytes(self.codex_home)

        attempts = module.attempt_trust_config_write(
            root=self.participant.root, target=self.trust_config
        )
        self.assertEqual(4, len(module.TRUST_CONFIG_WRITE_NAMES))
        for shape in ("absolute", "parent_traversal", "symlink_component"):
            self.assertIn(shape, module.TRUST_CONFIG_WRITE_NAMES)
        self.assertEqual(
            set(module.TRUST_CONFIG_WRITE_NAMES),
            {attempt.name for attempt in attempts},
        )
        for attempt in attempts:
            with self.subTest(name=attempt.name):
                self.assertEqual("path_not_contained", attempt.refusal_code)
                self.assertFalse(attempt.wrote)

        self.assertEqual(before, self.tree_bytes(self.codex_home))

        # And the real hook session, run to completion, writes none of it.
        self.burn_one_invocation()
        self.assertEqual(before, self.tree_bytes(self.codex_home))

        # The trust observation itself is unchanged by the attempt: the hook is
        # still what it was, and still not trusted by us.
        from floati.codex_hook_trust import observe_codex_waiter_hooks

        rows = observe_codex_waiter_hooks(self.hooks_path, self.trust_config)
        self.assertEqual(1, len(rows))
        self.assertEqual("modified", rows[0]["hook_trust_status"])
        self.assertFalse(rows[0]["hook_armed"])


if __name__ == "__main__":  # pragma: no cover - focused runner entry
    unittest.main()
