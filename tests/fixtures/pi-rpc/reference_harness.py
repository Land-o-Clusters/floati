from __future__ import annotations

import json
import sys
import time
from pathlib import Path


MODE = sys.argv[1]
WORKSPACE = Path.cwd()
EVIDENCE = WORKSPACE / ".floati"
RAW_REQUESTS = EVIDENCE / "pi-requests.raw"


def emit(payload: object) -> None:
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


for raw in sys.stdin.buffer:
    with RAW_REQUESTS.open("ab") as handle:
        handle.write(raw)
    message = json.loads(raw.rstrip(b"\r\n").decode("utf-8"))
    if MODE == "timeout":
        time.sleep(2)
        continue
    if MODE == "malformed":
        sys.stdout.buffer.write(b"{not-json}\n")
        sys.stdout.buffer.flush()
        continue
    if MODE == "unicode":
        emit({"type": "event", "message": "payload\u2028separator"})
    if message.get("type") != "prompt":
        continue
    proof = WORKSPACE / "PI-PROOF.txt"
    proof.write_text("FLOATI pi fixture proof\n", encoding="utf-8")
    emit({"id": message.get("id"), "type": "response", "command": "prompt", "success": True})
    if MODE == "interleaved":
        emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "ok"}})
    emit({"type": "agent_end"})
