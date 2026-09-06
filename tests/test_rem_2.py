"""REM-2: ProtocolRefusal codes classified by inline remedy=.

A code is BOUND when every ProtocolRefusal site passes a non-empty remedy
or the code is in DRILL_REMEDIES, MIXED when some sites do, UNBOUND
otherwise. The unbound set is a ratchet: it may only shrink.
"""

from __future__ import annotations

import ast
import hashlib
import tempfile
import unittest
from pathlib import Path

from floati.errors import DRILL_REMEDIES


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONSTRUCTORS = frozenset({"ProtocolRefusal", "IntegrityFailure", "DurabilityFailure"})
ROLE_VERB_FILES = frozenset(
    {
        "floati/role_assignment.py",
        "floati/role_templates.py",
        "floati/admin_cli.py",
    }
)

PINNED_PROTOCOL_COUNT = 1117

PINNED_KIND_NONE_COUNT = 1026

PINNED_BOUND_COUNT = 91

PINNED_MIXED_COUNT = 6

PINNED_UNBOUND_COUNT = 1020

PINNED_UNRESOLVED_PROTOCOL = 106

PINNED_INTEGRITY_COUNT = 108

PINNED_DURABILITY_COUNT = 14

PINNED_ROOT_SHARED = frozenset(
    {
        "root_not_absolute",
        "root_required",
        "root_symlinked_entry",
        "root_unavailable",
    }
)

PINNED_INTEGRITY_DURABILITY = frozenset(
    {
        "deployment_manifest_invalid",
        "duplicate_record_id",
        "effect_evidence_invalid",
        "epoch_selected_member_changed",
        "fleet_update_completion_invalid",
        "fleet_update_install_metadata_invalid",
        "fleet_update_install_readback_invalid",
        "fleet_update_owner_review_invalid",
        "fleet_update_receipt_invalid",
        "fleet_update_receipt_order_invalid",
        "fleet_update_step_invalid",
        "fleet_update_waiter_binding_readback_invalid",
        "journal_chain_interrupted",
        "ledger_record_limit",
        "ledger_too_large",
        "message_retraction_duplicate",
        "message_retraction_party_invalid",
        "message_retraction_session_invalid",
        "quota_receipt_invalid",
        "record_kind_invalid",
        "record_not_object",
        "record_too_large",
        "schema_version_invalid",
        "thread_attachment_detached",
        "thread_attachment_missing",
        "tide_reading_invalid",
        "update_index_storage_invalid",
        "update_ledger_record_invalid",
        "wake_daemon_adapter_unknown",
        "work_dependencies_blocked",
        "worker_claim_already_bound",
        "worker_session_mismatch",
        "worker_transition_invalid",
        "workspace_declaration_invalid",
        "{dyn}_invalid",
        "{dyn}_unavailable",
    }
)

PINNED_ROLE_INVALID_OUTSIDE = frozenset(
    {
        "context_role_invalid",
        "node_projection_role_invalid",
        "operator_role_invalid",
    }
)

PINNED_OTHER_COUNT = 983

PINNED_PROTOCOL_DIGEST = (
    "76e59862d81866669379243cc41eac7d1ed160b8cd24c76a403978dcc6bc5fe3"
)

PINNED_KIND_NONE_DIGEST = (
    "72908f2114d78fad60fcb2114b18190d60c40aa08286eb861c1010ed6db9d719"
)

PINNED_OTHER_DIGEST = (
    "8496cf4d498e49fe096e8733bf195da91be0b20eae8ca06ce7dbf0ed76e20153"
)

PINNED_INTEGRITY_DIGEST = (
    "8ba8eeaf34b24aeb48784639cfbfe3840dd626cbe7c6f78e4e269b97ad26c055"
)

PINNED_DURABILITY_DIGEST = (
    "4221f3f405085af5ba032af31bfa8c6cce781a6500915a6610d36ed72c911746"
)

