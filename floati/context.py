"""Read-only context observability and turnover projections."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from .admin_registry import RegistryAdminBackend
from .context_absences import load_shipped_context_absences
from .errors import ProtocolRefusal
from .role_templates import RoleTemplate, load_shipped_role_templates
from .root import FloatiRoot, resolve_command_root, validate_identifier


_UUID7_HEX = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_ROLE_RECORD_ID = re.compile(r"^registry-role-" + _UUID7_HEX + r"$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$"
)
_ROLE_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "tenant_id",
        "timestamp",
        "kind",
        "node_id",
        "template_role",
        "template_version",
        "template_sha256",
        "answers",
        "state",
        "predecessor_role_record_id",
    }
)
_STATUS_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "node_id",
        "harness",
        "dataset",
        "remaining_context",
        "read_only",
    }
)
_TURNOVER_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "node_id",
        "harness",
        "state_file",
        "inputs",
        "role_provenance",
        "steps",
        "read_only",
    }
)
_STEP_FIELDS = frozenset({"kind", "artifact_kind", "argv", "optional_argv"})
_DECLARED_ROOTS = "<DECLARED_ROOTS_FILE>"
_MANAGED_EXECUTABLE = "<MANAGED_EXECUTABLE>"
_PROFILE = "<PROFILE>"
_PRIOR_MTIME = "<PRIOR_MTIME_NS>"


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal(code, detail)


def _safe_text(value: object, *, code: str, detail: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        _refuse(code, detail)
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character)
        in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        for character in value
    ):
        _refuse(code, detail)
    return value


def _safe_ascii(value: object, *, code: str, detail: str, maximum: int = 4096) -> str:
    value = _safe_text(value, code=code, detail=detail, maximum=maximum)
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProtocolRefusal(code, detail) from exc
    return value


def _mapping(value: object, *, code: str, detail: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _refuse(code, detail)
    return value


def _sequence(value: object, *, code: str, detail: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _refuse(code, detail)
    return value


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


class ContextNodeSource(Protocol):
    """The live node and role evidence required by E2."""

    def active_node(self, node_id: str) -> Mapping[str, object]: ...

    def role_record(self, node_id: str) -> Mapping[str, object]: ...


class _RegistryContextSource:
    def __init__(self, root: FloatiRoot) -> None:
        self.backend = RegistryAdminBackend(root)

    def active_node(self, node_id: str) -> Mapping[str, object]:
        return self.backend.active_node(node_id)

    def role_record(self, node_id: str) -> Mapping[str, object]:
        return self.backend.role_record(node_id)


class _ContextProjection:
    def __init__(
        self,
        root: FloatiRoot,
        node_id: str,
        *,
        source: Optional[ContextNodeSource] = None,
    ) -> None:
        if not isinstance(root, FloatiRoot):
            _refuse("context_root_invalid", "context projection requires a validated root")
        self.root = root
        self.node_id = validate_identifier(node_id, "node")
        self.source = _RegistryContextSource(root) if source is None else source

    def _active(self) -> Dict[str, str]:
        raw = _mapping(
            self.source.active_node(self.node_id),
            code="context_node_invalid",
            detail="active node evidence must be an object",
        )
        if (
            raw.get("tenant_id") != self.root.tenant_id
            or raw.get("node_id") != self.node_id
            or raw.get("state") != "active"
        ):
            _refuse("context_node_invalid", "active node evidence is stale or foreign")
        harness_value = raw.get("harness", raw.get("role"))
        harness = _safe_ascii(
            harness_value,
            code="context_node_invalid",
            detail="active node harness is invalid",
            maximum=64,
        )
        return {"node_id": self.node_id, "harness": harness}

    def project(self) -> Dict[str, object]:
        raise NotImplementedError

    def to_json(self) -> str:
        return json.dumps(
            self.project(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"

    def render(self) -> str:
        return render_context_projection(self.project())


class ContextStatusProjection(_ContextProjection):
    """Project the selected node's E1-bound observability state."""

    def project(self) -> Dict[str, object]:
        active = self._active()
        dataset = load_shipped_context_absences()
        row = dataset.for_harness(active["harness"])
        return {
            "schema_version": 0,
            "kind": "context_status_projection",
            "node_id": self.node_id,
            "harness": row.harness,
            "dataset": {
                "id": dataset.dataset_id,
                "source_commit": dataset.source_commit,
            },
            "remaining_context": {
                "access_class": row.access_class,
                "state": row.state,
                "message": (
                    "remaining context: not exposed to external probes by "
                    f"{row.harness}"
                ),
                "receipt": {
                    "path": row.receipt_path,
                    "sha256": row.receipt_sha256,
                },
            },
            "read_only": True,
        }


