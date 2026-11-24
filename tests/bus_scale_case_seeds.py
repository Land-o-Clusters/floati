"""Approved native prefaces and exact one-ledger padding for expanded cases.

Bulk publication is a fixture construction, never a claim that 10,000 native
commands ran. Native producers establish the shapes and ancestry; native
readers validate the complete published histories. Original nine-case seeds
are constructed by bus_scale_seeds and remain independent of this module.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import io
import os
from pathlib import Path
import shutil
import stat
from tests.bus_scale_test_profile import stable_fixture_sources

from floati.admin_registry import RegistryAdminBackend
from floati.codex_wait_contract import CodexWaitReceiptLedger
from floati.events import EVENT_KINDS, EventLog, validate_event_records
from floati.framing import encode_frame
from floati.jsonl import MAX_LEDGER_BYTES, MAX_RECORD_BYTES, read_records
from floati.provider_switch import ProviderSwitchWizard
from floati.records import validate_record
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from floati.wake_hold import WAKE_HOLD_KINDS
from tests.bus_scale_seeds import _base_path, _native_prelude, _PlanAssignment, _suffix


HISTORY = "scale-history"
FAMILIES = {
    "events": ("events.jsonl", frozenset(EVENT_KINDS)),
    "registry": ("registry/entries.jsonl", frozenset(REGISTRY_KINDS)),
    "wait-exhaustion": ("receipts/codex-wait-exhaustion/scale-worker.jsonl",
                        frozenset({"codex_wait_exhaustion_receipt"})),
    "deliveries": ("receipts/deliveries/architect.jsonl", frozenset(WAKE_HOLD_KINDS)),
}


def inventory(home):
    result = {}
    for path in sorted(Path(home).rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or path.resolve(strict=True) != path or info.st_nlink != 1:
            raise ValueError("case seed contains a nonordinary or shared file")
        result[path.relative_to(home).as_posix()] = path.read_bytes()
    return result


class FixtureCacheRefusal(ValueError):
    """A named refusal of the fixture-only disposable-cache removal step."""

    step = "fixture_send_cache_cleanup"
    status = "refused"

    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


class FixtureSendCache:
    """Bind the ruled cache literals to this fixture's own native sends.

    Capture ownership before preparation, then supply the IDs returned by its
    actual sends. Existing cache directories never acquire removal authority.
    This helper is for disposable seeds, before their immutable capture only.
    """

    def __init__(self, scratch_root, home):
        self.scratch, self.home = Path(scratch_root), Path(home)
        if (not self.scratch.is_absolute() or not self.home.is_absolute()
                or str(self.scratch) != os.path.normpath(str(self.scratch))
                or str(self.home) != os.path.normpath(str(self.home))
                or self.scratch not in self.home.parents):
            self._refuse("fixture_cache_outside_root", "fixture home must sit strictly inside its scratch root")
        self.device = self.scratch.lstat().st_dev
        self._ordinary(self.scratch, directory=True)
        if self.scratch.resolve(strict=True) != self.scratch:
            self._refuse("fixture_cache_symlink", "scratch root has a symlinked ancestor")
        cursor = self.scratch
        self.bound_directories = [(cursor, self._identity(cursor))]
        for component in self.home.relative_to(self.scratch).parts:
            cursor = cursor / component
            self._ordinary(cursor, directory=True)
            self.bound_directories.append((cursor, self._identity(cursor)))
        self.root = FloatiRoot.open_direct_home(self.home, create=False)
        self.fixed_caches = tuple(self.home / name for name in (
            ".send-prefix", ".registry-prefix", ".inbox-prefix"))
        self.caches = self.fixed_caches + self._consumed_caches()
        self.cache = self.caches[0]
        if any(cache.exists() or cache.is_symlink() for cache in self.caches):
            self._refuse("fixture_cache_preexisting", "cache existed before this fixture's sends")
        self.before_ids = {row["id"] for row in EventLog(self.root).records()}

    def _consumed_caches(self):
        """Derive exact hashes from native ledger coordinates, never a glob grant."""
        from floati.cursor import SparseCursor
        from floati.jsonl import _decode_path_records
        directory = self.home / "receipts/consumed"
        if not directory.exists() and not directory.is_symlink():
            return ()
        self._ordinary(directory.parent, directory=True)
        self._ordinary(directory, directory=True)
        ledgers = []
        # The only admitted native source shapes are node.jsonl and
        # node/<sha256(worker-session)>.jsonl. Every row must reproduce its
        # physical coordinate before that coordinate can name a cache.
        for child in sorted(directory.iterdir()):
            info = child.lstat()
            if stat.S_ISDIR(info.st_mode):
                self._ordinary(child, directory=True)
                ledgers.extend(path for path in sorted(child.iterdir()) if path.suffix == ".jsonl")
            elif child.suffix == ".jsonl":
                ledgers.append(child)
        result = []
        cursor = SparseCursor(self.root)
        for path in ledgers:
            self._ordinary(path)
            relative = path.relative_to(self.home)
            if path.suffix != ".jsonl" or len(relative.parts) not in (3, 4):
                self._refuse("fixture_cache_consumed_coordinate", "consumed source is not a native ledger coordinate")
            rows = _decode_path_records(path, self.root.tenant_id,
                frozenset({"consumption_receipt"}), path.read_bytes())
            for row in rows:
                ack = cursor._relative_path_for(row["recipient"],
                    worker_session_id=row["worker_session_id"])
                expected = Path("receipts/consumed").joinpath(*ack.relative_to("receipts/acks").parts)
                if relative != expected:
                    self._refuse("fixture_cache_consumed_coordinate", "consumed row belongs to another physical plane")
            if rows:
                result.append(self.home / (".consumed-prefix-" + hashlib.sha256(
                    relative.as_posix().encode()).hexdigest()))
        return tuple(result)

    @staticmethod
    def _refuse(code, detail):
        raise FixtureCacheRefusal(code, detail)

    @staticmethod
    def _identity(path):
        metadata = path.lstat()
        return metadata.st_dev, metadata.st_ino

    def _ordinary(self, path, *, directory=False):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            self._refuse("fixture_cache_symlink", "fixture cache cleanup never follows a symlink")
        if metadata.st_dev != self.device:
            self._refuse("fixture_cache_device_mismatch", "fixture cache crossed its scratch device")
        expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
        if not expected or metadata.st_uid != os.getuid() or (not directory and metadata.st_nlink != 1):
            self._refuse("fixture_cache_nonordinary", "fixture cache must have ordinary owned unshared entries")
        return metadata

    def remove(self, own_send_ids, *, cache_path=None):
        self.caches = self.fixed_caches + self._consumed_caches()
        cache = self.cache if cache_path is None else Path(cache_path)
        if cache not in self.caches or self.scratch not in cache.parents:
            self._refuse("fixture_cache_outside_root",
                "only this fixture home's exact ruled cache coordinates may be removed")
        for directory, identity in self.bound_directories:
            self._ordinary(directory, directory=True)
            if self._identity(directory) != identity:
                self._refuse("fixture_cache_identity_changed", "fixture ownership changed after preparation began")
        rows = EventLog(self.root).records()
        added_ids = [row["id"] for row in rows if row["id"] not in self.before_ids]
        if (not isinstance(own_send_ids, list) or not own_send_ids
                or added_ids != own_send_ids or len(set(own_send_ids)) != len(own_send_ids)):
            self._refuse("fixture_cache_own_sends_missing", "cleanup requires the exact newly authored native send IDs")
        caches = [candidate for candidate in self.caches
                  if candidate.exists() or candidate.is_symlink()]
        if not caches:
            self._refuse("fixture_cache_missing", "this fixture has no ruled cache to remove")
        for candidate in caches:
            self._ordinary(candidate, directory=True)
        removed_files = 0
        # Validate every complete subtree before removing even one file.
        # scandir and lstat inspect links without traversing them.
        pending = list(caches)
        while pending:
            directory = pending.pop()
            self._ordinary(directory, directory=True)
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    metadata = path.lstat()
                    is_directory = stat.S_ISDIR(metadata.st_mode)
                    self._ordinary(path, directory=is_directory)
                    if is_directory:
                        pending.append(path)
                    else:
                        removed_files += 1
        for candidate in caches:
            shutil.rmtree(candidate)
        return {"step": "fixture_send_cache_cleanup", "status": "ok", "outcome": "removed",
                "cache_relative": caches[0].name,
                "cache_relatives": [candidate.name for candidate in caches],
                "own_send_ids": list(own_send_ids),
                "same_device": True, "no_symlinks": True, "removed_files": removed_files}


def _message(root, workspace, recipient, key, *, sender=HISTORY):
    reference = Path(workspace) / ".git/refs/remotes/origin/main"
    sha = reference.read_text(encoding="ascii").strip()
    return EventLog(root).send(sender, recipient, "floati", sha,
        "note.md", "unselected native census history", idempotency_key=key)["message"]


def _rows(root, family):
    relative, kinds = FAMILIES[family]
    return read_records(root, relative, allowed_kinds=kinds)


def _history_role(root, node_id=HISTORY):
    """Author the unselected member's genuine role before registry padding."""
    from floati.ids import uuid7_hex
    from floati.role_assignment import RoleStepWizard
    from floati.role_library import RoleTemplateLibrary
    backend = RegistryAdminBackend(root)
    templates = RoleTemplateLibrary(root).templates()
    answers = {"repo": "floati", "never_touch": "foreign-project",
               "owner_stops": "owner-tier", "architect": "architect"}
    provided = [answers.get(question.key, "" if question.default is not None else "bounded-private-fixture")
                for question in templates["builder"].questions]
    RoleStepWizard(root, backend, templates, id_factory=uuid7_hex).assign_from_keys(
        [node_id, "builder", *provided], io.StringIO())


