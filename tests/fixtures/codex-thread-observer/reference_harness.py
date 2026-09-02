#!/usr/bin/env python3
"""Deterministic JSON-line app-server harness for Thread Observer tests only."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path


MODE, METHODS_PATH, PARAMS_PATH, DIAGNOSTIC_PATH = sys.argv[1:5]
THREAD_ID = "018f3a2b-4c5d-7e8f-9a0b-1c2d3e4f5678"


def emit(value: object) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def record_method(message: dict) -> None:
    method = message.get("method")
    if isinstance(method, str):
        with Path(METHODS_PATH).open("a", encoding="utf-8") as stream:
            stream.write(method + "\n")


def receive() -> dict:
    line = sys.stdin.buffer.readline()
    if not line:
        raise SystemExit(0)
    value = json.loads(line)
    if not isinstance(value, dict):
        raise SystemExit(91)
    record_method(value)
    return value


def diagnostic() -> None:
    fds = {}
    try:
        for name in os.listdir("/dev/fd"):
            if name.isdigit():
                try:
                    fds[name] = os.readlink("/dev/fd/" + name)
                except OSError:
                    pass
    except OSError:
        pass
    Path(DIAGNOSTIC_PATH).write_text(
        json.dumps(
            {
                "cwd": os.getcwd(),
                "environment": dict(os.environ),
                "fds": fds,
                "initialize": initialize,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


initialize = receive()
emit({"id": 1.0 if MODE == "float-response-id" else initialize.get("id"), "result": {}})
initialized = receive()
request = receive()
Path(PARAMS_PATH).write_text(
    json.dumps(request.get("params"), sort_keys=True, separators=(",", ":")),
    encoding="utf-8",
)
diagnostic()

if MODE == "crash":
    os._exit(92)
if MODE == "hang":
    while True:
        time.sleep(10)
if MODE == "malformed":
    sys.stdout.write("{malformed\n")
    sys.stdout.flush()
    raise SystemExit(0)
if MODE == "oversized":
    sys.stdout.write("{" + "x" * 1_048_577 + "\n")
    sys.stdout.flush()
    raise SystemExit(0)
if MODE == "partial":
    sys.stdout.write('{"id":2')
    sys.stdout.flush()
    raise SystemExit(0)
if MODE == "server-request":
    emit({"id": 99, "method": "turn/start", "params": {}})
    while True:
        time.sleep(10)
if MODE == "malformed-method":
    emit({"method": [], "params": {}})
    while True:
        time.sleep(10)
if MODE == "unknown-notification":
    emit({"method": "unknown/notification", "params": {}})
    while True:
        time.sleep(10)
if MODE == "missing":
    emit({"id": request.get("id"), "error": {"code": -32602, "message": "thread not found"}})
    raise SystemExit(0)
if MODE == "null-error":
    emit({"id": request.get("id"), "error": None})
    raise SystemExit(0)
if MODE in {"provider-error-data", "missing-data"}:
    emit(
        {
            "id": request.get("id"),
            "error": {
                "code": -32602 if MODE == "missing-data" else -32000,
                "message": "thread not found" if MODE == "missing-data" else "provider failed",
                "data": {"detail": "HOSTILE_ERROR_DATA"},
            },
        }
    )
    raise SystemExit(0)

statuses = {
    "idle": ("idle", []),
    "not-loaded": ("notLoaded", []),
    "system-error": ("systemError", []),
    "active-approval": ("active", ["waitingOnApproval"]),
    "active-input": ("active", ["waitingOnUserInput"]),
    "active-both": ("active", ["waitingOnApproval", "waitingOnUserInput"]),
    "duplicate": ("idle", []),
    "trailing": ("idle", []),
    "nonempty-turns": ("idle", []),
    "wrong-thread": ("idle", []),
    "ignore-term-child": ("idle", []),
    "notification": ("idle", []),
    "wrong-response-id": ("idle", []),
    "response-extra": ("idle", []),
    "duplicate-root-key": ("idle", []),
    "updated-float": ("idle", []),
    "extra-status": ("idle", []),
    "extra-flag": ("active", ["unknownFlag"]),
    "float-response-id": ("idle", []),
    "active-reversed": ("active", ["waitingOnUserInput", "waitingOnApproval"]),
}
if MODE == "notification":
    emit({"method": "thread/status/changed", "params": {"threadId": THREAD_ID}})
status, flags = statuses.get(MODE, ("unrecognized", []))
thread_id = "018f3a2b-4c5d-7e8f-9a0b-1c2d3e4f5679" if MODE == "wrong-thread" else THREAD_ID
turns = [{"id": "HOSTILE_TURN", "text": "session-secret"}] if MODE == "nonempty-turns" else []
status_value = {"type": status}
if status == "active":
    status_value["activeFlags"] = flags
if MODE == "extra-status":
    status_value["future"] = True
result = {
    "thread": {
        "id": thread_id,
        "status": status_value,
        "updatedAt": 1786622400.0 if MODE == "updated-float" else 1786622400,
        "turns": turns,
        "title": "HOSTILE_TITLE",
        "preview": "HOSTILE_PREVIEW",
        "cwd": "/private/hostile",
        "model": "model-secret",
    }
}
if MODE == "ignore-term-child":
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(10)
    Path(DIAGNOSTIC_PATH + ".descendant").write_text(str(child), encoding="ascii")

response = {
    "id": 99 if MODE == "wrong-response-id" else request.get("id"),
    "result": result,
}
if MODE == "response-extra":
    response["future"] = True
if MODE == "duplicate-root-key":
    sys.stdout.write(
        '{"id":2,"id":2,"result":'
        + json.dumps(result, sort_keys=True, separators=(",", ":"))
        + "}\n"
    )
    sys.stdout.flush()
elif MODE in {"duplicate", "trailing"}:
    # CI-GREEN-24: ONE write, ONE flush. These two modes used to emit the
    # lawful response and then, in a SECOND flush, the thing that makes the
    # stream unlawful. Between the two flushes the observer could read a
    # complete, well-formed response and correctly return `observed` - the
    # observer was right and the fixture was racing it. Reproduced by putting
    # a 60 ms sleep in that gap: 8 of 8 rounds returned
    # ('observed', 'exact_thread_read') in BOTH modes, and on a runner
    # ordinary scheduling supplied the gap for `trailing` unaided. Emitting
    # the unlawful suffix in the same write makes it present the instant the
    # response is readable, so the mode tests what it names.
    suffix = (
        json.dumps(
            {"id": request.get("id"), "result": result},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        if MODE == "duplicate"
        else "trailing-byte"
    )
    sys.stdout.write(
        json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n" + suffix
    )
    sys.stdout.flush()
else:
    emit(response)

if MODE in {"ignore-term-child", "duplicate", "trailing"}:
    while True:
        time.sleep(10)
