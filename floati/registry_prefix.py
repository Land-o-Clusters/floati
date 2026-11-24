"""Disposable selected registry rows under the existing KeyPrefix contract."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from . import jsonl, records, send_prefix
from .bus_epoch import epoch_guard
from .errors import IntegrityFailure, ProtocolRefusal
from .key_prefix import KeyPrefix
from .registry import REGISTRY_KINDS, read_registry_compatible

RELATIVE = Path("registry/entries.jsonl")
CACHE_NAME = ".registry-prefix"
DOMAINS = {"registry_entry": "registration", "node_lease": "lease",
           "registry_role_record": "role"}


def _roster(rows):
    registrations, roles = {}, {}
    for row in rows:
        if row["kind"] == "registry_entry":
            registrations[row["node_id"]] = row
        elif row["kind"] == "registry_role_record":
            roles[row["node_id"]] = row
    return [[node, row["role"], roles.get(node, {}).get("template_role"),
             roles.get(node, {}).get("state")]
            for node, row in sorted(registrations.items()) if row["state"] == "active"]


class RegistryPrefix(KeyPrefix):
    def __init__(self, root, *, revision=None, read_only=False):
        super().__init__(root, REGISTRY_KINDS, relative=RELATIVE,
            cache_name=CACHE_NAME, format="registry-prefix-v2",
            folds={"max_timestamp": "chart-time-text-v1"},
            revision={"source": send_prefix._revision() if revision is None else revision,
                      "reader_version": records.READER_VERSION}, read_only=read_only)

    def _fold(self, rows, prior):
        from .multi_bus_chart import _parse_time
        maximum = prior.get("max_timestamp")
        try:
            for row in rows:
                value = row["timestamp"]
                instant = _parse_time(value)
                if maximum is None or (instant, value) > (_parse_time(maximum), maximum):
                    maximum = value
        except ProtocolRefusal as error:
            raise send_prefix.CacheMiss() from error
        return {"max_timestamp": maximum}

    def rebuild(self):
        before = send_prefix._identity(self.path)
        unknown = {}
        rows = jsonl._read_path_records(self.path, self.root.tenant_id,
            self.kinds, unrecognized=unknown)
        # A compatible future history remains readable, but cannot be
        # certified by KeyPrefix's unchanged strict known-kind decoder.
        if unknown:
            raise send_prefix.CacheMiss()
        data = self.path.read_bytes() if before is not None else b""
        if before != send_prefix._identity(self.path):
            raise IntegrityFailure("ledger_identity_changed_during_read",
                "registry changed during prefix validation")
        entries = {}
        for row in rows:
            domain = DOMAINS.get(row["kind"])
            if domain is not None:
                entries[send_prefix._key(domain, row["node_id"])] = row
        entries[send_prefix._key("catalog", "active")] = sorted(
            row["node_id"] for key, row in entries.items()
            if row["kind"] == "registry_entry" and row["state"] == "active")
        entries[send_prefix._key("roster", "active")] = _roster(rows)
        self.rebuild_entries(rows, entries, before, data)

    def _advance(self, batch, encoded):
        # Any registration or role replacement requires the same native
        # generation rebuild as the immutable latest-row/catalog members.
        if any(row["kind"] in {"registry_entry", "registry_role_record"} for row in batch):
            raise send_prefix.CacheMiss()
        active = set(self._read_entry(send_prefix._key("catalog", "active")))
        for row in batch:
            domain = DOMAINS.get(row["kind"])
            if domain is not None:
                self.set(domain, row["node_id"], row)
            if row["kind"] == "registry_entry":
                if row["state"] == "active":
                    active.add(row["node_id"])
                else:
                    active.discard(row["node_id"])
        self.set("catalog", "active", sorted(active))
        for frame in encoded:
            self.payload["head"] = hashlib.sha256(bytes.fromhex(self.payload["head"]) + frame).hexdigest()
        self.payload["count"] += len(batch)


def _open(root, *, create):
    bank = root.tenant_home / CACHE_NAME
    if not create and not (bank.exists() or bank.is_symlink()):
        return None
    try:
        prefix = RegistryPrefix(root)
        try:
            prefix.load()
        except send_prefix._CACHE_ERRORS:
            prefix.rebuild()
        return prefix
    except send_prefix._CACHE_ERRORS:
        return None


def read_selected(root, node):
    """Select latest physical rows; return native compatible rows on a miss."""
    return read_selected_nodes(root, (node,))


def read_selected_nodes(root, nodes):
    """Select one registry cut for the named nodes, including retired ones."""
    nodes = tuple(dict.fromkeys(nodes))
    path, _tenant = jsonl._resolve(root, RELATIVE, write=False)
    lock, lock_relative = jsonl._lock_beside(path, RELATIVE)
    with epoch_guard(root), jsonl._locked_path(lock, exclusive=True, relative=lock_relative):
        prefix = _open(root, create=True)
        for attempt in range(2):
            if prefix is None:
                break
            try:
                rows = []
                for node in nodes:
                    for kind, domain in DOMAINS.items():
                        row = prefix.get(domain, node)
                        if row is not None:
                            if (type(row) is not dict or row.get("kind") != kind
                                    or row.get("node_id") != node):
                                raise send_prefix.CacheMiss()
                            rows.append(row)
                prefix._assert_current()
                return rows
            except send_prefix._CACHE_ERRORS:
                if attempt == 0:
                    try:
                        prefix.rebuild()
                    except send_prefix._CACHE_ERRORS:
                        prefix = None
                else:
                    prefix = None
        rows, _unknown, _versions = jsonl.read_records_compatible_with_versions(
            root, RELATIVE, allowed_kinds=REGISTRY_KINDS, snapshot=True)
        return rows


def read_catalog(root):
    """Read live registrations and sealed history activity, or native history."""
    path, _tenant = jsonl._resolve(root, RELATIVE, write=False)
    lock, lock_relative = jsonl._lock_beside(path, RELATIVE)
    with epoch_guard(root), jsonl._locked_path(lock, exclusive=True, relative=lock_relative):
        prefix = _open(root, create=True)
        for attempt in range(2):
            if prefix is None:
                break
            try:
                active = prefix.get("catalog", "active")
                if (type(active) is not list or any(type(node) is not str for node in active)
                        or active != sorted(set(active))):
                    raise send_prefix.CacheMiss()
                rows = []
                for node in active:
                    row = prefix.get("registration", node)
                    if (row is None or row["kind"] != "registry_entry"
                            or row["node_id"] != node or row["state"] != "active"):
                        raise send_prefix.CacheMiss()
                    rows.append(row)
                maximum = prefix.payload["folds"]["max_timestamp"]
                if maximum is not None:
                    from .multi_bus_chart import _parse_time
                    try:
                        _parse_time(maximum)
                    except ProtocolRefusal as error:
                        raise send_prefix.CacheMiss() from error
                elif prefix.payload["count"]:
                    raise send_prefix.CacheMiss()
                prefix._assert_current()
                return rows, maximum
            except send_prefix._CACHE_ERRORS:
                if attempt == 0:
                    try:
                        prefix.rebuild()
                    except send_prefix._CACHE_ERRORS:
                        prefix = None
                else:
                    prefix = None
        rows, _unknown, _versions = read_registry_compatible(root)
        return rows, None


@dataclass(frozen=True)
class RegistryCut:
    identity: object
    node: object
    rows: list
    roster: list


def _cut(prefix, node):
    rows = []
    if node is not None:
        for kind, domain in DOMAINS.items():
            row = prefix.get(domain, node)
            if row is not None:
                if type(row) is not dict or row.get("kind") != kind or row.get("node_id") != node:
                    raise send_prefix.CacheMiss()
                rows.append(row)
    roster = prefix.get("roster", "active")
    if (type(roster) is not list or any(type(row) is not list or len(row) != 4
            or type(row[0]) is not str or type(row[1]) is not str
            or (row[2] is not None and type(row[2]) is not str)
            or (row[3] is not None and row[3] not in {"active", "retired"}) for row in roster)
            or [row[0] for row in roster] != sorted(set(row[0] for row in roster))):
        raise send_prefix.CacheMiss()
    prefix._assert_current()
    return RegistryCut(prefix.payload["identity"], node, rows, roster)


def read_cut(root, node=None):
    """Capture once under the writer lock; check immutable members outside it."""
    path, _tenant = jsonl._resolve(root, RELATIVE, write=False)
    lock, coordinate = jsonl._lock_beside(path, RELATIVE)
    revision = send_prefix._revision()
    with epoch_guard(root):
        try:
            with jsonl._locked_path(lock, exclusive=True, relative=coordinate):
                prefix = RegistryPrefix(root, revision=revision, read_only=True)
                prefix.load()
            cut = _cut(prefix, node)
            if path != root.resolve_relative(RELATIVE) or cut.identity != send_prefix._identity(path):
                raise send_prefix.CacheMiss()
            return cut
        except send_prefix._CACHE_ERRORS:
            pass
        # Recovery retains the native locked reader/rebuild discipline.
        with jsonl._locked_path(lock, exclusive=True, relative=coordinate):
            prefix = _open(root, create=True)
            if prefix is not None:
                try:
                    return _cut(prefix, node)
                except send_prefix._CACHE_ERRORS:
                    try:
                        prefix.rebuild()
                        return _cut(prefix, node)
                    except send_prefix._CACHE_ERRORS:
                        pass
            rows, _unknown, _versions = read_registry_compatible(root)
            return RegistryCut(send_prefix._identity(path), node, rows, _roster(rows))


def prepare_append(root, relative, kinds, max_bytes):
    """Called only while the native registry writer already owns its lock."""
    if (Path(relative) != RELATIVE or kinds != frozenset(REGISTRY_KINDS)
            or max_bytes != jsonl.MAX_RECORD_BYTES):
        return None
    # A missing bank creates no new writer side effect. The first selected
    # reader builds it; every subsequent transaction advances it here.
    return _open(root, create=False)


def advance(prefix, batch, encoded, identity):
    if prefix is not None:
        try:
            prefix.appended(batch, encoded, identity)
        except send_prefix._CACHE_ERRORS:
            # KeyPrefix members are immutable within a generation. A latest
            # registration/lease/role replacement must rotate that generation via
            # native rebuild, never overwrite an old still-valid entry seal.
            try:
                prefix.rebuild()
            except send_prefix._CACHE_ERRORS:
                pass  # Native fsync succeeded; stale identity forces recovery.


def warm(root):
    """Certify a copied registry outside measurement, without ledger mutation."""
    path, _tenant = jsonl._resolve(root, RELATIVE, write=False)
    lock, lock_relative = jsonl._lock_beside(path, RELATIVE)
    with epoch_guard(root), jsonl._locked_path(lock, exclusive=True, relative=lock_relative):
        prefix = _open(root, create=True)
        return None if prefix is None else {"identity": prefix.payload["identity"],
            "count": prefix.payload["count"], "head": prefix.payload["head"]}
