"""Closed Confluence projection of authenticated native Codex starts."""

from datetime import datetime
import errno
import json
import os
from pathlib import Path
import stat
import tempfile

from .adhoc_journal import AdhocJournal, JOURNAL_KINDS
from .adhoc_records import CODEX_PERMISSION, PROFILE_RELATIVE
from .bus_epoch import _acquire_epoch_descriptor
from .errors import FloatiError
from .jsonl import read_records_sealed_snapshot

LAUNCHES_BASENAME = "codex-launches.json"
_MAX_RECORDS = 10_000
_MAX_BYTES = 16 * 1024 * 1024


def _document(records, complete):
    return {"schema_version": 0, "kind": "codex_headless_launch_projection",
            "complete": complete, "records": records}


def _project_launch(admission, opened, profile):
    launcher = admission["evidence"].get("native_launcher")
    if opened is None or profile is None or launcher is None:
        return None
    coordinate = opened["evidence"]["provider_coordinate"]
    cwd = profile["workspace"]
    requested, observed = admission["timestamp"], opened["timestamp"]
    if (profile["adapter"] != "codex" or profile["digest"] != admission["profile_digest"]
            or coordinate["provider"] != "codex_local"
            or coordinate["session_id"] == launcher["sender_native_session_id"]
            or datetime.fromisoformat(requested.replace("Z", "+00:00"))
            > datetime.fromisoformat(observed.replace("Z", "+00:00"))):
        return None
    return {
        "schema_version": 0, "kind": "codex_headless_launch", "id": opened["id"],
        "dispatch_message_id": admission["envelope_id"], "launcher_record_id": launcher["id"],
        "launcher_node_id": launcher["sender_node_id"], "launcher_provider": launcher["sender_provider"],
        "launcher_session_id": launcher["sender_native_session_id"],
        "child_session_id": coordinate["session_id"], "cwd": cwd,
        "launch_requested_at": requested, "launch_observed_at": observed,
    }


def codex_launch_projection_bytes(root):
    # Seal the journal first: every profile referenced in that snapshot must
    # already have been committed, and profiles are immutable.
    try:
        journal = read_records_sealed_snapshot(root, AdhocJournal.relative_path, allowed_kinds=JOURNAL_KINDS)
        admissions, opened = AdhocJournal._project_history(journal)
        profiles = read_records_sealed_snapshot(root, PROFILE_RELATIVE, allowed_kinds={"adhoc_profile_record"})
    except (FloatiError, OSError):
        return _encode_projection([], False)
    by_profile = {profile["id"]: profile for profile in profiles}
    reserved = {row["dispatch_id"] for row in journal if row["phase"] == "spawn_reserved"}
    records = []
    complete = True
    for dispatch_id, admission in admissions.items():
        dispatch = admission["dispatch"]
        if (dispatch["operation"] != "start" or dispatch["permission"] != CODEX_PERMISSION
                or (dispatch_id not in opened and dispatch_id not in reserved)):
            continue
        record = _project_launch(admission, opened.get(dispatch_id), by_profile.get(admission["profile_id"]))
        if record is None:
            complete = False
            continue
        records.append(record)
        if len(records) > _MAX_RECORDS:
            return _encode_projection([], False)
    return _encode_projection(records, complete)


def _encode_projection(records, complete):
    data = (json.dumps(_document(records, complete), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > _MAX_BYTES:
        return _encode_projection([], False)
    return data


def _stage_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def publish_bundle_files(out, data, launches):
    """Publish private files, with sidecar absence during a pair replacement."""
    with tempfile.TemporaryDirectory(prefix=".confluence-", dir=out.parent) as staging:
        stage = Path(staging)
        primary = stage / "bundle"
        _stage_private(primary, data)
        if launches is None:
            os.replace(primary, out)
            return
        projection = stage / LAUNCHES_BASENAME
        _stage_private(projection, launches)
        descriptor = os.open(out.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _acquire_epoch_descriptor(descriptor, exclusive=True, use_turnstile=False)
            _publish_pair(out, primary, projection, stage / "previous-launches", descriptor)
        finally:
            os.close(descriptor)


def _publish_pair(out, primary, projection, previous, descriptor):
    sidecar = out.with_name(LAUNCHES_BASENAME)
    had_previous = False
    try:
        if stat.S_ISDIR(sidecar.lstat().st_mode):
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(sidecar))
        os.replace(sidecar, previous)
        had_previous = True
    except FileNotFoundError:
        pass

    # Remove the old association first. A failed second publication can leave
    # the new bundle without a sidecar, which consumers already treat as unknown.
    try:
        os.fsync(descriptor)
        os.replace(primary, out)
    except OSError:
        if had_previous:
            os.replace(previous, sidecar)
            os.fsync(descriptor)
        raise
    os.fsync(descriptor)
    os.replace(projection, sidecar)
    os.fsync(descriptor)