class ContextTurnoverProjection(_ContextProjection):
    """Project the operator-supplied D3/D5 turnover recipe."""

    @staticmethod
    def _templates() -> Dict[str, RoleTemplate]:
        return load_shipped_role_templates(Path(__file__).parents[1] / "roles" / "shipped")

    def _role_provenance(self) -> Dict[str, object]:
        raw = _mapping(
            self.source.role_record(self.node_id),
            code="context_role_invalid",
            detail="role provenance must be an object",
        )
        if set(raw) != _ROLE_FIELDS:
            _refuse("context_role_invalid", "role provenance fields do not match D2")
        record_id = raw.get("id")
        if (
            raw.get("schema_version") != 0
            or isinstance(raw.get("schema_version"), bool)
            or raw.get("tenant_id") != self.root.tenant_id
            or raw.get("kind") != "registry_role_record"
            or raw.get("node_id") != self.node_id
            or raw.get("state") != "active"
            or not isinstance(record_id, str)
            or _ROLE_RECORD_ID.fullmatch(record_id) is None
            or not _valid_timestamp(raw.get("timestamp"))
        ):
            _refuse("context_role_invalid", "role provenance identity is invalid")
        predecessor = raw.get("predecessor_role_record_id")
        if predecessor is not None and (
            not isinstance(predecessor, str)
            or _ROLE_RECORD_ID.fullmatch(predecessor) is None
        ):
            _refuse("context_role_invalid", "role provenance predecessor is invalid")
        try:
            template_role = validate_identifier(raw.get("template_role"), "template_role")
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "context_role_invalid", "role provenance template is invalid"
            ) from exc
        version = raw.get("template_version")
        digest = raw.get("template_sha256")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            _refuse("context_role_invalid", "role provenance version or digest is invalid")
        templates = self._templates()
        template = templates.get(template_role)
        if (
            template is None
            or template.template_version != version
            or template.digest != digest
        ):
            _refuse("context_role_mismatch", "role provenance does not match shipped D1 copy")
        answers = _mapping(
            raw.get("answers"),
            code="context_role_invalid",
            detail="role provenance answers must be an object",
        )
        question_keys = {question.key for question in template.questions}
        if set(answers) != question_keys:
            _refuse("context_role_invalid", "role provenance answers do not match D1")
        for key in sorted(question_keys):
            _safe_text(
                answers.get(key),
                code="context_role_invalid",
                detail="role provenance answer is invalid",
                maximum=500,
            )
        return {
            "role_record_id": record_id,
            "template_role": template_role,
            "template_version": version,
            "template_sha256": digest,
        }

    def project(self) -> Dict[str, object]:
        active = self._active()
        root = str(self.root.path)
        shared = [
            "--root",
            root,
            "--node",
            self.node_id,
            "--declared-roots",
            _DECLARED_ROOTS,
            "--managed-executable",
            _MANAGED_EXECUTABLE,
            "--profile",
            _PROFILE,
        ]
        return {
            "schema_version": 0,
            "kind": "context_turnover_projection",
            "node_id": self.node_id,
            "harness": active["harness"],
            "state_file": str(self.root.path / "nodes" / self.node_id / "STATE.md"),
            "inputs": {
                "source": "operator_supplied",
                "values": {
                    "declared_roots": _DECLARED_ROOTS,
                    "managed_executable": _MANAGED_EXECUTABLE,
                    "profile": _PROFILE,
                    "prior_mtime_ns": _PRIOR_MTIME,
                },
            },
            "role_provenance": self._role_provenance(),
            "steps": [
                {
                    "kind": "teardown_projection",
                    "artifact_kind": "node_teardown_projection",
                    "argv": ["floati", "node", "teardown", *shared],
                    "optional_argv": ["--json"],
                },
                {
                    "kind": "state_flush_receipt",
                    "artifact_kind": "node_state_flush_receipt",
                    "argv": [
                        "floati",
                        "node",
                        "state-flush",
                        "--root",
                        root,
                        "--node",
                        self.node_id,
                    ],
                    "optional_argv": ["--prior-mtime-ns", _PRIOR_MTIME],
                },
                {
                    "kind": "boot_projection",
                    "artifact_kind": "node_boot_projection",
                    "argv": ["floati", "node", "boot", *shared],
                    "optional_argv": ["--json"],
                },
            ],
            "read_only": True,
        }


