#!/usr/bin/env python3
"""Local-only newline-delimited JSON-RPC harness for the Floati Codex adapter."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import select
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


def observed_identity(pid: int) -> object:
    """Query only a retained PID; use the runner's native birth witness."""
    source = Path(__file__).resolve().parents[3] / "scripts" / "parallel_processes.py"
    spec = importlib.util.spec_from_file_location("reference_owned_process_identity", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    row = module.ProcessQuery().identity(pid)
    if row is None:
        return None
    return {**row, "pgid": os.getpgid(pid), "sid": os.getsid(pid)}


def observe_process(workspace: Path, role: str) -> int:
    """An owned fixture child: readiness precedes completion, exit is bounded."""
    evidence = workspace / ".floati"
    stopped = [None]

    def stop(number: int, frame: object) -> None:
        if stopped[0] is None:
            stopped[0] = number

    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, stop)
    identity = observed_identity(os.getpid())
    ready = {"role": role, "workspace": str(workspace), "identity": identity,
             "observed_monotonic": time.monotonic()}
    (evidence / (role + "-ready.json")).write_text(json.dumps(ready, sort_keys=True) + "\n")
    print(json.dumps(ready, sort_keys=True), flush=True)
    # A safety watchdog for the release handshake, not a product deadline.
    deadline = time.monotonic() + 60
    release = evidence / (role + "-release")
    while stopped[0] is None and not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    result = {"role": role, "workspace": str(workspace), "ready_identity": identity,
              "exit_identity": observed_identity(os.getpid()), "signal": stopped[0],
              "reason": "signal" if stopped[0] is not None else "release" if release.exists() else "deadline",
              "observed_monotonic": time.monotonic()}
    (evidence / (role + "-exit.json")).write_text(json.dumps(result, sort_keys=True) + "\n")
    return 0


def collect_observed_descendant(child: subprocess.Popen, workspace: Path) -> None:
    """Keep the exact handle until wait returns, including bounded-wait failure."""
    errors = []
    if child.poll() is None:
        try:
            child.terminate()
        except OSError as error:
            errors.append({"operation": "terminate", "error": repr(error)})
    try:
        code = child.wait(timeout=4)
    except (subprocess.TimeoutExpired, OSError) as error:
        errors.append({"operation": "bounded_wait", "error": repr(error)})
        try:
            child.kill()
        except OSError as error:
            errors.append({"operation": "kill", "error": repr(error)})
        code = child.wait()
    result = {"pid": child.pid, "exit_code": code, "collected": True}
    if errors:
        result["collection_errors"] = errors
    (workspace / ".floati" / "descendant-collected.json").write_text(
        json.dumps(result, sort_keys=True) + "\n")
    if child.stdout is not None:
        child.stdout.close()


