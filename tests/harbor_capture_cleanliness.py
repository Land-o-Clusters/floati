"""Opt-in native control for receipt-fixture contamination of capture sources."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import harbor_capture_shot1_locf1, test_confluence_receipts_v2
from tests.temp_roots import REAL_TEMP_ROOT


SOURCE = Path(__file__).resolve().parents[1]
CAPTURE_ID = "test_shot_moments_are_the_exact_three_missed_frames"


class CaptureCleanlinessDiagnostic(unittest.TestCase):
    def test_real_receipt_fixture_keeps_native_capture_source_clean(self):
        native_run = subprocess.run
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GIT_")}
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"

        def git(root, *arguments):
            result = native_run(["/usr/bin/git", *arguments], cwd=root,
                                env=environment, capture_output=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
            return result.stdout

        def status(root):
            return git(root, "status", "--porcelain=v1", "--untracked-files=all").decode()

        def candidate_identity():
            names = sorted(set(git(SOURCE, "ls-files", "--cached", "--others",
                                   "--exclude-standard", "-z").decode().split("\0")) - {""})
            rows = []
            for name in names:
                path = SOURCE / name
                metadata = path.lstat()
                raw = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
                rows.append([name, stat.S_IMODE(metadata.st_mode), hashlib.sha256(raw).hexdigest()])
            return {"head": git(SOURCE, "rev-parse", "HEAD").decode().strip(),
                    "status": status(SOURCE), "paths": len(rows),
                    "digest": hashlib.sha256(json.dumps(rows).encode()).hexdigest()}

        before = candidate_identity()
        report = {"scope": "controlled exact-bundle scratch source; original full-run typed cause remains unknown",
                  "candidate_before": before, "captures": [], "installs": []}
        bank = None
        try:
            with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="capture-cleanliness-") as raw:
                bank = Path(raw)
                source = bank / "source"
                source.mkdir(mode=0o700)
                manifest_bytes = (SOURCE / "bundle-manifest.v0.json").read_bytes()
                manifest = json.loads(manifest_bytes)
                bindings = []
                for entry in manifest["files"]:
                    relative = entry["path"]
                    origin = SOURCE / relative
                    actual = hashlib.sha256(origin.read_bytes()).hexdigest()
                    self.assertEqual(entry["sha256"], actual, relative)
                    destination = source / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(origin, destination)
                    self.assertEqual(origin.read_bytes(), destination.read_bytes(), relative)
                    bindings.append({"path": relative, "sha256": actual})
                (source / "bundle-manifest.v0.json").write_bytes(manifest_bytes)
                git(source, "init", "--quiet", "--initial-branch=codex/capture-cleanliness")
                git(source, "config", "user.name", "Floati Fixture")
                git(source, "config", "user.email", "floati-fixture@example.invalid")
                git(source, "add", "--all")
                git(source, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                    "commit", "--quiet", "-m", "Exact manifest capture source")
                report["source"] = {"path": str(source),
                    "head": git(source, "rev-parse", "HEAD").decode().strip(),
                    "status": status(source), "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    "files": bindings}
                self.assertEqual("", report["source"]["status"])
                original_loader = harbor_capture_shot1_locf1.load_capture_module

                def load_owned_capture():
                    capture = original_loader()
                    capture.REPOSITORY_ROOT = source
                    return capture

                def run_capture(label):
                    def observe_run(argv, *arguments, **keywords):
                        vector = [str(value) for value in argv]
                        selected = (len(vector) >= 2 and vector[1] == "install"
                                    and "--source" in vector
                                    and "--committed-tree" in vector)
                        if selected:
                            install_source = Path(vector[vector.index("--source") + 1])
                            self.assertEqual(install_source / "scripts/floati", Path(vector[0]))
                            # The capture owns a committed checkout of the
                            # declared fixture. Bind its tree, rather than
                            # expecting the install to run from the caller.
                            self.assertEqual(git(source, "rev-parse", "HEAD^{tree}"),
                                             git(install_source, "rev-parse", "HEAD^{tree}"))
                        prior = status(source) if selected else None
                        result = native_run(argv, *arguments, **keywords)
                        if selected:
                            report["installs"].append({"phase": label, "argv": vector,
                                "status_before": prior, "status_after": status(source),
                                "returncode": result.returncode, "stdout": result.stdout,
                                "stderr": result.stderr})
                        return result
                    result = unittest.TestResult()
                    with mock.patch.object(harbor_capture_shot1_locf1, "load_capture_module", load_owned_capture),\
                            mock.patch.object(subprocess, "run", observe_run):
                        harbor_capture_shot1_locf1.CaptureMomentTests(CAPTURE_ID).run(result)
                    report["captures"].append({"phase": label, "tests_run": result.testsRun,
                        "errors": [detail for _case, detail in result.errors],
                        "failures": [detail for _case, detail in result.failures],
                        "skipped": [reason for _case, reason in result.skipped],
                        "successful": result.wasSuccessful()})

                run_capture("baseline")
                fixture = test_confluence_receipts_v2.ReceiptsReadV2Tests("test_envelope_carries_sender_and_zero_exclusions")
                try:
                    with mock.patch.object(test_confluence_receipts_v2, "REPOSITORY_ROOT", source):
                        fixture.setUp()
                        fixture._seed_message()
                        report["held_fixture"] = {"base": str(fixture.base),
                            "home": os.environ["HOME"], "root": str(fixture.root.path),
                            "source_status": status(source)}
                        run_capture("held_real_fixture")
                finally:
                    self.assertTrue(fixture.doCleanups())
                report["source_after_fixture"] = {"head": git(source, "rev-parse", "HEAD").decode().strip(),
                                                   "status": status(source)}
                run_capture("after_cleanup")
        finally:
            report["scratch_removed"] = bank is not None and not bank.exists()
            report["candidate_after"] = candidate_identity()
            print(json.dumps(report, sort_keys=True))
        self.assertEqual(before, report["candidate_after"])
        self.assertTrue(report["scratch_removed"])
        self.assertEqual(report["source"]["head"], report["source_after_fixture"]["head"])
        self.assertEqual("", report["source_after_fixture"]["status"])
        self.assertEqual(["baseline", "held_real_fixture", "after_cleanup"],
                         [row["phase"] for row in report["captures"]])
        self.assertEqual([1, 1, 1], [row["tests_run"] for row in report["captures"]])
        self.assertEqual([[], [], []], [row["skipped"] for row in report["captures"]])
        self.assertEqual([0, 0, 0], [row["returncode"] for row in report["installs"]])
        self.assertEqual([True, True, True], [row["successful"] for row in report["captures"]])
        self.assertEqual("", report["held_fixture"]["source_status"])
