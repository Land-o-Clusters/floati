"""Profiles derived from exact temporary leases and native lane ownership."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from .adhoc_records import (ADHOC_DIRECTORY, PROFILE_RELATIVE, PROVIDER_STATE_DIRECTORY,
    canonical_json, profile_digest, refuse)
from .bus_epoch import epoch_guard
from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records, _transact_adhoc_records
from .lane_workspaces import LaneWorkspaces
from .records import validate_record
from .registry import Registry
from .root import FloatiRoot, validate_identifier
from .seat_declaration import SeatDeclaration

PROFILE_KINDS = {"adhoc_profile_record"}
SLOT_MARKER = "floati-adhoc-owner.json"


def current_time(now=None):
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        refuse("adhoc_dispatch_authentication_failed", "ad-hoc time must be aware")
    return current.astimezone(timezone.utc)


def timestamp(now):
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read_descriptor(descriptor, maximum):
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        refuse("adhoc_binding_changed", "binding marker is not a bounded ordinary file")
    os.lseek(descriptor, 0, os.SEEK_SET)
    data = os.read(descriptor, maximum + 1)
    if len(data) > maximum:
        refuse("adhoc_binding_changed", "binding marker exceeds its bound")
    return data


class AdhocProfiles:
    relative_path = PROFILE_RELATIVE

    def __init__(self, root):
        if not isinstance(root, FloatiRoot):
            refuse("root_required", "ad-hoc profiles require a validated root")
        self.root = root
        self.registry = Registry(root)
        self.lanes = LaneWorkspaces(root)

    def records(self):
        return read_records(self.root, self.relative_path, allowed_kinds=PROFILE_KINDS)

    def _lease(self, node, now, *, expected=None, allow_expired=False):
        try:
            self.registry.require_active(node)
            leases = [row for row in self.registry.known_records()
                      if row.get("kind") == "node_lease" and row.get("node_id") == node]
            lease = leases[-1] if leases else None
            if lease is None or lease["state"] != "active" or lease["workspace"] != str(self.root.path / "nodes" / node):
                raise ValueError("not an exact active temporary HOME lease")
            if expected is not None and lease["id"] != expected:
                raise ValueError("lease was replaced")
            expires = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
            if not allow_expired and now >= expires:
                raise ValueError("lease expired")
        except (ProtocolRefusal, ValueError, KeyError) as exc:
            raise ProtocolRefusal("adhoc_lease_invalid", "profile requires the exact active temporary registry lease and HOME") from exc
        return lease

    def _seat(self, node):
        try:
            declaration = SeatDeclaration.load(self.root.path / "nodes" / node)
            governance = self.registry.governance()
            if declaration is None or governance is None or declaration.node_id != node or declaration.root != str(self.root.path) or declaration.tenant_id != self.root.tenant_id:
                raise ValueError("seat identity mismatch")
            declared = declaration.artifact()
            if any(declared.get(key) != value for key, value in governance.artifact().items()):
                raise ValueError("seat governance mismatch")
        except (ProtocolRefusal, ValueError, OSError) as exc:
            raise ProtocolRefusal("adhoc_binding_changed", "exact HOME SEAT binding differs") from exc
        return declaration

    def _coordinates(self, node, binding, profile_id):
        return {"root": self.root.tenant_home,
            "node_home": self.root.path / "nodes" / node,
            "seat_file": self.root.path / "nodes" / node / "SEAT.json",
            "workspace": binding["workspace"], "git_common_dir": binding["git_common_dir"],
            "git_admin_dir": binding["git_admin_dir"],
            "provider_state": self.root.tenant_home / PROVIDER_STATE_DIRECTORY / profile_id}

    def _marker(self, profile_id, lease_id):
        return canonical_json({"schema_version": 1, "tenant_id": self.root.tenant_id,
            "profile_id": profile_id, "node_lease_id": lease_id}) + b"\n"

    def _new_slot(self, profile_id, lease_id):
        try:
            self._create_slot(profile_id, lease_id)
        except OSError as exc:
            raise ProtocolRefusal("adhoc_binding_changed", "private slot creation is incomplete; preserve its exact owned staging") from exc

    def _create_slot(self, profile_id, lease_id):
        with self.root.bind_adhoc_coordinates({"root": self.root.tenant_home}) as (_ids, descriptors):
            root_fd = descriptors["root"]
            try:
                os.mkdir(PROVIDER_STATE_DIRECTORY.name, 0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            parent = os.open(PROVIDER_STATE_DIRECTORY.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                info = os.fstat(parent)
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    refuse("adhoc_binding_changed", "provider-state family must be owned and private")
                try:
                    os.mkdir(profile_id, 0o700, dir_fd=parent)
                except FileExistsError as exc:
                    raise ProtocolRefusal("adhoc_binding_changed", "an existing provider slot cannot be adopted") from exc
                slot = os.open(profile_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    descriptor = os.open(SLOT_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=slot)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(self._marker(profile_id, lease_id))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.fsync(slot)
                finally:
                    os.close(slot)
                os.fsync(parent)
            finally:
                os.close(parent)

    @contextmanager
    def _pinned(self, record, binding, now, *, allow_expired=False):
        lease = self._lease(record["node_id"], now, expected=record["node_lease_id"], allow_expired=allow_expired)
        self._seat(record["node_id"])
        checks = {"workspace": str(binding["workspace"]), "git_common_dir": str(binding["git_common_dir"]),
                  "git_admin_dir": str(binding["git_admin_dir"]), "branch": binding["branch"],
                  "base_sha": binding["base_sha"], "lease_expires_at": lease["expires_at"],
                  "node_home": str(self.root.path / "nodes" / record["node_id"])}
        if any(record[field] != value for field, value in checks.items()):
            refuse("adhoc_binding_changed", "profile derived bindings changed")
        coordinates = self._coordinates(record["node_id"], binding, record["id"])
        with self.root.bind_adhoc_coordinates(coordinates, files={"seat_file"}) as (identities, descriptors):
            if identities != record["filesystem_bindings"]:
                refuse("adhoc_binding_changed", "a descriptor-bound profile coordinate was replaced")
            if hashlib.sha256(_read_descriptor(descriptors["seat_file"], 16384)).hexdigest() != record["seat_digest"]:
                refuse("adhoc_binding_changed", "SEAT content differs from profile binding")
            slot = descriptors["provider_state"]
            if stat.S_IMODE(os.fstat(slot).st_mode) != 0o700:
                refuse("adhoc_binding_changed", "provider slot must remain private 0700")
            try:
                marker = os.open(SLOT_MARKER, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=slot)
                try:
                    if _read_descriptor(marker, 4096) != self._marker(record["id"], record["node_lease_id"]):
                        refuse("adhoc_binding_changed", "provider slot marker differs")
                finally:
                    os.close(marker)
            except OSError as exc:
                raise ProtocolRefusal("adhoc_binding_changed", "provider slot marker is unavailable") from exc
            yield record, binding
            self._lease(record["node_id"], now, expected=record["node_lease_id"], allow_expired=allow_expired)
            if hashlib.sha256(_read_descriptor(descriptors["seat_file"], 16384)).hexdigest() != record["seat_digest"]:
                refuse("adhoc_binding_changed", "SEAT content changed during the bound operation")

    @contextmanager
    def validated(self, profile_id, *, now=None, allow_expired=False, require_clean=False):
        current = current_time(now)
        records = [row for row in self.records() if row["id"] == profile_id]
        if len(records) != 1:
            refuse("adhoc_binding_changed", "profile ID does not name one exact recorded binding")
        record = records[0]
        self._lease(record["node_id"], current, expected=record["node_lease_id"], allow_expired=allow_expired)
        try:
            inspect = self.lanes.inspect_binding if allow_expired else self.lanes.validated_binding
            with inspect(record["node_id"], record["lane_opening_id"], record["repository"], require_clean=require_clean) as binding:
                with self._pinned(record, binding, current, allow_expired=allow_expired) as result:
                    yield result
        except ProtocolRefusal as exc:
            if exc.code.startswith("lane_"):
                raise ProtocolRefusal("adhoc_binding_changed", "recorded lane binding changed") from exc
            raise

    def get(self, profile_id):
        with self.validated(profile_id) as (record, _binding):
            return deepcopy(record)

    def create(self, node_id, adapter, lane_opening_id, repository, recipient, *, idempotency_key, now=None):
        current = current_time(now)
        for name, value in (("node", node_id), ("repository", repository), ("recipient", recipient), ("idempotency_key", idempotency_key)):
            validate_identifier(value, name)
        if not isinstance(adapter, str) or adapter not in {"codex", "claude", "pi"}:
            refuse("native_permission_unsupported", "only declared native adapters can create profiles")
        inputs = {"node_id": node_id, "adapter": adapter, "lane_opening_id": lane_opening_id,
                  "repository": repository, "result_recipient": recipient}
        self._lease(node_id, current)
        self._seat(node_id)
        self.registry.require_active(recipient)
        existing = self.records()
        for row in existing:
            if row["idempotency_key"] == idempotency_key:
                if any(row[key] != value for key, value in inputs.items()):
                    refuse("adhoc_dispatch_key_conflict", "profile key was reused with different immutable inputs")
                with self.validated(row["id"], now=current) as (validated, _binding):
                    return deepcopy(validated)
            if row["lane_opening_id"] == lane_opening_id:
                with self.validated(row["id"], now=current):
                    pass
        try:
            with self.lanes.validated_binding(node_id, lane_opening_id, repository, require_clean=True) as binding, ExitStack() as pins:
                def decide(rows):
                    for row in rows:
                        if row["lane_opening_id"] == lane_opening_id:
                            with self._pinned(row, binding, current):
                                pass
                        if row["idempotency_key"] == idempotency_key:
                            if any(row[key] != value for key, value in inputs.items()):
                                refuse("adhoc_dispatch_key_conflict", "profile key conflict")
                            return deepcopy(row), None
                    lease = self._lease(node_id, current)
                    profile_id = "adhoc-profile-" + uuid7_hex()
                    self._new_slot(profile_id, lease["id"])
                    coordinates = self._coordinates(node_id, binding, profile_id)
                    identities, descriptors = pins.enter_context(
                        self.root.bind_adhoc_coordinates(coordinates, files={"seat_file"}))
                    row = {"schema_version": 1, "id": profile_id, "tenant_id": self.root.tenant_id,
                        "timestamp": timestamp(current), "kind": "adhoc_profile_record", **inputs,
                        "node_lease_id": lease["id"], "node_home": str(coordinates["node_home"]),
                        "seat_digest": hashlib.sha256(_read_descriptor(descriptors["seat_file"], 16384)).hexdigest(),
                        "workspace": str(binding["workspace"]), "git_common_dir": str(binding["git_common_dir"]),
                        "git_admin_dir": str(binding["git_admin_dir"]), "branch": binding["branch"],
                        "base_sha": binding["base_sha"], "lease_expires_at": lease["expires_at"],
                        "provider_state": str(PROVIDER_STATE_DIRECTORY / profile_id), "filesystem_bindings": identities,
                        "idempotency_key": idempotency_key}
                    row["digest"] = profile_digest(row)
                    validate_record(row, self.root.tenant_id, frozenset(PROFILE_KINDS), integrity=False)
                    return deepcopy(row), row
                return self._transact_profile(decide)
        except ProtocolRefusal as exc:
            if exc.code.startswith("lane_"):
                raise ProtocolRefusal("adhoc_binding_changed", "profile requires this node's exact owned row") from exc
            raise

    def _transact_profile(self, decide):
        caller = sys._getframe(1)
        if caller.f_code is not _ADHOC_PROFILE_CREATE_CODE or caller.f_locals.get("self") is not self:
            refuse("adhoc_dispatch_authentication_failed", "profile transaction requires its validated creation route")
        return _transact_adhoc_records(self.root, decide)


_ADHOC_PROFILE_TRANSACTION_CODE = AdhocProfiles._transact_profile.__code__
_ADHOC_PROFILE_CREATE_CODE = AdhocProfiles.create.__code__