def _render_argv(value: object) -> str:
    parts = _sequence(
        value,
        code="context_output_invalid",
        detail="recipe argv must be a sequence",
    )
    checked = [
        _safe_ascii(
            part,
            code="context_output_invalid",
            detail="recipe argv contains unsafe text",
        )
        for part in parts
    ]
    if not checked:
        _refuse("context_output_invalid", "recipe argv is empty")
    return shlex.join(checked)


def render_context_projection(artifact: Mapping[str, object]) -> str:
    """Render one validated E2 artifact as deterministic ASCII text."""

    value = _mapping(
        artifact,
        code="context_output_invalid",
        detail="context artifact must be an object",
    )
    kind = value.get("kind")
    node_id = _safe_ascii(
        value.get("node_id"),
        code="context_output_invalid",
        detail="context node ID is invalid",
        maximum=64,
    )
    harness = _safe_ascii(
        value.get("harness"),
        code="context_output_invalid",
        detail="context harness is invalid",
        maximum=64,
    )
    if value.get("schema_version") != 0 or value.get("read_only") is not True:
        _refuse("context_output_invalid", "context artifact header is invalid")
    if kind == "context_status_projection":
        if set(value) != _STATUS_FIELDS:
            _refuse("context_output_invalid", "context status fields do not match v0")
        remaining = _mapping(
            value.get("remaining_context"),
            code="context_output_invalid",
            detail="context status body is invalid",
        )
        receipt = _mapping(
            remaining.get("receipt"),
            code="context_output_invalid",
            detail="context status receipt is invalid",
        )
        if not {"path", "sha256"}.issubset(receipt):
            _refuse(
                "context_absence_citation_missing",
                "context status requires receipt path and SHA-256",
            )
        if set(receipt) != {"path", "sha256"} or set(remaining) != {
            "access_class",
            "state",
            "message",
            "receipt",
        }:
            _refuse("context_output_invalid", "context status body fields are invalid")
        state = _safe_ascii(
            remaining.get("state"),
            code="context_output_invalid",
            detail="context status state is invalid",
            maximum=32,
        )
        access_class = _safe_ascii(
            remaining.get("access_class"),
            code="context_output_invalid",
            detail="context status access class is invalid",
            maximum=1,
        )
        message = _safe_ascii(
            remaining.get("message"),
            code="context_output_invalid",
            detail="context status message is invalid",
        )
        path = _safe_ascii(
            receipt.get("path"),
            code="context_output_invalid",
            detail="context status receipt path is invalid",
        )
        digest = _safe_ascii(
            receipt.get("sha256"),
            code="context_output_invalid",
            detail="context status receipt SHA-256 is invalid",
            maximum=64,
        )
        if (
            access_class != "A"
            or state != "not_exposed"
            or _SHA256.fullmatch(digest) is None
        ):
            _refuse("context_output_invalid", "context status evidence is invalid")
        if message != (
            "remaining context: not exposed to external probes by " + harness
        ):
            _refuse("context_output_invalid", "context status message is not derived")
        dataset = _mapping(
            value.get("dataset"),
            code="context_output_invalid",
            detail="context dataset citation is invalid",
        )
        if (
            set(dataset) != {"id", "source_commit"}
            or dataset.get("id") != "e1-context-absence-v1"
            or not isinstance(dataset.get("source_commit"), str)
            or re.fullmatch(r"[0-9a-f]{40}", str(dataset["source_commit"])) is None
        ):
            _refuse("context_output_invalid", "context dataset citation is invalid")
        shipped = load_shipped_context_absences()
        try:
            shipped_row = shipped.for_harness(harness)
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "context_output_invalid", "context harness is absent from shipped E1 evidence"
            ) from exc
        if (
            dataset.get("id") != shipped.dataset_id
            or dataset.get("source_commit") != shipped.source_commit
            or state != shipped_row.state
            or access_class != shipped_row.access_class
            or path != shipped_row.receipt_path
            or digest != shipped_row.receipt_sha256
        ):
            _refuse(
                "context_output_invalid",
                "context status does not match shipped E1 evidence",
            )
        lines = [
            "CONTEXT STATUS PROJECTION",
            f"NODE: {node_id}",
            f"HARNESS: {harness}",
            "ACCESS CLASS: A - external/programmatic",
            message,
            f"RECEIPT PATH: {path}",
            f"RECEIPT SHA256: {digest}",
        ]
    elif kind == "context_turnover_projection":
        if set(value) != _TURNOVER_FIELDS:
            _refuse("context_output_invalid", "turnover fields do not match v0")
        inputs = _mapping(
            value.get("inputs"),
            code="context_output_invalid",
            detail="turnover inputs are invalid",
        )
        if inputs.get("source") != "operator_supplied":
            _refuse("context_output_invalid", "turnover inputs must be operator supplied")
        input_values = _mapping(
            inputs.get("values"),
            code="context_output_invalid",
            detail="turnover input values are invalid",
        )
        if set(inputs) != {"source", "values"} or input_values != {
            "declared_roots": _DECLARED_ROOTS,
            "managed_executable": _MANAGED_EXECUTABLE,
            "profile": _PROFILE,
            "prior_mtime_ns": _PRIOR_MTIME,
        }:
            _refuse("context_output_invalid", "turnover input values are invalid")
        provenance = _mapping(
            value.get("role_provenance"),
            code="context_output_invalid",
            detail="turnover role provenance is invalid",
        )
        if set(provenance) != {
            "role_record_id",
            "template_role",
            "template_version",
            "template_sha256",
        }:
            _refuse("context_output_invalid", "turnover role provenance fields are invalid")
        role = _safe_ascii(
            provenance.get("template_role"),
            code="context_output_invalid",
            detail="turnover role is invalid",
            maximum=64,
        )
        role_record = _safe_ascii(
            provenance.get("role_record_id"),
            code="context_output_invalid",
            detail="turnover role record is invalid",
            maximum=128,
        )
        digest = _safe_ascii(
            provenance.get("template_sha256"),
            code="context_output_invalid",
            detail="turnover template SHA-256 is invalid",
            maximum=64,
        )
        version = provenance.get("template_version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
            or _ROLE_RECORD_ID.fullmatch(role_record) is None
            or _SHA256.fullmatch(digest) is None
        ):
            _refuse("context_output_invalid", "turnover template provenance is invalid")
        try:
            role_key = validate_identifier(role, "template_role")
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "context_output_invalid", "turnover template role is invalid"
            ) from exc
        shipped_template = ContextTurnoverProjection._templates().get(role_key)
        if (
            shipped_template is None
            or shipped_template.template_version != version
            or shipped_template.digest != digest
        ):
            _refuse(
                "context_output_invalid",
                "turnover provenance does not match shipped D1 copy",
            )
        steps = _sequence(
            value.get("steps"),
            code="context_output_invalid",
            detail="turnover steps are invalid",
        )
        if len(steps) != 3:
            _refuse("context_output_invalid", "turnover requires three ordered steps")
        expected = ("teardown_projection", "state_flush_receipt", "boot_projection")
        state_file = _safe_ascii(
            value.get("state_file"),
            code="context_output_invalid",
            detail="turnover state file is invalid",
        )
        selected_state = Path(state_file)
        if (
            not selected_state.is_absolute()
            or selected_state.name != "STATE.md"
            or len(selected_state.parents) < 3
        ):
            _refuse("context_output_invalid", "turnover state file is invalid")
        root = str(selected_state.parents[2])
        if selected_state != Path(root) / "nodes" / node_id / "STATE.md":
            _refuse("context_output_invalid", "turnover state file is not canonical")
        try:
            live_root = FloatiRoot.open_direct_home(root, create=False)
            live_role = RegistryAdminBackend(live_root).role_record(node_id)
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "context_output_invalid",
                "turnover provenance is absent from the live D2 ledger",
            ) from exc
        if (
            live_role.get("id") != role_record
            or live_role.get("template_role") != role_key
            or live_role.get("template_version") != version
            or live_role.get("template_sha256") != digest
        ):
            _refuse(
                "context_output_invalid",
                "turnover provenance does not match the live D2 ledger",
            )
        shared = [
            "--root",
            root,
            "--node",
            node_id,
            "--declared-roots",
            _DECLARED_ROOTS,
            "--managed-executable",
            _MANAGED_EXECUTABLE,
            "--profile",
            _PROFILE,
        ]
        expected_argv = (
            ["floati", "node", "teardown", *shared],
            ["floati", "node", "state-flush", "--root", root, "--node", node_id],
            ["floati", "node", "boot", *shared],
        )
        expected_optional = (
            ["--json"],
            ["--prior-mtime-ns", _PRIOR_MTIME],
            ["--json"],
        )
        expected_artifacts = (
            "node_teardown_projection",
            "node_state_flush_receipt",
            "node_boot_projection",
        )
        lines = [
            "CONTEXT TURNOVER PROJECTION",
            "INPUTS: operator supplied",
            f"NODE: {node_id}",
            f"HARNESS: {harness}",
            f"ROLE: {role} v{version} via {role_record}",
            f"ROLE SHA256: {digest}",
        ]
        for index, (raw_step, expected_kind) in enumerate(zip(steps, expected), start=1):
            step = _mapping(
                raw_step,
                code="context_output_invalid",
                detail="turnover step is invalid",
            )
            if (
                set(step) != _STEP_FIELDS
                or step.get("kind") != expected_kind
                or step.get("artifact_kind") != expected_artifacts[index - 1]
                or step.get("argv") != expected_argv[index - 1]
                or step.get("optional_argv") != expected_optional[index - 1]
            ):
                _refuse(
                    "context_turnover_shape_invalid",
                    "turnover step does not match the landed verb shape",
                )
            line = f"STEP {index}: {_render_argv(step.get('argv'))}"
            optional = _sequence(
                step.get("optional_argv"),
                code="context_output_invalid",
                detail="turnover optional argv is invalid",
            )
            if optional:
                line += " [optional: " + _render_argv(optional) + "]"
            lines.append(line)
    else:
        _refuse("context_output_invalid", "context artifact kind is invalid")
    rendered = "\n".join(lines) + "\n"
    rendered.encode("ascii")
    return rendered


