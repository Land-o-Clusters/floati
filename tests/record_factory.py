"""Per-kind record factory behind the prior-kind walk (REC-FACTORY-1).

The factory derives one valid record per prior record kind so a single test
can walk the whole vocabulary and RED on any loosening that breaks a kind.
Derivation sources, in order:

1. the committed old-record fixture corpus (``tests/fixtures/v7-pc``),
2. the governed JSON Schemas under ``schemas/v0`` and ``schemas/v1``: the
   kind→schema mapping is CONTENT-based — every schema file is scanned for
   the ``kind`` const/enum its record branches declare, so shared files with
   ``oneOf`` branches and differently-suffixed file names resolve without
   guessing (Am.1). Only the ``required`` fields are built, and every value
   is synthesized from the schema's own contract — ``const``, ``enum``,
   ``pattern`` (solved deterministically through ``sre_parse``), and
   numeric/string bounds. Record-level and property-level ``oneOf``/
   ``anyOf``/``if-then`` combinators are resolved deterministically (first
   matching/first branch), which is what exactly-one-of field pairs require.

A kind the factory cannot derive raises :class:`KindNotDerivable` — a typed
absence with the reason. Absences are counted and pinned by the walk; they
are never faked. Two honest absence classes exist, all pinned below:

* :data:`NO_SCHEMA_KINDS` — no governed schema file's content carries the
  kind's const/enum (measured 6: the V7-PC-F1 read's count),
* :data:`VALIDATOR_CONTRACT_KINDS` — a schema exists and a record is
  synthesized, but `validate_record` carries a value contract BEYOND the
  schema (computed digests, cross-field time ordering, cross-record refs,
  scope/state-machine grammar); each kind is pinned with its measured
  refusal code.

RETRACTION (Am.1): the first bank pinned ``node_lease`` as a genuine
schema/runtime field-set drift. That was wrong — schema and validator AGREE
on exactly-one-of ``expires_at``/``predecessor_lease_id``; the factory's
synthesizer simply never resolved ``oneOf``. With combinator resolution the
kind derives; `validate_record` then refuses only its beyond-schema
workspace contract (``node_workspace_invalid``), which is the pinned honest
state. The drift finding is withdrawn.

A synthesized record that `validate_record` refuses with a code that is NOT
pinned for its kind is NOT an absence: the walk surfaces the refusal,
because a prior kind that stops reading is exactly the loosening this row
exists to catch. No counts are typed here: the census lives in the two
pinned dicts below and :func:`census` derives it live; the walk asserts
their partition of the vocabulary.
"""

from __future__ import annotations

import json
from pathlib import Path

from floati.ids import uuid7_hex
from floati.records import _SPECS

try:  # pragma: no cover - the stdlib moved the parser in 3.12
    import sre_parse
except ImportError:  # pragma: no cover
    from re import _parser as sre_parse  # type: ignore[no-redef]

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPOSITORY_ROOT / "tests" / "fixtures" / "v7-pc"
SCHEMA_ROOTS = (REPOSITORY_ROOT / "schemas" / "v1", REPOSITORY_ROOT / "schemas" / "v0")

#: Prior kinds with no governed schema at all: a content scan of every
#: v0/v1 file finds no branch declaring the kind's const/enum. Exactly the
#: six the V7-PC-F1 read measured.
NO_SCHEMA_KINDS: dict[str, str] = {
    kind: "no governed schema content declares this kind (v0 and v1 both scanned)"
    for kind in (
        "confluence_grant", "delivery_claim", "journal_checkpoint_state",
        "lane_spawn_receipt", "lane_teardown_receipt", "verification_receipt",
    )
}

