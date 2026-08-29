"""Explicit-path, permanently read-only survey of foreign agent-bus shapes."""

from __future__ import annotations

import json
import os
import shlex
import stat
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import ProtocolRefusal
from .multi_bus_chart import DeclaredRoots


_MAX_JSON_BYTES = 4 * 1024 * 1024
_NOTICE = (
    "Survey is read-only; foreign buses are never written, drained, "
    "acknowledged, registered, or locked."
)


def _absolute_regular_json(path: Path, label: str) -> Any:
    if not path.is_absolute():
        raise ProtocolRefusal(f"{label}_absolute_required", f"{label} path must be absolute")
    if path.is_symlink():
        raise ProtocolRefusal(f"{label}_symlinked", f"{label} path must not be a symlink")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ProtocolRefusal(f"{label}_unavailable", f"{label} file is unavailable") from exc
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode) or identity.st_size > _MAX_JSON_BYTES:
            raise ProtocolRefusal(f"{label}_invalid", f"{label} must be a bounded regular file")
        chunks = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal(f"{label}_invalid", f"{label} is not valid UTF-8 JSON") from exc


def _all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                yield key
            yield from _all_strings(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _all_strings(child)


def _search_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise ProtocolRefusal("survey_search_absolute_required", "survey search paths must be absolute")
    if path.is_symlink() or not path.is_dir():
        raise ProtocolRefusal(
            "survey_search_invalid", "survey search paths must be existing non-symlink directories"
        )
    return path.resolve()


def _shape(path: Path) -> Optional[str]:
    try:
        entries = list(os.scandir(path))
    except OSError:
        return None
    names = {entry.name: entry for entry in entries}
    registry = names.get("registry")
    events = names.get("events.jsonl")
    if (
        registry is not None and registry.is_dir(follow_symlinks=False)
    ) or (
        events is not None and events.is_file(follow_symlinks=False)
    ):
        return "floati-direct-v0-shaped"
    tenants = names.get("tenants")
    if tenants is not None and tenants.is_dir(follow_symlinks=False):
        return "agent-bus-namespaced-shaped"
    if any(
        entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False)
        for entry in entries
    ):
        return "agent-bus-jsonl-shaped"
    return None


class ForeignBusSurvey:
    """Survey only one bounded request; construction performs no observation."""

    def __init__(
        self,
        declared_roots: os.PathLike[str] | str,
        *,
        search_paths: Sequence[os.PathLike[str] | str],
        hooks_path: Optional[os.PathLike[str] | str],
        targets_paths: Sequence[os.PathLike[str] | str],
    ) -> None:
        self.declared_roots = DeclaredRoots(declared_roots)
        self.search_path_args = tuple(search_paths)
        self.hooks_path_arg = hooks_path
        self.targets_path_args = tuple(targets_paths)

    @staticmethod
    def _our_workspaces(declarations: Sequence[Mapping[str, Any]]) -> Set[str]:
        workspaces: Set[str] = set()
        for declaration in declarations:
            root = Path(declaration["root"])
            workspaces.add(str(root))
            nodes = root / "nodes"
            if nodes.is_symlink() or not nodes.is_dir():
                continue
            try:
                entries = list(os.scandir(nodes))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        workspaces.add(str((nodes / entry.name).resolve()))
                except OSError:
                    continue
        return workspaces

    def _requested_search_paths(
        self, declarations: Sequence[Mapping[str, Any]]
    ) -> Tuple[Path, ...]:
        paths = {
            _search_directory(Path(value).expanduser())
            for value in self.search_path_args
        }
        paths.update(Path(declaration["root"]).parent for declaration in declarations)
        return tuple(sorted(paths, key=str))

    @staticmethod
    def _candidates(search_paths: Sequence[Path]) -> Tuple[Path, ...]:
        candidates: Set[Path] = set()
        for search in search_paths:
            candidates.add(search)
            try:
                children = list(os.scandir(search))
            except OSError as exc:
                raise ProtocolRefusal(
                    "survey_search_unavailable", "survey search directory is unreadable"
                ) from exc
            for child in children:
                try:
                    if child.is_dir(follow_symlinks=False):
                        candidates.add((search / child.name).resolve())
                except OSError:
                    continue
        return tuple(sorted(candidates, key=str))

    def _hook_inputs(self) -> Tuple[Optional[Path], Set[str], List[Tuple[Path, Set[str]]]]:
        hooks_path = None
        hook_strings: Set[str] = set()
        if self.hooks_path_arg is not None:
            hooks_path = Path(self.hooks_path_arg).expanduser()
            hooks_payload = _absolute_regular_json(hooks_path, "survey_hooks")
            hook_strings = set(_all_strings(hooks_payload))
            hooks_path = hooks_path.resolve()
        targets: List[Tuple[Path, Set[str]]] = []
        for value in self.targets_path_args:
            path = Path(value).expanduser()
            payload = _absolute_regular_json(path, "survey_targets")
            targets.append((path.resolve(), set(_all_strings(payload))))
        return hooks_path, hook_strings, targets

    @staticmethod
    def _bindings(
        root: Path,
        our_workspaces: Set[str],
        hooks_path: Optional[Path],
        hook_strings: Set[str],
        targets: Sequence[Tuple[Path, Set[str]]],
    ) -> List[Dict[str, str]]:
        mentioned = False
        for value in hook_strings:
            try:
                tokens = shlex.split(value)
            except ValueError:
                tokens = [value]
            for token in tokens:
                candidate = Path(token).expanduser()
                if not candidate.is_absolute():
                    continue
                canonical = candidate.resolve(strict=False)
                if canonical == root or root in canonical.parents:
                    mentioned = True
                    break
            if mentioned:
                break
        if hooks_path is None or not mentioned:
            return []
        bindings = []
        for targets_path, strings in targets:
            for workspace in sorted(our_workspaces & strings):
                bindings.append(
                    {
                        "hooks_file": str(hooks_path),
                        "targets_file": str(targets_path),
                        "workspace": workspace,
                    }
                )
        return bindings

    def run(self) -> Dict[str, Any]:
        declarations = self.declared_roots.load()
        declared_paths = {Path(declaration["root"]) for declaration in declarations}
        search_paths = self._requested_search_paths(declarations)
        workspaces = self._our_workspaces(declarations)
        hooks_path, hook_strings, targets = self._hook_inputs()
        foreign = []
        for candidate in self._candidates(search_paths):
            if candidate in declared_paths:
                continue
            apparent = _shape(candidate)
            if apparent is None:
                continue
            foreign.append(
                {
                    "root": str(candidate),
                    "apparent_schema": apparent,
                    "foreign_waiter_bindings": self._bindings(
                        candidate,
                        workspaces,
                        hooks_path,
                        hook_strings,
                        targets,
                    ),
                }
            )
        return {
            "schema_version": 0,
            "invocation": "explicit_user_request",
            "search_paths": [str(path) for path in search_paths],
            "declared_roots": [str(path) for path in sorted(declared_paths, key=str)],
            "foreign_buses": foreign,
            "notice": _NOTICE,
        }
