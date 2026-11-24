"""No provider calls: fake Pi reproducing persistent allocation and RPC hazards."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid

parser = argparse.ArgumentParser()
parser.add_argument("--fixture-mode", required=True)
parser.add_argument("--fixture-store", required=True)
parser.add_argument("--mode", required=True)
parser.add_argument("--session-dir", required=True)
parser.add_argument("--session")
parser.add_argument("--no-tools", action="store_true")
parser.add_argument("--tools")
for flag in ("--no-extensions", "--no-skills", "--no-prompt-templates",
             "--no-themes", "--no-context-files"):
    parser.add_argument(flag, action="store_true", required=True)
args = parser.parse_args()
if args.mode != "rpc" or not (args.no_tools ^ bool(args.tools)):
    sys.exit(70)
if args.tools is not None and args.tools.split(",") != ["read", "write", "edit"]:
    sys.exit(71)
slot = Path(args.session_dir).resolve()
store = Path(args.fixture_store)
launch = {"pid": os.getpid(), "home": os.environ["HOME"], "cwd": os.getcwd(),
          "session_dir": str(slot), "resume": args.session, "argv": sys.argv[1:]}
with (store / "launches.jsonl").open("a") as stream:
    stream.write(json.dumps(launch) + "\n")
prior_file = store / "state.json"
prior = json.loads(prior_file.read_text()) if prior_file.exists() else {"creations": 0, "turns": 0}
if args.session:
    session_file = Path(args.session)
    # Reproduce upstream: missing/empty explicit files allocate a replacement.
    if session_file.exists() and session_file.stat().st_size:
        header = json.loads(session_file.read_text().splitlines()[0])
        session_id = header["id"]
    else:
        session_id = str(uuid.uuid4())
        prior["creations"] += 1
        (store / "replacement-created").write_text(session_id)
else:
    session_id = str(uuid.uuid4())
    session_file = slot / (session_id + ".jsonl")
    prior["creations"] += 1
prior_file.write_text(json.dumps(prior))
settled = False
busy = False
startup_emitted = False


def output(record):
    print(json.dumps(record), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    with (store / "requests.jsonl").open("a") as stream:
        stream.write(json.dumps(request) + "\n")
    command = request.get("type")
    if command == "abort":
        output({"type": "response", "command": "abort", "id": request.get("id"), "success": True})
        continue
    if command == "get_state":
        if args.fixture_mode in {"usage-startup", "usage-startup-missing"} and not startup_emitted:
            startup = {"role": "assistant", "content": [], "stopReason": "stop"}
            if args.fixture_mode == "usage-startup":
                startup["usage"] = {"input": 900, "output": 90}
            output({"type": "message_end", "message": startup})
            startup_emitted = True
        state = {"sessionId": session_id, "sessionFile": str(session_file),
                 "isStreaming": busy, "isCompacting": False,
                 "pendingMessageCount": 0, "messageCount": prior["turns"] * 2}
        if args.fixture_mode == "substitute":
            state["sessionId"] = str(uuid.uuid4())
        if args.fixture_mode == "wrong-path":
            state["sessionFile"] = str(slot / "foreign.jsonl")
        if args.fixture_mode == "busy":
            state["isStreaming"] = True
        if args.fixture_mode == "compacting":
            state["isCompacting"] = True
        if args.fixture_mode == "queued":
            state["pendingMessageCount"] = 1
        if settled:
            (store / "settled-state-checked").write_text("yes")
            if args.fixture_mode == "settled-busy":
                state["pendingMessageCount"] = 1
        output({"type": "response", "command": command, "id": request["id"],
                "success": True, "data": state})
        continue
    if command != "prompt":
        sys.exit(72)
    if not (store / "opened-ack").exists():
        (store / "prompt-before-ack").write_text("yes")
    prior["turns"] += 1
    prior_file.write_text(json.dumps(prior))
    Path("PROOF.txt").write_text(str(prior["turns"]))
    if args.fixture_mode != "unmaterialized":
        session_file.write_text(json.dumps({"type": "session", "version": 3,
            "id": session_id, "cwd": os.getcwd()}) + "\n")
        if args.fixture_mode != "upstream-file-mode":
            session_file.chmod(0o600)
    output({"type": "response", "command": command, "id": request["id"], "success": True})
    if args.fixture_mode == "extension-ui":
        output({"type": "extension_ui_request", "id": "ui-1", "method": "confirm",
                "title": "Unexpected extension approval"})
        continue
    if args.fixture_mode in {"assistant-error", "assistant-error-retry", "retry-exhausted"}:
        error = {"role": "assistant", "content": [], "stopReason": "error",
                 "errorMessage": "503 service unavailable"}
        output({"type": "message_end", "message": error})
        output({"type": "turn_end", "message": error, "toolResults": []})
        retry = args.fixture_mode != "assistant-error"
        output({"type": "agent_end", "messages": [error], "willRetry": retry})
        if retry:
            output({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 1,
                    "delayMs": 0, "errorMessage": error["errorMessage"]})
            output({"type": "agent_start"})
            if args.fixture_mode == "assistant-error-retry":
                final = {"role": "assistant", "content": [{"type": "text", "text": "Recovered"}],
                         "stopReason": "stop"}
                output({"type": "message_end", "message": final})
                output({"type": "auto_retry_end", "success": True, "attempt": 1})
                Path("RETRY.txt").write_text("finished continuation")
            else:
                final = error
                output({"type": "message_end", "message": final})
            output({"type": "turn_end", "message": final, "toolResults": []})
            output({"type": "agent_end", "messages": [final], "willRetry": False})
            if args.fixture_mode == "retry-exhausted":
                output({"type": "auto_retry_end", "success": False, "attempt": 1,
                        "finalError": error["errorMessage"]})
        settled = True
        output({"type": "agent_settled"})
        continue
    if args.fixture_mode.startswith("usage"):
        for count in (100, 40):
            message = {"role": "assistant", "content": [], "stopReason": "stop",
                "usage": {"input": count, "output": 10, "cacheRead": 20, "cacheWrite": 5}}
            if args.fixture_mode == "usage-invalid":
                message["usage"]["input"] = "100"
            output({"type": "message_end", "message": message})
        # Repeated terminal summaries must not be counted as new model calls.
        output({"type": "agent_end", "messages": [message]})
    else:
        output({"type": "agent_end", "messages": []})
    if args.fixture_mode == "retry":
        output({"type": "auto_retry_start", "attempt": 1})
        output({"type": "agent_start"})
        Path("RETRY.txt").write_text("finished continuation")
        output({"type": "agent_end", "messages": []})
    output({"type": "turn_end"})
    settled = True
    output({"type": "agent_settled"})
    if args.fixture_mode == "tail-overflow":
        sys.stderr.write("x" * (1048576 + 1))
        sys.stderr.flush()