#: Prior kinds whose synthesized record `validate_record` refuses with a
#: value contract beyond the schema; pinned with the measured refusal code.
VALIDATOR_CONTRACT_KINDS: dict[str, str] = {
    "approval_consumed_for_resume": "requested_scope_invalid",
    "approval_decision": "granted_scope_invalid",
    "approval_request": "scope_invalid",
    "attempt_cancelled_before_start": "cancel_transition_invalid",
    "attempt_spawn_policy_bound": "spawn_policy_limits_invalid",
    "attempt_suspended_for_approval": "requested_scope_invalid",
    "attempt_terminal": "terminal_policy_invalid",
    "bridge_consent": "bridge_same_root",
    "bridge_forward": "bridge_same_root",
    "bridge_record": "bridge_same_root",
    "bus_epoch_roll_receipt": "invalidated_followers_invalid",
    "capability": "time_order_invalid",
    "capability_grant": "capability_grant_digest_invalid",
    "capability_set_bound": "capability_set_digest_invalid",
    "credential_lease_granted": "credential_lease_time_invalid",
    "decision_record": "decision_scope_invalid",
    "fleet_update_completed": "timestamp_invalid",
    "fleet_update_started": "timestamp_invalid",
    "fleet_update_step": "timestamp_invalid",
    "gateway_session_ingress": "workspace_invalid",
    "intake_snapshot": "payload_path_invalid",
    "ledger_repair_receipt": "replaced_inode_invalid",
    "liveness_presence": "time_order_invalid",
    "mcp_integration_pin": "server_command_invalid",
    "node_lease": "node_workspace_invalid",
    "plan_amendment": "child_task_contract_digest_invalid",
    "quota_receipt_record": "quota_state_invalid",
    "result_accepted": "acceptance_receipt_id_invalid",
    "run_admission_bound": "run_admission_digest_invalid",
    "run_created": "dependency_edges_invalid",
    "run_environment_observed": "run_environment_unknown_invalid",
    "run_manifest_fact": "tool_set_invalid",
    "run_spawn_admission_enabled": "spawn_base_plan_digest_invalid",
    "segment_sealed": "seal_digest_invalid",
    "sequencer_epoch": "previous_epoch_record_id_invalid",
    "session_adoption": "time_order_invalid",
    "spawn_group_aborted": "cancel_scope_resolved_id_invalid",
    "thread_observation_recorded": "active_flags_invalid",
    "wake_attempt_receipt": "decision_receipt_id_invalid",
    "wake_daemon_consent_receipt": "predecessor_receipt_id_invalid",
    "wake_daemon_lifecycle_receipt": "plist_digest_invalid",
    "wake_hold_receipt": "wake_hold_decision_digest_invalid",
}


