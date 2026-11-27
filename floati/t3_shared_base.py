"""Read-only board for explicitly bound T3 children on one branch."""
from collections import Counter
from pathlib import Path
import json
import re

from .errors import ProtocolRefusal
from .jsonl import read_records_snapshot
from .overlap_radar import _git, _resolve_commit
from .t3_dispatch import checked_ledger
from .t3_dispatch_hook import coordinate
from .t3_dispatch_records import DISPATCH_KIND, T3_DISPATCH_KINDS, UUID7
from .territory import _bounded, _observe


def _read_bindings(path):
    invalid = ProtocolRefusal("t3_board_bindings_invalid",
                              "Supply a bounded JSON list of unique dispatch, task and child bindings.")
    try:
        if not path.is_absolute() or not path.is_file():
            raise invalid
        with path.open("rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise invalid
        rows = json.loads(data)
    except (OSError, ValueError, RecursionError) as exc:
        raise invalid from exc
    if not isinstance(rows, list) or len(rows) > 256:
        raise invalid
    seen = {field: set() for field in ("dispatch_id", "taskId", "childThreadId")}
    paths = {}
    for row in rows:
        if (not isinstance(row, dict) or row.keys() != seen.keys() | {"worktree"}
                or not all(coordinate(row[field]) for field in seen)
                or not re.fullmatch("t3-dispatch-" + UUID7, row["dispatch_id"])):
            raise invalid
        for field, ids in seen.items():
            if row[field] in ids:
                raise invalid
            ids.add(row[field])
        workspace = row["worktree"]
        if workspace is not None and (not isinstance(workspace, str) or len(workspace) > 4096
                                      or "\0" in workspace or not Path(workspace).is_absolute()):
            raise invalid
        try:
            paths[row["dispatch_id"]] = str(Path(workspace).resolve()) if workspace else None
        except (OSError, ValueError, RuntimeError) as exc:
            raise invalid from exc
    return rows, paths


def board(root, repository, base_ref, bindings_path, producer_tip):
    # Accept branch coordinates verbatim so moved tips do not split a group.
    if not coordinate(base_ref) or (producer_tip is not None and not coordinate(producer_tip)):
        raise ProtocolRefusal("t3_board_ref_invalid", "Name a base branch and an optional producer ref.")
    bindings, paths = _read_bindings(Path(bindings_path))
    receipts = {row["id"]: row for row in read_records_snapshot(
        root, checked_ledger(root), allowed_kinds=T3_DISPATCH_KINDS) if row["kind"] == DISPATCH_KIND}
    selected = []
    for binding in bindings:
        receipt = receipts.get(binding["dispatch_id"])
        if receipt is None or receipt["decision"] == "denied":
            raise ProtocolRefusal("t3_board_dispatch_unbound", "Bind each child to its allowed dispatch receipt.")
        if receipt["base_ref"] == base_ref:
            selected.append((binding, receipt, paths[binding["dispatch_id"]]))

    ownership = Counter(path for path in paths.values() if path is not None)
    distinct = {path: binding["childThreadId"] for binding, receipt, path in selected
                if path is not None and ownership[path] == 1 and receipt["isolation"] == "worktree"}
    observed, changed = _observe(repository, base_ref, None, selected_worktrees=distinct)
    tip = _resolve_commit(Path(repository), producer_tip) if producer_tip is not None else None
    worktrees = {row["worktree"]: row for row in observed["worktrees"]}
    included = {}
    children = []
    for binding, receipt, path in selected:
        child = binding["childThreadId"]
        view = {
            "dispatch_id": receipt["id"], "task_id": binding["taskId"], "child_thread_id": child,
            "parent_thread_id": receipt["parent_thread_id"], "role": receipt["role"],
            "worktree": path, "head": None, "territory": None, "contains_producer_tip": None,
        }
        if receipt["isolation"] != "worktree" or (path is not None and ownership[path] > 1):
            view["state"] = "shared_or_unattributed_worktree"
        elif path is None or path not in worktrees:
            view["state"] = "worktree_unavailable"
        else:
            tree = worktrees[path]
            view.update(state=tree["state"], head=tree["head"])
            if path in changed:
                view["territory"] = _bounded(changed[path])
                if tip is not None:
                    if view["head"] not in included:
                        try:
                            ancestor = _git(Path(repository), "merge-base", tip, view["head"]).strip()
                            included[view["head"]] = ancestor == tip
                        except ProtocolRefusal:
                            included[view["head"]] = None
                    view["contains_producer_tip"] = included[view["head"]]
        children.append(view)
    overlaps = [{**{key: pair[key] for key in ("paths", "path_count", "truncated")},
                 "left": distinct[pair["left"]], "right": distinct[pair["right"]]}
                for pair in observed["pairs"]]
    return {"advisory": True, "base_ref": base_ref, "base_sha": observed["base_sha"],
            "producer_tip": tip, "children": children, "overlaps": overlaps}
