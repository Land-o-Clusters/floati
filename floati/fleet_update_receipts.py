"""Append-only, consent-joined G1 fleet-update receipt saga."""

from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .records import validate_record
from .root import FloatiRoot, validate_identifier
from .registry import utc_now
from .update_consent import UpdateConsentLedger


_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?\Z")
_KINDS = {"fleet_update_started", "fleet_update_step", "fleet_update_completed"}
_READER_FIELDS = {
    "reader", "surface", "registry", "transport", "manifest_path",
    "current_schema_version", "target_schema_version", "added_fields",
    "removed_fields", "change", "compatibility_after_update", "remedy",
}

_PLAN_BODY_FIELDS = {
    "schema_version", "kind", "inputs", "current_source_sha", "target_source_sha",
    "current_manifest_sha256", "target_manifest_sha256", "binding_inventory_sha256",
    "transport_registry_sha256", "target_transport_registry_sha256", "waiter_bindings", "target_waiter_digest",
    "current_encoder_sha256", "target_encoder_sha256", "current_transport_pins",
    "target_transport_pins", "current_managed_paths", "shared_install_intents",
    "reader_consequences", "seat_binding_consequences", "seat_exclusions",
    "owner_review_batch_digest", "requires_epoch_roll", "stale_pins", "moves", "unchanged",
}
_WAITER_BINDING_FIELDS = {
    "kind", "configuration", "store", "configuration_sha256",
    "named_tree_digest", "current_tree_digest", "current_tree",
    "target_configuration_sha256",
}
_TRANSPORT_PIN_FIELDS = ("manifest_sha256", "source_sha")
_TRANSPORT_PIN_EVIDENCE_FIELDS = {
    "kind", "registry", "transport", "registry_before_sha256",
    "registry_after_sha256", "previous_source_sha", "target_source_sha",
    "epoch_roll_state",
}
_TERMINAL_PROJECTION_FIELDS = (
    "previous_source_sha", "target_source_sha", "epoch_roll_state",
    "registry_before_sha256", "registry_after_sha256",
)
_CONSEQUENCE_FIELDS = {
    "node_id", "workspace", "harness", "configuration", "store",
    "association_basis", "hook_trust_key", "current_hook_hash",
    "target_hook_hash", "observed_trusted_hash", "observed_enabled",
    "current_waiter_digest", "target_waiter_digest",
    "trust_rotated_by_update", "review_required_after_update",
    "enable_required_after_update", "relaunch_required_after_update",
    "reachability_after_update", "remedy",
}
_EXCLUSION_FIELDS = {
    "node_id", "workspace", "authoritative_state", "harness", "reason",
}
_CONSEQUENCE_IDENTITY_FIELDS = (
    "node_id", "workspace", "harness", "configuration", "store",
    "association_basis",
)
_METADATA_RELATIVE = ".floati-install/manifest.v0.json"
_PLAN_INPUT_FIELDS = {
    "root", "actor", "destination", "channel", "version", "waiter_binding",
    "transport_registry", "transport",
}
_EXECUTION_TOKEN_AUTHORITY = object()


class _FleetUpdateExecutionToken:
    """Opaque proof that this thread owns a parent lock and exact root leaf."""

    __slots__ = (
        "_authority",
        "_guard",
        "parent_device",
        "parent_inode",
        "device",
        "inode",
        "pid",
        "thread_id",
    )

    def __init__(
        self,
        authority: object,
        guard: "_FleetUpdateExecutionGuard",
        parent_device: int,
        parent_inode: int,
        root_device: int,
        root_inode: int,
    ) -> None:
        if authority is not _EXECUTION_TOKEN_AUTHORITY:
            raise TypeError("fleet update execution tokens are guard-created")
        self._authority = authority
        self._guard = guard
        self.parent_device = parent_device
        self.parent_inode = parent_inode
        self.device = root_device
        self.inode = root_inode
        self.pid = os.getpid()
        self.thread_id = threading.get_ident()


class _FleetUpdateExecutionGuard:
    """One stable-parent exclusion bound to an exact validated Floati root leaf."""

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal(
                "fleet_update_execution_lock_invalid",
                "execution guard requires one validated Floati root",
            )
        self.root = root
        self.path = root.path.parent
        self.root_path = root.path
        self._descriptor: Optional[int] = None
        self._token: Optional[_FleetUpdateExecutionToken] = None

    @staticmethod
    def _identity_invalid(detail: str, exc: Optional[BaseException] = None) -> None:
        refusal = ProtocolRefusal("fleet_update_execution_lock_invalid", detail)
        if exc is None:
            raise refusal
        raise refusal from exc

    @staticmethod
    def _durability(exc: OSError) -> DurabilityFailure:
        return DurabilityFailure(
            "fleet_update_execution_lock_unavailable",
            "fleet update root execution lock is unavailable: "
            + (exc.strerror or str(exc)),
        )

    def acquire(self) -> _FleetUpdateExecutionToken:
        if self._descriptor is not None:
            self._identity_invalid("execution guard is already acquired")
        try:
            parent_before = os.lstat(self.path)
            root_before = os.lstat(self.root_path)
        except OSError as exc:
            raise self._durability(exc) from exc
        if (
            not stat.S_ISDIR(parent_before.st_mode)
            or stat.S_ISLNK(parent_before.st_mode)
        ):
            self._identity_invalid(
                "validated Floati root parent is no longer a real directory"
            )
        if not stat.S_ISDIR(root_before.st_mode) or stat.S_ISLNK(root_before.st_mode):
            self._identity_invalid("validated Floati root is no longer a real directory")
        required_flags = tuple(
            getattr(os, name, None)
            for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
        )
        if not all(type(value) is int for value in required_flags):
            raise DurabilityFailure(
                "fleet_update_execution_lock_unavailable",
                "platform cannot open the root execution lock fail-closed",
            )
        flags = os.O_RDONLY
        for value in required_flags:
            flags |= value
        try:
            descriptor = os.open(self.path, flags)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                self._identity_invalid("validated Floati root identity changed", exc)
            raise self._durability(exc) from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_dev != parent_before.st_dev
                or opened.st_ino != parent_before.st_ino
            ):
                self._identity_invalid("opened Floati root parent identity is not exact")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ProtocolRefusal(
                    "fleet_update_execution_contended",
                    "another fleet update execution owns this Floati root",
                ) from exc
            except OSError as exc:
                if exc.errno in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EWOULDBLOCK,
                }:
                    raise ProtocolRefusal(
                        "fleet_update_execution_contended",
                        "another fleet update execution owns this Floati root",
                    ) from exc
                raise self._durability(exc) from exc
            try:
                parent_after = os.lstat(self.path)
                root_after = os.lstat(self.root_path)
            except OSError as exc:
                raise self._durability(exc) from exc
            if (
                not stat.S_ISDIR(parent_after.st_mode)
                or stat.S_ISLNK(parent_after.st_mode)
                or parent_after.st_dev != opened.st_dev
                or parent_after.st_ino != opened.st_ino
            ):
                self._identity_invalid(
                    "Floati root parent identity changed after lock acquisition"
                )
            if (
                not stat.S_ISDIR(root_after.st_mode)
                or stat.S_ISLNK(root_after.st_mode)
                or root_after.st_dev != root_before.st_dev
                or root_after.st_ino != root_before.st_ino
            ):
                self._identity_invalid(
                    "Floati root leaf identity changed after lock acquisition"
                )
        except BaseException:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        self._descriptor = descriptor
        self._token = _FleetUpdateExecutionToken(
            _EXECUTION_TOKEN_AUTHORITY,
            self,
            opened.st_dev,
            opened.st_ino,
            root_before.st_dev,
            root_before.st_ino,
        )
        return self._token

    def release(self, *, suppress_errors: bool = False) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            if self._token is not None:
                self._identity_invalid("execution guard state is inconsistent")
            return
        token = self._token
        if (
            not isinstance(token, _FleetUpdateExecutionToken)
            or token._authority is not _EXECUTION_TOKEN_AUTHORITY
            or token._guard is not self
        ):
            self._identity_invalid(
                "execution guard ownership token is invalid"
            )
        if token.pid != os.getpid():
            # A fork child shares the parent's open-file description.  It may
            # drop only its inherited descriptor reference; LOCK_UN would
            # release the parent's exclusion too.  These field assignments
            # mutate only the child's copied Python object.
            self._descriptor = None
            self._token = None
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._identity_invalid(
                "only the acquiring process may release this execution guard"
            )
        if token.thread_id != threading.get_ident():
            self._identity_invalid(
                "only the acquiring thread may release this execution guard"
            )
        self._descriptor = None
        self._token = None
        failure: Optional[OSError] = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as exc:
            failure = exc
        try:
            os.close(descriptor)
        except OSError as exc:
            if failure is None:
                failure = exc
        if failure is not None and not suppress_errors:
            raise self._durability(failure) from failure

    def __enter__(self) -> _FleetUpdateExecutionToken:
        return self.acquire()

    def __exit__(
        self,
        exception_type: object,
        _exception: object,
        _traceback: object,
    ) -> None:
        self.release(suppress_errors=exception_type is not None)


def normalize_fleet_update_receipt(record: object) -> Dict[str, object]:
    """Project legacy derived claims and the raw v1 wire to one fact object.

    This is the explicit migration/consumer seam.  New receipts never persist
    the absolute metadata concatenation or ``hook_armed``; both are computed
    here from schema-carried raw facts.  Legacy forms are accepted only when
    their redundant claim equals the same formula, and hybrid forms refuse.
    """

    if not isinstance(record, dict) or record.get("kind") not in _KINDS:
        raise ProtocolRefusal(
            "fleet_update_receipt_invalid",
            "fleet update receipt normalization requires one receipt object",
        )
    normalized = copy.deepcopy(record)
    if normalized.get("kind") != "fleet_update_step":
        return normalized
    step_kind = normalized.get("step_kind")
    coordinate = normalized.get("step_coordinate")
    evidence = normalized.get("step_evidence")
    if not isinstance(coordinate, dict) or not isinstance(evidence, dict):
        raise ProtocolRefusal(
            "fleet_update_step_invalid", "fleet update step facts are absent"
        )
    if step_kind == "shared_install":
        legacy_fields = {"kind", "destination", "metadata"}
        raw_fields = {"kind", "destination", "metadata_relative"}
        if set(coordinate) == legacy_fields:
            expected = str(
                Path(str(coordinate.get("destination"))) / _METADATA_RELATIVE
            )
            if coordinate.get("metadata") != expected:
                raise ProtocolRefusal(
                    "fleet_update_step_invalid",
                    "legacy shared metadata claim is not derived",
                )
        elif set(coordinate) == raw_fields:
            if coordinate.get("metadata_relative") != _METADATA_RELATIVE:
                raise ProtocolRefusal(
                    "fleet_update_step_invalid",
                    "shared metadata suffix is not canonical",
                )
            normalized["step_coordinate"] = {
                "kind": "shared_install",
                "destination": coordinate.get("destination"),
                "metadata": str(
                    Path(str(coordinate.get("destination")))
                    / _METADATA_RELATIVE
                ),
            }
        else:
            raise ProtocolRefusal(
                "fleet_update_step_invalid",
                "shared coordinate mixes receipt representations",
            )
    elif step_kind == "waiter_binding":
        observation = evidence.get("hook_post_observation")
        if not isinstance(observation, dict):
            raise ProtocolRefusal(
                "fleet_update_step_invalid", "waiter observation is absent"
            )
        raw_fields = {
            "hook_trust_key", "current_hook_hash", "observed_trusted_hash",
            "observed_enabled",
        }
        legacy_fields = raw_fields | {"hook_armed"}
        armed = (
            observation.get("observed_enabled") is True
            and observation.get("observed_trusted_hash")
            == observation.get("current_hook_hash")
        )
        if set(observation) == legacy_fields:
            if observation.get("hook_armed") is not armed:
                raise ProtocolRefusal(
                    "fleet_update_step_invalid",
                    "legacy hook armed claim is not derived",
                )
        elif set(observation) == raw_fields:
            normalized_observation = dict(observation)
            normalized_observation["hook_armed"] = armed
            normalized["step_evidence"] = {
                "kind": "waiter_binding",
                "hook_post_observation": normalized_observation,
            }
        else:
            raise ProtocolRefusal(
                "fleet_update_step_invalid",
                "waiter observation mixes receipt representations",
            )
    return normalized