class KindNotDerivable(LookupError):
    """Typed absence: no record can be derived for this kind, with the reason."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason


def prior_kinds() -> list[str]:
    """The pinned prior-kind vocabulary the walk must cover."""

    return sorted(set(json.loads((EVIDENCE / "kinds-before.json").read_text())))


def census() -> dict[str, list[str]]:
    """The live partition of the prior vocabulary, derived from the pins."""

    from floati.errors import ProtocolRefusal
    from floati.records import validate_record

    walks: list[str] = []
    no_schema: list[str] = []
    contracts: list[str] = []
    for kind in prior_kinds():
        try:
            record = synthesize(kind)
        except KindNotDerivable:
            no_schema.append(kind)
            continue
        try:
            validate_record(record, record["tenant_id"], frozenset({kind}), integrity=False)
        except ProtocolRefusal as refusal:
            contracts.append(f"{kind}: {refusal.code}")
            continue
        walks.append(kind)
    return {"walks": walks, "no_schema": no_schema, "validator_contracts": contracts}


def fixture_records() -> dict[str, dict]:
    """The committed old-record corpus, keyed by kind."""

    rows = json.loads((EVIDENCE / "old-records.json").read_text())
    return {row["kind"]: dict(row) for row in rows}


def _kind_of_property(kind_spec: dict) -> str | None:
    """The kind identity a schema branch declares, if any."""

    if "const" in kind_spec and isinstance(kind_spec["const"], str):
        return kind_spec["const"]
    if "enum" in kind_spec:
        strings = [value for value in kind_spec["enum"] if isinstance(value, str)]
        if strings:
            return strings[0]
    return None


def _index_kind_declarations(node: object, owner: dict | None, index: dict) -> None:
    """Collect kind→schema-branch declarations from a schema file's content.

    Anywhere a schema names a ``kind`` property with a const/enum, the dict
    holding that ``properties`` block is a record schema branch for that
    kind — including branches inside ``oneOf``/``anyOf``/``$defs`` of a
    shared file. File names are never guessed.
    """

    if isinstance(node, list):
        for item in node:
            _index_kind_declarations(item, owner, index)
        return
    if not isinstance(node, dict):
        return
    properties = node.get("properties")
    if isinstance(properties, dict) and isinstance(properties.get("kind"), dict):
        kind = _kind_of_property(properties["kind"])
        if kind is not None and kind not in index:
            index[kind] = node
    for value in node.values():
        _index_kind_declarations(value, node, index)


_INDEX: dict[str, tuple[dict, dict, Path]] | None = None


def _schema_index() -> dict[str, tuple[dict, dict, Path]]:
    """kind → (record branch, enclosing file root, file directory), once.

    v1 files are indexed before v0 so the freshest governed contract wins.
    """

    global _INDEX
    if _INDEX is None:
        index: dict[str, tuple[dict, dict, Path]] = {}
        for root in SCHEMA_ROOTS:
            for path in sorted(root.glob("*.json")):
                file_root = json.loads(path.read_text(encoding="utf-8"))
                local: dict[str, dict] = {}
                _index_kind_declarations(file_root, None, local)
                for kind, branch in local.items():
                    index.setdefault(kind, (branch, file_root, root))
        _INDEX = index
    return _INDEX


def _schema_branch(kind: str) -> tuple[dict, dict, Path] | None:
    return _schema_index().get(kind)


class _UnsupportedPattern(Exception):
    """The pattern uses regex syntax the deterministic solver does not carry."""


def _pattern_value(pattern: str) -> str:
    """One deterministic string matching ``pattern``, or _UnsupportedPattern.

    The solver walks the stdlib parse tree and takes the minimal branch at
    every choice: the first alternation arm, the minimum repeat count, the
    first range character. Anchors contribute nothing.
    """

    tree = sre_parse.parse(pattern)
    out: list[str] = []

    def walk(node) -> None:
        for op, av in node:
            if op is sre_parse.LITERAL:
                out.append(chr(av))
            elif op is sre_parse.NOT_LITERAL:
                out.append("a")
            elif op is sre_parse.ANY:
                out.append("a")
            elif op in (sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT):
                minimum, _maximum, sub = av
                for _ in range(minimum):
                    walk(sub)
            elif op is sre_parse.SUBPATTERN:
                walk(av[-1])
            elif op is sre_parse.BRANCH:
                walk(av[1][0])
            elif op in (sre_parse.ASSERT, sre_parse.ASSERT_NOT):
                continue  # lookaround constrains; the minimal tail satisfies it here
            elif op is sre_parse.AT:
                continue
            elif op is sre_parse.IN:
                members = list(av)
                if members and members[0][0] is sre_parse.NEGATE:
                    out.append("a")
                    continue
                for member in members:
                    if member[0] is sre_parse.RANGE:
                        out.append(chr(member[1][0]))
                        break
                    if member[0] is sre_parse.LITERAL:
                        out.append(chr(member[1]))
                        break
                    if member[0] is sre_parse.CATEGORY:
                        if member[1] is sre_parse.CATEGORY_DIGIT:
                            out.append("0")
                        elif member[1] is sre_parse.CATEGORY_SPACE:
                            out.append(" ")
                        else:
                            out.append("a")
                        break
                else:
                    raise _UnsupportedPattern(pattern)
            elif op is sre_parse.CATEGORY:
                out.append("a")
            else:
                raise _UnsupportedPattern(pattern)

    walk(tree)
    return "".join(out)


def _deref(spec: dict, root: dict, base: Path, depth: int = 0) -> tuple[dict, dict, Path]:
    """Follow ``$ref`` links; return the spec plus the file context it lives in.

    Same-file pointers resolve against ``root``; file-bearing refs load the
    sibling schema and re-base relative refs on its directory.
    """

    if depth > 4:
        raise _UnsupportedPattern("$ref depth exceeds 4")
    ref = spec.get("$ref")
    if ref is None:
        return spec, root, base
    if ref.startswith("#/"):
        node: object = root
        for part in ref[2:].split("/"):
            if not isinstance(node, dict) or part not in node:
                raise _UnsupportedPattern(f"unresolvable ref {ref!r}")
            node = node[part]
        return _deref(node, root, base, depth + 1)
    file_part, _, pointer = ref.partition("#")
    target_root = json.loads((base / file_part).read_text(encoding="utf-8"))
    node = target_root
    for part in pointer.strip("/").split("/"):
        if not part:
            continue
        if not isinstance(node, dict) or part not in node:
            raise _UnsupportedPattern(f"unresolvable ref {ref!r}")
        node = node[part]
    return _deref(node, target_root, base / file_part, depth + 1)


_CONTRACT_KEYS = ("type", "pattern", "format", "minLength", "maxLength",
                  "minimum", "maximum", "enum", "const")


def _field_value(name: str, spec: dict, root: dict, base: Path) -> object:
    spec, spec_root, spec_base = _deref(spec, root, base)
    for member in spec.get("allOf", ()):
        # Merge composed contracts: each member's own constraints join the
        # effective spec, with refs resolved in the member's file context.
        resolved, member_root, member_base = _deref(member, spec_root, spec_base)
        for key in _CONTRACT_KEYS:
            if key in resolved and key not in spec:
                spec[key] = resolved[key]
    if "const" in spec:
        return spec["const"]
    if "enum" in spec:
        return spec["enum"][0]
    field_type = spec.get("type")
    if field_type == "null":
        return None
    if isinstance(field_type, list) and "null" in field_type:
        return None
    if field_type == "integer" or field_type == "number":
        if "minimum" in spec:
            return spec["minimum"]
        return 1
    if field_type == "boolean":
        return True
    if field_type == "array":
        items = spec.get("items")
        if isinstance(items, dict):
            # Validators require populated lists (minItems, membership checks):
            # derive one element from the schema's own items contract.
            return [_field_value(name, items, spec_root, spec_base)]
        return []
    if field_type == "object":
        # One level of required-member synthesis for object contracts.
        properties = spec.get("properties") or {}
        if spec.get("required") and properties:
            return {
                member: _field_value(member, properties[member], spec_root, spec_base)
                for member in spec["required"]
                if member in properties
            }
        member_schema = spec.get("additionalProperties")
        if int(spec.get("minProperties", 0) or 0) >= 1 and isinstance(member_schema, dict):
            # A bounded non-empty map: derive its one member from the
            # additionalProperties contract itself.
            return {"a": _field_value(name, member_schema, spec_root, spec_base)}
        return {}
    if "pattern" in spec:
        pattern = spec["pattern"]
        if "\\d{4}-\\d{2}-\\d{2}T" in pattern:
            # A date-time shape must also be a REAL instant for the validator;
            # the deterministic fixture instant is 2026-09-05T17:49:39.319Z.
            return "2026-09-05T17:49:39.319Z"
        return _pattern_value(pattern)
    if spec.get("format") == "date-time":
        return "2026-09-05T17:49:39.319Z"
    if field_type == "string" or field_type is None:
        floor = int(spec.get("minLength", 1) or 1)
        ceiling = spec.get("maxLength")
        value = "x" * max(1, floor)
        if ceiling is not None:
            value = value[: int(ceiling)]
        return value
    raise _UnsupportedPattern(f"{name}: unsupported type {field_type!r}")


def synthesize(kind: str) -> dict:
    """Return one record for ``kind`` or raise the typed absence by name."""

    corpus = fixture_records()
    if kind in corpus:
        return dict(corpus[kind])

    entry = _schema_branch(kind)
    if entry is None:
        raise KindNotDerivable(
            kind,
            NO_SCHEMA_KINDS.get(
                kind,
                "no governed schema content declares this kind (v0 and v1 both scanned)",
            ),
        )
    node, schema, base = entry
    branch_props = node.get("properties") or {}
    root_props = schema.get("properties") or {}
    required = list(
        dict.fromkeys([*schema.get("required", []), *node.get("required", [])])
    )
    undescribed = [
        field for field in required if field not in branch_props and field not in root_props
    ]
    if undescribed:
        raise KindNotDerivable(
            kind,
            "governed schema does not describe required fields: "
            + ", ".join(sorted(undescribed)),
        )
    record: dict[str, object] = {}
    for field in required:
        spec = branch_props.get(field) or root_props.get(field)
        try:
            record[field] = _field_value(field, spec, schema, base)
        except _UnsupportedPattern as unsupported:
            raise KindNotDerivable(kind, f"unsupported pattern contract: {unsupported}")
    if "id" in record and kind in _SPECS:
        # Record ids are prefix + uuid7; the shared-file generic id def cannot
        # know the per-kind prefix, so rebuild it from the runtime contract.
        record["id"] = _SPECS[kind][0] + uuid7_hex()
    _apply_combinators(kind, node, schema, base, record)
    return record


def _arm_for_kind(arms: list, kind: str) -> dict | None:
    """The combinator arm whose declared kind matches, else the first."""

    for candidate in arms:
        local: dict[str, dict] = {}
        _index_kind_declarations(candidate, None, local)
        if kind in local:
            return candidate
    return arms[0]


def _kind_matches(condition: dict, kind: str) -> bool:
    """Whether an ``if`` condition's kind const/enum names this kind."""

    properties = condition.get("properties") or {}
    kind_spec = properties.get("kind")
    if not isinstance(kind_spec, dict):
        return False
    return _kind_of_property(kind_spec) == kind


