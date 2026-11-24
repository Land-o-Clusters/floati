"""Scratch child for shared transport tests; never starts a provider binary."""
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import threading
import time


def write(fd, data):
    while data:
        data = data[os.write(fd, data):]


def line(value):
    write(1, json.dumps(value, separators=(",", ":")).encode() + b"\n")


def main():
    mode = sys.argv[1]
    if mode == "silent":
        time.sleep(10)
    elif mode == "stdout-over":
        write(1, b"x" * 1_048_577)
    elif mode == "stderr-over":
        write(2, b"x" * 1_048_577)
    elif mode == "simultaneous":
        thread = threading.Thread(target=write, args=(2, b"e" * 1_048_576))
        thread.start()
        write(1, b" " * 1_048_574 + b"{}")
        thread.join()
    elif mode == "frame-over":
        write(1, b'{"x":"' + b"x" * 65_536 + b'"}\n')
    elif mode == "depth-over":
        write(1, b'{"x":' + b"[" * 64 + b"0" + b"]" * 64 + b"}\n")
    elif mode == "depth-exact":
        write(1, b'{"x":' + b"[" * 63 + b"0" + b"]" * 63 + b"}\n")
    elif mode == "frame-exact":
        write(1, b'{"x":"' + b"x" * 65_528 + b'"}\n')
    elif mode == "invalid-utf8":
        write(1, b'{"x":"\xff"}\n')
    elif mode == "duplicate-key":
        write(1, b'{"x":1,"x":2}\n')
    elif mode == "non-finite":
        write(1, b'{"x":NaN}\n')
    elif mode == "partial-eof":
        write(1, b'{"x":')
    elif mode == "records-over":
        write(1, b'{"ignored":true}\n' * 4097)
    elif mode == "records-exact":
        write(1, b'{}\n' * 4096)
    elif mode == "ignored-over":
        for _ in range(100):
            line({"ignored": "x" * 11_000})
    elif mode == "terminal-stderr-over":
        line({"terminal": True})
        write(2, b"e" * 1_048_577)
    elif mode == "terminal-malformed":
        line({"terminal": True})
        write(1, b'{"x":"\xff"}\n')
    elif mode == "two-terminal":
        write(1, b'{"terminal":true}\n{"terminal":true}\n')
    elif mode == "digest":
        write(2, b"diagnostic bytes only\n")
        write(1, b'{"ok":true}\n')
    elif mode == "terminal-large":
        write(1, json.dumps({"value": "x" * 70_000}).encode())
    elif mode == "inherited-pipe":
        # Preserve the owner's pre-spawn deadline as fixture data; this negative
        # child deliberately ignores it so the owner must enforce the drain bound.
        deadline = sys.argv[sys.argv.index("--fixture-deadline") + 1]
        child = subprocess.Popen([sys.executable, "-I", "-B", __file__, "silent",
                                  "--fixture-deadline", deadline],
                                 stdin=subprocess.DEVNULL)
        Path(sys.argv[2]).write_text(str(child.pid))
        line({"terminal": True})
        # Child intentionally outlives this leader with stdout/stderr inherited.
    elif mode == "opened":
        # The production emitter owns ACK validation and the absolute deadline.
        # Explicit repo argument is fixture-only, never a provider/dispatch input.
        sys.path.insert(0, sys.argv[2])
        from floati.native_thread_protocol import NativeThreadEmitter
        from floati.worker_errors import WorkerAdapterFailure
        from floati.worker_bootstrap_protocol import BootstrapChannel
        from tests.readiness import Readiness
        config = json.loads(sys.stdin.buffer.readline())
        channel = BootstrapChannel(int(sys.argv[3]))
        try:
            # Fixture clock translation only: macOS Python 3.9 monotonic origins
            # differ between processes. The parent owns the pre-spawn budget;
            # ACK wait and final drain must not reset that original deadline.
            child_sample = time.monotonic()
            handshake_deadline = child_sample + config["handshake_budget"]
            readiness = Readiness("native fixture clock ACK", timeout=config["handshake_budget"])
            try:
                channel.send(("fixture_clock", {"schema_version": 1, "monotonic": child_sample}),
                             deadline=handshake_deadline)
                verb, clock_ack = channel.recv(timeout=readiness.remaining())
            except WorkerAdapterFailure as error:
                raise readiness.failure() from error
            if (verb != "fixture_clock_ack" or type(clock_ack) is not dict
                or set(clock_ack) != {"schema_version", "child_deadline"}
                or clock_ack["schema_version"] != 1):
                raise ValueError("fixture clock ACK has invalid fields")
            if config.get("saturate_channel"):
                duplicate = socket.socket(fileno=os.dup(int(sys.argv[3])))
                try:
                    duplicate.setblocking(False)
                    filled = 0
                    while True:
                        try:
                            filled += duplicate.send(b"x" * 4096)
                        except BlockingIOError:
                            break
                        if filled > 1_048_576:
                            raise ValueError("fixture socket did not backpressure within bound")
                finally:
                    duplicate.setblocking(True)
                    duplicate.close()
            emitter = NativeThreadEmitter(config["context"], channel=channel,
                                          deadline=clock_ack["child_deadline"])
            emitter.opened(config["coordinate"])
            line({"prompt_sent": True})
            if config.get("hold_after_prompt"):
                time.sleep(10)
        except WorkerAdapterFailure as failure:
            # Only the actual typed emitter failure gets this observation.
            observation = {"fixture_failure": {"type": "WorkerAdapterFailure", "code": failure.code}}
            if config.get("saturate_channel"):
                observation["transport_prefilled"] = True
            raw = json.dumps(observation, separators=(",", ":")).encode() + b"\n"
            if len(raw) > 512:
                raise ValueError("fixture failure observation exceeded bound")
            write(2, raw)
            return 42
        finally:
            channel.close()
    else:
        raise ValueError("unknown fixture mode")


if __name__ == "__main__":
    sys.exit(main() or 0)
