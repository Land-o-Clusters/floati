from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot


class _NonTTY(io.StringIO):
    def isatty(self) -> bool:
        return False


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class SupportBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.out = self.base / "support.tar.gz"
        self.collectors = (("host-facts", lambda: {"status": "ok", "value": 1}),)

    def create(self, **overrides):
        from floati.support_bundle import create_support_bundle

        arguments = {
            "root": self.root,
            "source": Path.cwd(),
            "out": self.out,
            "lines": 240,
            "yes": True,
            "stream": io.StringIO(),
            "input_stream": _NonTTY(),
            "collectors": self.collectors,
        }
        arguments.update(overrides)
        return create_support_bundle(**arguments)

    def members(self) -> dict[str, bytes]:
        with tarfile.open(self.out, "r:gz") as archive:
            return {
                member.name: archive.extractfile(member).read()
                for member in archive.getmembers()
                if member.isfile()
            }

    def test_non_tty_without_yes_refuses_without_prompt_or_file(self) -> None:
        from floati.support_bundle import create_support_bundle

        output = io.StringIO()
        with self.assertRaises(ProtocolRefusal) as caught:
            create_support_bundle(
                root=self.root,
                source=Path.cwd(),
                out=self.out,
                lines=240,
                yes=False,
                stream=output,
                input_stream=_NonTTY(),
                collectors=self.collectors,
            )

        self.assertEqual("snapshot_consent_unavailable", caught.exception.code)
        self.assertIn("--yes", caught.exception.detail)
        self.assertEqual("", output.getvalue())
        self.assertFalse(self.out.exists())

    def test_yes_prints_exact_disclosure_and_builds_verified_bundle(self) -> None:
        from floati.support_bundle import verify_support_bundle

        output = io.StringIO()
        evidence = self.create(stream=output)

        consent = output.getvalue()
        self.assertIn(
            "This bundle is for a maintainer. Read what it holds before you send it.",
            consent,
        )
        self.assertIn("Removed: your username, your home path, temporary paths.", consent)
        self.assertIn("Kept: your bus history, your node names, your host facts.", consent)
        self.assertIn("  host facts", consent)
        self.assertTrue(consent.endswith("host facts.\n\n"))
        self.assertTrue(evidence["written"])
        self.assertEqual([], verify_support_bundle(self.out))

    def test_default_no_leaves_output_directory_byte_identical(self) -> None:
        before = sorted(path.name for path in self.base.iterdir())
        evidence = self.create(
            yes=False,
            input_stream=_TTY("N\n"),
            stream=io.StringIO(),
        )

        self.assertFalse(evidence["written"])
        self.assertEqual(before, sorted(path.name for path in self.base.iterdir()))

    def test_mid_write_failure_leaves_no_output_or_temporary(self) -> None:
        def fail(phase: str) -> None:
            if phase == "after_fsync_before_replace":
                raise OSError("fixture failure")

        with self.assertRaises(OSError):
            self.create(fault_hook=fail)

        self.assertFalse(self.out.exists())
        self.assertEqual([], list(self.base.glob(".support.tar.gz.*.tmp")))

    def test_failing_collector_is_typed_unavailable_and_bundle_continues(self) -> None:
        def fail():
            raise RuntimeError("fixture collector failure")

        self.create(collectors=(("broken-plane", fail),))
        section = json.loads(self.members()["collectors/broken-plane.json"])
        self.assertEqual(
            {"status": "unavailable", "reason_code": "collector_failed"},
            section,
        )

    def test_manifest_tamper_is_refused(self) -> None:
        from floati.support_bundle import verify_support_bundle

        self.create()
        data = bytearray(self.out.read_bytes())
        data[len(data) // 2] ^= 1
        self.out.write_bytes(bytes(data))

        self.assertNotEqual([], verify_support_bundle(self.out))

    def test_identity_gate_refuses_raw_governed_text_and_output_is_scrubbed(self) -> None:
        from floati.identity_fence import HOME_PREFIX
        from floati.support_bundle import identity_gate

        with self.assertRaises(ProtocolRefusal) as caught:
            identity_gate(f"path={HOME_PREFIX}operator/fleet".encode("utf-8"))
        self.assertEqual("snapshot_identity_fence_failed", caught.exception.code)

        self.create(
            collectors=(("identity", lambda: {"path": f"{HOME_PREFIX}operator/fleet"}),)
        )
        section = self.members()["collectors/identity.json"]
        self.assertNotIn(HOME_PREFIX.encode("ascii"), section)
        self.assertEqual({"path": "~/fleet"}, json.loads(section))

        temp_prefixes = (
            bytes.fromhex("2f707269766174652f746d70").decode("ascii"),
            bytes.fromhex("2f707269766174652f7661722f746d70").decode("ascii"),
            bytes.fromhex("2f7661722f666f6c64657273").decode("ascii"),
            bytes.fromhex("2f746d70").decode("ascii"),
        )
        for index, prefix in enumerate(temp_prefixes):
            with self.subTest(prefix=prefix):
                with self.assertRaises(ProtocolRefusal) as caught_temp:
                    identity_gate(f"path={prefix}/fleet".encode("utf-8"))
                self.assertEqual(
                    "snapshot_identity_fence_failed", caught_temp.exception.code
                )
                destination = self.base / f"temp-{index}.tar.gz"
                self.create(
                    out=destination,
                    collectors=(("identity", lambda prefix=prefix: {"path": f"{prefix}/fleet"}),),
                )
                with tarfile.open(destination, "r:gz") as archive:
                    scrubbed = archive.extractfile("collectors/identity.json").read()
                self.assertEqual({"path": "<temp>/fleet"}, json.loads(scrubbed))

    def test_finished_bundle_derives_every_member_into_identity_gate(self) -> None:
        """Catches an archive member bypassing the shared identity fence."""

        from floati.support_bundle import identity_gate

        self.create()
        with tarfile.open(self.out, "r:gz") as archive:
            members = [member for member in archive.getmembers() if member.isfile()]
            self.assertGreaterEqual(len(members), 2)
            for member in members:
                extracted = archive.extractfile(member)
                self.assertIsNotNone(extracted)
                with self.subTest(member=member.name):
                    identity_gate(extracted.read())

    def test_opaque_member_is_excluded_as_typed_unavailable(self) -> None:
        from PIL import Image, ImageDraw

        from floati.identity_fence import PRIVATE_TMP_PREFIX

        rendered = io.BytesIO()
        image = Image.new("RGB", (360, 48), "white")
        ImageDraw.Draw(image).text((4, 12), PRIVATE_TMP_PREFIX, fill="black")
        image.save(rendered, format="GIF")
        self.assertNotIn(PRIVATE_TMP_PREFIX.encode("ascii"), rendered.getvalue())

        self.create(
            collectors=(("image", lambda: {"capture": rendered.getvalue()}),)
        )
        section = json.loads(self.members()["collectors/image.json"])
        self.assertEqual(
            {
                "status": "unavailable",
                "reason_code": "snapshot_opaque_member",
                "opaque_format": "bytes",
                "key_path": "$.capture",
            },
            section,
        )

    def test_base64_encoded_image_is_excluded_as_typed_unavailable(self) -> None:
        """Catches opaque raster bytes bypassing sanitization as JSON text."""

        from PIL import Image

        rendered = io.BytesIO()
        Image.new("RGB", (2, 2), "navy").save(rendered, format="GIF")
        encoded = base64.b64encode(rendered.getvalue()).decode("ascii")

        self.create(collectors=(("image", lambda: {"capture": encoded}),))
        section = json.loads(self.members()["collectors/image.json"])
        self.assertEqual(
            {
                "status": "unavailable",
                "reason_code": "snapshot_opaque_member",
                "opaque_format": "gif",
                "key_path": "$.capture",
            },
            section,
        )

    def test_closed_raster_signature_set_is_classified(self) -> None:
        """Catches a ruled raster format falling out of the closed enumeration."""

        signatures = {
            "png": b"\x89PNG\r\n\x1a\n",
            "gif": b"GIF89a",
            "jpeg": b"\xff\xd8\xff",
            "webp": b"RIFF\x04\x00\x00\x00WEBP",
            "bmp": (
                b"BM"
                + (78).to_bytes(4, "little")
                + b"\x00" * 4
                + (14).to_bytes(4, "little")
            ),
            "tiff": b"II*\x00",
        }
        for image_format, signature in signatures.items():
            with self.subTest(image_format=image_format):
                destination = self.base / f"{image_format}.tar.gz"
                encoded = base64.b64encode(signature + b"\x00" * 64).decode("ascii")
                self.create(
                    out=destination,
                    collectors=(("image", lambda encoded=encoded: {"capture": encoded}),),
                )
                with tarfile.open(destination, "r:gz") as archive:
                    section = json.loads(
                        archive.extractfile("collectors/image.json").read()
                    )
                self.assertEqual(image_format, section["opaque_format"])
                self.assertEqual("$.capture", section["key_path"])

    def test_closed_opaque_binary_signature_set_is_classified(self) -> None:
        """Catches a ruled opaque binary format bypassing the JSON-text fence."""

        cases = (
            ("pdf", "pdf", b"%PDF-2.0\n%\xe2\xe3\xcf\xd3\n"),
            ("zip-local", "zip", b"PK\x03\x04" + b"\x00" * 26),
            ("zip-empty", "zip", b"PK\x05\x06" + b"\x00" * 18),
            ("zip-spanned", "zip", b"PK\x07\x08" + b"\x00" * 12),
            ("gzip", "gzip", b"\x1f\x8b\x08\x00" + b"\x00" * 6),
            ("mach-o-32-be", "mach-o", b"\xfe\xed\xfa\xce" + b"\x00" * 24),
            ("mach-o-32-le", "mach-o", b"\xce\xfa\xed\xfe" + b"\x00" * 24),
            ("mach-o-64-be", "mach-o", b"\xfe\xed\xfa\xcf" + b"\x00" * 28),
            ("mach-o-64-le", "mach-o", b"\xcf\xfa\xed\xfe" + b"\x00" * 28),
            ("mach-o-fat-be", "mach-o", b"\xca\xfe\xba\xbe" + b"\x00" * 4),
            ("mach-o-fat-le", "mach-o", b"\xbe\xba\xfe\xca" + b"\x00" * 4),
            ("mach-o-fat64-be", "mach-o", b"\xca\xfe\xba\xbf" + b"\x00" * 4),
            ("mach-o-fat64-le", "mach-o", b"\xbf\xba\xfe\xca" + b"\x00" * 4),
            ("elf", "elf", b"\x7fELF\x02\x01\x01" + b"\x00" * 9),
        )
        for case_name, opaque_format, payload in cases:
            with self.subTest(case_name=case_name):
                destination = self.base / f"opaque-{case_name}.tar.gz"
                encoded = base64.b64encode(payload).decode("ascii")
                self.create(
                    out=destination,
                    collectors=(("opaque", lambda encoded=encoded: {"value": encoded}),),
                )
                with tarfile.open(destination, "r:gz") as archive:
                    section = json.loads(
                        archive.extractfile("collectors/opaque.json").read()
                    )
                self.assertEqual(
                    {
                        "status": "unavailable",
                        "reason_code": "snapshot_opaque_member",
                        "opaque_format": opaque_format,
                        "key_path": "$.value",
                    },
                    section,
                )

    def test_data_uri_media_type_does_not_override_opaque_payload_signature(self) -> None:
        """Catches non-image data-URI carriers bypassing payload classification."""

        cases = (
            ("application/pdf", "pdf", b"%PDF-2.0\n"),
            ("text/plain", "elf", b"\x7fELF\x02\x01\x01" + b"\x00" * 9),
        )
        for media_type, opaque_format, payload in cases:
            with self.subTest(media_type=media_type):
                destination = self.base / f"data-uri-{opaque_format}.tar.gz"
                encoded = base64.b64encode(payload).decode("ascii")
                value = f"data:{media_type};base64,{encoded}"
                self.create(
                    out=destination,
                    collectors=(("opaque", lambda value=value: {"value": value}),),
                )
                with tarfile.open(destination, "r:gz") as archive:
                    section = json.loads(
                        archive.extractfile("collectors/opaque.json").read()
                    )
                self.assertEqual(
                    {
                        "status": "unavailable",
                        "reason_code": "snapshot_opaque_member",
                        "opaque_format": opaque_format,
                        "key_path": "$.value",
                    },
                    section,
                )

    def test_data_uri_with_omitted_media_type_is_typed_like_any_carrier(self) -> None:
        """D1b: catches the mediatype-omitted carrier escaping opaque typing."""

        cases = (
            ("pdf", b"%PDF-2.0\n"),
            ("zip", b"PK\x03\x04" + b"\x00" * 8),
        )
        for opaque_format, payload in cases:
            with self.subTest(opaque_format=opaque_format):
                destination = self.base / f"omitted-mediatype-{opaque_format}.tar.gz"
                encoded = base64.b64encode(payload).decode("ascii")
                value = f"data:;base64,{encoded}"
                self.create(
                    out=destination,
                    collectors=(("opaque", lambda value=value: {"value": value}),),
                )
                with tarfile.open(destination, "r:gz") as archive:
                    section = json.loads(
                        archive.extractfile("collectors/opaque.json").read()
                    )
                self.assertEqual(
                    {
                        "status": "unavailable",
                        "reason_code": "snapshot_opaque_member",
                        "opaque_format": opaque_format,
                        "key_path": "$.value",
                    },
                    section,
                )

    def test_omitted_media_type_carrier_with_ordinary_text_stays_text(self) -> None:
        """D1b control: the omitted form must not widen into a media-type fence."""

        encoded = base64.b64encode(b"ordinary support text").decode("ascii")
        value = f"data:;base64,{encoded}"

        self.create(collectors=(("text", lambda: {"value": value}),))
        section = json.loads(self.members()["collectors/text.json"])
        self.assertEqual({"value": value}, section)

    def test_data_uri_with_ordinary_base64_remains_json_text(self) -> None:
        """Catches carrier normalization broadening into a media-type fence."""

        encoded = base64.b64encode(b"ordinary support text").decode("ascii")
        value = f"data:application/octet-stream;base64,{encoded}"

        self.create(collectors=(("text", lambda: {"value": value}),))
        section = json.loads(self.members()["collectors/text.json"])
        self.assertEqual({"value": value}, section)

    def test_data_uri_and_mime_wrapping_are_normalized_before_image_fence(self) -> None:
        """Catches the common inline-image carrier bypassing strict base64 decode."""

        from PIL import Image

        rendered = io.BytesIO()
        Image.new("RGB", (2, 2), "navy").save(rendered, format="GIF")
        encoded = base64.b64encode(rendered.getvalue()).decode("ascii")
        wrapped = "\r\n".join(
            encoded[index : index + 12] for index in range(0, len(encoded), 12)
        ) + "\n"

        self.create(
            collectors=(
                (
                    "image",
                    lambda: {
                        "nested": [{"capture": f"data:image/gif;base64,{wrapped}"}]
                    },
                ),
            )
        )
        section = json.loads(self.members()["collectors/image.json"])
        self.assertEqual(
            {
                "status": "unavailable",
                "reason_code": "snapshot_opaque_member",
                "opaque_format": "gif",
                "key_path": "$.nested[0].capture",
            },
            section,
        )

    def test_bmp_signature_requires_declared_size_to_match_payload(self) -> None:
        """Catches random BM-prefixed data being discarded as a BMP image."""

        false_positive = b"BM" + (70).to_bytes(4, "little") + b"\x00" * 32
        encoded = base64.b64encode(false_positive).decode("ascii")

        self.create(collectors=(("text", lambda: {"value": encoded}),))
        section = json.loads(self.members()["collectors/text.json"])
        self.assertEqual({"value": encoded}, section)

    def test_bmp_signature_requires_plausible_reserved_bytes_and_pixel_offset(self) -> None:
        """Catches BM-prefixed payloads without a plausible bitmap file header."""

        malformed_headers = {
            "reserved": b"BM"
            + (70).to_bytes(4, "little")
            + b"\x01\x00\x00\x00"
            + (14).to_bytes(4, "little")
            + b"\x00" * 56,
            "pixel-offset": b"BM"
            + (70).to_bytes(4, "little")
            + b"\x00" * 4
            + (70).to_bytes(4, "little")
            + b"\x00" * 56,
        }
        for name, payload in malformed_headers.items():
            with self.subTest(name=name):
                destination = self.base / f"bmp-{name}.tar.gz"
                encoded = base64.b64encode(payload).decode("ascii")
                self.create(
                    out=destination,
                    collectors=(("text", lambda encoded=encoded: {"value": encoded}),),
                )
                with tarfile.open(destination, "r:gz") as archive:
                    section = json.loads(
                        archive.extractfile("collectors/text.json").read()
                    )
                self.assertEqual({"value": encoded}, section)

    def test_non_base64_character_after_signature_prefix_prevents_classification(self) -> None:
        """Catches bounded signature decoding without whole-string alphabet evidence."""

        encoded = base64.b64encode(b"GIF89a" + b"\x00" * 64).decode("ascii") + "!"

        self.create(collectors=(("text", lambda: {"value": encoded}),))
        section = json.loads(self.members()["collectors/text.json"])
        self.assertEqual({"value": encoded}, section)

    def test_truncated_image_base64_is_classified_from_bounded_prefix(self) -> None:
        """Catches an incomplete opaque payload bypassing whole-string validation."""

        from PIL import Image

        rendered = io.BytesIO()
        Image.new("RGB", (2, 2), "navy").save(rendered, format="GIF")
        encoded = base64.b64encode(rendered.getvalue()).decode("ascii")[:-3]

        self.create(collectors=(("image", lambda: {"capture": encoded}),))
        section = json.loads(self.members()["collectors/image.json"])
        self.assertEqual(
            {
                "status": "unavailable",
                "reason_code": "snapshot_opaque_member",
                "opaque_format": "gif",
                "key_path": "$.capture",
            },
            section,
        )

    def test_base64_encoded_text_remains_json_text(self) -> None:
        """Catches the image fence broadening into a generic base64 heuristic."""

        encoded = base64.b64encode(b"ordinary support text").decode("ascii")

        self.create(collectors=(("text", lambda: {"value": encoded}),))
        section = json.loads(self.members()["collectors/text.json"])
        self.assertEqual({"value": encoded}, section)

    def test_installed_tree_imports_shared_fence_without_scripts_on_sys_path(self) -> None:
        from floati.manifest import _deployable_paths

        source = Path.cwd()
        installed = self.base / "installed"
        for relative in _deployable_paths(source):
            destination = installed / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, destination)

        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(installed)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json,sys; "
                    "import floati.identity_fence as identity; "
                    "import floati.support_bundle as support; "
                    "support.identity_gate(b'public-safe'); "
                    "print(json.dumps({'shared': support.HOME_PREFIX is identity.HOME_PREFIX, "
                    "'scripts_on_path': any(p.endswith('/scripts') for p in sys.path)}))"
                ),
            ],
            cwd=self.base,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            {"shared": True, "scripts_on_path": False},
            json.loads(completed.stdout),
        )

    def test_snapshot_directory_collision_and_size_cap_refuse_without_partial(self) -> None:
        with self.assertRaises(ProtocolRefusal) as collision:
            self.create(out=self.root.path / ".floati-snapshots" / "support.tar.gz")
        self.assertEqual("snapshot_output_reserved", collision.exception.code)

        with self.assertRaises(ProtocolRefusal) as too_large:
            self.create(max_bytes=1)
        self.assertEqual("snapshot_bundle_too_large", too_large.exception.code)
        self.assertFalse(self.out.exists())

    def test_snapshot_cli_has_exact_shape_and_is_denied_from_mcp(self) -> None:
        from floati.cli import _parser
        from floati.command_contract import describe_parser, project_mcp_surface

        parser = _parser()
        contract = describe_parser(parser)
        command = next(row for row in contract["commands"] if row["path"] == ["snapshot"])
        arguments = {row["name"]: row for row in command["arguments"]}
        self.assertEqual({"root", "out", "lines", "yes"}, set(arguments))
        self.assertTrue(arguments["root"]["required"])
        self.assertTrue(arguments["out"]["required"])
        self.assertEqual(240, arguments["lines"]["default"])
        self.assertFalse(arguments["yes"]["default"])

        surface = project_mcp_surface(parser)
        self.assertIn(["snapshot"], surface["denied_paths"])
        self.assertNotIn("snapshot", {tool["name"] for tool in surface["tools"]})

    def test_snapshot_cli_routes_consent_and_emits_final_artifact(self) -> None:
        from floati.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        result = {
            "written": True,
            "out": str(self.out),
            "bytes": 10,
            "collector_count": 1,
            "manifest_sha256": "a" * 64,
        }
        with mock.patch(
            "floati.support_bundle.create_support_bundle", return_value=result
        ) as create, redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "snapshot",
                    "--root",
                    str(self.root.path),
                    "--out",
                    str(self.out),
                    "--lines",
                    "12",
                    "--yes",
                ]
            )

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("snapshot", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(result, artifact["evidence"])
        self.assertEqual(12, create.call_args.kwargs["lines"])
        self.assertTrue(create.call_args.kwargs["yes"])

    def test_snapshot_help_uses_the_ruled_contract(self) -> None:
        from floati.helptext import help_for

        page = help_for(["snapshot", "--help"])
        self.assertIsNotNone(page)
        self.assertIn(
            "floati snapshot --root ROOT --out PATH [--lines N] [--yes]", page
        )
        self.assertIn("maintainer", page)


if __name__ == "__main__":
    unittest.main()