def _apply_required(arm: dict, owner: dict, kind: str, schema: dict, base: Path, record: dict) -> None:
    properties = arm.get("properties") or {}
    for field in arm.get("required", []):
        spec = properties.get(field) or owner.get("properties", {}).get(field)
        if spec is None:
            continue
        try:
            record[field] = _field_value(field, spec, schema, base)
        except _UnsupportedPattern as unsupported:
            raise KindNotDerivable(kind, f"unsupported pattern contract: {unsupported}")


def _apply_combinators(
    kind: str, node: dict, schema: dict, base: Path, record: dict
) -> None:
    """Resolve exactly-one-of style combinators (Am.1, root scope Am.2).

    The kind-matched ``oneOf``/``anyOf`` arm — or an ``if``/``then`` pair —
    names the alternative field set the record must carry; its required
    fields are synthesized (branch consts override), which is what
    node_lease's expires_at/predecessor_lease_id pair needed. The SCHEMA
    ROOT's ``allOf`` and kind-keyed ``if``/``then`` members are resolved too
    (Am.2): fleet_update_started's recovery_witness lives under a root
    ``if kind==fleet_update_started / then`` and is part of its contract.
    """

    arm = None
    for key in ("oneOf", "anyOf"):
        arms = node.get(key)
        if isinstance(arms, list) and arms:
            arm = _arm_for_kind(arms, kind)
            break
    if arm is None and isinstance(node.get("then"), dict):
        arm = node["then"]
    if arm is not None:
        _apply_required(arm, node, kind, schema, base, record)
        for field, spec in (arm.get("properties") or {}).items():
            resolved = _deref(spec, schema, base)[0]
            if "const" in resolved and field in record:
                record[field] = resolved["const"]

    for source in (node, schema):
        for member in source.get("allOf", ()) or ():
            condition = member.get("if")
            if isinstance(condition, dict):
                if _kind_matches(condition, kind) and isinstance(member.get("then"), dict):
                    _apply_required(member["then"], schema, kind, schema, base, record)
                continue
            _apply_required(member, schema, kind, schema, base, record)
        condition = source.get("if")
        if isinstance(condition, dict):
            if _kind_matches(condition, kind) and isinstance(source.get("then"), dict):
                _apply_required(source["then"], schema, kind, schema, base, record)
