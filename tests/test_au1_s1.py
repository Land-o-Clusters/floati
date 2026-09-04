from __future__ import annotations

from tests.test_cli import LAUNCHER

import base64
import builtins
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import typing
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot
from floati.signing import sign_minisign
from tests import managed_test_tools

try:
    from floati.update_consent import UpdateConsentLedger
except (ImportError, ModuleNotFoundError):
    UpdateConsentLedger = None

try:
    from floati.update_check import (
        check_for_updates,
        decode_release_index_envelope,
        validate_release_index,
        verify_release_index,
    )
except (ImportError, ModuleNotFoundError):
    check_for_updates = None
    decode_release_index_envelope = None
    validate_release_index = None
    verify_release_index = None


TRANSPORT_IMPORTED_EAGERLY = "floati.update_transport" in sys.modules
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MINISIGN_FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "minisign"


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.reason = "fixture"
        self.body = body
        self.read_sizes: list[int] = []

    def read(self, amount: int = -1) -> bytes:
        self.read_sizes.append(amount)
        if amount < 0:
            return self.body
        return self.body[:amount]


class _Connection:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.requests: list[tuple[str, str, object, dict[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        target: str,
        body: object = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.requests.append((method, target, body, dict(headers or {})))

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        self.closed = True


class AU1S1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.destination = self.base / "installed"
        self.destination.mkdir()
        self.entrypoint = self._write_standalone_install(self.destination)
        self.channel = "https://updates.example.invalid/release-index.v0"
        selected = managed_test_tools.executable(
            "FLOATI_TEST_MINISIGN_EXECUTABLE", "minisign"
        )
        self.assertIsNotNone(selected, "AU1-S1 requires a real minisign binary")
        self.minisign = Path(selected)
        self.index = {
            "schema_version": 0,
            "channel_id": "stable-fixture",
            "index_version": "7",
            "latest_version": "1.0.0",
            "releases": [
                {
                    "version": "1.0.0",
                    "source_sha": "a" * 40,
                    "bundle_filename": "floati-1.0.0.bundle",
                    "bundle_url": "https://updates.example.invalid/floati-1.0.0.bundle",
                    "bundle_sha256": "b" * 64,
                    "bundle_size": 123,
                }
            ],
        }

    @staticmethod
    def _fixture_key_id(public_key: bytes) -> str:
        lines = public_key.splitlines()
        raw = base64.b64decode(lines[1], validate=True)
        return raw[2:10][::-1].hex().upper()

    def _write_standalone_install(self, destination: Path) -> Path:
        entrypoint = destination / "scripts" / "floati"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_bytes(b"#!/bin/sh\nexit 0\n")
        entrypoint.chmod(0o755)
        metadata = {
            "schema_version": 1,
            "source_ref": "refs/heads/main",
            "source_sha": "a" * 40,
            "files": [
                {
                    "path": "scripts/floati",
                    "sha256": hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
                }
            ],
            "ownership": {
                "kind": "floati_standalone",
                "destination": str(destination.resolve()),
                "entrypoint": "scripts/floati",
                "entrypoint_sha256": hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
                "manager": None,
                "remedy": None,
            },
        }
        install_metadata = destination / ".floati-install"
        install_metadata.mkdir()
        (install_metadata / "manifest.v0.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        public_key = (MINISIGN_FIXTURES / "fixture.pub").read_bytes()
        trust = destination / "trust"
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
        return entrypoint

    def _require_consent(self):
        self.assertIsNotNone(
            UpdateConsentLedger,
            "S1 must expose destination-bound consent and revocation",
        )
        return UpdateConsentLedger(self.destination)

    def _require_check(self) -> None:
        self.assertIsNotNone(
            check_for_updates,
            "S1 must expose the explicit one-call update check",
        )

    def _transport(self):
        spec = importlib.util.find_spec("floati.update_transport")
        self.assertIsNotNone(
            spec,
            "S1 must isolate HTTPS in one lazy update_transport module",
        )
        return importlib.import_module("floati.update_transport")

    def _consent(self, *, channel: str | None = None):
        return self._require_consent().consent(
            channel=channel or self.channel,
            epoch=1,
            idempotency_key="consent-one",
        )

    def _signed_index(
        self,
        *,
        index: dict[str, object] | None = None,
        signed_version: str | None = None,
    ) -> tuple[bytes, bytes]:
        payload = json.dumps(
            index or self.index,
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
            version=signed_version or str((index or self.index)["index_version"]),
            minisign_executable=self.minisign,
        )
        return payload, root.resolve_relative(signature).read_bytes()

    @staticmethod
    def _envelope(index: bytes, signature: bytes) -> bytes:
        return json.dumps(
            {
                "release-index.v0.json": base64.b64encode(index).decode("ascii"),
                "release-index.v0.json.minisig": base64.b64encode(signature).decode(
                    "ascii"
                ),
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")

    def _assert_no_transport(self, operation) -> ProtocolRefusal:
        self.assertFalse(
            TRANSPORT_IMPORTED_EAGERLY,
            "update_check must not import update_transport at module load",
        )
        sys.modules.pop("floati.update_transport", None)
        original_import = builtins.__import__
        imported: list[str] = []

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            imported.append(name)
            return original_import(name, globals, locals, fromlist, level)

        with (
            mock.patch.object(builtins, "__import__", side_effect=guarded_import),
            mock.patch.object(
                socket,
                "socket",
                side_effect=AssertionError("consent refusal must precede socket creation"),
            ),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            operation()

        self.assertNotIn("floati.update_transport", sys.modules)
        self.assertFalse(
            any(name == "floati.update_transport" for name in imported),
            imported,
        )
        return caught.exception

    def test_s1_00_without_consent_never_imports_transport_or_opens_socket(self) -> None:
        """Catches an explicit check touching the network before consent authority."""

        self._require_check()
        refusal = self._assert_no_transport(
            lambda: check_for_updates(
                destination=self.destination,
                channel=self.channel,
                entrypoint=self.entrypoint,
                idempotency_key="check-without-consent",
            )
        )

        self.assertEqual("update_consent_missing", refusal.code)
        self.assertIn("update-consent.v0.jsonl", refusal.detail)
        self.assertTrue((refusal.remedy or "").strip())
        self.assertFalse((refusal.remedy or "").startswith("DRAFT"))
        self.assertIn(str(self.destination), refusal.remedy or "")
        self.assertIn(self.channel, refusal.remedy or "")

    def test_s1_00_cli_accepts_an_explicit_minisign_before_consent(self) -> None:
        """Catches update check lacking the operator declaration surface."""

        completed = subprocess.run(
            [
                str(LAUNCHER),
                "update",
                "check",
                "--destination",
                str(self.destination),
                "--channel",
                self.channel,
                "--idempotency-key",
                "check-without-consent",
                "--minisign-executable",
                str(self.minisign),
                "--json",
            ],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(20, completed.returncode, completed.stdout)
        artifact = json.loads(completed.stdout)
        self.assertEqual("", completed.stderr)
        self.assertEqual("update_consent_missing", artifact["evidence"]["code"])

    def test_s1_00_update_check_type_hints_resolve_the_executable_seam(self) -> None:
        """Catches a postponed annotation naming an unavailable type."""

        hints = typing.get_type_hints(verify_release_index)
        self.assertIn("minisign_executable", hints)

    def test_s1_01_revoked_consent_never_imports_transport_or_opens_socket(self) -> None:
        """Catches revocation becoming advisory while checks still reach a socket."""

        self._require_check()
        ledger = self._require_consent()
        ledger.consent(
            channel=self.channel,
            epoch=1,
            idempotency_key="consent-before-revoke",
        )
        ledger.revoke(channel=self.channel, idempotency_key="revoke-one")

        refusal = self._assert_no_transport(
            lambda: check_for_updates(
                destination=self.destination,
                channel=self.channel,
                entrypoint=self.entrypoint,
                idempotency_key="check-after-revoke",
            )
        )

        self.assertEqual("update_consent_revoked", refusal.code)
        self.assertIn("update-consent.v0.jsonl", refusal.detail)
        self.assertTrue((refusal.remedy or "").strip())
        self.assertFalse((refusal.remedy or "").startswith("DRAFT"))

    def test_s1_02_consent_is_bound_to_the_exact_destination_channel_pair(self) -> None:
        """Catches consent for one channel authorizing a request to another channel."""

        self._require_check()
        self._consent()
        other = "https://other.example.invalid/release-index.v0"

        refusal = self._assert_no_transport(
            lambda: check_for_updates(
                destination=self.destination,
                channel=other,
                entrypoint=self.entrypoint,
                idempotency_key="check-wrong-pair",
            )
        )

        self.assertEqual("update_consent_mismatch", refusal.code)
        self.assertIn(str(self.destination), refusal.detail)
        self.assertIn(other, refusal.detail)
        self.assertTrue((refusal.remedy or "").strip())
        self.assertFalse((refusal.remedy or "").startswith("DRAFT"))

    def test_s1_03_redirect_refuses_after_exactly_one_request(self) -> None:
        """Catches a 3xx becoming an implicit second request or retry."""

        transport = self._transport()
        response = _Response(302, b"")
        connection = _Connection(response)

        with (
            mock.patch.object(
                transport.http.client,
                "HTTPSConnection",
                return_value=connection,
            ),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            transport.fetch_one_https(self.channel, max_bytes=128 * 1024)

        self.assertEqual("update_redirect_refused", caught.exception.code)
        self.assertEqual(1, len(connection.requests))
        self.assertTrue(connection.closed)

    def test_s1_04_transport_caps_the_envelope_without_a_second_request(self) -> None:
        """Catches an oversized response being accepted or fetched again."""

        transport = self._transport()
        response = _Response(200, b"x" * (128 * 1024 + 1))
        connection = _Connection(response)

        with (
            mock.patch.object(
                transport.http.client,
                "HTTPSConnection",
                return_value=connection,
            ),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            transport.fetch_one_https(self.channel, max_bytes=128 * 1024)

        self.assertEqual("update_envelope_too_large", caught.exception.code)
        self.assertEqual(1, len(connection.requests))
        self.assertEqual([128 * 1024 + 1], response.read_sizes)

    def test_s1_05_envelope_has_exact_file_fields_and_strict_base64(self) -> None:
        """Catches transport metadata or permissive base64 becoming signed authority."""

        self.assertIsNotNone(
            decode_release_index_envelope,
            "S1 must decode only the two ruled transport files",
        )
        index = b'{"schema_version":0}'
        signature = b"untrusted fixture signature bytes"
        decoded = decode_release_index_envelope(self._envelope(index, signature))
        self.assertEqual((index, signature), decoded)

        for malformed in (
            {
                "release-index.v0.json": base64.b64encode(index).decode("ascii"),
                "release-index.v0.json.minisig": base64.b64encode(signature).decode(
                    "ascii"
                ),
                "mirror": "https://redirect.example.invalid/",
            },
            {
                "release-index.v0.json": "***not-base64***",
                "release-index.v0.json.minisig": base64.b64encode(signature).decode(
                    "ascii"
                ),
            },
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaises(ProtocolRefusal) as caught:
                    decode_release_index_envelope(
                        json.dumps(malformed, separators=(",", ":")).encode("ascii")
                    )
                self.assertEqual("update_envelope_invalid", caught.exception.code)

    def test_s1_06_release_index_is_strict_and_selects_one_exact_latest_entry(self) -> None:
        """Catches extra fields, duplicate versions, or a dangling latest selector."""

        self.assertIsNotNone(
            validate_release_index,
            "S1 must strictly validate the signed release index",
        )
        encoded = json.dumps(
            self.index, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        self.assertEqual(self.index, validate_release_index(encoded))

        extra = {**self.index, "fallback_mirror": "https://mirror.example.invalid"}
        duplicate = {
            **self.index,
            "releases": [self.index["releases"][0], self.index["releases"][0]],
        }
        dangling = {**self.index, "latest_version": "9.9.9"}
        for malformed in (extra, duplicate, dangling):
            with self.subTest(malformed=malformed):
                with self.assertRaises(ProtocolRefusal) as caught:
                    validate_release_index(
                        json.dumps(malformed, separators=(",", ":")).encode("ascii")
                    )
                self.assertEqual("update_index_invalid", caught.exception.code)

    def test_s1_07_signature_tamper_and_trusted_comment_binding_refuse(self) -> None:
        """Catches transport bytes or an untrusted index_version bypassing V2."""

        self.assertIsNotNone(
            verify_release_index,
            "S1 must verify exact index bytes with the installed pinned key",
        )
        index, signature = self._signed_index()
        tampered = index.replace(b'"bundle_size":123', b'"bundle_size":124')
        with self.assertRaises(ProtocolRefusal) as caught:
            verify_release_index(
                self.destination,
                tampered,
                signature,
                minisign_executable=self.minisign,
            )
        self.assertEqual("signature_invalid", caught.exception.code)

        index, wrong_binding = self._signed_index(signed_version="8")
        with self.assertRaises(ProtocolRefusal) as caught:
            verify_release_index(
                self.destination,
                index,
                wrong_binding,
                minisign_executable=self.minisign,
            )
        self.assertEqual("signature_binding_mismatch", caught.exception.code)

    def test_s1_08_undeclared_minisign_is_a_typed_refusal_not_an_observation(self) -> None:
        """Catches tool absence being projected as a verified release fact."""

        self.assertIsNotNone(
            verify_release_index,
            "S1 must turn V2 tool absence into an update refusal",
        )
        index, signature = self._signed_index()
        with self.assertRaises(ProtocolRefusal) as caught:
            verify_release_index(self.destination, index, signature)

        self.assertEqual("signature_tool_absent", caught.exception.code)
        self.assertIn("operator-declared", caught.exception.detail)
        self.assertTrue((caught.exception.remedy or "").strip())
        self.assertIn("--minisign-executable", caught.exception.remedy or "")

    def test_s1_09_identical_idempotency_key_returns_observation_without_refetch(self) -> None:
        """Catches an exact check replay opening a second network request."""

        self._require_check()
        self._consent()
        index, signature = self._signed_index()
        envelope = self._envelope(index, signature)
        transport = self._transport()

        with mock.patch.object(
            transport, "fetch_one_https", return_value=envelope
        ) as fetch:
            first = check_for_updates(
                destination=self.destination,
                channel=self.channel,
                entrypoint=self.entrypoint,
                idempotency_key="check-replay",
                minisign_executable=self.minisign,
            )
            second = check_for_updates(
                destination=self.destination,
                channel=self.channel,
                entrypoint=self.entrypoint,
                idempotency_key="check-replay",
                minisign_executable=self.minisign,
            )

        self.assertEqual(first, second)
        self.assertEqual(1, fetch.call_count)
        self.assertEqual("current", first["state"])
        self.assertEqual(1, first["request_count"])
        observations = self.destination / ".floati-install" / "update-observations.v0.jsonl"
        self.assertEqual(1, len(observations.read_text(encoding="utf-8").splitlines()))

    def test_s1_10_cli_consent_status_and_revoke_use_the_ruled_shape(self) -> None:
        """Catches S1 domain authority existing without the explicit product verbs."""

        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        commands = (
            (
                "consent",
                "--destination",
                str(self.destination),
                "--channel",
                self.channel,
                "--epoch",
                "1",
                "--idempotency-key",
                "cli-consent",
                "active",
            ),
            (
                "status",
                "--destination",
                str(self.destination),
                "--channel",
                self.channel,
                "active",
            ),
            (
                "revoke",
                "--destination",
                str(self.destination),
                "--channel",
                self.channel,
                "--idempotency-key",
                "cli-revoke",
                "revoked",
            ),
        )
        for *arguments, expected_state in commands:
            with self.subTest(operation=arguments[0]):
                completed = subprocess.run(
                    [str(LAUNCHER), "update", *arguments],
                    cwd=REPOSITORY_ROOT,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                artifact = json.loads(completed.stdout)
                self.assertEqual("update", artifact["command"])
                self.assertEqual(expected_state, artifact["evidence"]["state"])

    def test_s1_11_legacy_source_checkout_update_help_remains_available(self) -> None:
        """Catches the optional S1 action removing the existing local update form."""

        completed = subprocess.run(
            [str(LAUNCHER), "update", "--help"],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("--source", completed.stdout)
        self.assertIn("--destination", completed.stdout)
        self.assertIn("--committed-tree", completed.stdout)

    def test_s1_12_verified_index_refuses_a_symlinked_retention_parent(self) -> None:
        """Catches verified release bytes escaping the installation metadata root."""

        self._require_check()
        self._consent()
        index, signature = self._signed_index()
        envelope = self._envelope(index, signature)
        escape = self.base / "retention-escape"
        escape.mkdir()
        retention = self.destination / ".floati-install" / "update-index"
        retention.symlink_to(escape, target_is_directory=True)
        transport = self._transport()

        with (
            mock.patch.object(transport, "fetch_one_https", return_value=envelope),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            check_for_updates(
                destination=self.destination,
                channel=self.channel,
                entrypoint=self.entrypoint,
                idempotency_key="check-symlinked-retention",
                minisign_executable=self.minisign,
            )

        self.assertEqual("update_index_storage_invalid", caught.exception.code)
        self.assertEqual([], list(escape.iterdir()))


if __name__ == "__main__":
    unittest.main()