def _handle_status(args: argparse.Namespace) -> Tuple[str, Dict[str, object], int]:
    root = resolve_command_root(args.root, create=False)
    return "ok", ContextStatusProjection(root, args.actor).project(), 0


def _handle_turnover(args: argparse.Namespace) -> Tuple[str, Dict[str, object], int]:
    root = resolve_command_root(args.root, create=False)
    return "ok", ContextTurnoverProjection(root, args.actor).project(), 0


def _handle_policy_set(args: argparse.Namespace) -> Tuple[str, Dict[str, object], int]:
    from .tide_policy import TidePolicyLedger

    root = resolve_command_root(args.root, create=False)
    return "ok", TidePolicyLedger(root).set(
        args.node, args.metric, args.threshold, args.action,
        idempotency_key=args.idempotency_key,
    ), 0


def _handle_policy_show(args: argparse.Namespace) -> Tuple[str, Dict[str, object], int]:
    from .tide import TideEvaluator

    root = resolve_command_root(args.root, create=False)
    evidence = TideEvaluator(
        root,
        source_sha="f2b587634cfc6d6a52cc24bd02bfd978919c359b",
    ).projection(args.node)
    policy = evidence["policy"]
    return ("ok", evidence, 0) if policy is not None else ("no_result", evidence, 32)