PINNED_BOUND = frozenset(
    {
        "ledger_policy_invalid",
        "ledger_policy_symlink",
        "uninstall_receipt_dir_absolute_required",
        "uninstall_receipt_dir_dry_run_conflict",
        "uninstall_receipt_dir_symlinked",
        "uninstall_receipt_dir_unwritable",
        "waiter_arm_identity_invalid",
        "waiter_harness_mismatch",
        "wake_daemon_log_prune_marker_unwritable",
        "wake_daemon_log_prune_receipt_unwritable",
        "wake_daemon_log_receipt_unwritable",
        "wake_daemon_log_rotation_exists",
        "wake_daemon_log_symlink",
        "ack_item_unknown",
        "arguments_invalid",
        "authority_holder_mismatch",
        "deployment_currency_unavailable",
        "door_cancelled",
        "door_terminal_io_failed",
        "gh_authentication_absent",
        "hook_burn_path_shape_hostful",
        "hook_burn_release_stale",
        "hook_confinement_leaked",
        "hook_confinement_release_stale",
        "hook_confinement_result_invalid",
        "hook_precondition_release_unreadable",
        "hook_trust_config_writable",
        "interactive_terminal_required",
        "lifecycle_prompts_name_reserved",
        "lifecycle_prompts_target_foreign",
        "node_add_plan_invalid",
        "node_add_plan_path_invalid",
        "node_add_plan_path_not_absolute",
        "prep_clear_actor_mismatch",
        "prep_clear_architect_unresolved",
        "prep_clear_claim_not_held",
        "prep_clear_complement_invalid",
        "prep_clear_idempotency_conflict",
        "prep_clear_participant_unresolved",
        "prep_clear_pushed_tip_absent",
        "prep_clear_stop_incomplete",
        "prep_clear_workspace_invalid",
        "prep_clear_workspace_unreadable",
        "role_architect_invalid",
        "role_invalid",
        "role_template_edit_invalid",
        "role_template_exists",
        "role_template_idempotency_conflict",
        "role_template_invalid",
        "role_template_name_mismatch",
        "role_template_path_invalid",
        "role_template_reserved",
        "role_template_write_conflict",
        "role_template_write_pending",
        "run_id_invalid",
        "seat_board_claim_contested",
        "seat_board_drain_unknown",
        "seat_board_idempotency_conflict",
        "seat_board_resume_stale",
        "seat_declaration_deaf",
        "sha_unbanked",
        "signature_tool_absent",
        "snapshot_identity_fence_failed",
        "solo_identity_ambiguous",
        "update_check_missing",
        "update_consent_changed",
        "update_consent_epoch_stale",
        "update_consent_missing",
        "update_consent_revoked",
        "update_envelope_too_large",
        "update_http_status",
        "update_install_metadata_missing",
        "update_package_manager_owned",
        "update_redirect_refused",
        "update_rollback_prior_missing",
        "update_rollback_target_mismatch",
        "update_rollback_to_invalid",
        "update_signature_unverified",
        "update_transport_failed",
        "update_trust_changed",
        "update_trust_unprovisioned",
        "update_version_unavailable",
        "version_skew",
        "wait_payload_absent",
        "wait_payload_invalid",
        "wake_daemon_codex_executable_absent",
        "wake_daemon_supervisor_digest_mismatch",
        "wake_daemon_zcode_entry_absent",
        "wake_daemon_zcode_node_absent",
        "wake_idempotency_key_invalid",
        "work_unknown",
    }
)

PINNED_MIXED = frozenset(
    {
        "idempotency_key_invalid",
        "purge_trash_unavailable",
        "recipient_unregistered",
        "repository_invalid",
        "role_template_unknown",
        "wizard_input_invalid",
    }
)

