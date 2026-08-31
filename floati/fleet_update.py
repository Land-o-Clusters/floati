"""Read-only planning boundary for one explicit fleet-wide Floati update."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from .errors import IntegrityFailure, ProtocolRefusal
from .codex_hook_install import (
    _observe_waiter_generation_target,
    commit_waiter_rebind,
    plan_waiter_rebind,
    stage_waiter_runtime,
)
from .codex_hook_trust import codex_hook_current_hash, observe_codex_waiter_hooks, observe_rebound_waiter
from .deploy import DeploymentWriter, render_install_metadata
from . import wiring_journal
from .fleet_update_receipts import _action_rows, owner_review_batch_digest
from .fleet_update_registry import (
    planned_transport_registry_bytes,
    planned_transport_registry_sha256,
    rewrite_transport_pins,
)
from .git_process import fixed_git_command, fixed_git_environment
from .jsonl import read_records_snapshot
from .manifest import verify_manifest_inventory
from .registry import REGISTRY_KINDS, Registry
from .root import FloatiRoot, validate_identifier
from .waiter_bundle import waiter_runtime_digest


_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_ENCODER_PATHS = ("floati/events.py", "floati/records.py")
_BINDING_FIELDS = {"kind", "configuration", "store"}
_TRANSPORT_PIN_FIELDS = ("manifest_sha256", "source_sha")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fault(fault_hook: Optional[Callable[[str], None]], event: str) -> None:
    if fault_hook is not None:
        fault_hook(event)


def _shared_install_join_id(plan_digest: str, actor: str, idempotency_key: str) -> str:
    """Return the deterministic G2 shared-install journal coordinate."""

    return _sha256(json.dumps(
        {
            "actor": actor,
            "idempotency_key": idempotency_key,
            "plan_digest": plan_digest,
            "step_ordinal": 1,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii"))


def _metadata_install_state(destination: Path, expected_digest: str) -> tuple[bool, list[Dict[str, str]]]:
    """Return whether a complete managed installation is exactly one metadata state."""

    metadata = destination / ".floati-install" / "manifest.v0.json"
    try:
        raw, decoded = _strict_json_file(metadata, "fleet_update_install_readback_invalid")
    except (IntegrityFailure, ProtocolRefusal):
        return False, []
    if _sha256(raw) != expected_digest or not isinstance(decoded.get("files"), list):
        return False, []
    files: list[Dict[str, str]] = []
    for row in decoded["files"]:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            return False, []
        relative, digest = row.get("path"), row.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            return False, []
        path = destination / relative
        try:
            if path.is_symlink() or not path.is_file() or path.resolve(strict=True) != path:
                return False, []
            if _sha256(path.read_bytes()) != digest:
                return False, []
        except OSError:
            return False, []
        files.append({"path": relative, "sha256": digest})
    return True, files


def _target_manifest_entries(source: Path) -> list[Dict[str, str]]:
    """Re-read the exact source bytes which the governed writer is allowed to install."""

    _raw, manifest = _strict_json_file(
        source / "bundle-manifest.v0.json", "fleet_update_install_readback_invalid"
    )
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise ProtocolRefusal("fleet_update_install_readback_invalid", "target manifest has no file inventory")
    result: list[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ProtocolRefusal("fleet_update_install_readback_invalid", "target manifest file inventory is invalid")
        relative, digest = row["path"], row["sha256"]
        if not isinstance(relative, str) or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ProtocolRefusal("fleet_update_install_readback_invalid", "target manifest file inventory is invalid")
        path = source / relative
        if path.is_symlink() or not path.is_file() or _sha256(path.read_bytes()) != digest:
            raise ProtocolRefusal("fleet_update_install_readback_invalid", "target manifest file changed during recovery")
        result.append({"path": relative, "sha256": digest})
    return result


def _managed_paths_from_metadata(
    document: Dict[str, object], *, code: str
) -> list[str]:
    """Return one canonical sorted inventory from decoded install metadata."""

    rows = document.get("files")
    if not isinstance(rows, list) or not rows:
        raise ProtocolRefusal(code, "install metadata has no managed-file inventory")
    paths: list[str] = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "sha256"}
            or not isinstance(row.get("path"), str)
            or not isinstance(row.get("sha256"), str)
            or _SHA256.fullmatch(str(row["sha256"])) is None
        ):
            raise ProtocolRefusal(code, "install managed-file inventory is malformed")
        relative = str(row["path"])
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or not pure.parts
            or pure.as_posix() != relative
            or any(part in {"", ".", ".."} for part in pure.parts)
            or ".floati-install" in pure.parts
        ):
            raise ProtocolRefusal(code, "install managed-file path is invalid")
        paths.append(relative)
    if len(paths) != len(set(paths)):
        raise ProtocolRefusal(code, "install managed-file inventory repeats a path")
    return sorted(paths)


def _verify_plan_managed_inventory(
    destination: Path,
    plan: Dict[str, object],
) -> Dict[str, object]:
    """Bind the authenticated pre/post inventory witness to physical metadata."""

    raw, document = _strict_json_file(
        destination / ".floati-install" / "manifest.v0.json",
        "fleet_update_install_metadata_invalid",
    )
    observed_digest = _sha256(raw)
    observed_paths = _managed_paths_from_metadata(
        document, code="fleet_update_install_metadata_invalid"
    )
    current_paths = plan.get("current_managed_paths")
    intents = plan.get("shared_install_intents")
    if not isinstance(current_paths, list) or not isinstance(intents, list):
        raise ProtocolRefusal(
            "fleet_update_plan_invalid", "plan has no managed-file inventory witnesses"
        )
    target_paths: list[str] = []
    try:
        for intent in intents[:-1]:
            target_paths.append(
                Path(str(intent["path"])).relative_to(destination).as_posix()
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolRefusal(
            "fleet_update_plan_invalid", "plan install intent inventory is invalid"
        ) from exc
    if observed_digest == plan.get("current_manifest_sha256"):
        expected_paths = current_paths
    elif observed_digest == plan.get("target_manifest_sha256"):
        expected_paths = target_paths
    else:
        raise ProtocolRefusal(
            "fleet_update_plan_drift",
            "physical install metadata is outside the authenticated pre/post states",
        )
    if observed_paths != expected_paths:
        raise ProtocolRefusal(
            "fleet_update_plan_drift",
            "physical install inventory diverges from its authenticated witness",
        )
    return document


def _shared_install_recovery_evidence(
    destination: Path,
    source: Path,
    pre_digest: str,
    post_digest: str,
    join_id: str,
    planned_intents: object,
) -> Dict[str, object]:
    """Classify a shared install without mutation and prove any post-state journal join.

    A caller may write only from exact pre-state with no join.  Exact post-state
    is recoverable only when every managed byte and one chain-valid, contiguous
    writer segment is present.
    """

    if any(not isinstance(value, str) or _SHA256.fullmatch(value) is None for value in (pre_digest, post_digest, join_id)):
        raise ProtocolRefusal("fleet_update_install_recovery_invalid", "shared install recovery coordinates are invalid")
    if not isinstance(planned_intents, list) or not planned_intents:
        raise ProtocolRefusal(
            "fleet_update_install_recovery_invalid",
            "shared install recovery has no authenticated writer intents",
        )
    expected: list[Dict[str, str]] = []
    for row in planned_intents:
        if (
            not isinstance(row, dict)
            or set(row) != {"kind", "op", "path", "sha256"}
            or row.get("kind") != "file"
            or row.get("op") not in {"create", "replace"}
            or not isinstance(row.get("path"), str)
            or not Path(str(row["path"])).is_absolute()
            or not isinstance(row.get("sha256"), str)
            or _SHA256.fullmatch(str(row["sha256"])) is None
        ):
            raise ProtocolRefusal(
                "fleet_update_install_recovery_invalid",
                "shared install recovery intent is invalid",
            )
        expected.append({
            "kind": "file", "op": str(row["op"]),
            "path": str(row["path"]), "sha256": str(row["sha256"]),
        })
    metadata_path = destination / ".floati-install" / "manifest.v0.json"
    if expected[-1] != {
        "kind": "file", "op": "replace", "path": str(metadata_path),
        "sha256": post_digest,
    }:
        raise ProtocolRefusal(
            "fleet_update_install_recovery_invalid",
            "shared install recovery metadata intent is invalid",
        )
    pre_ok, _pre_files = _metadata_install_state(destination, pre_digest)
    post_ok, post_files = _metadata_install_state(destination, post_digest)
    target_entries: list[Dict[str, str]] = []
    for intent in expected[:-1]:
        try:
            relative = Path(intent["path"]).relative_to(destination).as_posix()
        except ValueError as exc:
            raise ProtocolRefusal(
                "fleet_update_install_recovery_invalid",
                "shared install recovery intent escapes its destination",
            ) from exc
        target_entries.append({"path": relative, "sha256": intent["sha256"]})
    if post_ok and post_files != target_entries:
        post_ok = False
    journal = wiring_journal.journal_path(destination)
    try:
        entries = wiring_journal.read_entries(journal) if journal.exists() else []
    except wiring_journal.WiringJournalCorrupt as exc:
        raise ProtocolRefusal("fleet_update_install_journal_invalid", "shared install wiring journal is corrupt") from exc
    segment = [entry for entry in entries if entry.payload.get("join_id") == join_id]
    if not segment:
        return {"phase": "pre" if pre_ok else "divergent", "evidence": None}
    contiguous = all(
        later.ordinal == earlier.ordinal + 1
        for earlier, later in zip(segment, segment[1:])
    )
    terminal = segment[-1].ordinal == len(entries)
    exact = len(segment) == len(expected) and contiguous and terminal and all(
        entry.payload.get("action") == "update"
        and entry.payload.get("kind") == intent["kind"]
        and entry.payload.get("path") == intent["path"]
        and entry.payload.get("sha256") == intent["sha256"]
        and entry.payload.get("op") == intent["op"]
        for entry, intent in zip(segment, expected)
    )
    if not post_ok or not exact:
        # A partial journal is useful only when every byte is the unique
        # pre/post state allowed by its ordinal.  In particular, a durable
        # metadata digest still authenticates the old plan while files are
        # being replaced; a valid-looking but different metadata document
        # must be rejected before the writer can append or copy anything.
        metadata = metadata_path
        try:
            pre_raw, pre_document = _strict_json_file(
                metadata, "fleet_update_install_readback_invalid"
            )
        except (IntegrityFailure, ProtocolRefusal):
            return {"phase": "divergent", "evidence": None}
        metadata_digest = _sha256(pre_raw)
        if metadata_digest not in {pre_digest, post_digest} or not isinstance(pre_document.get("files"), list):
            return {"phase": "divergent", "evidence": None}
        pre_files: dict[str, str] = {}
        for row in pre_document["files"]:
            if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
                return {"phase": "divergent", "evidence": None}
            relative, digest = row["path"], row["sha256"]
            if not isinstance(relative, str) or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                return {"phase": "divergent", "evidence": None}
            pre_files[relative] = digest
        prefix_exact = (
            segment and contiguous and terminal and len(segment) <= len(expected) and all(
                entry.payload.get("action") == "update"
                and entry.payload.get("kind") == intent["kind"]
                and entry.payload.get("path") == intent["path"]
                and entry.payload.get("sha256") == intent["sha256"]
                and entry.payload.get("op") == intent["op"]
                for entry, intent in zip(segment, expected)
            )
        )
        if prefix_exact:
            prefix_length = len(segment)
            if prefix_length <= len(target_entries) and metadata_digest != pre_digest:
                return {"phase": "divergent", "evidence": None}
            states_valid = True
            for index, row in enumerate(target_entries):
                path = destination / row["path"]
                try:
                    digest = _sha256(path.read_bytes()) if path.is_file() and not path.is_symlink() else None
                except OSError:
                    digest = None
                pre_digest_for_file = pre_files.get(row["path"])
                pre_state = digest is None if pre_digest_for_file is None else digest == pre_digest_for_file
                post_state = digest == row["sha256"]
                if index < prefix_length - 1:
                    states_valid = states_valid and post_state
                elif index == prefix_length - 1:
                    states_valid = states_valid and (pre_state or post_state)
                else:
                    states_valid = states_valid and pre_state
            if prefix_length <= len(target_entries):
                states_valid = states_valid and metadata_digest == pre_digest
            else:
                states_valid = states_valid and metadata_digest in {pre_digest, post_digest}
            if states_valid:
                return {"phase": "partial", "evidence": None}
        return {"phase": "divergent", "evidence": None}
    first = segment[0]
    predecessor = entries[first.ordinal - 2] if first.ordinal > 1 else None
    return {
        "phase": "post",
        "evidence": {
            "kind": "shared_install",
            "journal_path": str(journal),
            "join_id": join_id,
            "predecessor_ordinal": predecessor.ordinal if predecessor is not None else None,
            "predecessor_entry_hash": predecessor.payload["entryHash"] if predecessor is not None else None,
            "first_ordinal": first.ordinal,
            "last_ordinal": segment[-1].ordinal,
            "entry_hashes": [entry.payload["entryHash"] for entry in segment],
        },
    }


def _waiter_recovery_evidence(staged: Dict[str, object]) -> Dict[str, object]:
    """Classify one staged hook document without writing trust or hook bytes."""

    required = {"configuration", "before", "after", "hook_trust_key", "target_hook_hash"}
    if not isinstance(staged, dict) or not required <= set(staged):
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter recovery staging is invalid")
    path = Path(str(staged["configuration"]))
    before, after = staged["before"], staged["after"]
    if not isinstance(before, bytes) or not isinstance(after, bytes):
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter recovery bytes are invalid")
    try:
        observed = path.read_bytes()
    except OSError as exc:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter hook document is unreadable") from exc
    if observed == before and before != after:
        return {"phase": "pre", "evidence": None}
    expected_hash = str(staged["target_hook_hash"] if observed == after else "")
    if observed != after:
        return {"phase": "divergent", "evidence": None}
    observation = observe_rebound_waiter(
        path, expected_key=str(staged["hook_trust_key"]), expected_hash=expected_hash
    )
    evidence = {
        "kind": "waiter_binding",
        "hook_post_observation": {
            "hook_trust_key": observation["hook_trust_key"],
            "current_hook_hash": observation["hook_trust_current_hash"],
            "observed_trusted_hash": observation["hook_trust_observed_hash"],
            "observed_enabled": observation["hook_enabled"],
        },
    }
    return {"phase": "unchanged" if before == after else "post", "evidence": evidence}


def _unique_object(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_file(path: Path, code: str) -> Tuple[bytes, Dict[str, object]]:
    selected = _canonical_file(path, code)
    try:
        payload = selected.read_bytes()
        decoded = json.loads(payload, object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise IntegrityFailure(code, f"strict JSON is unreadable at {selected}") from exc
    if not isinstance(decoded, dict):
        raise IntegrityFailure(code, f"strict JSON must be an object at {selected}")
    return payload, decoded


def _canonical_file(path: Path, code: str) -> Path:
    selected = Path(path)
    try:
        if (
            not selected.is_absolute()
            or selected.is_symlink()
            or not selected.is_file()
            or selected.resolve(strict=True) != selected
        ):
            raise OSError("not one canonical ordinary file")
    except OSError as exc:
        raise ProtocolRefusal(code, f"expected one canonical absolute file at {selected}") from exc
    return selected


def _canonical_directory(path: Path, code: str) -> Path:
    selected = Path(path)
    try:
        if (
            not selected.is_absolute()
            or selected.is_symlink()
            or not selected.is_dir()
            or selected.resolve(strict=True) != selected
        ):
            raise OSError("not one canonical ordinary directory")
    except OSError as exc:
        raise ProtocolRefusal(code, f"expected one canonical absolute directory at {selected}") from exc
    return selected


def _lexical_path(value: object, code: str) -> Path:
    """Accept only one absolute Path coordinate without touching its filesystem object."""

    if not isinstance(value, Path) or not value.is_absolute():
        raise ProtocolRefusal(code, "path must be one absolute Path coordinate")
    return value


def _lexical_safe_absolute_path(value: object, code: str) -> Path:
    """Validate one schema-v0 absolute path string before filesystem access."""

    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or not Path(value).is_absolute()
        or any(
            unicodedata.category(character) in {"Cc", "Cs"}
            or unicodedata.bidirectional(character)
            in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
            for character in value
        )
    ):
        raise ProtocolRefusal(code, code)
    return Path(value)


def _lexical_inputs(root: object, actor: object, destination: object, target_source: object, target_source_sha: object, channel: object, version: object, binding_path: object, transport_registry: object, transport_name: object) -> tuple[FloatiRoot, str, Path, Path, str, str, str, Path, Path, str]:
    """Validate every public scalar/path form before any stats, reads, or receipts."""

    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal("fleet_update_root_invalid", "planner requires one validated Floati root")
    selected_actor = validate_identifier(actor, "actor")
    selected_destination = _lexical_path(destination, "fleet_update_destination_invalid")
    selected_source = _lexical_path(target_source, "fleet_update_target_invalid")
    selected_source_sha = _validate_source_sha(target_source_sha)
    selected_channel = _validate_channel(channel)
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ProtocolRefusal("fleet_update_version_invalid", "version must be one bounded terminal-safe value")
    selected_binding = _lexical_path(binding_path, "fleet_update_binding_invalid")
    selected_registry = _lexical_path(transport_registry, "fleet_update_transport_registry_invalid")
    selected_transport = validate_identifier(transport_name, "transport")
    return root, selected_actor, selected_destination, selected_source, selected_source_sha, selected_channel, version, selected_binding, selected_registry, selected_transport


def _validate_channel(channel: object) -> str:
    if not isinstance(channel, str) or not 1 <= len(channel) <= 2048:
        raise ProtocolRefusal("update_channel_invalid", "update channel must be one bounded HTTPS URL")
    try:
        parts = urlsplit(channel)
        port = parts.port
    except ValueError as exc:
        raise ProtocolRefusal("update_channel_invalid", "update channel is not a valid HTTPS URL") from exc
    if (
        parts.scheme != "https"
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or not parts.path.startswith("/")
        or (port is not None and not 1 <= port <= 65535)
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in channel)
    ):
        raise ProtocolRefusal("update_channel_invalid", "update channel must be exact HTTPS with a host and path")
    return channel


def _validate_source_sha(value: object) -> str:
    if not isinstance(value, str) or _SHA1.fullmatch(value) is None:
        raise ProtocolRefusal("fleet_update_source_sha_invalid", "target source SHA must be lowercase 40-hex")
    return value


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        if path.is_symlink():
            raise ProtocolRefusal("fleet_update_waiter_invalid", f"waiter tree contains a symlink at {path}")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _selected_tree_digest(source: Path, relative_paths: Iterable[str]) -> str:
    digest = hashlib.sha256()
    found = 0
    for relative in sorted(set(relative_paths)):
        path = source / relative
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ProtocolRefusal("fleet_update_target_invalid", f"target runtime path is not an ordinary file: {relative}")
        found += 1
        digest.update(relative.encode("utf-8") + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    if found == 0:
        raise ProtocolRefusal("fleet_update_target_invalid", "target has no waiter runtime files")
    return digest.hexdigest()


def _encoder_digest(source: Path) -> str:
    digest = hashlib.sha256()
    for relative in _ENCODER_PATHS:
        path = source / relative
        if path.is_symlink() or not path.is_file():
            raise ProtocolRefusal("fleet_update_encoder_invalid", f"encoder file is absent or unsafe: {relative}")
        digest.update(relative.encode("ascii") + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _load_bindings(path: Path) -> Tuple[str, List[Dict[str, str]]]:
    payload, decoded = _strict_json_file(path, "fleet_update_binding_invalid")
    if (
        set(decoded) != {"schema_version", "bindings"}
        or type(decoded.get("schema_version")) is not int
        or decoded.get("schema_version") != 0
    ):
        raise ProtocolRefusal("fleet_update_binding_invalid", "binding inventory has an unsupported shape")
    raw_bindings = decoded.get("bindings")
    if not isinstance(raw_bindings, list) or not 1 <= len(raw_bindings) <= 1024:
        raise ProtocolRefusal("fleet_update_binding_invalid", "binding inventory must name at least one binding")
    bindings: List[Dict[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for raw in raw_bindings:
        if not isinstance(raw, dict) or set(raw) != _BINDING_FIELDS or raw.get("kind") != "codex_stop_hook":
            raise ProtocolRefusal("fleet_update_binding_invalid", "binding entry has an unsupported shape")
        configuration = _canonical_file(
            _lexical_safe_absolute_path(
                raw.get("configuration"), "fleet_update_binding_invalid"
            ),
            "fleet_update_binding_invalid",
        )
        store = _canonical_directory(
            _lexical_safe_absolute_path(
                raw.get("store"), "fleet_update_binding_invalid"
            ),
            "fleet_update_binding_invalid",
        )
        coordinate = (str(configuration), str(store))
        if coordinate in seen:
            raise ProtocolRefusal("fleet_update_binding_invalid", "binding inventory repeats a coordinate")
        seen.add(coordinate)
        bindings.append({"kind": "codex_stop_hook", "configuration": str(configuration), "store": str(store)})
    bindings.sort(key=lambda row: (row["configuration"], row["store"]))
    return _sha256(payload), bindings


def _waiter_from_configuration(configuration: Path, store: Path) -> Tuple[bytes, Path, str]:
    payload, decoded = _strict_json_file(configuration, "fleet_update_waiter_configuration_invalid")
    try:
        stop = decoded["hooks"]
        rows = stop["Stop"] if isinstance(stop, dict) else None
    except (KeyError, TypeError):
        rows = None
    candidates: List[Path] = []
    if isinstance(rows, list):
        for group in rows:
            hooks = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(hooks, list):
                continue
            for hook in hooks:
                command = hook.get("command") if isinstance(hook, dict) else None
                if not isinstance(command, str):
                    continue
                try:
                    words = shlex.split(command)
                except ValueError:
                    continue
                for word in words:
                    candidate = Path(word)
                    if candidate.name == "floati-codex-wait" and candidate.is_absolute():
                        candidates.append(candidate)
    if len(candidates) != 1:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "configuration must name exactly one Floati Stop waiter")
    launcher = _canonical_file(candidates[0], "fleet_update_waiter_binding_invalid")
    tree = launcher.parent.parent
    try:
        relative = tree.relative_to(store)
    except ValueError as exc:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter launcher is outside its declared store") from exc
    if len(relative.parts) != 1 or _SHA256.fullmatch(relative.name) is None:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter launcher is not in one named digest directory")
    return payload, tree, relative.name


def _validate_binding_for_root(root: FloatiRoot, binding: Dict[str, str]) -> None:
    """Validate every explicit hook coordinate before workspace-map projection."""

    configuration, store = Path(binding["configuration"]), Path(binding["store"])
    _payload, tree, _named = _waiter_from_configuration(configuration, store)
    _configuration_payload, document = _strict_json_file(configuration, "fleet_update_waiter_binding_invalid")
    commands: list[list[str]] = []
    try:
        groups = document["hooks"]["Stop"]
    except (KeyError, TypeError) as exc:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "configuration has no valid Floati waiter root") from exc
    if not isinstance(groups, list):
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "configuration has no valid Floati waiter root")
    for group in groups:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            command = hook.get("command") if isinstance(hook, dict) else None
            if not isinstance(command, str):
                continue
            try:
                words = shlex.split(command)
            except ValueError as exc:
                raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "configuration has no valid Floati waiter root") from exc
            if any(Path(word).name == "floati-codex-wait" and Path(word).is_absolute() for word in words):
                commands.append(words)
    if len(commands) != 1 or commands[0].count("--root") != 1:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "configuration has no unique Floati waiter root")
    root_index = commands[0].index("--root")
    if root_index + 1 >= len(commands[0]) or commands[0][root_index + 1] != str(root.path):
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "configuration waiter does not name this exact fleet root")
    launcher = tree / "scripts" / "floati-codex-wait"
    if str(launcher) not in commands[0]:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "configuration waiter launcher does not match its declared store")
    if len(observe_codex_waiter_hooks(configuration)) != 1:
        raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "configuration has no unique Floati hook trust coordinate")


def _validated_target_manifest(target_source: Path, target_source_sha: str) -> Dict[str, object]:
    _payload, manifest = _strict_json_file(target_source / "bundle-manifest.v0.json", "fleet_update_target_manifest_invalid")
    if (
        set(manifest) != {"schema_version", "protocol_version", "canonical_ref", "files", "source_sha"}
        or manifest.get("schema_version") != 0
        or manifest.get("source_sha") != target_source_sha
        or not isinstance(manifest.get("canonical_ref"), str)
        or not manifest.get("canonical_ref")
        or any(character.isspace() for character in str(manifest.get("canonical_ref")))
        or not isinstance(manifest.get("files"), list)
    ):
        raise ProtocolRefusal("fleet_update_target_manifest_invalid", "target manifest has an unsupported or mismatched identity")
    files = manifest["files"]
    inventory_errors = verify_manifest_inventory(target_source, files)
    if inventory_errors:
        raise ProtocolRefusal(
            "fleet_update_target_manifest_invalid",
            f"target manifest inventory is invalid: {inventory_errors[0]}",
        )
    for row in files:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ProtocolRefusal("fleet_update_target_manifest_invalid", "target manifest entry is invalid")
        relative = row.get("path")
        expected = row.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
            raise ProtocolRefusal("fleet_update_target_manifest_invalid", "target manifest entry is invalid")
        path = target_source / relative
        if path.is_symlink() or not path.is_file() or _sha256(path.read_bytes()) != expected:
            raise ProtocolRefusal("fleet_update_target_manifest_invalid", f"target manifest digest mismatch: {relative}")
    return manifest


def _planned_install_metadata(
    current: Dict[str, object],
    target: Dict[str, object],
    target_source: Path,
    destination: Path,
) -> bytes:
    schema_version = current.get("schema_version")
    if isinstance(schema_version, bool) or schema_version not in {0, 1}:
        raise ProtocolRefusal("fleet_update_install_metadata_invalid", "installed metadata schema is unsupported")
    expected = {"schema_version", "source_ref", "source_sha", "files"}
    if schema_version == 1:
        expected.add("ownership")
    if set(current) != expected:
        raise ProtocolRefusal("fleet_update_install_metadata_invalid", "installed metadata has an unexpected shape")
    previous_ownership = current.get("ownership") if schema_version == 1 else None
    if schema_version == 1 and not isinstance(previous_ownership, dict):
        raise ProtocolRefusal("fleet_update_install_metadata_invalid", "installed ownership metadata is invalid")
    entrypoint = target_source / "scripts" / "floati"
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise ProtocolRefusal("fleet_update_target_manifest_invalid", "target entrypoint is unavailable")
    try:
        return render_install_metadata(
            destination=destination,
            source_ref="HEAD",
            source_sha=str(target["source_sha"]),
            entries=target["files"],
            entrypoint_sha256=_sha256(entrypoint.read_bytes()),
            previous_ownership=previous_ownership,
        )
    except ProtocolRefusal as exc:
        raise ProtocolRefusal("fleet_update_install_metadata_invalid", exc.detail) from exc


def _reader_consequences(
    *,
    current: Dict[str, object],
    target_bytes: bytes,
    registry_path: Path,
    transport_name: str,
    manifest_path: Path,
) -> list[Dict[str, object]]:
    try:
        target = json.loads(target_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityFailure("fleet_update_install_metadata_invalid", "projected metadata is unreadable") from exc
    current_schema = current.get("schema_version")
    target_schema = target.get("schema_version") if isinstance(target, dict) else None
    if (
        isinstance(current_schema, bool)
        or isinstance(target_schema, bool)
        or not isinstance(current_schema, int)
        or not isinstance(target_schema, int)
    ):
        raise ProtocolRefusal("fleet_update_install_metadata_invalid", "manifest vocabulary has an invalid schema version")
    current_fields = set(current)
    target_fields = set(target)
    added = sorted(target_fields - current_fields)
    removed = sorted(current_fields - target_fields)
    if current_schema == target_schema and not added and not removed:
        return []
    if removed or not added or target_schema <= current_schema:
        raise ProtocolRefusal("fleet_update_manifest_vocabulary_invalid", "installed manifest vocabulary transition is not additive")
    return [{
        "reader": "codex_fleet_bus_gateway",
        "surface": "install_manifest",
        "registry": str(registry_path),
        "transport": transport_name,
        "manifest_path": str(manifest_path),
        "current_schema_version": current_schema,
        "target_schema_version": target_schema,
        "added_fields": added,
        "removed_fields": removed,
        "change": "additive_widened",
        "compatibility_after_update": "not_observed",
        "remedy": "review the Codex fleet gateway reader before applying the widened manifest vocabulary",
    }]


def _canonical_plan_digest(plan: Dict[str, object]) -> str:
    encoded = json.dumps(plan, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("ascii")
    return _sha256(encoded)


def _explicit_executable(value: object, code: str) -> str:
    """Accept one caller-supplied executable; fleet updates never resolve PATH."""

    if not isinstance(value, (str, Path)):
        raise ProtocolRefusal(code, "executable must be an explicit absolute path")
    path = Path(value)
    try:
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or path.resolve(strict=True) != path
            or not os.access(path, os.X_OK)
        ):
            raise OSError("not one canonical executable")
    except OSError as exc:
        raise ProtocolRefusal(code, "executable must be an explicit canonical executable") from exc
    return str(path)


def _require_detached_source(source: Path, git: str, expected_sha: str) -> None:
    """Bind G2 to one detached, explicit-Git source commit before mutation."""

    environment = fixed_git_environment(git)
    try:
        head = subprocess.run(
            fixed_git_command(
                git, source, ("rev-parse", "--verify", "HEAD^{commit}")
            ),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        attached = subprocess.run(
            fixed_git_command(git, source, ("symbolic-ref", "-q", "HEAD")),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        dirty = subprocess.run(
            fixed_git_command(git, source, ("status", "--porcelain")),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProtocolRefusal("fleet_update_source_sha_invalid", "explicit Git could not verify detached target source") from exc
    if head.returncode != 0 or head.stdout.strip() != expected_sha:
        raise ProtocolRefusal("fleet_update_source_sha_invalid", "target source HEAD does not equal the planned SHA")
    if attached.returncode == 0:
        raise ProtocolRefusal("fleet_update_source_sha_invalid", "target source must be detached at the planned SHA")
    if dirty.returncode != 0 or dirty.stdout:
        raise ProtocolRefusal("fleet_update_source_sha_invalid", "target source must be clean at the planned SHA")


def _governed_installer_shadow_path(destination: Path, installer: str) -> str:
    """Construct the only PATH observation used by a G2 writer.

    The destination's installed command directory is deliberately first so the
    shadow observer can establish its authoritative boundary; the sole caller
    supplied executable contributes only its canonical parent directory.
    """

    return os.pathsep.join((str(destination / "scripts"), str(Path(installer).parent)))


def _verify_waiter_generation_targets(
    bindings: object, target_waiter_digest: object
) -> None:
    """Reject every non-directory/divergent generation leaf without writing."""

    if (
        not isinstance(bindings, list)
        or not bindings
        or not isinstance(target_waiter_digest, str)
        or _SHA256.fullmatch(target_waiter_digest) is None
    ):
        raise ProtocolRefusal(
            "fleet_update_plan_invalid",
            "G2 waiter target set is not canonical",
        )
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ProtocolRefusal(
                "fleet_update_plan_invalid", "G2 stage binding is invalid"
            )
        try:
            store = _canonical_directory(
                Path(str(binding["store"])),
                "fleet_update_waiter_binding_invalid",
            )
        except (KeyError, TypeError) as exc:
            raise ProtocolRefusal(
                "fleet_update_plan_invalid",
                "G2 stage binding lacks coordinates",
            ) from exc
        _observe_waiter_generation_target(store, target_waiter_digest)


def stage_fleet_update(
    *,
    plan: Dict[str, object],
    target_source: Path,
    installer_executable: str | Path,
    git_executable: str | Path,
    _authenticated_shared_join_id: Optional[str] = None,
    _root: Optional[FloatiRoot] = None,
    _authenticated_ledger: Optional[object] = None,
    _execution_token: Optional[object] = None,
    _authenticated_idempotency_key: Optional[str] = None,
) -> Dict[str, object]:
    """Verify every G2 target and produce immutable commit instructions.

    This stage is pure: it validates the supplied source through the same
    DeploymentWriter preflight and computes exact hook-byte replacements. It
    never creates a waiter generation, binds a hook, or rewrites the shared
    installation.
    """

    if not isinstance(plan, dict):
        raise ProtocolRefusal("fleet_update_plan_invalid", "G2 staging requires one exact plan")
    from .fleet_update_receipts import (
        FleetUpdateReceiptLedger,
        _reconcile_owner_review_physical,
        authenticate_plan,
    )

    plan_inputs = plan.get("inputs")
    if not isinstance(plan_inputs, dict):
        raise ProtocolRefusal(
            "fleet_update_plan_invalid", "G2 stage plan lacks named coordinates"
        )
    selected_root = _root
    if selected_root is None:
        try:
            selected_root = FloatiRoot.open_direct_home(
                Path(str(plan_inputs["root"])), create=False
            )
        except (KeyError, TypeError) as exc:
            raise ProtocolRefusal(
                "fleet_update_plan_invalid", "G2 stage plan lacks its root coordinate"
            ) from exc
    selected_actor = plan_inputs.get("actor")
    plan = authenticate_plan(plan, selected_actor, selected_root)
    source = _canonical_directory(Path(target_source), "fleet_update_target_invalid")
    installer = _explicit_executable(installer_executable, "fleet_update_installer_invalid")
    git = _explicit_executable(git_executable, "fleet_update_git_invalid")
    target_sha = plan.get("target_source_sha")
    destination_value = plan.get("inputs", {}).get("destination") if isinstance(plan.get("inputs"), dict) else None
    target_waiter_digest = plan.get("target_waiter_digest")
    bindings = plan.get("waiter_bindings")
    if (
        not isinstance(target_sha, str)
        or _SHA1.fullmatch(target_sha) is None
        or not isinstance(destination_value, str)
        or not isinstance(target_waiter_digest, str)
        or _SHA256.fullmatch(target_waiter_digest) is None
        or not isinstance(bindings, list)
        or not bindings
    ):
        raise ProtocolRefusal("fleet_update_plan_invalid", "G2 stage plan lacks exact install or waiter coordinates")
    destination = _canonical_directory(Path(destination_value), "fleet_update_destination_invalid")
    manifest = _validated_target_manifest(source, target_sha)
    _require_detached_source(source, git, target_sha)
    if waiter_runtime_digest(source) != target_waiter_digest:
        raise ProtocolRefusal("fleet_update_waiter_invalid", "target waiter runtime digest diverges from the plan")
    current_install_metadata = _verify_plan_managed_inventory(destination, plan)
    writer = DeploymentWriter(
        source,
        destination,
        "update",
        ref="HEAD",
        committed_tree=True,
        installer_path=_governed_installer_shadow_path(destination, installer),
        git_executable=git,
        planned_intents=plan.get("shared_install_intents"),
    )
    if _authenticated_shared_join_id is None:
        install_stage = writer.stage()
    else:
        if _SHA256.fullmatch(_authenticated_shared_join_id) is None:
            raise ProtocolRefusal(
                "fleet_update_install_recovery_invalid",
                "authenticated shared-install join coordinate is invalid",
            )
        recovery = _shared_install_recovery_evidence(
            destination,
            source,
            str(plan.get("current_manifest_sha256")),
            str(plan.get("target_manifest_sha256")),
            _authenticated_shared_join_id,
            plan.get("shared_install_intents"),
        )
        if recovery["phase"] == "divergent":
            raise ProtocolRefusal(
                "fleet_update_install_recovery_invalid",
                "authenticated shared install is outside its exact pre/join/post states",
            )
        # Joined recovery replaces only destination collision validation.  The
        # same explicit Git and complete manifest inventory are still checked
        # through the writer's ordinary source stage before any new action.
        install_stage = (
            writer.stage()
            if recovery["phase"] == "pre"
            else writer.stage_source()
        )
    if install_stage["source_sha"] != target_sha:
        raise ProtocolRefusal("fleet_update_source_sha_invalid", "staged source does not equal the planned source SHA")
    current_rows = current_install_metadata.get("files")
    target_rows = manifest.get("files")
    if not isinstance(current_rows, list) or not isinstance(target_rows, list):
        raise ProtocolRefusal("fleet_update_install_metadata_invalid", "install metadata has no exact managed-file inventory")
    current_paths = {
        row.get("path") for row in current_rows
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    }
    target_paths = {
        row.get("path") for row in target_rows
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    }
    if len(current_paths) != len(current_rows) or len(target_paths) != len(target_rows):
        raise ProtocolRefusal("fleet_update_install_metadata_invalid", "install managed-file inventory is malformed")
    if current_paths - target_paths:
        raise ProtocolRefusal(
            "fleet_update_install_stale_removal_unsupported",
            "G2 refuses a target manifest that would remove a previously managed file",
        )
    expected_metadata = _planned_install_metadata(
        current_install_metadata,
        manifest,
        source,
        destination,
    )
    if _sha256(expected_metadata) != plan.get("target_manifest_sha256"):
        raise ProtocolRefusal("fleet_update_plan_drift", "staged install metadata diverges from the plan")
    # Verify all pre-existing immutable targets before any binding can reach
    # the later materialization boundary.
    _verify_waiter_generation_targets(bindings, target_waiter_digest)

    waiter_stages: list[Dict[str, object]] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ProtocolRefusal("fleet_update_plan_invalid", "G2 stage binding is invalid")
        try:
            configuration = _canonical_file(Path(str(binding["configuration"])), "fleet_update_waiter_binding_invalid")
            store = _canonical_directory(Path(str(binding["store"])), "fleet_update_waiter_binding_invalid")
            before, current_tree, _named = _waiter_from_configuration(configuration, store)
        except (KeyError, TypeError) as exc:
            raise ProtocolRefusal("fleet_update_plan_invalid", "G2 stage binding lacks coordinates") from exc
        current_configuration_digest = _sha256(before)
        planned_pre = binding.get("configuration_sha256")
        planned_post = binding.get("target_configuration_sha256")
        if (
            not isinstance(planned_pre, str) or _SHA256.fullmatch(planned_pre) is None
            or not isinstance(planned_post, str) or _SHA256.fullmatch(planned_post) is None
        ):
            raise ProtocolRefusal("fleet_update_plan_invalid", "G2 stage binding lacks planned document identity")
        current_digest = waiter_runtime_digest(current_tree)
        staged = plan_waiter_rebind(configuration, store, target_waiter_digest)
        if current_configuration_digest == planned_pre:
            if current_digest != binding.get("current_tree_digest") or _sha256(staged["after"]) != planned_post:
                raise ProtocolRefusal("fleet_update_plan_drift", "waiter binding changed after preview")
        elif current_configuration_digest == planned_post:
            if current_digest != target_waiter_digest or _sha256(staged["after"]) != planned_post:
                raise ProtocolRefusal("fleet_update_plan_drift", "waiter post state diverges from the plan")
            staged["recovery_post"] = True
        else:
            raise ProtocolRefusal("fleet_update_plan_drift", "waiter binding changed after preview")
        staged["planned_pre_digest"] = planned_pre
        staged["planned_post_digest"] = planned_post
        trust_rows = observe_codex_waiter_hooks(configuration)
        if len(trust_rows) != 1 or trust_rows[0].get("hook_trust_current_hash") != staged["current_hook_hash"]:
            raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "waiter trust coordinate diverges during staging")
        staged["hook_trust_key"] = trust_rows[0]["hook_trust_key"]
        staged["pre_trust_observation"] = {
            "hook_trust_key": trust_rows[0]["hook_trust_key"],
            "current_hook_hash": trust_rows[0]["hook_trust_current_hash"],
            "observed_trusted_hash": trust_rows[0]["hook_trust_observed_hash"],
            "observed_enabled": trust_rows[0]["hook_enabled"],
        }
        target_launcher = Path(str(staged["target_launcher"]))
        if target_launcher.parent.parent != store / target_waiter_digest:
            raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "target waiter launcher is outside its declared digest directory")
        waiter_stages.append(staged)
    # Specific coordinate/document checks above retain their typed ordering;
    # owner review then refines those valid binding phases against the raw
    # physical hook facts authenticated by the plan.
    context_values = (
        _authenticated_ledger,
        _execution_token,
        _authenticated_idempotency_key,
    )
    if any(value is not None for value in context_values):
        if (
            not isinstance(_authenticated_ledger, FleetUpdateReceiptLedger)
            or _authenticated_ledger.root is not selected_root
            or _execution_token is None
            or not isinstance(_authenticated_idempotency_key, str)
        ):
            raise ProtocolRefusal(
                "fleet_update_execution_lock_invalid",
                "authenticated staging saga context is incomplete",
            )
        _authenticated_ledger._require_execution_token(_execution_token)
        reconciliation_rows = _authenticated_ledger.rows(str(selected_actor))
        reconciliation_actor: Optional[str] = str(selected_actor)
        reconciliation_key: Optional[str] = _authenticated_idempotency_key
    else:
        reconciliation_rows = None
        reconciliation_actor = None
        reconciliation_key = None
    _reconcile_owner_review_physical(
        plan,
        selected_root,
        rows=reconciliation_rows,
        actor=reconciliation_actor,
        key=reconciliation_key,
        allow_unreceipted_post=True,
    )
    return {
        "source": str(source),
        "installer_executable": installer,
        "git_executable": git,
        "install": {
            "pre_digest": str(plan["current_manifest_sha256"]),
            "post_digest": str(plan["target_manifest_sha256"]),
        },
        "waiters": waiter_stages,
    }


def commit_fleet_update_g2(
    *,
    plan: Dict[str, object],
    root: FloatiRoot,
    actor: str,
    idempotency_key: str,
    target_source: Path,
    installer_executable: str | Path,
    git_executable: str | Path,
    _fault_hook: Optional[Callable[[str], None]] = None,
) -> Dict[str, object]:
    """Commit only G2's shared-install and waiter-binding saga steps.

    Transport pins and epoch work remain deliberately absent for later gates.
    Exact retries use receipt evidence and avoid a second writer invocation.
    """

    if not isinstance(plan, dict):
        raise ProtocolRefusal("fleet_update_plan_invalid", "G2 commit requires one exact plan")
    if _fault_hook is not None and not callable(_fault_hook):
        raise ProtocolRefusal(
            "fleet_update_fault_hook_invalid",
            "internal G2 fault hook must be callable when supplied",
        )
    from .fleet_update_receipts import (
        FleetUpdateReceiptLedger,
        _FleetUpdateExecutionGuard,
        authenticate_plan,
    )

    authenticate_plan(plan, actor, root)
    inputs = plan.get("inputs")
    if not isinstance(inputs, dict):
        raise ProtocolRefusal("fleet_update_plan_invalid", "G2 commit plan lacks named coordinates")
    ledger = FleetUpdateReceiptLedger(root)
    # Pure staging is allowed before exclusion, but none of its observations
    # authorize a later mutation.  Re-run the whole decision after acquiring
    # the root inode and use only that guarded result below.
    _select_fleet_update_g2_execution(
        plan=plan,
        root=root,
        actor=actor,
        idempotency_key=idempotency_key,
        target_source=target_source,
        ledger=ledger,
    )
    with _FleetUpdateExecutionGuard(root) as execution_token:
        ledger._root_has_active_saga(actor)
        fresh, join_id, has_authenticated_start = _select_fleet_update_g2_execution(
            plan=plan,
            root=root,
            actor=actor,
            idempotency_key=idempotency_key,
            target_source=target_source,
            ledger=ledger,
        )
        staged = stage_fleet_update(
            plan=fresh,
            target_source=target_source,
            installer_executable=installer_executable,
            git_executable=git_executable,
            _authenticated_shared_join_id=(
                join_id if has_authenticated_start else None
            ),
            _root=root,
            _authenticated_ledger=ledger,
            _execution_token=execution_token,
            _authenticated_idempotency_key=idempotency_key,
        )
        return _commit_fleet_update_g2_guarded(
            fresh=fresh,
            staged=staged,
            join_id=join_id,
            ledger=ledger,
            actor=actor,
            idempotency_key=idempotency_key,
            execution_token=execution_token,
            fault_hook=_fault_hook,
        )


def _select_fleet_update_g2_execution(
    *,
    plan: Dict[str, object],
    root: FloatiRoot,
    actor: str,
    idempotency_key: str,
    target_source: Path,
    ledger: object,
) -> tuple[Dict[str, object], str, bool]:
    """Recompute every observation which selects one G2 plan and join."""

    inputs = plan.get("inputs")
    if not isinstance(inputs, dict):
        raise ProtocolRefusal(
            "fleet_update_plan_invalid", "G2 commit plan lacks named coordinates"
        )
    existing_rows = ledger.rows(actor)
    has_authenticated_start = any(
        row.get("kind") == "fleet_update_started"
        and row.get("plan_digest") == plan.get("plan_digest")
        and row.get("idempotency_key") == idempotency_key
        and row.get("actor") == actor
        for row in existing_rows
    )
    if has_authenticated_start:
        # A resumed saga must compare against the immutable authorized plan:
        # its install coordinate is intentionally already at the planned post
        # state, so re-previewing it as a fresh proposal would be dishonest.
        fresh = plan
    else:
        fresh = preview_fleet_update(
            root=root,
            actor=actor,
            destination=Path(str(inputs.get("destination", ""))),
            target_source=Path(target_source),
            target_source_sha=plan.get("target_source_sha"),
            channel=inputs.get("channel"),
            version=inputs.get("version"),
            binding_path=Path(str(inputs.get("waiter_binding", ""))),
            transport_registry=Path(str(inputs.get("transport_registry", ""))),
            transport_name=inputs.get("transport"),
        )
        if fresh["plan_digest"] != plan.get("plan_digest"):
            raise ProtocolRefusal("fleet_update_plan_drift", "G2 commit plan no longer matches every observed coordinate")
    join_id = _shared_install_join_id(
        str(fresh["plan_digest"]), actor, idempotency_key
    )
    return fresh, join_id, has_authenticated_start


def _commit_fleet_update_g2_guarded(
    *,
    fresh: Dict[str, object],
    staged: Dict[str, object],
    join_id: str,
    ledger: object,
    actor: str,
    idempotency_key: str,
    execution_token: object,
    fault_hook: Optional[Callable[[str], None]],
) -> Dict[str, object]:
    """Perform the G2 saga while one exact root execution guard is held."""

    # Staging verified these coordinates under the guard; bind them once more
    # at the final pre-start boundary so no intermediate staging observation
    # can authorize a durable receipt.
    _require_detached_source(
        Path(str(staged["source"])), str(staged["git_executable"]),
        str(fresh["target_source_sha"]),
    )
    _verify_plan_managed_inventory(
        Path(str(fresh["inputs"]["destination"])), fresh
    )
    _verify_waiter_generation_targets(
        fresh.get("waiter_bindings"), fresh.get("target_waiter_digest")
    )
    start = ledger._start_guarded(
        fresh, actor, idempotency_key, execution_token
    )
    rows = [row for row in ledger.rows(actor) if row.get("idempotency_key") == idempotency_key]
    predecessor = str(rows[-1]["id"])
    completed_steps = [row for row in rows if row.get("kind") == "fleet_update_step"]
    shared_steps = [row for row in completed_steps if row.get("step_kind") == "shared_install"]
    destination = Path(str(fresh["inputs"]["destination"]))
    source = Path(str(staged["source"]))
    pre_digest = str(fresh["current_manifest_sha256"])
    post_digest = str(fresh["target_manifest_sha256"])
    shared_predecessor = str(start["id"])
    prepared_shared = ledger._prepare_step_guarded(
        fresh, actor, idempotency_key, shared_predecessor, execution_token
    )
    if shared_steps:
        shared = ledger._step_guarded(
            fresh, actor, idempotency_key, shared_predecessor, execution_token
        )
        predecessor = str(shared["id"])
        _fault(fault_hook, "after_shared_step_receipt")
    else:
        initial_phase = str(prepared_shared["initial_phase"])
        writer = DeploymentWriter(
            source, destination, "update", ref="HEAD", committed_tree=True,
            installer_path=_governed_installer_shadow_path(
                destination, str(staged["installer_executable"])
            ),
            git_executable=str(staged["git_executable"]), join_id=join_id,
            planned_intents=fresh["shared_install_intents"],
            fault_hook=fault_hook,
        )
        if initial_phase in {"pre", "partial"}:
            _fault(fault_hook, "before_shared_writer_run")
            ledger._require_execution_token(execution_token)
            writer.run()
            ledger._require_execution_token(execution_token)
            recovery = _shared_install_recovery_evidence(
                destination, source, pre_digest, post_digest, join_id,
                fresh["shared_install_intents"],
            )
            if recovery["phase"] != "post":
                raise IntegrityFailure(
                    "fleet_update_install_readback_invalid",
                    "shared install did not produce its exact governed journal join",
                )
        else:
            # Exact post bytes plus a complete join authorize only a
            # no-replace replay of every parent-fsync/readback barrier.
            ledger._require_execution_token(execution_token)
            durable = writer.verify_durable_post()
            ledger._require_execution_token(execution_token)
            if durable.get("metadata_sha256") != post_digest:
                raise IntegrityFailure(
                    "fleet_update_install_readback_invalid",
                    "durable shared-install readback diverged from the plan",
                )
            recovery = _shared_install_recovery_evidence(
                destination, source, pre_digest, post_digest, join_id,
                fresh["shared_install_intents"],
            )
            if recovery["phase"] != "post":
                raise IntegrityFailure(
                    "fleet_update_install_readback_invalid",
                    "shared install changed during durable post readback",
                )
        _fault(fault_hook, "after_shared_commit_readback")
        shared = ledger._step_guarded(
            fresh, actor, idempotency_key, shared_predecessor, execution_token
        )
        predecessor = str(shared["id"])
        _fault(fault_hook, "after_shared_step_receipt")
        completed_steps = [*completed_steps, shared]
    waiter_receipts: list[Dict[str, object]] = []
    waiter_rows = {
        row.get("step_ordinal"): row
        for row in completed_steps if row.get("step_kind") == "waiter_binding"
    }
    for binding_index, staged_waiter in enumerate(list(staged["waiters"])):
        ordinal = binding_index + 2
        prepared_waiter = ledger._prepare_step_guarded(
            fresh, actor, idempotency_key, predecessor, execution_token
        )
        existing = waiter_rows.get(ordinal)
        if existing is not None:
            verified = ledger._step_guarded(
                fresh, actor, idempotency_key, predecessor, execution_token
            )
            predecessor = str(verified["id"])
            waiter_receipts.append({
                "receipt": verified,
                "trust_observation": verified["step_evidence"]["hook_post_observation"],
            })
            _fault(fault_hook, "after_waiter_step_receipt")
            continue
        initial_phase = str(prepared_waiter["initial_phase"])
        if initial_phase == "pre":
            ledger._require_execution_token(execution_token)
            stage_waiter_runtime(
                Path(str(staged["source"])), Path(str(staged_waiter["store"])),
                str(fresh["target_waiter_digest"]),
                _fault_hook=fault_hook,
            )
            ledger._require_execution_token(execution_token)
            commit_waiter_rebind(
                staged_waiter, _fault_hook=fault_hook
            )
            ledger._require_execution_token(execution_token)
            recovery = _waiter_recovery_evidence(staged_waiter)
            if recovery["phase"] != "post":
                raise IntegrityFailure("fleet_update_waiter_binding_readback_invalid", "waiter rebind did not produce exact post bytes")
        else:
            ledger._require_execution_token(execution_token)
            commit_waiter_rebind(
                staged_waiter, _fault_hook=fault_hook
            )
            ledger._require_execution_token(execution_token)
            recovery = _waiter_recovery_evidence(staged_waiter)
            if recovery["phase"] not in {"post", "unchanged"}:
                raise IntegrityFailure(
                    "fleet_update_waiter_binding_readback_invalid",
                    "waiter post-state durability replay diverged",
                )
        receipt = ledger._step_guarded(
            fresh, actor, idempotency_key, predecessor, execution_token
        )
        predecessor = str(receipt["id"])
        waiter_receipts.append({
            "receipt": receipt,
            "trust_observation": receipt["step_evidence"]["hook_post_observation"],
        })
        _fault(fault_hook, "after_waiter_step_receipt")
    return {
        "started_receipt": start,
        "last_receipt_id": predecessor,
        "waiter_steps": waiter_receipts,
        "state": "g2_committed_pending_later_gates",
    }


def _owner_review_batch(root: FloatiRoot, bindings: Sequence[Dict[str, str]], target_waiter_digest: str) -> tuple[list[Dict[str, object]], list[Dict[str, object]]]:
    """Project every explicit workspace mapping against every explicit hook binding."""

    mapping_path = root.path / "codex-wait" / "workspaces.v0.json"
    if mapping_path.is_symlink():
        raise ProtocolRefusal("fleet_update_mapping_invalid", "fleet_update_mapping_invalid")
    if not mapping_path.exists():
        return [], []
    _payload, mapping = _strict_json_file(mapping_path, "fleet_update_mapping_invalid")
    if set(mapping) != {"schema_version", "tenant_id", "mappings"} or mapping.get("schema_version") != 0 or mapping.get("tenant_id") != root.tenant_id or not isinstance(mapping.get("mappings"), list):
        raise ProtocolRefusal("fleet_update_mapping_invalid", "workspace mapping inventory has an unsupported shape")
    records = read_records_snapshot(root, "registry/entries.jsonl", allowed_kinds=REGISTRY_KINDS)
    latest: Dict[str, Dict[str, object]] = {}
    leases: Dict[str, Dict[str, object]] = {}
    for row in records:
        if row.get("kind") == "registry_entry": latest[str(row["node_id"])] = row
        if row.get("kind") == "node_lease": leases[str(row["node_id"])] = row
    mappings: list[tuple[str, str]] = []
    previous: tuple[str, str] | None = None
    for raw in mapping["mappings"]:
        if not isinstance(raw, dict) or set(raw) != {"workspace", "node_id"} or not isinstance(raw.get("workspace"), str):
            raise ProtocolRefusal("fleet_update_mapping_invalid", "workspace mapping entry is invalid")
        workspace = str(_lexical_safe_absolute_path(
            raw["workspace"], "fleet_update_mapping_invalid"
        ))
        node = validate_identifier(raw.get("node_id"), "node")
        coordinate = (workspace, node)
        if previous is not None and coordinate <= previous:
            raise ProtocolRefusal("fleet_update_mapping_invalid", "workspace mappings must be strictly sorted and unique")
        previous = coordinate
        mappings.append(coordinate)
    consequences: list[Dict[str, object]] = []
    exclusions: list[Dict[str, object]] = []
    for workspace, node in mappings:
        current = latest.get(node)
        # An unregistered map retains its existing coordinate-first refusal.
        # Registered inactive nodes have no required live workspace.
        if current is not None:
            harness = current.get("role")
            if not isinstance(harness, str):
                raise ProtocolRefusal("fleet_update_mapping_invalid", "workspace mapping has no current harness")
            if current.get("state") != "active":
                exclusions.append({"node_id": node, "workspace": workspace, "authoritative_state": "retired", "harness": harness, "reason": "node_retired"})
                continue
            lease = Registry(root).node_lease_state(node)
            if lease["state"] == "expired":
                exclusions.append({"node_id": node, "workspace": workspace, "authoritative_state": "lease_expired", "harness": harness, "reason": "lease_expired"})
                continue
        workspace = _canonical_directory(
            Path(workspace), "fleet_update_mapping_invalid"
        ).as_posix()
        if current is None:
            raise ProtocolRefusal("fleet_update_mapping_unregistered", "workspace mapping names an unregistered node")
        if harness.casefold() != "codex":
            exclusions.append({"node_id": node, "workspace": workspace, "authoritative_state": "active", "harness": harness, "reason": "harness_not_codex"})
            continue
        for binding in bindings:
            configuration, store = Path(binding["configuration"]), Path(binding["store"])
            try:
                configuration_document = json.loads(configuration.read_text(encoding="utf-8"))
                commands = [hook["command"] for block in configuration_document["hooks"]["Stop"] for hook in block["hooks"] if isinstance(hook, dict) and isinstance(hook.get("command"), str) and "floati-codex-wait" in hook["command"]]
                roots = []
                for command in commands:
                    words = shlex.split(command)
                    roots.append(words[words.index("--root") + 1])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "configuration has no valid Floati waiter root") from exc
            if roots != [str(root.path)]:
                raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "configuration waiter does not name this exact fleet root")
            _configuration, current_tree, _named = _waiter_from_configuration(configuration, store)
            trust = observe_codex_waiter_hooks(configuration)
            if len(trust) != 1:
                raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "configuration has no unique Floati hook trust coordinate")
            observed = trust[0]
            current_hash = observed["hook_trust_current_hash"]
            try:
                document = json.loads(configuration.read_text(encoding="utf-8"))
                block = next(block for block in document["hooks"]["Stop"] if "floati-codex-wait" in json.dumps(block, sort_keys=True))
                target_block = json.loads(json.dumps(block))
                command = target_block["hooks"][0]["command"]
                target_launcher = store / target_waiter_digest / "scripts" / "floati-codex-wait"
                target_block["hooks"][0]["command"] = command.replace(str(current_tree / "scripts" / "floati-codex-wait"), str(target_launcher))
                target_hash = codex_hook_current_hash(target_block)
            except (KeyError, StopIteration, TypeError, json.JSONDecodeError) as exc:
                raise ProtocolRefusal("fleet_update_waiter_binding_invalid", "configuration has no rewritable Floati waiter block") from exc
            rotated = current_hash != target_hash
            review = target_hash != observed["hook_trust_observed_hash"]
            enable = observed["hook_enabled"] is not True
            relaunch = rotated or review or enable
            remedies = []
            if enable: remedies.append("enable the exact Stop hook in Codex settings")
            if review: remedies.append("review and trust the exact Stop hook in Codex settings")
            if relaunch: remedies.append("relaunch the affected session")
            consequences.append({"node_id": node, "workspace": workspace, "harness": harness, "configuration": str(configuration), "store": str(store), "association_basis": "conservative_root_scope", "hook_trust_key": observed["hook_trust_key"], "current_hook_hash": current_hash, "target_hook_hash": target_hash, "observed_trusted_hash": observed["hook_trust_observed_hash"], "observed_enabled": observed["hook_enabled"], "current_waiter_digest": waiter_runtime_digest(current_tree), "target_waiter_digest": target_waiter_digest, "trust_rotated_by_update": rotated, "review_required_after_update": review, "enable_required_after_update": enable, "relaunch_required_after_update": relaunch, "reachability_after_update": "unknown_until_review_and_relaunch" if relaunch else "not_observed", "remedy": ";".join(remedies) if remedies else None})
    consequences.sort(key=lambda row: (str(row["node_id"]), str(row["workspace"]), str(row["configuration"])))
    exclusions.sort(key=lambda row: (str(row["node_id"]), str(row["workspace"]), str(row["reason"])))
    return consequences, exclusions


def preview_fleet_update(
    *,
    root: FloatiRoot,
    actor: str,
    destination: Path,
    target_source: Path,
    target_source_sha: str,
    channel: str,
    version: str,
    binding_path: Path,
    transport_registry: Path,
    transport_name: str,
) -> Dict[str, object]:
    """Re-observe every named byte and return one deterministic update plan."""

    root, selected_actor, raw_destination, raw_source, selected_source_sha, selected_channel, version, raw_binding, raw_registry, selected_transport = _lexical_inputs(root, actor, destination, target_source, target_source_sha, channel, version, binding_path, transport_registry, transport_name)
    selected_destination = _canonical_directory(raw_destination, "fleet_update_destination_invalid")
    selected_source = _canonical_directory(raw_source, "fleet_update_target_invalid")

    metadata_path = _canonical_file(selected_destination / ".floati-install" / "manifest.v0.json", "fleet_update_install_metadata_invalid")
    metadata_bytes, current_metadata = _strict_json_file(metadata_path, "fleet_update_install_metadata_invalid")
    current_source_sha = current_metadata.get("source_sha")
    if not isinstance(current_source_sha, str) or _SHA1.fullmatch(current_source_sha) is None:
        raise ProtocolRefusal("fleet_update_install_metadata_invalid", "installed metadata has no valid source identity")
    target_manifest = _validated_target_manifest(selected_source, selected_source_sha)
    planned_metadata = _planned_install_metadata(
        current_metadata, target_manifest, selected_source, selected_destination
    )
    target_manifest_sha256 = _sha256(planned_metadata)
    target_waiter_digest = waiter_runtime_digest(selected_source)

    binding_digest, bindings = _load_bindings(raw_binding)
    # Hook-coordinate validity is independent of workspace eligibility and
    # must win over any later runtime-digest observation.
    for binding in bindings:
        _validate_binding_for_root(root, binding)
    waiter_bindings: List[Dict[str, object]] = []
    for binding in bindings:
        configuration = Path(binding["configuration"])
        store = Path(binding["store"])
        configuration_bytes, current_tree, named_digest = _waiter_from_configuration(configuration, store)
        target_configuration = plan_waiter_rebind(
            configuration, store, target_waiter_digest
        )["after"]
        waiter_bindings.append({
            **binding,
            "configuration_sha256": _sha256(configuration_bytes),
            "named_tree_digest": named_digest,
            "current_tree_digest": waiter_runtime_digest(current_tree),
            "current_tree": str(current_tree),
            "target_configuration_sha256": _sha256(target_configuration),
        })

    registry_bytes, registry = _strict_json_file(raw_registry, "fleet_update_transport_registry_invalid")
    transports = registry.get("transports")
    transport = transports.get(selected_transport) if isinstance(transports, dict) else None
    if not isinstance(transport, dict):
        raise ProtocolRefusal("fleet_update_transport_missing", f"transport {selected_transport} is absent")
    pinned_manifest = transport.get("manifest_sha256")
    pinned_source = transport.get("source_sha")
    observed_manifest_path = transport.get("manifest_path")
    if not isinstance(pinned_manifest, str) or _SHA256.fullmatch(pinned_manifest) is None or not isinstance(pinned_source, str) or _SHA1.fullmatch(pinned_source) is None:
        raise ProtocolRefusal("fleet_update_transport_registry_invalid", "selected transport pins are invalid")
    if observed_manifest_path != str(metadata_path):
        raise ProtocolRefusal("fleet_update_transport_registry_invalid", "selected transport manifest path does not name the exact installed metadata")

    reader_consequences = _reader_consequences(
        current=current_metadata,
        target_bytes=planned_metadata,
        registry_path=raw_registry,
        transport_name=selected_transport,
        manifest_path=metadata_path,
    )

    current_transport_pins = {
        "manifest_sha256": pinned_manifest,
        "source_sha": pinned_source,
    }
    target_transport_pins = {
        "manifest_sha256": target_manifest_sha256,
        "source_sha": selected_source_sha,
    }
    target_transport_registry_sha256 = _sha256(planned_transport_registry_bytes(
        registry_bytes, selected_transport,
        manifest_sha256=target_transport_pins["manifest_sha256"],
        source_sha=target_transport_pins["source_sha"],
    ))
    stale_pins = [
        {
            "field": field,
            "registry": str(raw_registry),
            "transport": selected_transport,
            "pinned": pinned,
            "observed": observed,
        }
        for field, pinned, observed in (
            (
                field,
                current_transport_pins[field],
                target_transport_pins[field],
            )
            for field in _TRANSPORT_PIN_FIELDS
        )
        if pinned != observed
    ]
    seat_binding_consequences, seat_exclusions = _owner_review_batch(
        root, bindings, target_waiter_digest
    )
    current_encoder_sha256 = _encoder_digest(selected_destination)
    target_encoder_sha256 = _encoder_digest(selected_source)
    requires_epoch_roll = current_encoder_sha256 != target_encoder_sha256
    current_rows = current_metadata.get("files")
    target_rows = target_manifest.get("files")
    if not isinstance(current_rows, list) or not isinstance(target_rows, list):
        raise ProtocolRefusal(
            "fleet_update_install_metadata_invalid",
            "install metadata has no exact managed-file inventory",
        )
    current_managed_paths = _managed_paths_from_metadata(
        current_metadata, code="fleet_update_install_metadata_invalid"
    )
    current_paths = set(current_managed_paths)
    shared_install_intents = [
        {
            "kind": "file",
            "op": "replace" if row["path"] in current_paths else "create",
            "path": str(selected_destination / row["path"]),
            "sha256": row["sha256"],
        }
        for row in target_rows
    ]
    shared_install_intents.append({
        "kind": "file",
        "op": "replace",
        "path": str(metadata_path),
        "sha256": target_manifest_sha256,
    })
    inputs: Dict[str, object] = {
        "root": str(root.path),
        "actor": selected_actor,
        "destination": str(selected_destination),
        "channel": selected_channel,
        "version": version,
        "waiter_binding": str(raw_binding),
        "transport_registry": str(raw_registry),
        "transport": selected_transport,
    }
    plan: Dict[str, object] = {
        "schema_version": 0,
        "kind": "fleet_update_plan",
        "inputs": inputs,
        "current_source_sha": current_source_sha,
        "target_source_sha": selected_source_sha,
        "current_manifest_sha256": _sha256(metadata_bytes),
        "target_manifest_sha256": target_manifest_sha256,
        "binding_inventory_sha256": binding_digest,
        "transport_registry_sha256": _sha256(registry_bytes),
        "target_transport_registry_sha256": target_transport_registry_sha256,
        "waiter_bindings": waiter_bindings,
        "target_waiter_digest": target_waiter_digest,
        "current_encoder_sha256": current_encoder_sha256,
        "target_encoder_sha256": target_encoder_sha256,
        "current_transport_pins": current_transport_pins,
        "target_transport_pins": target_transport_pins,
        "current_managed_paths": current_managed_paths,
        "shared_install_intents": shared_install_intents,
        "reader_consequences": reader_consequences,
        "seat_binding_consequences": seat_binding_consequences,
        "seat_exclusions": seat_exclusions,
        "owner_review_batch_digest": owner_review_batch_digest(
            reader_consequences, seat_binding_consequences, seat_exclusions
        ),
        "requires_epoch_roll": requires_epoch_roll,
        "stale_pins": stale_pins,
        "moves": [],
        "unchanged": [],
    }
    plan["moves"], plan["unchanged"] = _action_rows(plan, waiter_bindings)
    digest = _canonical_plan_digest(plan)
    apply_argv = [
        "update", "fleet", "apply",
        "--root", str(root.path),
        "--as", selected_actor,
        "--destination", str(selected_destination),
        "--channel", selected_channel,
        "--version", version,
        "--waiter-binding", str(raw_binding),
        "--transport-registry", str(raw_registry),
        "--transport", selected_transport,
        "--plan-digest", digest,
        "--idempotency-key", "KEY",
    ]
    return {**plan, "plan_digest": digest, "apply_argv": apply_argv}


def fleet_update_pin_approval_artifact(plan: Dict[str, object]) -> Dict[str, object]:
    """Project the aggregate stale-pin approval facts from one authenticated plan."""

    from .fleet_update_receipts import authenticate_plan

    inputs = plan.get("inputs") if isinstance(plan, dict) else None
    actor = inputs.get("actor") if isinstance(inputs, dict) else None
    authenticated = authenticate_plan(plan, actor)
    stale = authenticated["stale_pins"]
    if not stale:
        raise ProtocolRefusal("fleet_update_pin_approval_not_required", "fleet_update_pin_approval_not_required")
    return {
        "code": "fleet_update_pin_approval_required",
        "stale_pins": stale,
        "apply_argv": authenticated["apply_argv"],
        "plan_digest": authenticated["plan_digest"],
    }


def apply_fleet_update(*, plan_digest: str, idempotency_key: str, **inputs: Any) -> Dict[str, object]:
    """Re-plan, join AU-1 consent, then append the G1 start receipt only."""

    if not isinstance(plan_digest, str) or _SHA256.fullmatch(plan_digest) is None:
        raise ProtocolRefusal("fleet_update_plan_drift", "supplied plan digest must be lowercase 64-hex")
    if not isinstance(idempotency_key, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", idempotency_key) is None:
        raise ProtocolRefusal("idempotency_key_invalid", "idempotency key is terminal-safe and between 1 and 128 bytes")
    fresh = preview_fleet_update(**inputs)
    from .fleet_update_receipts import (
        FleetUpdateReceiptLedger,
        _FleetUpdateExecutionGuard,
    )
    ledger = FleetUpdateReceiptLedger(inputs["root"])
    with _FleetUpdateExecutionGuard(inputs["root"]) as execution_token:
        # The pre-lock preview establishes lexical/observation ordering only;
        # every value which can authorize the start is recomputed under the
        # same root exclusion that protects the append.
        fresh = preview_fleet_update(**inputs)
        retry = ledger._exact_retry_guarded(
            fresh, str(inputs["actor"]), idempotency_key, execution_token
        )
        if retry is not None:
            if plan_digest != fresh["plan_digest"]:
                raise ProtocolRefusal("fleet_update_plan_drift", "supplied plan digest does not match the exact retry plan")
            return retry
        consent = ledger._active_consent(fresh)
        if plan_digest != fresh["plan_digest"]:
            raise ProtocolRefusal("fleet_update_plan_drift", "fresh observations do not match the supplied plan digest")
        plan, digest, actor, readers, consequences, exclusions, batch = ledger._plan(fresh, str(inputs["actor"]), idempotency_key)
        return ledger._start_authorized(
            plan, digest, actor, idempotency_key, readers, consequences,
            exclusions, batch, consent, execution_token,
        )


def commit_fleet_update_g3(
    *, plan: Dict[str, object], root: FloatiRoot, actor: str,
    idempotency_key: str, epoch_roll: object = None,
    _epoch_roll_result: object = None,
) -> Dict[str, object]:
    """Continue an authenticated G2 saga only where VS-7 permits it.

    VS-7 has no product verb on this branch.  Encoder-changing plans are
    consequently an explicit no-write refusal; a supplied result is never
    testimony.  Equal-encoder plans continue from the exact G2 frontier under
    the existing root-wide execution guard, then receipt pins before terminal
    completion.
    """
    from .fleet_update_receipts import (
        FleetUpdateReceiptLedger, _FleetUpdateExecutionGuard, authenticate_plan,
    )

    authenticated = authenticate_plan(plan, actor, root)
    if authenticated.get("requires_epoch_roll") is False and (epoch_roll is not None or _epoch_roll_result is not None):
        raise ProtocolRefusal("fleet_update_epoch_roll_unexpected", "stable encoders must not carry epoch-roll testimony")
    if authenticated.get("requires_epoch_roll") is True:
        raise ProtocolRefusal("fleet_update_epoch_roll_unavailable", "VS-7 epoch roll is not a product API on this branch")
    ledger = FleetUpdateReceiptLedger(root)
    with _FleetUpdateExecutionGuard(root) as token:
        ledger._root_has_active_saga(actor)
        rows = [row for row in ledger.rows(actor) if row.get("idempotency_key") == idempotency_key and row.get("plan_digest") == authenticated.get("plan_digest")]
        expected = ["shared_install"] + ["waiter_binding"] * len(authenticated["waiter_bindings"])
        actual = [row.get("step_kind") for row in rows if row.get("kind") == "fleet_update_step"]
        if actual not in (expected, expected + ["transport_pins"]):
            raise ProtocolRefusal("fleet_update_g2_frontier_invalid", "G3 requires the exact contiguous G2 receipt frontier")
        completed = next((row for row in rows if row.get("kind") == "fleet_update_completed"), None)
        if completed is not None:
            pins = next(
                row for row in rows
                if row.get("kind") == "fleet_update_step"
                and row.get("step_kind") == "transport_pins"
            )
            # A completed response-loss retry still re-observes the exact
            # planned registry post-state; target pins alone are insufficient.
            ledger._step_guarded(
                authenticated, actor, idempotency_key,
                str(pins["predecessor_receipt_id"]), token,
            )
            return ledger._complete_guarded(
                authenticated, actor, idempotency_key, str(pins["id"]), token
            )
        if actual == expected + ["transport_pins"]:
            pins = rows[-1]
            # A crash after the surgical replacement but before its terminal
            # receipt resumes only after the persisted pin receipt agrees with
            # the exact authenticated whole-registry post state.
            ledger._step_guarded(
                authenticated, actor, idempotency_key,
                str(pins["predecessor_receipt_id"]), token,
            )
            return ledger._complete_guarded(
                authenticated, actor, idempotency_key, str(pins["id"]), token
            )
        predecessor = str(rows[-1]["id"])
        prepared = ledger._prepare_step_guarded(authenticated, actor, idempotency_key, predecessor, token)
        if prepared["step_kind"] != "transport_pins":
            raise ProtocolRefusal("fleet_update_g2_frontier_invalid", "G3 next receipt is not transport pins")
        if prepared["initial_phase"] == "pre":
            identity = prepared.get("registry_identity")
            if (
                not isinstance(identity, tuple)
                or len(identity) != 2
                or any(type(value) is not int for value in identity)
            ):
                raise ProtocolRefusal(
                    "fleet_update_transport_registry_invalid",
                    "prepared transport registry has no exact inode witness",
                )
            rewrite_transport_pins(
                Path(str(authenticated["inputs"]["transport_registry"])),
                str(authenticated["inputs"]["transport"]),
                manifest_sha256=str(authenticated["target_transport_pins"]["manifest_sha256"]),
                source_sha=str(authenticated["target_transport_pins"]["source_sha"]),
                expected_registry_sha256=str(authenticated["transport_registry_sha256"]),
                expected_identity=identity,
            )
        pins = ledger._step_guarded(authenticated, actor, idempotency_key, predecessor, token)
        return ledger._complete_guarded(authenticated, actor, idempotency_key, str(pins["id"]), token)
