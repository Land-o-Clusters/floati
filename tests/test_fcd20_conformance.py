from __future__ import annotations

import copy
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from floati import host_paths
from floati.errors import ProtocolRefusal


class FCD20HostPathTests(unittest.TestCase):
    def test_runner_scratch_parent_follows_the_patched_host_platform(self) -> None:
        for platform, expected in (
            ("darwin", "\x2fprivate/tmp/floati-fcd20"),
            ("linux", "\x2ftmp/floati-fcd20"),
        ):
            with self.subTest(platform=platform), patch.object(
                host_paths.sys, "platform", platform
            ):
                self.assertEqual(expected, str(host_paths.fcd20_scratch_parent()))

    def test_host_paths_has_no_third_party_harness_candidate_policy(self) -> None:
        self.assertFalse(hasattr(host_paths, "harness_executable_candidates"))

    def test_python_executable_preserves_the_running_interpreter_path(self) -> None:
        with patch.object(host_paths.sys, "executable", "/host/python3"):
            self.assertEqual(Path("/host/python3"), host_paths.python_executable())


class FCD20ArtifactTests(unittest.TestCase):
    @staticmethod
    def _host() -> dict[str, object]:
        return {
            "platform": "darwin",
            "machine": "arm64",
            "python_version": "3.9.6",
        }

    def test_rows_are_fixed_and_ordered(self) -> None:
        from floati import fcd20_conformance as fcd20

        def resolve(spec: fcd20.RowSpec) -> fcd20.ExecutableResolution:
            path = Path("/fixture") / spec.harness
            return fcd20.ExecutableResolution(path, (path,))

        def probe(_spec: fcd20.RowSpec, _path: Path) -> fcd20.ProbeResult:
            return fcd20.ProbeResult(
                exit_code=0,
                timed_out=False,
                stdout_size=4,
                stdout_sha256="a" * 64,
                stderr_size=0,
                stderr_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                duration_ms=1,
                version="1.0",
            )

        artifact = fcd20.run(resolve=resolve, probe=probe, host=self._host)
        self.assertEqual(
            [f"C{index}" for index in range(1, 10)],
            [row["row"] for row in artifact["evidence"]["rows"]],
        )
        self.assertEqual(
            ["measured"] * 9,
            [row["status"] for row in artifact["evidence"]["rows"]],
        )
        self.assertEqual("ok", artifact["status"])

    def test_host_condition_and_probe_failure_are_not_collapsed(self) -> None:
        from floati import fcd20_conformance as fcd20

        def resolve(spec: fcd20.RowSpec) -> fcd20.ExecutableResolution:
            path = Path("/fixture") / spec.harness
            if spec.row == "C2":
                return fcd20.ExecutableResolution(None, ())
            return fcd20.ExecutableResolution(path, (path,))

        def probe(spec: fcd20.RowSpec, _path: Path) -> fcd20.ProbeResult:
            if spec.row == "C3":
                raise AssertionError("perturbation")
            return fcd20.ProbeResult(
                exit_code=0,
                timed_out=False,
                stdout_size=0,
                stdout_sha256="0" * 64,
                stderr_size=0,
                stderr_sha256="0" * 64,
                duration_ms=0,
                version=None,
            )

        artifact = fcd20.run(resolve=resolve, probe=probe, host=self._host)
        rows = {row["row"]: row for row in artifact["evidence"]["rows"]}
        self.assertEqual("host_condition", rows["C2"]["status"])
        self.assertEqual(
            "fcd20_claude_executable_undeclared",
            rows["C2"]["evidence"]["code"],
        )
        self.assertEqual([], rows["C2"]["evidence"]["paths"])
        self.assertIn(
            "--claude-executable", rows["C2"]["evidence"]["remedy"]
        )
        self.assertEqual("probe_failed", rows["C3"]["status"])
        self.assertEqual(
            "fcd20_probe_internal_failure", rows["C3"]["evidence"]["code"]
        )
        self.assertEqual("probe_failed", artifact["status"])
        self.assertNotIn("skip", json.dumps(artifact))

    def test_nonzero_process_exit_is_measured(self) -> None:
        from floati import fcd20_conformance as fcd20

        def resolve(spec: fcd20.RowSpec) -> fcd20.ExecutableResolution:
            path = Path("/fixture") / spec.harness
            return fcd20.ExecutableResolution(path, (path,))

        def probe(_spec: fcd20.RowSpec, _path: Path) -> fcd20.ProbeResult:
            return fcd20.ProbeResult(
                exit_code=7,
                timed_out=False,
                stdout_size=0,
                stdout_sha256="0" * 64,
                stderr_size=5,
                stderr_sha256="b" * 64,
                duration_ms=3,
                version=None,
            )

        artifact = fcd20.run(resolve=resolve, probe=probe, host=self._host)
        row = artifact["evidence"]["rows"][0]
        self.assertEqual("measured", row["status"])
        self.assertEqual(7, row["evidence"]["exit_code"])

    def test_validator_rejects_row_reordering_and_skip_keys(self) -> None:
        from floati import fcd20_conformance as fcd20

        def resolve(spec: fcd20.RowSpec) -> fcd20.ExecutableResolution:
            return fcd20.ExecutableResolution(None, ())

        artifact = fcd20.run(resolve=resolve, probe=None, host=self._host)

        reordered = copy.deepcopy(artifact)
        rows = reordered["evidence"]["rows"]
        rows[0], rows[1] = rows[1], rows[0]
        with self.assertRaises(ProtocolRefusal) as raised:
            fcd20.validate_artifact(reordered)
        self.assertEqual("fcd20_artifact_invalid", raised.exception.code)

        skipped = copy.deepcopy(artifact)
        skipped["evidence"]["rows"][0]["skip"] = True
        with self.assertRaises(ProtocolRefusal) as raised:
            fcd20.validate_artifact(skipped)
        self.assertEqual("fcd20_artifact_invalid", raised.exception.code)

    def test_validator_rejects_malformed_status_specific_fields(self) -> None:
        from floati import fcd20_conformance as fcd20

        def resolve(spec: fcd20.RowSpec) -> fcd20.ExecutableResolution:
            path = Path("/fixture") / spec.harness
            return fcd20.ExecutableResolution(path, (path,))

        def probe(_spec: fcd20.RowSpec, _path: Path) -> fcd20.ProbeResult:
            return fcd20.ProbeResult(
                exit_code=0,
                timed_out=False,
                stdout_size=0,
                stdout_sha256="0" * 64,
                stderr_size=0,
                stderr_sha256="0" * 64,
                duration_ms=0,
                version=None,
            )

        measured = fcd20.run(resolve=resolve, probe=probe, host=self._host)
        malformed = copy.deepcopy(measured)
        malformed["evidence"]["host"]["platform"] = 7
        with self.assertRaises(ProtocolRefusal) as raised:
            fcd20.validate_artifact(malformed)
        self.assertEqual("fcd20_artifact_invalid", raised.exception.code)

        malformed = copy.deepcopy(measured)
        malformed["evidence"]["rows"][0]["evidence"]["stdout_size"] = "0"
        with self.assertRaises(ProtocolRefusal) as raised:
            fcd20.validate_artifact(malformed)
        self.assertEqual("fcd20_artifact_invalid", raised.exception.code)

        def absent(spec: fcd20.RowSpec) -> fcd20.ExecutableResolution:
            return fcd20.ExecutableResolution(None, ())

        host_condition = fcd20.run(
            resolve=absent,
            probe=probe,
            host=self._host,
        )
        malformed = copy.deepcopy(host_condition)
        malformed["evidence"]["rows"][0]["evidence"]["detail"] = None
        with self.assertRaises(ProtocolRefusal) as raised:
            fcd20.validate_artifact(malformed)
        self.assertEqual("fcd20_artifact_invalid", raised.exception.code)

        malformed = copy.deepcopy(measured)
        malformed["status"] = "probe_failed"
        malformed["evidence"]["rows"][0] = {
            "row": "C1",
            "harness": "codex",
            "status": "probe_failed",
            "evidence": {"code": "broken", "detail": "broken", "path": 7},
        }
        with self.assertRaises(ProtocolRefusal) as raised:
            fcd20.validate_artifact(malformed)
        self.assertEqual("fcd20_artifact_invalid", raised.exception.code)

    def test_cannot_see_is_derived_from_host_and_keeps_an_exact_count(self) -> None:
        from floati import fcd20_conformance as fcd20

        def absent(_spec: fcd20.RowSpec) -> fcd20.ExecutableResolution:
            return fcd20.ExecutableResolution(None, ())

        linux_host = dict(self._host())
        linux_host["platform"] = "linux"
        linux = fcd20.run(
            resolve=absent,
            probe=None,
            host=lambda: linux_host,
        )
        linux_cannot_see = linux["evidence"]["cannot_see"]
        self.assertEqual(3, len(linux_cannot_see))
        self.assertNotIn(
            "linux_measurements_from_a_non_linux_host",
            linux_cannot_see,
        )

        missing = copy.deepcopy(linux)
        missing["evidence"]["cannot_see"].pop()
        with self.assertRaises(ProtocolRefusal) as raised:
            fcd20.validate_artifact(missing)
        self.assertEqual("fcd20_artifact_invalid", raised.exception.code)

        stale = copy.deepcopy(linux)
        stale["evidence"]["cannot_see"].insert(
            0, "linux_measurements_from_a_non_linux_host"
        )
        with self.assertRaises(ProtocolRefusal) as raised:
            fcd20.validate_artifact(stale)
        self.assertEqual("fcd20_artifact_invalid", raised.exception.code)

        darwin = fcd20.run(
            resolve=absent,
            probe=None,
            host=self._host,
        )
        self.assertEqual(4, len(darwin["evidence"]["cannot_see"]))
        self.assertEqual(
            "linux_measurements_from_a_non_linux_host",
            darwin["evidence"]["cannot_see"][0],
        )