def _handle_policy_clear(args: argparse.Namespace) -> Tuple[str, Dict[str, object], int]:
    from .tide_policy import TidePolicyLedger

    root = resolve_command_root(args.root, create=False)
    return "ok", TidePolicyLedger(root).clear(
        args.node, idempotency_key=args.idempotency_key
    ), 0


def _handle_reading_record(args: argparse.Namespace) -> Tuple[str, Dict[str, object], int]:
    from .tide_policy import TideTestimonyLedger

    root = resolve_command_root(args.root, create=False)
    return "ok", TideTestimonyLedger(root).record(
        args.actor,
        args.metric,
        args.value,
        args.testimony_command,
        idempotency_key=args.idempotency_key,
    ), 0


def _add_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True)
    parser.add_argument("--as", dest="actor", required=True)
    parser.add_argument("--json", action="store_true")


def register_cli(commands: argparse._SubParsersAction) -> None:
    """Register the dark E2 command family for integration activation."""

    context = commands.add_parser(
        "context",
        help="inspect context evidence and project turnover",
    )
    context_commands = context.add_subparsers(dest="context_command", required=True)
    status = context_commands.add_parser(
        "status",
        help="report the selected harness evidence",
    )
    _add_identity(status)
    status.set_defaults(handler=_handle_status)
    turnover = context_commands.add_parser(
        "turnover",
        help="print the ordered read-only turnover recipe",
    )
    _add_identity(turnover)
    turnover.set_defaults(handler=_handle_turnover)

    policy = context_commands.add_parser("policy")
    policy_commands = policy.add_subparsers(dest="policy_command", required=True)
    policy_set = policy_commands.add_parser(
        "set", description="set one optional T1-authorized tide policy"
    )
    policy_set.add_argument("--root", required=True)
    policy_set.add_argument("--node", required=True)
    policy_set.add_argument("--metric", required=True)
    policy_set.add_argument("--threshold", required=True)
    policy_set.add_argument("--action", choices=("recommend", "direct"), required=True)
    policy_set.add_argument("--idempotency-key", required=True)
    policy_set.add_argument("--json", action="store_true")
    policy_set.set_defaults(handler=_handle_policy_set)
    policy_show = policy_commands.add_parser(
        "show", description="show the active tide policy or typed absence"
    )
    policy_show.add_argument("--root", required=True)
    policy_show.add_argument("--node", required=True)
    policy_show.add_argument("--json", action="store_true")
    policy_show.set_defaults(handler=_handle_policy_show)
    policy_clear = policy_commands.add_parser(
        "clear", description="clear one active tide policy"
    )
    policy_clear.add_argument("--root", required=True)
    policy_clear.add_argument("--node", required=True)
    policy_clear.add_argument("--idempotency-key", required=True)
    policy_clear.add_argument("--json", action="store_true")
    policy_clear.set_defaults(handler=_handle_policy_clear)

    reading = context_commands.add_parser("reading")
    reading_commands = reading.add_subparsers(dest="reading_command", required=True)
    reading_record = reading_commands.add_parser(
        "record", description="record the seated node's class-B context testimony"
    )
    reading_record.add_argument("--root", required=True)
    reading_record.add_argument("--as", dest="actor", required=True)
    reading_record.add_argument("--metric", required=True)
    reading_record.add_argument("--value", required=True)
    reading_record.add_argument(
        "--command", dest="testimony_command",
        choices=("/context", "/status", "/usage", "/cost"), required=True,
    )
    reading_record.add_argument("--idempotency-key", required=True)
    reading_record.add_argument("--json", action="store_true")
    reading_record.set_defaults(handler=_handle_reading_record)
