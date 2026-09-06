"""The three measured preconditions that hold Codex hooks OFF.

The 2026-08-30 hooks ruling — restated in
`docs/rulings/2026-09-01-the-two-rows-i-was-holding-for-a-word.md` §1 — says
that enabling hooks on a harness that cannot be allowlisted fuses write-config
with persistent execution, and names three preconditions:

1. **a behavioural record from the burn** — what one hook invocation was
   *observed* to do, witnessed by a capture digest;
2. **confinement at some layer, retested per release** — the containment
   result carries the release identifier the package spells, so a result
   measured against another release is stale rather than reusable;
3. **MEASURED proof a session cannot write the config** — the write is
   *attempted* from the hook session's own authority and refused by name.

This module is the measurement, not the enabling. Nothing here installs a
hook, writes Codex trust state, or grants trust. ⇒ A PRECONDITION NOBODY CAN
RUN IS A PROMISE; the three functions below are the difference.

One scope limit is structural rather than apologetic: `invocation_source`
admits `live_codex_stop`, and no such record can be produced here, because
producing one means trusting a Stop hook in a live harness — the act the
ruling forbids. The vocabulary carries that absence instead of hiding it, so
a harness-executed burn can never be read as a live one.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Mapping, Optional, Tuple

from .codex_hook_trust import codex_hook_current_hash
from .errors import ProtocolRefusal
from .records import (
    HOOK_BURN_EFFECT_CLASSES,
    HOOK_BURN_INVOCATION_SOURCES,
    _SPECS,
    validate_record,
)
from .release import module_version
from .root import FloatiRoot


__all__ = (
    "CONFINEMENT_ESCAPE_NAMES",
    "ConfinementResult",
    "ContainmentAttempt",
    "HOOK_BURN_EFFECT_CLASSES",
    "HOOK_BURN_FIELDS",
    "HOOK_BURN_INVOCATION_SOURCES",
    "HOOK_BURN_KINDS",
    "TRUST_CONFIG_WRITE_NAMES",
    "attempt_trust_config_write",
    "confinement_retest",
    "hook_burn_record",
    "hook_command_identity",
    "observe_hook_burn",
    "package_release_id",
    "path_shape",
    "require_burn_current",
    "require_confinement_current",
    "validate_hook_burn",
)

HOOK_BURN_KINDS: FrozenSet[str] = frozenset({"hook_burn_record"})
HOOK_BURN_FIELDS: FrozenSet[str] = _SPECS["hook_burn_record"][1]

CODEX_HOME_PLACEHOLDER = "<codex-home>"

#: The closed set of spellings a confined writer can use to name something
#: outside its own home. Each name exercises a DIFFERENT clause of the
#: containment guard, because a guard's clauses are not independent and one
#: removal can disarm a check two lines away.
CONFINEMENT_ESCAPE_NAMES: Tuple[str, ...] = (
    "absolute",
    "embedded_traversal",
    "parent_traversal",
    "symlink_component",
    "symlink_leaf",
)

#: The same spellings, aimed at the Codex trust config specifically.
TRUST_CONFIG_WRITE_NAMES: Tuple[str, ...] = (
    "absolute",
    "embedded_traversal",
    "parent_traversal",
    "symlink_component",
)

_PROBE_DIRECTORY = ".floati-hook-precondition-probe"
_PROBE_LINK = ".floati-hook-precondition-link"
_PROBE_LEAF_LINK = ".floati-hook-precondition-leaf"
_PROBE_BYTES = b"floati-hook-precondition-probe\n"


# --------------------------------------------------------------------------
# Release identity
# --------------------------------------------------------------------------


def package_release_id(repo_root) -> str:
    """The release the package SPELLS, read from the tree, never a literal.

    Read rather than imported for the reason `floati.release` already states:
    importing a package to ask what it says lets a re-export answer for it.
    A literal here would make "retested per release" true forever.
    """

    version = module_version(repo_root)
    if not isinstance(version, str) or not version:
        raise ProtocolRefusal(
            "hook_precondition_release_unreadable",
            "no release identifier could be read from the package",
            remedy="run this measurement against a tree whose floati/__init__.py assigns __version__",
        )
    return version


# --------------------------------------------------------------------------
# Clause 1 — the behavioural burn record
# --------------------------------------------------------------------------


def hook_command_identity(block: object) -> str:
    """Codex's own normalized trust hash for the Stop group that executed."""

    return codex_hook_current_hash(block)


