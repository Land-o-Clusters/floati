"""Explicit, tenant-scoped filesystem roots with opt-in read observation."""

from __future__ import annotations

import os
import re
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Literal, Mapping, Optional, Tuple, Union

from .errors import ProtocolRefusal
from .session_identity import IDENTIFIER_PATTERN, validate_identifier


_AUTHORITY_TOKEN = object()
# NET-HOLD-INBOUND-1: every root this process opens, by tenant, so the display
# gate can read the inbound holds of whichever root an envelope came from.
# Bounded by the number of distinct roots the process opens.
_OPENED_ROOTS: dict = {}


def opened_roots(tenant_id: str) -> list:
    """The roots this process has opened for one tenant that still exist."""
    homes = _OPENED_ROOTS.get(tenant_id, {})
    for home in [home for home in list(homes) if not os.path.isdir(home)]:
        homes.pop(home, None)
    return list(homes.values())
CommandRootSource = Literal["explicit", "environment"]


def resolve_command_root(
    explicit_root: Optional[Union[Path, str]],
    *,
    create: bool = False,
    environ: Optional[Mapping[str, str]] = None,
) -> "FloatiRoot":
    """Resolve the only v0 command-root precedence without ambient config."""

    root, _source = resolve_command_root_with_source(
        explicit_root,
        create=create,
        environ=environ,
    )
    return root


def resolve_command_root_with_source(
    explicit_root: Optional[Union[Path, str]],
    *,
    create: bool = False,
    environ: Optional[Mapping[str, str]] = None,
) -> Tuple["FloatiRoot", CommandRootSource]:
    """Resolve one command root together with its selected input source."""

    if explicit_root is not None:
        selected: Optional[Union[Path, str]] = explicit_root
        root_source: CommandRootSource = "explicit"
    else:
        source = os.environ if environ is None else environ
        selected = source.get("FLOATI_BUS_ROOT")
        root_source = "environment"
    if selected is None or (isinstance(selected, str) and not selected):
        raise ProtocolRefusal(
            "cannot_speak",
            "no command root was resolved from --root or FLOATI_BUS_ROOT",
        )
    return FloatiRoot.open_direct_home(selected, create=create), root_source


@dataclass(frozen=True, init=False)
class ObservationCapability:
    """An unforgeable grant created by a writable root."""

    _root_path: Path
    _tenant_ids: FrozenSet[str]

    def __init__(self, token: object, root_path: Path, tenant_ids: FrozenSet[str]) -> None:
        if token is not _AUTHORITY_TOKEN:
            raise TypeError("observation capabilities are created by FloatiRoot")
        object.__setattr__(self, "_root_path", root_path)
        object.__setattr__(self, "_tenant_ids", tenant_ids)


@dataclass(frozen=True, init=False)
class TenantObservation:
    """Opaque read authority for one tenant; it exposes no writable path."""

    tenant_id: str
    _root_path: Path

    def __init__(self, token: object, root_path: Path, tenant_id: str) -> None:
        if token is not _AUTHORITY_TOKEN:
            raise TypeError("tenant observations are created by FloatiRoot")
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "_root_path", root_path)

    def _resolve_relative(self, relative: Union[Path, str]) -> Path:
        return _resolve_contained(self._root_path / "tenants" / self.tenant_id, relative)


