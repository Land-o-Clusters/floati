"""Leased working paths as a liveness fact, read as advisory overlap (NET-TERRITORY-1).

A node declares the absolute paths it is working in as a lease with an
expiry: who holds which paths, until when. A dispatch that names the paths its
work would edit carries a preview of every other node's live lease those
paths enter, with the `floati overlap` fact for the two worktrees. Nothing is
refused and no override exists; the preview informs, it never gates.

Paths compare canonically: symlinks and dot segments resolve, existing
components take their on-disk spelling, a missing tail follows the case rule
of the filesystem that would hold it, two paths collide when nested on disk,
and a path inside a Git worktree also compares as a path of its repository,
so every worktree of one repository is one path space. A lease is live only
while it is unreleased, unexpired, and its holder is an active node whose own
node lease has not expired.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .registry import Registry
from .session_identity import validate_identifier
from .territory_records import (
    LEASE_PREFIX, MAX_PATHS, MAX_TTL_SECONDS, MIN_TTL_SECONDS,
    TERRITORY_KINDS, TERRITORY_RELATIVE,
    format_timestamp, key_valid, parse_timestamp, path_shape_valid, visible_text,
)



def _refuse(code, detail, remedy=None, preview=None):
    refusal = ProtocolRefusal(code, detail, remedy=remedy)
    if preview is not None:
        refusal.refusal_detail = preview  # type: ignore[attr-defined]
    return refusal


def _now(value: Optional[datetime] = None) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise _refuse("territory_time_invalid", "territory time must be timezone-aware")
    # Record timestamps carry milliseconds; evaluate on the same grid.
    current = current.astimezone(timezone.utc)
    return current.replace(microsecond=current.microsecond // 1000 * 1000)


# ---------------------------------------------------------------- canonical paths


def _listing(directory: str) -> Optional[List[str]]:
    try:
        return os.listdir(directory)
    except OSError:
        return None


def _same(left: str, right: str) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _on_disk(resolved: str) -> Tuple[str, int]:
    """Return the on-disk spelling and how many components exist on disk."""
    parts = PurePosixPath(resolved).parts
    current, spelled, existing, present = "/", ["/"], 1, True
    for component in parts[1:]:
        candidate = os.path.join(current, component)
        if present and not os.path.lexists(candidate):
            present = False
        if present:
            names = _listing(current)
            if names is not None and component not in names:
                matches = [name for name in names if name.casefold() == component.casefold()
                           and _same(os.path.join(current, name), candidate)]
                if len(matches) == 1:
                    component = matches[0]
            existing += 1
        spelled.append(component)
        current = os.path.join(current, component)
    return str(PurePosixPath(*spelled)), existing


def _case_insensitive(directory: str) -> bool:
    """Ask the filesystem holding `directory` whether it folds case."""
    names = _listing(directory) or []
    present = set(names)
    for name in names:
        swapped = name.swapcase()
        if swapped != name:
            if swapped in present:
                return False
            return _same(os.path.join(directory, swapped), os.path.join(directory, name))
    base = os.path.basename(directory)
    if base.swapcase() != base:
        return _same(os.path.join(os.path.dirname(directory), base.swapcase()), directory)
    return False


@dataclass(frozen=True)
class PathKey:
    """One canonical comparison coordinate; `spelled` is what a person reads."""

    namespace: str          # "git" (a repository path) or "fs" (an absolute path)
    anchor: str             # the repository's common directory, or "" for fs
    components: Tuple[str, ...]
    spelled: str
    worktree: Optional[str]
    absolute: Tuple[str, ...]  # on-disk components from "/", whatever the namespace
    folds: bool = False        # the holding filesystem ignores case

    @staticmethod
    def _nested(left: Tuple[str, ...], right: Tuple[str, ...]) -> bool:
        shorter = min(len(left), len(right))
        return left[:shorter] == right[:shorter]

    def shares_repository(self, other: "PathKey") -> bool:
        return self.namespace == other.namespace == "git" and self.anchor == other.anchor

    def contains_or_within(self, other: "PathKey") -> bool:
        """Nested on disk, across any repository boundary, or nested within
        one repository's path space, across its worktrees."""
        if self._nested(self.absolute, other.absolute):
            return True
        return self.shares_repository(other) and self.within_repository(other.components, other.folds)

    def within_repository(self, relative: Tuple[str, ...], folds: bool = False) -> bool:
        """Nested with one repository-relative path, under either side's case rule."""
        mine = self.components
        if self.folds or folds:
            mine, relative = (tuple(c.casefold() for c in mine), tuple(c.casefold() for c in relative))
        return self._nested(mine, relative)

    def coordinate(self, other: "PathKey") -> str:
        """The narrower of two colliding paths: repository-relative or absolute."""
        if self.shares_repository(other):
            deeper = self if len(self.components) >= len(other.components) else other
            return "/".join(deeper.spelled_components()) or "."
        deeper = self if len(self.absolute) >= len(other.absolute) else other
        return deeper.spelled

    def spelled_components(self) -> Tuple[str, ...]:
        parts = PurePosixPath(self.spelled).parts[1:]
        if self.namespace == "git" and self.worktree is None:
            return self.components
        if self.worktree is None:
            return parts
        return parts[len(PurePosixPath(self.worktree).parts) - 1:]


