from __future__ import annotations

import base64
import builtins
import hashlib
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati import wiring_journal
from floati.deploy import DeploymentWriter
from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot
from floati.signing import sign_minisign
from floati.update_check import check_for_updates
from floati.update_consent import UpdateConsentLedger

try:
    from floati.update_apply import apply_update
except (ImportError, ModuleNotFoundError):
    apply_update = None


TRANSPORT_IMPORTED_EAGERLY = "floati.update_transport" in sys.modules
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MINISIGN_FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "minisign"


class AU1S2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.git = shutil.which("git")
        self.assertIsNotNone(self.git)
        self.git_directory = Path(str(self.git)).resolve().parent
        selected = shutil.which("minisign")
        self.assertIsNotNone(selected, "AU1-S2 requires a real minisign binary")
        self.minisign = Path(selected).resolve(strict=True)
        self._git("init", "--quiet", "--initial-branch=lane/hm0")
        self._git("config", "user.name", "Floati AU-1 Fixture")
        self._git("config", "user.email", "floati-au1@example.invalid")

        self._write_release("1.0.0")
        self._commit("release A")
        self.sha_a = self._git("rev-parse", "HEAD")
        self.bytes_a = self._source_managed_bytes()
        self.bundle_a = self._bundle("floati-1.0.0.bundle")

        self.destination = self.base / "installed"
        with mock.patch.dict(
            os.environ,
            {"PATH": os.pathsep.join((str(self.source / "scripts"), str(self.git_directory)))},
            clear=False,
        ):
            DeploymentWriter(
                self.source,
                self.destination,
                "install",
                ref="HEAD",
                committed_tree=True,
            ).run()
        self.entrypoint = self.destination / "scripts" / "floati"
        self._provision_fixture_trust()

        self._write_release("2.0.0")
        self._commit("release B")
        self.sha_b = self._git("rev-parse", "HEAD")
        self.bytes_b = self._source_managed_bytes()
        self.bundle_b = self._bundle("floati-2.0.0.bundle")
        self.channel = "https://updates.example.invalid/release-index.v0"
        self.index = {
            "schema_version": 0,
            "channel_id": "stable-fixture",
            "index_version": "8",
            "latest_version": "2.0.0",
            "releases": [
                self._release("1.0.0", self.sha_a, self.bundle_a),
                self._release("2.0.0", self.sha_b, self.bundle_b),
            ],
        }

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            [str(self.git or "git"), *arguments],
            cwd=self.source,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def _write_release(self, version: str) -> None:
        files = {
            "LICENSE": "fixture license\n",
            "floati/__init__.py": f"VERSION = {version!r}\n",
            "scripts/floati": f"#!/bin/sh\nprintf '%s\\n' {version!r}\n",
            "scripts/floati-codex-wait": "#!/bin/sh\nexit 0\n",
        }
        for relative, content in files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (self.source / "scripts" / "floati").chmod(0o755)
        (self.source / "scripts" / "floati-codex-wait").chmod(0o755)
        self._write_manifest()

    def _write_manifest(self) -> None:
        paths = sorted(
            path.relative_to(self.source).as_posix()
            for path in self.source.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and path.name != "bundle-manifest.v0.json"
        )
        manifest = {
            "schema_version": 0,
            "protocol_version": "0",
            "canonical_ref": "refs/heads/lane/hm0",
            "files": [
                {
                    "path": relative,
                    "sha256": hashlib.sha256(
                        (self.source / relative).read_bytes()
                    ).hexdigest(),
                }
                for relative in paths
            ],
        }
        (self.source / "bundle-manifest.v0.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _commit(self, message: str) -> None:
        self._git("add", "--all")
        self._git("commit", "--quiet", "-m", message)

    def _bundle(self, filename: str) -> bytes:
        path = self.base / filename
        self._git("bundle", "create", str(path), "--all")
        return path.read_bytes()

    def _source_managed_bytes(self) -> dict[str, bytes]:
        manifest = json.loads(
            (self.source / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )
        return {
            row["path"]: (self.source / row["path"]).read_bytes()
            for row in manifest["files"]
        }

    def _destination_managed_bytes(self) -> dict[str, bytes]:
        metadata = json.loads(
            (self.destination / ".floati-install" / "manifest.v0.json").read_text(
                encoding="utf-8"
            )
        )
        return {
            row["path"]: (self.destination / row["path"]).read_bytes()
            for row in metadata["files"]
        }

    @staticmethod
    def _fixture_key_id(public_key: bytes) -> str:
        raw = base64.b64decode(public_key.splitlines()[1], validate=True)
        return raw[2:10][::-1].hex().upper()

    def _provision_fixture_trust(self) -> None:
        public_key = (MINISIGN_FIXTURES / "fixture.pub").read_bytes()
        trust = self.destination / "trust"
        trust.mkdir()
        (trust / "fixture.pub").write_bytes(public_key)
        (trust / "keys.json").write_text(
            json.dumps(
                {
                    "format": "floati-trust-keys.v1",
                    "keys": [
                        {
                            "key_id": self._fixture_key_id(public_key),
                            "public_key_file": "fixture.pub",
                            "valid_from": "2026-08-29",
                            "valid_to": None,
                            "status": "active",
                            "transition": None,
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _release(version: str, source_sha: str, bundle: bytes) -> dict[str, object]:
        return {
            "version": version,
            "source_sha": source_sha,
            "bundle_filename": f"floati-{version}.bundle",
            "bundle_url": f"https://updates.example.invalid/floati-{version}.bundle",
            "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
            "bundle_size": len(bundle),
        }

    def _require_apply(self):
        self.assertIsNotNone(
            apply_update,
            "S2 must expose the one-path apply and rollback operation",
        )
        return apply_update

    def _consent(self) -> UpdateConsentLedger:
        ledger = UpdateConsentLedger(self.destination)
        ledger.consent(
            channel=self.channel,
            epoch=1,
            idempotency_key="s2-consent-one",
        )
        return ledger

    def _signed_envelope(self, index: dict[str, object]) -> bytes:
        payload = json.dumps(
            index,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        root = FloatiRoot.open(self.base / ("signed-" + os.urandom(4).hex()), "fixture")
        artifact = Path("release-index.v0.json")
        signature = Path("release-index.v0.json.minisig")
        root.resolve_relative(artifact).write_bytes(payload)
        sign_minisign(
            root,
            artifact,
            signature,
            secret_key=(MINISIGN_FIXTURES / "fixture.key").resolve(),
            version=str(index["index_version"]),
            minisign_executable=self.minisign,
        )
        signature_bytes = root.resolve_relative(signature).read_bytes()
        return json.dumps(
            {
                "release-index.v0.json": base64.b64encode(payload).decode("ascii"),
                "release-index.v0.json.minisig": base64.b64encode(
                    signature_bytes
                ).decode("ascii"),
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")

    def _observe(self, index: dict[str, object] | None = None) -> dict[str, object]:
        self._consent()
        selected = index or self.index
        envelope = self._signed_envelope(selected)
        transport = importlib.import_module("floati.update_transport")
        with mock.patch.object(transport, "fetch_one_https", return_value=envelope):
            return check_for_updates(
                destination=self.destination,
                channel=self.channel,
                entrypoint=self.entrypoint,
                idempotency_key="s2-check-one",
                minisign_executable=self.minisign,
            )

    def _run_apply(
        self,
        bundle: bytes | object,
        *,
        version: str = "2.0.0",
        idempotency_key: str = "s2-apply-one",
    ) -> dict[str, object]:
        operation = self._require_apply()
        transport = importlib.import_module("floati.update_transport")
        fetch = mock.patch.object(
            transport,
            "fetch_one_https",
            side_effect=bundle if callable(bundle) else None,
            return_value=mock.DEFAULT if callable(bundle) else bundle,
        )
        path = os.pathsep.join(
            (
                str(self.source / "scripts"),
                str(self.destination / "scripts"),
                str(self.git_directory),
            )
        )
        with (
            fetch,
            mock.patch.dict(os.environ, {"PATH": path}, clear=False),
        ):
            return operation(
                destination=self.destination,
                channel=self.channel,
                entrypoint=self.entrypoint,
                version=version,
                idempotency_key=idempotency_key,
            )

    def _index_with(self, release: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": 0,
            "channel_id": "stable-fixture",
            "index_version": "8",
            "latest_version": str(release["version"]),
            "releases": [release],
        }

    def test_s2_00_apply_without_consent_never_imports_transport_or_opens_socket(self) -> None:
        """Catches apply touching the network before consent authority."""

        operation = self._require_apply()
        self.assertFalse(TRANSPORT_IMPORTED_EAGERLY)
        sys.modules.pop("floati.update_transport", None)
        original_import = builtins.__import__
        imported: list[str] = []

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            imported.append(name)
            return original_import(name, globals, locals, fromlist, level)

        before = self._destination_managed_bytes()
        with (
            mock.patch.object(builtins, "__import__", side_effect=guarded_import),
            mock.patch.object(
                socket,
                "socket",
                side_effect=AssertionError("consent refusal must precede a socket"),
            ),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            operation(
                destination=self.destination,
                channel=self.channel,
                entrypoint=self.entrypoint,
                version="2.0.0",
                idempotency_key="s2-without-consent",
            )

        self.assertEqual("update_consent_missing", caught.exception.code)
        self.assertNotIn("floati.update_transport", sys.modules)
        self.assertFalse(any(name == "floati.update_transport" for name in imported))
        self.assertEqual(before, self._destination_managed_bytes())

    def test_s2_01_wrong_signed_bundle_hash_refuses_without_destination_change(self) -> None:
        """Catches a bundle digest mismatch reaching the destination writer."""

        self._require_apply()
        release = {**self.index["releases"][1], "bundle_sha256": "0" * 64}
        self._observe(self._index_with(release))
        before = self._destination_managed_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            self._run_apply(self.bundle_b)

        self.assertEqual("update_bundle_digest_mismatch", caught.exception.code)
        self.assertEqual(before, self._destination_managed_bytes())

    def test_s2_02_wrong_signed_bundle_size_refuses_without_destination_change(self) -> None:
        """Catches a signed size mismatch being accepted after download."""

        self._require_apply()
        release = {
            **self.index["releases"][1],
            "bundle_size": len(self.bundle_b) + 1,
        }
        self._observe(self._index_with(release))
        before = self._destination_managed_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            self._run_apply(self.bundle_b)

        self.assertEqual("update_bundle_size_mismatch", caught.exception.code)
        self.assertEqual(before, self._destination_managed_bytes())

    def test_s2_03_malformed_or_prerequisite_bundle_refuses_before_destination_write(self) -> None:
        """Catches arbitrary bytes bypassing local git bundle verification."""

        self._require_apply()
        malformed = b"not a git bundle\n"
        release = self._release("2.0.0", self.sha_b, malformed)
        self._observe(self._index_with(release))
        before = self._destination_managed_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            self._run_apply(malformed)

        self.assertEqual("update_bundle_invalid", caught.exception.code)
        self.assertEqual(before, self._destination_managed_bytes())

    def test_s2_04_bundle_checkout_must_equal_the_signed_source_sha(self) -> None:
        """Catches a valid bundle whose staged checkout does not match signed provenance."""

        self._require_apply()
        release = self._release("2.0.0", self.sha_b, self.bundle_a)
        self._observe(self._index_with(release))
        before = self._destination_managed_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            self._run_apply(self.bundle_a)

        self.assertEqual("update_bundle_source_mismatch", caught.exception.code)
        self.assertEqual(before, self._destination_managed_bytes())

    def test_s2_05_staged_manifest_failure_refuses_before_destination_write(self) -> None:
        """Catches a Git-valid signed commit bypassing staged manifest verification."""

        self._require_apply()
        self._write_release("3.0.0")
        manifest_path = self.source / "bundle-manifest.v0.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["sha256"] = "0" * 64
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._commit("release C with invalid manifest")
        sha_c = self._git("rev-parse", "HEAD")
        bundle_c = self._bundle("floati-3.0.0.bundle")
        self._observe(self._index_with(self._release("3.0.0", sha_c, bundle_c)))
        before = self._destination_managed_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            self._run_apply(bundle_c, version="3.0.0")

        self.assertEqual("update_bundle_manifest_invalid", caught.exception.code)
        self.assertEqual(before, self._destination_managed_bytes())

    def test_s2_05a_staged_checkout_ignores_ambient_git_filters(self) -> None:
        """Catches downloaded checkout bytes triggering operator Git configuration."""

        self._require_apply()
        self._write_release("3.0.0")
        self._commit("release C with hostile attribute")
        sha_c = self._git("rev-parse", "HEAD")
        bundle_c = self._bundle("floati-3.0.0-filter.bundle")
        self._observe(self._index_with(self._release("3.0.0", sha_c, bundle_c)))
        marker = self.base / "ambient-filter-ran"
        attacker_home = self.base / "attacker-home"
        attacker_home.mkdir()
        attributes = attacker_home / "attributes"
        attributes.write_text(
            "floati/__init__.py filter=pwn\n", encoding="utf-8"
        )
        (attacker_home / ".gitconfig").write_text(
            "[core]\n"
            f"\tattributesfile = {attributes}\n"
            "[filter \"pwn\"]\n"
            "\tclean = /bin/cat\n"
            f"\tsmudge = /usr/bin/tee {marker}\n"
            "\trequired = true\n",
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"HOME": str(attacker_home)}, clear=False):
            applied = self._run_apply(
                bundle_c,
                version="3.0.0",
                idempotency_key="s2-apply-filter",
            )

        self.assertEqual(sha_c, applied["source_sha"])
        self.assertFalse(marker.exists())

    def test_s2_05b_update_apply_ignores_a_git_decoy_earlier_on_path(self) -> None:
        """Catches either update-path Git lookup selecting an ambient PATH decoy."""

        self._require_apply()
        self._observe()
        marker = self.base / "ambient-git-ran"
        attacker_git = self.source / "scripts" / "git"
        attacker_git.write_text(
            "#!/usr/bin/python3\n"
            "import os\n"
            "import pathlib\n"
            "import sys\n"
            f"pathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
            f"os.execv({str(self.git)!r}, [{str(self.git)!r}, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        attacker_git.chmod(0o755)

        applied = self._run_apply(
            self.bundle_b,
            idempotency_key="s2-apply-git-path",
        )

        self.assertEqual(self.sha_b, applied["source_sha"])
        self.assertFalse(marker.exists())

    def test_s2_05c_git_absence_names_the_fixed_host_candidates(self) -> None:
        """Catches Git absence being collapsed into bundle-invalid evidence."""

        from floati import update_apply

        candidates = ("/missing/usr/bin/git", "/missing/bin/git")
        with mock.patch.object(update_apply, "_GIT_CANDIDATES", candidates):
            with self.assertRaises(ProtocolRefusal) as caught:
                update_apply._select_git_executable()

        self.assertEqual("update_git_unavailable", caught.exception.code)
        self.assertIn("/missing/usr/bin/git", caught.exception.detail)
        self.assertIn("/missing/bin/git", caught.exception.detail)

    def test_s2_05d_operator_git_seam_uses_the_shared_explicit_path_policy(self) -> None:
        """Catches the operator seam bypassing fleet_update's path predicate."""

        from floati import update_apply

        self.assertEqual(
            str(Path(str(self.git))),
            update_apply._select_git_executable(str(self.git)),
        )
        relative = Path(str(self.git)).name
        with self.assertRaises(ProtocolRefusal) as caught:
            update_apply._select_git_executable(relative)
        self.assertEqual("update_git_unavailable", caught.exception.code)
        self.assertIn(str(relative), caught.exception.detail)

    def test_s2_06_consent_change_after_download_refuses_before_destination_write(self) -> None:
        """Catches revoked download authority being treated as apply authority."""

        self._require_apply()
        ledger = self._observe() and UpdateConsentLedger(self.destination)
        before = self._destination_managed_bytes()

        def revoke_then_return(*_args, **_kwargs):
            ledger.revoke(channel=self.channel, idempotency_key="s2-revoke-during-apply")
            return self.bundle_b

        with self.assertRaises(ProtocolRefusal) as caught:
            self._run_apply(revoke_then_return)

        self.assertEqual("update_consent_changed", caught.exception.code)
        self.assertEqual(before, self._destination_managed_bytes())

    def test_s2_07_owner_change_after_download_refuses_before_destination_write(self) -> None:
        """Catches a concurrent package-manager takeover reaching DeploymentWriter."""

        self._require_apply()
        self._observe()
        before = self._destination_managed_bytes()

        def change_owner_then_return(*_args, **_kwargs):
            metadata_path = self.destination / ".floati-install" / "manifest.v0.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["ownership"]["kind"] = "package_manager"
            metadata["ownership"]["manager"] = "fixture-manager"
            metadata["ownership"]["remedy"] = "DRAFT - run fixture-manager upgrade"
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return self.bundle_b

        with self.assertRaises(ProtocolRefusal) as caught:
            self._run_apply(change_owner_then_return)

        self.assertEqual("update_ownership_changed", caught.exception.code)
        self.assertEqual(before, self._destination_managed_bytes())

    def test_s2_08_destination_identity_change_after_download_refuses_without_writing_replacement(self) -> None:
        """Catches pathname replacement redirecting the sole destination writer."""

        self._require_apply()
        self._observe()
        before = self._destination_managed_bytes()
        displaced = self.base / "displaced-install"

        def replace_destination_then_return(*_args, **_kwargs):
            self.destination.rename(displaced)
            shutil.copytree(displaced, self.destination)
            return self.bundle_b

        with self.assertRaises(ProtocolRefusal) as caught:
            self._run_apply(replace_destination_then_return)

        self.assertEqual("update_destination_changed", caught.exception.code)
        self.assertEqual(before, self._destination_managed_bytes())

    def test_s2_09_a_to_b_to_a_rollback_uses_the_same_verified_apply_path(self) -> None:
        """Catches rollback gaining a second writer or bypassing signed bundle checks."""

        self._require_apply()
        self._observe()

        applied_b = self._run_apply(
            self.bundle_b,
            version="2.0.0",
            idempotency_key="s2-apply-b",
        )
        self.assertEqual(self.sha_b, applied_b["source_sha"])
        self.assertEqual(self.bytes_b, self._destination_managed_bytes())

        applied_a = self._run_apply(
            self.bundle_a,
            version="1.0.0",
            idempotency_key="s2-apply-a",
        )
        self.assertEqual(self.sha_a, applied_a["source_sha"])
        self.assertEqual(self.sha_b, applied_a["previous_source_sha"])
        self.assertEqual(self.bytes_a, self._destination_managed_bytes())

    def test_s2_10_apply_receipt_links_check_bundle_source_and_wiring_chain(self) -> None:
        """Catches an apply success that cannot be independently joined to its evidence."""

        self._require_apply()
        check = self._observe()
        applied = self._run_apply(self.bundle_b)
        ledger = self.destination / ".floati-install" / "update-observations.v0.jsonl"
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        receipt = rows[-1]

        self.assertEqual("update_application", receipt["kind"])
        self.assertEqual(check["id"], receipt["check_observation_id"])
        self.assertEqual(hashlib.sha256(self.bundle_b).hexdigest(), receipt["bundle_sha256"])
        self.assertEqual(self.sha_a, receipt["previous_source_sha"])
        self.assertEqual(self.sha_b, receipt["source_sha"])
        self.assertEqual("signature_verified", receipt["verification_state"])
        self.assertEqual(applied, receipt)
        journal = Path(receipt["wiring_journal"])
        self.assertTrue(journal.is_file())
        self.assertGreater(len(wiring_journal.read_entries(journal)), 0)

    def test_s2_11_identical_apply_key_returns_receipt_without_refetch(self) -> None:
        """Catches response-loss retry downloading or writing the same release twice."""

        self._require_apply()
        self._observe()
        first = self._run_apply(self.bundle_b, idempotency_key="s2-apply-replay")

        def unexpected_fetch(*_args, **_kwargs):
            raise AssertionError("an exact apply retry must not fetch")

        second = self._run_apply(
            unexpected_fetch,
            idempotency_key="s2-apply-replay",
        )

        self.assertEqual(first, second)

    def test_s2_12_cli_apply_uses_the_ruled_explicit_version_shape(self) -> None:
        """Catches S2 domain behavior existing without the governed CLI route."""

        completed = subprocess.run(
            [
                "python3",
                "-m",
                "floati",
                "update",
                "apply",
                "--destination",
                str(self.destination),
                "--channel",
                self.channel,
                "--version",
                "2.0.0",
                "--idempotency-key",
                "s2-cli-apply",
            ],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(20, completed.returncode, completed.stderr)
        artifact = json.loads(completed.stderr)
        self.assertEqual("update_consent_missing", artifact["evidence"]["code"])


if __name__ == "__main__":
    unittest.main()
