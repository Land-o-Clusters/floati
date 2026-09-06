"""Every refusal the role authoring verbs serve must name an operator act."""
from __future__ import annotations

import ast
import io
import json
import os
import stat
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

from floati.cli import main
from floati.errors import DRILL_REMEDIES, UNNAMED_REMEDY, ProtocolRefusal
from tests.test_role_templates import template_payload


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ROLE_MODULES = ("floati/role_library.py", "floati/role_templates.py")
ROLE_CODE_PREFIX = "role_template_"
# Refusals the verbs reach through validators shared with other commands, so
# they carry no role_template_ prefix and cannot be derived from the role
# modules. Counted, not enumerated as coverage: each is probed below.
SHARED_VALIDATOR_CODES = frozenset({"role_invalid", "wake_idempotency_key_invalid"})
# Counted at this tip. Exact, not a floor: reason codes recur across sites, so
# the per-code assertions below still pass after one duplicate-coded site is
# deleted, and a floor passes as long as the walk still reaches forty. Only an
# equality can see a site leave. Raise it deliberately when a site is added.
DERIVED_ROLE_REFUSAL_SITES = 41


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _refusal_sites(module: str, scopes: list[ast.AST], *, prefix: str = "") -> list[tuple]:
    """Literal ProtocolRefusal sites and whether each one binds a remedy."""

    sites: list[tuple] = []
    for scope in scopes:
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call) or _callee_name(node) != "ProtocolRefusal":
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            code = first.value
            if not code.startswith(prefix):
                continue
            supplied = [
                keyword.value
                for keyword in node.keywords
                if keyword.arg == "remedy"
            ]
            if len(node.args) >= 3:
                supplied.append(node.args[2])
            literal = None
            if supplied and isinstance(supplied[0], ast.Constant):
                literal = supplied[0].value
            sites.append((module, node.lineno, code, bool(supplied), literal))
    return sites


def derived_role_refusal_sites() -> list[tuple]:
    """Role-verb refusal sites derived from the parser wiring, never hand-listed."""

    sites: list[tuple] = []
    for module in ROLE_MODULES:
        tree = ast.parse((REPOSITORY_ROOT / module).read_text(encoding="utf-8"))
        sites.extend(_refusal_sites(module, [tree]))

    admin = REPOSITORY_ROOT / "floati/admin_cli.py"
    tree = ast.parse(admin.read_text(encoding="utf-8"))
    handlers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_name(node) != "set_defaults":
            continue
        target = node.func.value if isinstance(node.func, ast.Attribute) else None
        if not isinstance(target, ast.Name) or not target.id.startswith("role_"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "handler" and isinstance(keyword.value, ast.Name):
                handlers.add(keyword.value.id)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    reached = set(handlers)
    for name in handlers:
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Call):
                called = _callee_name(node)
                if called in functions:
                    reached.add(called)
    sites.extend(
        _refusal_sites("floati/admin_cli.py", [functions[n] for n in sorted(reached)],
                       prefix=ROLE_CODE_PREFIX)
    )
    return sites


class RoleRefusalRemedyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "fleet"
        self.call("init", "--root", str(self.root))

    def call(self, *arguments: str, expected: int = 0) -> dict:
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(list(arguments))
        artifact = json.loads(output.getvalue())
        self.assertEqual(expected, status, artifact)
        return artifact["evidence"]

    def refuse(self, *arguments: str) -> dict:
        return self.call(*arguments, expected=20)

    def assert_names_an_act(self, remedy: object, where: str) -> None:
        self.assertNotEqual(dict(UNNAMED_REMEDY), remedy, f"{where} serves the placeholder")
        self.assertIsInstance(remedy, str, f"{where} serves no action text")
        self.assertTrue(remedy.strip(), f"{where} serves empty guidance")

    def test_every_derived_role_refusal_site_binds_a_remedy(self) -> None:
        """Catches a role refusal site added or left without a bound remedy."""

        sites = derived_role_refusal_sites()
        # Pin the population exactly so a deleted site cannot pass by hiding
        # behind another site that carries the same reason code.
        per_module = Counter(module for module, _, _, _, _ in sites)
        self.assertEqual(
            DERIVED_ROLE_REFUSAL_SITES,
            len(sites),
            f"derived {len(sites)} role refusal sites, pinned "
            f"{DERIVED_ROLE_REFUSAL_SITES}; per module {dict(sorted(per_module.items()))}",
        )
        self.assertEqual(
            frozenset(ROLE_MODULES) | {"floati/admin_cli.py"},
            frozenset(module for module, _, _, _, _ in sites),
        )
        unbound = [
            (module, line, code)
            for module, line, code, supplied, _ in sites
            if not supplied and code not in DRILL_REMEDIES
        ]
        self.assertEqual([], unbound)
        empty = [
            (module, line, code)
            for module, line, code, _, literal in sites
            if isinstance(literal, str) and not literal.strip()
        ]
        self.assertEqual([], empty)

    def test_no_role_reason_code_serves_the_unnamed_remedy_placeholder(self) -> None:
        """Catches any reason code the four verbs serve falling back to the placeholder."""

        derived = frozenset(code for _, _, code, _, _ in derived_role_refusal_sites())
        self.assertEqual(2, len(SHARED_VALIDATOR_CODES))
        root = ("--root", str(self.root))
        source = self.base / "custom.json"
        source.write_text(json.dumps(template_payload("local-review")), encoding="utf-8")
        renamed = self.base / "renamed.json"
        renamed.write_text(json.dumps(template_payload("other-role")), encoding="utf-8")

        self.call("role", "new", *root, "--name", "specialist",
                  "--from", "builder", "--idempotency-key", "seed-1")

        observed: dict[str, dict] = {}
        for code, arguments in (
            ("role_template_reserved",
             ("role", "new", *root, "--name", "builder", "--from", "builder",
              "--idempotency-key", "p-reserved")),
            ("role_template_unknown",
             ("role", "new", *root, "--name", "fresh", "--from", "absent-role",
              "--idempotency-key", "p-unknown")),
            ("role_template_exists",
             ("role", "new", *root, "--name", "specialist", "--from", "builder",
              "--idempotency-key", "p-exists")),
            ("role_template_path_invalid",
             ("role", "validate", *root, "--from", str(self.base / "absent.json"))),
            ("role_template_invalid",
             ("role", "edit", *root, "--name", "specialist", "--set", "duties=invalid",
              "--idempotency-key", "p-invalid")),
            ("role_template_name_mismatch",
             ("role", "edit", *root, "--name", "specialist", "--from", str(renamed),
              "--idempotency-key", "p-mismatch")),
            ("role_template_idempotency_conflict",
             ("role", "new", *root, "--name", "another", "--from", "builder",
              "--idempotency-key", "seed-1")),
            ("role_invalid",
             ("role", "new", *root, "--name", "Not An Id", "--from", "builder",
              "--idempotency-key", "p-name")),
            ("wake_idempotency_key_invalid",
             ("role", "new", *root, "--name", "keyed", "--from", "builder",
              "--idempotency-key", "bad")),
        ):
            with self.subTest(code=code):
                evidence = self.refuse(*arguments)
                self.assertEqual(code, evidence["code"], evidence)
                self.assert_names_an_act(evidence.get("remedy"), code)
                observed[code] = evidence

        for code, refusal in self.library_only_refusals().items():
            with self.subTest(code=code):
                self.assertEqual(code, refusal.code)
                self.assert_names_an_act(refusal.remedy, code)
                observed[code] = {"code": code}

        self.assertEqual(
            derived | SHARED_VALIDATOR_CODES,
            frozenset(observed),
            "a reason code the role verbs can serve has no probe",
        )

    def library_only_refusals(self) -> dict[str, ProtocolRefusal]:
        """Reach the codes argparse's own grouping and the happy path exclude."""

        from floati.role_library import RoleTemplateLibrary
        from floati.root import FloatiRoot

        library = RoleTemplateLibrary(FloatiRoot.open_direct_home(self.root))
        caught: dict[str, ProtocolRefusal] = {}

        with self.assertRaises(ProtocolRefusal) as edit_invalid:
            library.edit("specialist", idempotency_key="p-edit-invalid")
        caught["role_template_edit_invalid"] = edit_invalid.exception

        custom = self.root / "roles/custom"
        mode = stat.S_IMODE(custom.stat().st_mode)
        os.chmod(custom, 0o500)
        try:
            with self.assertRaises(Exception):
                library.new("pending", from_role="builder", idempotency_key="p-pending")
        finally:
            os.chmod(custom, mode)

        with self.assertRaises(ProtocolRefusal) as pending:
            library.new("pending", from_role="builder", idempotency_key="p-other-key")
        caught["role_template_write_pending"] = pending.exception

        # A valid but different template: the library read must survive so the
        # write path, not the parser, is the site under test.
        divergent = dict(template_payload("pending"), cadence="on-demand")
        (custom / "pending.json").write_text(json.dumps(divergent), encoding="utf-8")
        with self.assertRaises(ProtocolRefusal) as conflict:
            library.new("pending", from_role="builder", idempotency_key="p-pending")
        caught["role_template_write_conflict"] = conflict.exception
        return caught


