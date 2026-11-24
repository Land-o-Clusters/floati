"""Bounded binary transport for an already-owned native provider process.

This reader never terminates, reaps on failure, or closes the owner's process
pipes. The process owner retains that responsibility, including descendants.
Only counts and digests escape through the evidence API.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import select
import subprocess
import time

from ..worker_bootstrap_protocol import NATIVE_THREAD_BOUNDS
from ..worker_errors import WorkerAdapterFailure


class BoundedNativeIO:
    """Read strict JSONL or one JSON document under one absolute deadline."""

    _READ_BYTES = 8192

    def __init__(self, process, *, deadline, bounds):
        if (type(bounds) is not dict or bounds != NATIVE_THREAD_BOUNDS
                or any(type(value) is not int for value in bounds.values())):
            raise WorkerAdapterFailure("protocol_error")
        if type(deadline) not in (int, float):
            raise WorkerAdapterFailure("protocol_error")
        try:
            finite_deadline = math.isfinite(deadline)
        except (OverflowError, ValueError):
            finite_deadline = False
        if not finite_deadline:
            raise WorkerAdapterFailure("protocol_error")
        if deadline <= time.monotonic():
            raise WorkerAdapterFailure("process_timeout")
        if process.stdout is None or process.stderr is None:
            raise WorkerAdapterFailure("protocol_error")

        self._process = process
        self._deadline = deadline
        self._bounds = dict(bounds)
        self._streams = {process.stdout.fileno(): "stdout",
                         process.stderr.fileno(): "stderr"}
        self._stdout_fd = process.stdout.fileno()
        self._counts = {"stdout": 0, "stderr": 0}
        self._hashes = {"stdout": hashlib.sha256(), "stderr": hashlib.sha256()}
        self._buffer = bytearray()
        self._records = 0
        self._mode = None
        self._terminal_read = False
        self._failure = None

    def _refuse(self, code):
        if self._failure is None:
            self._failure = WorkerAdapterFailure(code)
        self._buffer.clear()
        raise self._failure

    def _remaining(self):
        if self._failure is not None:
            raise self._failure
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self._refuse("process_timeout")
        return remaining

    def _set_mode(self, mode):
        self._remaining()
        if self._mode is not None and self._mode != mode:
            self._refuse("protocol_error")
        self._mode = mode

    def _pump(self):
        """Consume one bounded chunk from each ready pipe, never waiting on one."""
        remaining = self._remaining()
        if not self._streams:
            return
        try:
            ready, _, _ = select.select(list(self._streams), [], [], remaining)
        except InterruptedError:
            return
        except (OSError, ValueError):
            self._refuse("protocol_error")
        if not ready:
            self._refuse("process_timeout")
        for descriptor in ready:
            self._remaining()
            try:
                chunk = os.read(descriptor, self._READ_BYTES)
            except InterruptedError:
                continue
            except OSError:
                self._refuse("protocol_error")
            if not chunk:
                del self._streams[descriptor]
                continue
            stream = self._streams[descriptor]
            # Observe bytes before any decoding, parsing, or transcript retention.
            self._counts[stream] += len(chunk)
            self._hashes[stream].update(chunk)
            if (self._counts[stream] > self._bounds[stream + "_bytes"]
                    or sum(self._counts.values()) > self._bounds["combined_bytes"]):
                self._refuse("native_output_limit")
            if stream == "stdout":
                self._buffer.extend(chunk)

    def _decode(self, data):
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            self._refuse("protocol_error")

        # Count containers outside strings before invoking the recursive decoder.
        # Invalid bracket matching is left to the strict JSON decoder below.
        depth = 0
        quoted = False
        escaped = False
        for character in text:
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in "[{":
                depth += 1
                if depth > self._bounds["json_depth"]:
                    self._refuse("native_protocol_limit")
            elif character in "]}":
                depth -= 1

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        def reject_constant(value):
            raise ValueError("non-finite JSON number")

        def finite_float(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("non-finite JSON number")
            return number

        try:
            value = json.loads(text, object_pairs_hook=unique_object,
                               parse_constant=reject_constant,
                               parse_float=finite_float)
        except (ValueError, RecursionError):
            self._refuse("protocol_error")
        return text, value

    def _count_record(self):
        self._records += 1
        if self._records > self._bounds["protocol_records"]:
            self._refuse("native_protocol_limit")

    def read_record(self):
        """Return the next JSON object, or None after clean stdout EOF."""
        self._set_mode("jsonl")
        while True:
            self._remaining()
            newline = self._buffer.find(b"\n")
            frame_length = len(self._buffer) if newline < 0 else newline
            if frame_length > self._bounds["jsonl_frame_bytes"]:
                self._refuse("native_protocol_limit")
            if newline >= 0:
                frame = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                _, record = self._decode(frame)
                # The protocol carries objects; None is reserved for clean EOF.
                if type(record) is not dict:
                    self._refuse("protocol_error")
                self._count_record()
                return record
            if self._stdout_fd not in self._streams:
                if self._buffer:
                    self._refuse("protocol_error")
                return None
            self._pump()

    def read_terminal_json(self):
        """Validate and return the original bytes of one stdout JSON document."""
        self._set_mode("terminal")
        if self._terminal_read:
            self._refuse("protocol_error")
        while self._stdout_fd in self._streams:
            self._pump()
        document = bytes(self._buffer)
        self._decode(document)
        self._buffer.clear()
        self._count_record()
        self._terminal_read = True
        return document

    def drain_to_exit(self, on_record=None):
        """Validate the tail, deliver JSONL records, and observe the owned exit."""
        self._remaining()
        if self._mode in (None, "jsonl"):
            while True:
                record = self.read_record()
                if record is None:
                    break
                if on_record is not None:
                    try:
                        on_record(record)
                    except WorkerAdapterFailure as failure:
                        self._failure = failure
                        self._buffer.clear()
                        raise
        elif not self._terminal_read:
            self.read_terminal_json()
        while self._streams:
            self._pump()
        try:
            self._process.wait(timeout=self._remaining())
        except subprocess.TimeoutExpired:
            self._refuse("process_timeout")
        return self.snapshot()

    def snapshot(self):
        """Return observed metadata, including after a typed transport failure."""
        return {"stdout_bytes": self._counts["stdout"],
                "stderr_bytes": self._counts["stderr"],
                "combined_bytes": sum(self._counts.values()),
                "protocol_records": self._records,
                "stdout_sha256": self._hashes["stdout"].hexdigest(),
                "stderr_sha256": self._hashes["stderr"].hexdigest(),
                "exit_code": self._process.poll()}