def _plan_invalid(detail: str) -> None:
    raise ProtocolRefusal("fleet_update_plan_invalid", detail)


def _sha(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _absolute_coordinate(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        return False
    path = Path(value)
    return (
        path.is_absolute()
        and str(path) == value
        and os.path.normpath(value) == value
        and all(part not in {"", ".", ".."} for part in path.parts[1:])
    )


def _https_channel(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return False
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname is not None
        and parts.username is None
        and parts.password is None
        and not parts.fragment
        and parts.path.startswith("/")
        and (port is None or 1 <= port <= 65535)
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _validate_waiter_bindings(plan: Dict[str, object]) -> list[Dict[str, object]]:
    raw = plan.get("waiter_bindings")
    if not isinstance(raw, list) or not raw:
        _plan_invalid("plan waiter bindings are absent")
    bindings: list[Dict[str, object]] = []
    coordinates: list[tuple[str, str]] = []
    for row in raw:
        if not isinstance(row, dict) or set(row) != _WAITER_BINDING_FIELDS:
            _plan_invalid("plan waiter binding vocabulary is invalid")
        if row.get("kind") != "codex_stop_hook":
            _plan_invalid("plan waiter binding kind is invalid")
        configuration, store = row.get("configuration"), row.get("store")
        if not _absolute_coordinate(configuration) or not _absolute_coordinate(store):
            _plan_invalid("plan waiter binding coordinate is invalid")
        for field in (
            "configuration_sha256", "named_tree_digest", "current_tree_digest",
            "target_configuration_sha256",
        ):
            if not _sha(row.get(field), _SHA256):
                _plan_invalid("plan waiter binding digest is invalid")
        if row.get("current_tree") != str(Path(str(store)) / str(row["named_tree_digest"])):
            _plan_invalid("plan waiter tree coordinate is not derived from its store and name")
        coordinates.append((str(configuration), str(store)))
        bindings.append(dict(row))
    if coordinates != sorted(coordinates) or len(coordinates) != len(set(coordinates)):
        _plan_invalid("plan waiter bindings are not sorted and unique")
    return bindings


def _validate_owner_review_plan(
    plan: Dict[str, object], bindings: list[Dict[str, object]]
) -> tuple[list[object], list[object], list[object], str]:
    readers = _validate_readers(plan.get("reader_consequences"), plan)
    consequences = plan.get("seat_binding_consequences")
    exclusions = plan.get("seat_exclusions")
    if not isinstance(consequences, list) or not isinstance(exclusions, list):
        _plan_invalid("plan owner review arrays are invalid")
    binding_by_coordinate = {
        (row["configuration"], row["store"]): row for row in bindings
    }
    consequence_keys: list[tuple[str, str, str]] = []
    for row in consequences:
        if not isinstance(row, dict) or set(row) != _CONSEQUENCE_FIELDS:
            _plan_invalid("plan seat consequence vocabulary is invalid")
        if (
            not isinstance(row.get("node_id"), str)
            or _IDENTIFIER.fullmatch(str(row["node_id"])) is None
            or not _absolute_coordinate(row.get("workspace"))
            or not _absolute_coordinate(row.get("configuration"))
            or not _absolute_coordinate(row.get("store"))
            or not isinstance(row.get("harness"), str)
            or not row["harness"]
            or row.get("association_basis") != "conservative_root_scope"
            or not isinstance(row.get("hook_trust_key"), str)
            or not row["hook_trust_key"]
        ):
            _plan_invalid("plan seat consequence coordinate is invalid")
        for field in (
            "current_hook_hash", "target_hook_hash", "current_waiter_digest",
            "target_waiter_digest",
        ):
            if not _sha(row.get(field), _SHA256):
                _plan_invalid("plan seat consequence digest is invalid")
        trusted = row.get("observed_trusted_hash")
        if trusted is not None and not _sha(trusted, _SHA256):
            _plan_invalid("plan seat consequence trust observation is invalid")
        if type(row.get("observed_enabled")) is not bool:
            _plan_invalid("plan seat consequence enablement observation is invalid")
        binding = binding_by_coordinate.get(
            (row["configuration"], row["store"])
        )
        if (
            binding is None
            or row.get("current_waiter_digest") != binding["current_tree_digest"]
            or row.get("target_waiter_digest") != plan.get("target_waiter_digest")
        ):
            _plan_invalid("plan seat consequence is not bound to a waiter binding")
        rotated = row["current_hook_hash"] != row["target_hook_hash"]
        review = row["target_hook_hash"] != trusted
        enable = row["observed_enabled"] is not True
        relaunch = rotated or review or enable
        remedies = []
        if enable:
            remedies.append("enable the exact Stop hook in Codex settings")
        if review:
            remedies.append("review and trust the exact Stop hook in Codex settings")
        if relaunch:
            remedies.append("relaunch the affected session")
        if (
            row.get("trust_rotated_by_update") is not rotated
            or row.get("review_required_after_update") is not review
            or row.get("enable_required_after_update") is not enable
            or row.get("relaunch_required_after_update") is not relaunch
            or row.get("reachability_after_update")
            != ("unknown_until_review_and_relaunch" if relaunch else "not_observed")
            or row.get("remedy") != (";".join(remedies) if remedies else None)
        ):
            _plan_invalid("plan seat consequence formulas are invalid")
        consequence_keys.append(
            (str(row["node_id"]), str(row["workspace"]), str(row["configuration"]))
        )
    if consequence_keys != sorted(consequence_keys) or len(consequence_keys) != len(set(consequence_keys)):
        _plan_invalid("plan seat consequences are not sorted and unique")
    exclusion_keys: list[tuple[str, str, str]] = []
    allowed_exclusions = {
        ("retired", "node_retired"),
        ("lease_expired", "lease_expired"),
        ("active", "harness_not_codex"),
    }
    for row in exclusions:
        if not isinstance(row, dict) or set(row) != _EXCLUSION_FIELDS:
            _plan_invalid("plan seat exclusion vocabulary is invalid")
        if (
            not isinstance(row.get("node_id"), str)
            or _IDENTIFIER.fullmatch(str(row["node_id"])) is None
            or not _absolute_coordinate(row.get("workspace"))
            or not isinstance(row.get("harness"), str)
            or not row["harness"]
            or (row.get("authoritative_state"), row.get("reason"))
            not in allowed_exclusions
        ):
            _plan_invalid("plan seat exclusion formula is invalid")
        exclusion_keys.append(
            (str(row["node_id"]), str(row["workspace"]), str(row["reason"]))
        )
    if exclusion_keys != sorted(exclusion_keys) or len(exclusion_keys) != len(set(exclusion_keys)):
        _plan_invalid("plan seat exclusions are not sorted and unique")
    batch = owner_review_batch_digest(readers, consequences, exclusions)
    if plan.get("owner_review_batch_digest") != batch:
        _plan_invalid("plan owner review digest is not canonical")
    return readers, list(consequences), list(exclusions), batch


def _validate_plan_semantics(
    plan: Dict[str, object], selected_actor: str, root: Optional[FloatiRoot]
) -> None:
    inputs = plan.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != _PLAN_INPUT_FIELDS:
        _plan_invalid("plan input vocabulary is invalid")
    if inputs.get("actor") != selected_actor:
        _plan_invalid("plan actor is invalid")
    for field in ("root", "destination", "waiter_binding", "transport_registry"):
        if not _absolute_coordinate(inputs.get(field)):
            _plan_invalid("plan path input is invalid")
    if root is not None and inputs.get("root") != str(root.path):
        _plan_invalid("plan root does not match receipt root")
    if not _https_channel(inputs.get("channel")):
        _plan_invalid("plan channel input is invalid")
    if not isinstance(inputs.get("version"), str) or _VERSION.fullmatch(str(inputs["version"])) is None:
        _plan_invalid("plan version input is invalid")
    if not isinstance(inputs.get("transport"), str) or _IDENTIFIER.fullmatch(str(inputs["transport"])) is None:
        _plan_invalid("plan transport input is invalid")

    for field in ("current_source_sha", "target_source_sha"):
        if not _sha(plan.get(field), _SHA1):
            _plan_invalid("plan source identity is invalid")
    for field in (
        "current_manifest_sha256", "target_manifest_sha256",
        "binding_inventory_sha256", "transport_registry_sha256", "target_transport_registry_sha256",
        "target_waiter_digest", "current_encoder_sha256", "target_encoder_sha256",
    ):
        if not _sha(plan.get(field), _SHA256):
            _plan_invalid("plan digest witness is invalid")

    pins: dict[str, Dict[str, object]] = {}
    for name in ("current_transport_pins", "target_transport_pins"):
        value = plan.get(name)
        if (
            not isinstance(value, dict)
            or set(value) != set(_TRANSPORT_PIN_FIELDS)
            or not _sha(value.get("manifest_sha256"), _SHA256)
            or not _sha(value.get("source_sha"), _SHA1)
        ):
            _plan_invalid("plan transport pin witness is invalid")
        pins[name] = value
    target_pins = {
        "manifest_sha256": plan["target_manifest_sha256"],
        "source_sha": plan["target_source_sha"],
    }
    if pins["target_transport_pins"] != target_pins:
        _plan_invalid("plan target transport pins are not derived")

    bindings = _validate_waiter_bindings(plan)
    _validate_owner_review_plan(plan, bindings)

    current_managed_paths = plan.get("current_managed_paths")
    if not isinstance(current_managed_paths, list) or not current_managed_paths:
        _plan_invalid("plan current managed inventory is absent")
    for relative in current_managed_paths:
        if not isinstance(relative, str):
            _plan_invalid("plan current managed path is invalid")
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or not pure.parts
            or pure.as_posix() != relative
            or any(part in {"", ".", ".."} for part in pure.parts)
            or _METADATA_RELATIVE.split("/", 1)[0] in pure.parts
        ):
            _plan_invalid("plan current managed path is invalid")
    if current_managed_paths != sorted(current_managed_paths) or len(
        current_managed_paths
    ) != len(set(current_managed_paths)):
        _plan_invalid("plan current managed inventory is not sorted and unique")
    current_managed = set(current_managed_paths)

    intents = plan.get("shared_install_intents")
    if not isinstance(intents, list) or len(intents) < 2:
        _plan_invalid("plan shared install intents are absent")
    destination = Path(str(inputs["destination"]))
    metadata_path = str(destination / _METADATA_RELATIVE)
    seen_paths: set[str] = set()
    relative_paths: list[str] = []
    target_encoder_members: dict[str, str] = {}
    for index, row in enumerate(intents):
        if (
            not isinstance(row, dict)
            or set(row) != {"kind", "op", "path", "sha256"}
            or row.get("kind") != "file"
            or row.get("op") not in {"create", "replace"}
            or not _absolute_coordinate(row.get("path"))
            or not _sha(row.get("sha256"), _SHA256)
        ):
            _plan_invalid("plan shared install intent is invalid")
        path = Path(str(row["path"]))
        try:
            relative = path.relative_to(destination).as_posix()
        except ValueError:
            _plan_invalid("plan shared install intent escapes its destination")
        if row["path"] in seen_paths:
            _plan_invalid("plan shared install intent path repeats")
        seen_paths.add(str(row["path"]))
        if index == len(intents) - 1:
            if row != {
                "kind": "file", "op": "replace", "path": metadata_path,
                "sha256": plan["target_manifest_sha256"],
            }:
                _plan_invalid("plan install metadata intent is not canonical")
            continue
        if relative == _METADATA_RELATIVE:
            _plan_invalid("plan install metadata intent is not terminal")
        expected_op = "replace" if relative in current_managed else "create"
        if row["op"] != expected_op:
            _plan_invalid("plan shared install operation is not derived")
        relative_paths.append(relative)
        if relative in {"floati/events.py", "floati/records.py"}:
            target_encoder_members[relative] = str(row["sha256"])
    if relative_paths != sorted(relative_paths) or len(relative_paths) != len(set(relative_paths)):
        _plan_invalid("plan shared install intents are not in manifest order")
    if set(target_encoder_members) != {"floati/events.py", "floati/records.py"}:
        _plan_invalid("plan shared install intents omit encoder members")
    encoder = hashlib.sha256()
    for relative in ("floati/events.py", "floati/records.py"):
        encoder.update(relative.encode("ascii") + b"\0")
        encoder.update(bytes.fromhex(target_encoder_members[relative]))
    if encoder.hexdigest() != plan["target_encoder_sha256"]:
        _plan_invalid("plan target encoder digest is not derived from writer intents")

    requires_roll = plan["current_encoder_sha256"] != plan["target_encoder_sha256"]
    if plan.get("requires_epoch_roll") is not requires_roll:
        _plan_invalid("plan epoch-roll formula is invalid")
    expected_stale = [
        {
            "field": field,
            "registry": inputs["transport_registry"],
            "transport": inputs["transport"],
            "pinned": pins["current_transport_pins"][field],
            "observed": pins["target_transport_pins"][field],
        }
        for field in _TRANSPORT_PIN_FIELDS
        if pins["current_transport_pins"][field]
        != pins["target_transport_pins"][field]
    ]
    if plan.get("stale_pins") != expected_stale:
        _plan_invalid("plan stale transport pins are not derived")
    expected_moves, expected_unchanged = _action_rows(plan, bindings)
    if plan.get("moves") != expected_moves:
        _plan_invalid("plan moves are not derived")
    if plan.get("unchanged") != expected_unchanged:
        _plan_invalid("plan unchanged rows are not canonical")


def _action_rows(
    plan: Dict[str, object], bindings: list[Dict[str, object]],
) -> tuple[list[Dict[str, object]], list[Dict[str, object]]]:
    """Derive the closed action/non-action projection from authenticated facts."""

    inputs = plan["inputs"]
    shared = {
        "kind": "shared_install", "path": inputs["destination"],
        "from": plan["current_manifest_sha256"],
        "to": plan["target_manifest_sha256"],
    }
    transport = {
        "kind": "transport_pins", "path": inputs["transport_registry"],
        "from": plan["current_transport_pins"],
        "to": plan["target_transport_pins"],
    }
    targets: list[Dict[str, object]] = [shared, transport]
    retained: dict[str, Dict[str, object]] = {}
    for binding in bindings:
        targets.append({
            "kind": "waiter_binding", "path": binding["configuration"],
            "store": binding["store"],
            "configuration_from_sha256": binding["configuration_sha256"],
            "configuration_to_sha256": binding["target_configuration_sha256"],
            "current_tree_digest": binding["current_tree_digest"],
            "target_tree_digest": plan["target_waiter_digest"],
        })
        current_tree = str(binding["current_tree"])
        retained[current_tree] = {
            "kind": "waiter_generation", "path": current_tree,
            "named_tree_digest": binding["named_tree_digest"],
            "current_tree_digest": binding["current_tree_digest"],
            "retained": True,
        }
    moves: list[Dict[str, object]] = []
    unchanged: list[Dict[str, object]] = []
    for row in targets:
        kind = row["kind"]
        changed = (
            row["from"] != row["to"]
            if kind in {"shared_install", "transport_pins"}
            else (
                row["configuration_from_sha256"]
                != row["configuration_to_sha256"]
                or row["current_tree_digest"] != row["target_tree_digest"]
            )
        )
        (moves if changed else unchanged).append(row)
    unchanged.extend(retained.values())
    key = lambda row: (str(row["kind"]), str(row["path"]))
    return sorted(moves, key=key), sorted(unchanged, key=key)


def authenticate_plan(plan: object, actor: object, root: Optional[FloatiRoot] = None) -> Dict[str, object]:
    """Accept only the exact canonical preview body bound to one actor/root.

    Receipt entry points call this before adding durable history so a caller
    cannot retain a digest while changing an un-hashed display or coordinate.
    """

    if not isinstance(plan, dict):
        raise ProtocolRefusal("fleet_update_plan_invalid", "receipt requires one exact plan object")
    expected = _PLAN_BODY_FIELDS | {"plan_digest", "apply_argv"}
    if set(plan) != expected or plan.get("schema_version") != 0 or plan.get("kind") != "fleet_update_plan":
        raise ProtocolRefusal("fleet_update_plan_invalid", "plan vocabulary is not exact")
    selected_actor = validate_identifier(actor, "actor")
    body = {field: plan[field] for field in _PLAN_BODY_FIELDS}
    try:
        encoded = json.dumps(body, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ProtocolRefusal("fleet_update_plan_invalid", "plan is not canonical JSON") from exc
    if plan.get("plan_digest") != hashlib.sha256(encoded).hexdigest():
        raise ProtocolRefusal("fleet_update_plan_invalid", "plan digest does not authenticate its body")
    _validate_plan_semantics(plan, selected_actor, root)
    inputs = plan["inputs"]
    expected_argv = [
        "update", "fleet", "apply", "--root", inputs["root"], "--as",
        selected_actor, "--destination", inputs["destination"], "--channel",
        inputs["channel"], "--version", inputs["version"], "--waiter-binding",
        inputs["waiter_binding"], "--transport-registry",
        inputs["transport_registry"], "--transport", inputs["transport"],
        "--plan-digest", plan["plan_digest"], "--idempotency-key", "KEY",
    ]
    authenticated = dict(plan)
    # The command is a presentation projection rather than authority.  Return
    # only the formula-derived rendering so callers cannot smuggle altered
    # display testimony into later receipts while legacy direct fixtures that
    # re-hash an authoritative body remain compatible.
    authenticated["apply_argv"] = expected_argv
    return authenticated


def owner_review_batch_digest(readers: object, consequences: object, exclusions: object) -> str:
    """Hash exactly the canonical owner-review three-array object."""

    try:
        encoded = json.dumps(
            {
                "reader_consequences": readers,
                "seat_binding_consequences": consequences,
                "seat_exclusions": exclusions,
            },
            ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ProtocolRefusal(
            "fleet_update_owner_review_invalid",
            "owner review evidence is not canonical JSON",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _validate_readers(readers: object, plan: Dict[str, object] | None = None) -> list[object]:
    if not isinstance(readers, list) or len(readers) > 1:
        raise ProtocolRefusal("fleet_update_plan_invalid", "reader consequences are not canonical")
    for row in readers:
        if not isinstance(row, dict) or set(row) != _READER_FIELDS:
            raise ProtocolRefusal("fleet_update_plan_invalid", "reader consequence has an invalid shape")
        if (
            row["reader"] != "codex_fleet_bus_gateway"
            or row["surface"] != "install_manifest"
            or type(row["current_schema_version"]) is not int
            or type(row["target_schema_version"]) is not int
            or row["current_schema_version"] != 0
            or row["target_schema_version"] != 1
            or row["added_fields"] != ["ownership"]
            or row["removed_fields"] != []
            or row["change"] != "additive_widened"
            or row["compatibility_after_update"] != "not_observed"
            or row["remedy"] != "review the Codex fleet gateway reader before applying the widened manifest vocabulary"
        ):
            raise ProtocolRefusal("fleet_update_plan_invalid", "reader consequence formula is invalid")
        for field in ("registry", "manifest_path"):
            value = row[field]
            if not isinstance(value, str) or not Path(value).is_absolute() or str(Path(value)) != value:
                raise ProtocolRefusal("fleet_update_plan_invalid", "reader consequence coordinate is invalid")
        if not isinstance(row["transport"], str):
            raise ProtocolRefusal("fleet_update_plan_invalid", "reader consequence coordinate is invalid")
        if plan is not None:
            inputs = plan.get("inputs")
            if not isinstance(inputs, dict):
                raise ProtocolRefusal("fleet_update_plan_invalid", "plan has no reader coordinate")
            expected_manifest = Path(str(inputs.get("destination"))) / ".floati-install" / "manifest.v0.json"
            if (
                row["registry"] != inputs.get("transport_registry")
                or row["transport"] != inputs.get("transport")
                or row["manifest_path"] != str(expected_manifest)
            ):
                raise ProtocolRefusal("fleet_update_plan_invalid", "reader consequence is not bound to the selected transport")
    return list(readers)


def _physical_step_kinds(plan: Dict[str, object]) -> list[str]:
    """Derive the exact append sequence from one already-observed plan."""

    bindings = plan.get("waiter_bindings")
    requires_epoch_roll = plan.get("requires_epoch_roll")
    if (
        not isinstance(bindings, list)
        or not bindings
        or type(requires_epoch_roll) is not bool
    ):
        raise ProtocolRefusal(
            "fleet_update_plan_invalid", "plan has no canonical physical step sequence"
        )
    kinds = ["shared_install"]
    kinds.extend("waiter_binding" for _ in bindings)
    if requires_epoch_roll:
        kinds.append("epoch_roll")
    kinds.append("transport_pins")
    return kinds


def recovery_witness(plan: Dict[str, object]) -> Dict[str, object]:
    """Return the one receipt-safe, self-authenticating recovery plan body.

    This deliberately preserves every authoritative preview field except its
    digest and display-only argv.  A reader can therefore re-run the ordinary
    plan semantic checks and derive the exact ordered physical sequence without
    reopening any external target or attempting digest inversion.
    """

    return copy.deepcopy({field: plan[field] for field in _PLAN_BODY_FIELDS})


def _authenticated_recovery_witness(
    record: Dict[str, object], root: Optional[FloatiRoot] = None,
) -> Dict[str, object]:
    witness = record.get("recovery_witness")
    if not isinstance(witness, dict) or set(witness) != _PLAN_BODY_FIELDS:
        raise IntegrityFailure(
            "fleet_update_receipt_invalid",
            "fleet_update_receipt_invalid",
        )
    candidate = dict(witness)
    candidate["plan_digest"] = record.get("plan_digest")
    candidate["apply_argv"] = []
    try:
        authenticated = authenticate_plan(candidate, record.get("actor"), root)
    except ProtocolRefusal as exc:
        raise IntegrityFailure(
            "fleet_update_receipt_invalid",
            "fleet_update_receipt_invalid",
        ) from exc
    if (
        authenticated.get("owner_review_batch_digest")
        != record.get("owner_review_batch_digest")
        or authenticated.get("reader_consequences")
        != record.get("reader_consequences")
        or authenticated.get("seat_binding_consequences")
        != record.get("seat_binding_consequences")
        or authenticated.get("seat_exclusions")
        != record.get("seat_exclusions")
    ):
        raise IntegrityFailure(
            "fleet_update_receipt_invalid",
            "fleet_update_receipt_invalid",
        )
    return authenticated


def _canonical_object_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ProtocolRefusal(
            "fleet_update_plan_invalid",
            "fleet update step witness is not canonical JSON",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _step_spec(
    plan: Dict[str, object], consequences: list[object], ordinal: int
) -> Dict[str, object]:
    """Derive every receipt field for one physical plan ordinal."""

    kinds = _physical_step_kinds(plan)
    if type(ordinal) is not int or ordinal < 1 or ordinal > len(kinds):
        raise ProtocolRefusal(
            "fleet_update_receipt_order_invalid",
            "fleet update step ordinal is outside the physical plan",
        )
    step_kind = kinds[ordinal - 1]
    if step_kind == "shared_install":
        pre_digest = plan["current_manifest_sha256"]
        post_digest = plan["target_manifest_sha256"]
        coordinate: Dict[str, object] = {
            "kind": step_kind,
            "destination": plan["inputs"]["destination"],
            "metadata_relative": _METADATA_RELATIVE,
        }
    elif step_kind == "waiter_binding":
        binding_index = sum(
            1 for prior in kinds[: ordinal - 1] if prior == "waiter_binding"
        )
        binding = plan["waiter_bindings"][binding_index]
        trust_keys = {
            row["hook_trust_key"]
            for row in consequences
            if isinstance(row, dict)
            and row.get("configuration") == binding["configuration"]
            and row.get("store") == binding["store"]
        }
        if len(trust_keys) > 1:
            raise ProtocolRefusal(
                "fleet_update_plan_invalid",
                "plan waiter coordinate has conflicting trust keys",
            )
        trust_key = next(iter(trust_keys), None)
        pre_digest = binding["configuration_sha256"]
        post_digest = binding["target_configuration_sha256"]
        coordinate = {
            "kind": step_kind,
            "index": binding_index,
            "configuration": binding["configuration"],
            "store": binding["store"],
            "trust_key": trust_key,
        }
    elif step_kind == "epoch_roll":
        pre_digest = plan["current_encoder_sha256"]
        post_digest = plan["target_encoder_sha256"]
        coordinate = {"kind": step_kind}
    else:
        pre_digest = _canonical_object_digest(plan["current_transport_pins"])
        post_digest = _canonical_object_digest(plan["target_transport_pins"])
        coordinate = {
            "kind": step_kind,
            "registry": plan["inputs"]["transport_registry"],
            "transport": plan["inputs"]["transport"],
        }
    if not _sha(pre_digest, _SHA256) or not _sha(post_digest, _SHA256):
        raise ProtocolRefusal(
            "fleet_update_plan_invalid",
            "fleet update step digests are not derived",
        )
    return {
        "step_kind": step_kind,
        "step_ordinal": ordinal,
        "pre_digest": pre_digest,
        "post_digest": post_digest,
        "step_coordinate": coordinate,
    }


def _terminal_projection(
    plan: Optional[Dict[str, object]], evidence: object,
) -> Dict[str, object]:
    """Derive the one terminal summary from closed transport-pin evidence.

    A completion stores no caller-selected summary facts.  The live path
    additionally proves this persisted projection is the authenticated plan;
    history can still reject every syntactically-valid substituted field using
    the immutable transport-pin step that immediately precedes completion.
    """

    if not isinstance(evidence, dict) or set(evidence) != _TRANSPORT_PIN_EVIDENCE_FIELDS:
        raise ProtocolRefusal(
            "fleet_update_completion_invalid",
            "transport pin evidence cannot derive a terminal projection",
        )
    projection = {
        field: evidence.get(field) for field in _TERMINAL_PROJECTION_FIELDS
    }
    if (
        evidence.get("kind") != "transport_pins"
        or not _absolute_coordinate(evidence.get("registry"))
        or not isinstance(evidence.get("transport"), str)
        or _IDENTIFIER.fullmatch(str(evidence["transport"])) is None
        or any(
            not isinstance(projection[field], str)
            for field in _TERMINAL_PROJECTION_FIELDS
        )
        or not _sha(projection["previous_source_sha"], _SHA1)
        or not _sha(projection["target_source_sha"], _SHA1)
        or projection["epoch_roll_state"] not in {"not_required", "completed"}
        or not _sha(projection["registry_before_sha256"], _SHA256)
        or not _sha(projection["registry_after_sha256"], _SHA256)
    ):
        raise ProtocolRefusal(
            "fleet_update_completion_invalid",
            "transport pin evidence has no valid terminal projection",
        )
    if plan is not None:
        expected = {
            "previous_source_sha": plan["current_source_sha"],
            "target_source_sha": plan["target_source_sha"],
            "epoch_roll_state": (
                "not_required" if plan["requires_epoch_roll"] is False else "completed"
            ),
            "registry_before_sha256": plan["transport_registry_sha256"],
            "registry_after_sha256": plan["target_transport_registry_sha256"],
        }
        if (
            evidence.get("registry") != plan["inputs"]["transport_registry"]
            or evidence.get("transport") != plan["inputs"]["transport"]
            or projection != expected
        ):
            raise ProtocolRefusal(
                "fleet_update_completion_invalid",
                "transport pin evidence diverges from the authenticated terminal projection",
            )
    return projection


def _owner_review_physical_invalid(detail: str) -> None:
    raise ProtocolRefusal("fleet_update_owner_review_invalid", detail)


def _planned_waiter_post_observation(
    plan: Dict[str, object], configuration: object, store: object
) -> Optional[Dict[str, object]]:
    projections = {
        (
            row.get("hook_trust_key"),
            row.get("target_hook_hash"),
            row.get("observed_trusted_hash"),
            row.get("observed_enabled"),
        )
        for row in plan.get("seat_binding_consequences", [])
        if isinstance(row, dict)
        and row.get("configuration") == configuration
        and row.get("store") == store
    }
    if len(projections) > 1:
        _owner_review_physical_invalid(
            "plan has conflicting waiter post consequence projections"
        )
    if not projections:
        return None
    projected = next(iter(projections))
    return {
        "hook_trust_key": projected[0],
        "current_hook_hash": projected[1],
        "observed_trusted_hash": projected[2],
        "observed_enabled": projected[3],
    }


_WAITER_POST_OBSERVATION_FIELDS = {
    "hook_trust_key",
    "current_hook_hash",
    "observed_trusted_hash",
    "observed_enabled",
}


def _waiter_post_from_evidence(evidence: object) -> Optional[Dict[str, object]]:
    if not isinstance(evidence, dict) or evidence.get("kind") != "waiter_binding":
        return None
    observation = evidence.get("hook_post_observation")
    if not isinstance(observation, dict) or set(observation) != _WAITER_POST_OBSERVATION_FIELDS:
        return None
    return observation


def _require_waiter_post_projection(
    plan: Dict[str, object],
    configuration: object,
    store: object,
    observed_post: object,
    *,
    persisted_evidence: object = None,
    allow_live_remediation: bool = False,
) -> None:
    persisted_post = (
        _waiter_post_from_evidence(persisted_evidence)
        if persisted_evidence is not None
        else None
    )
    expected_post = _planned_waiter_post_observation(plan, configuration, store)
    if expected_post is None:
        expected_post = persisted_post
    if persisted_evidence is not None and (
        expected_post is None or persisted_post != expected_post
    ):
        _owner_review_physical_invalid(
            "persisted waiter evidence diverges from its plan post projection"
        )
    if expected_post is None:
        return
    if (
        not isinstance(observed_post, dict)
        or set(observed_post) != _WAITER_POST_OBSERVATION_FIELDS
    ):
        _owner_review_physical_invalid("waiter post observation is incomplete")
    if allow_live_remediation:
        matches = (
            observed_post["hook_trust_key"] == expected_post["hook_trust_key"]
            and observed_post["current_hook_hash"]
            == expected_post["current_hook_hash"]
        )
    else:
        matches = observed_post == expected_post
    if not matches:
        _owner_review_physical_invalid(
            "waiter post observation diverges from its plan consequence"
        )


def _reconcile_owner_review_physical(
    plan: Dict[str, object],
    root: FloatiRoot,
    *,
    rows: Optional[list[Dict[str, object]]] = None,
    actor: Optional[str] = None,
    key: Optional[str] = None,
    allow_unreceipted_post: bool = True,
) -> list[str]:
    """Reconcile one authenticated owner-review batch with physical hook phases.

    A legal saga has a target-state prefix followed by an untouched pre-state
    suffix.  A target-state binding may lack its waiter receipt only at the
    single response-loss frontier.  Persisted waiter receipts prove their raw
    post facts; unfinished bindings are measured directly.
    """

    from .codex_hook_trust import observe_codex_waiter_hooks
    from .fleet_update import _owner_review_batch, _waiter_from_configuration
    from .waiter_bundle import waiter_runtime_digest

    if not isinstance(root, FloatiRoot):
        _owner_review_physical_invalid("owner review requires one validated root")
    bindings = plan.get("waiter_bindings")
    consequences = plan.get("seat_binding_consequences")
    exclusions = plan.get("seat_exclusions")
    target_digest = plan.get("target_waiter_digest")
    if (
        not isinstance(bindings, list)
        or not bindings
        or not isinstance(consequences, list)
        or not isinstance(exclusions, list)
        or not _sha(target_digest, _SHA256)
    ):
        _owner_review_physical_invalid("owner review plan facts are absent")

    selected_rows = rows or []
    related = [
        row
        for row in selected_rows
        if row.get("plan_digest") == plan.get("plan_digest")
        and (key is None or row.get("idempotency_key") == key)
        and (actor is None or row.get("actor") == actor)
    ]
    has_start = any(row.get("kind") == "fleet_update_started" for row in related)
    completed_by_index: dict[int, Dict[str, object]] = {}
    for record in related:
        if record.get("kind") != "fleet_update_step" or record.get("step_kind") != "waiter_binding":
            continue
        ordinal = record.get("step_ordinal")
        if type(ordinal) is not int:
            _owner_review_physical_invalid("waiter receipt has no physical ordinal")
        try:
            spec = _step_spec(plan, consequences, ordinal)
        except ProtocolRefusal as exc:
            _owner_review_physical_invalid("waiter receipt is outside the plan")
        expected = {
            "step_kind": spec["step_kind"],
            "step_ordinal": spec["step_ordinal"],
            "pre_digest": spec["pre_digest"],
            "post_digest": spec["post_digest"],
            "step_coordinate": spec["step_coordinate"],
        }
        if expected["step_kind"] != "waiter_binding" or any(
            record.get(field) != value for field, value in expected.items()
        ):
            _owner_review_physical_invalid("waiter receipt diverges from its plan")
        index = spec["step_coordinate"].get("index")
        if type(index) is not int or index in completed_by_index:
            _owner_review_physical_invalid("waiter receipt index is not unique")
        completed_by_index[index] = record
    completed_indices = sorted(completed_by_index)
    if completed_indices != list(range(len(completed_indices))):
        _owner_review_physical_invalid("waiter receipts are not a physical prefix")

    phases: list[str] = []
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            _owner_review_physical_invalid("waiter binding is not one object")
        try:
            configuration = Path(str(binding["configuration"]))
            store = Path(str(binding["store"]))
            configuration_bytes, current_tree, _named = _waiter_from_configuration(
                configuration, store
            )
            configuration_digest = hashlib.sha256(configuration_bytes).hexdigest()
            current_tree_digest = waiter_runtime_digest(current_tree)
        except (KeyError, OSError, ProtocolRefusal) as exc:
            raise ProtocolRefusal(
                "fleet_update_owner_review_invalid",
                "waiter owner-review coordinate is unreadable",
            ) from exc
        pre_digest = binding.get("configuration_sha256")
        post_digest = binding.get("target_configuration_sha256")
        if pre_digest == post_digest:
            phase = "post" if index in completed_by_index else "pre"
        elif configuration_digest == pre_digest:
            phase = "pre"
        elif configuration_digest == post_digest:
            phase = "post"
        else:
            _owner_review_physical_invalid("waiter hook bytes diverge from both plan phases")
        expected_tree_digest = (
            target_digest if phase == "post" else binding.get("current_tree_digest")
        )
        if current_tree_digest != expected_tree_digest:
            _owner_review_physical_invalid("waiter runtime diverges from its plan phase")
        phases.append(phase)

    post_count = 0
    for phase in phases:
        if phase == "post":
            post_count += 1
        else:
            break
    if phases != ["post"] * post_count + ["pre"] * (len(phases) - post_count):
        _owner_review_physical_invalid("waiter phases are not a post-prefix/pre-suffix")
    completed_count = len(completed_by_index)
    if completed_count > post_count or post_count > completed_count + 1:
        _owner_review_physical_invalid("waiter post frontier is not receipt contiguous")
    if post_count and not has_start and not allow_unreceipted_post:
        _owner_review_physical_invalid("an unstarted update cannot inherit waiter post state")

    # Once every waiter binding has its exact receipt and physical post bytes,
    # later seat retirement or mapping evolution cannot retroactively invalidate
    # the immutable saga.  Reconcile the persisted evidence directly against
    # the plan projection and current raw trust state without re-running live
    # eligibility classification.
    if completed_count == len(bindings) and post_count == len(bindings):
        for index, binding in enumerate(bindings):
            if not isinstance(binding, dict):
                _owner_review_physical_invalid("waiter binding is not one object")
            configuration = Path(str(binding["configuration"]))
            trust_rows = observe_codex_waiter_hooks(configuration)
            if len(trust_rows) != 1:
                _owner_review_physical_invalid(
                    "completed waiter trust coordinate is not unique"
                )
            observation = trust_rows[0]
            actual_post = {
                "hook_trust_key": observation["hook_trust_key"],
                "current_hook_hash": observation["hook_trust_current_hash"],
                "observed_trusted_hash": observation["hook_trust_observed_hash"],
                "observed_enabled": observation["hook_enabled"],
            }
            completed = completed_by_index[index]
            _require_waiter_post_projection(
                plan,
                binding.get("configuration"),
                binding.get("store"),
                actual_post,
                persisted_evidence=completed.get("step_evidence"),
                allow_live_remediation=True,
            )
        return phases

    try:
        fresh_consequences, fresh_exclusions = _owner_review_batch(
            root, bindings, str(target_digest)
        )
    except ProtocolRefusal as exc:
        raise ProtocolRefusal(
            "fleet_update_owner_review_invalid",
            "owner review could not be re-observed",
        ) from exc
    if fresh_exclusions != exclusions:
        _owner_review_physical_invalid("owner-review exclusions changed after planning")

    def identity(row: Dict[str, object]) -> tuple[object, ...]:
        return tuple(row.get(field) for field in _CONSEQUENCE_IDENTITY_FIELDS)

    if [identity(row) for row in fresh_consequences] != [
        identity(row) for row in consequences
    ]:
        _owner_review_physical_invalid("owner-review seat eligibility changed after planning")
    binding_indices = {
        (str(binding["configuration"]), str(binding["store"])): index
        for index, binding in enumerate(bindings)
    }
    for planned, fresh in zip(consequences, fresh_consequences):
        if not isinstance(planned, dict) or not isinstance(fresh, dict):
            _owner_review_physical_invalid("owner-review consequence is not one object")
        coordinate = (str(planned.get("configuration")), str(planned.get("store")))
        index = binding_indices.get(coordinate)
        if index is None:
            _owner_review_physical_invalid("owner-review consequence lost its binding")
        phase = phases[index]
        binding = bindings[index]
        expected_current_hash = planned[
            "target_hook_hash" if phase == "post" else "current_hook_hash"
        ]
        expected_waiter_digest = (
            target_digest if phase == "post" else binding["current_tree_digest"]
        )
        if (
            fresh.get("hook_trust_key") != planned.get("hook_trust_key")
            or fresh.get("current_hook_hash") != expected_current_hash
            or fresh.get("target_hook_hash") != planned.get("target_hook_hash")
            or fresh.get("current_waiter_digest") != expected_waiter_digest
            or fresh.get("target_waiter_digest") != target_digest
        ):
            _owner_review_physical_invalid("owner-review hook phase changed after planning")
        completed = completed_by_index.get(index)
        expected_observation = {
            "hook_trust_key": planned["hook_trust_key"],
            "current_hook_hash": planned["target_hook_hash"],
            "observed_trusted_hash": planned["observed_trusted_hash"],
            "observed_enabled": planned["observed_enabled"],
        }
        if completed is not None:
            if completed.get("step_evidence") != {
                "kind": "waiter_binding",
                "hook_post_observation": expected_observation,
            }:
                _owner_review_physical_invalid(
                    "persisted waiter evidence diverges from the plan post projection"
                )
        elif (
            fresh.get("observed_trusted_hash") != planned.get("observed_trusted_hash")
            or fresh.get("observed_enabled") != planned.get("observed_enabled")
        ):
            _owner_review_physical_invalid("owner-review raw hook facts changed after planning")
    return phases


class FleetUpdateReceiptLedger:
    """Strict physical-order evidence for one actor's fleet update saga."""

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("fleet_update_root_invalid", "receipt ledger requires a validated Floati root")
        self.root = root
        self._prepared_steps: Dict[tuple[str, str, str, Optional[str]], Dict[str, object]] = {}

    def _require_execution_token(
        self, token: object
    ) -> _FleetUpdateExecutionToken:
        if (
            not isinstance(token, _FleetUpdateExecutionToken)
            or token._authority is not _EXECUTION_TOKEN_AUTHORITY
            or token._guard.root is not self.root
            or token._guard._token is not token
            or token._guard._descriptor is None
            or token.pid != os.getpid()
            or token.thread_id != threading.get_ident()
        ):
            raise ProtocolRefusal(
                "fleet_update_execution_lock_invalid",
                "fleet update mutation lacks this thread's exact root execution guard",
            )
        try:
            opened = os.fstat(token._guard._descriptor)
            parent = os.lstat(token._guard.path)
            current = os.lstat(self.root.path)
        except OSError as exc:
            raise DurabilityFailure(
                "fleet_update_execution_lock_unavailable",
                "fleet update root identity cannot be revalidated",
            ) from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or opened.st_dev != token.parent_device
            or opened.st_ino != token.parent_inode
            or parent.st_dev != token.parent_device
            or parent.st_ino != token.parent_inode
            or current.st_dev != token.device
            or current.st_ino != token.inode
        ):
            raise ProtocolRefusal(
                "fleet_update_execution_lock_invalid",
                "fleet update root identity changed while guarded",
            )
        return token

    def _observe_step(
        self,
        plan: Dict[str, object],
        actor: str,
        key: str,
        spec: Dict[str, object],
        *,
        post_mutation_observation: bool = False,
    ) -> Dict[str, object]:
        """Measure one plan-covered physical step; never accept testimony."""

        step_kind = spec["step_kind"]
        if step_kind == "shared_install":
            from .fleet_update import (
                _shared_install_join_id,
                _shared_install_recovery_evidence,
            )

            destination = Path(str(plan["inputs"]["destination"]))
            return _shared_install_recovery_evidence(
                destination,
                destination,
                str(spec["pre_digest"]),
                str(spec["post_digest"]),
                _shared_install_join_id(str(plan["plan_digest"]), actor, key),
                plan["shared_install_intents"],
            )
        if step_kind == "waiter_binding":
            from .codex_hook_install import plan_waiter_rebind, waiter_runtime_digest
            from .codex_hook_trust import observe_codex_waiter_hooks

            coordinate = spec["step_coordinate"]
            configuration = Path(str(coordinate["configuration"]))
            store = Path(str(coordinate["store"]))
            target_digest = str(plan["target_waiter_digest"])
            try:
                observed = configuration.read_bytes()
            except OSError as exc:
                raise ProtocolRefusal(
                    "fleet_update_waiter_binding_invalid",
                    "waiter hook document is unreadable",
                ) from exc
            observed_digest = hashlib.sha256(observed).hexdigest()
            if observed_digest == spec["pre_digest"] and spec["pre_digest"] != spec["post_digest"]:
                return {"phase": "pre", "evidence": None}
            if observed_digest != spec["post_digest"]:
                return {"phase": "divergent", "evidence": None}
            target = store / target_digest
            try:
                if (
                    not os.path.lexists(target)
                    or target.is_symlink()
                    or not target.is_dir()
                    or waiter_runtime_digest(target) != target_digest
                ):
                    raise OSError("target generation is absent or divergent")
            except (OSError, ProtocolRefusal) as exc:
                raise ProtocolRefusal(
                    "fleet_update_step_evidence_missing",
                    "waiter step has no exact plan-covered target generation",
                ) from exc
            staged = plan_waiter_rebind(configuration, store, target_digest)
            if staged.get("after") != observed:
                return {"phase": "divergent", "evidence": None}
            trust_rows = observe_codex_waiter_hooks(configuration)
            if (
                len(trust_rows) != 1
                or trust_rows[0].get("hook_trust_current_hash")
                != staged.get("target_hook_hash")
                or coordinate.get("trust_key") is not None
                and coordinate.get("trust_key") != trust_rows[0].get("hook_trust_key")
            ):
                return {"phase": "divergent", "evidence": None}
            observation = trust_rows[0]
            observed_post = {
                "hook_trust_key": observation["hook_trust_key"],
                "current_hook_hash": observation["hook_trust_current_hash"],
                "observed_trusted_hash": observation["hook_trust_observed_hash"],
                "observed_enabled": observation["hook_enabled"],
            }
            return {
                "phase": (
                    "unchanged"
                    if spec["pre_digest"] == spec["post_digest"]
                    else "post"
                ),
                "evidence": {
                    "kind": "waiter_binding",
                    "hook_post_observation": observed_post,
                },
            }
        if step_kind == "transport_pins":
            from .fleet_update_registry import verify_transport_pins_post

            inputs = plan["inputs"]
            registry = Path(str(inputs["transport_registry"]))
            transport_name = str(inputs["transport"])
            target = plan["target_transport_pins"]
            if post_mutation_observation:
                # A persisted pins receipt has already authenticated the only
                # possible physical post-state.  Do not reclassify a missing
                # or malformed registry as a hypothetical pre-mutation input.
                verify_transport_pins_post(
                    registry, transport_name,
                    manifest_sha256=str(target["manifest_sha256"]),
                    source_sha=str(target["source_sha"]),
                    expected_registry_sha256=str(
                        plan["target_transport_registry_sha256"]
                    ),
                )
                return {
                    "phase": "post",
                    "evidence": {
                        "kind": "transport_pins",
                        "registry": str(registry),
                        "transport": transport_name,
                        "registry_before_sha256": str(plan["transport_registry_sha256"]),
                        "registry_after_sha256": str(plan["target_transport_registry_sha256"]),
                        "previous_source_sha": str(plan["current_source_sha"]),
                        "target_source_sha": str(plan["target_source_sha"]),
                        "epoch_roll_state": (
                            "not_required"
                            if plan["requires_epoch_roll"] is False
                            else "completed"
                        ),
                    },
                }
            try:
                raw = registry.read_bytes()
                document = json.loads(raw.decode("utf-8"))
                transports = document["transports"]
                if not isinstance(transports, dict):
                    raise TypeError("registry transports are not an object")
                selected = transports[transport_name]
                if not isinstance(selected, dict):
                    raise TypeError("selected transport is not an object")
                observed = {
                    field: selected.get(field) for field in _TRANSPORT_PIN_FIELDS
                }
                identity = os.stat(registry, follow_symlinks=False)
                if not stat.S_ISREG(identity.st_mode):
                    raise TypeError("transport registry is no longer a file")
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ProtocolRefusal(
                    "fleet_update_transport_registry_invalid",
                    "transport pin receipt cannot read the selected registry",
                ) from exc
            current = plan["current_transport_pins"]
            if observed == current:
                if hashlib.sha256(raw).hexdigest() != plan["transport_registry_sha256"]:
                    return {"phase": "divergent", "evidence": None}
                return {
                    "phase": "pre",
                    "evidence": None,
                    "registry_identity": (identity.st_dev, identity.st_ino),
                }
            if (
                observed != target
                or hashlib.sha256(raw).hexdigest()
                != plan["target_transport_registry_sha256"]
            ):
                return {"phase": "divergent", "evidence": None}
            verify_transport_pins_post(
                registry, transport_name,
                manifest_sha256=str(target["manifest_sha256"]),
                source_sha=str(target["source_sha"]),
                expected_registry_sha256=str(
                    plan["target_transport_registry_sha256"]
                ),
            )
            return {
                "phase": "post",
                "evidence": {
                    "kind": "transport_pins",
                    "registry": str(registry),
                    "transport": transport_name,
                    "registry_before_sha256": str(plan["transport_registry_sha256"]),
                    "registry_after_sha256": hashlib.sha256(raw).hexdigest(),
                    "previous_source_sha": str(plan["current_source_sha"]),
                    "target_source_sha": str(plan["target_source_sha"]),
                    "epoch_roll_state": (
                        "not_required"
                        if plan["requires_epoch_roll"] is False
                        else "completed"
                    ),
                },
            }
        raise ProtocolRefusal(
            "fleet_update_step_evidence_missing",
            f"{step_kind} has no plan-covered physical verifier",
        )

    @staticmethod
    def relative(actor: str) -> Path:
        return Path("receipts/fleet-update") / f"{validate_identifier(actor, 'actor')}.jsonl"

    def rows(self, actor: str) -> list[Dict[str, object]]:
        rows = read_records_snapshot(self.root, self.relative(actor), allowed_kinds=_KINDS)
        self._validate_history(rows, actor)
        return rows

    def _root_has_active_saga(self, selected_actor: Optional[str] = None) -> bool:
        """Read every product-owned actor history beneath this exact root."""

        directory = self.root.path / "receipts" / "fleet-update"
        try:
            if directory.is_symlink():
                raise IntegrityFailure(
                    "fleet_update_receipt_invalid",
                    "fleet_update_receipt_invalid",
                )
            if not directory.exists():
                return False
            if not directory.is_dir():
                raise IntegrityFailure(
                    "fleet_update_receipt_invalid",
                    "fleet_update_receipt_invalid",
                )
            paths = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise IntegrityFailure(
                "fleet_update_receipt_invalid",
                "fleet_update_receipt_invalid",
            ) from exc
        active_coordinates: list[tuple[str, object, object]] = []
        for path in paths:
            actor: Optional[str] = None
            try:
                if (
                    path.name.endswith(".jsonl.lock")
                    and path.is_file()
                    and not path.is_symlink()
                ):
                    continue
                if path.is_symlink() or not path.is_file() or path.suffix != ".jsonl":
                    raise IntegrityFailure(
                        "fleet_update_receipt_invalid",
                        "fleet_update_receipt_invalid",
                    )
                actor = validate_identifier(path.stem, "actor")
                rows = self.rows(actor)
            except IntegrityFailure:
                if actor == selected_actor:
                    raise
                raise IntegrityFailure(
                    "fleet_update_receipt_invalid",
                    "fleet_update_receipt_invalid",
                ) from None
            except (OSError, ProtocolRefusal) as exc:
                raise IntegrityFailure(
                    "fleet_update_receipt_invalid",
                    "fleet_update_receipt_invalid",
                ) from exc
            started = {
                (row.get("plan_digest"), row.get("idempotency_key"))
                for row in rows
                if row.get("kind") == "fleet_update_started"
            }
            completed = {
                (row.get("plan_digest"), row.get("idempotency_key"))
                for row in rows
                if row.get("kind") == "fleet_update_completed"
            }
            active_coordinates.extend(
                (str(actor), plan_digest, key)
                for plan_digest, key in started - completed
            )
        if len(active_coordinates) > 1:
            raise IntegrityFailure(
                "fleet_update_receipt_order_invalid",
                "fleet_update_receipt_order_invalid",
            )
        return bool(active_coordinates)

    @staticmethod
    def _plan(plan: object, actor: str, key: object) -> tuple[Dict[str, object], str, str, list[object], list[object], list[object], str]:
        if not isinstance(plan, dict):
            raise ProtocolRefusal("fleet_update_plan_invalid", "receipt start requires one exact plan object")
        digest = plan.get("plan_digest")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ProtocolRefusal("fleet_update_plan_invalid", "plan has no valid digest")
        selected_actor = validate_identifier(actor, "actor")
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise ProtocolRefusal("idempotency_key_invalid", "idempotency key is terminal-safe and between 1 and 128 bytes")
        if "reader_consequences" not in plan:
            raise ProtocolRefusal("fleet_update_plan_invalid", "plan omits reader consequences")
        readers = _validate_readers(plan["reader_consequences"], plan)
        consequences = plan.get("seat_binding_consequences", [])
        exclusions = plan.get("seat_exclusions", [])
        if not isinstance(consequences, list) or not isinstance(exclusions, list):
            raise ProtocolRefusal("fleet_update_plan_invalid", "owner review arrays are invalid")
        if consequences != sorted(consequences, key=lambda row: (str(row.get("node_id")), str(row.get("workspace")), str(row.get("configuration")))):
            raise ProtocolRefusal("fleet_update_plan_invalid", "seat binding consequences are not canonical")
        if exclusions != sorted(exclusions, key=lambda row: (str(row.get("node_id")), str(row.get("workspace")), str(row.get("reason")))):
            raise ProtocolRefusal("fleet_update_plan_invalid", "seat exclusions are not canonical")
        batch = owner_review_batch_digest(readers, consequences, exclusions)
        if plan.get("owner_review_batch_digest", batch) != batch:
            raise ProtocolRefusal("fleet_update_plan_invalid", "plan owner review digest is not canonical")
        return dict(plan), digest, selected_actor, readers, list(consequences), list(exclusions), batch

    def _validate_history(self, rows: list[Dict[str, object]], actor: str) -> None:
        prior: Optional[str] = None
        starts: dict[str, tuple[Dict[str, object], Dict[str, object]]] = {}
        completed: set[str] = set()
        next_step_ordinal: dict[str, int] = {}
        seen = set()
        for index, row in enumerate(rows):
            try:
                validate_record(row, row["tenant_id"], frozenset(_KINDS), integrity=True)
            except (KeyError, IntegrityFailure) as exc:
                raise IntegrityFailure("fleet_update_receipt_invalid", "fleet update receipt is malformed") from exc
            if row.get("actor") != actor or row.get("id") in seen:
                raise IntegrityFailure("fleet_update_receipt_invalid", "fleet update receipt actor or identity is invalid")
            seen.add(row["id"])
            if row.get("predecessor_receipt_id") != prior:
                raise IntegrityFailure("fleet_update_receipt_order_invalid", "fleet update receipts are not in physical predecessor order")
            try:
                observed_batch = owner_review_batch_digest(
                    row.get("reader_consequences"),
                    row.get("seat_binding_consequences"),
                    row.get("seat_exclusions"),
                )
            except ProtocolRefusal as exc:
                raise IntegrityFailure(
                    "fleet_update_owner_review_invalid",
                    "fleet update receipt owner review evidence is not canonical JSON",
                ) from exc
            if row.get("owner_review_batch_digest") != observed_batch:
                raise IntegrityFailure("fleet_update_receipt_invalid", "fleet update receipt owner review digest is invalid")
            key = str(row.get("idempotency_key"))
            if row.get("kind") == "fleet_update_started":
                if key in starts:
                    raise IntegrityFailure("fleet_update_receipt_invalid", "fleet update key is already bound to one start")
                plan = _authenticated_recovery_witness(row, self.root)
                starts[key] = (row, plan)
                next_step_ordinal[key] = 1
            else:
                started = starts.get(key)
                if started is None:
                    raise IntegrityFailure("fleet_update_receipt_order_invalid", "fleet update row lacks a physical start")
                start, plan = started
                if key in completed:
                    raise IntegrityFailure("fleet_update_receipt_order_invalid", "fleet update receipt follows terminal completion")
                for field in ("plan_digest", "consent_receipt_id", "owner_review_batch_digest", "reader_consequences", "seat_binding_consequences", "seat_exclusions"):
                    if row.get(field) != start.get(field):
                        raise IntegrityFailure("fleet_update_receipt_invalid", "fleet update row diverges from its start binding")
                if row.get("kind") == "fleet_update_completed":
                    if (
                        row.get("moves") != plan["moves"]
                        or row.get("unchanged") != plan["unchanged"]
                    ):
                        raise IntegrityFailure(
                            "fleet_update_completion_invalid",
                            "fleet update completion move summary diverges from its start witness",
                        )
                    step_ids = [prior_row["id"] for prior_row in rows[:index] if prior_row.get("idempotency_key") == key and prior_row.get("kind") == "fleet_update_step"]
                    if row.get("step_receipt_ids") != step_ids:
                        raise IntegrityFailure("fleet_update_receipt_invalid", "fleet update completion step ids do not match physical history")
                    pins = [
                        prior_row for prior_row in rows[:index]
                        if prior_row.get("idempotency_key") == key
                        and prior_row.get("kind") == "fleet_update_step"
                        and prior_row.get("step_kind") == "transport_pins"
                    ]
                    if len(pins) != 1:
                        raise IntegrityFailure(
                            "fleet_update_completion_invalid",
                            "fleet update completion has no unique transport pin step",
                        )
                    try:
                        projection = _terminal_projection(
                            plan, pins[0].get("step_evidence")
                        )
                    except ProtocolRefusal as exc:
                        raise IntegrityFailure(
                            "fleet_update_completion_invalid",
                            "fleet update terminal projection is not receipt-derived",
                        ) from exc
                    if any(row.get(field) != value for field, value in projection.items()):
                        raise IntegrityFailure(
                            "fleet_update_completion_invalid",
                            "fleet update completion terminal projection diverges from pins",
                        )
                    completed.add(key)
                elif row.get("kind") == "fleet_update_step":
                    if row.get("step_ordinal") != next_step_ordinal.get(key):
                        raise IntegrityFailure("fleet_update_receipt_order_invalid", "fleet update step ordinal is not globally contiguous")
                    try:
                        spec = _step_spec(
                            plan, plan["seat_binding_consequences"],
                            int(row["step_ordinal"]),
                        )
                        self._validate_step_against_spec(row, spec)
                        self._validate_history_step_evidence(
                            plan, actor, key, row, spec,
                        )
                    except (KeyError, ProtocolRefusal) as exc:
                        raise IntegrityFailure(
                            "fleet_update_step_invalid",
                            "fleet_update_step_invalid",
                        ) from exc
                    next_step_ordinal[key] = next_step_ordinal.get(key, 1) + 1
            prior = str(row["id"])

    @staticmethod
    def _validate_history_step_evidence(
        plan: Dict[str, object], actor: str, key: str,
        record: Dict[str, object], spec: Dict[str, object],
    ) -> None:
        """Validate durable step evidence solely from the authenticated plan."""

        evidence = record.get("step_evidence")
        kind = spec["step_kind"]
        if not isinstance(evidence, dict) or evidence.get("kind") != kind:
            raise ProtocolRefusal("fleet_update_step_invalid", "fleet_update_step_invalid")
        if kind == "shared_install":
            from .fleet_update import _shared_install_join_id
            from .wiring_journal import journal_path

            required = {
                "kind", "journal_path", "join_id", "predecessor_ordinal",
                "predecessor_entry_hash", "first_ordinal", "last_ordinal",
                "entry_hashes",
            }
            entries = evidence.get("entry_hashes")
            first = evidence.get("first_ordinal")
            last = evidence.get("last_ordinal")
            predecessor = evidence.get("predecessor_ordinal")
            predecessor_hash = evidence.get("predecessor_entry_hash")
            if (
                set(evidence) != required
                or evidence.get("journal_path")
                != str(journal_path(Path(str(plan["inputs"]["destination"]))))
                or evidence.get("join_id")
                != _shared_install_join_id(str(plan["plan_digest"]), actor, key)
                or type(first) is not int or first < 1
                or type(last) is not int or last < first
                or not isinstance(entries, list)
                or len(entries) != len(plan["shared_install_intents"])
                or last - first + 1 != len(entries)
                or any(not _sha(value, _SHA256) for value in entries)
                or (
                    (first == 1 and (predecessor is not None or predecessor_hash is not None))
                    or (first > 1 and (predecessor != first - 1 or not _sha(predecessor_hash, _SHA256)))
                )
            ):
                raise ProtocolRefusal("fleet_update_step_invalid", "fleet_update_step_invalid")
            return
        if kind == "waiter_binding":
            coordinate = spec["step_coordinate"]
            observation = _waiter_post_from_evidence(evidence)
            if (
                observation is None
                or not isinstance(observation.get("hook_trust_key"), str)
                or not _sha(observation.get("current_hook_hash"), _SHA256)
                or (
                    observation.get("observed_trusted_hash") is not None
                    and not _sha(observation.get("observed_trusted_hash"), _SHA256)
                )
                or (
                    observation.get("observed_enabled") is not None
                    and type(observation.get("observed_enabled")) is not bool
                )
            ):
                raise ProtocolRefusal("fleet_update_step_invalid", "fleet_update_step_invalid")
            _require_waiter_post_projection(
                plan, coordinate.get("configuration"), coordinate.get("store"),
                observation, persisted_evidence=evidence,
            )
            return
        if kind == "transport_pins":
            _terminal_projection(plan, evidence)
            return
        if evidence != {"kind": "epoch_roll"}:
            raise ProtocolRefusal("fleet_update_step_invalid", "fleet_update_step_invalid")

    @staticmethod
    def _base(
        *, kind: str, plan_digest: str, actor: str, key: str, consent: Dict[str, object], predecessor: Optional[str],
        readers: list[object], consequences: list[object], exclusions: list[object], batch: str,
        operation: str, step_kind: Optional[str], pre_digest: Optional[str], post_digest: Optional[str], state: str,
        step_ordinal: Optional[int] = None, step_coordinate: Optional[Dict[str, object]] = None,
        commit_disposition: Optional[str] = None, step_evidence: Optional[Dict[str, object]] = None,
        recovery_witness: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        if kind == "fleet_update_step" and (
            step_ordinal is None
            or step_coordinate is None
            or commit_disposition is None
            or step_evidence is None
        ):
            raise ProtocolRefusal(
                "fleet_update_step_invalid",
                "step receipt facts must be ledger-derived before construction",
            )
        record = {
            "schema_version": 1, "id": {"fleet_update_started": "fleet-update-started-", "fleet_update_step": "fleet-update-step-", "fleet_update_completed": "fleet-update-completed-"}[kind] + uuid7_hex(),
            "tenant_id": "", "timestamp": utc_now(), "kind": kind,
            "plan_digest": plan_digest, "actor": actor, "consent_receipt_id": consent["id"],
            "operation": operation, "step_kind": step_kind, "pre_digest": pre_digest, "post_digest": post_digest,
            "step_ordinal": step_ordinal, "step_coordinate": step_coordinate,
            "commit_disposition": commit_disposition, "step_evidence": step_evidence,
            "state": state, "predecessor_receipt_id": predecessor, "idempotency_key": key,
            "owner_review_batch_digest": batch, "reader_consequences": readers,
            "seat_binding_consequences": consequences, "seat_exclusions": exclusions,
        }
        if kind == "fleet_update_started":
            if recovery_witness is None:
                raise ProtocolRefusal(
                    "fleet_update_plan_invalid",
                    "fleet update start requires one plan-derived recovery witness",
                )
            record["recovery_witness"] = recovery_witness
        if kind == "fleet_update_completed":
            record.update(
                previous_source_sha="a" * 40,
                target_source_sha="a" * 40,
                epoch_roll_state="not_required",
                registry_before_sha256="a" * 64,
                registry_after_sha256="a" * 64,
            )
        return record

    @staticmethod
    def _active_consent(plan: Dict[str, object]) -> Dict[str, object]:
        inputs = plan.get("inputs")
        if not isinstance(inputs, dict) or not isinstance(inputs.get("destination"), str) or not isinstance(inputs.get("channel"), str):
            raise ProtocolRefusal("fleet_update_plan_invalid", "plan has no destination-bound consent coordinate")
        return UpdateConsentLedger(Path(inputs["destination"])).require_active(inputs["channel"])

    def start(self, plan: object, actor: str, key: str) -> Dict[str, object]:
        with _FleetUpdateExecutionGuard(self.root) as execution_token:
            return self._start_guarded(plan, actor, key, execution_token)

    def _start_guarded(
        self,
        plan: object,
        actor: str,
        key: str,
        execution_token: _FleetUpdateExecutionToken,
    ) -> Dict[str, object]:
        self._require_execution_token(execution_token)
        authenticated = authenticate_plan(plan, actor, self.root)
        plan, digest, selected_actor, readers, consequences, exclusions, batch = self._plan(authenticated, actor, key)
        retry = self._exact_retry_guarded(
            plan, selected_actor, key, execution_token
        )
        if retry is not None:
            return retry
        consent = self._active_consent(plan)
        return self._start_authorized(
            plan, digest, selected_actor, key, readers, consequences,
            exclusions, batch, consent, execution_token,
        )

    def _start_authorized(self, plan: Dict[str, object], digest: str, selected_actor: str, key: str, readers: list[object], consequences: list[object], exclusions: list[object], batch: str, consent: Dict[str, object], execution_token: _FleetUpdateExecutionToken) -> Dict[str, object]:
        """Append a first start after the caller has made the authoritative consent join."""

        self._require_execution_token(execution_token)
        self._root_has_active_saga(selected_actor)
        relative = self.relative(selected_actor)

        def decide(rows: list[Dict[str, object]]):
            self._validate_history(rows, selected_actor)
            matching = [row for row in rows if row.get("idempotency_key") == key]
            if not matching and self._root_has_active_saga(selected_actor):
                raise ProtocolRefusal(
                    "fleet_update_receipt_order_invalid",
                    "fleet_update_receipt_order_invalid",
                )
            _reconcile_owner_review_physical(
                plan,
                self.root,
                rows=rows,
                actor=selected_actor,
                key=key,
                allow_unreceipted_post=bool(matching),
            )
            if matching:
                start = matching[0]
                compare = ("plan_digest", "actor", "owner_review_batch_digest", "reader_consequences", "seat_binding_consequences", "seat_exclusions")
                expected = {"plan_digest": digest, "actor": selected_actor, "owner_review_batch_digest": batch, "reader_consequences": readers, "seat_binding_consequences": consequences, "seat_exclusions": exclusions}
                if start.get("kind") != "fleet_update_started" or any(start.get(field) != expected[field] for field in compare):
                    raise ProtocolRefusal("fleet_update_idempotency_conflict", "fleet update key already names different content")
                return dict(start), None
            record = self._base(kind="fleet_update_started", plan_digest=digest, actor=selected_actor, key=key, consent=consent, predecessor=rows[-1]["id"] if rows else None, readers=readers, consequences=consequences, exclusions=exclusions, batch=batch, operation="start", step_kind=None, pre_digest=None, post_digest=None, state="started", recovery_witness=recovery_witness(plan))
            record["tenant_id"] = self.root.tenant_id
            return record, record
        return transact(self.root, relative, decide, allowed_kinds=_KINDS)

    def exact_retry(self, plan: object, actor: str, key: str) -> Optional[Dict[str, object]]:
        """Return an already-started exact operation without revisiting consent authority."""

        with _FleetUpdateExecutionGuard(self.root) as execution_token:
            return self._exact_retry_guarded(plan, actor, key, execution_token)

    def _exact_retry_guarded(
        self,
        plan: object,
        actor: str,
        key: str,
        execution_token: _FleetUpdateExecutionToken,
    ) -> Optional[Dict[str, object]]:
        self._require_execution_token(execution_token)
        authenticated = authenticate_plan(plan, actor, self.root)
        _plan, digest, selected_actor, readers, consequences, exclusions, batch = self._plan(authenticated, actor, key)
        self._root_has_active_saga(selected_actor)
        rows = self.rows(selected_actor)
        matching = [row for row in rows if row.get("idempotency_key") == key]
        _reconcile_owner_review_physical(
            _plan,
            self.root,
            rows=rows,
            actor=selected_actor,
            key=key,
            allow_unreceipted_post=bool(matching),
        )
        if not matching:
            return None
        start = matching[0]
        expected = {"plan_digest": digest, "actor": selected_actor, "owner_review_batch_digest": batch, "reader_consequences": readers, "seat_binding_consequences": consequences, "seat_exclusions": exclusions}
        if start.get("kind") != "fleet_update_started" or any(start.get(field) != value for field, value in expected.items()):
            raise ProtocolRefusal("fleet_update_idempotency_conflict", "fleet update key already names different content")
        return dict(start)

    @staticmethod
    def _validate_step_against_spec(
        record: Dict[str, object],
        spec: Dict[str, object],
        evidence: Optional[Dict[str, object]] = None,
    ) -> None:
        expected = {
            "step_kind": spec["step_kind"],
            "step_ordinal": spec["step_ordinal"],
            "pre_digest": spec["pre_digest"],
            "post_digest": spec["post_digest"],
            "step_coordinate": spec["step_coordinate"],
        }
        if any(record.get(field) != value for field, value in expected.items()):
            raise IntegrityFailure(
                "fleet_update_step_invalid",
                "persisted fleet update step diverges from its authenticated plan",
            )
        if evidence is not None and record.get("step_evidence") != evidence:
            raise IntegrityFailure(
                "fleet_update_step_invalid",
                "persisted fleet update step evidence diverges from physical state",
            )

    @staticmethod
    def _start_for_step(
        rows: list[Dict[str, object]],
        digest: str,
        key: str,
        *,
        readers: list[object],
        consequences: list[object],
        exclusions: list[object],
        batch: str,
    ) -> Dict[str, object]:
        start = next(
            (
                row
                for row in rows
                if row.get("kind") == "fleet_update_started"
                and row.get("plan_digest") == digest
                and row.get("idempotency_key") == key
            ),
            None,
        )
        if start is None:
            raise ProtocolRefusal(
                "fleet_update_receipt_order_invalid",
                "fleet update step lacks a matching start",
            )
        expected = {
            "owner_review_batch_digest": batch,
            "reader_consequences": readers,
            "seat_binding_consequences": consequences,
            "seat_exclusions": exclusions,
        }
        if any(start.get(field) != value for field, value in expected.items()):
            raise ProtocolRefusal(
                "fleet_update_idempotency_conflict",
                "fleet update step plan diverges from its start binding",
            )
        return start

    def _step_transaction_context(
        self,
        rows: list[Dict[str, object]],
        plan: Dict[str, object],
        digest: str,
        actor: str,
        key: str,
        predecessor: Optional[str],
        readers: list[object],
        consequences: list[object],
        exclusions: list[object],
        batch: str,
    ) -> tuple[Dict[str, object], Optional[Dict[str, object]], Dict[str, object]]:
        """Resolve one existing retry or the sole next physical plan step."""

        self._validate_history(rows, actor)
        start = self._start_for_step(
            rows, digest, key, readers=readers, consequences=consequences,
            exclusions=exclusions, batch=batch,
        )
        related = [
            row
            for row in rows
            if row.get("plan_digest") == digest
            and row.get("idempotency_key") == key
        ]
        existing = next(
            (
                row
                for row in related
                if row.get("kind") == "fleet_update_step"
                and row.get("predecessor_receipt_id") == predecessor
            ),
            None,
        )
        if existing is not None:
            ordinal = existing.get("step_ordinal")
            if type(ordinal) is not int:
                raise IntegrityFailure(
                    "fleet_update_step_invalid",
                    "persisted fleet update step has no physical ordinal",
                )
            return start, existing, _step_spec(plan, consequences, ordinal)
        if any(row.get("kind") == "fleet_update_completed" for row in related):
            raise ProtocolRefusal(
                "fleet_update_receipt_order_invalid",
                "fleet update step cannot follow terminal completion",
            )
        if not rows or rows[-1].get("id") != predecessor:
            raise ProtocolRefusal(
                "fleet_update_receipt_order_invalid",
                "fleet update step must join the physical predecessor",
            )
        ordinal = 1 + sum(
            row.get("kind") == "fleet_update_step" for row in related
        )
        return start, None, _step_spec(plan, consequences, ordinal)

    def _observe_persisted_step(
        self,
        plan: Dict[str, object],
        actor: str,
        key: str,
        record: Dict[str, object],
        spec: Dict[str, object],
    ) -> Dict[str, object]:
        # Establish the authenticated receipt frontier before touching the
        # physical target.  Its transport post-state means later read failures
        # are durability failures, not a new pre-mutation refusal.
        self._validate_step_against_spec(record, spec)
        if spec.get("step_kind") == "transport_pins":
            observation = self._observe_step(
                plan, actor, key, spec, post_mutation_observation=True,
            )
        else:
            observation = self._observe_step(plan, actor, key, spec)
        evidence = observation.get("evidence")
        if observation.get("phase") not in {"post", "unchanged"} or not isinstance(
            evidence, dict
        ):
            raise ProtocolRefusal(
                "fleet_update_step_evidence_missing",
                "persisted step has no exact physical post evidence",
            )
        if spec.get("step_kind") == "waiter_binding":
            coordinate = spec.get("step_coordinate")
            if not isinstance(coordinate, dict):
                raise IntegrityFailure(
                    "fleet_update_step_invalid",
                    "persisted waiter step has no exact coordinate",
                )
            _require_waiter_post_projection(
                plan,
                coordinate.get("configuration"),
                coordinate.get("store"),
                _waiter_post_from_evidence(evidence),
                persisted_evidence=record.get("step_evidence"),
                allow_live_remediation=True,
            )
        else:
            self._validate_step_against_spec(record, spec, evidence)
        return observation

    def prepare_step(
        self,
        plan: object,
        actor: str,
        key: str,
        predecessor_receipt_id: Optional[str],
    ) -> Dict[str, object]:
        """Observe the next physical step under the ledger lock without appending."""

        with _FleetUpdateExecutionGuard(self.root) as execution_token:
            return self._prepare_step_guarded(
                plan, actor, key, predecessor_receipt_id, execution_token
            )

    def _prepare_step_guarded(
        self,
        plan: object,
        actor: str,
        key: str,
        predecessor_receipt_id: Optional[str],
        execution_token: _FleetUpdateExecutionToken,
    ) -> Dict[str, object]:
        self._require_execution_token(execution_token)
        authenticated = authenticate_plan(plan, actor, self.root)
        selected_plan, digest, selected_actor, readers, consequences, exclusions, batch = self._plan(
            authenticated, actor, key
        )
        self._root_has_active_saga(selected_actor)
        token_key = (digest, selected_actor, key, predecessor_receipt_id)

        def decide(rows: list[Dict[str, object]]):
            _start, existing, spec = self._step_transaction_context(
                rows, selected_plan, digest, selected_actor, key,
                predecessor_receipt_id, readers, consequences, exclusions, batch,
            )
            if existing is not None:
                observation = self._observe_persisted_step(
                    selected_plan, selected_actor, key, existing, spec
                )
                return {
                    **spec,
                    "initial_phase": observation["phase"],
                    "existing": True,
                }, None
            observation = self._observe_step(
                selected_plan, selected_actor, key, spec
            )
            allowed = (
                {"pre", "partial", "post"}
                if spec["step_kind"] == "shared_install"
                else {"pre", "post", "unchanged"}
            )
            if observation.get("phase") not in allowed:
                raise ProtocolRefusal(
                    "fleet_update_step_evidence_missing",
                    "next fleet update step is outside its exact pre/post states",
                )
            prepared = {
                **spec,
                "initial_phase": observation["phase"],
                "existing": False,
            }, None
            if "registry_identity" in observation:
                prepared[0]["registry_identity"] = observation["registry_identity"]
            return prepared

        prepared = transact(
            self.root, self.relative(selected_actor), decide, allowed_kinds=_KINDS
        )
        if prepared["existing"] is False:
            self._prepared_steps[token_key] = dict(prepared)
        return dict(prepared)

    def step(
        self,
        plan: object,
        actor: str,
        key: str,
        predecessor_receipt_id: Optional[str],
    ) -> Dict[str, object]:
        """Append only ledger-derived state after a fresh physical observation."""

        with _FleetUpdateExecutionGuard(self.root) as execution_token:
            return self._step_guarded(
                plan, actor, key, predecessor_receipt_id, execution_token
            )

    def _step_guarded(
        self,
        plan: object,
        actor: str,
        key: str,
        predecessor_receipt_id: Optional[str],
        execution_token: _FleetUpdateExecutionToken,
    ) -> Dict[str, object]:
        self._require_execution_token(execution_token)
        authenticated = authenticate_plan(plan, actor, self.root)
        selected_plan, digest, selected_actor, readers, consequences, exclusions, batch = self._plan(
            authenticated, actor, key
        )
        self._root_has_active_saga(selected_actor)
        token_key = (digest, selected_actor, key, predecessor_receipt_id)
        # A preparation authenticates one append attempt only.  Exact
        # response-loss retries remain safe because an already-persisted row
        # is independently re-observed below without requiring a token.
        prepared = self._prepared_steps.pop(token_key, None)

        def decide(rows: list[Dict[str, object]]):
            start, existing, spec = self._step_transaction_context(
                rows, selected_plan, digest, selected_actor, key,
                predecessor_receipt_id, readers, consequences, exclusions, batch,
            )
            if existing is not None:
                self._observe_persisted_step(
                    selected_plan, selected_actor, key, existing, spec
                )
                return dict(existing), None
            if prepared is None or any(
                prepared.get(field) != spec[field]
                for field in (
                    "step_kind", "step_ordinal", "pre_digest", "post_digest",
                    "step_coordinate",
                )
            ):
                raise ProtocolRefusal(
                    "fleet_update_step_evidence_missing",
                    "fleet update step requires a same-ledger prepared observation",
                )
            observation = self._observe_step(
                selected_plan, selected_actor, key, spec
            )
            if observation.get("phase") not in {"post", "unchanged"} or not isinstance(
                observation.get("evidence"), dict
            ):
                raise ProtocolRefusal(
                    "fleet_update_step_evidence_missing",
                    "fleet update step has no exact physical post evidence",
                )
            if spec.get("step_kind") == "waiter_binding":
                coordinate = spec.get("step_coordinate")
                if isinstance(coordinate, dict):
                    _require_waiter_post_projection(
                        selected_plan,
                        coordinate.get("configuration"),
                        coordinate.get("store"),
                        _waiter_post_from_evidence(observation["evidence"]),
                    )
                else:
                    _owner_review_physical_invalid(
                        "waiter post observation has no exact coordinate"
                    )
            initial_phase = prepared.get("initial_phase")
            if initial_phase in {"pre", "partial"}:
                disposition = "applied"
            elif initial_phase == "post":
                disposition = "recovered_post_state"
            elif initial_phase == "unchanged":
                disposition = "unchanged"
            else:
                raise ProtocolRefusal(
                    "fleet_update_step_evidence_missing",
                    "prepared fleet update phase is invalid",
                )
            record = self._base(
                kind="fleet_update_step", plan_digest=digest,
                actor=selected_actor, key=key,
                consent={"id": start["consent_receipt_id"]},
                predecessor=predecessor_receipt_id, readers=readers,
                consequences=consequences, exclusions=exclusions, batch=batch,
                operation="step", step_kind=str(spec["step_kind"]),
                pre_digest=str(spec["pre_digest"]),
                post_digest=str(spec["post_digest"]), state="completed",
                step_ordinal=int(spec["step_ordinal"]),
                step_coordinate=dict(spec["step_coordinate"]),
                commit_disposition=disposition,
                step_evidence=dict(observation["evidence"]),
            )
            record["tenant_id"] = self.root.tenant_id
            return record, record

        return transact(
            self.root, self.relative(selected_actor), decide, allowed_kinds=_KINDS
        )

    def complete(self, plan: object, actor: str, key: str, predecessor_receipt_id: Optional[str]) -> Dict[str, object]:
        with _FleetUpdateExecutionGuard(self.root) as execution_token:
            return self._complete_guarded(
                plan, actor, key, predecessor_receipt_id, execution_token
            )

    def _complete_guarded(
        self,
        plan: object,
        actor: str,
        key: str,
        predecessor_receipt_id: Optional[str],
        execution_token: _FleetUpdateExecutionToken,
    ) -> Dict[str, object]:
        self._require_execution_token(execution_token)
        authenticated = authenticate_plan(plan, actor, self.root)
        plan, digest, selected_actor, readers, consequences, exclusions, batch = self._plan(authenticated, actor, key)
        self._root_has_active_saga(selected_actor)
        expected_kinds = _physical_step_kinds(plan)
        def decide(rows: list[Dict[str, object]]):
            self._validate_history(rows, selected_actor)
            related = [row for row in rows if row.get("plan_digest") == digest and row.get("idempotency_key") == key]
            start = next((row for row in related if row.get("kind") == "fleet_update_started"), None)
            if start is None:
                raise ProtocolRefusal("fleet_update_receipt_order_invalid", "fleet update completion lacks a matching start")
            steps = [row for row in related if row.get("kind") == "fleet_update_step"]
            if [row.get("step_kind") for row in steps] != expected_kinds:
                raise ProtocolRefusal(
                    "fleet_update_receipt_order_invalid",
                    "fleet update completion requires the exact physical plan sequence",
                )
            for ordinal, step_record in enumerate(steps, start=1):
                self._validate_step_against_spec(
                    step_record, _step_spec(plan, consequences, ordinal)
                )
            pin = next(row for row in steps if row.get("step_kind") == "transport_pins")
            projection = _terminal_projection(plan, pin.get("step_evidence"))
            existing = next((row for row in related if row.get("kind") == "fleet_update_completed"), None)
            if existing is not None:
                expected = {
                    "owner_review_batch_digest": batch,
                    "reader_consequences": readers,
                    "seat_binding_consequences": consequences,
                    "seat_exclusions": exclusions,
                    "moves": plan.get("moves", []),
                    "unchanged": plan.get("unchanged", []),
                    **projection,
                }
                if existing.get("predecessor_receipt_id") == predecessor_receipt_id and all(existing.get(field) == value for field, value in expected.items()):
                    return dict(existing), None
                raise ProtocolRefusal("fleet_update_idempotency_conflict", "fleet update completion key already names different content")
            if not rows or rows[-1].get("id") != predecessor_receipt_id:
                raise ProtocolRefusal("fleet_update_receipt_order_invalid", "fleet update completion must join the physical predecessor")
            record = self._base(kind="fleet_update_completed", plan_digest=digest, actor=selected_actor, key=key, consent={"id": start["consent_receipt_id"]}, predecessor=predecessor_receipt_id, readers=readers, consequences=consequences, exclusions=exclusions, batch=batch, operation="complete", step_kind=None, pre_digest=None, post_digest=None, state="completed")
            record["tenant_id"] = self.root.tenant_id
            record["step_receipt_ids"] = [row["id"] for row in steps]
            record["moves"] = plan.get("moves", [])
            record["unchanged"] = plan.get("unchanged", [])
            record.update(projection)
            return record, record
        return transact(self.root, self.relative(selected_actor), decide, allowed_kinds=_KINDS)
