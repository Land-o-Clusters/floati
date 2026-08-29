from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


try:
    import floati.installer_shadow as installer_shadow
    from floati.installer_shadow import enumerate_installer_shadow
except (ImportError, ModuleNotFoundError):
    installer_shadow = None
    enumerate_installer_shadow: Optional[Callable[..., dict[str, Any]]] = None


class InstallerShadowEnumeratorTests(unittest.TestCase):
    """The shared observer must prove only what its physical PATH scan saw."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def directory(self, name: str) -> Path:
        path = self.base / name
        path.mkdir()
        return path

    @staticmethod
    def write_slip(directory: Path, content: bytes) -> Path:
        path = directory / "floati"
        path.write_bytes(content)
        return path

    def bundle(self, name: str, content: bytes = b"authoritative\n") -> tuple[Path, Path]:
        root = self.directory(name)
        scripts = root / "scripts"
        scripts.mkdir()
        return root, self.write_slip(scripts, content)

    def observe(self, destination: Path, path: str, **kwargs: object) -> dict[str, Any]:
        if enumerate_installer_shadow is None:
            self.fail("missing shared installer-shadow enumerator")
        return enumerate_installer_shadow(destination, path=path, **kwargs)

    def test_destination_precedence_is_explicit_then_install_environment_only(self) -> None:
        """Falling back to the bus root or an ambient source path would cross the TD2/TD5 boundary."""
        if installer_shadow is None:
            self.fail("missing shared installer-shadow module")
        resolver = getattr(installer_shadow, "resolve_installer_destination", None)
        self.assertIsNotNone(resolver, "missing installer-destination resolver")
        explicit = self.directory("explicit")
        environment = self.directory("environment")
        environ = {
            "FLOATI_INSTALL_DESTINATION": str(environment),
            "FLOATI_BUS_ROOT": str(self.directory("bus-root")),
        }

        self.assertEqual(str(explicit), resolver(str(explicit), environ=environ))
        self.assertEqual(str(environment), resolver(None, environ=environ))
        self.assertIsNone(resolver(None, environ={"FLOATI_BUS_ROOT": environ["FLOATI_BUS_ROOT"]}))

    def test_closed_observation_exit_mapping_has_no_aliases(self) -> None:
        """Collapsing partial, absent, or found observations would erase their different truth claims."""
        if installer_shadow is None:
            self.fail("missing shared installer-shadow module")
        mapper = getattr(installer_shadow, "observation_exit_code", None)
        self.assertIsNotNone(mapper, "missing TD5 observation exit mapper")

        self.assertEqual(0, mapper({"outcome": "found"}))
        self.assertEqual(20, mapper({"outcome": "affirmative_none"}))
        self.assertEqual(21, mapper({"outcome": "unknown"}))
        self.assertEqual(22, mapper({"outcome": "cannot_speak"}))
        self.assertEqual(22, mapper({"outcome": "invented"}))

    def test_presenter_helper_resolves_only_the_ruled_destination_coordinate(self) -> None:
        """Letting a presenter choose its own destination would make its observation diverge from its peers."""
        if installer_shadow is None:
            self.fail("missing shared installer-shadow module")
        presenter = getattr(installer_shadow, "observe_installer_shadow", None)
        self.assertIsNotNone(presenter, "missing shared presenter observation helper")
        destination, _destination_slip = self.bundle("destination")
        command_root = destination / "scripts"
        environment = {"FLOATI_INSTALL_DESTINATION": str(destination)}

        artifact = presenter(None, path=str(command_root), environ=environment)
        absent = presenter(None, path=str(command_root), environ={})

        self.assertEqual("affirmative_none", artifact["outcome"])
        self.assertEqual("cannot_speak", absent["outcome"])
        self.assertEqual([], absent["enumerated_roots"])
        self.assertEqual("Every PATH entry was checked; the installed floati answers first.", artifact["reason"])
        self.assertEqual("No installer destination named; the shadow check could not run.", absent["reason"])

    def test_reports_preceding_resolvable_slips_in_physical_path_order(self) -> None:
        """Removing the pre-destination filter or sorting candidates would misreport the shadow chain."""
        first = self.directory("first")
        second = self.directory("second")
        destination, _destination_slip = self.bundle("destination")
        command_root = destination / "scripts"
        after = self.directory("after")
        first_slip = self.write_slip(first, b"first\n")
        second_slip = self.write_slip(second, b"second\n")
        self.write_slip(after, b"after\n")

        artifact = self.observe(
            destination,
            os.pathsep.join(str(path) for path in (first, second, command_root, after)),
        )

        self.assertEqual("found", artifact["outcome"])
        self.assertEqual(
            [str(path.resolve()) for path in (first, second, command_root, after)],
            artifact["enumerated_roots"],
        )
        self.assertEqual(
            [
                {
                    "path": str(first_slip.resolve()),
                    "digest": hashlib.sha256(b"first\n").hexdigest(),
                },
                {
                    "path": str(second_slip.resolve()),
                    "digest": hashlib.sha256(b"second\n").hexdigest(),
                },
            ],
            artifact["found"],
        )
        self.assertNotIn("blocked_entry", artifact)
        self.assertEqual("A floati ahead of the installed copy answered first on PATH.", artifact["reason"])

    def test_partial_path_scan_is_unknown_and_names_the_unreadable_entry(self) -> None:
        """Treating an unstattable PATH entry as none would manufacture a no-shadow claim."""
        first = self.directory("first")
        missing = self.base / "missing"
        destination, _destination_slip = self.bundle("destination")
        command_root = destination / "scripts"

        artifact = self.observe(
            destination,
            os.pathsep.join(str(path) for path in (first, missing, command_root)),
        )

        self.assertEqual("unknown", artifact["outcome"])
        self.assertEqual([str(first.resolve())], artifact["enumerated_roots"])
        self.assertEqual([], artifact["found"])
        self.assertEqual(str(missing), artifact["blocked_entry"])
        self.assertEqual("Some PATH entries could not be read; shadow state unknown.", artifact["reason"])

    def test_named_nonexistent_destination_is_a_valid_first_install_coordinate(self) -> None:
        """A lexical future destination must not make the pre-write safety check impossible."""
        candidate = self.directory("candidate")

        artifact = self.observe(self.base / "missing-destination", str(candidate))

        self.assertEqual("affirmative_none", artifact["outcome"])
        self.assertEqual([str(candidate.resolve())], artifact["enumerated_roots"])
        self.assertEqual([], artifact["found"])
        self.assertEqual("Every PATH entry was checked; the installed floati answers first.", artifact["reason"])

    def test_scan_omitting_the_destination_scripts_dir_is_unknown_not_a_shadow(self) -> None:
        """Characterizes the documented scan-input condition: the authoritative entry
        must be SEEN to prove nothing shadows it, so a PATH without it is unknown."""
        destination, _destination_slip = self.bundle("destination")
        elsewhere = self.directory("elsewhere")

        artifact = self.observe(destination, str(elsewhere))

        self.assertEqual("unknown", artifact["outcome"])
        self.assertEqual(str((destination / "scripts").resolve()), artifact["blocked_entry"])
        self.assertEqual([], artifact["found"])
        self.assertEqual(
            "Some PATH entries could not be read; shadow state unknown.",
            artifact["reason"],
        )

    def test_partial_scans_are_never_promoted_to_affirmative_none(self) -> None:
        """Characterizes the law the operator note must state: an incomplete scan
        never becomes an affirmative all-clear."""
        destination, _destination_slip = self.bundle("destination")
        unreadable = self.base / "absent"

        for path in (
            str(unreadable),
            os.pathsep.join((str(unreadable), str(destination / "scripts"))),
        ):
            with self.subTest(path=path):
                self.assertEqual("unknown", self.observe(destination, path)["outcome"])

    def test_explicit_source_script_is_never_reported_as_a_shadow(self) -> None:
        """Counting build material as a predecessor would violate the Item 7 command-root ruling."""
        source, source_script = self.bundle("source", b"source\n")
        source_scripts = source / "scripts"
        destination, _destination_slip = self.bundle("destination")
        command_root = destination / "scripts"

        artifact = self.observe(
            destination,
            os.pathsep.join((str(source_scripts), str(command_root))),
            source_script=source_script,
        )

        self.assertEqual("affirmative_none", artifact["outcome"])
        self.assertEqual([], artifact["found"])
        self.assertEqual("Every PATH entry was checked; the installed floati answers first.", artifact["reason"])

    def test_loaded_source_scripts_slip_is_never_inferred_as_a_shadow(self) -> None:
        """The build-tree launcher must not become a predecessor merely because tests invoke it from PATH."""
        source_scripts = Path(__file__).resolve().parents[1] / "scripts"
        self.assertTrue((source_scripts / "floati").is_file())
        destination, _destination_slip = self.bundle("destination")

        artifact = self.observe(
            destination,
            os.pathsep.join((str(source_scripts), str(destination / "scripts"))),
        )

        self.assertEqual("affirmative_none", artifact["outcome"])
        self.assertEqual([], artifact["found"])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlinked_destination_or_derived_command_path_is_unknown(self) -> None:
        """A named but unsafe coordinate is a failed check, not an absent coordinate."""
        target, _target_slip = self.bundle("target")
        target_command_root = target / "scripts"
        destination_alias = self.base / "destination-alias"
        os.symlink(target, destination_alias)

        alias_artifact = self.observe(destination_alias, str(target_command_root))

        self.assertEqual("unknown", alias_artifact["outcome"])
        self.assertEqual([], alias_artifact["enumerated_roots"])
        self.assertEqual(str(destination_alias), alias_artifact["blocked_entry"])

        bundle = self.directory("bundle")
        command_target = self.directory("command-target")
        self.write_slip(command_target, b"authoritative\n")
        os.symlink(command_target, bundle / "scripts")
        command_artifact = self.observe(bundle, str(command_target))

        self.assertEqual("unknown", command_artifact["outcome"])
        self.assertEqual([], command_artifact["enumerated_roots"])
        self.assertEqual(str(bundle / "scripts"), command_artifact["blocked_entry"])

    def test_status_and_doctor_expose_the_same_v1_shadow_observation(self) -> None:
        """Divergent presenter wrappers could turn one PATH scan into contradictory observations."""
        from floati.doctor import Doctor
        from floati.root import FloatiRoot
        from tests.schema_validation import validate_json_schema

        home = self.base / "fleet"
        FloatiRoot.open_direct_home(home, create=True)
        destination, _destination_slip = self.bundle("destination")
        command_root = destination / "scripts"
        shadow = self.directory("shadow")
        shadow_slip = self.write_slip(shadow, b"shadow\n")
        environment = {
            **os.environ,
            "PATH": os.pathsep.join((str(shadow), str(command_root))),
            "FLOATI_INSTALL_DESTINATION": str(destination),
        }

        status = subprocess.run(
            [sys.executable, "-m", "floati", "status", "--root", str(home), "--json"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, status.returncode, status.stderr)
        status_artifact = json.loads(status.stdout)
        expected = {
            "outcome": "found",
            "enumerated_roots": [str(shadow.resolve()), str(command_root.resolve())],
            "found": [{
                "path": str(shadow_slip.resolve()),
                "digest": hashlib.sha256(b"shadow\n").hexdigest(),
            }],
            "reason": "A floati ahead of the installed copy answered first on PATH.",
        }
        self.assertEqual(1, status_artifact["evidence"]["status_schema_version"])
        self.assertEqual(expected, status_artifact["evidence"]["installer_shadow"])
        validate_json_schema(
            status_artifact,
            REPOSITORY_ROOT / "schemas/v1/fleet-status-artifact.schema.json",
        )

        with patch.dict(os.environ, environment, clear=False):
            doctor_artifact, _return_code = Doctor(
                REPOSITORY_ROOT,
                home,
                destination=destination,
            ).artifact()
        doctor_finding = next(
            row for row in doctor_artifact["findings"] if row["code"] == "installer_shadow"
        )
        self.assertEqual(1, doctor_artifact["schema_version"])
        self.assertEqual(expected, doctor_finding["installer_shadow"])
        self.assertEqual(expected["reason"], doctor_finding["detail"])
        validate_json_schema(
            doctor_artifact,
            REPOSITORY_ROOT / "schemas/v1/doctor-artifact.schema.json",
        )

    def test_watch_v1_places_the_shadow_observation_in_its_snapshot(self) -> None:
        """Putting the leg beside a delta instead of in its snapshot would give watch a divergent read model."""
        from floati.root import FloatiRoot
        from tests.schema_validation import validate_json_schema

        home = self.base / "fleet"
        FloatiRoot.open_direct_home(home, create=True)
        destination, _destination_slip = self.bundle("destination")
        command_root = destination / "scripts"
        shadow = self.directory("shadow")
        shadow_slip = self.write_slip(shadow, b"shadow\n")
        environment = {
            **os.environ,
            "PATH": os.pathsep.join((str(shadow), str(command_root))),
            "FLOATI_INSTALL_DESTINATION": str(destination),
        }

        watch = subprocess.run(
            [
                sys.executable, "-m", "floati", "watch", "--root", str(home),
                "--interval", "0.05", "--iterations", "1",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, watch.returncode, watch.stderr)
        artifact = json.loads(watch.stdout)
        expected = {
            "outcome": "found",
            "enumerated_roots": [str(shadow.resolve()), str(command_root.resolve())],
            "found": [{
                "path": str(shadow_slip.resolve()),
                "digest": hashlib.sha256(b"shadow\n").hexdigest(),
            }],
            "reason": "A floati ahead of the installed copy answered first on PATH.",
        }
        self.assertEqual(1, artifact["schema_version"])
        self.assertEqual(expected, artifact["evidence"]["delta"]["snapshot"]["installer_shadow"])
        validate_json_schema(artifact, REPOSITORY_ROOT / "schemas/v1/watch-artifact.schema.json")


if __name__ == "__main__":
    unittest.main()
