"""Items Floati observes for the needs-you inbox (NEEDS-YOU-OBSERVED-1).

An agent raises an item with ``floati needs file``. This module adds the
items nobody raised: a thread an orchestrator launched that finished, or that
waits on a person, read from the orchestrator's own local thread list.

``floati needs observe`` is one bounded pull. It reads the explicit source
configuration a caller names (which orchestrator, its CLI as one canonical
absolute executable, its home and the project), runs that CLI once with a
minimal environment and no input, and appends one ``needs_observed`` item per
new event to the inbox ledger. An event is keyed on the run identity the
orchestrator reports, never on the thread alone, so a thread that completes a
second turn after a follow-up is a new item, and that item supersedes the
thread's earlier one. A thread waiting on its own background task is not
waiting on a person and is not an item.

The read never writes to the orchestrator, opens no socket of Floati's own,
and passes the orchestrator no credential: only HOME, PATH, LANG and LC_ALL.
An absent or invalid configuration is a typed refusal; an orchestrator that
cannot be read is ``degraded`` with its cause, never an empty inbox. Each
change of a source's outcome is one ``needs_observation`` row, so the inbox
read can say whether, and since when, a source has been read.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import select
import stat
import subprocess
import sys
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import ProtocolRefusal
from .needs_records import MAX_OBSERVED_COUNT

CONFIG_FIELDS = frozenset({"schema_version", "orchestrator", "cli", "home", "project"})
# How often a seat's Stop waiter may pull this source (NEEDS-YOU-OBSERVED-1, waiter condition).
OPTIONAL_FIELDS = frozenset({"interval_seconds"})
DEFAULT_INTERVAL_SECONDS = 60
MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 3600
MAX_CONFIG_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_THREADS = 4096
MAX_EVENTS_PER_OBSERVE = 64
READ_DEADLINE_SECONDS = 10.0
FINISHED = ("completed", "interrupted", "failed", "error")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_STATUS = re.compile(r"[a-z][a-z_-]{0,63}\Z")
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ENVIRONMENT_PATH = "/usr/bin:/bin"


class SourceUnreadable(Exception):
    """The orchestrator could not be read; ``code`` names why."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _invalid(detail: str) -> ProtocolRefusal:
    return ProtocolRefusal("needs_source_config_invalid", detail,
                           remedy="Name a source configuration file holding exactly schema_version 1, orchestrator, "
                                  "cli (one absolute executable), home (one absolute directory) and project.")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object member")
        value[key] = item
    return value


def _strict_json(data: bytes):
    return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite number")))