PINNED_UNBOUND = frozenset(
    Path(__file__)
    .with_name("rem_2_unbound_pin.txt")
    .read_text(encoding="utf-8")
    .splitlines()
)
def _set_digest(codes: frozenset[str]) -> str:
    return hashlib.sha256("\n".join(sorted(codes)).encode("utf-8")).hexdigest()


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _const_str(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{dyn}")
        return "".join(parts)
    return None


def _code_arg(call: ast.Call) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == "code":
            return keyword.value
    if call.args:
        return call.args[0]
    return None


def constructor_sites(root: Path) -> list[tuple[str, str, str | None, bool]]:
    """Return `(file, constructor, code, nonempty_remedy)` for each constructor."""

    found: list[tuple[str, str, str | None, bool]] = []
    for path in sorted((root / "floati").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name not in CONSTRUCTORS:
                continue
            found.append(
                (rel, name, _const_str(_code_arg(node)), _nonempty_remedy(_remedy_node(node)))
            )
    return found


def _remedy_node(call: ast.Call) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == "remedy":
            return keyword.value
    if len(call.args) >= 3:
        return call.args[2]
    return None


def _nonempty_remedy(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        if node.value is None:
            return False
        if isinstance(node.value, str):
            return bool(node.value.strip())
        return False
    if isinstance(node, ast.Name) and node.id == "None":
        return False
    return True


def bounded_string_role_invalid(root: Path) -> bool:
    """True when validate_role can serve role_invalid through _bounded_string."""

    path = root / "floati" / "records.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename="floati/records.py")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node.func) != "_bounded_string":
            continue
        if len(node.args) < 4:
            continue
        field = node.args[3]
        if isinstance(field, ast.Constant) and field.value == "role":
            return True
    return False


def census(root: Path) -> dict[str, object]:
    protocol: set[str] = set()
    integrity: set[str] = set()
    durability: set[str] = set()
    unresolved_protocol = 0
    role_literals: set[str] = set()
    site_bounds: dict[str, list[bool]] = {}
    for rel, kind, code, site_bound in constructor_sites(root):
        if code is None:
            if kind == "ProtocolRefusal":
                unresolved_protocol += 1
            continue
        if kind == "ProtocolRefusal":
            protocol.add(code)
            site_bounds.setdefault(code, []).append(site_bound)
            if code == "role_invalid" or code.endswith("role_invalid"):
                if rel not in ROLE_VERB_FILES:
                    role_literals.add(code)
        elif kind == "IntegrityFailure":
            integrity.add(code)
        else:
            durability.add(code)
    if bounded_string_role_invalid(root):
        protocol.add("role_invalid")
        role_literals.add("role_invalid")
        site_bounds.setdefault("role_invalid", []).append(True)
    bound: set[str] = set()
    mixed: set[str] = set()
    unbound: set[str] = set()
    for code in protocol:
        if code in DRILL_REMEDIES:
            bound.add(code)
            continue
        flags = site_bounds.get(code, [])
        if flags and all(flags):
            bound.add(code)
        elif flags and any(flags):
            mixed.add(code)
        else:
            unbound.add(code)
    kind_none = frozenset(unbound | mixed)
    root_shared = frozenset(code for code in kind_none if code.startswith("root_"))
    integrity_durability = frozenset(
        (kind_none & integrity) | (kind_none & durability)
    )
    role_outside = frozenset(role_literals & kind_none)
    other = kind_none - root_shared - integrity_durability - role_outside
    return {
        "protocol": frozenset(protocol),
        "kind_none": kind_none,
        "bound": frozenset(bound),
        "mixed": frozenset(mixed),
        "unbound": frozenset(unbound),
        "unresolved_protocol": unresolved_protocol,
        "integrity": frozenset(integrity),
        "durability": frozenset(durability),
        "root_shared": root_shared,
        "integrity_durability": integrity_durability,
        "role_invalid_outside": role_outside,
        "other": frozenset(other),
    }


class Rem2KindNoneCensusTests(unittest.TestCase):
    def test_constructed_fixture_classes_kind_none_codes(self) -> None:
        """Catches a walker that cannot see constructor literals or role_invalid."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package = root / "floati"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "sample.py").write_text(
            "from .errors import ProtocolRefusal, IntegrityFailure, DurabilityFailure\n"
            "raise ProtocolRefusal('root_required', 'x')\n"
            "raise ProtocolRefusal('operator_role_invalid', 'x')\n"
            "raise IntegrityFailure('record_not_object', 'x')\n"
            "raise DurabilityFailure('root_deleted', 'x')\n",
            encoding="utf-8",
        )
        (package / "records.py").write_text(
            "def _bounded_string(value, minimum, maximum, field, refuse):\n"
            "    refuse(f'{field}_invalid', 'x')\n"
            "def validate_role(value):\n"
            "    _bounded_string(value, 1, 64, 'role', None)\n",
            encoding="utf-8",
        )
        derived = census(root)
        self.assertIn("root_required", derived["kind_none"])
        self.assertIn("root_required", derived["root_shared"])
        self.assertIn("record_not_object", derived["integrity"])
        self.assertIn("operator_role_invalid", derived["role_invalid_outside"])
        self.assertIn("role_invalid", derived["bound"])
        self.assertIn("root_deleted", derived["durability"])

    def test_constructed_fixture_bound_site_is_not_kind_none(self) -> None:
        """Catches bind_refusal_remedy(code, None) ignoring inline remedy=."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package = root / "floati"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "records.py").write_text("", encoding="utf-8")
        (package / "sample.py").write_text(
            "from .errors import ProtocolRefusal\n"
            "raise ProtocolRefusal('always_bound', 'x', remedy='retry with --ref')\n"
            "raise ProtocolRefusal('sometimes_bound', 'x', remedy='retry with --ref')\n"
            "raise ProtocolRefusal('sometimes_bound', 'y')\n"
            "raise ProtocolRefusal('never_bound', 'x')\n",
            encoding="utf-8",
        )
        derived = census(root)
        self.assertNotIn("always_bound", derived["kind_none"])
        self.assertIn("always_bound", derived["bound"])
        self.assertIn("sometimes_bound", derived["mixed"])
        self.assertIn("never_bound", derived["unbound"])

    def test_live_tree_kind_none_census_is_pinned(self) -> None:
        """Catches a new unbound code, or a moved class, without a pin change."""

        derived = census(REPOSITORY_ROOT)
        protocol = derived["protocol"]
        kind_none = derived["kind_none"]
        self.assertEqual(PINNED_PROTOCOL_COUNT, len(protocol), len(protocol))
        self.assertEqual(PINNED_KIND_NONE_COUNT, len(kind_none), len(kind_none))
        self.assertEqual(
            PINNED_UNRESOLVED_PROTOCOL,
            derived["unresolved_protocol"],
            derived["unresolved_protocol"],
        )
        self.assertEqual(
            PINNED_INTEGRITY_COUNT,
            len(derived["integrity"]),
            len(derived["integrity"]),
        )
        self.assertEqual(
            PINNED_DURABILITY_COUNT,
            len(derived["durability"]),
            len(derived["durability"]),
        )
        self.assertEqual(PINNED_ROOT_SHARED, derived["root_shared"])
        self.assertEqual(PINNED_INTEGRITY_DURABILITY, derived["integrity_durability"])
        self.assertEqual(PINNED_ROLE_INVALID_OUTSIDE, derived["role_invalid_outside"])
        self.assertEqual(PINNED_OTHER_COUNT, len(derived["other"]), len(derived["other"]))
        self.assertEqual(PINNED_PROTOCOL_DIGEST, _set_digest(protocol), _set_digest(protocol))
        self.assertEqual(
            PINNED_KIND_NONE_DIGEST, _set_digest(kind_none), _set_digest(kind_none)
        )
        self.assertEqual(
            PINNED_OTHER_DIGEST,
            _set_digest(derived["other"]),
            _set_digest(derived["other"]),
        )
        self.assertEqual(
            PINNED_INTEGRITY_DIGEST,
            _set_digest(derived["integrity"]),
            _set_digest(derived["integrity"]),
        )
        self.assertEqual(
            PINNED_DURABILITY_DIGEST,
            _set_digest(derived["durability"]),
            _set_digest(derived["durability"]),
        )
        self.assertTrue(set(DRILL_REMEDIES) <= protocol)
        self.assertTrue(set(DRILL_REMEDIES) <= derived["bound"])
        self.assertEqual(PINNED_BOUND, derived["bound"])
        self.assertEqual(PINNED_MIXED, derived["mixed"])
        self.assertEqual(PINNED_BOUND_COUNT, len(derived["bound"]), len(derived["bound"]))
        self.assertEqual(PINNED_MIXED_COUNT, len(derived["mixed"]), len(derived["mixed"]))
        self.assertLessEqual(
            len(derived["unbound"]),
            PINNED_UNBOUND_COUNT,
            len(derived["unbound"]),
        )
        arrived_unbound = derived["unbound"] - PINNED_UNBOUND
        self.assertEqual(
            frozenset(),
            arrived_unbound,
            "new unbound codes: " + ", ".join(sorted(arrived_unbound)),
        )


if __name__ == "__main__":
    unittest.main()
