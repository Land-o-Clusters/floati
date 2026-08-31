"""Explicit, tenant-scoped filesystem roots with opt-in read observation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Literal, Mapping, Optional, Tuple, Union

from .errors import ProtocolRefusal


IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_AUTHORITY_TOKEN = object()
CommandRootSource = Literal["explicit", "environment"]


def validate_identifier(value: Optional[str], field: str = "tenant") -> str:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise ProtocolRefusal(f"{field}_invalid", f"{field} must match {IDENTIFIER_PATTERN.pattern}")
    return value


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
        tenant_home.mkdir(parents=True, exist_ok=True)
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
        home = candidate.resolve()
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
                home.mkdir(parents=True, exist_ok=True)
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