RETIRED_GENERIC_REMEDY = "correct the field named in detail, then repeat the command"


def _payload(**changes: object) -> dict:
    return dict(template_payload(), **changes)


class RoleTemplateValidatorRemedyTests(unittest.TestCase):
    """Every template validator failure must name its own field, not one sentence."""

    # One case per _refuse site in role_templates.py, with the token the
    # remedy has to name for the operator to know which field to act on.
    CASES = (
        ("row is empty text", _payload(duties=[""]), "duties"),
        ("row is terminal-unsafe", _payload(duties=["a\x07b"]), "duties"),
        ("list is empty", _payload(duties=[]), "duties"),
        ("record is not an object", [], "source file"),
        ("field is unknown", _payload(fleet_map={}), "fleet_map"),
        ("schema_version is wrong", _payload(schema_version=1), "schema_version"),
        ("template_version is wrong", _payload(template_version=0), "template_version"),
        ("cadence is invalid", _payload(cadence="Not A Cadence"), "cadence"),
        ("ack_sla_minutes is wrong", _payload(ack_sla_minutes=0), "ack_sla_minutes"),
        ("questions list is empty", _payload(questions=[]), "questions"),
        ("question fields are wrong", _payload(questions=[{"key": "repo"}]), "questions"),
        (
            "question keys repeat",
            _payload(questions=[{"key": "repo", "ask": "a"}, {"key": "repo", "ask": "b"}]),
            "questions",
        ),
    )
    # Cases 1 and 2 are the same field, so they share one remedy; the other
    # ten are distinct. A remedy that stops naming its field collapses this.
    DISTINCT_REMEDIES = 11

    def test_no_template_validator_remedy_serves_the_retired_generic_sentence(self) -> None:
        """Catches the one-sentence remedy returning to any validator failure."""

        from floati.role_templates import parse_role_template

        served: set[str] = set()
        self.assertEqual(12, len(self.CASES))
        for label, payload, expected in self.CASES:
            with self.subTest(case=label):
                with self.assertRaises(ProtocolRefusal) as raised:
                    parse_role_template(payload)
                remedy = raised.exception.remedy
                self.assertEqual("role_template_invalid", raised.exception.code)
                self.assertIsInstance(remedy, str, label)
                self.assertNotEqual(RETIRED_GENERIC_REMEDY, remedy, label)
                self.assertIn(expected, remedy, (label, remedy))
                served.add(remedy)
        self.assertEqual(self.DISTINCT_REMEDIES, len(served), sorted(served))


if __name__ == "__main__":
    unittest.main()