def _padding(root, family, workspace, counts):
    """Return a native preface and enough legal successor rows for 10×."""
    relative, _kinds = FAMILIES[family]
    identifiers = itertools.count(5000000)
    if family == "events":
        template = _message(root, workspace, HISTORY, "case-padding-template")
        prefix = _rows(root, family)
        padding = [dict(template, id="msg-" + _suffix(next(identifiers)),
                        idempotency_key="case-padding-%08d" % index)
                   for index in range(counts[1] - len(prefix))]
    elif family == "registry":
        prefix = _rows(root, family)
        # Provider switches are native two-frame transitions. An unselected
        # membership record makes both exact count boundaries pair-aligned.
        if len(prefix) % 2:
            Registry(root).register("scale-history-padding", "codex")
            prefix = _rows(root, family)
        assignment = _PlanAssignment(RegistryAdminBackend(root).active_assignment(HISTORY))
        wizard = ProviderSwitchWizard(root, assignment,
            id_factory=lambda: _suffix(next(identifiers)))
        padding = []
        for index in range((counts[1] - len(prefix)) // 2):
            plan = wizard._plan((HISTORY, "codex", "scale-history-a" if index % 2 == 0 else "scale-history-b"))
            assignment.accept(plan)
            padding.extend(dict(row) for row in plan.records)
    elif family == "wait-exhaustion":
        template = CodexWaitReceiptLedger(root).record_exhaustion(
            node_id="scale-worker", session_digest=hashlib.sha256(b"case-history-template").hexdigest(),
            waited_seconds=90, idempotency_key="case-history-template")
        prefix = _rows(root, family)
        padding = [dict(template, id="codex-wait-exhaustion-" + _suffix(next(identifiers)),
                        session_digest=hashlib.sha256(("case-history-%d" % index).encode()).hexdigest(),
                        idempotency_key="case-history-%08d" % index)
                   for index in range(counts[1] - len(prefix))]
    else:
        # A distinct, acknowledged historical envelope is unselected by every
        # measured drain/wake. Never present the actual selected envelope to
        # manufacture the scaled delivery history.
        history = _message(root, workspace, "architect", "case-delivery-template")
        template = EventLog(root)._write_delivery_receipt("architect", [history])
        if template is None:
            raise ValueError("case seed delivery lacks native publication")
        from floati.cursor import SparseCursor
        SparseCursor(root).ack("architect", [history["id"]],
                               acting_session_id="case-history-acknowledged")
        prefix = _rows(root, family)
        padding = [dict(template, id="delivery-" + _suffix(next(identifiers)),
                        presentation_count=len(prefix) + index + 1)
                   for index in range(counts[1] - len(prefix))]
    if len(prefix) >= counts[0] or len(prefix) + len(padding) != counts[1]:
        raise ValueError("native case preface cannot meet exact bounded scale counts")
    return prefix + padding


def _encode(root, family, rows, counts):
    _relative, kinds = FAMILIES[family]
    if len(rows) != counts[1] or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("case history repeats an identity or has the wrong count")
    frames = []
    for row in rows:
        validate_record(row, root.tenant_id, kinds, integrity=True)
        frame = encode_frame(row)
        if len(frame) - 1 > MAX_RECORD_BYTES:
            raise ValueError("case frame exceeds the native byte bound")
        frames.append(frame)
    if family == "events":
        validate_event_records(rows)
    if sum(map(len, frames)) > MAX_LEDGER_BYTES:
        raise ValueError("case history exceeds the native ledger byte bound")
    return b"".join(frames[:counts[0]]), b"".join(frames)


@stable_fixture_sources()
def make_case_seed_pair(base, family, case, helper, workspace, *, counts=(1000, 10000)):
    """Author one native case preface, then publish independent 1×/10× seeds.

    prepared_home is retained as its actual native authoring coordinate for
    cases whose native consent binds absolute roots. Consumers must obtain
    the separate ruled restoration contract before measuring at that path.
    """
    if (not isinstance(counts, tuple) or len(counts) != 2
            or any(type(value) is not int for value in counts)
            or not 0 < counts[0] < counts[1] <= 10000
            or any(value % 2 for value in counts)):
        raise ValueError("case seed counts require two increasing even bounded populations")
    base = _base_path(base)
    if family not in FAMILIES:
        raise ValueError("unknown case seed family")
    if case == "wake-callback":
        root = FloatiRoot.open(base / "callback-root", "scale-fleet")
        prepared = root.tenant_home
        prepared.chmod(0o700)
        for node in ("scale-worker", "architect"):
            Registry(root).register(node, "codex")
    else:
        (base / "prepared").mkdir(mode=0o700)
        prepared = base / "prepared" / "scale-fleet"
        root = _native_prelude(prepared)
    bank = base / "native-preparation"
    bank.mkdir(mode=0o700)
    cache_owner = FixtureSendCache(base, prepared) if case in {"supervise", "watch"} else None
    context = helper.prepare(case, prepared, Path(workspace), bank)
    if type(context) is not dict:
        raise ValueError("native case preparation must return retained JSON context")
    Registry(root).register(HISTORY, "codex")
    if case in {"node-boot", "node-explain", "node-teardown"}:
        _history_role(root)
    rows = _padding(root, family, workspace, counts)
    small_raw, large_raw = _encode(root, family, rows, counts)
    if cache_owner is not None:
        # A preparation wrapper may already have removed its own cache. Exact
        # absence needs no removal authority; any remaining/new cache must
        # satisfy the same owned-send boundary before immutable seed capture.
        if any(cache.exists() or cache.is_symlink() for cache in cache_owner.caches):
            own_send_ids = [row["id"] for row in EventLog(root).records()
                            if row["id"] not in cache_owner.before_ids]
            cache_owner.remove(own_send_ids)
    relative, kinds = FAMILIES[family]
    before = inventory(prepared)
    result = {"ledger_relative": relative, "context": copy.deepcopy(context),
              "record_counts": dict(zip(("1", "10"), counts)),
              "bounded_fixture": counts != (1000, 10000),
              "prepared_home": prepared, "preparation_bank": bank}
    for scale, raw, count in (("1", small_raw, counts[0]), ("10", large_raw, counts[1])):
        (base / scale).mkdir(mode=0o700)
        home = base / scale / "scale-fleet"
        shutil.copytree(prepared, home)
        destination = home / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        destination.chmod(0o600)
        clone = FloatiRoot.open_direct_home(home)
        actual = read_records(clone, relative, allowed_kinds=kinds)
        if actual != rows[:count]:
            raise ValueError("native reader changed the complete case seed history")
        if family == "events":
            validate_event_records(EventLog(clone).event_records())
        other = inventory(home)
        other.pop(relative)
        expected = dict(before)
        expected.pop(relative, None)
        if other != expected:
            raise ValueError("case seed changed ancillary input bytes")
        result[scale] = home
    if not (result["10"] / relative).read_bytes().startswith((result["1"] / relative).read_bytes()):
        raise ValueError("case scale history lost its exact common prefix")
    return result
