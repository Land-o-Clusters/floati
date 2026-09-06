"""WIND-DOWN as one verb: `node prep-clear` (LC-1c).

Composed, never re-implemented. The commit-and-push state is measured with the
fixed Git coordinates every production observer already uses; the checkpoint
envelope goes through the bus send primitive behind RB-1's own banked-sha
predicate; the wake claim is identified by the native Codex-wait session
authority and released through the existing WakeController. This module writes
exactly one record of its own, and nothing else in the fleet changes shape.

LC-R4: a verb carries its consequences. A stop order that leaves unpushed
commits or a dirty tree must NAME WHAT IT DOES NOT COVER, and that sentence is
recorded verbatim, because the next seat reads the receipt and not the tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .errors import ProtocolRefusal
from .git_process import fixed_git_command, fixed_git_environment
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .records import _terminal_unsafe
from .registry import Registry, utc_now
from .root import FloatiRoot


KIND = "prep_clear_receipt"
_GIT_CANDIDATES = ("/usr/bin/git", "/bin/git")
_GIT_TIMEOUT_SECONDS = 30
_MAX_COMPLEMENT = 1024


def _select_git_executable(explicit: object = None) -> str:
    """One Git from a fixed candidate list; PATH is never consulted."""

    from .fleet_update import _explicit_executable

    code = "prep_clear_git_unavailable"
    if explicit is not None:
        return _explicit_executable(explicit, code)
    for candidate in _GIT_CANDIDATES:
        path = Path(candidate)
        if not path.exists() and not path.is_symlink():
            continue
        return _explicit_executable(candidate, code)
    raise ProtocolRefusal(
        code,
        "git is absent from the fixed candidates: " + ", ".join(_GIT_CANDIDATES),
        "install git at one of those paths, or pass --git-executable",
    )


def _banked_sha_predicate() -> Callable[[str], None]:
    """Return RB-1's send-side fence itself; prep-clear grows no second one."""

    from .cli import _require_banked_sha

    return _require_banked_sha


