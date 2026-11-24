"""Bounded, owner-authorized issue-16 intake; never reads provider credentials.

Run from the repository root with the owner's HOME. Each phase refuses replay.
Children, scratch roots, and their cleanup belong to this visible script.
"""

import argparse
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from floati.adapters.opencode import OpenCodeAdapter
from floati.host_paths import worker_workspace_root
from floati.ids import uuid7_hex
from floati.admin_registry import RegistryAdminBackend
from floati.cursor import SparseCursor
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.node_wizard import NodeWizard
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerReceipts, WorkerRunner


ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "tests/fixtures/opencode-issue16-2026-10-25.json"
BINARY = "/opt/homebrew/bin/opencode"
MODEL = "opencode/glm-5.3-flash-free"


def classify_receipt(receipt):
    """Derive only the observations proved by the captured native receipts."""
    calls = receipt["run_invocations"]
    details = receipt["dynamics"]
    surface_ok = calls[0].get("exit_code") == 0 and calls[0].get("adapter_validation") == "ok"
    lease = details["lease_after"]
    ack = details["late_ack"]
    lease_proved = (lease["state"] == "expired"
        and details["fresh_delivery_after_expiry"]["code"] == "node_lease_expired"
        and ack["node_lease_id"] == lease["node_lease_id"]
        and ack["node_lease_expires_at"] == lease["expires_at"]
        and ack["node_lease_state_at_ack"] == "expired")
    session = details.get("interrupted_session")
    interrupted = next((row for row in calls if row.get("signal") == "SIGTERM"), {})
    interrupted_argv = interrupted.get("argv", [])
    interrupted_session_matches = ("--session" in interrupted_argv
        and interrupted_argv[interrupted_argv.index("--session") + 1] == session)
    resumed = calls[-1]
    reply = []
    for line in resumed.get("stdout", "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "text" and event.get("sessionID") == session:
            reply.append(event.get("part", {}).get("text", ""))
    exports = []
    for key in ("session_before_interrupt", "session_after_interrupt", "session_after_resume"):
        row = details.get(key, {})
        try:
            exports.append(json.loads(row["stdout"])["info"]["id"] if row.get("exit_code") == 0 else None)
        except (KeyError, ValueError):
            exports.append(None)
    restart_proved = (bool(session) and exports == [session] * 3
        and interrupted_session_matches
        and interrupted.get("running_before_interrupt") is True
        and interrupted.get("owned_child_reaped") is True
        and resumed.get("exit_code") == 0 and "--session" in resumed["argv"]
        and resumed["argv"][resumed["argv"].index("--session") + 1] == session
        and "".join(reply).strip() == details["nonce"])
    worker = details["worker_result"]
    worker_blocked = worker["transition"] == "degrade" and worker["outcome_code"] == "adapter_error"
    after_expiry = (datetime.fromisoformat(resumed["started_at"]) >
                    datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00")))
    return {
        "surface_verified": surface_ok,
        "default_entrypoint": ("ok" if surface_ok else
            "cannot-measure:default_provider_error" if "Provider returned error" in calls[0].get("stderr", "")
            else "cannot-measure:entrypoint_failed"),
        "lease": "expired_delivery_refused_late_ack_retained" if lease_proved else "cannot-measure:lease_evidence_incomplete",
        "restart": "conversation_survived_client_restart" if restart_proved else "cannot-measure:conversation_recovery_unproved",
        "native_resume_after_lease_expiry": bool(restart_proved and lease_proved and after_expiry),
        "governed_worker_lease": "cannot-measure:worker_adapter_error" if worker_blocked else "cannot-measure:worker_lease_not_observed",
        "floati_native_session_binding": "cannot-measure:opencode_not_in_worker_cli_adapter_choices",
        "worker_receipts_unchanged_by_native_resume": details["worker_receipts_before_restart"] == details["worker_receipts_after_restart"],
        "run_invocations_including_help": len(calls) + receipt["non_model_run_help_invocations"],
    }


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def save(receipt):
    RECEIPT.write_text(json.dumps(receipt, indent=2) + "\n")


def native_call(receipt, workspace, arguments, *, interrupt=False):
    if len(receipt["run_invocations"]) + receipt["non_model_run_help_invocations"] >= 6:
        raise RuntimeError("six-invocation budget exhausted")
    row = {"argv": [BINARY, "run", *arguments], "started_at": timestamp(),
           "home": "owner", "workspace": str(workspace)}
    receipt["run_invocations"].append(row)
    save(receipt)  # Debit before launch, including a failed launch.
    process = subprocess.Popen(row["argv"], cwd=workspace, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    try:
        if interrupt:
            time.sleep(0.5)
            row["running_before_interrupt"] = process.poll() is None
            if row["running_before_interrupt"]:
                process.terminate()  # Exact child deliberately restarted by this drill.
                row["signal"] = "SIGTERM"
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        row["deadline"] = "cannot-measure:provider_deadline"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        row["owned_child_reaped"] = process.poll() is not None
    row.update(exit_code=process.returncode, stdout=stdout.decode(errors="replace"),
               stderr=stderr.decode(errors="replace"), ended_at=timestamp())
    save(receipt)
    print(json.dumps({key: row[key] for key in ("argv", "exit_code", "ended_at")}), flush=True)
    return row


def session_ids(row):
    found = []
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("sessionID", "sessionId", "session_id") and isinstance(item, str):
                    found.append(item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    for line in row["stdout"].splitlines():
        try:
            visit(json.loads(line))
        except ValueError:
            continue
    return list(dict.fromkeys(found))


def export_session(workspace, session):
    result = subprocess.run([BINARY, "session", "export", session], cwd=workspace,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    return {"argv": [BINARY, "session", "export", session], "exit_code": result.returncode,
            "stdout": result.stdout.decode(errors="replace"),
            "stderr": result.stderr.decode(errors="replace")}


def dynamics():
    receipt = json.loads(RECEIPT.read_text())
    if "dynamics" in receipt:
        raise SystemExit("dynamics already attempted; refusing another quota call")
    scratch = Path(tempfile.mkdtemp(prefix="floati-issue16-", dir="\x2fprivate/tmp"))
    details = receipt["dynamics"] = {"scratch": str(scratch), "model_override": MODEL}
    save(receipt)
    workspace = None
    try:
        root = FloatiRoot.open_direct_home(scratch / "fleet", create=True)
        node = "oc-temp"
        now = datetime.now(timezone.utc)
        added = NodeWizard(root, RegistryAdminBackend(root), id_factory=uuid7_hex,
                           now=lambda: now).add_from_keys(
                               [node, "OpenCode", "temporary", "1"], io.StringIO())
        details["node_add"] = added
        details["lease_before"] = Registry(root).node_lease_state(node)
        AuthorityGrantStore(root).claim("oc-claims", node, 300, 60, now)
        work = WorkLog(root)
        item = work.add("Reply OK.", node, [], provision_workspace=True)
        workspace = Path(item["workspace"])
        details["work_item"] = item
        # This is the shipped worker path, not an adapter replacement.
        receipt["run_invocations"].append({
            "argv": [BINARY, "run", "--", item["title"]], "home": "owner",
            "started_at": timestamp(), "via": "WorkerRunner"})
        save(receipt)
        runner = WorkerRunner(root, {"opencode": OpenCodeAdapter()}, call_timeout=15)
        details["worker_result"] = runner.run(node, "opencode")
        details["worker_process_audit"] = runner.last_process_audit
        details["worker_receipts_before_restart"] = WorkerReceipts(root).records()
        receipt["run_invocations"][-1]["ended_at"] = timestamp()
        print(json.dumps({"worker_result": details["worker_result"],
                          "worker_process_audit": runner.last_process_audit}), flush=True)
        events = EventLog(root)
        sha = subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
        message = events.send(node, node, "floati", sha, "README.md", "issue 16 scratch lease",
                              idempotency_key="issue16-before-expiry")
        _, delivery = events.present(node)
        details["message"] = message
        details["delivery"] = delivery
        nonce = "issue16-" + uuid7_hex()
        details["nonce"] = nonce
        seed = native_call(receipt, workspace, ["--model", MODEL, "--format", "json", "--",
                            "Remember " + nonce + ". Reply OK. No tools."])
        sessions = session_ids(seed)
        details["seed_sessions"] = sessions
        if len(sessions) == 1:
            session = sessions[0]
            details["session_before_interrupt"] = export_session(workspace, session)
            interrupted = native_call(receipt, workspace,
                ["--model", MODEL, "--format", "json", "--session", session,
                 "--", "Reply OK. No tools."], interrupt=True)
            details["interrupted_session"] = session
            details["session_after_interrupt"] = export_session(workspace, session)
        else:
            details["restart"] = {"status": "cannot-measure", "reason": "native_session_id_unavailable"}
        expiry = datetime.fromisoformat(details["lease_before"]["expires_at"].replace("Z", "+00:00"))
        print(json.dumps({"waiting_for_lease": expiry.isoformat()}), flush=True)
        while datetime.now(timezone.utc) <= expiry:
            time.sleep(1)
        details["lease_after"] = Registry(root).node_lease_state(node)
        try:
            events.present(node)
        except ProtocolRefusal as exc:
            details["fresh_delivery_after_expiry"] = {"code": exc.code, "detail": exc.detail}
        details["late_ack"] = SparseCursor(root).ack(node, [message["id"]],
                                                     acting_session_id="issue16-after-expiry")
        if len(sessions) == 1:
            native_call(receipt, workspace,
                ["--model", MODEL, "--format", "json", "--session", session,
                 "--", "What nonce did I ask you to remember? No tools."])
            details["session_after_resume"] = export_session(workspace, session)
        details["worker_receipts_after_restart"] = WorkerReceipts(root).records()
        details["registry_records"] = Registry(root).known_records()
        details["public_worker"] = {"status": "cannot-measure", "reason": "opencode_not_in_worker_cli_adapter_choices"}
    finally:
        if workspace is not None and workspace.exists():
            shutil.rmtree(workspace)
        shutil.rmtree(scratch)
        details["scratch_removed"] = not scratch.exists()
        details["work_workspace_removed"] = workspace is None or not workspace.exists()
        save(receipt)
    print(json.dumps({"lease_after": details["lease_after"],
                      "total_run_invocations_including_help": len(receipt["run_invocations"]) + 1}), flush=True)


def surface():
    if RECEIPT.exists():
        raise SystemExit("surface intake already recorded; refusing another quota call")
    adapter = OpenCodeAdapter()
    work_id = "work-" + uuid7_hex()
    workspace = worker_workspace_root() / work_id
    item = {"id": work_id, "workspace": str(workspace),
            "title": "Reply exactly OK. Do not use tools."}
    argv = [*adapter.command, *adapter.profile.headless_arguments,
            *adapter._prompt_arguments(item)]
    row = {"argv": argv, "started_at": timestamp(), "home": "owner",
           "workspace": str(workspace)}
    handle = None
    try:
        handle = adapter.spawn(item, deadline_seconds=90)
        try:
            stdout, _ = handle.process.communicate(timeout=90)
            row.update(exit_code=handle.process.returncode,
                       stdout=stdout.decode("utf-8", errors="replace"),
                       stderr=handle.stderr_path.read_text(errors="replace"))
            try:
                adapter._validate_result(stdout, handle.process.returncode, handle.stderr_path)
                row["adapter_validation"] = "ok"
            except Exception as exc:
                row["adapter_validation"] = getattr(exc, "code", str(exc))
        except subprocess.TimeoutExpired:
            row["adapter_validation"] = "cannot-measure:entrypoint_deadline"
    finally:
        if handle is not None and handle.process.poll() is None:
            adapter.cancel()
        elif handle is not None:
            if handle.process.stdout is not None:
                handle.process.stdout.close()
            adapter._active_process = None
        row["ended_at"] = timestamp()
        row["owned_child_reaped"] = handle is None or handle.process.poll() is not None
        if workspace.exists():
            shutil.rmtree(workspace)
    receipt = {"artifact_version": 0, "binary": BINARY, "version": "2.1.4",
               "non_model_run_help_invocations": 1, "run_invocations": [row]}
    RECEIPT.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(row, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("surface", "dynamics", "classify"))
    arguments = parser.parse_args()
    if arguments.phase == "classify":
        receipt = json.loads(RECEIPT.read_text())
        receipt["classification"] = classify_receipt(receipt)
        save(receipt)
        print(json.dumps(receipt["classification"], indent=2))
        return
    if not Path(BINARY).is_file():
        print(json.dumps({"status": "skipped", "reason": "opencode_binary_unavailable", "binary": BINARY}))
        return
    if arguments.phase == "surface":
        surface()
    else:
        dynamics()


if __name__ == "__main__":
    main()
