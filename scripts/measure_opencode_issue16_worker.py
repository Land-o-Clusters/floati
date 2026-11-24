"""One-shot governed-worker remeasurement; owns scratch state and child cleanup.

Use the owner's HOME for the explicitly selected free model. Never reads auth.
The original issue-16 photograph is immutable; this drill writes a new capture.
"""

import io
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from floati.adapters.headless_template import HarnessProfile
from floati.adapters.opencode import OpenCodeAdapter
from floati.admin_registry import RegistryAdminBackend
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.node_wizard import NodeWizard
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerReceipts, WorkerRunner


ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "tests/fixtures/opencode-issue16-worker-2026-10-25.json"
BINARY = "/opt/homebrew/bin/opencode"
MODEL = "opencode/glm-5.3-flash-free"


def classify_worker_receipt(receipt):
    """Replay the captured Python worker path, without asserting live defects."""
    phases = {row["phase"]: row for row in receipt["run_invocations"]}

    def completed(row):
        item = row["item"]
        result = row.get("result", {})
        bindings = result.get("artifact_bindings", [])
        records = row.get("receipts", [])
        audit = row["process_audit"]
        return (result.get("transition") == "complete" and result.get("outcome_code") is None
            and bool(bindings) and row["work_after"].get("state") == "completed"
            and row["work_after"].get("artifact_bindings") == bindings
            and [r["transition"] for r in records] == ["claim", "spawn", "drive", "bind_artifact", "complete"]
            and all(r["node_id"] == item["owner"] and r["adapter"] == "opencode"
                    and r["work_item_id"] == item["id"] and r["session_id"] == result["session_id"]
                    for r in records)
            and records[-1] == result and records[-2]["artifact_bindings"] == bindings
            and [a["binding"] for a in row["artifacts"]] == bindings
            and all(a["content"].strip() == "OK" and a["binding"]["doc"] == "answer.txt"
                    for a in row["artifacts"])
            and audit["adapter_pid"] is not None and bool(audit["registered_process_groups"])
            and audit["alive_after_cleanup"] == [])

    boundary = phases["expiry_boundary"]
    before, after = boundary["lease_at_start"], boundary["lease_at_end"]
    expiry = datetime.fromisoformat(before["expires_at"].replace("Z", "+00:00"))
    crossed = (completed(boundary) and before["state"] == "active" and after["state"] == "expired"
        and before["node_lease_id"] == after["node_lease_id"] == receipt["lease_before"]["node_lease_id"]
        and before["expires_at"] == after["expires_at"] == receipt["lease_before"]["expires_at"]
        and datetime.fromisoformat(boundary["started_at"])
        <= datetime.fromisoformat(boundary["receipts"][0]["timestamp"].replace("Z", "+00:00")) < expiry
        < datetime.fromisoformat(boundary["result"]["timestamp"].replace("Z", "+00:00")))
    expired = receipt["expired_start"]
    audit = expired["process_audit"]
    fenced = (expired.get("refusal", {}).get("code") == "node_lease_expired"
        and datetime.fromisoformat(expired["started_at"]) > expiry
        and audit["adapter_pid"] is None and audit["registered_process_groups"] == []
        and audit["alive_after_cleanup"] == [] and expired["worker_receipts"] == []
        and any(r["node_id"] == expired["item"]["owner"] and r["adapter"] == "opencode"
                and r["reason_code"] == "worker_claim_lost" for r in expired["refusal_receipts"]))
    return {
        "round_trip": "complete_with_bound_artifact" if completed(phases["round_trip"]) else "cannot-measure:worker_round_trip_unproved",
        "expiry_boundary": "admitted_worker_completed_after_lease_expiry" if crossed else "cannot-measure:worker_expiry_boundary_unproved",
        "expired_start": "expired_start_refused_before_spawn" if fenced else "cannot-measure:expired_admission_unproved",
    }


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def capture():
    preflight = []
    if RECEIPT.exists():
        previous = json.loads(RECEIPT.read_text())
        if (previous.get("run_invocations") != [] or not previous.get("scratch_removed")
                or not previous.get("work_workspaces_removed")):
            raise SystemExit("worker remeasurement already attempted; refusing quota replay")
        preflight = [previous]
    if not Path(BINARY).is_file():
        print(json.dumps({"status": "skipped", "reason": "opencode_binary_unavailable", "binary": BINARY}))
        return
    receipt = {"artifact_version": 0, "model": MODEL, "home": "owner",
               "source_sha": subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
               "run_invocations": [], "started_at": timestamp(), "preflight_attempts": preflight}

    def save():
        RECEIPT.write_text(json.dumps(receipt, indent=2) + "\n")

    save()  # Refuse replay even after an interrupted attempt.
    scratch = Path(tempfile.mkdtemp(prefix="floati-issue16-worker-", dir="\x2fprivate/tmp"))
    workspaces = []
    try:
        root = FloatiRoot.open_direct_home(scratch / "fleet", create=True)
        node = "oc-temp"
        added_at = datetime.now(timezone.utc)
        receipt["node_add"] = NodeWizard(root, RegistryAdminBackend(root), id_factory=uuid7_hex,
            now=lambda: added_at).add_from_keys([node, "OpenCode", "temporary", "1"], io.StringIO())
        receipt["lease_before"] = Registry(root).node_lease_state(node)
        expiry = datetime.fromisoformat(receipt["lease_before"]["expires_at"].replace("Z", "+00:00"))
        AuthorityGrantStore(root).claim("oc-claims", node, 300, 120, added_at)
        work = WorkLog(root)
        for phase in ("round_trip", "expiry_boundary"):
            item = work.add("Do nothing else.", node, [], provision_workspace=True)
            workspace = Path(item["workspace"])
            workspaces.append(workspace)
            # Use an absolute, owned output path: a configured server may have
            # a project directory different from its CLI client's cwd.
            prompt = "Write OK to " + str(workspace / "answer.txt") + "."
            profile = HarnessProfile(name="opencode", command=(BINARY,),
                headless_arguments=("run", "--model", MODEL, "--format", "json"),
                prompt_form=("--", prompt), stderr_name="opencode.stderr",
                cited_source="https://opencode.ai/docs/cli/")
            adapter = OpenCodeAdapter(profile)
            runner = WorkerRunner(root, {"opencode": adapter}, call_timeout=60)
            if phase == "expiry_boundary":
                while (expiry - datetime.now(timezone.utc)).total_seconds() > 2:
                    time.sleep(0.25)
            row = {"phase": phase, "item": item, "started_at": timestamp(),
                   "lease_at_start": Registry(root).node_lease_state(node),
                   "argv": [BINARY, *profile.headless_arguments, *adapter._prompt_arguments(item)]}
            receipt["run_invocations"].append(row)
            save()  # Debit before the runner can spawn a model-capable process.
            try:
                row["result"] = runner.run(node, "opencode")
            except ProtocolRefusal as exc:
                row["refusal"] = {"code": exc.code, "detail": exc.detail}
            row["ended_at"] = timestamp()
            row["lease_at_end"] = Registry(root).node_lease_state(node)
            row["process_audit"] = runner.last_process_audit
            row["receipts"] = [record for record in WorkerReceipts(root).records()
                               if record["work_item_id"] == item["id"]]
            row["work_after"] = work.show(item["id"])[0]
            row["stderr"] = (workspace / ".floati" / "opencode.stderr").read_text() if (workspace / ".floati" / "opencode.stderr").exists() else ""
            row["artifacts"] = []
            for binding in row.get("result", {}).get("artifact_bindings", []):
                content = subprocess.check_output(["/usr/bin/git", "show", binding["sha"] + ":" + binding["doc"]], cwd=workspace).decode()
                row["artifacts"].append({"binding": binding, "content": content})
            save()
            print(json.dumps({"phase": phase, "result": row.get("result"), "refusal": row.get("refusal"),
                              "lease_at_end": row["lease_at_end"], "process_audit": row["process_audit"]}), flush=True)
        while datetime.now(timezone.utc) <= expiry:
            time.sleep(0.25)
        item = work.add("Reply OK. No tools.", node, [], provision_workspace=True)
        workspaces.append(Path(item["workspace"]))
        runner = WorkerRunner(root, {"opencode": OpenCodeAdapter(profile)}, call_timeout=60)
        receipt["expired_start"] = {"started_at": timestamp(), "item": item}
        try:
            receipt["expired_start"]["result"] = runner.run(node, "opencode")
        except ProtocolRefusal as exc:
            receipt["expired_start"]["refusal"] = {"code": exc.code, "detail": exc.detail}
        receipt["expired_start"].update(process_audit=runner.last_process_audit,
            worker_receipts=[r for r in WorkerReceipts(root).records() if r["work_item_id"] == item["id"]],
            refusal_receipts=runner.refusals.records())
        receipt["lease_after"] = Registry(root).node_lease_state(node)
    finally:
        for workspace in workspaces:
            if workspace.exists():
                shutil.rmtree(workspace)
        shutil.rmtree(scratch)
        receipt["scratch_removed"] = not scratch.exists()
        receipt["work_workspaces_removed"] = all(not path.exists() for path in workspaces)
        receipt["ended_at"] = timestamp()
        save()
    print(json.dumps({"expired_start": receipt["expired_start"], "run_invocations": len(receipt["run_invocations"]),
                      "scratch_removed": receipt["scratch_removed"], "work_workspaces_removed": receipt["work_workspaces_removed"]}), flush=True)


if __name__ == "__main__":
    capture()
