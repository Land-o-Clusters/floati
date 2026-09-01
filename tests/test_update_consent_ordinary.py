from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.update_consent import canonical_destination, load_update_trust
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MINISIGN_FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "minisign"


class UpdateConsentOrdinaryPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private = Path(
            tempfile.mkdtemp(prefix="floati-ci-r2-1-upd-", dir=REAL_TEMP_ROOT)
        )
        self.addCleanup(lambda: shutil.rmtree(self.private, ignore_errors=True))

    @staticmethod
    def _fixture_key_id(public_key: bytes) -> str:
        lines = public_key.splitlines()
        raw = base64.b64decode(lines[1], validate=True)
        return raw[2:10][::-1].hex().upper()

    def _write_install(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / ".floati-install").mkdir()
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

    def _tmp_spellings(self) -> tuple[Path, Path]:
        tmp = Path("\x2ftmp")
        private = Path("\x2fprivate/tmp")
        if not tmp.is_dir() or not private.is_dir():
            self.skipTest("this host does not expose both tmp directory spellings")
        if not tmp.is_symlink() or tmp.resolve() != private.resolve():
            self.skipTest(
                "this host does not reach the same temp directory through "
                "a tmp symlink and a real path"
            )
        return tmp, private

    def test_destination_accepts_both_tmp_spellings_of_the_same_directory(self) -> None:
        tmp, private = self._tmp_spellings()
        selected = Path(
            tempfile.mkdtemp(prefix="floati-ci-r2-1-dest-", dir=str(private))
        )
        self.addCleanup(lambda: shutil.rmtree(selected, ignore_errors=True))
        (selected / ".floati-install").mkdir()
        tmp_spelling = tmp / selected.name
        private_spelling = private / selected.name
        self.assertEqual(tmp_spelling.resolve(), private_spelling.resolve())
        try:
            self.assertEqual(canonical_destination(tmp_spelling), tmp_spelling)
            self.assertEqual(canonical_destination(private_spelling), private_spelling)
        except ProtocolRefusal as exc:
            self.fail(f"same directory under two tmp spellings refused {exc.code}")

    def test_destination_refuses_a_symlinked_leaf(self) -> None:
        real = self.private / "real-install"
        self._write_install(real)
        link = self.private / "linked-install"
        link.symlink_to(real)
        with self.assertRaises(ProtocolRefusal) as caught:
            canonical_destination(link)
        self.assertEqual("update_destination_invalid", caught.exception.code)
        self.assertNotIn("canonical", caught.exception.detail)

    def test_load_update_trust_accepts_public_key_under_tmp_spelling(self) -> None:
        tmp, private = self._tmp_spellings()
        selected = Path(
            tempfile.mkdtemp(prefix="floati-ci-r2-1-trust-", dir=str(private))
        )
        self.addCleanup(lambda: shutil.rmtree(selected, ignore_errors=True))
        self._write_install(selected)
        tmp_spelling = tmp / selected.name
        try:
            trust = load_update_trust(tmp_spelling)
        except ProtocolRefusal as exc:
            self.fail(f"trust under tmp spelling refused {exc.code}")
        self.assertEqual(
            hashlib.sha256((selected / "trust" / "fixture.pub").read_bytes()).hexdigest(),
            trust["public_key_sha256"],
        )

    def test_load_update_trust_refuses_a_symlinked_leaf_public_key(self) -> None:
        destination = self.private / "install"
        self._write_install(destination)
        real = destination / "trust" / "fixture.pub"
        linked = destination / "trust" / "linked.pub"
        linked.symlink_to(real)
        metadata = destination / "trust" / "keys.json"
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        payload["keys"][0]["public_key_file"] = "linked.pub"
        metadata.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            load_update_trust(destination)
        self.assertEqual("update_trust_unprovisioned", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