def path_shape(path, *, host_root, placeholder: str = CODEX_HOME_PLACEHOLDER) -> str:
    """Keep the path's shape and lose its host prefix.

    A durable record may say *where in a Codex home* the hook lived; it may not
    say whose machine that was.
    """

    candidate = Path(path)
    host = Path(host_root)
    try:
        relative = candidate.relative_to(host)
    except ValueError as exc:
        raise ProtocolRefusal(
            "hook_burn_path_shape_hostful",
            "the recorded path is not inside the host root it claims to shape",
            remedy="name the host root the path actually lies under",
        ) from exc
    if not relative.parts:
        raise ProtocolRefusal(
            "hook_burn_path_shape_hostful",
            "a path shape must name at least one segment below the host root",
            remedy="record the file inside the host root, not the host root itself",
        )
    return placeholder + "/" + relative.as_posix()


def observe_hook_burn(
    *,
    before: Mapping[str, bytes],
    after: Mapping[str, bytes],
    exit_status: object,
    stdout: str,
    stderr: str,
) -> Dict[str, object]:
    """Derive one invocation's effect classes from what actually changed.

    The effects are computed from the before/after byte maps and the captured
    streams. Nothing is taken on the invocation's word: a hook that claims to
    have blocked and wrote nothing produces no `bus_path_created`.
    """

    created = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(
        path for path in set(after) & set(before) if after[path] != before[path]
    )
    effects = set()
    if created:
        effects.add("bus_path_created")
    if modified:
        effects.add("bus_path_modified")
    if removed:
        effects.add("bus_path_removed")
    if stdout:
        effects.add("stdout_emitted")
    if stderr:
        effects.add("stderr_diagnostic")
    try:
        decision = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        decision = None
    if isinstance(decision, dict) and decision.get("decision") == "block":
        effects.add("stop_decision_blocked")
    if not effects:
        effects.add("no_observable_effect")
    capture = hashlib.sha256(
        json.dumps(
            {
                "created": created,
                "exit_status": exit_status,
                "modified": modified,
                "removed": removed,
                "stderr": stderr,
                "stdout": stdout,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "observed_exit_code": exit_status,
        "observed_effects": sorted(effects),
        "capture_sha256": capture,
    }


def hook_burn_record(
    *,
    tenant_id: str,
    record_id: str,
    timestamp: str,
    release_id: str,
    hook_command_sha256: str,
    hook_group_index: int,
    hooks_path_shape: str,
    invocation_source: str,
    observation: Mapping[str, object],
) -> Dict[str, object]:
    """Assemble one burn record; the observation half is never invented here."""

    capture = observation.get("capture_sha256")
    return {
        "schema_version": 1,
        "id": record_id,
        "tenant_id": tenant_id,
        "timestamp": timestamp,
        "kind": "hook_burn_record",
        "release_id": release_id,
        "hook_command_sha256": hook_command_sha256,
        "hook_group_index": hook_group_index,
        "hooks_path_shape": hooks_path_shape,
        "invocation_source": invocation_source,
        "observed_exit_code": observation.get("observed_exit_code"),
        "observed_effects": list(observation.get("observed_effects") or []),
        "capture_sha256": capture,
        "unknown_fields": [] if capture is not None else ["capture_sha256"],
    }


def validate_hook_burn(record: object, expected_tenant: str) -> Dict[str, object]:
    """Validate one burn record at the durable boundary."""

    return validate_record(
        record, expected_tenant, HOOK_BURN_KINDS, integrity=False
    )


def require_burn_current(record: Mapping[str, object], *, repo_root) -> str:
    """Refuse a burn measured against a release this tree no longer spells."""

    release = package_release_id(repo_root)
    observed = record.get("release_id")
    if observed != release:
        raise ProtocolRefusal(
            "hook_burn_release_stale",
            f"burn record was measured against {observed!r}; this package spells {release!r}",
            remedy="re-run the hook burn against this release and record the result",
        )
    return release


# --------------------------------------------------------------------------
# Clauses 2 and 3 — containment, attempted rather than asserted
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainmentAttempt:
    """One spelling tried from a confined writer's authority, and what happened."""

    name: str
    spelling: str
    refusal_code: Optional[str]
    wrote: bool


@dataclass(frozen=True)
class ConfinementResult:
    """A confinement measurement bound to the release it was measured against."""

    release_id: str
    attempts: Tuple[ContainmentAttempt, ...]
    leaked: Tuple[str, ...]


def _escape_spellings(home: Path, target: Path, names: Tuple[str, ...]) -> Dict[str, str]:
    """Spell one outside target every way a contained relative path can."""

    relative = os.path.relpath(str(target), str(home))
    spellings = {
        "absolute": str(target),
        "parent_traversal": relative,
        "embedded_traversal": (_PROBE_DIRECTORY + "/" + relative),
        "symlink_component": _PROBE_LINK + "/" + target.name,
        "symlink_leaf": _PROBE_LEAF_LINK,
    }
    return {name: spellings[name] for name in names}


def _attempt(root: FloatiRoot, name: str, spelling: str) -> ContainmentAttempt:
    """Resolve one spelling from the writer's authority and, if it resolves
    outside, actually try to put bytes there — a resolution that is never
    exercised proves nothing about the write it would have permitted."""

    home = Path(root.tenant_home).resolve()
    try:
        resolved = root.resolve_relative(spelling)
    except ProtocolRefusal as exc:
        return ContainmentAttempt(name, spelling, exc.code, False)
    final = Path(resolved).resolve(strict=False)
    try:
        final.relative_to(home)
        escaped = False
    except ValueError:
        escaped = True
    wrote = False
    if escaped:
        original: Optional[bytes] = None
        existed = final.is_file()
        if existed:
            try:
                original = final.read_bytes()
            except OSError:
                original = None
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_bytes(_PROBE_BYTES)
            wrote = final.is_file() and final.read_bytes() == _PROBE_BYTES
        except OSError:
            wrote = False
        finally:
            # The attempt is the measurement; the damage is not. Whatever the
            # write reached is put back exactly as it was found.
            try:
                if wrote and existed and original is not None:
                    final.write_bytes(original)
                elif wrote and not existed:
                    final.unlink(missing_ok=True)
            except OSError:
                pass
    return ContainmentAttempt(name, spelling, None, wrote)


def _with_probe_links(home: Path, outside: Path, body):
    """Create, use and remove the two symlink shapes an escape can wear."""

    component = home / _PROBE_LINK
    leaf = home / _PROBE_LEAF_LINK
    created = []
    try:
        if not component.exists() and not component.is_symlink():
            component.symlink_to(outside.parent, target_is_directory=True)
            created.append(component)
        if not leaf.exists() and not leaf.is_symlink():
            leaf.symlink_to(outside)
            created.append(leaf)
        return body()
    finally:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass


def confinement_retest(*, root: FloatiRoot, repo_root) -> ConfinementResult:
    """Run the containment battery NOW and bind the result to this release.

    The battery aims at a sibling of the tenant home rather than at anything a
    host cares about: a broken guard must be observable without being
    destructive.
    """

    release = package_release_id(repo_root)
    home = Path(root.tenant_home).resolve()
    outside = home.parent / _PROBE_DIRECTORY / "escaped"
    spellings = _escape_spellings(home, outside, CONFINEMENT_ESCAPE_NAMES)

    def run() -> Tuple[ContainmentAttempt, ...]:
        return tuple(
            _attempt(root, name, spellings[name]) for name in CONFINEMENT_ESCAPE_NAMES
        )

    attempts = _with_probe_links(home, outside, run)
    leaked = tuple(attempt.name for attempt in attempts if attempt.wrote)
    return ConfinementResult(release_id=release, attempts=attempts, leaked=leaked)


def require_confinement_current(result: ConfinementResult, *, repo_root) -> str:
    """Refuse a confinement result measured against another release."""

    release = package_release_id(repo_root)
    if not isinstance(result, ConfinementResult):
        raise ProtocolRefusal(
            "hook_confinement_result_invalid",
            "a confinement precondition needs a confinement measurement",
            remedy="pass the result confinement_retest returned",
        )
    if result.release_id != release:
        raise ProtocolRefusal(
            "hook_confinement_release_stale",
            f"confinement was measured against {result.release_id!r}; this package spells {release!r}",
            remedy="re-run confinement_retest against this release",
        )
    if result.leaked:
        raise ProtocolRefusal(
            "hook_confinement_leaked",
            "confinement let a write land outside the tenant home: "
            + ", ".join(result.leaked),
            remedy="repair the containment guard before any hook precondition is claimed",
        )
    return release


def attempt_trust_config_write(
    *, root: FloatiRoot, target
) -> Tuple[ContainmentAttempt, ...]:
    """Attempt the Codex trust-config write from one hook session's authority.

    This is the third precondition's whole content. A permission bit is not a
    proof; the write is spelled every way the session can spell it and the
    refusal each spelling earns is returned by code. If any spelling actually
    lands bytes, the original content is restored and the attempt refuses —
    a writable trust config is a finding, not a return value.
    """

    config = Path(target)
    home = Path(root.tenant_home).resolve()
    spellings = _escape_spellings(home, config, TRUST_CONFIG_WRITE_NAMES)

    def run() -> Tuple[ContainmentAttempt, ...]:
        return tuple(
            _attempt(root, name, spellings[name]) for name in TRUST_CONFIG_WRITE_NAMES
        )

    attempts = _with_probe_links(home, config, run)
    wrote = tuple(attempt.name for attempt in attempts if attempt.wrote)
    if wrote:
        raise ProtocolRefusal(
            "hook_trust_config_writable",
            "a hook session's authority reached the Codex trust config as: "
            + ", ".join(wrote),
            remedy="repair containment before any hook is trusted; hooks stay off while this holds",
        )
    return attempts