class _Canonicalizer:
    """Per-invocation canonicalization with one Git probe per worktree."""

    def __init__(self) -> None:
        self._common: Dict[str, str] = {}

    def spelled(self, raw: object) -> str:
        if not isinstance(raw, str) or not raw.startswith("/") or not visible_text(raw, 4096):
            raise _refuse("territory_path_invalid", "each path must be absolute visible text",
                          remedy="pass every --path or --edits value as an absolute path")
        spelled, _existing = _on_disk(os.path.realpath(raw))
        if not path_shape_valid(spelled):
            raise _refuse("territory_path_invalid", "path does not resolve to a bounded absolute path",
                          remedy="pass every --path or --edits value as an absolute path")
        return spelled

    def key(self, raw: str) -> PathKey:
        spelled, existing = _on_disk(os.path.realpath(raw))
        parts = PurePosixPath(spelled).parts
        present = str(PurePosixPath(*parts[:existing]))
        directory = present if os.path.isdir(present) else os.path.dirname(present)
        tail = parts[existing:]
        folds = _case_insensitive(directory)
        if tail and folds:
            tail = tuple(part.casefold() for part in tail)
        components = tuple(parts[1:existing]) + tuple(tail)
        worktree = self._worktree(directory)
        if worktree is None:
            return PathKey("fs", "", components, spelled, None, components, folds)
        depth = len(PurePosixPath(worktree).parts) - 1
        return PathKey("git", self._common_dir(worktree), components[depth:], spelled, worktree,
                       components, folds)

    def lease_key(self, entry: Dict[str, object]) -> PathKey:
        """A lease path as claimed: its repository and relative path are the
        claim-time facts; its absolute form is read from the disk today."""
        current = self.key(str(entry["path"]))
        if entry.get("repository") is None:
            return current
        relative = str(entry["relative"])
        components = () if relative == "." else tuple(relative.split("/"))
        worktree = current.worktree if current.anchor == entry["repository"] else None
        spelled = str(entry["path"])
        return PathKey("git", str(entry["repository"]), components, spelled, worktree,
                       current.absolute, current.folds)

    def claim(self, spelled: str) -> Dict[str, object]:
        key = self.key(spelled)
        if key.namespace != "git":
            return {"path": spelled, "repository": None, "relative": None}
        return {"path": spelled, "repository": key.anchor,
                "relative": "/".join(key.spelled_components()) or "."}

    @staticmethod
    def _worktree(directory: str) -> Optional[str]:
        current = directory
        while True:
            if os.path.lexists(os.path.join(current, ".git")):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent

    def _common_dir(self, worktree: str) -> str:
        if worktree not in self._common:
            from .overlap_radar import _git

            common = _git(Path(worktree), "rev-parse", "--git-common-dir").strip()
            if not common.startswith("/"):
                common = os.path.join(worktree, common)
            self._common[worktree] = _on_disk(os.path.realpath(common))[0]
        return self._common[worktree]