class PrepClear:
    """One idempotent wind-down: refuse, checkpoint, release, receipt."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    # ---- measurement -----------------------------------------------------

    def _git(self, executable: str, repository: Path, arguments: tuple) -> str:
        try:
            completed = subprocess.run(
                fixed_git_command(executable, repository, arguments),
                env=fixed_git_environment(executable),
                capture_output=True,
                text=True,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProtocolRefusal(
                "prep_clear_workspace_unreadable",
                f"git could not inspect {repository}: {exc}",
                "run prep-clear from a checkout git can read, or pass --git-executable",
            ) from exc
        if completed.returncode != 0:
            raise ProtocolRefusal(
                "prep_clear_workspace_unreadable",
                completed.stderr.strip() or f"git {arguments[0]} failed in {repository}",
                "run prep-clear against the seat's own git checkout",
            )
        return completed.stdout

    def _workspace_state(self, executable: str, workspace: Path) -> Dict[str, Any]:
        """Measure dirt and unbanked commits, then name the banked tip."""

        toplevel = self._git(executable, workspace, ("rev-parse", "--show-toplevel")).strip()
        if not toplevel or Path(toplevel).resolve() != workspace.resolve():
            raise ProtocolRefusal(
                "prep_clear_workspace_invalid",
                f"{workspace} is not the top level of its own git checkout",
                "pass --workspace as the seat's checkout root, not a subdirectory",
            )
        dirty_lines = [
            line
            for line in self._git(
                executable, workspace, ("status", "--porcelain", "--untracked-files=normal")
            ).splitlines()
            if line.strip()
        ]
        # The same ref set RB-1 checks: reachable from refs/remotes, or unbanked.
        unpushed = [
            line.strip()
            for line in self._git(
                executable, workspace, ("rev-list", "HEAD", "--not", "--remotes")
            ).splitlines()
            if line.strip()
        ]
        unbanked = set(unpushed)
        pushed_tip: Optional[str] = None
        for line in self._git(executable, workspace, ("rev-list", "HEAD")).splitlines():
            candidate = line.strip()
            if candidate and candidate not in unbanked:
                pushed_tip = candidate
                break
        if pushed_tip is None:
            raise ProtocolRefusal(
                "prep_clear_pushed_tip_absent",
                f"no commit reachable from HEAD in {workspace} is on any refs/remotes ref",
                "push this branch, run git fetch --all in the workspace, then prep-clear again",
            )
        return {
            "dirty": bool(dirty_lines),
            "dirty_entry_count": len(dirty_lines),
            "unpushed_commit_count": len(unpushed),
            "pushed_tip": pushed_tip,
        }

    # ---- coordinates -----------------------------------------------------

    @staticmethod
    def _key(value: object) -> str:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 128
            or _terminal_unsafe(value)
        ):
            raise ProtocolRefusal(
                "idempotency_key_invalid",
                "idempotency key is out of bounds or terminal-unsafe",
                "pass --idempotency-key as 1 to 128 terminal-safe characters",
            )
        return value

    @staticmethod
    def _complement(value: object) -> Optional[str]:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= _MAX_COMPLEMENT
            or _terminal_unsafe(value)
            or "\n" in value
        ):
            raise ProtocolRefusal(
                "prep_clear_complement_invalid",
                f"the complement must be one terminal-safe line of 1 to {_MAX_COMPLEMENT} characters",
                'pass --complement "<what this stop does not cover>" as one line',
            )
        return value

    def _architect(self, explicit: object) -> str:
        registry = Registry(self.root)
        if explicit is not None:
            return registry.resolve_node_id(explicit, field="to")
        architects = [
            node_id
            for node_id in registry.active_node_ids()
            if str(registry.require_active(node_id).get("role", "")).casefold()
            == "architect"
        ]
        if len(architects) != 1:
            raise ProtocolRefusal(
                "prep_clear_architect_unresolved",
                f"the active fleet declares {len(architects)} architects; the checkpoint has no single recipient",
                "pass --to with the exact node the checkpoint envelope is for",
            )
        return architects[0]

    def _held_claim(self, binding: Any, session: str) -> Dict[str, Any]:
        """Name the claim this session holds, or the claimant that holds it."""

        from .codex_wait_contract import CodexWaitSessionLedger

        rows = [
            row
            for row in read_records_snapshot(
                self.root,
                CodexWaitSessionLedger._relative(binding.node_id),
                allowed_kinds={"codex_wait_session_receipt"},
            )
            if row.get("workspace") == binding.workspace.as_posix()
        ]
        if not rows:
            raise ProtocolRefusal(
                "prep_clear_claim_not_held",
                f"no wake claim exists for {binding.workspace}; this session released nothing",
                "arm the seat with wake arm before winding it down",
            )
        current = rows[-1]
        if current.get("acting_session_id") != session:
            raise ProtocolRefusal(
                "prep_clear_claim_not_held",
                "the wake claim is held by acting session "
                f"{current.get('acting_session_id')}, not by {session}",
                "wind down from the session that holds the claim, or take it over first",
            )
        return current

    # ---- the verb --------------------------------------------------------

    def clear(
        self,
        node: str,
        workspace: Path | str,
        session: object,
        *,
        repo: str,
        doc: str,
        note: str,
        idempotency_key: str,
        complement: object = None,
        to: object = None,
        git_executable: object = None,
    ) -> Dict[str, Any]:
        from .codex_wait_contract import CodexWaitConsentLedger, resolve_participant
        from .events import EventLog
        from .wake_control import WakeController, validate_session_id

        node_id = Registry(self.root).resolve_node_id(node, field="node")
        acting_session = validate_session_id(session)
        key = self._key(idempotency_key)
        complement_text = self._complement(complement)
        relative = Path("receipts/prep-clear") / f"{node_id}.jsonl"

        # A repeated key is answered from the ledger: the envelope is posted
        # once and the claim is released once, however many times this runs.
        for row in read_records_snapshot(
            self.root, relative, allowed_kinds={KIND}
        ):
            if row.get("idempotency_key") == key:
                if (
                    row.get("node_id") != node_id
                    or row.get("acting_session_id") != acting_session
                    or row.get("complement") != complement_text
                ):
                    raise ProtocolRefusal(
                        "prep_clear_idempotency_conflict",
                        "this wind-down key was used for different coordinates",
                        "use a new --idempotency-key for a different wind-down",
                    )
                return dict(row)

        path = Path(workspace)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise ProtocolRefusal(
                "prep_clear_workspace_invalid",
                "workspace must be an existing absolute directory that is not a symlink",
                "pass --workspace as the seat's own checkout root",
            )
        participant = resolve_participant(self.root.tenant_home, path)
        if participant is None or participant.root.tenant_home != self.root.tenant_home:
            raise ProtocolRefusal(
                "prep_clear_participant_unresolved",
                "no waiter workspace binding for this workspace; there is no wake claim to release",
                "install the waiter through the governed path, then wind down",
            )
        binding = participant.binding
        if binding.node_id != node_id:
            raise ProtocolRefusal(
                "prep_clear_actor_mismatch",
                f"workspace binding belongs to {binding.node_id}, not to {node_id}",
                "wind down as the node that owns this workspace binding",
            )
        CodexWaitConsentLedger(self.root).require_armed(binding)
        claim = self._held_claim(binding, acting_session)

        executable = _select_git_executable(git_executable)
        state = self._workspace_state(executable, path)
        if (state["dirty"] or state["unpushed_commit_count"]) and complement_text is None:
            raise ProtocolRefusal(
                "prep_clear_stop_incomplete",
                f"{path} has {state['dirty_entry_count']} uncommitted entries and "
                f"{state['unpushed_commit_count']} unpushed commits; this stop does not cover them",
                'commit and push, or pass --complement "<what this stop does not cover>"',
            )

        pushed_tip = state["pushed_tip"]
        _banked_sha_predicate()(pushed_tip)
        recipient = self._architect(to)

        envelope = EventLog(self.root).send(
            node_id,
            recipient,
            repo,
            pushed_tip,
            doc,
            note,
            idempotency_key="prep-clear-envelope-" + key,
        )["message"]

        controller = WakeController(self.root)
        try:
            release = controller.pause(
                node_id, acting_session, idempotency_key="prep-clear-release-" + key
            )["receipt"]
            outcome, release_id = "released", str(release["id"])
        except ProtocolRefusal as exc:
            if exc.code != "wake_session_already_paused":
                raise
            # Recorded absence, not a silent success: the claim was already out
            # of service, so this run released nothing and says so.
            outcome, release_id = "already_released", None

        row: Dict[str, Any] = {
            "schema_version": 1,
            "id": "prep-clear-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": KIND,
            "node_id": node_id,
            "workspace": binding.workspace.as_posix(),
            "workspace_map_digest": binding.map_digest,
            "acting_session_id": acting_session,
            "pushed_tip": pushed_tip,
            "workspace_dirty": state["dirty"],
            "unpushed_commit_count": state["unpushed_commit_count"],
            "complement": complement_text,
            "envelope_id": str(envelope["id"]),
            "envelope_recipient": recipient,
            "released_claim_receipt_id": str(claim["id"]),
            "wake_release_outcome": outcome,
            "wake_release_receipt_id": release_id,
            "idempotency_key": key,
        }

        def decide(prior: List[Dict[str, Any]]):
            for existing in prior:
                if existing.get("idempotency_key") == key:
                    return existing, None
            return row, row

        return transact(self.root, relative, decide, allowed_kinds={KIND})