def start_observed_descendant(workspace: Path) -> subprocess.Popen:
    """Retain the real child before construction and record readiness."""
    child = subprocess.Popen.__new__(subprocess.Popen)
    try:
        child.__init__([sys.executable, str(Path(__file__).resolve()), "--mode", "observed-descendant",
                        "--observation-workspace", str(workspace)], cwd=workspace,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        started = time.monotonic()
        bound = 60.0
        ready, _, _ = select.select([child.stdout], [], [], bound)
        if not ready:
            raise RuntimeError(
                "owned descendant did not publish readiness: "
                f"bound={bound:.3f}s elapsed={time.monotonic() - started:.3f}s "
                f"host_load={os.getloadavg()}"
            )
        row = json.loads(child.stdout.readline())
        identity = row["identity"]
        if (row["role"] != "descendant" or row["workspace"] != str(workspace)
                or identity["pid"] != child.pid or identity["ppid"] != os.getpid()
                or identity["pgid"] != os.getpgrp() or identity["sid"] != os.getsid(0)):
            raise RuntimeError("owned descendant identity did not bind its native group")
        proof = {"app_server": observed_identity(os.getpid()), "descendant": row,
                 "workspace": str(workspace)}
        (workspace / ".floati" / "owned-group.json").write_text(json.dumps(proof, sort_keys=True) + "\n")
        return child
    except BaseException:
        if getattr(child, "pid", None) is not None:
            collect_observed_descendant(child, workspace)
        raise


def read_message() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("message must be an object")
    return value


def send(value: dict) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _write_errno(path: Path) -> object:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    except OSError as exc:
        return exc.errno
    try:
        os.write(descriptor, b"changed")
    finally:
        os.close(descriptor)
    return None


def write_boundary_evidence(args: argparse.Namespace, workspace: Path) -> None:
    """Run test-only probes from the real post-isolation provider process."""

    if args.boundary_mode is None:
        return
    if args.tenant_target is None:
        raise ValueError("boundary mode requires --tenant-target")
    tenant_target = Path(args.tenant_target)
    results: dict[str, object] = {}
    if args.boundary_mode == "main-thread":
        thread_result: list[object] = []
        thread = threading.Thread(
            target=lambda: thread_result.append(_write_errno(tenant_target))
        )
        thread.start()
        results["main"] = _write_errno(tenant_target)
        thread.join(2)
        results["thread_alive"] = thread.is_alive()
        results["thread"] = thread_result[0] if thread_result else "missing"
    elif args.boundary_mode == "provider-descendant":
        provider = subprocess.run(
            [
                "/bin/sh",
                "-c",
                'printf changed >> "$1"',
                "sh",
                os.fspath(tenant_target),
            ],
            check=False,
            capture_output=True,
        )
        results["provider"] = provider.returncode
        read_descriptor, write_descriptor = os.pipe()
        descendant_pid = os.fork()
        if descendant_pid == 0:
            os.close(read_descriptor)
            try:
                value = _write_errno(tenant_target)
                os.write(write_descriptor, json.dumps(value).encode("utf-8"))
            finally:
                os.close(write_descriptor)
                os._exit(0)
        os.close(write_descriptor)
        descendant_data = os.read(read_descriptor, 4096)
        os.close(read_descriptor)
        waited_pid, status = os.waitpid(descendant_pid, 0)
        results["descendant_status"] = status if waited_pid == descendant_pid else -1
        results["descendant"] = json.loads(descendant_data.decode("utf-8"))
    elif args.boundary_mode == "aliases":
        if args.hard_alias is None or args.symbolic_alias is None:
            raise ValueError("aliases mode requires both alias paths")
        results["hardlink"] = _write_errno(Path(args.hard_alias))
        results["symlink"] = _write_errno(Path(args.symbolic_alias))
        try:
            os.rename(tenant_target, workspace / "renamed-target")
        except OSError as exc:
            results["rename"] = exc.errno
        else:
            results["rename"] = None
    elif args.boundary_mode == "lawful":
        results["callback"] = "ready"
    else:
        raise ValueError("unknown boundary mode")
    (workspace / "isolation-evidence.json").write_text(
        json.dumps(results, sort_keys=True), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "complete",
            "complete-empty",
            "complete-many",
            "complete-background-mutate",
            "complete-background-observed",
            "observed-descendant",
            "observed-peer",
            "complete-pathspec",
            "complete-replace-git",
            "complete-symlink",
            "interleaved",
            "failed",
            "malformed",
            "hang",
            "approval-command",
            "approval-file",
            "approval-permissions",
        ),
        default="complete",
    )
    parser.add_argument(
        "--boundary-mode",
        choices=("main-thread", "provider-descendant", "aliases", "lawful"),
    )
    parser.add_argument("--tenant-target")
    parser.add_argument("--hard-alias")
    parser.add_argument("--symbolic-alias")
    parser.add_argument("--observation-workspace")
    args = parser.parse_args()
    if args.mode in ("observed-descendant", "observed-peer"):
        if args.observation_workspace is None:
            raise ValueError("observed process requires its exact workspace")
        return observe_process(Path(args.observation_workspace), args.mode.removeprefix("observed-"))
    observed_child = None
    if args.mode == "complete-background-observed":
        def stop_observed_group(number: int, frame: object) -> None:
            # Keep EOF cleanup alive when the adapter signals its native group.
            # The retained child still supplies the definitive wait receipt.
            if observed_child is not None:
                try:
                    observed_child.send_signal(number)
                except OSError:
                    pass
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, stop_observed_group)
    received: list[dict] = []

    initialize = read_message()
    received.append(initialize)
    send(
        {
            "id": initialize["id"],
            "result": {
                "codexHome": "\x2fprivate/tmp/reference-codex-home",
                "platformFamily": "unix",
                "platformOs": "macos",
                "userAgent": "floati-reference-harness/0",
            },
        }
    )
    initialized = read_message()
    received.append(initialized)
    thread_start = read_message()
    received.append(thread_start)
    workspace = Path(thread_start["params"]["cwd"])
    log_path = workspace / ".floati" / "harness-requests.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    (workspace / ".floati" / "harness.pid").write_text(str(os.getpid()), encoding="utf-8")
    (workspace / ".floati" / "harness.pgid").write_text(str(os.getpgrp()), encoding="utf-8")
    write_boundary_evidence(args, workspace)
    thread = {
        "cliVersion": "0.0.0-reference",
        "createdAt": 1785528000,
        "cwd": str(workspace),
        "ephemeral": True,
        "id": "019fba00-0000-7000-8000-000000000001",
        "modelProvider": "reference",
        "preview": "",
        "sessionId": "019fba00-0000-7000-8000-000000000002",
        "source": "appServer",
        "status": {"type": "idle"},
        "turns": [],
        "updatedAt": 1785528000,
    }
    send(
        {
            "id": thread_start["id"],
            "result": {
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "cwd": str(workspace),
                "model": "reference",
                "modelProvider": "reference",
                "sandbox": {
                    "type": "workspaceWrite",
                    "writableRoots": [str(workspace)],
                    "networkAccess": False,
                },
                "thread": thread,
            },
        }
    )
    turn_start = read_message()
    received.append(turn_start)
    turn = {
        "id": "019fba00-0000-7000-8000-000000000003",
        "items": [],
        "status": "inProgress",
    }
    if args.mode == "malformed":
        sys.stdout.write("not-json\n")
        sys.stdout.flush()
    elif args.mode == "hang":
        time.sleep(30)
    else:
        if args.mode == "interleaved":
            send({"method": "thread/started", "params": {"thread": thread}})
            send({"id": 999, "result": {"ignored": True}})
        send({"id": turn_start["id"], "result": {"turn": turn}})
        approval_methods = {
            "approval-command": "item/commandExecution/requestApproval",
            "approval-file": "item/fileChange/requestApproval",
            "approval-permissions": "item/permissions/requestApproval",
        }
        approval_method = approval_methods.get(args.mode)
        if approval_method is not None:
            send(
                {
                    "id": "approval-1",
                    "method": approval_method,
                    "params": {
                        "threadId": thread["id"],
                        "turnId": turn["id"],
                        "itemId": "item-1",
                    },
                }
            )
            try:
                while True:
                    message = read_message()
                    received.append(message)
                    if message.get("method") == "turn/interrupt":
                        send({"id": message["id"], "result": {}})
                        break
            except EOFError:
                pass
        else:
            if args.mode in (
                "complete",
                "complete-empty",
                "complete-many",
                "complete-background-mutate",
                "complete-background-observed",
                "complete-pathspec",
                "complete-replace-git",
                "complete-symlink",
                "interleaved",
            ):
                if args.mode in ("complete", "interleaved"):
                    (workspace / "PROOF.txt").write_text(
                        "floati live worker proof\n", encoding="utf-8"
                    )
                elif args.mode == "complete-background-mutate":
                    (workspace / "PROOF.txt").write_text(
                        "floati live worker proof\n", encoding="utf-8"
                    )
                    subprocess.Popen(
                        (
                            sys.executable,
                            "-c",
                            "import pathlib,sys,time; time.sleep(0.15); "
                            "root=pathlib.Path(sys.argv[1]); "
                            "(root/'LATE.txt').write_text('late mutation\\n'); "
                            "(root/'.git'/'config').write_text('[core]\\nfsmonitor=/bin/false\\n')",
                            str(workspace),
                        )
                    )
                elif args.mode == "complete-background-observed":
                    (workspace / "PROOF.txt").write_text("floati live worker proof\n", encoding="utf-8")
                    observed_child = start_observed_descendant(workspace)
                elif args.mode == "complete-many":
                    for index in range(33):
                        (workspace / f"proof-{index:02d}.txt").write_text(
                            f"proof {index}\n", encoding="utf-8"
                        )
                elif args.mode == "complete-pathspec":
                    (workspace / ":(exclude)PROOF.txt").write_text(
                        "literal pathspec proof\n", encoding="utf-8"
                    )
                elif args.mode == "complete-replace-git":
                    (workspace / "PROOF.txt").write_text(
                        "replaced git metadata proof\n", encoding="utf-8"
                    )
                    replaced = workspace / ".floati" / "replaced-git"
                    shutil.move(str(workspace / ".git"), replaced)
                    (workspace / ".git").symlink_to(replaced, target_is_directory=True)
                elif args.mode == "complete-symlink":
                    (workspace / "ESCAPE").symlink_to("\x2fprivate/tmp")
                final_status = "completed"
            else:
                final_status = "failed"
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread["id"],
                        "turn": {
                            "id": turn["id"],
                            "items": [],
                            "status": final_status,
                        },
                    },
                }
            )

    with log_path.open("w", encoding="utf-8") as handle:
        for message in received:
            handle.write(json.dumps(message, separators=(",", ":")) + "\n")
    try:
        while read_message():
            pass
    except EOFError:
        if observed_child is not None:
            # Only this new mode owns this handle. Closing the real session
            # releases and reaps its child before the app-server itself exits.
            collect_observed_descendant(observed_child, workspace)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