def _canonical_list(canonicalizer: _Canonicalizer, raw_paths: Sequence[str], label: str) -> List[str]:
    if not raw_paths:
        raise _refuse("territory_path_invalid", label + " needs at least one path",
                      remedy="pass at least one absolute " + label + " path")
    spelled: List[str] = []
    for raw in raw_paths:
        value = canonicalizer.spelled(raw)
        if value not in spelled:
            spelled.append(value)
    if len(spelled) > MAX_PATHS:
        raise _refuse("territory_path_invalid", label + " names more than 64 paths",
                      remedy="split the work into smaller territories or dispatches")
    return spelled


# ---------------------------------------------------------------- projection


def _lease_rows(records: Sequence[Dict[str, object]]):
    opened = {str(r["id"]): r for r in records
              if r.get("kind") == "territory_lease" and r.get("phase") == "leased"}
    released = {str(r["predecessor_receipt_id"]): r for r in records
                if r.get("kind") == "territory_lease" and r.get("phase") == "released"}
    return opened, released


def _states(records, registry_rows, now):
    opened, released = _lease_rows(records)
    states = []
    for ident, lease in opened.items():
        expires = parse_timestamp(lease["expires_at"])
        if ident in released:
            state = "released"
        elif expires is None or now >= expires:
            state = "expired"
        else:
            try:
                node_lease = Registry._node_lease_state_from_records(
                    str(lease["node_id"]), registry_rows, now=now)
            except ProtocolRefusal:
                state = "holder_inactive"
            else:
                state = "holder_lease_expired" if node_lease["state"] == "expired" else "live"
        states.append((lease, state))
    return states


@dataclass
class _Conflict:
    lease: Dict[str, object]
    territory: PathKey
    edit: PathKey