@dataclass(frozen=True, init=False)
class FloatiRoot:
    """A validated absolute root and exactly one writable tenant."""

    path: Path
    tenant_id: str
    tenant_home: Path

    def __init__(self, token: object, path: Path, tenant_id: str, tenant_home: Path) -> None:
        if token is not _AUTHORITY_TOKEN:
            raise TypeError("roots are created by FloatiRoot.open")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "tenant_home", tenant_home)
        _OPENED_ROOTS.setdefault(tenant_id, {})[str(tenant_home)] = self

    @classmethod
    def open(cls, path: Optional[Union[Path, str]], tenant_id: Optional[str]) -> "FloatiRoot":
        if path is None:
            raise ProtocolRefusal("root_required", "an explicit root path is required")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ProtocolRefusal("root_not_absolute", "the root path must be absolute")
        if candidate.is_symlink():
            raise ProtocolRefusal(
                "root_symlinked_entry",
                "the invoked root path must not be a symlink",
            )
        tenant = validate_identifier(tenant_id)
        resolved = candidate.resolve()
        namespace_layout = resolved / "tenants"
        if namespace_layout.is_symlink():
            raise ProtocolRefusal(
                "namespace_symlinked_entry",
                "the tenant namespace must not be a symlink",
            )
        tenant_home = namespace_layout / tenant
        if tenant_home.is_symlink():
            raise ProtocolRefusal(
                "tenant_symlinked_entry",
                "the selected tenant home must not be a symlink",
            )
        tenant_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        return cls(_AUTHORITY_TOKEN, resolved, tenant, tenant_home)

    @classmethod
    def open_direct_home(cls, path: Optional[Union[Path, str]], *, create: bool = False) -> "FloatiRoot":
        if path is None:
            raise ProtocolRefusal("root_required", "an explicit root path is required")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ProtocolRefusal("root_not_absolute", "the root path must be absolute")
        if candidate.is_symlink():
            raise ProtocolRefusal(
                "direct_home_symlinked_entry",
                "the invoked direct home must not be a symlink",
            )
        try:
            home = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise ProtocolRefusal(
                "root_unavailable",
                "the direct home path could not be resolved",
            ) from exc
        tenant = validate_identifier(home.name, "direct_home_tenant")
        if create and home.exists() and not home.is_dir():
            raise ProtocolRefusal(
                "direct_home_not_directory",
                "the direct home path exists and is not a directory",
            )
        namespace_layout = home / "tenants"
        if namespace_layout.exists() or namespace_layout.is_symlink():
            raise ProtocolRefusal("namespace_root_layout_present", "direct home cannot contain a tenants namespace")
        if create:
            try:
                home.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                raise ProtocolRefusal(
                    "root_unavailable",
                    "the direct home could not be created",
                ) from exc
        elif not home.is_dir():
            raise ProtocolRefusal("direct_home_missing", "the direct home must be an existing directory")
        return cls(_AUTHORITY_TOKEN, home, tenant, home)

    def resolve_relative(self, relative: Union[Path, str]) -> Path:
        return _resolve_contained(self.tenant_home, relative)

    @contextmanager
    def bind_adhoc_coordinates(self, coordinates, *, files=()):
        """Pin derived ad-hoc coordinates, rejecting replacement of every path edge.

        Callers derive these coordinates from registry and lane receipts, never
        dispatch-supplied paths. Descriptors remain open through the operation.
        """
        if coordinates.get("root") != self.tenant_home:
            raise ProtocolRefusal("adhoc_binding_changed", "ad-hoc coordinates require this exact tenant root")
        descriptors, identities, edges = {}, {}, []
        with ExitStack() as stack:
            try:
                for name, supplied in coordinates.items():
                    path = Path(supplied)
                    if not path.is_absolute() or path.resolve(strict=True) != path:
                        raise OSError("noncanonical coordinate")
                    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    stack.callback(os.close, parent)
                    for index, part in enumerate(path.parts[1:]):
                        directory = index != len(path.parts[1:]) - 1 or name not in files
                        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                        if directory:
                            flags |= os.O_DIRECTORY
                        child = os.open(part, flags, dir_fd=parent)
                        stack.callback(os.close, child)
                        edges.append((parent, part, child))
                        parent = child
                    metadata = os.fstat(parent)
                    ordinary = stat.S_ISREG(metadata.st_mode) if name in files else stat.S_ISDIR(metadata.st_mode)
                    if not ordinary or metadata.st_uid != os.geteuid() or (name in files and metadata.st_nlink != 1):
                        raise OSError("foreign or nonordinary coordinate")
                    descriptors[name] = parent
                    identities[name] = {"dev": metadata.st_dev, "ino": metadata.st_ino}

                def revalidate():
                    for parent, part, child in edges:
                        named = os.stat(part, dir_fd=parent, follow_symlinks=False)
                        opened = os.fstat(child)
                        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino) or stat.S_ISLNK(named.st_mode):
                            raise OSError("descriptor path changed")

                revalidate()
            except (OSError, ValueError, RuntimeError) as exc:
                raise ProtocolRefusal("adhoc_binding_changed", "a derived ad-hoc filesystem coordinate could not be pinned") from exc
            try:
                yield identities, descriptors
                revalidate()
            except OSError as exc:
                raise ProtocolRefusal("adhoc_binding_changed", "a pinned ad-hoc filesystem coordinate changed") from exc

    def grant_observation(self, *tenant_ids: str) -> ObservationCapability:
        tenants = frozenset(validate_identifier(value) for value in tenant_ids)
        return ObservationCapability(_AUTHORITY_TOKEN, self.path, tenants)

    def observe_tenant(self, capability: ObservationCapability, tenant_id: str) -> TenantObservation:
        tenant = validate_identifier(tenant_id)
        if not isinstance(capability, ObservationCapability) or capability._root_path != self.path:
            raise ProtocolRefusal("observation_not_granted", "observation grant belongs to another root")
        if tenant not in capability._tenant_ids:
            raise ProtocolRefusal("observation_not_granted", f"cross-tenant observation is not granted for {tenant}")
        tenant_home = self.path / "tenants" / tenant
        if not tenant_home.is_dir():
            raise ProtocolRefusal("observation_missing", f"tenant {tenant} does not exist")
        return TenantObservation(_AUTHORITY_TOKEN, self.path, tenant)


def _resolve_contained(tenant_home: Path, relative: Union[Path, str]) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
        raise ProtocolRefusal("path_not_contained", "path must be a contained tenant-relative path")
    resolved_home = tenant_home.resolve()
    resolved = (resolved_home / candidate).resolve(strict=False)
    try:
        resolved.relative_to(resolved_home)
    except ValueError as exc:
        raise ProtocolRefusal("path_not_contained", "path escapes the selected tenant") from exc
    return resolved
