"""Explicit, byte-backed ownership of one Codex seat's configuration entries.

The caller stores the returned plan privately. No default paths, home discovery,
hook-trust changes, or mandatory third-party dependencies are involved.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

from .errors import ProtocolRefusal

MAX_BYTES = 1024 * 1024
IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
PROFILE_FIELDS = {"transport", "git_common_dir", "bus_root", "identity",
                  "allowed_operations", "send_repository", "send_recipients", "workspace_root"}
TRANSPORT_FIELDS = {"kind", "install_root", "manifest_path", "manifest_sha256", "source_sha",
                    "interpreter", "max_files", "max_file_bytes", "max_total_bytes"}
OPERATIONS = {"board", "takeover", "inbox", "ack", "send", "attach", "doctor-probe", "waiter-smoke"}


def _refuse(detail):
    raise ProtocolRefusal(
        "seat_harness_config_invalid", detail,
        remedy="Correct the exact configuration path or conflicting entry named in detail; preserve the saved seat ownership plan and retry. Use Python 3.11 or newer, or install optional tomli, when TOML parsing is unavailable.",
    )


def _path(value):
    if not isinstance(value, (str, Path)):
        _refuse("Configuration coordinates must be absolute normalized paths.")
    raw = str(value)
    path = Path(raw)
    if not path.is_absolute() or str(path) != raw or ".." in path.parts or "\x00" in raw:
        _refuse("Path is not absolute and normalized: " + raw)
    return path


def _open_parent(path):
    """Walk through directory descriptors so replacement cannot follow a symlink."""
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        _refuse("Configuration parent is missing, symlinked, or unreadable: " + str(path) + ": " + str(exc))


def _read_at(parent, name, label):
    descriptor = None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            _refuse("Configuration must be an ordinary file owned by this account: " + label)
        if before.st_size > MAX_BYTES:
            _refuse("Configuration exceeds the 1 MiB bound: " + label)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(content) > MAX_BYTES or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            _refuse("Configuration changed while reading or exceeds 1 MiB: " + label)
        return content, stat.S_IMODE(before.st_mode)
    except OSError as exc:
        _refuse("Configuration is missing, symlinked, or unreadable: " + label + ": " + str(exc))
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read(path):
    parent = _open_parent(path)
    try:
        return _read_at(parent, path.name, str(path))
    finally:
        os.close(parent)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _refuse("Gateway JSON contains duplicate key: " + key)
        result[key] = value
    return result


def _gateway(content):
    try:
        document = json.loads(content.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeError, ValueError) as exc:
        _refuse("Gateway configuration is not valid UTF-8 JSON: " + str(exc))
    if (not isinstance(document, dict) or set(document) != {"schema_version", "profiles", "transports"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 2
            or not isinstance(document["profiles"], dict) or not isinstance(document["transports"], dict)
            or not document["transports"]):
        _refuse("Gateway configuration requires schema_version 2 and profiles/transports objects.")
    for name, transport in document["transports"].items():
        _identifier(name, "Gateway transport name")
        if not isinstance(transport, dict) or set(transport) != TRANSPORT_FIELDS:
            _refuse("Gateway transport requires exactly its nine metadata fields: " + name)
        if transport["kind"] != "floati-install-manifest-v0":
            _refuse("Gateway transport has unsupported kind: " + name)
        for field in ("install_root", "manifest_path", "interpreter"):
            if not isinstance(transport[field], str):
                _refuse("Gateway transport path must be a JSON string: " + name + " " + field)
            _path(transport[field])
        if Path(transport["manifest_path"]) != Path(transport["install_root"]) / ".floati-install" / "manifest.v0.json":
            _refuse("Gateway transport manifest is outside its fixed install location: " + name)
        for field, length in (("manifest_sha256", 64), ("source_sha", 40)):
            value = transport[field]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value):
                _refuse("Gateway transport digest is invalid: " + name + " " + field)
        for field, ceiling in (("max_files", 4096), ("max_file_bytes", 64 * 1024 * 1024),
                               ("max_total_bytes", 256 * 1024 * 1024)):
            value = transport[field]
            if type(value) is not int or not 1 <= value <= ceiling:
                _refuse("Gateway transport bound is invalid: " + name + " " + field)
        if transport["max_file_bytes"] > transport["max_total_bytes"]:
            _refuse("Gateway transport file bound exceeds its total bound: " + name)
    for name, profile in document["profiles"].items():
        _profile_values(name, profile, document)
    return document


def _toml(content):
    try:
        import tomllib as parser
    except ImportError:
        try:
            import tomli as parser
        except ImportError:
            _refuse("TOML validation is unavailable. Use Python 3.11 or newer, or install optional tomli for this interpreter.")
    try:
        document = parser.loads(content.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        _refuse("Codex configuration contains malformed or duplicate TOML tables: " + str(exc))
    if "projects" in document and not isinstance(document["projects"], dict):
        _refuse("Codex projects must be a table.")
    return document


def _identifier(value, label):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        _refuse(label + " must be one lower-case bounded identifier.")


def _profile_values(name, profile, document):
    _identifier(name, "Profile name")
    if not isinstance(profile, dict) or set(profile) != PROFILE_FIELDS:
        _refuse("Seat profile requires exactly the eight gateway profile fields.")
    for field in ("transport", "identity", "send_repository"):
        _identifier(profile[field], "Seat profile " + field)
    for field in ("git_common_dir", "bus_root", "workspace_root"):
        if not isinstance(profile[field], str):
            _refuse("Seat profile " + field + " must be a JSON string path.")
        _path(profile[field])
    _identifier(Path(profile["bus_root"]).name, "Seat bus tenant")
    for field in ("allowed_operations", "send_recipients"):
        values = profile[field]
        if (not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values)
                or values != sorted(set(values))):
            _refuse("Seat profile " + field + " must be a nonempty sorted list of unique strings.")
        if field == "allowed_operations" and not set(values) <= OPERATIONS:
            _refuse("Seat profile allowed_operations contains an unsupported gateway operation.")
        if field == "send_recipients":
            for value in values:
                _identifier(value, "Send recipient")
    if profile["transport"] not in document["transports"]:
        _refuse("Seat profile selects an absent transport: " + profile["transport"])


def _profile(name, profile, document, replace_profile=False):
    _profile_values(name, profile, document)
    previous = document["profiles"].get(name)
    if name in document["profiles"]:
        if not replace_profile:
            _refuse("Gateway profile already exists: " + name)
        others = {key: value for key, value in document["profiles"].items() if key != name}
        _profile(name, previous, {**document, "profiles": others})
        if previous["identity"] != profile["identity"] or previous["bus_root"] != profile["bus_root"]:
            _refuse("Rehome profile identity and bus_root do not match the existing owned profile: " + name)
    for other_name, other in document["profiles"].items():
        if replace_profile and other_name == name:
            continue
        if not isinstance(other, dict):
            _refuse("Existing gateway profile is not an object: " + other_name)
        if other.get("workspace_root") == profile["workspace_root"] or other.get("identity") == profile["identity"]:
            _refuse("Seat workspace or identity collides with gateway profile: " + other_name)
    return previous


def _json_bytes(document):
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _backup(path, content, mode, added, removed):
    if len(added) > MAX_BYTES:
        _refuse("Adding this seat exceeds the 1 MiB configuration bound: " + str(path))
    return {"path": str(path), "mode": mode,
            "baseline_base64": base64.b64encode(content).decode("ascii"),
            "baseline_sha256": hashlib.sha256(content).hexdigest(),
            "added_base64": base64.b64encode(added).decode("ascii"),
            "added_sha256": hashlib.sha256(added).hexdigest(),
            "removed_base64": base64.b64encode(removed).decode("ascii"),
            "removed_sha256": hashlib.sha256(removed).hexdigest()}


def _without_old_trust(content, workspace):
    """Only a whole canonical trust-only table can be retired automatically."""
    document = _toml(content)
    if workspace not in document.get("projects", {}):
        return content
    if document["projects"][workspace] != {"trust_level": "trusted"}:
        _refuse("Old workspace trust table has foreign fields or is not trusted: " + workspace)
    header = ("[projects." + json.dumps(workspace, ensure_ascii=False) + "]").encode("utf-8")
    lines = content.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.rstrip(b"\r\n") == header]
    if len(matches) != 1:
        _refuse("Old workspace trust table is not one canonical, unambiguous table: " + workspace)
    start = matches[0]
    end = next((index for index in range(start + 1, len(lines))
                if lines[index].lstrip().startswith(b"[")), len(lines))
    body = b"".join(lines[start + 1:end])
    if body.strip() != b'trust_level = "trusted"':
        _refuse("Old workspace trust table includes comments or noncanonical fields; retire it explicitly: " + workspace)
    remaining = b"".join(lines[:start] + lines[end:])
    if workspace in _toml(remaining).get("projects", {}):
        _refuse("Removing the canonical old trust table did not retire its workspace: " + workspace)
    return remaining


def _configuration_bytes(gateway_bytes, codex_bytes, profile_name, profile, replace_profile):
    gateway = _gateway(gateway_bytes)
    previous = _profile(profile_name, profile, gateway, replace_profile)
    codex = _toml(codex_bytes)
    workspace = profile["workspace_root"]
    if workspace in codex.get("projects", {}):
        _refuse("Codex already owns a trust entry for the new seat workspace: " + workspace)
    removed_gateway = gateway_bytes
    removed_codex = codex_bytes
    if previous is not None:
        removed_codex = _without_old_trust(codex_bytes, previous["workspace_root"])
        del gateway["profiles"][profile_name]
        removed_gateway = _json_bytes(gateway)
    gateway["profiles"][profile_name] = profile
    separator = b"\n" if removed_codex.endswith(b"\n") else b"\n\n"
    block = separator + ("[projects." + json.dumps(workspace, ensure_ascii=False)
                         + ']\ntrust_level = "trusted"\n').encode("utf-8")
    added_codex = removed_codex + block
    _toml(added_codex)
    return _json_bytes(gateway), added_codex, removed_gateway, removed_codex, block


def prepare(gateway_path: Path, codex_path: Path, profile_name: str, profile: dict,
            replace_profile=False) -> dict:
    """Read and validate existing configs; return an entirely JSON-serializable plan."""
    gateway_path, codex_path = _path(gateway_path), _path(codex_path)
    if gateway_path == codex_path:
        _refuse("Gateway and Codex configuration paths must be distinct.")
    gateway_bytes, gateway_mode = _read(gateway_path)
    codex_bytes, codex_mode = _read(codex_path)
    if type(replace_profile) is not bool:
        _refuse("replace_profile must be an explicit boolean.")
    added_gateway, added_codex, removed_gateway, removed_codex, block = _configuration_bytes(
        gateway_bytes, codex_bytes, profile_name, profile, replace_profile)
    from .seat_transport_preflight import preflight_transport
    preflight_transport(_gateway(gateway_bytes)["transports"][profile["transport"]])
    profile = json.loads(json.dumps(profile))
    return {"schema_version": 1, "profile_name": profile_name, "profile": profile,
            "replace_profile": replace_profile,
            "trust_block_base64": base64.b64encode(block).decode("ascii"),
            "gateway": _backup(gateway_path, gateway_bytes, gateway_mode, added_gateway, removed_gateway),
            "codex": _backup(codex_path, codex_bytes, codex_mode, added_codex, removed_codex)}


def validate_transport(gateway_path, transport_name):
    """Enforce the selected existing install before any seat ownership write."""
    from .seat_transport_preflight import preflight_transport
    document = _gateway(_read(_path(gateway_path))[0])
    if transport_name not in document["transports"]:
        _refuse("Seat profile selects an absent transport: " + transport_name)
    return preflight_transport(document["transports"][transport_name])


def _decode(value, label):
    try:
        if not isinstance(value, str) or len(value) > 2 * MAX_BYTES:
            _refuse("Saved configuration backup is absent or oversized: " + label)
        data = base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError) as exc:
        _refuse("Saved configuration backup is invalid base64: " + label + ": " + str(exc))
    if len(data) > MAX_BYTES:
        _refuse("Saved configuration backup exceeds 1 MiB: " + label)
    return data


def _plan(plan):
    base_fields = {"schema_version", "profile_name", "profile", "trust_block_base64", "gateway", "codex"}
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] not in (0, 1):
        _refuse("Saved seat configuration plan has an unsupported shape or version.")
    version = plan["schema_version"]
    if set(plan) != base_fields | ({"replace_profile"} if version == 1 else set()):
        _refuse("Saved seat configuration plan has unknown or missing fields.")
    replace_profile = plan.get("replace_profile", False)
    if type(replace_profile) is not bool:
        _refuse("Saved replace_profile must be an explicit boolean.")
    contents = {}
    for name in ("gateway", "codex"):
        row = plan[name]
        row_fields = {"path", "mode", "baseline_base64", "baseline_sha256", "added_base64", "added_sha256"}
        if version == 1:
            row_fields |= {"removed_base64", "removed_sha256"}
        if not isinstance(row, dict) or set(row) != row_fields:
            _refuse("Saved configuration ownership row is malformed: " + name)
        _path(row["path"])
        if type(row["mode"]) is not int or not 0 <= row["mode"] <= 0o777:
            _refuse("Saved configuration mode is invalid: " + name)
        for label in (("baseline", "added", "removed") if version == 1 else ("baseline", "added")):
            data = _decode(row[label + "_base64"], name + " " + label)
            if hashlib.sha256(data).hexdigest() != row[label + "_sha256"]:
                _refuse("Saved configuration backup digest mismatch: " + name + " " + label)
            contents[name + "_" + label] = data
        if version == 0:
            contents[name + "_removed"] = contents[name + "_baseline"]
    if plan["gateway"]["path"] == plan["codex"]["path"]:
        _refuse("Saved configuration paths are not distinct.")
    expected_gateway, expected_codex, removed_gateway, removed_codex, expected_block = _configuration_bytes(
        contents["gateway_baseline"], contents["codex_baseline"], plan["profile_name"], plan["profile"], replace_profile)
    block = _decode(plan["trust_block_base64"], "owned trust block")
    if (expected_gateway != contents["gateway_added"] or expected_codex != contents["codex_added"]
            or removed_gateway != contents["gateway_removed"] or removed_codex != contents["codex_removed"]
            or block != expected_block):
        _refuse("Saved configuration additions or removal targets do not match their owned entries.")
    return contents, block


def _stage(parent, content):
    name = ".seat-config-" + secrets.token_hex(16)
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        observed, mode = _read_at(parent, name, name)
        if observed != content or mode != 0o600:
            _refuse("Private configuration staging backup failed readback: " + name)
        return name
    except BaseException:
        os.unlink(name, dir_fd=parent)
        raise


def _transaction(changes):
    """Preflight and privately stage all backups before replacing either file."""
    rows = []
    published = []
    try:
        for path, current, current_mode, target, target_mode in changes:
            parent = _open_parent(path)
            rows.append([path, parent, current, current_mode, target, target_mode, None, None])
            row = rows[-1]
            if _read_at(parent, path.name, str(path)) != (current, current_mode):
                _refuse("Configuration changed since preflight: " + str(path))
            row[6] = _stage(parent, current)
            row[7] = _stage(parent, target)
        for row in rows:
            path, parent, current, current_mode, target, target_mode, backup, staged = row
            if _read_at(parent, path.name, str(path)) != (current, current_mode):
                _refuse("Configuration changed before publication: " + str(path))
            os.chmod(staged, target_mode, dir_fd=parent, follow_symlinks=False)
            # Record the attempted publication before the syscall: interruption
            # can arrive after replacement but before Python regains control.
            published.append(row)
            os.replace(staged, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            row[7] = None
            if _read_at(parent, path.name, str(path)) != (target, target_mode):
                _refuse("Published configuration failed byte or mode readback: " + str(path))
            os.fsync(parent)
    except BaseException as exc:
        rollback_errors = []
        for row in reversed(published):
            path, parent, current, current_mode, target, target_mode, backup, _ = row
            try:
                observed = _read_at(parent, path.name, str(path))
                if observed == (current, current_mode):
                    continue
                if observed != (target, target_mode):
                    # The publication no longer owns these bytes or this mode.
                    # Its saved plan remains the recovery backup; do not replace
                    # a third party's subsequent configuration with our baseline.
                    rollback_errors.append(str(path))
                    continue
                os.chmod(backup, current_mode, dir_fd=parent, follow_symlinks=False)
                os.replace(backup, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                row[6] = None
                if _read_at(parent, path.name, str(path)) != (current, current_mode):
                    rollback_errors.append(str(path))
                os.fsync(parent)
            except (OSError, ProtocolRefusal):
                rollback_errors.append(str(path))
        if rollback_errors:
            _refuse("Configuration publication failed and rollback incomplete; differing or unreadable foreign configurations were preserved. Retain the saved ownership plan and inspect its baseline backups before repairing: " + ", ".join(rollback_errors))
        if not isinstance(exc, OSError):
            raise
        _refuse("Atomic configuration publication failed; prior bytes were restored: " + str(exc))
    finally:
        for row in rows:
            for name in row[6:]:
                if name is not None:
                    try:
                        os.unlink(name, dir_fd=row[1])
                    except OSError:
                        pass
            os.close(row[1])


def apply(plan) -> None:
    """Add owned entries only if both existing configurations match the backups."""
    contents, _ = _plan(plan)
    changes = []
    for name in ("gateway", "codex"):
        row = plan[name]
        path = _path(row["path"])
        current, mode = _read(path)
        if (current, mode) != (contents[name + "_baseline"], row["mode"]):
            _refuse("Configuration changed after the seat plan was prepared: " + str(path))
        changes.append((path, current, mode, contents[name + "_added"], row["mode"]))
    _transaction(changes)


def _restore_changes(plan):
    contents, block = _plan(plan)
    changes = []
    for name in ("gateway", "codex"):
        row = plan[name]
        path = _path(row["path"])
        current, mode = _read(path)
        if current in (contents[name + "_baseline"], contents[name + "_removed"]):
            target, target_mode = contents[name + "_removed"], mode
        elif name == "gateway":
            document = _gateway(current)
            if plan["profile_name"] not in document["profiles"]:
                changes.append((path, current, mode, current, mode))
                continue
            if document["profiles"].get(plan["profile_name"]) != plan["profile"]:
                _refuse("Owned gateway profile is missing or changed: " + plan["profile_name"])
            del document["profiles"][plan["profile_name"]]
            target = contents[name + "_removed"] if current == contents[name + "_added"] else _json_bytes(document)
            target_mode = row["mode"] if current == contents[name + "_added"] and mode == row["mode"] else mode
        else:
            document = _toml(current)
            workspace = plan["profile"]["workspace_root"]
            if workspace not in document.get("projects", {}):
                changes.append((path, current, mode, current, mode))
                continue
            if document.get("projects", {}).get(workspace) != {"trust_level": "trusted"} or current.count(block) != 1:
                _refuse("Owned Codex trust block is missing or changed: " + workspace)
            target = contents[name + "_removed"] if current == contents[name + "_added"] else current.replace(block, b"", 1)
            _toml(target)
            target_mode = row["mode"] if current == contents[name + "_added"] and mode == row["mode"] else mode
        changes.append((path, current, mode, target, target_mode))
    return changes


def preflight_restore(plan) -> None:
    """Refuse changed ownership or malformed configs before any removal writes."""
    _restore_changes(plan)


def restore(plan) -> None:
    """Restore exact unchanged baselines, otherwise remove only owned entries."""
    _transaction([row for row in _restore_changes(plan) if (row[1], row[2]) != (row[3], row[4])])
