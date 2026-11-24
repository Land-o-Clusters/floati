"""Real bank publication controls; no census, child, Git or fleet operations."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tests.temp_roots import REAL_TEMP_ROOT


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py"
PREFIX = "bus-scale-census-2026-10-09-"
CAP = 32 * 1024 * 1024


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def inventory(home):
    """Retain actual file bytes and metadata when checking publication."""
    result = {}
    for path in sorted(home.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        row = {"mode": stat.S_IMODE(info.st_mode), "mtime_ns": info.st_mtime_ns}
        if not stat.S_ISREG(info.st_mode):
            raise AssertionError("control fixture contains a nonordinary object")
        row.update(kind="file", bytes=info.st_size, sha256=digest(path))
        result[path.relative_to(home).as_posix()] = row
    return result


class BankAtomicityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bus-scale-bank-controls-", dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.private_temp = self.base / "input"
        self.private_temp.mkdir(mode=0o700)
        self.private_root = self.base / "destination"
        self.evidence = self.private_root / "docs/evidence"
        self.evidence.mkdir(mode=0o700, parents=True)
        specification = importlib.util.spec_from_file_location("real_bus_scale_bank_controls", RUNNER)
        self.runner = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(self.runner)
        self.scope = patch.multiple(self.runner, ROOT=self.private_root, TEMP=self.private_temp)
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def fixture(self, attempt, *, archive_bytes=31):
        output = self.private_temp / (PREFIX + attempt)
        output.mkdir(mode=0o700)
        (output / "terminal.txt").write_bytes(b"collected native observation refused; raw retained\n")
        archive = output / "census-raw.tar.gz"
        # bank() transports opaque archive bytes; it does not extract or
        # interpret tar/gzip. Exercise the real cap with actual owned bytes,
        # without running a census or substituting a mocked size/hash.
        with archive.open("xb") as stream:
            remaining = archive_bytes
            block = b"x" * min(1024 * 1024, archive_bytes)
            while remaining:
                written = min(remaining, len(block))
                stream.write(block[:written])
                remaining -= written
        census = {"mode": "first-pass", "status": "incomplete", "child_collected": True,
                  "scratch_removed": True, "matrix": {},
                  "error": "retained native observation refusal",
                  "raw_archive_bytes": archive_bytes, "raw_archive_sha256": digest(archive)}
        receipt = {"action": "census-first-pass", "attempt": attempt, "status": "complete",
                   "exit_code": 1, "child_collected": True, "interrupted": False,
                   "scratch_removed": True, "source_unchanged": True, "content_unchanged": True,
                   "native": {"exit_code": 1, "native_home_removed": True},
                   "terminal_sha256": digest(output / "terminal.txt")}
        for name, value in (("receipt.json", receipt), ("census.json", census)):
            (output / name).write_text(json.dumps(value, sort_keys=True) + "\n")
        return output, {".txt": output / "terminal.txt", ".json": output / "receipt.json",
                        "-census.json": output / "census.json", "-census-raw.tar.gz": archive}

    def destination(self, attempt, suffix):
        return self.evidence / (PREFIX + attempt + suffix)

    def test_oversized_last_archive_refuses_before_any_metadata_is_published(self):
        attempt = "oversized-last"
        output, _members = self.fixture(attempt, archive_bytes=CAP + 1)
        source_before = inventory(output)
        with redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            self.runner.bank(attempt)
        self.assertEqual({}, inventory(self.evidence),
                         "a late member-cap refusal must not publish the first three metadata files")
        self.assertEqual(source_before, inventory(output), "refused source evidence must remain exact")

    def test_archives_at_or_below_cap_keep_the_complete_legacy_four_file_format(self):
        for size in (31, CAP):
            with self.subTest(archive_bytes=size):
                attempt = "legacy-positive-%d" % size
                existing_names = {path.name for path in self.evidence.iterdir()}
                output, members = self.fixture(attempt, archive_bytes=size)
                source_before = inventory(output)
                captured = io.StringIO()
                with redirect_stdout(captured):
                    code = self.runner.bank(attempt)
                self.assertEqual(0, code, "banking a collected failed observation is valid evidence publication")
                self.assertEqual(existing_names | {PREFIX + attempt + suffix for suffix in members},
                                 {path.name for path in self.evidence.iterdir()})
                for suffix, source in members.items():
                    self.assertEqual(source.read_bytes(), self.destination(attempt, suffix).read_bytes())
                artifact = json.loads(captured.getvalue())
                self.assertEqual("bank", artifact["action"])
                self.assertEqual(attempt, artifact["attempt"])
                self.assertEqual(4, len(artifact["files"]))
                self.assertEqual({str(self.destination(attempt, suffix)): digest(source) for suffix, source in members.items()},
                                 {row["path"]: row["sha256"] for row in artifact["files"]})
                self.assertEqual(source_before, inventory(output))
