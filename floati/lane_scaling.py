"""Numbered shipped-role instances with atomic spawn and symmetric teardown."""

from __future__ import annotations

import json
import re
import shutil
import string
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .admin_registry import RegistryAdminBackend
from .cursor import SparseCursor
from .errors import ProtocolRefusal
from .events import EventLog
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact, transact_records
from .locks.cleanup import CleanupInspector
from .records import validate_record, validate_role
from .registry import REGISTRY_KINDS, Registry
from .role_templates import RoleTemplate, load_shipped_role_templates
from .root import FloatiRoot, validate_identifier
from .work import WorkLog


_PROFILE_FIELDS = frozenset(
    {
        "schema_version", "profile_version", "name", "role_template",
        "workspace_recipe", "harness", "lifetime", "lease_minutes",
        "role_answers", "boot_prompt_template",
    }
)
_INSTANCE = re.compile(r"^([a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?)-([1-9][0-9]{0,5})$")
_PROMPT_FIELDS = frozenset(
    {
        "profile", "instance", "workspace", "architect", "root", "harness",
        "role", "committer_name", "committer_email",
    }
)
_SCALING_LABEL = "lane_scaling".replace("_", "-")


def _record_id(kind: str) -> str:
    return kind.replace("_", "-") + "-" + uuid7_hex()


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal(code, detail)


def _safe_text(value: object, field: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        _refuse("lane_profile_invalid", f"{field} must be bounded text")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character)
        in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        for character in value
    ):
        _refuse("lane_profile_invalid", f"{field} is terminal-unsafe")
    return value


