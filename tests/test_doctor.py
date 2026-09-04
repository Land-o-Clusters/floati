from __future__ import annotations

from tests.test_cli import LAUNCHER

from floati import fixture_ids as public_ids

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.planes import LivenessPresenceStore
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema

from floati.doctor import (
    LAUNCHER_INTERPRETER_CANDIDATES,
    LAUNCHER_INTERPRETER_DECLARATION,
    LAUNCHER_INTERPRETER_SELECTION,
    project_launcher_interpreter,
)

try:
    from floati.doctor import Doctor
except (ImportError, ModuleNotFoundError):
    Doctor = None


NOW = datetime(2026, 8, 1, 14, 0, 0, tzinfo=timezone.utc)


def root_entries(root: Path) -> dict[Path, tuple[str, bytes]]:
    return {
        path.relative_to(root): (
            "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file",
            b"" if path.is_symlink() or path.is_dir() else path.read_bytes(),
        )
        for path in root.rglob("*")
    }


class DoctorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        for relative, content in (
            ("floati/core.py", "VALUE = 1\n"),
            ("schemas/v0/example.json", '{"schema_version":0}\n'),
            ("scripts/floati", "#!/bin/sh\nexit 0\n"),
        ):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (self.source / "scripts/floati").chmod(0o755)
        self._write_manifest()
        self._git("init", "--quiet", "--initial-branch=lane/hm0")
        self._git("config", "user.name", "Floati Test")
        self._git("config", "user.email", "floati-test@example.invalid")
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "source")
        self._git("update-ref", "refs/remotes/origin/lane/hm0", "HEAD")

        self.home = self.base / "alpha"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.node = public_ids.builder('a')
        Registry(self.root).register(self.node, "Codex")
        LivenessPresenceStore(self.root).observe(self.node, 120, NOW)
        (self.home / "nodes" / self.node).mkdir(parents=True)
        self.destination = self.base / "installed"
        destination_scripts = self.destination / "scripts"
        destination_scripts.mkdir(parents=True)
        (destination_scripts / "floati").write_bytes(b"installed\n")
        self.shadow = self.base / "shadow"
        self.shadow.mkdir()
        (self.shadow / "floati").write_bytes(b"shadow\n")
        # CI-GREEN-6(b)/F18: the system components are named /usr/bin explicitly.
        # Deriving a directory from which("git") puts a Homebrew bin (which has
        # no dirname) on the subprocess PATH, where python3 resolves to
        # Homebrew 3.14 on the macOS runner instead of /usr/bin/python3.
        self.assertTrue(Path("/usr/bin/git").is_file())
        self._path_patch = patch.dict(
            os.environ,
            {
                "HOME": str(self.base / "test-home"),
                "PATH": os.pathsep.join((
                    str(self.shadow),
                    str(self.source / "scripts"),
                    str(destination_scripts),
                    "/usr/bin",
                )),
            },
            clear=False,
        )
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)
        # Keep unrelated Doctor contracts independent of the host interpreter;
        # the two interpreter-trust contracts disable this fixture locally.
        self._interpreter_trust_projection_patch = patch(
            "floati.doctor.project_effect_reconciliation_interpreter_trust",
            return_value={
                "code": "effect_reconciliation_interpreter_trust",
                "severity": "ok",
                "subject": "test-fixture:trusted-interpreter",
                "detail": (
                    "Reconciliation can run here. Floati trusts this Python "
                    "and every directory above it."
                ),
                "remediation": None,
            },
        )
        self._interpreter_trust_projection_patch.start()
        self.addCleanup(self._interpreter_trust_projection_patch.stop)

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.source, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def _write_manifest(self) -> None:
        paths = [
            "floati/core.py",
            "schemas/v0/example.json",
            "scripts/floati",
        ]
        payload = {
            "schema_version": 0,
            "protocol_version": "0",
            "canonical_ref": "refs/heads/lane/hm0",
            "files": [
                {
                    "path": relative,
                    "sha256": hashlib.sha256((self.source / relative).read_bytes()).hexdigest(),
                }
                for relative in paths
            ],
        }
        (self.source / "bundle-manifest.v0.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def doctor(
        self,
        root: Path | None = None,
        gateway_config: Path | None = None,
        profile: str | None = None,
        codex_hooks: Path | None = None,
        codex_config: Path | None = None,
        codex_gateway_host: Path | None = None,
        no_sandbox: bool = False,
    ):
        self.assertIsNotNone(Doctor, "typed doctor implementation must exist")
        return Doctor(
            self.source,
            self.home if root is None else root,
            ref="origin/lane/hm0",
            gateway_config=gateway_config,
            destination=self.destination,
            profile=profile,
            codex_hooks=codex_hooks,
            codex_config=codex_config,
            codex_gateway_host=codex_gateway_host,
            no_sandbox=no_sandbox,
        )

    def test_workspace_layout_findings_are_wired_and_read_only(self) -> None:
        """WSL-1: doctor is the product caller for every detected layout defect."""

        workspace = self.home / "nodes" / self.node
        workspace.rmdir()
        orphan = self.home / "nodes" / "orphan"
        orphan.mkdir()
        before = root_entries(self.home)

        artifact, rc = self.doctor(no_sandbox=True).artifact()

        rows = [
            row
            for row in artifact["findings"]
            if row["code"].startswith("node_workspace_")
        ]
        self.assertEqual(
            [
                {
                    "code": "node_workspace_missing",
                    "severity": "warning",
                    "subject": str(workspace.resolve(strict=False)),
                    "detail": f"{self.node}: node workspace missing",
                    "remediation": None,
                },
                {
                    "code": "node_workspace_orphan",
                    "severity": "ok",
                    "subject": str(orphan.resolve()),
                    "detail": "orphan: node workspace orphan",
                    "remediation": None,
                },
            ],
            rows,
        )
        self.assertEqual(35, rc)
        self.assertEqual(before, root_entries(self.home))
        validate_json_schema(
            artifact,
            Path("schemas/v1/doctor-artifact.schema.json"),
        )

    def test_workspace_layout_empty_result_adds_no_json_finding(self) -> None:
        """WSL-1: a conforming layout leaves the doctor finding bytes unchanged."""

        with mock.patch(
            "floati.workspace_layout.inspect_workspace_layout",
            return_value=[],
        ) as inspect:
            artifact, _rc = self.doctor(no_sandbox=True).artifact()

        inspect.assert_called_once_with(self.root)
        self.assertEqual(
            [],
            [
                row
                for row in artifact["findings"]
                if row["code"].startswith("node_workspace_")
                or row["code"] == "nodes_root_invalid"
            ],
        )

    def test_workspace_layout_registry_integrity_failure_stays_typed(self) -> None:
        """WSL-1: wiring the inspector must not turn malformed evidence into a crash."""

        registry = self.home / "registry" / "entries.jsonl"
        registry.write_bytes(b"not a framed ledger\n")

        artifact, rc = self.doctor(no_sandbox=True).artifact()

        self.assertEqual(33, rc)
        self.assertTrue(
            any(row["severity"] == "error" for row in artifact["findings"]),
            artifact["findings"],
        )

    def test_bundle_subjects_collapse_the_fake_operator_home_at_write_time(self) -> None:
        """BUNDLE-1: a doctor artifact must not expose its operator-home prefix."""

        fake_home = self.base / "home" / "operator"
        destination = fake_home / "floati-work" / "work-01a00000000070008000000000000000"
        entrypoint = destination / "scripts" / "floati"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_bytes(b"installed\n")

        with mock.patch.object(Path, "home", return_value=fake_home):
            artifact, _return_code = Doctor(
                self.source,
                self.home,
                ref="origin/lane/hm0",
                destination=destination,
            ).artifact()

        path_codes = {
            "installer_shadow",
            "update_ownership",
            "update_consent",
            "update_last_check",
            "update_last_apply",
        }
        path_subjects = [
            str(row["subject"])
            for row in artifact["findings"]
            if row["code"] in path_codes
        ]
        self.assertTrue(path_subjects)
        self.assertEqual(
            [],
            [subject for subject in path_subjects if not subject.startswith("~/")],
        )
        encoded = json.dumps(
            [str(row["subject"]) for row in artifact["findings"]],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertIn("~/floati-work/work-01a00000000070008000000000000000", encoded)
        self.assertNotIn(str(fake_home), encoded)
        self.assertNotIn("\x2fUsers/", encoded)
        self.assertNotIn("/home/", encoded)

    def test_non_home_subject_json_bytes_are_unchanged(self) -> None:
        """BUNDLE-1: redaction must not rewrite a destination outside HOME."""

        from floati.doctor import _finding as doctor_finding
        from floati.update_status import _finding as update_finding

        expected = (
            b'{"code":"outside","detail":"unchanged","remediation":null,'
            b'"severity":"ok","subject":"/opt/floati"}'
        )
        findings = (
            doctor_finding("outside", "ok", "/opt/floati", "unchanged"),
            update_finding("outside", Path("/opt/floati"), "unchanged"),
        )
        for finding in findings:
            with self.subTest(finding=finding):
                self.assertEqual(
                    expected,
                    json.dumps(
                        finding,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("ascii"),
                )

    def test_subject_writer_census_pins_the_two_path_capable_helpers(self) -> None:
        """BUNDLE-1: a new subject writer cannot bypass the artifact path fence."""

        production = Path(__file__).resolve().parents[1] / "floati"
        writers: list[tuple[str, str, bool]] = []

        class SubjectWriterVisitor(ast.NodeVisitor):
            def __init__(self, relative: str) -> None:
                self.relative = relative
                self.functions: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.functions.append(node.name)
                self.generic_visit(node)
                self.functions.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Dict(self, node: ast.Dict) -> None:
                for key, value in zip(node.keys, node.values):
                    if not (
                        isinstance(key, ast.Constant)
                        and key.value == "subject"
                    ):
                        continue
                    helper_call = (
                        isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Name)
                        and value.func.id == "artifact_subject"
                    )
                    writers.append(
                        (
                            self.relative,
                            self.functions[-1] if self.functions else "<module>",
                            helper_call,
                        )
                    )
                self.generic_visit(node)

        for path in sorted(production.glob("*.py")):
            relative = path.relative_to(production.parent).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            SubjectWriterVisitor(relative).visit(tree)

        self.assertEqual(
            [
                ("floati/delivery_health.py", "analyze", False),
                ("floati/delivery_health.py", "analyze", False),
                ("floati/doctor.py", "_finding", True),
                ("floati/doctor_probe.py", "run", False),
                ("floati/doctor_probe.py", "run", False),
                ("floati/update_status.py", "_finding", True),
            ],
            sorted(writers),
        )

        workspace_tree = ast.parse(
            (production / "workspace_layout.py").read_text(encoding="utf-8"),
            filename="floati/workspace_layout.py",
        )
        workspace_path_codes = sorted(
            {
                node.value
                for node in ast.walk(workspace_tree)
                if isinstance(node, ast.Constant)
                and node.value in {
                    "node_workspace_missing",
                    "node_workspace_orphan",
                }
            }
        )
        self.assertEqual(
            ["node_workspace_missing", "node_workspace_orphan"],
            workspace_path_codes,
        )

        doctor_tree = ast.parse(
            (production / "doctor.py").read_text(encoding="utf-8"),
            filename="floati/doctor.py",
        )
        workspace_projections = [
            node
            for node in doctor_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_workspace_layout_finding"
        ]
        # PIN-DRAFT-1: allowing (0, 1) meant the inner pin never ran on a tree
        # that lacked the function. This tree has the function; require it.
        self.assertEqual(1, len(workspace_projections))
        projection_calls = [
            node
            for node in ast.walk(workspace_projections[0])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_finding"
        ]
        self.assertEqual(1, len(projection_calls))
        subject = projection_calls[0].args[2]
        # Compare unparse to unparse: ast.unparse renders string constants
        # with single quotes, so a hand-typed double-quoted literal never
        # matches and this branch reds on every composition that reaches it
        # (it did, in train 0902x). The expected form is derived from the
        # same renderer, so the pin is about the CALL and not about quoting.
        expected_subject = ast.unparse(
            ast.parse('str(row["path"])', mode="eval").body
        )
        self.assertEqual(expected_subject, ast.unparse(subject))

    def _assert_typed_launcher_doctor_outcome(self, result) -> dict:
        """LAUNCH-1: a doctor started through scripts/floati is asserted by TYPE.

        Pinning ``rc == 0`` asserts a property of the runner's Python, not of
        doctor: on a host whose ruled interpreter fails the root-trust fence the
        artifact is correct and the exit code is 35. What is ruled is the
        BICONDITIONAL - the artifact's own interpreter-trust finding decides the
        outcome, and nothing else on this fixture may. A trust finding that is ok
        still demands rc 0 and state healthy, so any other check degrading this
        run is caught exactly as before; a trust warning demands 35 and
        degraded. No finding may reach severity error either way.

        The launcher's own provenance is asserted unconditionally, because that
        is this row's property and it does not vary with the host.
        """

        self.assertIn(
            result.returncode,
            (0, 35),
            f"rc={result.returncode} stderr={result.stderr}",
        )
        envelope = json.loads(result.stdout)
        self.assertEqual("doctor", envelope["command"])
        evidence = envelope["evidence"]
        validate_json_schema(evidence, Path("schemas/v1/doctor-artifact.schema.json"))

        launcher = next(
            row for row in evidence["findings"] if row["code"] == "launcher_interpreter"
        )
        self.assertEqual("ok", launcher["severity"], launcher["detail"])
        self.assertTrue(
            launcher["detail"].startswith(("declared: ", "candidate: ")),
            f"the launcher's interpreter came from no ruled source: {launcher['detail']}",
        )
        self.assertIn(
            launcher["subject"],
            (os.environ.get(LAUNCHER_INTERPRETER_DECLARATION),
             *LAUNCHER_INTERPRETER_CANDIDATES),
        )

        self.assertEqual(
            [],
            [row["code"] for row in evidence["findings"] if row["severity"] == "error"],
        )
        trust = self._interpreter_trust_finding(evidence)
        if trust["severity"] == "ok":
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("healthy", evidence["state"])
            return evidence

        self.assertEqual("warning", trust["severity"])
        self.assertEqual(35, result.returncode, result.stderr)
        self.assertEqual("degraded", evidence["state"])
        print(
            "TYPED HOST FACT launcher-interpreter: the launcher chose "
            f"{launcher['subject']}, running {trust['subject']}, which this host does "
            f"not root-trust at {trust['interpreter_trust']['failing_component']} "
            f"[uid {trust['interpreter_trust']['component_uid']}]",
            file=sys.stderr,
        )
        return evidence

    @staticmethod
    def _interpreter_trust_finding(artifact: dict) -> dict:
        return next(
            row
            for row in artifact["findings"]
            if row["code"] == "effect_reconciliation_interpreter_trust"
        )

    def test_doctor_reports_trusted_reconciliation_interpreter(self) -> None:
        """Catches a runnable reconciliation host remaining absent from preflight."""
        import floati.effect_reconciliation_exec as reconciliation_exec

        interpreter = "/test-fixture/python3"
        self._interpreter_trust_projection_patch.stop()
        with mock.patch.object(sys, "executable", interpreter):
            with mock.patch.object(
                reconciliation_exec,
                "_freeze_trusted_interpreter",
                return_value=Path(interpreter),
            ):
                artifact, _ = self.doctor(no_sandbox=True).artifact()

        finding = self._interpreter_trust_finding(artifact)

        self.assertEqual("ok", finding["severity"])
        self.assertEqual(interpreter, finding["subject"])
        self.assertEqual(
            "Reconciliation can run here. Floati trusts this Python and every directory above it.",
            finding["detail"],
        )
        self.assertIsNone(finding["remediation"])
        self.assertNotIn("interpreter_trust", finding)

    def test_doctor_names_untrusted_reconciliation_interpreter_component(self) -> None:
        """Catches a permanent interpreter refusal remaining component-free."""
        self._interpreter_trust_projection_patch.stop()
        directory = self.base / "untrusted-python"
        directory.mkdir()
        interpreter = directory / "python3"
        interpreter.write_bytes(b"#!/bin/sh\nexit 0\n")
        interpreter.chmod(0o700)
        canonical = Path(os.path.realpath(interpreter))
        failing_component = canonical.parent
        metadata = os.lstat(failing_component)
        expected_observation = {
            "component_mode": metadata.st_mode,
            "component_uid": metadata.st_uid,
            "failing_component": str(failing_component),
            "interpreter_path": str(canonical),
        }

        with mock.patch.object(sys, "executable", str(interpreter)):
            artifact, rc = self.doctor(no_sandbox=True).artifact()

        finding = self._interpreter_trust_finding(artifact)
        self.assertEqual(35, rc)
        self.assertEqual("warning", finding["severity"])
        self.assertEqual(str(canonical), finding["subject"])
        self.assertEqual(
            (
                "Reconciliation cannot run, so every effect stays unknown. "
                f"Floati will not trust {failing_component}."
            ),
            finding["detail"],
        )
        self.assertEqual(
            (
                "Every directory above the Python must be a real directory owned by root "
                "and writable by nobody else. Run Floati with a root-owned Python; on "
                "macOS that is /usr/bin/python3."
            ),
            finding["remediation"],
        )
        self.assertEqual(expected_observation, finding["interpreter_trust"])

    def _vendor_codex_gateway(self, content: bytes) -> None:
        vendored = self.source / "tools/codex/codex-fleet-bus.py"
        vendored.parent.mkdir(parents=True, exist_ok=True)
        vendored.write_bytes(content)
        (vendored.parent / "codex-fleet-bus.sha256.json").write_text(
            json.dumps(
                {
                    "path": "tools/codex/codex-fleet-bus.py",
                    "schema_version": 0,
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self._git("add", "tools/codex")
        self._git("commit", "--quiet", "-m", "vendor gateway fixture")
        self._git("update-ref", "refs/remotes/origin/lane/hm0", "HEAD")

    def _vendor_bus_watch(self, content: bytes) -> Path:
        path = self.source / "scripts" / "bus-watch" / "floati-bus-watch.ts"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self._git("add", str(path.relative_to(self.source)))
        self._git("commit", "--quiet", "-m", "vendor bus-watch fixture")
        self._git("update-ref", "refs/remotes/origin/lane/hm0", "HEAD")
        return path

    def _remove_presence(self) -> None:
        self.root.resolve_relative(public_ids.compose('liveness-presence/', public_ids.ledger(public_ids.builder('a')))).unlink()

    def _codes(self, artifact: dict) -> list[str]:
        return [row["code"] for row in artifact["findings"]]

    def test_absent_presence_still_degrades_when_no_profile_is_named(self) -> None:
        """No silent default change: absence without a declared profile still warns."""
        self._remove_presence()

        artifact, rc = self.doctor().artifact()

        self.assertEqual(35, rc)
        self.assertEqual("degraded", artifact["state"])
        self.assertIn("registry_live_dirs_mismatch", self._codes(artifact))
        self.assertNotIn("registry_live_dirs_expected_absent", self._codes(artifact))

    def test_codex_gateway_digest_match_is_a_stamped_ok_fact(self) -> None:
        """A doctor run that cannot attest the installed gateway source is rejected."""

        content = b"governed gateway\n"
        self._vendor_codex_gateway(content)
        host = self.base / "codex-fleet-bus"
        host.write_bytes(content)

        artifact, rc = self.doctor(codex_gateway_host=host).artifact()

        self.assertEqual(0, rc)
        finding = next(
            row for row in artifact["findings"]
            if row["code"] == "codex_gateway_vendored_source_current"
        )
        self.assertEqual("ok", finding["severity"])
        self.assertIsNone(finding["remediation"])

    def test_codex_gateway_host_drift_degrades_with_explicit_install_remedy(self) -> None:
        """A hand-edited host wrapper remaining invisible to doctor is rejected."""

        self._vendor_codex_gateway(b"governed gateway\n")
        host = self.base / "codex-fleet-bus"
        host.write_bytes(b"hand edited\n")

        artifact, rc = self.doctor(codex_gateway_host=host).artifact()

        self.assertEqual(35, rc)
        finding = next(
            row for row in artifact["findings"]
            if row["code"] == "codex_gateway_vendored_source_drift"
        )
        self.assertEqual("warning", finding["severity"])
        self.assertIn("gateway drifted from vendored source", finding["detail"])
        self.assertIn("complete source tree", finding["remediation"])

    def test_absent_optional_codex_gateway_is_a_typed_ok_absence(self) -> None:
        """A clean host has not installed an optional harness integration yet."""

        self._vendor_codex_gateway(b"governed gateway\n")
        host = self.base / "codex-fleet-bus-not-installed"

        artifact, rc = self.doctor(codex_gateway_host=host).artifact()

        self.assertEqual(0, rc)
        finding = next(
            row for row in artifact["findings"]
            if row["code"] == "codex_gateway_vendored_source_missing"
        )
        self.assertEqual("ok", finding["severity"])
        self.assertIsNone(finding["remediation"])

    def test_absent_presence_still_degrades_when_the_directory_never_existed(self) -> None:
        """Catches doctor inferring a bus-only fleet from a missing directory."""
        self._remove_presence()
        shutil.rmtree(self.root.resolve_relative("liveness-presence"))

        artifact, rc = self.doctor().artifact()

        self.assertEqual(35, rc)
        self.assertIn("registry_live_dirs_mismatch", self._codes(artifact))

    def test_declared_bus_only_renders_expected_absence_as_an_ok_row(self) -> None:
        """The chartered fix: declared session-based fleets reach GREEN honestly."""
        self._remove_presence()

        artifact, rc = self.doctor(profile="bus-only").artifact()

        self.assertEqual(0, rc)
        self.assertEqual("healthy", artifact["state"])
        self.assertNotIn("registry_live_dirs_mismatch", self._codes(artifact))
        row = next(
            item for item in artifact["findings"]
            if item["code"] == "registry_live_dirs_expected_absent"
        )
        self.assertEqual("ok", row["severity"])
        self.assertIsNone(row["remediation"])

    def test_fresh_solo_root_without_runtime_presence_is_expected_idle(self) -> None:
        """Registration is durable identity; it does not claim a live waiter."""
        from floati.solo import initialize_solo

        solo_home = self.base / "fresh-solo"
        solo_root = FloatiRoot.open_direct_home(solo_home, create=True)
        initialize_solo(solo_root, "me", "Codex", NOW)

        artifact, rc = self.doctor(root=solo_home, no_sandbox=True).artifact()

        self.assertEqual(0, rc)
        row = next(
            item for item in artifact["findings"]
            if item["code"] == "registry_live_dirs_expected_idle"
        )
        self.assertEqual("ok", row["severity"])
        self.assertIsNone(row["remediation"])

    def test_doctor_accepts_a_node_lease_written_to_the_registry_ledger(self) -> None:
        """Catches doctor declaring a narrower vocabulary than the registry writer."""
        append_record(
            self.root,
            "registry/entries.jsonl",
            {
                "schema_version": 0,
                "id": "lease-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": "2026-08-01T14:00:00.000Z",
                "kind": "node_lease",
                "node_id": public_ids.builder('a'),
                "workspace": str(self.root.path / "nodes" / public_ids.builder('a')),
                "expires_at": "2026-08-01T14:01:00.000Z",
                "state": "active",
            },
            allowed_kinds=set(REGISTRY_KINDS),
        )

        artifact, rc = self.doctor().artifact()

        self.assertEqual(0, rc)
        self.assertEqual("healthy", artifact["state"])
        self.assertNotIn("record_kind_invalid", self._codes(artifact))

    def test_declared_bus_only_keeps_warning_when_presence_is_only_partly_absent(self) -> None:
        """Honest-absence law: absence is stated, never guessed from a partial set."""
        Registry(self.root).register(public_ids.builder('b'), "Codex")

        artifact, rc = self.doctor(profile="bus-only").artifact()

        self.assertEqual(35, rc)
        self.assertIn("registry_live_dirs_mismatch", self._codes(artifact))
        self.assertNotIn("registry_live_dirs_expected_absent", self._codes(artifact))

    def test_declared_bus_only_does_not_mask_a_genuine_presence_match(self) -> None:
        """A bus-only declaration must not rewrite testimony that already agrees."""
        artifact, rc = self.doctor(profile="bus-only").artifact()

        self.assertEqual(0, rc)
        self.assertIn("registry_live_dirs_match", self._codes(artifact))
        self.assertNotIn("registry_live_dirs_expected_absent", self._codes(artifact))

    def test_declared_orchestration_profile_keeps_the_warning(self) -> None:
        """Orchestration-mode fleets keep the existing degraded testimony."""
        self._remove_presence()

        artifact, rc = self.doctor(profile="orchestration").artifact()

        self.assertEqual(35, rc)
        self.assertIn("registry_live_dirs_mismatch", self._codes(artifact))

    def test_an_unruled_profile_refuses_before_any_artifact_is_produced(self) -> None:
        """Catches a typo silently selecting a lenient aggregate."""
        for invalid in ("bus only", "BUS-ONLY", "session", ""):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.doctor(profile=invalid)
                self.assertEqual("doctor_profile_invalid", caught.exception.code)

    def test_explicit_codex_hook_trust_is_reported_per_hook(self) -> None:
        from floati.codex_hook_trust import codex_hook_current_hash

        hooks_path = self.base / ".codex" / "hooks.json"
        hooks_path.parent.mkdir()
        block = {
            "hooks": [{
                "type": "command",
                "command": "/usr/bin/python3 /opt/floati-codex-wait --root \x2ftmp/fleet",
                "timeout": 1800,
                "statusMessage": "Watching Floati bus",
            }]
        }
        hooks_path.write_text(
            json.dumps({"hooks": {"Stop": [block]}}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = hooks_path.with_name("config.toml")
        key = f"{hooks_path}:stop:0:0"
        current_hash = codex_hook_current_hash(block)
        self.assertEqual(
            "ac3a5c59887f923baa872af6eb031650bfcfd4b4d27461c746c2af530f7645ca",
            current_hash,
        )
        config_path.write_text(
            "[hooks.state." + json.dumps(key) + "]\n"
            "trusted_hash = " + json.dumps(current_hash) + "\n",
            encoding="utf-8",
        )

        artifact, rc = self.doctor(
            codex_hooks=hooks_path,
            codex_config=config_path,
        ).artifact()

        self.assertEqual(0, rc)
        row = next(
            item for item in artifact["findings"]
            if item["code"] == "codex_wait_hook_trust"
        )
        self.assertEqual("trusted", row["hook_trust"])
        self.assertEqual(key, row["hook_trust_key"])

        config_path.write_text(
            "[hooks.state." + json.dumps(key) + "]\n"
            "trusted_hash = " + json.dumps("sha256:" + current_hash) + "\n",
            encoding="utf-8",
        )
        prefixed, prefixed_rc = self.doctor(
            codex_hooks=hooks_path,
            codex_config=config_path,
        ).artifact()
        prefixed_row = next(
            item for item in prefixed["findings"]
            if item["code"] == "codex_wait_hook_trust"
        )
        self.assertEqual(0, prefixed_rc)
        self.assertEqual("trusted", prefixed_row["hook_trust"])
        self.assertEqual(current_hash, prefixed_row["hook_trust_current_hash"])

        config_path.write_text(
            "[hooks.state." + json.dumps(key) + "]\n"
            "trusted_hash = \"sha256:" + "0" * 64 + "\"\n",
            encoding="utf-8",
        )
        before = config_path.read_bytes()
        degraded, degraded_rc = self.doctor(
            codex_hooks=hooks_path,
            codex_config=config_path,
        ).artifact()
        degraded_row = next(
            item for item in degraded["findings"]
            if item["code"] == "codex_wait_hook_trust"
        )
        self.assertEqual(35, degraded_rc)
        self.assertEqual("untrusted_pending_user", degraded_row["hook_trust"])
        self.assertFalse(degraded_row["hook_armed"])
        self.assertTrue(degraded_row["remediation"])
        self.assertNotIn("DRAFT - ", degraded_row["remediation"])
        self.assertEqual(before, config_path.read_bytes())

    def test_codex_hook_trust_runs_by_default_from_the_codex_home(self) -> None:
        """Catches bare doctor omitting the only check that can name a trust lapse."""
        from floati.codex_hook_trust import codex_hook_current_hash

        codex_home = self.base / "default-codex-home"
        hooks_path = codex_home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        block = {
            "hooks": [{
                "type": "command",
                "command": "/usr/bin/python3 /opt/floati-codex-wait --root \x2ftmp/fleet",
                "timeout": 1800,
                "statusMessage": "Watching Floati bus",
            }]
        }
        hooks_path.write_text(
            json.dumps({"hooks": {"Stop": [block]}}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        key = f"{hooks_path}:stop:0:0"
        hooks_path.with_name("config.toml").write_text(
            "[hooks.state." + json.dumps(key) + "]\n"
            "trusted_hash = " + json.dumps(codex_hook_current_hash(block)) + "\n",
            encoding="utf-8",
        )

        with mock.patch("floati.doctor.Path.home", return_value=codex_home):
            artifact, rc = self.doctor().artifact()

        self.assertEqual(0, rc)
        rows = [
            row for row in artifact["findings"]
            if row["code"] == "codex_wait_hook_trust"
        ]
        self.assertEqual(1, len(rows))
        self.assertEqual("trusted", rows[0]["hook_trust"])
        self.assertEqual(key, rows[0]["hook_trust_key"])
        validate_json_schema(
            artifact,
            Path("schemas/v1/doctor-artifact.schema.json"),
        )

    def test_default_codex_hook_absence_is_visible_without_degrading_doctor(self) -> None:
        """Catches a non-Codex host becoming either silent or spuriously unhealthy."""
        codex_home = self.base / "codex-not-installed"
        codex_home.mkdir()

        with mock.patch("floati.doctor.Path.home", return_value=codex_home):
            artifact, rc = self.doctor().artifact()

        self.assertEqual(0, rc)
        rows = [
            item for item in artifact["findings"]
            if item["code"] == "codex_wait_hook_trust"
        ]
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("ok", row["severity"])
        self.assertEqual("~/.codex/hooks.json", row["subject"])
        self.assertIsNone(row["remediation"])

    def test_cli_requires_the_explicit_profile_flag_and_refuses_an_unruled_value(self) -> None:
        self._remove_presence()
        # LAUNCH-1: the CLI is reached through the launcher. Spelling this
        # `python3 -m floati` made the test's own argv a bare PATH lookup - the
        # discovery the executable-provenance policy forbids - so the interpreter
        # under test was whatever the ambient PATH happened to name.
        base = [
            "scripts/floati", "doctor",
            "--root", str(self.home),
            "--source", str(self.source),
            "--ref", "origin/lane/hm0",
            "--destination", str(self.destination),
        ]

        degraded = subprocess.run(base, capture_output=True, text=True, check=False)
        self.assertEqual(35, degraded.returncode)

        healthy = subprocess.run(
            [*base, "--profile", "bus-only"], capture_output=True, text=True, check=False
        )
        evidence = self._assert_typed_launcher_doctor_outcome(healthy)
        self.assertIn(
            "registry_live_dirs_expected_absent",
            [row["code"] for row in evidence["findings"]],
        )

        refused = subprocess.run(
            [*base, "--profile", "nonsense"], capture_output=True, text=True, check=False
        )
        self.assertEqual(20, refused.returncode)
        self.assertEqual(
            "doctor_profile_invalid",
            json.loads(refused.stdout)["evidence"]["code"],
        )

    def test_registered_set_folds_latest_row_wins_and_excludes_retired_nodes(self) -> None:
        """Catches doctor counting a retired node because its old active row remains."""
        registry = Registry(self.root)
        registry.register(public_ids.builder('b'), "Codex")
        registry.retire(public_ids.builder('b'))

        artifact, rc = self.doctor().artifact()

        self.assertEqual((public_ids.builder('a'),), registry.active_node_ids())
        self.assertEqual(0, rc, "a retired node with no presence file is not a mismatch")
        self.assertIn("registry_live_dirs_match", self._codes(artifact))

    def test_wake_namespace_must_be_a_physically_read_only_subset_of_registry_lineage(self) -> None:
        """Catches an unregistered spelling minting a healthy-looking wake coordinator."""
        canonical = self.root.resolve_relative(
            public_ids.compose('receipts/wake-coordination/', public_ids.builder('a'), '/lane.lock')
        )
        alias = self.root.resolve_relative(
            "receipts/wake-coordination/lane_alias/lane.lock"
        )
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(b"")
        alias.parent.mkdir(parents=True)
        alias.write_bytes(b"")
        before = root_entries(self.home)

        artifact, rc = self.doctor().artifact()

        self.assertEqual(35, rc)
        self.assertEqual(before, root_entries(self.home))
        finding = next(
            row for row in artifact["findings"]
            if row["code"] == "wake_namespace_registry_mismatch"
        )
        self.assertEqual("warning", finding["severity"])
        self.assertIn("lane_alias", finding["detail"])
        self.assertNotIn(public_ids.compose(public_ids.builder('a'), ']'), finding["detail"])

    # WD-2 wake-doctor health: the runtime the daemon already writes
    # (consecutive_refusals, current_backoff, breaker state, last typed
    # outcome) must surface in doctor; absence must be typed, never silence.

    def _bind_wake_daemon(
        self, node: str, harness: str, session_id: str, *, resume_state: str | None = None
    ) -> None:
        from floati.wake_daemon_adapters import adapter_contract_digest
        from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate

        coordinate = DaemonCoordinate(self.root, node, harness)
        AdapterBindingStore(self.root).write(
            coordinate,
            session_id=session_id,
            workspace=self.home,
            executable=self.source / "scripts/floati",
            adapter_version="1",
            adapter_digest=adapter_contract_digest(harness),
            binding_epoch=1,
            resume_state=resume_state,
        )

    def _write_wake_runtime(
        self,
        node: str,
        harness: str,
        session_id: str,
        *,
        circuit_state: str,
        consecutive_refusals: int,
        last_state: str = "idle",
        last_reason_code: str | None = None,
    ) -> None:
        from floati.wake_daemon_contract import DaemonCoordinate

        coordinate = DaemonCoordinate(self.root, node, harness)
        payload = {
            "schema_version": 0,
            "tenant_id": self.root.tenant_id,
            "node_id": coordinate.node_id,
            "harness": harness,
            "coordinate_digest": coordinate.digest,
            "daemon_instance_id": "daemon-test",
            "activation_epoch": 1,
            "cycle_index": 3,
            "current_wake_key": None,
            "consecutive_refusals": consecutive_refusals,
            "circuit_state": circuit_state,
            "next_poll_at": 0,
            "current_backoff": 5,
            "wake_timestamps": [],
            "session_digest": hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
            "last_state": last_state,
            "last_reason_code": last_reason_code,
            "last_lifecycle_receipt_id": "receipt-test",
        }
        path = self.root.resolve_relative(
            Path("state/wake-daemon/runtime") / f"{coordinate.digest}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _wake_health_finding(self, artifact: dict) -> dict:
        return next(
            row for row in artifact["findings"] if row["code"] == "wake_daemon_health"
        )

    def test_wake_daemon_health_is_a_typed_absence_when_no_daemon_is_bound(self) -> None:
        """WD-2 GAP 2 RED pin: today doctor emits no wake-health finding at all."""
        artifact, rc = self.doctor().artifact()

        finding = self._wake_health_finding(artifact)
        self.assertEqual("ok", finding["severity"])
        self.assertIn("no wake daemon is bound", finding["detail"])
        self.assertEqual(0, rc)

    def test_wake_daemon_health_reports_ok_from_a_healthy_bound_runtime(self) -> None:
        self._bind_wake_daemon(public_ids.builder("a"), "cursor", "worker-cursor-session")
        self._write_wake_runtime(
            public_ids.builder("a"), "cursor", "worker-cursor-session",
            circuit_state="closed", consecutive_refusals=0,
        )

        artifact, rc = self.doctor().artifact()

        finding = self._wake_health_finding(artifact)
        self.assertEqual("ok", finding["severity"])
        self.assertIn(f"{public_ids.builder('a')}/cursor", finding["subject"])
        self.assertIn("closed", finding["detail"])
        self.assertEqual(0, rc)

    def test_wake_daemon_health_warns_when_breaker_is_open(self) -> None:
        self._bind_wake_daemon(public_ids.builder("a"), "cursor", "worker-cursor-session")
        self._write_wake_runtime(
            public_ids.builder("a"), "cursor", "worker-cursor-session",
            circuit_state="open", consecutive_refusals=19,
            last_state="refused", last_reason_code="wake_daemon_adapter_timeout",
        )

        artifact, rc = self.doctor().artifact()

        finding = self._wake_health_finding(artifact)
        self.assertEqual("warning", finding["severity"])
        self.assertIn("open", finding["detail"])
        self.assertIn("consecutive_refusals=19", finding["detail"])
        self.assertIn("current_backoff=5", finding["detail"])
        self.assertIn("wake_daemon_adapter_timeout", finding["detail"])
        self.assertIsNotNone(finding["remediation"])
        self.assertEqual(35, rc)

    def test_wake_daemon_health_warns_at_the_refusal_threshold_even_when_closed(self) -> None:
        grok_session = public_ids.compose(public_ids.verifier(), "-session-1")
        self._bind_wake_daemon(public_ids.builder("a"), "grok-build", grok_session)
        self._write_wake_runtime(
            public_ids.builder("a"), "grok-build", grok_session,
            circuit_state="closed", consecutive_refusals=3,
            last_state="refused", last_reason_code="wake_daemon_adapter_nonzero",
        )

        artifact, rc = self.doctor().artifact()

        finding = self._wake_health_finding(artifact)
        self.assertEqual("warning", finding["severity"])
        self.assertEqual(35, rc)

    def test_wake_daemon_health_states_a_bound_daemon_without_runtime_as_ok(self) -> None:
        self._bind_wake_daemon(public_ids.builder("a"), "cursor", "worker-cursor-session")

        artifact, rc = self.doctor().artifact()

        finding = self._wake_health_finding(artifact)
        self.assertEqual("ok", finding["severity"])
        self.assertIn("runtime is absent", finding["detail"])
        self.assertEqual(0, rc)

    def test_wake_daemon_health_reports_malformed_runtime_as_malformed_evidence(self) -> None:
        from floati.wake_daemon_contract import DaemonCoordinate

        self._bind_wake_daemon(public_ids.builder("a"), "cursor", "worker-cursor-session")
        runtime_path = self.root.resolve_relative(
            Path("state/wake-daemon/runtime")
            / f"{DaemonCoordinate(self.root, public_ids.builder('a'), 'cursor').digest}.json"
        )
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text("{not json", encoding="utf-8")

        artifact, rc = self.doctor().artifact()

        finding = self._wake_health_finding(artifact)
        self.assertEqual("error", finding["severity"])
        self.assertEqual(33, rc)

    def test_wake_daemon_health_names_a_suspect_binding_with_its_session(self) -> None:
        """WD-R5c: the always-on backstop's verdict is doctor-visible - a
        resume_suspect binding is a warning naming the bound session, even
        while the breaker itself is quiet."""
        session_id = "worker-cursor-session"
        self._bind_wake_daemon(
            public_ids.builder("a"), "cursor", session_id,
            resume_state="resume_suspect",
        )
        self._write_wake_runtime(
            public_ids.builder("a"), "cursor", session_id,
            circuit_state="closed", consecutive_refusals=0,
        )

        artifact, rc = self.doctor().artifact()

        finding = self._wake_health_finding(artifact)
        self.assertEqual("warning", finding["severity"])
        self.assertIn("resume_suspect", finding["detail"])
        self.assertIn(session_id, finding["detail"])
        self.assertEqual(35, rc)

    def test_wake_daemon_health_records_a_proven_resume_in_the_detail(self) -> None:
        self._bind_wake_daemon(
            public_ids.builder("a"), "cursor", "worker-cursor-session",
            resume_state="resume_proven",
        )
        self._write_wake_runtime(
            public_ids.builder("a"), "cursor", "worker-cursor-session",
            circuit_state="closed", consecutive_refusals=0,
        )

        artifact, rc = self.doctor().artifact()

        finding = self._wake_health_finding(artifact)
        self.assertEqual("ok", finding["severity"])
        self.assertIn("resume_state=resume_proven", finding["detail"])
        self.assertEqual(0, rc)

    def test_installed_bridge_drift_is_reported_with_both_coordinates(self) -> None:
        """HT-R6: a deployed copy carries the coordinate it was deployed
        from - drift is stated with both SHAs, never a refusal."""
        import floati.doctor as doctor_module

        hooks = self.base / "installed-hooks"
        hooks.mkdir()
        bridge = hooks / "stop-hook-bridge.py"
        bridge.write_bytes(b"installed bytes\n")
        sidecar = hooks / "stop-hook-bridge.sha256"
        sidecar.write_text(hashlib.sha256(b"installed bytes\n").hexdigest() + "\n")
        (self.source / "hooks").mkdir()
        (self.source / "hooks" / "stop-hook-bridge.py").write_bytes(b"repository bytes\n")

        with mock.patch.object(
            doctor_module, "installed_bridge_paths", return_value=(bridge, sidecar)
        ):
            artifact, rc = self.doctor().artifact()

        finding = next(row for row in artifact["findings"] if row["code"] == "wake_bridge_drift")
        self.assertEqual("warning", finding["severity"])
        self.assertIn("installed wake bridge from", finding["detail"])
        self.assertIn("repository at", finding["detail"])

    def test_installed_bridge_in_sync_is_an_ok_fact(self) -> None:
        import floati.doctor as doctor_module

        hooks = self.base / "installed-hooks"
        hooks.mkdir()
        bridge = hooks / "stop-hook-bridge.py"
        bridge.write_bytes(b"same bytes\n")
        sidecar = hooks / "stop-hook-bridge.sha256"  # pre-HT-R6 install: no sidecar yet
        (self.source / "hooks").mkdir()
        (self.source / "hooks" / "stop-hook-bridge.py").write_bytes(b"same bytes\n")
        # keep the fixture source clean and current: a dirty tree reads as a
        # currency warning and would mask the ok row under test
        self._git("add", "hooks/stop-hook-bridge.py")
        self._git("commit", "--quiet", "-m", "hooks fixture")
        self._git("update-ref", "refs/remotes/origin/lane/hm0", "HEAD")

        with mock.patch.object(
            doctor_module, "installed_bridge_paths", return_value=(bridge, sidecar)
        ):
            artifact, rc = self.doctor().artifact()

        finding = next(row for row in artifact["findings"] if row["code"] == "wake_bridge_current")
        self.assertEqual("ok", finding["severity"])
        self.assertEqual(0, rc)

    def test_absent_installed_bridge_is_a_typed_absence(self) -> None:
        import floati.doctor as doctor_module

        with mock.patch.object(doctor_module, "installed_bridge_paths", return_value=None):
            artifact, rc = self.doctor().artifact()

        finding = next(row for row in artifact["findings"] if row["code"] == "wake_bridge_uninstalled")
        self.assertEqual("ok", finding["severity"])
        self.assertIn("typed", finding["detail"].lower() + finding["subject"].lower() + "typed absence")

    def test_installed_bridge_without_repository_source_is_unnameable(self) -> None:
        """A present bridge must not be mislabeled as uninstalled or healthy."""

        import floati.doctor as doctor_module

        hooks = self.base / "installed-hooks-without-source"
        hooks.mkdir()
        bridge = hooks / "stop-hook-bridge.py"
        bridge.write_bytes(b"installed bridge\n")
        sidecar = hooks / "stop-hook-bridge.sha256"
        sidecar.write_text(
            hashlib.sha256(bridge.read_bytes()).hexdigest() + "\n",
            encoding="utf-8",
        )

        with mock.patch.object(
            doctor_module,
            "installed_bridge_paths",
            return_value=(bridge, sidecar),
        ):
            artifact, rc = self.doctor().artifact()

        self.assertEqual(35, rc)
        finding = next(
            row
            for row in artifact["findings"]
            if row["code"] == "wake_bridge_repository_source_unnameable"
        )
        self.assertEqual("warning", finding["severity"])
        self.assertIn(
            "complete Floati source", str(finding["remediation"])
        )

    def _write_zcode_binding(self, session_id: str = "worker-zcode-session") -> None:
        """A zcode binding cannot pass DaemonCoordinate at this tree (the
        adapter lives on the kit lane), so the record is written by hand in
        the exact stored shape."""
        import hashlib as _hashlib

        node = public_ids.builder("a")
        record = {
            "schema_version": 1,
            "tenant_id": self.root.tenant_id,
            "node_id": node,
            "harness": "zcode",
            "coordinate_digest": "z" * 64,
            "session_id": session_id,
            "session_digest": _hashlib.sha256(session_id.encode()).hexdigest(),
            "workspace": str(self.home),
            "executable": str(self.source / "scripts/floati"),
            "executable_digest": _hashlib.sha256(
                (self.source / "scripts/floati").read_bytes()).hexdigest(),
            "adapter_version": "1",
            "adapter_digest": "c" * 64,
            "binding_epoch": 2,
        }
        path = self.root.resolve_relative(
            Path("state/wake-daemon/adapters") / node / "zcode.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        return node

    def test_absent_bridge_suppresses_hook_unproven_for_bound_zcode(self) -> None:
        import floati.doctor as doctor_module

        self._write_zcode_binding()
        with mock.patch.object(
            doctor_module, "installed_bridge_paths", return_value=None
        ):
            artifact, rc = self.doctor().artifact()

        absence = next(
            row
            for row in artifact["findings"]
            if row["code"] == "wake_bridge_uninstalled"
        )
        self.assertEqual("ok", absence["severity"])
        self.assertFalse(
            any(row["code"] == "hook_unproven" for row in artifact["findings"])
        )
        self.assertEqual(0, rc)

    def test_a_bound_zcode_hook_with_no_observed_firing_is_hook_unproven(self) -> None:
        """WD-R8: read-back proves what was written; only a firing proves
        what will run. A bound zcode seat with zero observed dispatches is
        hook_unproven, beside trust and breaker state."""
        import floati.doctor as doctor_module

        node = self._write_zcode_binding()
        hooks = self.base / "installed-zcode-hooks"
        hooks.mkdir()
        bridge = hooks / "stop-hook-bridge.py"
        bridge.write_bytes(b"installed bridge\n")
        sidecar = hooks / "stop-hook-bridge.sha256"
        sidecar.write_text(hashlib.sha256(bridge.read_bytes()).hexdigest() + "\n")
        with mock.patch.object(
            doctor_module,
            "installed_bridge_paths",
            return_value=(bridge, sidecar),
        ):
            artifact, rc = self.doctor().artifact()

        finding = next(
            row for row in artifact["findings"] if row["code"] == "hook_unproven"
        )
        self.assertEqual("warning", finding["severity"])
        self.assertIn(f"{node}/zcode", finding["subject"])
        self.assertIsNotNone(finding["remediation"])
        self.assertEqual(35, rc)

    def test_an_observed_firing_is_reported_as_proof(self) -> None:
        import floati.doctor as doctor_module

        node = self._write_zcode_binding()
        firings = self.root.resolve_relative(
            Path("state/zcode-hook/firings") / f"{node}.jsonl"
        )
        firings.parent.mkdir(parents=True, exist_ok=True)
        firings.write_text(
            json.dumps({"timestamp": "2026-08-30T23:22:08+00:00", "injected": 4}) + "\n",
            encoding="utf-8",
        )

        hooks = self.base / "observed-zcode-hooks"
        hooks.mkdir()
        bridge = hooks / "stop-hook-bridge.py"
        bridge.write_bytes(b"installed bridge\n")
        sidecar = hooks / "stop-hook-bridge.sha256"
        sidecar.write_text(hashlib.sha256(bridge.read_bytes()).hexdigest() + "\n")
        with mock.patch.object(
            doctor_module,
            "installed_bridge_paths",
            return_value=(bridge, sidecar),
        ):
            artifact, rc = self.doctor().artifact()

        finding = next(
            row for row in artifact["findings"] if row["code"] == "hook_firing_observed"
        )
        self.assertEqual("ok", finding["severity"])
        self.assertIn("2026-08-30T23:22:08+00:00", finding["detail"])
        self.assertIn("injected=4", finding["detail"])

    def test_bus_only_visible_copy_is_the_architects_strings(self) -> None:
        """Catches the architect's strings regressing to placeholder keys."""
        from floati import copy as copy_module

        for key in ("doctor.profile.invalid", "doctor.live_dirs.expected_absent"):
            with self.subTest(key=key):
                self.assertNotIn("[[", copy_module._ENTRIES[key][0])

    def test_installed_bus_watch_drift_names_both_coordinates_and_reinstall(self) -> None:
        """Catches a stale deployed watcher remaining invisible to doctor."""
        import floati.doctor as doctor_module

        repository_bytes = b"repository watcher\n"
        installed_bytes = b"installed watcher\n"
        self._vendor_bus_watch(repository_bytes)
        installed = self.base / "installed-plugin" / "floati-bus-watch.ts"
        installed.parent.mkdir()
        installed.write_bytes(installed_bytes)
        sidecar = installed.with_suffix(".sha256")
        installed_sha = hashlib.sha256(installed_bytes).hexdigest()
        repository_sha = hashlib.sha256(repository_bytes).hexdigest()
        sidecar.write_text(installed_sha + "\n", encoding="utf-8")

        with patch.object(
            doctor_module,
            "installed_bus_watch_paths",
            return_value=(installed, sidecar),
            create=True,
        ):
            artifact, rc = self.doctor().artifact()

        self.assertEqual(35, rc)
        finding = next(
            (row for row in artifact["findings"] if row["code"] == "bus_watch_drift"),
            None,
        )
        self.assertIsNotNone(finding, "doctor omitted the bus-watch drift finding")
        assert finding is not None
        self.assertEqual("warning", finding["severity"])
        self.assertIn(f"installed watcher from {installed_sha[:12]}", finding["detail"])
        self.assertIn(f"repository at {repository_sha[:12]}", finding["detail"])
        self.assertIn("reinstall", finding["remediation"])

    def test_installed_bus_watch_matching_sidecar_is_current(self) -> None:
        import floati.doctor as doctor_module

        content = b"same watcher\n"
        self._vendor_bus_watch(content)
        installed = self.base / "installed-plugin" / "floati-bus-watch.ts"
        installed.parent.mkdir()
        installed.write_bytes(content)
        sidecar = installed.with_suffix(".sha256")
        digest = hashlib.sha256(content).hexdigest()
        sidecar.write_text(digest + "\n", encoding="utf-8")

        with patch.object(
            doctor_module,
            "installed_bus_watch_paths",
            return_value=(installed, sidecar),
            create=True,
        ):
            artifact, rc = self.doctor().artifact()

        self.assertEqual(0, rc)
        finding = next(
            (row for row in artifact["findings"] if row["code"] == "bus_watch_current"),
            None,
        )
        self.assertIsNotNone(finding, "doctor omitted the current bus-watch fact")
        assert finding is not None
        self.assertEqual("ok", finding["severity"])
        self.assertIn(digest[:12], finding["detail"])

    def test_absent_installed_bus_watch_is_a_typed_absence(self) -> None:
        artifact, rc = self.doctor().artifact()

        self.assertEqual(0, rc)
        finding = next(
            (row for row in artifact["findings"] if row["code"] == "bus_watch_uninstalled"),
            None,
        )
        self.assertIsNotNone(finding, "doctor omitted the uninstalled typed absence")
        assert finding is not None
        self.assertEqual("ok", finding["severity"])
        self.assertIsNone(finding["remediation"])

    def test_installed_bus_watch_without_valid_sidecar_is_unnameable(self) -> None:
        import floati.doctor as doctor_module

        self._vendor_bus_watch(b"repository watcher\n")
        installed = self.base / "installed-plugin" / "floati-bus-watch.ts"
        installed.parent.mkdir()
        installed.write_bytes(b"installed watcher\n")
        missing_sidecar = installed.with_suffix(".sha256")

        with patch.object(
            doctor_module,
            "installed_bus_watch_paths",
            return_value=(installed, missing_sidecar),
            create=True,
        ):
            artifact, rc = self.doctor().artifact()

        self.assertEqual(35, rc)
        finding = next(
            (
                row
                for row in artifact["findings"]
                if row["code"] == "bus_watch_installed_source_unnameable"
            ),
            None,
        )
        self.assertIsNotNone(finding, "doctor omitted the installed-source absence")
        assert finding is not None
        self.assertEqual("warning", finding["severity"])
        self.assertIsNotNone(finding["remediation"])
        self.assertIn("reinstall", finding["remediation"])

    def test_repository_bus_watch_absence_is_typed_not_refused(self) -> None:
        import floati.doctor as doctor_module

        installed = self.base / "installed-plugin" / "floati-bus-watch.ts"
        installed.parent.mkdir()
        content = b"installed watcher\n"
        installed.write_bytes(content)
        sidecar = installed.with_suffix(".sha256")
        sidecar.write_text(hashlib.sha256(content).hexdigest() + "\n", encoding="utf-8")

        with patch.object(
            doctor_module,
            "installed_bus_watch_paths",
            return_value=(installed, sidecar),
            create=True,
        ):
            artifact, rc = self.doctor().artifact()

        self.assertEqual(35, rc)
        finding = next(
            (
                row
                for row in artifact["findings"]
                if row["code"] == "bus_watch_repository_source_unnameable"
            ),
            None,
        )
        self.assertIsNotNone(finding, "doctor omitted the repository-source absence")
        assert finding is not None
        self.assertEqual("warning", finding["severity"])
        self.assertIsNotNone(finding["remediation"])
        self.assertIn("complete Floati source", finding["remediation"])

    def _gateway_config(self) -> Path:
        path = self.base / "gateway.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "kind": "local_gateway_config",
                    "transport": "stdio",
                    "network": "disabled",
                    "workspace_root": str(self.base / "gateway-work"),
                    "approval_mode": "forward_fail_closed",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_healthy_artifact_reports_every_named_diagnostic_family(self) -> None:
        artifact, rc = self.doctor().artifact()

        self.assertEqual(0, rc)
        self.assertEqual("healthy", artifact["state"])
        self.assertEqual(
            [
                "root_valid",
                "launcher_interpreter",
                "effect_reconciliation_interpreter_trust",
                "registry_live_dirs_match",
                "wake_namespace_registry_subset",
                "wake_daemon_health",
                "wake_bridge_uninstalled",
                "delivery_health",
                "acknowledgment_health",
                "wake_health",
                "sandbox_write",
                "sandbox_write",
                "sandbox_write",
                "sandbox_write",
                "sandbox_write",
                "manifest_exact_set",
                "deploy_currency_current",
                "bus_watch_uninstalled",
                "symlink_identity_valid",
                "consumption_coordinate_valid",
                "installer_shadow",
                "herdr_protocol_pins",
                "codex_wait_hook_trust",
            ],
            [finding["code"] for finding in artifact["findings"]],
        )
        self.assertTrue(all(
            finding["severity"] == "ok"
            for finding in artifact["findings"]
            if finding["code"] not in {
                "installer_shadow", "sandbox_write", "codex_wait_hook_trust",
            }
        ))
        self.assertTrue(all(
            finding["remediation"] is None
            for finding in artifact["findings"]
            if finding["code"] not in {"sandbox_write", "codex_wait_hook_trust"}
        ))
        shadow = next(row for row in artifact["findings"] if row["code"] == "installer_shadow")
        self.assertEqual("warning", shadow["severity"])
        self.assertEqual("found", shadow["installer_shadow"]["outcome"])

    def test_root_registry_and_symlink_failures_have_typed_return_codes(self) -> None:
        missing, missing_rc = self.doctor(self.base / "missing").artifact()
        self.assertEqual(20, missing_rc)
        self.assertIn("direct_home_missing", [row["code"] for row in missing["findings"]])

        (self.root.resolve_relative(public_ids.compose('liveness-presence/', public_ids.ledger(public_ids.builder('a'))))).unlink()
        mismatch, mismatch_rc = self.doctor().artifact()
        self.assertEqual(35, mismatch_rc)
        finding = next(row for row in mismatch["findings"] if row["code"] == "registry_live_dirs_mismatch")
        self.assertIsNotNone(finding["remediation"])

        alias = self.base / "alias"
        os.symlink(self.home, alias)
        symlinked, symlinked_rc = self.doctor(alias).artifact()
        self.assertEqual(20, symlinked_rc)
        finding = next(row for row in symlinked["findings"] if row["code"] == "direct_home_symlinked_entry")
        self.assertIsNone(finding["remediation"])

    def test_manifest_currency_and_consumption_corruption_are_not_repair_claims(self) -> None:
        (self.source / "floati/core.py").write_text("DIRTY = 1\n", encoding="utf-8")
        invalid, invalid_rc = self.doctor().artifact()
        self.assertEqual(33, invalid_rc)
        manifest = next(row for row in invalid["findings"] if row["code"] == "manifest_invalid")
        self.assertIsNone(manifest["remediation"])
        self.assertIn("deploy_currency_unavailable", [row["code"] for row in invalid["findings"]])

        self._git("checkout", "--", "floati/core.py")
        work = self.root.resolve_relative("work/items.jsonl")
        work.parent.mkdir(parents=True, exist_ok=True)
        work.write_text('{"kind":"work_item"}\n', encoding="utf-8")
        corrupted, corrupted_rc = self.doctor().artifact()
        self.assertEqual(33, corrupted_rc)
        self.assertIn("consumption_state_unavailable", [row["code"] for row in corrupted["findings"]])

    def test_alternate_work_coordinate_is_degraded_not_consumed(self) -> None:
        alternate = self.root.resolve_relative("work/queue.jsonl")
        alternate.parent.mkdir(parents=True, exist_ok=True)
        alternate.write_text("", encoding="utf-8")

        artifact, rc = self.doctor().artifact()

        self.assertEqual(35, rc)
        finding = next(row for row in artifact["findings"] if row["code"] == "consumption_coordinate_ambiguous")
        self.assertIn("work/items.jsonl", finding["detail"])

    def test_schema_fixture_and_cli_artifact_return_code(self) -> None:
        fixture = json.loads(Path("tests/fixtures/doctor/v0/healthy.json").read_text(encoding="utf-8"))
        validate_json_schema(fixture, Path("schemas/v0/doctor-artifact.schema.json"))

        result = subprocess.run(
            [
                "scripts/floati", "doctor", "--root", str(self.home),
                "--source", str(self.source), "--ref", "origin/lane/hm0",
                "--destination", str(self.destination),
            ],
            check=False, capture_output=True, text=True,
        )
        self._assert_typed_launcher_doctor_outcome(result)

    def test_explicit_gateway_config_is_checked_without_discovery_or_activation(self) -> None:
        config = self._gateway_config()

        artifact, rc = self.doctor(gateway_config=config).artifact()

        self.assertEqual(0, rc)
        finding = next(row for row in artifact["findings"] if row["code"] == "gateway_config_valid")
        self.assertEqual("ok", finding["severity"])
        self.assertEqual(str(config), finding["subject"])
        self.assertIsNone(finding["remediation"])
        self.assertFalse((self.base / "gateway-work").exists())

        without_config, without_rc = self.doctor().artifact()
        self.assertEqual(0, without_rc)
        self.assertFalse(any(row["code"].startswith("gateway_") for row in without_config["findings"]))

    def test_invalid_gateway_config_is_typed_and_currency_conditional(self) -> None:
        missing = self.base / "missing-gateway.json"
        artifact, rc = self.doctor(gateway_config=missing).artifact()
        self.assertEqual(20, rc)
        finding = next(row for row in artifact["findings"] if row["code"] == "gateway_config_missing")
        self.assertIsNotNone(finding["remediation"])

        malformed = self._gateway_config()
        malformed.write_text('{"transport":"https"}\n', encoding="utf-8")
        artifact, rc = self.doctor(gateway_config=malformed).artifact()
        self.assertEqual(20, rc)
        self.assertIn("gateway_config_malformed", [row["code"] for row in artifact["findings"]])

        self._git("checkout", "--", "floati/core.py")
        (self.source / "floati/core.py").write_text("DIRTY = 1\n", encoding="utf-8")
        artifact, _ = self.doctor(gateway_config=missing).artifact()
        finding = next(row for row in artifact["findings"] if row["code"] == "gateway_config_missing")
        self.assertIsNone(finding["remediation"])

    def test_doctor_cli_accepts_only_an_explicit_gateway_config_path(self) -> None:
        config = self._gateway_config()
        result = subprocess.run(
            [
                "scripts/floati", "doctor", "--root", str(self.home),
                "--source", str(self.source), "--ref", "origin/lane/hm0",
                "--gateway-config", str(config),
                "--destination", str(self.destination),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        evidence = self._assert_typed_launcher_doctor_outcome(result)
        self.assertIn("gateway_config_valid", [row["code"] for row in evidence["findings"]])

    def test_the_health_check_line_printed_in_agents_md_executes_without_refusal(self) -> None:
        """DOC-2: the manual's printed health-check line must run as written."""

        agents_md = Path(__file__).resolve().parents[1] / "AGENTS.md"
        text = agents_md.read_text(encoding="utf-8")
        printed = re.search(r"\*\*Health check:\*\* `([^`]+)`", text)
        self.assertIsNotNone(printed, "AGENTS.md must print a doctor health-check line")
        words = printed.group(1).split()
        fixtures = {"--root": str(self.home), "--source": str(self.source)}
        command: list[str] = []
        index = 0
        while index < len(words):
            word = words[index]
            if word in fixtures and index + 1 < len(words):
                command.extend([word, fixtures[word]])
                index += 2
                continue
            command.append(word)
            index += 1
        completed = subprocess.run(
            [str(LAUNCHER), *command],
            cwd=str(agents_md.parent),
            capture_output=True,
            text=True,
            check=False,
        )
    def test_doctor_accepts_the_interpreter_trust_warning_as_a_typed_outcome(self) -> None:
        """CI-GREEN-6(a)/G0-9: the trust warning is a typed finding, not a crash.

        The assertion names the finding, not the exit code: a run whose
        interpreter fails the trust fence completes with the warning row and
        its observation attached, whatever exit code the severity maps to.
        """

        from floati.effect_reconciliation_exec import _InterpreterTrustFailure

        observation = {
            "interpreter_path": "/test-fixture/python3",
            "failing_component": "/test-fixture/python3",
            "component_uid": 501,
            "component_mode": 0o100755,
        }
        self._interpreter_trust_projection_patch.stop()
        self.addCleanup(self._interpreter_trust_projection_patch.start)
        with mock.patch(
            "floati.effect_reconciliation_exec._freeze_trusted_interpreter",
            side_effect=_InterpreterTrustFailure(observation),
        ):
            artifact, return_code = self.doctor(no_sandbox=True).artifact()

        self.assertNotEqual(20, return_code)
        warning = self._interpreter_trust_finding(artifact)
        self.assertEqual("warning", warning["severity"])
        self.assertEqual(observation, warning["interpreter_trust"])
        self.assertTrue(warning["remediation"])

    def test_fixture_names_usr_bin_for_the_system_components(self) -> None:
        """CI-GREEN-6(b)/F18: the subprocess PATH never derives from which(git)."""

        path_entries = os.environ["PATH"].split(os.pathsep)
        self.assertIn(
            "/usr/bin",
            path_entries,
            "the fixture PATH must name /usr/bin for the system components",
        )
        self.assertNotIn(
            "/opt/homebrew",
            os.environ["PATH"],
            "a Homebrew bin on the fixture PATH is the F18 defect",
        )


    def test_probe_artifact_prints_on_stdout_like_every_other_verb(self) -> None:
        """PROBE-1(a): a script redirecting stdout captures the probe artifact."""

        completed = subprocess.run(
            [
                str(LAUNCHER), "doctor",
                "--root", str(self.home),
                "--source", str(self.source),
                "--probe", "--probe-budget", "2",
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(35, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        artifact = json.loads(completed.stdout)
        self.assertEqual("degraded", artifact["status"])
        self.assertIn("probe", artifact["evidence"])

    def test_probe_artifact_carries_total_budget_seconds(self) -> None:
        """PROBE-1(b): the artifact states the total budget the reader waited for."""

        with mock.patch("floati.doctor_probe.time.sleep", lambda _seconds: None):
            artifact, return_code = self.doctor().probe(budget_seconds=2.0)
        self.assertEqual(35, return_code)
        self.assertEqual(2.0, artifact["budget_seconds"])
        self.assertEqual(1, len(artifact["nodes"]))
        self.assertEqual("DEAF", artifact["nodes"][0]["verdict"])
        self.assertEqual(
            2.0,
            artifact["total_budget_seconds"],
            "probe block must carry budget_seconds x node count",
        )

    def test_probe_run_mirrors_the_deaf_verdict_into_findings_and_validates(
        self,
    ) -> None:
        """PROBE-1(c): the obvious key carries the DEAF verdict and its remedy."""

        completed = subprocess.run(
            [
                str(LAUNCHER), "doctor",
                "--root", str(self.home),
                "--source", str(self.source),
                "--probe", "--probe-budget", "2",
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(35, completed.returncode, completed.stderr)
        evidence = json.loads(completed.stdout)["evidence"]
        validate_json_schema(evidence, Path("schemas/v1/doctor-artifact.schema.json"))
        deaf = [
            row
            for row in evidence["findings"]
            if row["code"] == "delivery_probe" and row["severity"] == "error"
        ]
        self.assertEqual(1, len(deaf), "DEAF verdict must be mirrored into findings")
        self.assertTrue(deaf[0]["remediation"])
        self.assertEqual("DEAF", evidence["probe"]["nodes"][0]["verdict"])

    def test_readme_cost_table_distinguishes_probe_and_names_the_deaf_definition(
        self,
    ) -> None:
        """PROBE-1(d): the table separates the probe's budget shape; the copy landed."""

        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )
        table_rows = [line for line in readme.splitlines() if line.startswith("| doctor")]
        self.assertTrue(
            any(row.startswith("| doctor |") for row in table_rows),
            "cost table must keep the plain doctor row: " + repr(table_rows),
        )
        self.assertTrue(
            any(row.startswith("| doctor --probe |") for row in table_rows),
            "cost table must distinguish doctor --probe from doctor: " + repr(table_rows),
        )
        self.assertIn(
            "DEAF by definition",
            readme,
            "the probe prose must say a fleet with no waiter armed is DEAF by definition",
        )


class LauncherInterpreterProvenanceTests(unittest.TestCase):
    """LAUNCH-1: scripts/floati locates its interpreter without consulting PATH.

    The decoy case is the control that decides the row. Every other assertion
    here passes on a host with one python3; only a decoy earlier on PATH
    separates a fixed candidate list from the bare lookup it replaced.
    """

    REPOSITORY = Path(__file__).resolve().parents[1]

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.marker = self.base / "decoy-ran"
        decoy_bin = self.base / "decoy-bin"
        decoy_bin.mkdir()
        self.decoy = decoy_bin / "python3"
        self.decoy.write_text(
            "#!/bin/sh\n"
            f"printf 'ran\\n' > {self.marker}\n"
            "printf 'DECOY-PYTHON3-SELECTED\\n' >&2\n"
            "exit 3\n",
            encoding="utf-8",
        )
        self.decoy.chmod(0o755)
        self.decoy_path = os.pathsep.join((str(decoy_bin), os.environ["PATH"]))

    def _launch(self, *arguments: str, **environment: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["scripts/floati", *arguments],
            cwd=str(self.REPOSITORY),
            env={**os.environ, "PATH": self.decoy_path, **environment},
            capture_output=True, text=True, check=False,
        )

    def assertDecoyNeverRan(self, result: subprocess.CompletedProcess) -> None:
        self.assertFalse(
            self.marker.exists(),
            "the launcher executed a python3 decoy earlier on PATH: " + result.stderr,
        )
        self.assertNotIn("DECOY-PYTHON3-SELECTED", result.stderr)

    def test_launcher_never_selects_a_python3_decoy_earlier_on_path(self) -> None:
        """THE CONTROL: a bare `exec python3` runs the decoy and exits 3."""

        result = self._launch("doctor", "--help")

        self.assertDecoyNeverRan(result)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("floati doctor", result.stdout)

    def test_launcher_execs_the_declared_interpreter_over_a_path_decoy(self) -> None:
        """The operator's declaration is the only override, and it is obeyed."""

        declared = self.base / "declared-python3"
        declared.write_text(
            "#!/bin/sh\nprintf 'DECLARED-INTERPRETER-RAN %s\\n' \"$*\"\nexit 7\n",
            encoding="utf-8",
        )
        declared.chmod(0o755)

        result = self._launch(
            "doctor", **{LAUNCHER_INTERPRETER_DECLARATION: str(declared)}
        )

        self.assertDecoyNeverRan(result)
        self.assertEqual(7, result.returncode, result.stderr)
        self.assertIn("DECLARED-INTERPRETER-RAN -m floati doctor", result.stdout)

    def test_an_undeclarable_interpreter_is_refused_and_never_falls_back_to_path(
        self,
    ) -> None:
        """Being unable to start is a safe state; starting the wrong Python is not."""

        for declaration in (str(self.base / "absent-python3"), "python3", str(self.base)):
            with self.subTest(declaration=declaration):
                result = self._launch(
                    "doctor", **{LAUNCHER_INTERPRETER_DECLARATION: declaration}
                )

                self.assertDecoyNeverRan(result)
                self.assertEqual(20, result.returncode, result.stdout)
                self.assertIn("floati: refused:", result.stderr)
                self.assertIn(LAUNCHER_INTERPRETER_DECLARATION, result.stderr)

    def test_a_declared_symlink_is_refused_so_the_operator_names_the_target(self) -> None:
        target = self.base / "real-python3"
        target.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        target.chmod(0o755)
        link = self.base / "linked-python3"
        link.symlink_to(target)

        result = self._launch("doctor", **{LAUNCHER_INTERPRETER_DECLARATION: str(link)})

        self.assertDecoyNeverRan(result)
        self.assertEqual(20, result.returncode, result.stdout)
        self.assertIn("symlink is refused", result.stderr)

    def test_the_launcher_and_the_doctor_name_the_same_ruled_vocabulary(self) -> None:
        """Two copies of one list is two things to keep correct; pin them equal."""

        script = (self.REPOSITORY / "scripts/floati").read_text(encoding="utf-8")

        loop = re.search(r"for candidate in ([^\n;]+); do", script)
        self.assertIsNotNone(loop, "scripts/floati must walk a fixed candidate list")
        self.assertEqual(
            list(LAUNCHER_INTERPRETER_CANDIDATES), loop.group(1).split()
        )
        self.assertIn(LAUNCHER_INTERPRETER_DECLARATION, script)
        self.assertIn(LAUNCHER_INTERPRETER_SELECTION, script)
        self.assertNotIn(
            "exec python3",
            script,
            "the interpreter must never be resolved through PATH",
        )

    def test_the_doctor_types_every_source_the_launcher_can_report(self) -> None:
        candidate = LAUNCHER_INTERPRETER_CANDIDATES[0]
        declared = str(self.base / "declared-python3")
        cases = (
            ({}, "ok", "absent: "),
            ({LAUNCHER_INTERPRETER_SELECTION: candidate}, "ok", "candidate: "),
            (
                {
                    LAUNCHER_INTERPRETER_SELECTION: declared,
                    LAUNCHER_INTERPRETER_DECLARATION: declared,
                },
                "ok",
                "declared: ",
            ),
            (
                {LAUNCHER_INTERPRETER_SELECTION: "/opt/elsewhere/bin/python3"},
                "warning",
                "unruled: ",
            ),
        )
        for environment, severity, opening in cases:
            with self.subTest(opening=opening):
                with mock.patch.dict(os.environ, environment, clear=False):
                    for key in (
                        LAUNCHER_INTERPRETER_SELECTION, LAUNCHER_INTERPRETER_DECLARATION
                    ):
                        if key not in environment:
                            os.environ.pop(key, None)
                    finding = project_launcher_interpreter()

                self.assertEqual("launcher_interpreter", finding["code"])
                self.assertEqual(severity, finding["severity"])
                self.assertTrue(finding["detail"].startswith(opening), finding["detail"])
                self.assertEqual(
                    severity == "warning", finding["remediation"] is not None
                )


if __name__ == "__main__":
    unittest.main()
