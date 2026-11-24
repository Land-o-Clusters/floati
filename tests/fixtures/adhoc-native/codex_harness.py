"""Independent persistent app-server fixture. No provider code is imported."""
import json
import os
from pathlib import Path
import sys

store = Path(sys.argv[1])
mode = sys.argv[2]
store.mkdir(exist_ok=True)
with (store / "launches.jsonl").open("a") as log:
    log.write(json.dumps({"pid": os.getpid(), "cwd": os.getcwd(), "home": os.environ.get("CODEX_HOME")}) + "\n")
state_path = store / "state.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {"creations": 0, "turns": 0}
session_id = "019a17f0-0000-7000-8000-000000000001"

def emit(value):
    print(json.dumps(value), flush=True)

for raw in sys.stdin:
    request = json.loads(raw)
    with (store / "requests.jsonl").open("a") as log:
        log.write(json.dumps(request) + "\n")
    method = request.get("method")
    params = request.get("params", {})
    if method == "initialize":
        emit({"id": request["id"], "result": {}})
    elif method in {"thread/start", "thread/resume"}:
        if method == "thread/start":
            state["creations"] += 1
            state["id"] = session_id
        elif state.get("id") != params.get("threadId"):
            emit({"id": request["id"], "error": {"code": 1}})
            continue
        state_path.write_text(json.dumps(state))
        thread = {"id": session_id, "cwd": os.getcwd(), "ephemeral": False}
        if mode == "substitute" and method == "thread/resume":
            thread["id"] = "019a17f0-0000-7000-8000-000000000002"
        if mode == "wrong-cwd":
            thread["cwd"] = str(store)
        if mode == "ephemeral":
            thread["ephemeral"] = True
        emit({"id": request["id"], "result": {"thread": thread}})
    elif method == "turn/start":
        state["turns"] += 1
        state_path.write_text(json.dumps(state))
        if not (store / "opened-ack").exists():
            (store / "prompt-before-ack").write_text("unsafe")
        Path("PROOF.txt").write_text(str(state["turns"]))
        emit({"id": request["id"], "result": {"turn": {"id": "turn-" + str(state["turns"])}}})
        if mode in {"approval", "input"}:
            method_name = "item/commandExecution/requestApproval" if mode == "approval" else "item/tool/requestUserInput"
            emit({"id": 901, "method": method_name, "params": {}})
        else:
            turn_id = "turn-wrong" if mode == "wrong-turn" else "turn-" + str(state["turns"])
            if mode.startswith("usage"):
                for total in (100, 140, 140):
                    emit({"method": "thread/tokenUsage/updated", "params": {
                        "threadId": session_id if mode != "usage-foreign" else "foreign",
                        "turnId": turn_id,
                        "tokenUsage": {"total": {"inputTokens": total,
                            "outputTokens": True if mode == "usage-invalid" else 20,
                            "cachedInputTokens": 40, "reasoningOutputTokens": 5,
                            "totalTokens": total + 20}}}})
            emit({"method": "turn/completed", "params": {"threadId": session_id,
                 "turn": {"id": turn_id, "status": "completed"}}})
            if mode == "tail-overflow":
                os.write(2, b"x" * 1_048_577)
    elif method == "turn/interrupt":
        emit({"id": request["id"], "result": {}})
