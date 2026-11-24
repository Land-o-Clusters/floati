"""Real scratch waiter launch and bounded observation shared by lifecycle tests."""
from __future__ import annotations

from tests.readiness import READY_SECONDS, wait_until

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.stop_hold import StopHoldLedger
from floati.waiter_consent import arm_waiter_consent
from tests.temp_roots import REAL_TEMP_ROOT

PROBE = Path(__file__).with_name("probe_cursor_wait_invocation.py").resolve()


class CursorInvocationFixture:
    def __init__(self, test, *, root=None, workspace=None, node="cursor-seat", session="cursor-live-chat", harness="Cursor"):
        self.test = test
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        test.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = root or FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.node, self.session = node, session
        self.harness = harness
        if root is None:
            Registry(self.root).register("architect", "architect")
            Registry(self.root).register(node, harness)
        self.workspace = workspace or self.base / "workspace"
        self.workspace.mkdir(exist_ok=True)
        self.runtime = self.base / "runtime"
        self.runtime.mkdir()
        arm_waiter_consent(self.root.path, self.workspace, node, harness, 60, 30)
        participant = resolve_participant(self.root.path, self.workspace)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        CodexWaitSessionLedger(self.root).arm(participant.binding, consent, session,
            idempotency_key="invocation-fixture-current-consent")
        self.jobs = []
        test.addCleanup(self.close)

    def rows(self):
        return StopHoldLedger(self.root).read(self.node)

    def start(self, *, host=False):
        control = self.base / ("host-stop-" + str(len(self.jobs)))
        home = self.base / ("home-" + str(len(self.jobs)))
        home.mkdir()
        stdout = (self.base / ("out-" + str(len(self.jobs)))).open("w+")
        stderr = (self.base / ("err-" + str(len(self.jobs)))).open("w+")
        environment = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen(["/usr/bin/python3", "-B", str(PROBE), str(self.root.path),
            self.node, str(self.runtime), self.session,
            "host-chain" if host == "chain" else "host" if host else "codex" if self.harness == "Codex" else "wait", str(control)],
            cwd=str(self.workspace), env=environment, stdout=stdout, stderr=stderr)
        job = {"process": process, "stdout": stdout, "stderr": stderr, "control": control}
        self.jobs.append(job)
        return job

    def wait_until(self, predicate, detail, seconds=READY_SECONDS):
        try:
            wait_until(predicate, detail, timeout=seconds)
        except AssertionError as error:
            self.test.fail(str(error) + "; stderr=" + repr(self.errors()))

    def wait_started(self, count=1):
        self.wait_until(lambda: len([row for row in self.rows() if row["phase"] == "start"]) >= count,
            "scratch hook did not start its native hold")

    def errors(self):
        result = []
        for job in self.jobs:
            job["stderr"].flush()
            job["stderr"].seek(0)
            result.append(job["stderr"].read())
        return result

    def send(self, key):
        return EventLog(self.root).send("architect", self.node, "floati", "a" * 40,
            "docs/evidence/mail.md", "scratch mail", idempotency_key=key)

    def close(self):
        for job in self.jobs:
            process = job["process"]
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            for stream in (job["stdout"], job["stderr"]):
                stream.close()