class TerritoryLedger:
    def __init__(self, root) -> None:
        self.root = root
        self.registry = Registry(root)

    # -- reads

    def records(self) -> List[Dict[str, object]]:
        return read_records_snapshot(self.root, TERRITORY_RELATIVE, allowed_kinds=TERRITORY_KINDS)

    def show(self, *, now: Optional[datetime] = None) -> Dict[str, object]:
        current = _now(now)
        rows = self.registry.known_records()
        territories = [{"id": lease["id"], "node_id": lease["node_id"], "paths": lease["paths"],
                        "expires_at": lease["expires_at"], "state": state}
                       for lease, state in _states(self.records(), rows, current)]
        return {"checked_at": format_timestamp(current), "territories": territories}

    def _conflicts(self, records, owner, edit_keys, canonicalizer, now) -> List[_Conflict]:
        conflicts = []
        rows = self.registry.known_records()
        for lease, state in _states(records, rows, now):
            if state != "live" or lease["node_id"] == owner:
                continue
            for entry in lease["paths"]:
                territory = canonicalizer.lease_key(entry)
                for edit in edit_keys:
                    if territory.contains_or_within(edit):
                        conflicts.append(_Conflict(lease, territory, edit))
        return conflicts

    def preview(self, owner: str, raw_edits: Sequence[str], *, records=None,
                now: Optional[datetime] = None, base_ref: Optional[str] = None,
                _allow_unavailable: bool = False) -> Dict[str, object]:
        """Read-only and advisory: the live leases a dispatch's edits enter
        (claimed) and the changes other worktrees already hold there (observed)."""
        owner = validate_identifier(owner, "owner")
        self.registry.require_active(owner)
        current = _now(now)
        canonicalizer = _Canonicalizer()
        edits = _canonical_list(canonicalizer, raw_edits, "--edits")
        edit_keys = [canonicalizer.key(path) for path in edits]
        try:
            rows = self.records() if records is None else records
        except (DurabilityFailure, IntegrityFailure) as error:
            if not _allow_unavailable:
                raise
            return {"advisory": True, "stamp": "UNKNOWN", "reason": error.code}
        conflicts = self._conflicts(rows, owner, edit_keys, canonicalizer, current)
        overlap_cache: Dict[Tuple[Optional[str], Optional[str]], Dict[str, object]] = {}
        listed = []
        for conflict in conflicts:
            entry: Dict[str, object] = {
                "territory_id": conflict.lease["id"],
                "holder": conflict.lease["node_id"],
                "expires_at": conflict.lease["expires_at"],
                "territory_path": conflict.territory.spelled,
                "edit_path": conflict.edit.spelled,
                "signal": {
                    "kind": "same_territory",
                    "coordinate": conflict.territory.coordinate(conflict.edit),
                    "stamp": "DERIVED",
                    "hard_lock": False,
                    "detail": "the edit path lies inside, or contains, another node's live territory",
                },
            }
            pair = (conflict.territory.worktree, conflict.edit.worktree)
            if pair not in overlap_cache:
                if conflict.territory.namespace != "git" or not conflict.territory.shares_repository(conflict.edit):
                    overlap_cache[pair] = {"stamp": "UNKNOWN", "reason": "not_one_repository",
                                           "detail": "the two paths are not in one Git repository"}
                elif pair[0] is None or not os.path.isdir(pair[0]):
                    overlap_cache[pair] = {"stamp": "UNKNOWN", "reason": "holder_worktree_absent",
                                           "detail": "the holder's worktree no longer exists"}
                else:
                    overlap_cache[pair] = _overlap_evidence(pair[0], str(pair[1]))
            entry["overlap"] = overlap_cache[pair]
            listed.append(entry)
        observed, absences = _observed_for_edits(canonicalizer, edit_keys, base_ref)
        return {"schema_version": 1, "advisory": True, "owner": owner, "edits": edits,
                "checked_at": format_timestamp(current), "conflicts": listed,
                "observed": observed, "observed_absences": absences, "coverage": COVERAGE}

    # -- writes

    def _require_actor(self, node: str, act: str) -> str:
        node = validate_identifier(node, "node")
        try:
            self.registry.require_active(node)
        except ProtocolRefusal:
            raise _refuse("territory_node_unknown", act + " needs a registered active node",
                          remedy="pass --as as a registered active node") from None
        self.registry.require_protocol_lease(node, act=act)
        return node

    @staticmethod
    def _require_key(key: object) -> str:
        if not key_valid(key):
            raise _refuse("territory_key_invalid", "idempotency key must be visible text of 1 to 128 characters",
                          remedy="pass --idempotency-key as visible text of 1 to 128 characters")
        return str(key)

    def declare(self, node: str, raw_paths: Sequence[str], ttl_seconds: int, key: str,
                *, now: Optional[datetime] = None) -> Dict[str, object]:
        node = self._require_actor(node, "territory declare")
        key = self._require_key(key)
        if type(ttl_seconds) is not int or not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS:
            raise _refuse("territory_ttl_invalid", "lease length is outside its bounds",
                          remedy=f"pass --ttl-seconds between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}")
        real = _now()
        start = real if now is None else _now(now)
        if start > real:
            raise _refuse("territory_time_invalid", "a lease cannot start in the future",
                          remedy="omit --now, or pass a UTC timestamp at or before the present")
        canonicalizer = _Canonicalizer()
        paths = [canonicalizer.claim(path) for path in _canonical_list(canonicalizer, raw_paths, "--path")]
        keys = [canonicalizer.key(str(entry["path"])) for entry in paths]
        candidate = {
            "schema_version": 1, "id": LEASE_PREFIX + uuid7_hex(), "tenant_id": self.root.tenant_id,
            "timestamp": format_timestamp(start), "kind": "territory_lease", "node_id": node,
            "paths": paths, "ttl_seconds": ttl_seconds,
            "expires_at": format_timestamp(start + timedelta(seconds=ttl_seconds)),
            "phase": "leased", "released_at": None, "predecessor_receipt_id": None,
            "idempotency_key": key,
        }

        def decide(records):
            prior = next((r for r in records if r.get("kind") == "territory_lease"
                          and r.get("idempotency_key") == key and r.get("node_id") == node), None)
            if prior is not None:
                if prior["phase"] == "leased" and prior["paths"] == paths and prior["ttl_seconds"] == ttl_seconds:
                    return prior, None
                raise _refuse("territory_key_conflict", "idempotency key already names a different territory act",
                              remedy="replay the original request with this key, or choose a fresh key")
            conflicts = self._conflicts(records, node, keys, canonicalizer, real)
            overlapping = [{"territory_id": c.lease["id"], "holder": c.lease["node_id"],
                            "expires_at": c.lease["expires_at"], "territory_path": c.territory.spelled,
                            "path": c.edit.spelled, "coordinate": c.territory.coordinate(c.edit)}
                           for c in conflicts]
            return {**candidate, "advisory": {"overlapping_leases": overlapping}}, candidate

        return transact(self.root, TERRITORY_RELATIVE, decide, allowed_kinds=TERRITORY_KINDS)

    def release(self, node: str, territory_id: str, key: str) -> Dict[str, object]:
        node = self._require_actor(node, "territory release")
        key = self._require_key(key)
        current = format_timestamp(_now())

        def decide(records):
            prior = next((r for r in records if r.get("kind") == "territory_lease"
                          and r.get("idempotency_key") == key and r.get("node_id") == node), None)
            if prior is not None:
                if prior["phase"] == "released" and prior["predecessor_receipt_id"] == territory_id:
                    return prior, None
                raise _refuse("territory_key_conflict", "idempotency key already names a different territory act",
                              remedy="replay the original request with this key, or choose a fresh key")
            opened, released = _lease_rows(records)
            original = opened.get(str(territory_id))
            if original is None or original["node_id"] != node:
                raise _refuse("territory_unknown", "the selected territory does not belong to this node",
                              remedy="pass --territory as one of this node's lease IDs from floati territory show")
            if str(territory_id) in released:
                raise _refuse("territory_already_released", "the selected territory is already released",
                              remedy="replay the release with its original key, or select another territory")
            row = {
                "schema_version": 1, "id": LEASE_PREFIX + uuid7_hex(), "tenant_id": self.root.tenant_id,
                "timestamp": current, "kind": "territory_lease", "node_id": node,
                "paths": original["paths"], "ttl_seconds": original["ttl_seconds"],
                "expires_at": original["expires_at"], "phase": "released", "released_at": current,
                "predecessor_receipt_id": original["id"], "idempotency_key": key,
            }
            return row, row

        return transact(self.root, TERRITORY_RELATIVE, decide, allowed_kinds=TERRITORY_KINDS)

    def advise_dispatch(self, owner: str, raw_edits: Sequence[str]) -> Optional[Dict[str, object]]:
        """Advisory only: the preview a dispatch with --edits carries. Never refuses."""
        if not raw_edits:
            return None
        return self.preview(owner, raw_edits, _allow_unavailable=True)