class FCD20CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            dir=host_paths.capture_temporary_parent()
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True)

    @staticmethod
    def _host() -> dict[str, object]:
        return {
            "platform": "darwin",
            "machine": "arm64",
            "python_version": "3.9.6",
        }

    def _executable(self, name: str, body: str) -> Path:
        path = self.root / name
        interpreter = host_paths.python_executable()
        path.write_text(
            f"#!{interpreter}\n{body}\n",
            encoding="utf-8",
        )
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return path

    def test_fixture_executables_run_all_nine_rows_end_to_end(self) -> None:
        from floati import fcd20_conformance as fcd20

        executables = {
            spec.harness: self._executable(
                spec.harness,
                f'print("{spec.harness} fixture 1.0")',
            )
            for spec in fcd20.ROWS
        }

        argv = []
        for spec in fcd20.ROWS:
            argv.extend(
                (f"--{spec.harness}-executable", str(executables[spec.harness]))
            )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = fcd20.main(
                argv, probe=fcd20.probe_version, host=self._host
            )
        self.assertEqual(0, exit_code)
        artifact = json.loads(stdout.getvalue())
        self.assertEqual(
            [f"C{index}" for index in range(1, 10)],
            [row["row"] for row in artifact["evidence"]["rows"]],
        )
        self.assertEqual(
            {"measured"},
            {row["status"] for row in artifact["evidence"]["rows"]},
        )

    def test_undeclared_harness_ignores_a_path_decoy_with_a_remedy(self) -> None:
        from floati import fcd20_conformance as fcd20

        decoy = self._executable("codex", 'print("decoy")')
        stdout = io.StringIO()
        with patch.dict(os.environ, {"PATH": str(decoy.parent)}), contextlib.redirect_stdout(
            stdout
        ):
            exit_code = fcd20.main([], host=self._host)
        self.assertEqual(32, exit_code)
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("degraded", artifact["status"])
        self.assertEqual(
            {"host_condition"},
            {row["status"] for row in artifact["evidence"]["rows"]},
        )
        first = artifact["evidence"]["rows"][0]
        self.assertEqual(
            "fcd20_codex_executable_undeclared", first["evidence"]["code"]
        )
        self.assertIn("--codex-executable", first["evidence"]["remedy"])
        self.assertEqual([], first["evidence"]["paths"])

    def test_partial_measurement_exits_zero(self) -> None:
        from floati import fcd20_conformance as fcd20

        executable = self._executable("codex", 'print("codex fixture 1.0")')
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = fcd20.main(
                ["--codex-executable", str(executable)],
                probe=fcd20.probe_version,
                host=self._host,
            )
        self.assertEqual(0, exit_code)
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("measured", artifact["evidence"]["rows"][0]["status"])
        self.assertEqual(
            {"measured", "host_condition"},
            {row["status"] for row in artifact["evidence"]["rows"]},
        )

    def test_probe_failure_exits_operation_died(self) -> None:
        from floati import fcd20_conformance as fcd20

        executable = self._executable("broken-probe", 'print("unused")')

        def resolve(_spec: fcd20.RowSpec) -> fcd20.ExecutableResolution:
            return fcd20.ExecutableResolution(executable, (executable,))

        def broken_probe(
            spec: fcd20.RowSpec, path: Path
        ) -> fcd20.ProbeResult:
            if spec.row == "C2":
                raise AssertionError("probe needle")
            return fcd20.probe_version(spec, path)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = fcd20.main(
                [],
                resolve=resolve,
                probe=broken_probe,
                host=self._host,
            )
        self.assertEqual(30, exit_code)
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("probe_failed", artifact["status"])
        self.assertEqual(
            {"measured", "probe_failed"},
            {row["status"] for row in artifact["evidence"]["rows"]},
        )

    def test_invalid_declared_executable_is_one_json_refusal(self) -> None:
        from floati import fcd20_conformance as fcd20

        target = self._executable("real-codex", 'print("real")')
        link = self.root / "codex-link"
        link.symlink_to(target)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = fcd20.main(
                ["--codex-executable", str(link)], host=self._host
            )
        self.assertEqual(20, exit_code)
        self.assertEqual("", stderr.getvalue())
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(
            "fcd20_codex_executable_invalid", artifact["evidence"]["code"]
        )
        self.assertIn("--codex-executable", artifact["evidence"]["remedy"])

    def test_timeout_is_probe_failed(self) -> None:
        from floati import fcd20_conformance as fcd20

        sleeper = self._executable(
            "sleeper",
            "import time\ntime.sleep(1)\nprint('late')",
        )
        result = fcd20.probe_version(fcd20.ROWS[0], sleeper, timeout=0.02)
        self.assertTrue(result.timed_out)

        def resolve(spec: fcd20.RowSpec) -> fcd20.ExecutableResolution:
            return fcd20.ExecutableResolution(sleeper, (sleeper,))

        artifact = fcd20.run(
            resolve=resolve,
            probe=lambda _spec, _path: result,
            host=self._host,
        )
        self.assertEqual(
            "probe_failed", artifact["evidence"]["rows"][0]["status"]
        )

    def test_output_overflow_is_a_typed_probe_failure(self) -> None:
        from floati import fcd20_conformance as fcd20

        noisy = self._executable("noisy", 'print("x" * 256)')
        with self.assertRaises(ProtocolRefusal) as raised:
            fcd20.probe_version(
                fcd20.ROWS[0], noisy, timeout=1.0, output_limit=128
            )
        self.assertEqual("fcd20_probe_output_overflow", raised.exception.code)

    def test_runner_source_has_no_host_prefix_literal(self) -> None:
        source = Path("floati/fcd20_conformance.py").read_text(encoding="utf-8")
        forbidden = ("\x2fprivate/tmp", "\x2ftmp", "/opt/homebrew", "\x2fUsers/")
        self.assertEqual([], [value for value in forbidden if value in source])

    def test_declared_executable_passes_through_shared_house_predicate(self) -> None:
        from floati import fcd20_conformance as fcd20

        executable = self._executable("shared-predicate", 'print("ok")')
        with patch.object(
            fcd20,
            "_explicit_executable",
            wraps=fcd20._explicit_executable,
        ) as explicit:
            declarations = fcd20.validate_declarations(
                {"codex": executable}
            )
        self.assertEqual(executable, declarations["codex"])
        explicit.assert_called_once_with(
            executable, "fcd20_codex_executable_invalid"
        )

    def test_selected_executable_outside_single_declared_candidate_is_rejected(self) -> None:
        from floati import fcd20_conformance as fcd20

        declared = self.root / "declared"
        selected = self.root / "selected"

        def resolve(_spec: fcd20.RowSpec) -> fcd20.ExecutableResolution:
            return fcd20.ExecutableResolution(selected, (declared,))

        artifact = fcd20.run(
            resolve=resolve,
            probe=fcd20.probe_version,
            host=self._host,
        )
        first = artifact["evidence"]["rows"][0]
        self.assertEqual("probe_failed", first["status"])
        self.assertEqual("fcd20_artifact_invalid", first["evidence"]["code"])

    def test_broken_resolver_and_probe_never_claim_a_host_condition(self) -> None:
        from floati import fcd20_conformance as fcd20

        executable = self._executable("broken-probe", 'print("unused")')

        def broken_resolve(_spec: fcd20.RowSpec) -> fcd20.ExecutableResolution:
            raise AssertionError("resolver needle")

        artifact = fcd20.run(
            resolve=broken_resolve,
            probe=fcd20.probe_version,
            host=self._host,
        )
        self.assertEqual(
            {"probe_failed"},
            {row["status"] for row in artifact["evidence"]["rows"]},
        )
        self.assertEqual(
            {"fcd20_probe_internal_failure"},
            {row["evidence"]["code"] for row in artifact["evidence"]["rows"]},
        )

        def resolved(_spec: fcd20.RowSpec) -> fcd20.ExecutableResolution:
            return fcd20.ExecutableResolution(executable, (executable,))

        def broken_probe(_spec: fcd20.RowSpec, _path: Path) -> fcd20.ProbeResult:
            raise AssertionError("probe needle")

        artifact = fcd20.run(
            resolve=resolved,
            probe=broken_probe,
            host=self._host,
        )
        self.assertEqual(
            {"probe_failed"},
            {row["status"] for row in artifact["evidence"]["rows"]},
        )

    def test_cli_argument_refusal_is_one_json_artifact(self) -> None:
        from floati import fcd20_conformance as fcd20

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = fcd20.main(["--invented"])
        self.assertEqual(20, exit_code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(1, len(stdout.getvalue().splitlines()))
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("arguments_invalid", artifact["evidence"]["code"])


if __name__ == "__main__":
    unittest.main()
