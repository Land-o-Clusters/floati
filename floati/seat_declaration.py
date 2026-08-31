"""Durable fleet governance and visible workspace seat declarations."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .root import FloatiRoot, validate_identifier


TOPOLOGIES = ("star", "mesh")
COORDINATOR_AUTHORITIES = (
    "dispatch_bounded_work",
    "gate_results_before_merge",
    "decide_non_owner_tier_questions",
)
OWNER_TIERS = ("publishing", "credentials", "key_custody")
SEAT_MARKER = "SEAT.json"
GOVERNANCE_RELATIVE = Path("registry/governance.json")

_GOVERNANCE_FIELDS = {
    "schema_version",
    "tenant_id",
    "root",
    "topology",
    "coordinator",
    "coordinator_authority",
    "owner_tier",
}
_SEAT_FIELDS = _GOVERNANCE_FIELDS | {"node_id"}
_MAX_DECLARATION_BYTES = 16 * 1024


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _directory_identity(descriptor: int) -> Tuple[int, int]:
    identity = os.fstat(descriptor)
    if not stat.S_ISDIR(identity.st_mode):
        raise OSError("workspace coordinate is not a directory")
    return identity.st_dev, identity.st_ino


def _matching_directory(
    identity: Optional[os.stat_result], expected: Tuple[int, int]
) -> bool:
    return bool(
        identity is not None
        and stat.S_ISDIR(identity.st_mode)
        and (identity.st_dev, identity.st_ino) == expected
    )


def _stat_at(descriptor: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_or_create_directory(
    parent_descriptor: int, name: str
) -> Tuple[int, bool]:
    created = False
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    try:
        _directory_identity(descriptor)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, created


class WorkspaceBinding:
    """One descriptor-pinned root/nodes/workspace authorization chain."""

    def __init__(
        self,
        root: FloatiRoot,
        node_id: str,
        root_descriptor: int,
        nodes_descriptor: int,
        workspace_descriptor: int,
        *,
        created_nodes: bool,
        created_workspace: bool,
    ) -> None:
        try:
            fcntl.flock(
                workspace_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except (BlockingIOError, OSError) as exc:
            raise OSError("node workspace is concurrently bound") from exc
        self.root = root
        self.node_id = node_id
        self.path = root.path / "nodes" / node_id
        self.root_descriptor = root_descriptor
        self.nodes_descriptor = nodes_descriptor
        self.workspace_descriptor = workspace_descriptor
        self.root_identity = _directory_identity(root_descriptor)
        self.nodes_identity = _directory_identity(nodes_descriptor)
        self.workspace_identity = _directory_identity(workspace_descriptor)
        self.created_nodes = created_nodes
        self.created_workspace = created_workspace
        self._closed = False

    @classmethod
    def prepare(cls, root: FloatiRoot, node_id: str) -> "WorkspaceBinding":
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal(
                "node_workspace_invalid", "workspace requires a validated root"
            )
        node = validate_identifier(node_id, "node")
        root_descriptor: Optional[int] = None
        nodes_descriptor: Optional[int] = None
        workspace_descriptor: Optional[int] = None
        created_nodes = False
        created_workspace = False
        try:
            root_descriptor = os.open(root.path, _directory_flags())
            root_identity = _directory_identity(root_descriptor)
            root_entry = os.stat(root.path, follow_symlinks=False)
            if not _matching_directory(root_entry, root_identity):
                raise OSError("validated root identity changed")
            nodes_descriptor, created_nodes = _open_or_create_directory(
                root_descriptor, "nodes"
            )
            workspace_descriptor, created_workspace = _open_or_create_directory(
                nodes_descriptor, node
            )
            binding = cls(
                root,
                node,
                root_descriptor,
                nodes_descriptor,
                workspace_descriptor,
                created_nodes=created_nodes,
                created_workspace=created_workspace,
            )
            binding.verify()
            return binding
        except (OSError, ProtocolRefusal, ValueError) as exc:
            if workspace_descriptor is not None:
                os.close(workspace_descriptor)
            if nodes_descriptor is not None:
                os.close(nodes_descriptor)
            if root_descriptor is not None:
                os.close(root_descriptor)
            refusal = ProtocolRefusal(
                "node_workspace_invalid",
                "node workspace could not be safely bound inside the validated root",
            )
            raise refusal from exc

    def verify(self) -> None:
        try:
            root_entry = os.stat(self.root.path, follow_symlinks=False)
            nodes_entry = _stat_at(self.root_descriptor, "nodes")
            workspace_entry = _stat_at(self.nodes_descriptor, self.node_id)
        except OSError as exc:
            raise ProtocolRefusal(
                "node_workspace_invalid", "node workspace identity changed"
            ) from exc
        if not (
            _matching_directory(root_entry, self.root_identity)
            and _matching_directory(nodes_entry, self.nodes_identity)
            and _matching_directory(workspace_entry, self.workspace_identity)
        ):
            raise ProtocolRefusal(
                "node_workspace_invalid", "node workspace identity changed"
            )

    def remove_owned_marker(self, ownership: Optional["MarkerOwnership"]) -> None:
        """Observe the cleanup candidate but conservatively retain its name.

        Python 3.9 exposes no atomic compare-and-unlink primitive.  The inode
        may change after observation, so rollback retains even a marker this
        operation created rather than risk deleting a foreign replacement.
        """

        if ownership is None:
            return
        try:
            _stat_at(self.workspace_descriptor, SEAT_MARKER)
        except OSError:
            pass

    def rollback_created_directories(self) -> None:
        """Observe created coordinates but retain them after a failed operation.

        Python 3.9 exposes no atomic compare-and-rmdir primitive.  A successful
        identity observation cannot authorize a later destructive name lookup.
        """

        if self.created_workspace:
            try:
                _stat_at(self.nodes_descriptor, self.node_id)
            except OSError:
                pass
        if self.created_nodes:
            try:
                _stat_at(self.root_descriptor, "nodes")
            except OSError:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self.workspace_descriptor)
        os.close(self.nodes_descriptor)
        os.close(self.root_descriptor)


@dataclass(frozen=True)
class MarkerOwnership:
    device: int
    inode: int


@dataclass(frozen=True)
class SeatPublication:
    declaration: "SeatDeclaration"
    ownership: Optional[MarkerOwnership]

    @property
    def created(self) -> bool:
        return self.ownership is not None


def _validate_vocabulary(
    values: object,
    *,
    field: str,
    vocabulary: Sequence[str],
) -> Tuple[str, ...]:
    if (
        not isinstance(values, (list, tuple))
        or not values
        or any(not isinstance(value, str) for value in values)
    ):
        raise ValueError(f"{field} must be a non-empty list")
    normalized = tuple(values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} values must be unique")
    if any(value not in vocabulary for value in normalized):
        raise ValueError(f"{field} contains an unsupported value")
    return normalized


def validate_governance_options(
    topology: Optional[str],
    coordinator: Optional[str],
    coordinator_authority: Optional[Sequence[str]],
    owner_tier: Optional[Sequence[str]],
) -> Optional[Tuple[str, str, Tuple[str, ...], Tuple[str, ...]]]:
    """Validate the optional init group completely before root creation."""

    group = (topology, coordinator, coordinator_authority, owner_tier)
    if all(value is None for value in group):
        return None
    if any(value is None for value in group):
        raise ProtocolRefusal(
            "arguments_invalid",
            "governance requires topology, coordinator, coordinator authority, and owner tier together",
        )
    try:
        if topology not in TOPOLOGIES:
            raise ValueError("topology is unsupported")
        selected_coordinator = validate_identifier(coordinator, "coordinator")
        authorities = _validate_vocabulary(
            coordinator_authority,
            field="coordinator_authority",
            vocabulary=COORDINATOR_AUTHORITIES,
        )
        owner_tiers = _validate_vocabulary(
            owner_tier,
            field="owner_tier",
            vocabulary=OWNER_TIERS,
        )
    except (ProtocolRefusal, ValueError) as exc:
        raise ProtocolRefusal(
            "arguments_invalid", "governance options contain an unsupported value"
        ) from exc
    return topology, selected_coordinator, authorities, owner_tiers


def _read_json(path: Path, *, fields: set[str], label: str) -> Dict[str, Any]:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise IntegrityFailure(f"{label}_invalid", f"{label} must be a regular file")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise IntegrityFailure(f"{label}_invalid", f"{label} could not be read") from exc
    try:
        raw = os.read(descriptor, _MAX_DECLARATION_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_DECLARATION_BYTES:
        raise IntegrityFailure(f"{label}_invalid", f"{label} exceeds its size limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityFailure(f"{label}_invalid", f"{label} is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != fields:
        raise IntegrityFailure(f"{label}_invalid", f"{label} has an unexpected shape")
    return value


def _read_json_at(
    directory_descriptor: int,
    name: str,
    *,
    fields: set[str],
    label: str,
) -> Dict[str, Any]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise IntegrityFailure(f"{label}_invalid", f"{label} could not be read") from exc
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            raise IntegrityFailure(f"{label}_invalid", f"{label} must be a regular file")
        raw = os.read(descriptor, _MAX_DECLARATION_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_DECLARATION_BYTES:
        raise IntegrityFailure(f"{label}_invalid", f"{label} exceeds its size limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityFailure(f"{label}_invalid", f"{label} is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != fields:
        raise IntegrityFailure(f"{label}_invalid", f"{label} has an unexpected shape")
    return value


def _write_new_json_at(
    directory_descriptor: int,
    name: str,
    value: Dict[str, Any],
    *,
    label: str,
) -> MarkerOwnership:
    encoded = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temporary: Optional[str] = None
    try:
        for _attempt in range(32):
            candidate = f".{name}.{secrets.token_hex(8)}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
            temporary = candidate
            break
        else:
            raise OSError("workspace declaration temporary names are exhausted")
        temporary_identity: Optional[Tuple[int, int]] = None
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("short declaration write")
            os.fsync(descriptor)
            identity = os.fstat(descriptor)
            if not stat.S_ISREG(identity.st_mode):
                raise OSError("workspace declaration temporary is not a regular file")
            temporary_identity = (identity.st_dev, identity.st_ino)
        finally:
            os.close(descriptor)
        os.link(
            temporary,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=directory_descriptor)
        temporary = None
        os.fsync(directory_descriptor)
        assert temporary_identity is not None
        return MarkerOwnership(*temporary_identity)
    except FileExistsError:
        raise
    except OSError as exc:
        raise DurabilityFailure(
            f"{label}_unavailable", f"{label} could not be committed"
        ) from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=directory_descriptor)
            except OSError:
                pass


def _write_new_json(path: Path, value: Dict[str, Any], *, label: str) -> None:
    encoded = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    created_parent = not path.parent.exists()
    temporary: Optional[Path] = None
    try:
        if path.parent.is_symlink() or (
            path.parent.exists() and not path.parent.is_dir()
        ):
            raise OSError("parent is not a directory")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("short declaration write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        temporary = None
        parent_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except FileExistsError:
        raise
    except OSError as exc:
        raise DurabilityFailure(
            f"{label}_unavailable", f"{label} could not be committed"
        ) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if created_parent:
            try:
                path.parent.rmdir()
            except OSError:
                pass


def _validate_common(value: Dict[str, Any], *, label: str) -> Dict[str, Any]:
    if value.get("schema_version") != 1 or isinstance(
        value.get("schema_version"), bool
    ):
        raise IntegrityFailure(f"{label}_invalid", f"{label} schema version is unsupported")
    try:
        tenant = validate_identifier(value.get("tenant_id"), "tenant")
        coordinator = validate_identifier(value.get("coordinator"), "coordinator")
        topology = value.get("topology")
        if topology not in TOPOLOGIES:
            raise ValueError("topology is unsupported")
        authorities = _validate_vocabulary(
            value.get("coordinator_authority"),
            field="coordinator_authority",
            vocabulary=COORDINATOR_AUTHORITIES,
        )
        owner_tiers = _validate_vocabulary(
            value.get("owner_tier"), field="owner_tier", vocabulary=OWNER_TIERS
        )
        root = value.get("root")
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise ValueError("root is not absolute")
    except (ProtocolRefusal, ValueError) as exc:
        raise IntegrityFailure(f"{label}_invalid", f"{label} fields are invalid") from exc
    return {
        "schema_version": 1,
        "tenant_id": tenant,
        "root": root,
        "topology": topology,
        "coordinator": coordinator,
        "coordinator_authority": authorities,
        "owner_tier": owner_tiers,
    }


@dataclass(frozen=True)
class FleetGovernance:
    schema_version: int
    tenant_id: str
    root: str
    topology: str
    coordinator: str
    coordinator_authority: Tuple[str, ...]
    owner_tier: Tuple[str, ...]

    @classmethod
    def create(
        cls,
        root: FloatiRoot,
        *,
        topology: str,
        coordinator: str,
        coordinator_authority: Sequence[str],
        owner_tier: Sequence[str],
    ) -> "FleetGovernance":
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal(
                "governance_root_invalid", "fleet governance requires a validated root"
            )
        selected = validate_governance_options(
            topology, coordinator, coordinator_authority, owner_tier
        )
        assert selected is not None
        declaration = cls(
            1,
            root.tenant_id,
            str(root.path),
            selected[0],
            selected[1],
            selected[2],
            selected[3],
        )
        path = root.resolve_relative(GOVERNANCE_RELATIVE)
        try:
            _write_new_json(path, declaration.artifact(), label="fleet_governance")
        except FileExistsError:
            existing = cls.load(root)
            if existing != declaration:
                raise ProtocolRefusal(
                    "fleet_governance_mismatch",
                    "existing fleet governance does not match the init request",
                )
        return declaration

    @classmethod
    def load(cls, root: FloatiRoot) -> Optional["FleetGovernance"]:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal(
                "governance_root_invalid", "fleet governance requires a validated root"
            )
        path = root.resolve_relative(GOVERNANCE_RELATIVE)
        if not path.exists() and not path.is_symlink():
            return None
        value = _validate_common(
            _read_json(path, fields=_GOVERNANCE_FIELDS, label="fleet_governance"),
            label="fleet_governance",
        )
        if value["tenant_id"] != root.tenant_id or value["root"] != str(root.path):
            raise IntegrityFailure(
                "fleet_governance_invalid",
                "fleet governance does not match its validated root",
            )
        return cls(**value)

    def artifact(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "root": self.root,
            "topology": self.topology,
            "coordinator": self.coordinator,
            "coordinator_authority": list(self.coordinator_authority),
            "owner_tier": list(self.owner_tier),
        }


@dataclass(frozen=True)
class SeatDeclaration:
    schema_version: int
    tenant_id: str
    root: str
    node_id: str
    topology: str
    coordinator: str
    coordinator_authority: Tuple[str, ...]
    owner_tier: Tuple[str, ...]

    @classmethod
    def create(
        cls,
        workspace: Path | WorkspaceBinding,
        node_id: str,
        root: FloatiRoot,
        governance: FleetGovernance,
    ) -> SeatPublication:
        if not isinstance(root, FloatiRoot) or not isinstance(
            governance, FleetGovernance
        ):
            raise ProtocolRefusal(
                "workspace_declaration_invalid",
                "workspace declaration requires validated root governance",
            )
        if governance.tenant_id != root.tenant_id or governance.root != str(root.path):
            raise ProtocolRefusal(
                "fleet_governance_mismatch",
                "fleet governance does not match the validated root",
            )
        node = validate_identifier(node_id, "node")
        expected = root.path / "nodes" / node
        owns_binding = not isinstance(workspace, WorkspaceBinding)
        if owns_binding:
            path = Path(workspace)
            if path != expected:
                raise ProtocolRefusal(
                    "node_workspace_invalid",
                    "workspace declaration must use the node's exact root coordinate",
                )
            binding = WorkspaceBinding.prepare(root, node)
        else:
            binding = workspace
            path = binding.path
        if (
            binding.root is not root
            or binding.node_id != node
            or path != expected
        ):
            if owns_binding:
                binding.close()
            raise ProtocolRefusal(
                "node_workspace_invalid",
                "workspace declaration must be written to the node's contained workspace",
            )
        declaration = cls(
            1,
            root.tenant_id,
            str(root.path),
            node,
            governance.topology,
            governance.coordinator,
            governance.coordinator_authority,
            governance.owner_tier,
        )
        ownership: Optional[MarkerOwnership] = None
        try:
            try:
                binding.verify()
                ownership = _write_new_json_at(
                    binding.workspace_descriptor,
                    SEAT_MARKER,
                    declaration.artifact(),
                    label="workspace_declaration",
                )
            except FileExistsError:
                existing = cls._load_bound(binding)
                if existing != declaration:
                    raise ProtocolRefusal(
                        "workspace_identity_mismatch",
                        "existing workspace declaration disagrees with the requested seat",
                    )
            if ownership is not None:
                marker_identity = _stat_at(binding.workspace_descriptor, SEAT_MARKER)
                if (
                    marker_identity is None
                    or (marker_identity.st_dev, marker_identity.st_ino)
                    != (ownership.device, ownership.inode)
                ):
                    existing = cls._load_bound(binding)
                    if existing != declaration:
                        raise ProtocolRefusal(
                            "workspace_identity_mismatch",
                            "workspace declaration changed during publication",
                        )
                    ownership = None
            try:
                binding.verify()
                return SeatPublication(declaration, ownership)
            except Exception:
                binding.remove_owned_marker(ownership)
                raise
        finally:
            if owns_binding:
                binding.close()

    @classmethod
    def _load_bound(cls, binding: WorkspaceBinding) -> Optional["SeatDeclaration"]:
        identity = _stat_at(binding.workspace_descriptor, SEAT_MARKER)
        if identity is None:
            return None
        raw = _read_json_at(
            binding.workspace_descriptor,
            SEAT_MARKER,
            fields=_SEAT_FIELDS,
            label="workspace_declaration",
        )
        return cls._from_raw(raw)

    @classmethod
    def load(cls, workspace: Path) -> Optional["SeatDeclaration"]:
        marker = Path(workspace) / SEAT_MARKER
        if not marker.exists() and not marker.is_symlink():
            return None
        raw = _read_json(marker, fields=_SEAT_FIELDS, label="workspace_declaration")
        return cls._from_raw(raw)

    @classmethod
    def _from_raw(cls, raw: Dict[str, Any]) -> "SeatDeclaration":
        value = _validate_common(raw, label="workspace_declaration")
        try:
            node = validate_identifier(raw.get("node_id"), "node")
        except ProtocolRefusal as exc:
            raise IntegrityFailure(
                "workspace_declaration_invalid",
                "workspace declaration node is invalid",
            ) from exc
        return cls(node_id=node, **value)

    def artifact(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "root": self.root,
            "node_id": self.node_id,
            "topology": self.topology,
            "coordinator": self.coordinator,
            "coordinator_authority": list(self.coordinator_authority),
            "owner_tier": list(self.owner_tier),
        }


def require_declared_coordinate(
    workspace: Path, node_id: str, root: FloatiRoot
) -> Dict[str, str]:
    """Refuse a present marker mismatch before any inbox ledger is opened."""

    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal(
            "workspace_identity_mismatch", "workspace identity requires a validated root"
        )
    node = validate_identifier(node_id, "node")
    declaration = SeatDeclaration.load(Path(workspace))
    testimony = {"used_node": node, "used_root": str(root.path)}
    if declaration is None:
        return {"workspace_identity": "absent", **testimony}
    if (
        declaration.node_id != node
        or declaration.root != str(root.path)
        or declaration.tenant_id != root.tenant_id
    ):
        raise ProtocolRefusal(
            "workspace_identity_mismatch",
            "workspace declaration disagrees with the requested node/root",
        )
    return {"workspace_identity": "declared", **testimony}