COVERAGE = {
    "ends_at": "participating_writers",
    "claimed": "live leases recorded on this fleet root",
    "observed": "worktrees registered with each edit's repository",
    "not_seen": "writers outside those worktrees, and nodes that declared nothing",
}
_MAX_LISTED_PATHS = 200


def _overlap_evidence(left_worktree: str, right_worktree: str) -> Dict[str, object]:
    """The existing overlap fact for two worktree HEADs of one repository."""
    from .overlap_radar import _git, derive_overlap_report

    try:
        left = _git(Path(left_worktree), "rev-parse", "--verify", "HEAD^{commit}").strip()
        right = _git(Path(right_worktree), "rev-parse", "--verify", "HEAD^{commit}").strip()
        base = _git(Path(right_worktree), "merge-base", left, right).strip()
        report = derive_overlap_report(Path(right_worktree), base, left, right)
    except ProtocolRefusal as exc:
        return {"stamp": "UNKNOWN", "reason": exc.code, "detail": exc.detail}
    return {"stamp": "MEASURED", "report": report}


def _bounded(paths: Sequence[str]) -> Dict[str, object]:
    ordered = sorted(paths)
    return {"paths": ordered[:_MAX_LISTED_PATHS], "path_count": len(ordered),
            "truncated": len(ordered) > _MAX_LISTED_PATHS}