def _canonical_directory(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise _invalid("home must be one absolute directory")
    path = Path(value)
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or path.resolve(strict=True) != path:
            raise OSError("not one canonical directory")
    except (OSError, RuntimeError) as exc:
        raise _invalid("home must be one existing canonical directory") from exc
    return value


def _trusted(metadata: os.stat_result) -> bool:
    """Only this user or root chose these bytes: no other account can rewrite what Floati runs."""
    return metadata.st_uid in (0, os.getuid()) and not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def load_config(value: object) -> Dict[str, object]:
    """Read and validate the one source configuration a caller names; nothing is discovered."""
    if not isinstance(value, str) or not value.startswith("/"):
        raise _invalid("the source configuration is named by one absolute path")
    try:
        descriptor = os.open(value, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ProtocolRefusal("needs_source_unconfigured", "no source configuration exists at " + value,
                              remedy="Write the orchestrator's source configuration at this path, "
                                     "or name the file that holds it.") from None
    except OSError as exc:
        raise _invalid("the source configuration could not be opened: " + type(exc).__name__) from exc
    try:
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise _invalid("the source configuration is one regular file, never a link")
            if not _trusted(metadata):
                raise _invalid("the source configuration is owned by this user or root and writable by no one else")
            if metadata.st_size > MAX_CONFIG_BYTES:
                raise _invalid("the source configuration is at most 16 KiB")
            data = source.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise _invalid("the source configuration is at most 16 KiB")
        config = _strict_json(data)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _invalid("the source configuration is not one readable JSON object") from exc
    if not isinstance(config, dict) or not CONFIG_FIELDS <= frozenset(config) <= CONFIG_FIELDS | OPTIONAL_FIELDS:
        raise _invalid("the source configuration names exactly " + ", ".join(sorted(CONFIG_FIELDS))
                       + ", and optionally interval_seconds")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise _invalid("the source configuration is schema version 1")
    if not isinstance(config["orchestrator"], str) or config["orchestrator"] not in ORCHESTRATORS:
        raise _invalid("this build reads the orchestrators " + ", ".join(sorted(ORCHESTRATORS)))
    if not isinstance(config["project"], str) or _PROJECT.match(config["project"]) is None:
        raise _invalid("project is one identifier of at most 128 characters")
    cli = config["cli"]
    if not isinstance(cli, str):
        raise _invalid("cli must be one absolute canonical executable")
    path = Path(cli)
    if path.anchor != "/" or len(path.parts) < 2 or ".." in path.parts:
        raise _invalid("cli must be one absolute canonical executable")
    home = _canonical_directory(config["home"])
    interval = config.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    if type(interval) is not int or not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
        raise _invalid("interval_seconds is a whole number of seconds from 30 to 3600")
    return {"orchestrator": config["orchestrator"], "cli": str(path), "home": home, "project": config["project"],
            "interval_seconds": interval, "path": value, "sha256": hashlib.sha256(data).hexdigest()}


# ---------------------------------------------------------------------------
# One bounded run of the orchestrator's CLI.
# ---------------------------------------------------------------------------

@contextmanager
def _open_executable(path: Path, *, descriptor_exec: bool):
    """Open a canonical path component by component and validate the selected executable."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        parent = os.open(path.anchor, flags)
        try:
            for part in path.parts[1:-1]:
                if not descriptor_exec:
                    _require_protected_directory(parent)
                child = os.open(part, flags, dir_fd=parent)
                os.close(parent)
                parent = child
            if not descriptor_exec:
                _require_protected_directory(parent)
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
    except OSError as exc:
        raise _invalid("cli must be one absolute canonical executable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & 0o111:
            raise _invalid("cli must be one absolute canonical executable")
        if not _trusted(metadata):
            raise _invalid("cli is owned by this user or root and writable by no one else")
        yield descriptor
    finally:
        os.close(descriptor)


def _require_protected_directory(descriptor: int) -> None:
    # A sticky directory protects the next component when it is owned by
    # root or this user (for example under <temp>).
    directory = os.fstat(descriptor)
    if directory.st_uid not in (0, os.getuid()) or (
            not _trusted(directory) and not directory.st_mode & stat.S_ISVTX):
        raise _invalid("cli path directories must protect their entries from other users")


def _run(argv: Sequence[str], home: str, *, authorize=None) -> bytes:
    from .thread_source import _cleanup

    environment = {"HOME": home, "PATH": _ENVIRONMENT_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    descriptor_exec = sys.platform == "linux"
    with _open_executable(Path(argv[0]), descriptor_exec=descriptor_exec) as executable:
        # Linux resolves the executable through the inherited descriptor.
        # Other hosts use the name only within the protected directory chain.
        launch = ({"executable": "/proc/self/fd/" + str(executable), "pass_fds": (executable,)}
                  if descriptor_exec else {})
        if authorize is not None:
            authorize()
        try:
            process = subprocess.Popen(list(argv), cwd=home, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
                                       start_new_session=True, **launch)
        except OSError as exc:
            raise SourceUnreadable("source_exec_failed",
                                   "the orchestrator CLI could not be started: " + type(exc).__name__) from exc
    output = bytearray()
    deadline = time.monotonic() + READ_DEADLINE_SECONDS
    failure: Optional[SourceUnreadable] = None
    try:
        descriptor = process.stdout.fileno()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = SourceUnreadable("source_timeout", "the orchestrator CLI did not finish within 10 s")
                break
            ready, _, _ = select.select([descriptor], [], [], remaining)
            if not ready:
                continue
            try:
                block = os.read(descriptor, 65536)
            except OSError as exc:
                failure = SourceUnreadable("source_exec_failed",
                                           "the orchestrator CLI output could not be read: " + type(exc).__name__)
                break
            if not block:
                break
            output.extend(block)
            if len(output) > MAX_OUTPUT_BYTES:
                failure = SourceUnreadable("source_output_too_large",
                                           "the orchestrator CLI wrote more than 4 MiB")
                break
        if failure is None:
            try:
                returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = SourceUnreadable("source_timeout", "the orchestrator CLI did not finish within 10 s")
            else:
                if returncode != 0:
                    failure = SourceUnreadable("source_exit_nonzero",
                                               "the orchestrator CLI exited with status " + str(returncode))
    finally:
        _cleanup(process)
    if failure is not None:
        raise failure
    return bytes(output)


def _optional(thread: Mapping[str, object], key: str, kinds: tuple, default):
    value = thread.get(key)
    if value is None:
        return default
    if not isinstance(value, kinds) or (bool not in kinds and isinstance(value, bool)):
        raise SourceUnreadable("source_output_invalid", "a thread's " + key + " has the wrong type")
    return value


def _title(value: object, fallback: str) -> str:
    """The orchestrator's title as one visible line; it is observed text, never trusted markup."""
    if not isinstance(value, str):
        return fallback
    text = " ".join("".join(" " if unicodedata.category(c).startswith("C") else c for c in value).split())
    return text or fallback


def read_t3_code(config: Mapping[str, object], *, authorize=None) -> List[Dict[str, object]]:
    """T3 Code's own thread list for one project, normalised; any malformed row fails the whole read."""
    raw = _run([str(config["cli"]), "thread", "list", "--project", str(config["project"]), "--json"],
               str(config["home"]), authorize=authorize)
    try:
        document = _strict_json(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise SourceUnreadable("source_output_invalid", "the orchestrator CLI did not print one JSON value") from exc
    rows = document if isinstance(document, list) else (
        document.get("threads") if isinstance(document, dict) else None)
    if not isinstance(rows, list) or len(rows) > MAX_THREADS:
        raise SourceUnreadable("source_output_invalid", "the orchestrator CLI printed no thread list")
    threads = []
    for row in rows:
        if not isinstance(row, dict):
            raise SourceUnreadable("source_output_invalid", "a thread is not a JSON object")
        ident, status = row.get("threadId"), row.get("status")
        if not isinstance(ident, str) or _IDENTIFIER.match(ident) is None:
            raise SourceUnreadable("source_output_invalid", "a thread has no readable threadId")
        if not isinstance(status, str) or _STATUS.match(status) is None:
            raise SourceUnreadable("source_output_invalid", "a thread has no readable status")
        run = _optional(row, "latestRunId", (str,), None)
        if run is not None and _IDENTIFIER.match(run) is None:
            raise SourceUnreadable("source_output_invalid", "a thread's latestRunId is not one identifier")
        background = _optional(row, "pendingBackgroundTasks", (list,), [])
        pending = _optional(row, "pendingRequestCount", (int,), 0)
        if not 0 <= pending <= MAX_OBSERVED_COUNT:
            raise SourceUnreadable("source_output_invalid",
                                   "a thread's pendingRequestCount must be between 0 and 2147483647")
        threads.append({"thread_id": ident, "status": status, "run_id": run,
                        "background_tasks": len(background), "pending_requests": pending,
                        "settled": bool(_optional(row, "settled", (bool,), False)),
                        "archived": bool(_optional(row, "archived", (bool,), False)),
                        "title": _title(row.get("title"), ident)})
    return threads


# Each orchestrator this build can read, by the name its configuration uses.
ORCHESTRATORS: Dict[str, Callable[..., List[Dict[str, object]]]] = {
    "t3-code": read_t3_code,
}


# ---------------------------------------------------------------------------
# Which threads need a person, and the event each one is.
# ---------------------------------------------------------------------------

def event_of(thread: Mapping[str, object]) -> Optional[str]:
    """The watcher's rule: finished, a pending request, or waiting with no task of its own."""
    if thread["settled"] or thread["archived"]:
        return None
    if thread["pending_requests"]:
        return "request_pending"
    if thread["status"] in FINISHED:
        return "finished"
    if thread["status"] == "waiting" and not thread["background_tasks"]:
        return "waiting_on_person"
    return None


def event_key(source: Mapping[str, object], thread: Mapping[str, object]) -> str:
    """The event's identity: its run and what the run reports, never the thread alone."""
    identity = {"orchestrator": source["orchestrator"], "project": source["project"],
                "thread_id": thread["thread_id"], "run_id": thread["run_id"], "status": thread["status"],
                "pending_requests": thread["pending_requests"]}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _bounded(prefix: str, title: str, limit: int = 256) -> str:
    data = (prefix + title[:limit]).encode("utf-8")
    return data[:limit].decode("utf-8", "ignore").rstrip() or prefix.rstrip(": ")


def statement_title(event: str, thread: Mapping[str, object]) -> str:
    if event == "finished":
        prefix = "Thread finished (" + str(thread["status"]) + "): "
    elif event == "request_pending":
        prefix = "Thread is waiting on a person (" + str(thread["pending_requests"]) + " pending request(s)): "
    else:
        prefix = "Thread is waiting on a person: "
    return _bounded(prefix, str(thread["title"]))


def coordinate_of(source: Mapping[str, object], thread: Mapping[str, object]) -> Dict[str, object]:
    from . import bound_record
    return bound_record.stamp_fact(bound_record.OBSERVED, kind="thread", orchestrator=source["orchestrator"],
                                   project=source["project"], thread_id=thread["thread_id"],
                                   run_id=thread["run_id"], status=thread["status"],
                                   pending_requests=thread["pending_requests"],
                                   background_tasks=thread["background_tasks"])


def thread_identity(coordinate: Mapping[str, object]) -> Tuple[str, str, str]:
    return str(coordinate["orchestrator"]), str(coordinate["project"]), str(coordinate["thread_id"])


def freshness(item: Mapping[str, object], successor: Optional[Mapping[str, object]]) -> Dict[str, object]:
    """An observed item is current until a later event on its thread is recorded in this root.

    The inbox read never reruns the orchestrator, so "current" means no later
    event is recorded, and a superseded item is stale naming the run it moved to.
    """
    (coordinate,) = item["coordinates"]
    if successor is None:
        verdict = {"state": "current", "cause": None, "moved": []}
    else:
        (later,) = successor["coordinates"]
        verdict = {"state": "stale", "cause": None, "moved": [{
            "what": "thread", "thread_id": coordinate["thread_id"],
            "from": {"run_id": coordinate["run_id"], "status": coordinate["status"]},
            "to": {"run_id": later["run_id"], "status": later["status"]}}]}
    token = hashlib.sha256(json.dumps([verdict], sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"state": verdict["state"], "coordinates": [verdict], "token": token}


def source_of(record: Mapping[str, object]) -> Dict[str, object]:
    """What put an item in the inbox: an agent raising it, or Floati observing it."""
    if record["kind"] == "needs_observed":
        (coordinate,) = record["coordinates"]
        return {"type": "observed", "orchestrator": coordinate["orchestrator"], "project": coordinate["project"],
                "observer": record["actor"]}
    return {"type": "raised", "by": record["actor"]}


def observed_sources(observations: Sequence[Mapping[str, object]],
                     items: Mapping[str, Mapping[str, object]]) -> Dict[str, object]:
    """Each source's latest recorded outcome, since when, and its latest observed item."""
    latest: Dict[Tuple[str, str], Mapping[str, object]] = {}
    for row in observations:
        latest[(row["source"]["orchestrator"], row["source"]["project"])] = row
    last_event: Dict[Tuple[str, str], str] = {}
    for item in items.values():
        if item["kind"] == "needs_observed":
            (coordinate,) = item["coordinates"]
            last_event[(coordinate["orchestrator"], coordinate["project"])] = item["timestamp"]
    if not latest:
        return {"state": "none_recorded", "cause": "no orchestrator observation is recorded in this root",
                "sources": []}
    return {"state": "recorded", "cause": None, "sources": [
        {"orchestrator": key[0], "project": key[1], "outcome": row["outcome"], "cause": row["cause"],
         "since": row["timestamp"], "receipt_id": row["id"], "last_item_at": last_event.get(key)}
        for key, row in sorted(latest.items())]}


# ---------------------------------------------------------------------------
# The pull.
# ---------------------------------------------------------------------------

def observe(root, node: str, config_path: str):
    """Read one configured orchestrator once and append each new event as an inbox item."""
    from .needs_inbox import _require_node

    return _observe_config(root, _require_node(root, node), load_config(config_path))


def _observe_config(root, node: str, config: Mapping[str, object], *, authorize=None):
    from .needs_inbox import LEDGER, _base, _state
    from .jsonl import transact_records
    from .needs_records import NEEDS_KINDS
    from .registry import utc_now

    source = {"orchestrator": config["orchestrator"], "project": config["project"]}
    try:
        threads = ORCHESTRATORS[str(config["orchestrator"])](config, authorize=authorize)
        cause = None
    except SourceUnreadable as exc:
        threads, cause = None, {"code": exc.code, "detail": exc.detail}
    outcome = "read" if threads is not None else "unknown"
    events = []
    unkeyed = []
    for thread in threads or ():
        event = event_of(thread)
        if event is None:
            continue
        if thread["run_id"] is None:
            # Without a run identity a second event on this thread could not be told from the first.
            unkeyed.append({"thread_id": thread["thread_id"], "status": thread["status"]})
            continue
        events.append((event, thread, event_key(source, thread), statement_title(event, thread)))

    def decide(rows):
        state = _state(rows)
        recorded = {row["event_key"] for row in rows if row["kind"] == "needs_observed"}
        heads = {}
        for ident, item in state["items"].items():
            if item["kind"] == "needs_observed" and ident not in state["superseded"]:
                heads[thread_identity(item["coordinates"][0])] = ident
        batch, already, deferred = [], 0, 0
        prior = next((row for row in reversed(state["observations"])
                      if (row["source"]["orchestrator"], row["source"]["project"])
                      == (source["orchestrator"], source["project"])), None)
        if prior is None or (prior["outcome"], (prior["cause"] or {}).get("code")) != (
                outcome, (cause or {}).get("code")):
            batch.append({**_base("needs_observation", "needs-observation-", root), "actor": node,
                          "source": dict(source), "outcome": outcome, "cause": cause})
        appended = []
        for event, thread, key, title in events:
            if key in recorded:
                already += 1
                continue
            if len(appended) >= MAX_EVENTS_PER_OBSERVE:
                deferred += 1
                continue
            coordinate = coordinate_of(source, thread)
            identity = thread_identity(coordinate)
            record = {**_base("needs_observed", "needs-item-", root), "actor": node,
                      "item_kind": "review" if event == "finished" else "question",
                      "statement": {"title": title, "detail": None,
                                    "provenance": "observed"},
                      "coordinates": [coordinate], "supersedes": heads.get(identity), "event_key": key}
            heads[identity] = record["id"]
            recorded.add(key)
            appended.append(record)
        batch.extend(appended)
        receipt = batch[0] if batch and batch[0]["kind"] == "needs_observation" else None
        if authorize is not None:
            authorize()
        return (appended, already, deferred, receipt), batch

    appended, already, deferred, receipt = transact_records(root, LEDGER, decide, allowed_kinds=set(NEEDS_KINDS))
    evidence = {"root": str(root.path), "observed_at": utc_now(),
                "source": {**source, "config": config["path"], "config_sha256": config["sha256"]},
                "observation": {"state": outcome, "threads": None if threads is None else len(threads),
                                "cause": cause},
                "appended": appended, "already_recorded": already, "deferred": deferred,
                "unkeyed": unkeyed, "observation_receipt": receipt}
    return evidence
