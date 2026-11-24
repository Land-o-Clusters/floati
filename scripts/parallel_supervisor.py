"""External qualification gate for the fixed, separately collected payload.

Only the payload runs the native suites. This process observes its two private
protocol pipes and retained Popen handle, never a PID census or process tree.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time


_FRAME_LIMIT = 65536
_SAFETY_SECONDS = 7200.0
_PARALLEL_WALL_SECONDS = 600.0
_BOOTSTRAP = r'''
import importlib.util, os, sys
root, output, transmit, acknowledge = sys.argv[1:]
transmit, acknowledge = int(transmit), int(acknowledge)
os.set_inheritable(transmit, False); os.set_inheritable(acknowledge, False)
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
helper = load('fixed_qualification_payload', root + '/scripts/parallel_qualification.py')
supervisor = load('fixed_qualification_supervisor', root + '/scripts/parallel_supervisor.py')
sys.exit(supervisor._payload(helper.__dict__, root, output, transmit, acknowledge))
'''


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate supervisor protocol field")
        result[key] = value
    return result


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("supervisor interval is not finite nonnegative seconds")
    return value


def _elapsed(clock, began):
    return _seconds(clock() - began)


def _send(descriptor, frame):
    raw = (json.dumps(frame, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > _FRAME_LIMIT:
        raise ValueError("supervisor protocol frame exceeds bound")
    view = memoryview(raw)
    while view:
        count = os.write(descriptor, view)
        if count <= 0:
            raise ValueError("supervisor protocol write made no progress")
        view = view[count:]


def _payload(engine, root, output, transmit, acknowledge):
    """Internal fresh-process entry; no caller-selectable profile or code path."""
    native_seconds = post_seconds = publication_seconds = 0.0
    phase = "preparation"
    post_began = None
    source = engine["_launcher"](Path(root)).source_identity(Path(root))
    registry = engine["_read_registry"]()[1]
    base = {"protocol_version": 1, "pid": os.getpid(), "source": source,
            "registry": registry}
    _send(transmit, dict(base, event="started"))

    def parallel_start():
        nonlocal phase
        if phase != "preparation":
            raise ValueError("duplicate/out-of-order parallel start")
        _send(transmit, dict(base, event="ready"))
        # Four bytes cannot hold an unbounded caller message. Both descriptors
        # are non-inheritable before any dependency or native child launches.
        ack = b""
        while len(ack) < 4:
            part = os.read(acknowledge, 4 - len(ack))
            if not part:
                raise ValueError("supervisor acknowledgment pipe closed")
            ack += part
        if ack != b"ACK\n":
            raise ValueError("invalid supervisor acknowledgment")
        phase = "parallel"

    def parallel_collected(seconds):
        nonlocal native_seconds, post_began, phase
        if phase != "parallel":
            raise ValueError("out-of-order parallel collection")
        native_seconds = _seconds(seconds)
        post_began = time.monotonic()
        phase = "validation"

    def post_validation_complete():
        nonlocal post_seconds, phase
        if phase == "validation":
            post_seconds = _elapsed(time.monotonic, post_began)
            phase = "publication"
        elif phase not in ("preparation", "parallel"):
            raise ValueError("duplicate/out-of-order post-validation hook")

    published_binding = None

    def published(path, sha256, byte_count, seconds):
        nonlocal publication_seconds, published_binding
        if published_binding is not None:
            raise ValueError("duplicate payload publication")
        publication_seconds = _seconds(seconds)
        published_binding = {"path": str(path), "sha256": sha256, "bytes": byte_count}

    hooks = {"parallel_start": parallel_start, "parallel_collected": parallel_collected,
             "post_validation_complete": post_validation_complete, "published": published}
    try:
        receipt = engine["run_pair"](root, output, _supervision=hooks)
        if published_binding is None:
            raise ValueError("payload omitted its publication commitment")
        _send(transmit, dict(base, event="final", payload_artifact=published_binding,
                            status=receipt["status"],
                            timing={"parallel_native_seconds": native_seconds,
                                    "payload_post_validation_seconds": post_seconds,
                                    "payload_publication_seconds": publication_seconds}))
        return (0 if receipt.get("successful") is True
                and receipt.get("status") == "awaiting_external_gate"
                and receipt.get("qualified") is False else receipt.get("exit_code", 1))
    except BaseException as exc:
        _send(transmit, dict(base, event="failure", error=type(exc).__name__ + ": " + str(exc)[:1024]))
        return 1
    finally:
        os.close(transmit)
        os.close(acknowledge)


def _artifact(engine, output, binding):
    if (type(binding) is not dict or set(binding) != {"path", "sha256", "bytes"}
            or binding["path"] != str(output / "qualification.json")
            or type(binding["bytes"]) is not int or not 0 < binding["bytes"] <= 16777216
            or type(binding["sha256"]) is not str or len(binding["sha256"]) != 64):
        raise ValueError("payload artifact commitment has invalid shape")
    path = engine["_ordinary"](binding["path"])
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if before.st_uid != os.getuid() or before.st_nlink != 1 or before.st_size != binding["bytes"]:
            raise ValueError("payload artifact is unowned/shared or changed size")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(16777217)
        signature = engine["_signature"]
        if (signature(before) != signature(os.fstat(descriptor))
                or signature(before) != signature(path.lstat())
                or len(raw) != binding["bytes"] or hashlib.sha256(raw).hexdigest() != binding["sha256"]):
            raise ValueError("payload artifact differs from its publication commitment")
        value = json.loads(raw, object_pairs_hook=_unique)
        if type(value) is not dict:
            raise ValueError("payload artifact is not an object")
        return value
    finally:
        os.close(descriptor)


def supervise_pair(root, output, *, engine, clock):
    """Adjudicate the collected payload against an external 600-second gate."""
    startup_began = clock()
    profile, registry = engine["_read_registry"]()
    repository = engine["validate_repository"](root, profile)
    root = Path(repository["workspace_root"])
    if engine["_git"](root, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("qualification requires clean committed source")
    output = engine["validate_output"](root, output)
    # Admit exact sibling sources now; source_identity commits their bytes.
    engine["_ordinary"](root / "scripts/parallel_qualification.py")
    engine["_ordinary"](root / "scripts/parallel_supervisor.py")
    launcher = engine["_launcher"](root)
    source = launcher.source_identity(root)
    platform_temp = engine['platform_temp_root']()
    scratch = Path(tempfile.mkdtemp(prefix="qualification-supervisor-", dir=platform_temp)).resolve()
    home, temporary = scratch / "home", scratch / "tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    environment = engine["make_environment"](profile, os.environ, home, temporary)
    receive, transmit = os.pipe()
    acknowledge, ack_write = os.pipe()
    owner = engine["_Cancellation"]()
    process = None
    began = ended = None
    startup_seconds = 0.0
    prepared_began = None
    final = None
    protocol_error = None
    operator_cancelled = False
    collected = False
    payload_cleanup = False
    receipt = {"status": "invalid", "successful": False, "qualified": False,
               "profile": "floati-harbor", "registry": registry, "source": source,
               "repository": repository, "parallel_limit_seconds": 600.0,
               "protocol_safety_seconds": _SAFETY_SECONDS,
               "timing_scope": {"publication_to_payload_exit_seconds":
                   "residual: handshake plus post-publication payload exit and parent collection; not an exit timestamp",
                   "excluded": ["serial_and_dependency_preflight", "supervisor_startup", "observer_publication"]}}
    argv = [profile["interpreter"], "-E", "-s", "-B", "-c", _BOOTSTRAP,
            str(root), str(output), str(transmit), str(acknowledge)]
    receipt["payload_launch"] = {"argv": argv, "environment": environment,
                                 "protocol_fds_inherited_only_by_payload": [transmit, acknowledge]}
    safety_end = time.monotonic() + _SAFETY_SECONDS
    parallel_deadline = None
    with owner.handlers():
        try:
            with (scratch / "payload.stdout").open("xb") as stdout, (scratch / "payload.stderr").open("xb") as stderr:
                # Adopt before __init__: even a post-spawn construction failure
                # leaves the exact object available for signaling/collection.
                process = subprocess.Popen.__new__(subprocess.Popen)
                owner.process, owner.forwarded = process, False
                try:
                    process.__init__(argv, cwd=root, env=environment, stdout=stdout, stderr=stderr,
                                     close_fds=True, pass_fds=(transmit, acknowledge), start_new_session=True)
                    receipt["payload_pid"] = process.pid
                    os.close(transmit)
                    transmit = None
                    os.close(acknowledge)
                    acknowledge = None
                    owner.forward()
                    buffer = b""
                    events = []
                    while True:
                        if owner.number is not None:
                            raise ValueError("supervisor cancelled")
                        if time.monotonic() >= safety_end:
                            raise TimeoutError("supervisor_protocol_safety_deadline")
                        if parallel_deadline is not None and time.monotonic() >= parallel_deadline:
                            raise TimeoutError('qualification_parallel_wall_deadline')
                        readable, _, _ = select.select([receive], [], [], 0.1)
                        if not readable:
                            continue
                        part = os.read(receive, 4096)
                        if not part:
                            if buffer:
                                raise ValueError("unterminated supervisor protocol frame")
                            break
                        buffer += part
                        if len(buffer) > _FRAME_LIMIT:
                            raise ValueError("supervisor protocol frame exceeds bound")
                        while b"\n" in buffer:
                            raw, buffer = buffer.split(b"\n", 1)
                            frame = json.loads(raw, object_pairs_hook=_unique)
                            if type(frame) is not dict:
                                raise ValueError("supervisor frame is not an object")
                            base = {"protocol_version", "pid", "source", "registry", "event"}
                            event = frame.get("event")
                            additions = {"started": set(), "ready": set(), "final": {"payload_artifact", "status", "timing"},
                                         "failure": {"error"}}
                            if (event not in additions or set(frame) != base | additions[event]
                                    or type(frame["protocol_version"]) is not int or frame["protocol_version"] != 1
                                    or type(frame["pid"]) is not int or frame["pid"] != process.pid
                                    or frame["source"] != source or frame["registry"] != registry):
                                raise ValueError("supervisor frame schema/authority binding differs")
                            if event == "started":
                                if events:
                                    raise ValueError("duplicate/out-of-order started frame")
                                startup_seconds = _elapsed(clock, startup_began)
                                prepared_began = clock()
                            elif event == "ready":
                                if events != ["started"]:
                                    raise ValueError("duplicate/out-of-order ready frame")
                                receipt["excluded_preparation_seconds"] = _elapsed(clock, prepared_began)
                                began = clock()
                                parallel_deadline = time.monotonic() + _PARALLEL_WALL_SECONDS
                                if os.write(ack_write, b"ACK\n") != 4:
                                    raise ValueError("supervisor acknowledgment was incomplete")
                                os.close(ack_write)
                                ack_write = None
                            elif event == "final":
                                if events not in (["started"], ["started", "ready"]):
                                    raise ValueError("duplicate/out-of-order final frame")
                                if type(frame["status"]) is not str or type(frame["timing"]) is not dict or set(frame["timing"]) != {
                                        "parallel_native_seconds", "payload_post_validation_seconds", "payload_publication_seconds"}:
                                    raise ValueError("final frame interval schema differs")
                                for value in frame["timing"].values():
                                    _seconds(value)
                                if events == ["started"] and (
                                        frame["status"] != "invalid"
                                        or frame["timing"]["parallel_native_seconds"] != 0
                                        or frame["timing"]["payload_post_validation_seconds"] != 0):
                                    raise ValueError("pre-parallel final must describe an invalid unmeasured run")
                                final = frame
                            else:
                                if events not in ([], ["started"], ["started", "ready"]):
                                    raise ValueError("duplicate/out-of-order failure frame")
                                raise ValueError("payload failure: " + str(frame["error"])[:1024])
                            events.append(event)
                            if len(events) > 3:
                                raise ValueError("excess supervisor frames")
                    if events not in (["started", "final"], ["started", "ready", "final"]):
                        raise ValueError("missing supervisor ready/final frame")
                    # EOF is not process exit. Keep the real wall fence live
                    # until collection, even if the payload closes its pipes
                    # before slow exit work. The exception path requests native
                    # cancellation; the final wait still collects that cleanup.
                    deadline = min(safety_end, parallel_deadline) if parallel_deadline is not None else safety_end
                    try:
                        process.wait(timeout=max(0.0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired as exc:
                        reason = ('qualification_parallel_wall_deadline'
                                  if parallel_deadline is not None and parallel_deadline <= safety_end
                                  else 'supervisor_protocol_safety_deadline')
                        raise TimeoutError(reason) from exc
                except BaseException as exc:
                    operator_cancelled = owner.number is not None
                    protocol_error = type(exc).__name__ + ": " + str(exc)
                    if isinstance(exc, TimeoutError):
                        receipt['refusal_reason'] = str(exc)
                    if getattr(process, "pid", None) is not None and process.poll() is None:
                        if owner.number is None:
                            owner.number = signal.SIGINT
                        try:
                            owner.forward()
                        except OSError as signal_error:
                            receipt["signal_error"] = type(signal_error).__name__ + ": " + str(signal_error)
                finally:
                    if getattr(process, "pid", None) is not None:
                        # Retain the live handle indefinitely if cancellation
                        # cleanup has not returned. The outer owned terminal
                        # stays controllable; no timeout abandons a child.
                        process.wait()
                        collected = True
                        ended = clock()
                        receipt["payload_exit_code"] = process.returncode
                    owner.process = None
            if protocol_error:
                raise ValueError(protocol_error)
            payload = _artifact(engine, output, final["payload_artifact"])
            receipt["payload_artifact"] = final["payload_artifact"]
            if (payload.get("source") != source or payload.get("registry") != registry
                    or payload.get("source_after") != source or payload.get("registry_after") != registry):
                raise ValueError("collected payload source/registry cannot qualify")
            receipt["registry_after"] = engine["_read_registry"]()[1]
            receipt["source_after"] = launcher.source_identity(root)
            if receipt["registry_after"] != registry or receipt["source_after"] != source:
                raise ValueError("fixed registry/source changed during supervised qualification")
            if events == ["started", "final"]:
                launches = payload.get("launches")
                if (final["status"] != "invalid" or payload.get("status") != "invalid"
                        or payload.get("successful") is not False
                        or payload.get("qualified") is not False
                        or process.returncode != 1
                        or type(launches) is not list or len(launches) != 1
                        or type(launches[0]) is not dict
                        or launches[0].get("phase") != "serial"
                        or launches[0].get("collected") is not True
                        or type(launches[0].get("exit_code")) is not int
                        or type(payload.get("error")) is not str or not payload["error"]):
                    raise ValueError("collected pre-parallel failure cannot be admitted")
                # The payload exited naturally and its failed artifact is bound.
                # Keep the primary error without inventing a parallel interval
                # or granting cleanup authority to an unsuccessful run.
                raise ValueError("payload failed before parallel: " + payload["error"][:1024])
            wall = _seconds(ended - began)
            timing = dict(final["timing"])
            subtotal = sum(timing.values())
            # Separate clocks can differ by rounding, but a materially negative
            # residual is inconsistent evidence, never silently clamped.
            if subtotal - wall > 0.001:
                raise ValueError("payload subintervals exceed externally collected interval")
            timing.update(publication_to_payload_exit_seconds=max(0.0, wall - subtotal),
                          supervisor_startup_seconds=startup_seconds, observer_publication_seconds=0.0)
            # Retain independently collected timing for a source-bound failed
            # run. Measurement is not qualification or cleanup authority.
            receipt.update(parallel_wall_seconds=wall, timing=timing)
            receipt["residual_rounding_tolerance_seconds"] = 0.001
            if (process.returncode != 0 or final["status"] != "awaiting_external_gate"
                    or payload.get("status") != "awaiting_external_gate"
                    or payload.get("successful") is not True or payload.get("qualified") is not False
                    or payload.get("cleanup", {}).get("successful") is not True):
                raise ValueError("collected payload status/cleanup cannot qualify")
            # Only admitted payload authority and consistent external testimony
            # can prove cleanup. A valid over-budget run still has that proof.
            payload_cleanup = True
            receipt.update(status="complete", successful=True, qualified=wall <= 600.0,
                           parallel_wall_seconds=wall, timing=timing)
            if wall > 600.0:
                receipt["gate_reason"] = "external_parallel_total_exceeded_600_seconds"
        except Exception as exc:
            if not collected and getattr(process, "pid", None) is not None:
                # Preserve scratch/protocol output; do not manufacture cleanup.
                raise
            receipt.update(error=type(exc).__name__ + ": " + str(exc))
        finally:
            for descriptor in (receive, transmit, acknowledge, ack_write):
                if descriptor is not None:
                    os.close(descriptor)
        if not collected:
            raise ValueError("payload was not launched/collected; retained scratch: " + str(scratch))
        # Only a private ordinary output created by the admitted payload may
        # receive terminal observer evidence and retained transport logs.
        if not output.exists():
            output.mkdir(mode=0o700)
        engine["_ordinary"](output, directory=True)
        info = output.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("payload output lost its private ownership")
        for name in ("payload.stdout", "payload.stderr"):
            with (output / name).open("xb") as destination, (scratch / name).open("rb") as original:
                shutil.copyfileobj(original, destination)
        if payload_cleanup:
            shutil.rmtree(scratch)
        receipt["cleanup"] = {"successful": payload_cleanup and not scratch.exists(),
                              "payload_native_cleanup_proved": payload_cleanup,
                              "payload_collected": collected,
                              "scratch_retained": scratch.exists()}
        if scratch.exists():
            receipt["scratch_root"] = str(scratch)
        with engine["final_commitment"](owner):
            receipt["signal_delivery_errors"] = list(owner.delivery_errors)
            if owner.number is not None:
                interrupted = operator_cancelled or protocol_error is None
                receipt.update(status="interrupted" if interrupted else "invalid",
                               successful=False, qualified=False,
                               exit_code=128 + owner.number if interrupted else 1)
            publication_began = time.monotonic()
            disk_receipt = dict(receipt)
            if "timing" in receipt:
                disk_receipt["timing"] = dict(receipt["timing"], observer_publication_seconds=None)
            engine["_publish"](output / "observer.json", disk_receipt)
            if "timing" in receipt:
                receipt["timing"]["observer_publication_seconds"] = _elapsed(time.monotonic, publication_began)
    return receipt