def _worktree_entries(top: str) -> List[Dict[str, object]]:
    from .overlap_radar import _git

    entries: List[Dict[str, object]] = []
    current: Dict[str, object] = {}
    for line in _git(Path(top), "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if current:
                entries.append(current)
            current = {}
            continue
        field, _space, value = line.partition(" ")
        current[field] = value if value else True
    return entries


def observe_repository(repository: str, base_ref: str) -> Dict[str, object]:
    """The public observed read: every worktree pair that changed one path."""
    facts, _changed = _observe(repository, base_ref, None)
    return facts


def _observe(repository: str, base_ref: str, canonicalizer: Optional[_Canonicalizer],
             *, selected_worktrees=None):
    """What each registered worktree changed against its merge base with
    `base_ref`, committed or not, and every pair that changed one path. A
    branch that has landed has no changes left, so its overlap expires."""
    from .overlap_radar import _git

    canonical = canonicalizer or _Canonicalizer()
    if not isinstance(base_ref, str) or not base_ref or base_ref.startswith("-"):
        raise _refuse("overlap_base_ref_invalid", "the base ref must name one commit",
                      remedy="pass --base-ref as a branch, tag or commit of this repository")
    top = canonical.spelled(_git(Path(canonical.spelled(repository)), "rev-parse",
                                 "--show-toplevel").strip())
    anchor = canonical._common_dir(top)
    base_sha = _git(Path(top), "rev-parse", "--verify", base_ref + "^{commit}").strip()
    worktrees: List[Dict[str, object]] = []
    changed: Dict[str, List[str]] = {}
    for entry in _worktree_entries(top):
        if entry.get("bare") is True or not isinstance(entry.get("worktree"), str):
            continue
        path = _on_disk(os.path.realpath(str(entry["worktree"])))[0]
        if selected_worktrees is not None and path not in selected_worktrees:
            continue
        row: Dict[str, object] = {"worktree": path, "branch": entry.get("branch"),
                                  "head": entry.get("HEAD")}
        if not os.path.isdir(path):
            worktrees.append({**row, "state": "absent"})
            continue
        try:
            merge_base = _git(Path(path), "merge-base", base_sha, str(entry.get("HEAD"))).strip()
            tracked = _git(Path(path), "diff", "--name-only", "-z", "--no-renames", merge_base, "--")
            untracked = _git(Path(path), "ls-files", "--others", "--exclude-standard", "-z")
        except ProtocolRefusal as exc:
            worktrees.append({**row, "state": "unknown", "reason": exc.code})
            continue
        files = sorted({item for item in (tracked + "\0" + untracked).split("\0") if item})
        changed[path] = files
        worktrees.append({**row, "state": "observed", "merge_base": merge_base,
                          "changed_count": len(files)})
    observed = sorted(changed)
    pairs = []
    for index, left in enumerate(observed):
        for right in observed[index + 1:]:
            shared = set(changed[left]) & set(changed[right])
            if shared:
                pairs.append({"left": left, "right": right, "basis": "observed", **_bounded(shared)})
    return ({"schema_version": 1, "advisory": True, "repository": anchor, "base_ref": base_ref,
             "base_sha": base_sha, "worktrees": worktrees, "pairs": pairs, "coverage": COVERAGE},
            changed)


def _observed_for_edits(canonicalizer: _Canonicalizer, edit_keys: Sequence[PathKey],
                        base_ref: Optional[str]):
    """Changes other worktrees already hold inside a dispatch's edit paths."""
    from .overlap_radar import _git

    groups: Dict[str, List[PathKey]] = {}
    for key in edit_keys:
        if key.namespace == "git" and key.worktree is not None:
            groups.setdefault(key.anchor, []).append(key)
    observed: List[Dict[str, object]] = []
    absences: List[Dict[str, object]] = []
    for anchor, keys in groups.items():
        own = str(keys[0].worktree)
        base = base_ref
        if base is None:
            try:
                base = _git(Path(own), "rev-parse", "--verify", "--quiet",
                            "refs/remotes/origin/HEAD^{commit}").strip()
            except ProtocolRefusal:
                absences.append({"repository": anchor, "stamp": "UNKNOWN", "reason": "base_ref_unknown",
                                 "detail": "no --base-ref was given and the repository has no origin/HEAD"})
                continue
        try:
            facts, changed = _observe(own, base, canonicalizer)
        except ProtocolRefusal as exc:
            absences.append({"repository": anchor, "stamp": "UNKNOWN", "reason": exc.code,
                             "detail": exc.detail})
            continue
        own_worktrees = {str(key.worktree) for key in keys}
        rows = {str(row["worktree"]): row for row in facts["worktrees"]}
        for worktree, files in sorted(changed.items()):
            if worktree in own_worktrees:
                continue
            inside = [path for path in files
                      if any(key.within_repository(tuple(path.split("/")), key.folds) for key in keys)]
            if not inside:
                continue
            observed.append({"basis": "observed", "repository": anchor, "worktree": worktree,
                             "branch": rows[worktree].get("branch"), "head": rows[worktree].get("head"),
                             "base_sha": facts["base_sha"], **_bounded(inside),
                             "overlap": _overlap_evidence(worktree, own)})
    return observed, absences


# ---------------------------------------------------------------- CLI


def register_cli(commands) -> None:
    territory = commands.add_parser("territory")
    actions = territory.add_subparsers(dest="territory_action", required=True)
    declare = actions.add_parser("declare")
    declare.add_argument("--root", required=True, metavar="ROOT")
    declare.add_argument("--as", dest="actor", required=True, metavar="NODE")
    declare.add_argument("--path", dest="paths", action="append", required=True, metavar="PATH")
    declare.add_argument("--ttl-seconds", required=True, type=int, metavar="N")
    declare.add_argument("--idempotency-key", required=True, metavar="KEY")
    declare.add_argument("--now", metavar="TIMESTAMP")
    release = actions.add_parser("release")
    release.add_argument("--root", required=True, metavar="ROOT")
    release.add_argument("--as", dest="actor", required=True, metavar="NODE")
    release.add_argument("--territory", required=True, metavar="ID")
    release.add_argument("--idempotency-key", required=True, metavar="KEY")
    show = actions.add_parser("show")
    show.add_argument("--root", required=True, metavar="ROOT")
    preview = actions.add_parser("preview")
    preview.add_argument("--root", required=True, metavar="ROOT")
    preview.add_argument("--owner", required=True, metavar="NODE")
    preview.add_argument("--edits", action="append", required=True, metavar="PATH")
    preview.add_argument("--base-ref", metavar="REF")
    for parser in (declare, release, show, preview):
        parser.set_defaults(handler=_handle)


def _parse_now(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise _refuse("territory_time_invalid", "--now must be a UTC timestamp",
                      remedy="pass --now as a UTC RFC3339 timestamp ending in Z, or omit it")
    return moment


def _handle(args):
    from .cli import _root

    ledger = TerritoryLedger(_root(args.root))
    action = args.territory_action
    if action == "declare":
        result = ledger.declare(args.actor, args.paths, args.ttl_seconds, args.idempotency_key,
                                now=_parse_now(args.now))
    elif action == "release":
        result = ledger.release(args.actor, args.territory, args.idempotency_key)
    elif action == "show":
        result = ledger.show()
    else:
        result = ledger.preview(args.owner, args.edits, base_ref=args.base_ref)
    return "ok", result, 0