def _timestamp(value: Optional[datetime] = None) -> str:
    current = datetime.now(timezone.utc) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        _refuse("time_invalid", f"{_SCALING_LABEL} clock must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class RoleProfile:
    name: str
    role_template: str
    workspace_recipe: str
    harness: str
    lifetime: str
    lease_minutes: Optional[int]
    role_answers: Mapping[str, str]
    boot_prompt_template: str


def _profile(value: object, path: Path) -> RoleProfile:
    if not isinstance(value, dict) or frozenset(value) != _PROFILE_FIELDS:
        _refuse("lane_profile_invalid", "role profile fields do not match the exact contract")
    if value["schema_version"] != 0 or value["profile_version"] != 1:
        _refuse("lane_profile_invalid", "role profile versions are unsupported")
    name = validate_identifier(value["name"], "profile")
    if name[-1].isdigit():
        _refuse(
            "lane_profile_name_digit_suffix",
            f"profile {name!r} may not end in a digit",
        )
    if path.stem != name:
        _refuse("lane_profile_invalid", "profile filename must equal its declared name")
    role_template = validate_identifier(value["role_template"], "role_template")
    workspace_recipe = _safe_text(value["workspace_recipe"], "workspace_recipe", maximum=128)
    if workspace_recipe != "nodes/{instance}":
        _refuse(
            "lane_profile_workspace_invalid",
            "workspace recipe must be the ruled nodes/{instance} coordinate",
        )
    harness = validate_role(value["harness"])
    lifetime = value["lifetime"]
    lease_minutes = value["lease_minutes"]
    if lifetime == "permanent":
        if lease_minutes is not None:
            _refuse("lane_profile_lifetime_invalid", "permanent profile cannot carry a lease")
    elif lifetime == "leased":
        if (
            not isinstance(lease_minutes, int)
            or isinstance(lease_minutes, bool)
            or not 1 <= lease_minutes <= 10080
        ):
            _refuse("lane_profile_lifetime_invalid", "leased profile needs 1 to 10080 minutes")
    else:
        _refuse("lane_profile_lifetime_invalid", "profile lifetime must be permanent or leased")
    raw_answers = value["role_answers"]
    if not isinstance(raw_answers, dict) or not 1 <= len(raw_answers) <= 32:
        _refuse("lane_profile_invalid", "role_answers must be a bounded object")
    answers: Dict[str, str] = {}
    for key, answer in raw_answers.items():
        answers[validate_identifier(key, "role_answer_key")] = _safe_text(
            answer, "role_answer", maximum=500
        )
    prompt = _safe_text(value["boot_prompt_template"], "boot_prompt_template")
    try:
        parsed = tuple(string.Formatter().parse(prompt))
    except ValueError as exc:
        raise ProtocolRefusal("lane_profile_copy_invalid", "boot prompt template is malformed") from exc
    if any(
        field_name is not None and (format_spec or conversion is not None)
        for _literal, field_name, format_spec, conversion in parsed
    ):
        _refuse(
            "lane_profile_copy_invalid",
            "boot prompt placeholders do not accept formatting or conversion",
        )
    fields = {
        field_name
        for _literal, field_name, _format_spec, _conversion in parsed
        if field_name is not None
    }
    if any(field not in _PROMPT_FIELDS for field in fields):
        _refuse("lane_profile_copy_invalid", "boot prompt template names an unknown fact")
    return RoleProfile(
        name=name,
        role_template=role_template,
        workspace_recipe=workspace_recipe,
        harness=harness,
        lifetime=lifetime,
        lease_minutes=lease_minutes,
        role_answers=answers,
        boot_prompt_template=prompt,
    )


def load_role_profiles(directory: Path) -> Dict[str, RoleProfile]:
    if not isinstance(directory, Path) or not directory.is_absolute():
        _refuse("lane_profile_catalog_invalid", "profile directory must be absolute")
    if directory.is_symlink() or not directory.is_dir():
        _refuse("lane_profile_catalog_invalid", "profile directory is unavailable")
    profiles: Dict[str, RoleProfile] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json" or path.is_symlink() or not path.is_file():
            _refuse("lane_profile_catalog_invalid", "profile directory contains a foreign entry")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProtocolRefusal("lane_profile_invalid", "role profile is unreadable") from exc
        profile = _profile(raw, path)
        if profile.name in profiles:
            _refuse("lane_profile_catalog_invalid", "role profile name is duplicated")
        profiles[profile.name] = profile
    if not profiles:
        _refuse("lane_profile_catalog_invalid", "profile catalog is empty")
    return profiles


def render_boot_prompt(
    profile: RoleProfile,
    *,
    instance: str,
    workspace: Path,
    architect: str,
    root: FloatiRoot,
) -> str:
    if not isinstance(profile, RoleProfile):
        _refuse("lane_profile_copy_invalid", "boot prompt requires a role profile")
    if any(line.startswith("DRAFT - ") for line in profile.boot_prompt_template.splitlines()):
        _refuse("lane_profile_copy_draft", "boot prompt copy still carries a DRAFT stamp")
    node = validate_identifier(instance, "instance")
    actor = validate_identifier(architect, "architect")
    expected = root.path / "nodes" / node
    if workspace != expected or workspace.is_symlink():
        _refuse("lane_profile_workspace_invalid", "boot prompt workspace is not the derived coordinate")
    committer_email = f"{node}@{root.tenant_id}"
    try:
        prompt = profile.boot_prompt_template.format(
            profile=profile.name,
            instance=node,
            workspace=str(workspace),
            architect=actor,
            root=str(root.path),
            harness=profile.harness,
            role=profile.role_template,
            committer_name=node,
            committer_email=committer_email,
        )
        prompt.encode("ascii")
    except (KeyError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal("lane_profile_copy_invalid", "boot prompt cannot render safely") from exc
    if any(line.startswith("DRAFT - ") for line in prompt.splitlines()):
        _refuse("lane_profile_copy_draft", "rendered boot prompt still carries a DRAFT stamp")
    return prompt


class LaneScalingService:
    def __init__(
        self,
        root: FloatiRoot,
        profiles: Mapping[str, RoleProfile],
        *,
        fault_injector: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not isinstance(root, FloatiRoot):
            _refuse("lane_root_invalid", "lane scaling requires a validated root")
        if not profiles or any(name != profile.name for name, profile in profiles.items()):
            _refuse("lane_profile_catalog_invalid", "profile catalog is malformed")
        self.root = root
        self.profiles = dict(profiles)
        self.registry = Registry(root)
        self.backend = RegistryAdminBackend(root)
        self.fault_injector = fault_injector
        self.templates = load_shipped_role_templates(
            Path(__file__).parents[1] / "roles" / "shipped"
        )

    def _require_architect(self, actor: str) -> str:
        node = validate_identifier(actor, "actor")
        active = self.registry.require_active(node)
        if str(active["role"]).casefold() == "architect":
            return node
        try:
            role = self.backend.role_record(node)
        except ProtocolRefusal:
            role = {}
        if role.get("template_role") != "architect":
            _refuse(
                "lane_scaling_requires_architect",
                "lane scaling requires the active architect node",
            )
        return node

    def _selected_profile(self, name: str) -> RoleProfile:
        selected = validate_identifier(name, "profile")
        profile = self.profiles.get(selected)
        if profile is None:
            _refuse("lane_profile_unknown", f"profile {selected!r} is not shipped")
        if profile.role_template not in self.templates:
            _refuse("lane_profile_role_unknown", "profile role template is not shipped")
        return profile

    @staticmethod
    def _ordinal(value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1000000:
            _refuse("lane_ordinal_invalid", "ordinal must be an integer from 1 to 1000000")
        return value

    def _answers(self, profile: RoleProfile, architect: str, template: RoleTemplate) -> Dict[str, str]:
        expected = {question.key for question in template.questions}
        if set(profile.role_answers) != expected:
            _refuse("lane_profile_role_answers_invalid", "profile answers do not match the role template")
        return {
            key: value.replace("{architect}", architect)
            for key, value in profile.role_answers.items()
        }

    @staticmethod
    def _latest_registry(records: list[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        latest: Dict[str, Dict[str, Any]] = {}
        for record in records:
            if record.get("kind") == "registry_entry":
                latest[str(record["node_id"])] = record
        return latest

    def _failure_receipt(
        self,
        *,
        actor: str,
        profile: RoleProfile,
        ordinal: int,
        node: str,
        workspace: Path,
        step: str,
        compensated: list[str],
    ) -> Dict[str, Any]:
        return {
            "schema_version": 0,
            "id": _record_id("lane_spawn_receipt"),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(),
            "kind": "lane_spawn_receipt",
            "profile": profile.name,
            "ordinal": ordinal,
            "node_id": node,
            "actor": actor,
            "state": "spawn_incomplete",
            "failing_step": step,
            "artifacts": {
                "workspace": str(workspace),
                "registry_entry_id": "retained-none",
                "role_record_id": "retained-none",
                "lease_id": None,
                "committer_name": node,
                "committer_email": f"{node}@{self.root.tenant_id}",
            },
            "compensated": compensated,
        }

    def spawn(
        self,
        *,
        actor: str,
        profile_name: str,
        ordinal: Optional[int] = None,
    ) -> Dict[str, Any]:
        architect = self._require_architect(actor)
        profile = self._selected_profile(profile_name)
        requested = self._ordinal(ordinal)
        attempt: Dict[str, Any] = {}
        created_workspace = False
        failing_step = "registry"

        def decide(existing: list[Dict[str, Any]]):
            nonlocal created_workspace, failing_step
            latest = self._latest_registry(existing)
            active = {
                node
                for node, record in latest.items()
                if record.get("state") == "active"
            }
            occupied = set()
            for node in active:
                matched = _INSTANCE.fullmatch(node)
                if matched is not None and matched.group(1) == profile.name:
                    occupied.add(int(matched.group(2)))
            if requested is not None:
                chosen = requested
                holder = f"{profile.name}-{chosen}"
                if chosen in occupied:
                    _refuse(
                        "lane_ordinal_collision",
                        f"ordinal {chosen} is held by live node {holder}",
                    )
            else:
                chosen = 1
                while chosen in occupied:
                    chosen += 1
            node = f"{profile.name}-{chosen}"
            workspace = self.root.path / "nodes" / node
            attempt.update(ordinal=chosen, node=node, workspace=workspace)
            prompt = render_boot_prompt(
                profile,
                instance=node,
                workspace=workspace,
                architect=architect,
                root=self.root,
            )
            if workspace.exists() or workspace.is_symlink():
                _refuse("lane_workspace_collision", f"workspace already exists for {node}")
            nodes = workspace.parent
            if nodes.is_symlink() or (nodes.exists() and not nodes.is_dir()):
                _refuse("lane_workspace_invalid", "nodes coordinate is not a directory")
            nodes.mkdir(mode=0o700, exist_ok=True)
            workspace.mkdir(mode=0o700)
            created_workspace = True
            failing_step = "workspace"
            if self.fault_injector is not None:
                self.fault_injector("workspace")
            failing_step = "registry"
            if self.fault_injector is not None:
                self.fault_injector("registry")

            observed = datetime.now(timezone.utc)
            timestamp = _timestamp(observed)
            registry_id = "registry-" + uuid7_hex()
            role_id = "registry-role-" + uuid7_hex()
            registry_record: Dict[str, Any] = {
                "schema_version": 0,
                "id": registry_id,
                "tenant_id": self.root.tenant_id,
                "timestamp": timestamp,
                "kind": "registry_entry",
                "node_id": node,
                "role": profile.harness,
                "state": "active",
            }
            records = [registry_record]
            lease_id = None
            if profile.lifetime == "leased":
                assert profile.lease_minutes is not None
                lease_id = "lease-" + uuid7_hex()
                records.append(
                    {
                        "schema_version": 0,
                        "id": lease_id,
                        "tenant_id": self.root.tenant_id,
                        "timestamp": timestamp,
                        "kind": "node_lease",
                        "node_id": node,
                        "workspace": str(workspace),
                        "expires_at": _timestamp(
                            observed + timedelta(minutes=profile.lease_minutes)
                        ),
                        "state": "active",
                    }
                )
            template = self.templates[profile.role_template]
            role_record = {
                "schema_version": 0,
                "id": role_id,
                "tenant_id": self.root.tenant_id,
                "timestamp": timestamp,
                "kind": "registry_role_record",
                "node_id": node,
                "template_role": template.role,
                "template_version": template.template_version,
                "template_sha256": template.digest,
                "answers": self._answers(profile, architect, template),
                "state": "active",
                "predecessor_role_record_id": None,
            }
            records.append(role_record)
            artifacts = {
                "workspace": str(workspace),
                "registry_entry_id": registry_id,
                "role_record_id": role_id,
                "lease_id": lease_id,
                "committer_name": node,
                "committer_email": f"{node}@{self.root.tenant_id}",
            }
            receipt = {
                "schema_version": 0,
                "id": _record_id("lane_spawn_receipt"),
                "tenant_id": self.root.tenant_id,
                "timestamp": timestamp,
                "kind": "lane_spawn_receipt",
                "profile": profile.name,
                "ordinal": chosen,
                "node_id": node,
                "actor": architect,
                "state": "complete",
                "failing_step": None,
                "artifacts": artifacts,
                "compensated": [],
            }
            records.append(receipt)
            result = dict(
                receipt,
                boot_prompt=prompt,
                committer_name=node,
                committer_email=artifacts["committer_email"],
                workspace=str(workspace),
            )
            return result, records

        try:
            return transact_records(
                self.root,
                self.registry.relative_path,
                decide,
                allowed_kinds=set(REGISTRY_KINDS),
            )
        except ProtocolRefusal as exc:
            if exc.code in {"lane_ordinal_collision", "lane_profile_copy_draft", "lane_workspace_collision"}:
                raise
            original: BaseException = exc
        except BaseException as exc:
            original = exc

        compensated: list[str] = []
        workspace = attempt.get("workspace")
        if created_workspace and isinstance(workspace, Path):
            try:
                workspace.rmdir()
                compensated.append("workspace")
                try:
                    workspace.parent.rmdir()
                except OSError:
                    pass
            except OSError:
                pass
        chosen = int(attempt.get("ordinal", requested or 1))
        node = str(attempt.get("node", f"{profile.name}-{chosen}"))
        candidate_workspace = (
            workspace if isinstance(workspace, Path) else self.root.path / "nodes" / node
        )
        receipt = self._failure_receipt(
            actor=architect,
            profile=profile,
            ordinal=chosen,
            node=node,
            workspace=candidate_workspace,
            step=failing_step,
            compensated=compensated,
        )

        def record_failure(existing: list[Dict[str, Any]]):
            return receipt, receipt

        transact(
            self.root,
            self.registry.relative_path,
            record_failure,
            allowed_kinds=set(REGISTRY_KINDS),
        )
        raise ProtocolRefusal(
            "lane_spawn_incomplete",
            f"spawn incomplete at {failing_step}; compensated: {','.join(compensated) or 'none'}",
        ) from original

    def receipts(self) -> list[Dict[str, Any]]:
        return [
            record
            for record in read_records_snapshot(
                self.root,
                self.registry.relative_path,
                allowed_kinds=set(REGISTRY_KINDS),
            )
            if record["kind"] in {"lane_spawn_receipt", "lane_teardown_receipt"}
        ]

    def _instance(self, value: str) -> tuple[RoleProfile, int, str]:
        node = validate_identifier(value, "instance")
        matched = _INSTANCE.fullmatch(node)
        if matched is None:
            _refuse("lane_instance_invalid", "instance must be profile-ordinal")
        profile = self._selected_profile(matched.group(1))
        return profile, int(matched.group(2)), node

    def _drain(self, node: str) -> None:
        outstanding = [
            item
            for item in WorkLog(self.root).show()
            if item["state"] != "completed"
            and (item["owner"] == node or item["holder"] == node)
        ]
        if outstanding:
            _refuse(
                "lane_teardown_work_outstanding",
                "outstanding work: " + ",".join(str(item["id"]) for item in outstanding),
            )
        frames = EventLog(self.root).event_records()
        retracted = {
            str(record["retracted_message_id"])
            for record in frames
            if record["kind"] == "message_retracted"
        }
        acked = SparseCursor(self.root).acked_ids(node)
        unacked = [
            str(record["id"])
            for record in frames
            if record["kind"] == "message_envelope"
            and record["recipient"] == node
            and record["id"] not in acked
            and record["id"] not in retracted
        ]
        if unacked:
            _refuse("lane_teardown_mail_unacked", "unacked mail: " + ",".join(unacked))

    @staticmethod
    def _git_status(workspace: Path) -> str:
        try:
            completed = subprocess.run(
                [
                    "/usr/bin/git", "--no-optional-locks", "--no-replace-objects",
                    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                    "-C", str(workspace), "status", "--porcelain=v1",
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
                env={
                    "PATH": "/usr/bin:/bin", "HOME": "/var/empty", "LANG": "C",
                    "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
                    "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProtocolRefusal("git_observation_unavailable", "bounded Git status failed") from exc
        if completed.returncode != 0:
            _refuse("git_observation_failed", "bounded Git status returned nonzero")
        return completed.stdout

    def _require_removable(self, workspace: Path) -> bool:
        if workspace.is_symlink() or (workspace.exists() and not workspace.is_dir()):
            _refuse("lane_workspace_invalid", "workspace is not a canonical directory")
        if not workspace.exists():
            return False
        entries = list(workspace.iterdir())
        if not entries:
            return False
        if not (workspace / ".git").exists():
            _refuse("lane_workspace_not_empty", "non-Git workspace contains retained bytes")
        CleanupInspector(workspace).require_eligible(workspace)
        if self._git_status(workspace):
            _refuse("lane_workspace_dirty", "workspace has uncommitted or untracked bytes")
        return True

    def retire(self, *, actor: str, instance: str, drain: bool) -> Dict[str, Any]:
        architect = self._require_architect(actor)
        if drain is not True:
            _refuse("lane_teardown_drain_required", "numbered instance retirement requires --drain")
        profile, ordinal, node = self._instance(instance)
        active = self.registry.require_active(node)
        if active["role"] != profile.harness:
            _refuse("lane_instance_profile_mismatch", "instance harness does not match its profile")
        self._drain(node)
        workspace = self.root.path / "nodes" / node
        recursive = self._require_removable(workspace)
        timestamp = _timestamp()

        def decide(existing: list[Dict[str, Any]]):
            latest = self._latest_registry(existing).get(node)
            if latest is None or latest.get("state") != "active":
                _refuse("unknown_node", "numbered instance is not active")
            records: list[Dict[str, Any]] = [
                {
                    "schema_version": 0,
                    "id": "registry-" + uuid7_hex(),
                    "tenant_id": self.root.tenant_id,
                    "timestamp": timestamp,
                    "kind": "registry_entry",
                    "node_id": node,
                    "role": profile.harness,
                    "state": "retired",
                }
            ]
            lease = None
            for record in existing:
                if record.get("kind") == "node_lease" and record.get("node_id") == node:
                    lease = record
            if lease is not None and lease.get("state") == "active":
                records.append(
                    {
                        "schema_version": 0,
                        "id": "lease-" + uuid7_hex(),
                        "tenant_id": self.root.tenant_id,
                        "timestamp": timestamp,
                        "kind": "node_lease",
                        "node_id": node,
                        "workspace": str(workspace),
                        "predecessor_lease_id": lease["id"],
                        "state": "retired",
                    }
                )
            receipt = {
                "schema_version": 0,
                "id": _record_id("lane_teardown_receipt"),
                "tenant_id": self.root.tenant_id,
                "timestamp": timestamp,
                "kind": "lane_teardown_receipt",
                "profile": profile.name,
                "ordinal": ordinal,
                "node_id": node,
                "actor": architect,
                "state": "complete",
                "removed": [str(workspace)],
                "retained": [
                    self.registry.relative_path.as_posix(),
                    "events.jsonl",
                    "receipts/",
                    "work/items.jsonl",
                ],
            }
            records.append(receipt)
            for record in records:
                validate_record(
                    record,
                    self.root.tenant_id,
                    frozenset(REGISTRY_KINDS),
                    integrity=False,
                )
            if workspace.exists():
                if recursive:
                    shutil.rmtree(workspace)
                else:
                    workspace.rmdir()
            return receipt, records

        return transact_records(
            self.root,
            self.registry.relative_path,
            decide,
            allowed_kinds=set(REGISTRY_KINDS),
        )
