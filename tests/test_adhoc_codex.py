"""Task4 persistent native Codex contract, qualified only by fake processes."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from floati.adapters.codex_live import CodexAppServerAdapter
from floati.adhoc_records import CODEX_PERMISSION
from floati.errors import ProtocolRefusal
from floati.worker_bootstrap_protocol import NATIVE_THREAD_BOUNDS
from floati.worker_errors import WorkerAdapterFailure
from tests.temp_roots import REAL_TEMP_ROOT

HARNESS = Path(__file__).parent / "fixtures/adhoc-native/codex_harness.py"
IDENT = "019a17f0000070008000000000000001"
BINDING_FIELDS = ("profile_id", "profile_digest", "dispatch_id", "dispatch_digest", "node_id",
                  "node_lease_id", "work_item_id", "effect_binding")

class AdhocCodexTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name).resolve()
        self.workspace = self.base / "row"
        self.slot = self.base / "provider"
        self.store = self.base / "independent-provider-store"
        self.workspace.mkdir(mode=0o700)
        self.slot.mkdir(mode=0o700)
        self.events = []
        self.context = {"schema_version": 1, "profile_id": "adhoc-profile-" + IDENT,
            "profile_digest": "a" * 64, "dispatch_id": "adhoc-dispatch-" + IDENT,
            "dispatch_digest": "b" * 64, "node_id": "node-a", "node_lease_id": "lease-" + IDENT,
            "work_item_id": "work-" + IDENT, "operation": "start", "provider_coordinate": None,
            "workspace_binding": {"profile_id": "adhoc-profile-" + IDENT,
                "lane_opening_id": "lane-workspace-" + IDENT, "node_lease_id": "lease-" + IDENT,
                "workspace": str(self.workspace)},
            "provider_state_binding": {"path": str(self.slot), "dev": self.slot.stat().st_dev,
                                       "ino": self.slot.stat().st_ino},
            "permission": deepcopy(CODEX_PERMISSION), "bounds": dict(NATIVE_THREAD_BOUNDS),
            "effect_binding": {"run_id": "run-" + IDENT, "item_id": "work-" + IDENT,
                               "attempt_id": "attempt-" + IDENT}}
        self.item = {"id": self.context["work_item_id"], "title": "Write PROOF.txt",
                     "workspace": str(self.workspace), "adhoc_binding": self.context["workspace_binding"]}

    def emit(self, event):
        self.events.append(event)
        if event["event"] == "opened":
            self.store.mkdir(exist_ok=True)
            (self.store / "opened-ack").write_text("durably accepted")
            return {**event, "event": "opened_ack", "opened_record_id": "adhoc-native-opened-" + IDENT}

    def run_native(self, *, mode="normal", context=None, callback=None):
        context = self.context if context is None else context
        adapter = CodexAppServerAdapter((str(Path(sys.executable).resolve()), str(HARNESS.resolve()),
                                        str(self.store), mode))
        self.addCleanup(adapter.cancel)
        adapter.set_native_thread_context(context, self.emit if callback is None else callback)
        handle = adapter.spawn(self.item, deadline_seconds=4)
        return adapter.drive(handle, self.item, deadline_seconds=4)

    def capture(self, **kwargs):
        try:
            return None, self.run_native(**kwargs)
        except (WorkerAdapterFailure, ProtocolRefusal) as failure:
            return failure.code, None

    def requests(self):
        path = self.store / "requests.jsonl"
        return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines()]

    def test_two_processes_resume_one_persistent_conversation(self):
        failure, result = self.capture()
        self.assertIsNone(failure, "a lawful borrowed start must run: " + str(failure))
        self.assertFalse((self.store / "prompt-before-ack").exists())
        self.assertEqual([event["event"] for event in self.events], ["opened", "terminal"])
        coordinate = self.events[0]["provider_coordinate"]
        resume = deepcopy(self.context)
        resume.update(operation="resume", provider_coordinate=coordinate)
        failure, result = self.capture(context=resume)
        self.assertIsNone(failure)
        state = json.loads((self.store / "state.json").read_text())
        self.assertEqual((state["creations"], state["turns"]), (1, 2))
        launches = [json.loads(line) for line in (self.store / "launches.jsonl").read_text().splitlines()]
        self.assertNotEqual(launches[0]["pid"], launches[1]["pid"])
        self.assertTrue(all(row["cwd"] == str(self.workspace) and row["home"] == str(self.slot) for row in launches))
        starts = [r for r in self.requests() if r.get("method") == "thread/start"]
        resumes = [r for r in self.requests() if r.get("method") == "thread/resume"]
        self.assertIs(starts[0]["params"]["ephemeral"], False)
        self.assertEqual(resumes[0]["params"]["threadId"], coordinate["session_id"])
        # Codex's own sandbox is set on every turn to the lease alone: the row
        # is the only writable root, <temp> is excluded, TMPDIR is the slot.
        turns = [r for r in self.requests() if r.get("method") == "turn/start"]
        self.assertEqual(2, len(turns))
        for turn in turns:
            self.assertEqual({"type": "workspaceWrite", "writableRoots": [str(self.workspace)],
                "networkAccess": False, "excludeSlashTmp": True, "excludeTmpdirEnvVar": False},
                turn["params"]["sandboxPolicy"])
        self.assertEqual((self.workspace / "PROOF.txt").read_text(), "2")
        # Borrowed-row commits belong to the parent after physical cleanup;
        # the provider adapter cannot invent artifact bindings at this stage.
        self.assertEqual(result, [])

    def test_missing_permission_refuses_before_launch(self):
        context = deepcopy(self.context)
        context["permission"] = {}
        failure, _ = self.capture(context=context)
        self.assertEqual(failure, "native_permission_unsupported")
        self.assertFalse((self.store / "launches.jsonl").exists())

    def test_substituted_resume_identity_never_starts_turn(self):
        failure, _ = self.capture()
        self.assertIsNone(failure)
        context = deepcopy(self.context)
        context.update(operation="resume", provider_coordinate=self.events[0]["provider_coordinate"])
        failure, _ = self.capture(mode="substitute", context=context)
        self.assertEqual(failure, "native_session_mismatch")
        self.assertEqual(json.loads((self.store / "state.json").read_text())["turns"], 1)

    def test_wrong_cwd_refuses_before_prompt(self):
        for mode, code in (("wrong-cwd", "native_session_mismatch"),
                           ("ephemeral", "native_session_unmaterialized"),
                           ("approval", "approval_required_unattended"),
                           ("input", "approval_required_unattended"),
                           ("wrong-turn", "protocol_error"),
                           ("tail-overflow", "native_output_limit")):
            with self.subTest(mode=mode):
                self.store = self.base / mode
                self.events = []
                self.assertEqual(self.capture(mode=mode)[0], code)
                requests = self.requests()
                if mode in ("wrong-cwd", "ephemeral"):
                    self.assertFalse(any(row.get("method") == "turn/start" for row in requests))
                if mode in ("approval", "tail-overflow"):
                    self.assertFalse(any(event["event"] == "terminal" and event["outcome"] == "success"
                                         for event in self.events))
                if mode == "approval":
                    self.assertTrue(any(row.get("id") == 901 and row.get("result") == {"decision": "cancel"}
                                        for row in requests))
                if mode == "input":
                    self.assertTrue(any(row.get("id") == 901 and row.get("result") == {"answers": {}}
                                        for row in requests))
