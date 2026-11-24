"""Fixture-native host tool refusal, through the existing Floati hook entry."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import tests
from floati.codex_wait_contract import (CodexWaitConsentLedger,
    CodexWaitSessionLedger, resolve_participant)
from floati.events import EventLog
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.waiter_consent import arm_waiter_consent
from floati.work import WorkLog
from tests.schema_validation import validate_json_schema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "tests/fixtures/f2-seat-stall-host-1/posttooluse-shipped-schema.json"
SCHEMA_PIN = "8ea1e4bccb262fad05b85c300d562d2653c5a64118d6a2c5704468fc4ea836a9"
BODY = "CODEX_FLEET_BUS_APPROVAL_REQUIRED: the selected fleet bus profile is not registered"
COMMAND = "~/.codex/bin/codex-fleet-bus fixture-profile send --to architect"
SESSION = "host-tool-native-session"
TURN = "host-tool-native-turn"
TOOL = "host-tool-native-call"
BINDING = {"repo": "floati", "sha": "a" * 40, "doc": "docs/car.md"}


class HostToolCallbackNativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hc-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        registry = Registry(self.root)
        registry.register("builder", "Codex")
        registry.register("architect", "Codex")
        registry.record_governance(topology="star", coordinator="architect",
            coordinator_authority=("dispatch_bounded_work",),
            owner_tier=("publishing", "credentials", "key_custody"))
        self.work = WorkLog(self.root)
        self.now = datetime.now(timezone.utc)
        self.grant = AuthorityGrantStore(self.root).claim("work-claims", "builder", 60, 60, self.now)
        self.item = self.work.add("host refusal", "builder", [BINDING], now=self.now,
            car_binding={"row": "F2-SEAT-STALL-HOST-1", "repository": "floati", "branch": "codex/car"})
        self.claim = self.work.claim(self.item["id"], "builder", "work-claims",
            self.grant["epoch"], now=self.now)
        arm_waiter_consent(self.root.path, self.workspace, "builder", "Codex", 2, 1)
        participant = resolve_participant(self.root.path, self.workspace)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        self.consent = consent
        self.session_claim = CodexWaitSessionLedger(self.root).arm(participant.binding, consent,
            SESSION, idempotency_key="host-fixture", take_over=True)

    def payload(self, exit_code=64, output=BODY):
        return {"cwd": str(self.workspace), "hook_event_name": "PostToolUse",
            "model": "fixture-model", "permission_mode": "default",
            "session_id": SESSION, "transcript_path": None, "turn_id": TURN,
            "tool_name": "Bash", "tool_use_id": TOOL,
            "tool_input": {"command": COMMAND},
            "tool_response": {"exit_code": exit_code, "output": output}}

    def validate_shipped_contract(self, payload):
        data = SCHEMA.read_bytes().rstrip(b"\n")
        self.assertEqual(SCHEMA_PIN, hashlib.sha256(data).hexdigest())
        schema = json.loads(data)
        # This repository's small validator predates boolean schemas. Empty
        # object is the standard equivalent of true, not a narrower contract.
        schema["properties"]["tool_input"] = {}
        schema["properties"]["tool_response"] = {}
        path = self.base / "validation.json"
        path.write_text(json.dumps(schema))
        validate_json_schema(payload, path)

    def hook(self, payload):
        self.validate_shipped_contract(payload)
        child = subprocess.run([sys.executable, "-B",
            str(ROOT / "scripts" / "floati-codex-wait"), "--root", str(self.root.path)],
            input=json.dumps(payload), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10, check=False)
        self.assertEqual(0, child.returncode, child.stderr + child.stdout)
        return child

    def executions(self):
        return [row for row in EventLog(self.root).event_records() if row["kind"] == "seat_execution"]

    def test_refusal_publishes_typed_action_with_native_car_and_exact_body(self):
        self.hook(self.payload())
        rows = self.executions()
        self.assertEqual(2, len(rows), "native PostToolUse refusal never produced execution testimony")
        row = rows[-1]
        self.assertEqual("refused_action", row["event"])
        self.assertEqual("CODEX_FLEET_BUS_APPROVAL_REQUIRED", row["code"])
        self.assertEqual(COMMAND, row["command"])
        self.assertEqual(BODY, row["detail"])
        self.assertEqual(self.claim["id"], row["work_claim_id"])
        self.assertEqual(SESSION, row["acting_session_id"])

    def test_refusal_atomically_posts_one_dispatcher_envelope_and_readiness(self):
        self.hook(self.payload())
        rows = EventLog(self.root).event_records()
        envelopes = [row for row in rows if row["kind"] == "message_envelope"]
        self.assertEqual(1, len(envelopes), "refusal never reached the declared dispatcher")
        envelope = envelopes[0]
        self.assertEqual("architect", envelope["recipient"])
        self.assertEqual("builder", envelope["sender"])
        self.assertEqual(self.executions()[-1]["envelope_id"], envelope["id"])
        self.assertEqual(1, sum(row["kind"] == "send_readiness_observation" for row in rows))

    def test_success_is_silent_even_when_output_mentions_refusal_token(self):
        before = EventLog(self.root).event_records()
        child = self.hook(self.payload(0, BODY))
        self.assertEqual("", child.stdout, "PostToolUse success must never enter Stop waiting")
        self.assertEqual("", child.stderr)
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_unmapped_workspace_cannot_invent_a_host_source(self):
        payload = self.payload()
        payload["cwd"] = str(self.base)
        self.hook(payload)
        self.assertEqual([], self.executions())

    def test_no_claim_is_no_car(self):
        self.work.complete(self.item["id"], "builder", [BINDING], now=self.now)
        self.hook(self.payload())
        self.assertEqual([], self.executions())

    def test_shipped_schema_proves_shape_but_not_native_response_object_keys(self):
        payload = self.payload()
        for value in (None, "unmeasured text", {"anything": True}):
            payload["tool_response"] = value
            self.validate_shipped_contract(payload)


if __name__ == "__main__":
    unittest.main()
