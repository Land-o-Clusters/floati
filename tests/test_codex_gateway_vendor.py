from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VENDORED = REPOSITORY_ROOT / "tools/codex/codex-fleet-bus.py"
DIGEST = REPOSITORY_ROOT / "tools/codex/codex-fleet-bus.sha256.json"
INSTALLER = REPOSITORY_ROOT / "tools/codex/install_codex_gateway.py"
INSTALL_ENTRYPOINT = REPOSITORY_ROOT / "scripts/install-codex-gateway.sh"


def load_installer():
    if not INSTALLER.is_file():
        raise AssertionError("VD-1 installer module is missing")
    spec = importlib.util.spec_from_file_location("install_codex_gateway", INSTALLER)
    if spec is None or spec.loader is None:
        raise AssertionError("VD-1 installer module is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_gateway():
    if not VENDORED.is_file():
        raise AssertionError("VD-1 gateway module is missing")
    spec = importlib.util.spec_from_file_location("codex_fleet_bus", VENDORED)
    if spec is None or spec.loader is None:
        raise AssertionError("VD-1 gateway module is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CodexGatewayVendorTests(unittest.TestCase):
    def _git(self, root: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _repository(self, root: Path) -> str:
        self._git(root, "init", "--quiet")
        self._git(root, "config", "user.name", "gateway-test")
        self._git(root, "config", "user.email", "gateway-test@example.invalid")
        (root / "source.txt").write_text("landed\n", encoding="utf-8")
        self._git(root, "add", "source.txt")
        self._git(root, "commit", "--quiet", "-m", "landed source")
        return self._git(root, "rev-parse", "HEAD")

    def test_vendored_gateway_matches_recorded_digest(self) -> None:
        """Changing the governed source without ratifying its digest is rejected."""

        self.assertTrue(VENDORED.is_file())
        record = json.loads(DIGEST.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "schema_version": 0,
                "path": "tools/codex/codex-fleet-bus.py",
                "sha256": hashlib.sha256(VENDORED.read_bytes()).hexdigest(),
            },
            record,
        )

    def test_explicit_install_writes_exact_bytes_and_receipts_both_digests(self) -> None:
        """A host write without source and installed digest evidence is rejected."""

        module = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "bin/codex-fleet-bus"
            receipt = root / "receipts/codex-fleet-bus-install.v0.json"

            artifact = module.install_gateway(
                source=VENDORED,
                digest_record=DIGEST,
                destination=destination,
                receipt_path=receipt,
            )

            expected = hashlib.sha256(VENDORED.read_bytes()).hexdigest()
            self.assertEqual(VENDORED.read_bytes(), destination.read_bytes())
            self.assertEqual(0o755, destination.stat().st_mode & 0o777)
            self.assertEqual("ok", artifact["status"])
            self.assertEqual(expected, artifact["evidence"]["source_sha256"])
            self.assertEqual(expected, artifact["evidence"]["installed_sha256"])
            self.assertEqual(artifact, json.loads(receipt.read_text(encoding="utf-8")))

    def test_recorded_digest_mismatch_refuses_before_host_mutation(self) -> None:
        """Installing bytes that disagree with the recorded source fence is rejected."""

        module = load_installer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.py"
            source.write_bytes(b"current\n")
            record = root / "digest.json"
            record.write_text(
                json.dumps(
                    {
                        "schema_version": 0,
                        "path": "tools/codex/codex-fleet-bus.py",
                        "sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
            destination = root / "bin/codex-fleet-bus"

            with self.assertRaises(module.InstallRefusal) as caught:
                module.install_gateway(
                    source=source,
                    digest_record=record,
                    destination=destination,
                    receipt_path=root / "receipt.json",
                )

            self.assertEqual("vendored_gateway_digest_mismatch", caught.exception.code)
            self.assertFalse(destination.exists())

    def test_installer_is_explicit_and_has_no_automatic_production_caller(self) -> None:
        """Turning source checkout, install, update, or doctor into auto-install is rejected."""

        self.assertTrue(INSTALL_ENTRYPOINT.is_file())
        entrypoint = INSTALL_ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn("install_codex_gateway.py", entrypoint)
        self.assertNotIn("--apply", entrypoint)
        callers = []
        for root in (REPOSITORY_ROOT / "floati", REPOSITORY_ROOT / "scripts"):
            for path in root.rglob("*"):
                if not path.is_file() or path == INSTALL_ENTRYPOINT or path.suffix not in ("", ".py"):
                    continue
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (SyntaxError, UnicodeDecodeError):
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        if node.func.id == "install_gateway":
                            callers.append(path.relative_to(REPOSITORY_ROOT).as_posix())
        self.assertEqual([], callers)

    def test_doctor_source_mismatch_refuses_with_both_coordinates(self) -> None:
        """A stale governed Doctor source remaining silent is rejected."""

        gateway = load_gateway()
        self.assertTrue(
            hasattr(gateway, "_require_doctor_source_coherence"),
            "the governed gateway does not check Doctor source coherence",
        )
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            workspace_sha = self._repository(repository)
            transport_sha = "0" * 40

            with self.assertRaises(gateway.ApprovalRequired) as caught:
                gateway._require_doctor_source_coherence(repository, transport_sha)

        detail = str(caught.exception)
        self.assertIn(f"workspace HEAD {workspace_sha}", detail)
        self.assertIn(f"transport source {transport_sha}", detail)
        self.assertIn("update", detail)

    def test_doctor_source_match_requires_clean_workspace(self) -> None:
        """A matching HEAD may proceed only when it names the bytes Doctor reads."""

        gateway = load_gateway()
        self.assertTrue(
            hasattr(gateway, "_require_doctor_source_coherence"),
            "the governed gateway does not check Doctor source coherence",
        )
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            source_sha = self._repository(repository)

            gateway._require_doctor_source_coherence(repository, source_sha)
            (repository / "source.txt").write_text("dirty\n", encoding="utf-8")

            with self.assertRaises(gateway.ApprovalRequired) as caught:
                gateway._require_doctor_source_coherence(repository, source_sha)

        self.assertIn("working tree is dirty", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
