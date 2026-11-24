"""F2-DISK-WATCH-1: doctor and board report host free space against the floor.

2026-10-01 this host hit 100% disk and macOS purged a framework cache, which
read as a test-class failure. Doctor was silent. This module pins the
observer: os.statvfs free space, a bounded walk of build output under the
fleet trees that actually exist, exit 0/1/2, no deletion.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from floati import fixture_ids as ids
from floati import fixture_ids as public_ids
from floati.codex_wait_contract import write_workspace_map
from floati.copy import DOCTOR_DISK_WATCH_REMEDIATION, DOCTOR_DISK_WATCH_UNMEASURED
from floati.doctor import Doctor
from floati.planes import LivenessPresenceStore
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.git_fixture_cache import cached_tree
from tests.schema_validation import validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_doctor import NOW, _build_doctor_source, root_entries
from tests import test_seat_board


SCHEMA = Path("schemas/v1/doctor-artifact.schema.json")
FLEET_FLOOR = 1 << 30


def _statvfs(free_bytes: int) -> SimpleNamespace:
    return SimpleNamespace(f_bavail=free_bytes, f_frsize=1)


def _plant_pycache(workspace: Path, payload: bytes) -> Path:
    cache = workspace / "__pycache__"
    cache.mkdir()
    artifact = cache / "mod.pyc"
    artifact.write_bytes(payload)
    return cache


class DiskWatchDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        cached_tree(
            self.source,
            ("test_f2_disk_watch.DiskWatchDoctorTests.source",),
            _build_doctor_source,
        )
        self.home = self.base / "alpha"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.node = public_ids.builder("a")
        Registry(self.root).register(self.node, "Codex")
        LivenessPresenceStore(self.root).observe(self.node, 120, NOW)
        self.workspace = self.home / "nodes" / self.node
        self.workspace.mkdir(parents=True)
        write_workspace_map(self.home, self.workspace, self.node)
        self.destination = self.base / "installed"
        scripts = self.destination / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "floati").write_bytes(b"installed\n")
        self._env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.base / "test-home"),
                "PATH": os.pathsep.join((str(scripts), str(self.source / "scripts"), "/usr/bin")),
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self._trust = mock.patch(
            "floati.doctor.project_effect_reconciliation_interpreter_trust",
            return_value={
                "code": "effect_reconciliation_interpreter_trust",
                "severity": "ok",
                "subject": "test-fixture:trusted-interpreter",
                "detail": "Reconciliation can run here.",
                "remediation": None,
            },
        )
        self._trust.start()
        self.addCleanup(self._trust.stop)

    def doctor(self) -> Doctor:
        return Doctor(
            self.source,
            self.home,
            ref="origin/lane/hm0",
            destination=self.destination,
            no_sandbox=True,
        )

    def _row(self, artifact: dict) -> dict:
        rows = [row for row in artifact["findings"] if row["code"] == "host_disk_watch"]
        self.assertEqual(1, len(rows), artifact["findings"])
        return rows[0]

    def test_under_floor_with_insufficient_build_output_is_exit_one(self) -> None:
        """Measured free space below the floor exits 1 even when build output is short."""

        _plant_pycache(self.workspace, b"\0" * 4096)
        before = root_entries(self.home)
        with mock.patch("os.statvfs", return_value=_statvfs(4096)):
            artifact, rc = self.doctor().artifact()
        self.assertEqual(before, root_entries(self.home))
        row = self._row(artifact)
        self.assertEqual(1, rc)
        self.assertEqual("degraded", artifact["state"])
        self.assertEqual("warning", row["severity"])
        self.assertEqual(1, row["disk_watch"]["exit"])
        self.assertEqual(FLEET_FLOOR, row["disk_watch"]["floor_bytes"])
        self.assertEqual(4096, row["disk_watch"]["free_bytes"])
        self.assertGreaterEqual(row["disk_watch"]["build_bytes"], 4096)
        self.assertIn("__pycache__", row["disk_watch"]["build_names"])
        self.assertEqual(DOCTOR_DISK_WATCH_REMEDIATION, row["remediation"])
        self.assertNotIn("cannot be measured", row["detail"])
        validate_json_schema(artifact, SCHEMA)

    def test_exact_floor_is_healthy(self) -> None:
        """free >= floor is exit 0 at the floor boundary."""

        _plant_pycache(self.workspace, b"\0" * 128)
        with mock.patch("os.statvfs", return_value=_statvfs(FLEET_FLOOR)):
            artifact, rc = self.doctor().artifact()
        self.assertEqual(0, rc)
        window = next(row for row in artifact["findings"] if row["code"] == "host_codex_wait_window")
        self.assertEqual("info", window["severity"])
        self.assertEqual("consent_unavailable", window["wait_window"]["window_state"])
        self.assertIsNone(window["wait_window"]["hook_timeout_seconds"])
        self.assertIsNone(window["wait_window"]["wait_deadline_seconds"])
        row = self._row(artifact)
        self.assertEqual("ok", row["severity"])
        self.assertEqual(0, row["disk_watch"]["exit"])
        self.assertIsNone(row["remediation"])
        self.assertNotIn("below", row["detail"])
        validate_json_schema(artifact, SCHEMA)

    def test_walk_cap_with_known_low_free_is_cannot_measure(self) -> None:
        """A walk cap is exit 2 and never uses below-floor copy."""

        _plant_pycache(self.workspace, b"\0" * 64)

        class _Clock:
            def __init__(self) -> None:
                self.t = 0.0

            def __call__(self) -> float:
                self.t += 1.0
                return self.t

        from floati.doctor_disk_watch import observe_disk_watch as real_observe

        def capped_observe(root, **kwargs):
            kwargs.setdefault("monotonic", _Clock())
            kwargs.setdefault("tree_budget_seconds", 0.5)
            kwargs.setdefault("statvfs", lambda _path: _statvfs(1))
            return real_observe(root, **kwargs)

        with mock.patch(
            "floati.doctor_disk_watch.observe_disk_watch", side_effect=capped_observe
        ):
            artifact, rc = self.doctor().artifact()
        row = self._row(artifact)
        self.assertEqual(2, rc)
        self.assertEqual("error", row["severity"])
        self.assertEqual(2, row["disk_watch"]["exit"])
        self.assertTrue(row["disk_watch"]["partial"])
        self.assertEqual("walk_capped", row["disk_watch"]["reason"])
        self.assertEqual(DOCTOR_DISK_WATCH_UNMEASURED, row["remediation"])
        self.assertNotEqual(DOCTOR_DISK_WATCH_REMEDIATION, row["remediation"])
        self.assertIn("cannot be measured", row["detail"])
        self.assertNotIn("below", row["detail"].lower())
        self.assertNotIn("below", row["remediation"].lower())
        validate_json_schema(artifact, SCHEMA)

    def test_reclaimable_build_output_is_exit_one(self) -> None:
        planted = _plant_pycache(self.workspace, b"\0" * 8000)
        self.assertTrue(planted.is_dir())
        with mock.patch("os.statvfs", return_value=_statvfs(FLEET_FLOOR - 4000)):
            artifact, rc = self.doctor().artifact()
        self.assertEqual(1, rc)
        row = self._row(artifact)
        self.assertEqual("warning", row["severity"])
        self.assertEqual(1, row["disk_watch"]["exit"])
        self.assertGreaterEqual(row["disk_watch"]["build_bytes"], 8000)
        validate_json_schema(artifact, SCHEMA)

    def test_above_the_floor_stays_healthy_and_still_reports_the_census(self) -> None:
        _plant_pycache(self.workspace, b"\0" * 128)
        with mock.patch("os.statvfs", return_value=_statvfs(FLEET_FLOOR + 1)):
            artifact, rc = self.doctor().artifact()
        self.assertEqual(0, rc)
        row = self._row(artifact)
        self.assertEqual("ok", row["severity"])
        self.assertEqual(0, row["disk_watch"]["exit"])
        self.assertIn("__pycache__", row["disk_watch"]["build_names"])
        validate_json_schema(artifact, SCHEMA)

    def test_doctor_registers_disk_watch_from_its_own_module(self) -> None:
        """host_disk_watch is produced by doctor_disk_watch.project_disk_watch, once."""

        planted = {
            "code": "host_disk_watch",
            "severity": "warning",
            "subject": "planted-module",
            "detail": "planted-by-doctor-disk-watch",
            "remediation": None,
            "disk_watch": {"exit": 1},
        }

        def plant(root, findings, rc, **kwargs):
            del root, rc, kwargs
            findings.append(planted)
            return 1

        import floati.doctor as doctor_mod
        import floati.doctor_disk_watch as module

        self.assertIs(doctor_mod.project_disk_watch, module.project_disk_watch)
        with mock.patch.object(doctor_mod, "project_disk_watch", side_effect=plant) as proj:
            artifact, rc = self.doctor().artifact()
        self.assertTrue(proj.called)
        self.assertEqual(1, proj.call_count)
        details = [row.get("detail") for row in artifact["findings"]]
        self.assertIn("planted-by-doctor-disk-watch", details)
        self.assertEqual(1, rc)


class DiskWatchObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        (self.home / "nodes" / "grok").mkdir(parents=True)

    def test_walk_cap_is_a_typed_partial_not_an_estimate(self) -> None:
        from floati.disk_watch import observe_disk_watch

        _plant_pycache(self.home / "nodes" / "grok", b"\0" * 64)

        class _Clock:
            def __init__(self) -> None:
                self.t = 0.0

            def __call__(self) -> float:
                self.t += 1.0
                return self.t

        observation = observe_disk_watch(
            self.root,
            statvfs=lambda _path: _statvfs(1),
            monotonic=_Clock(),
            tree_budget_seconds=0.5,
        )
        self.assertEqual(2, observation["exit"])
        self.assertTrue(observation["partial"])
        self.assertEqual("walk_capped", observation["reason"])
        self.assertTrue(any(tree["partial"] for tree in observation["trees"]))

    def test_statvfs_failure_is_typed_partial(self) -> None:
        from floati.disk_watch import observe_disk_watch

        def boom(_path: str) -> object:
            raise OSError("fixture statvfs")

        observation = observe_disk_watch(self.root, statvfs=boom)
        self.assertEqual(2, observation["exit"])
        self.assertTrue(observation["partial"])
        self.assertEqual("statvfs_unavailable", observation["reason"])
        self.assertIsNone(observation["free_bytes"])

    def test_observer_does_not_delete(self) -> None:
        from floati.disk_watch import observe_disk_watch

        cache = _plant_pycache(self.home / "nodes" / "grok", b"hello")
        before = root_entries(self.home)
        observe_disk_watch(self.root, statvfs=lambda _path: _statvfs(1))
        self.assertEqual(before, root_entries(self.home))
        self.assertTrue(cache.is_dir())

    def test_ownership_home_is_walked_and_stray_bus_child_is_not(self) -> None:
        from floati.disk_watch import observe_disk_watch

        stray = self.home / "nodes" / "grok"
        _plant_pycache(stray, b"\0" * 2048)
        owned = Path(self.temp.name) / "seat-home"
        owned.mkdir()
        _plant_pycache(owned, b"\0" * 4096)
        directory = self.home / "state" / "seat-onboarding"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "owned.json").write_text(
            json.dumps({"home": str(owned)}),
            encoding="utf-8",
        )
        observation = observe_disk_watch(
            self.root, statvfs=lambda _path: _statvfs(FLEET_FLOOR)
        )
        trees = [tree["path"] for tree in observation["trees"]]
        self.assertIn(str(owned.resolve()), trees)
        self.assertNotIn(str(stray.resolve()), trees)
        self.assertGreaterEqual(observation["build_bytes"], 4096)
        self.assertLess(observation["build_bytes"], 4096 + 2048)
        self.assertEqual(0, observation["exit"])

    def test_mapped_workspace_is_walked(self) -> None:
        from floati.disk_watch import observe_disk_watch

        workspace = self.home / "nodes" / "grok"
        _plant_pycache(workspace, b"\0" * 3000)
        write_workspace_map(self.home, workspace, "grok")
        observation = observe_disk_watch(
            self.root, statvfs=lambda _path: _statvfs(FLEET_FLOOR)
        )
        self.assertIn(str(workspace.resolve()), [tree["path"] for tree in observation["trees"]])
        self.assertGreaterEqual(observation["build_bytes"], 3000)
        self.assertEqual(0, observation["exit"])


class DiskWatchBoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        Registry(self.root).register(public_ids.builder("a"), "Codex")
        workspace = self.home / "nodes" / public_ids.builder("a")
        workspace.mkdir(parents=True)
        _plant_pycache(workspace, b"\0" * 4096)

    def test_board_names_the_floor_when_the_host_is_below_it(self) -> None:
        from floati.tui import model_from_root
        from floati.tui_render import DISK_WATCH_ALERT, DISK_WATCH_UNMEASURED_ALERT, render_frame

        with mock.patch("os.statvfs", return_value=_statvfs(4096)):
            model = model_from_root(self.root, now=NOW)
        self.assertEqual(1, model.effects["disk_watch"]["exit"])
        frame = render_frame(model, 100, 30, selected=0, color=False)
        self.assertIn(DISK_WATCH_ALERT, frame)
        self.assertNotIn(DISK_WATCH_UNMEASURED_ALERT, frame)

    def test_board_names_cannot_measure_without_below_floor(self) -> None:
        from floati.tui import model_from_root
        from floati.tui_render import DISK_WATCH_ALERT, DISK_WATCH_UNMEASURED_ALERT, render_frame

        def boom(_path: str) -> object:
            raise OSError("fixture statvfs")

        with mock.patch("os.statvfs", side_effect=boom):
            model = model_from_root(self.root, now=NOW)
        self.assertEqual(2, model.effects["disk_watch"]["exit"])
        frame = render_frame(model, 100, 30, selected=0, color=False)
        self.assertIn(DISK_WATCH_UNMEASURED_ALERT, frame)
        self.assertNotIn(DISK_WATCH_ALERT, frame)

    def test_board_stays_quiet_above_the_floor(self) -> None:
        from floati.tui import model_from_root
        from floati.tui_render import DISK_WATCH_ALERT, DISK_WATCH_UNMEASURED_ALERT, render_frame

        with mock.patch("os.statvfs", return_value=_statvfs(FLEET_FLOOR + 8)):
            model = model_from_root(self.root, now=NOW)
        self.assertNotIn("disk_watch", model.effects)
        frame = render_frame(model, 100, 30, selected=0, color=False)
        self.assertNotIn(DISK_WATCH_ALERT, frame)
        self.assertNotIn(DISK_WATCH_UNMEASURED_ALERT, frame)


class DiskWatchSeatBoardAndWakeTests(unittest.TestCase):
    """Am.7: seat board and the wake path name the floor. TUI board is not enough."""

    write_map = test_seat_board.SeatBoardTests.write_map
    send = test_seat_board.SeatBoardTests.send
    arm_session = test_seat_board.SeatBoardTests.arm_session
    board = test_seat_board.SeatBoardTests.board

    def setUp(self) -> None:
        test_seat_board.SeatBoardTests.setUp(self)

    def test_measured_free_space_survives_capped_build_walks_in_board_summary(self) -> None:
        """The owner's receipt measured free space, even with an incomplete build walk."""
        from floati.doctor_disk_watch import attach_disk_watch_report
        from floati.tui import model_from_root
        from floati.tui_render import (
            DISK_WATCH_CAPPED_TREES, DISK_WATCH_ONE_CAPPED_TREE,
            DISK_WATCH_PARTIAL_ALERT, render_frame, render_plain_dump,
        )

        # The third value is this host's measured total capacity, an upper
        # bound on its real free-space figure (diskutil info /, 2026-10-25).
        for free_bytes, free_figure, capped_count in (
            (308_932_784_128, "287.7 GiB", 1),
            (308_932_784_128, "287.7 GiB", 3),
            (994_610_155_520, "926.3 GiB", 3),
        ):
            with self.subTest(free_bytes=free_bytes, capped_trees=capped_count):
                observation = {
                    "exit": 2,
                    "floor_bytes": FLEET_FLOOR,
                    "free_bytes": free_bytes,
                    "build_bytes": 4096,
                    "partial": True,
                    "reason": "walk_capped",
                    "trees": [
                        {"path": str(self.root.path / str(index)), "build_bytes": 1024,
                         "partial": True, "reason": "walk_capped"}
                        for index in range(capped_count)
                    ] + [{"path": str(self.root.path), "build_bytes": 1024,
                          "partial": False, "reason": None}],
                    "build_names": ["__pycache__"],
                }
                with mock.patch("floati.doctor_disk_watch.observe_disk_watch", return_value=observation):
                    report = attach_disk_watch_report(self.root, {})
                detail = report["disk_watch"]
                line = report["disk_watch_line"]
                self.assertEqual(free_bytes, detail["free_bytes"])
                self.assertIn(free_figure, line)
                suffix = (DISK_WATCH_ONE_CAPPED_TREE if capped_count == 1
                          else DISK_WATCH_CAPPED_TREES.format(count=capped_count))
                self.assertEqual(DISK_WATCH_PARTIAL_ALERT.format(free=free_figure) + suffix, line)
                with mock.patch("floati.disk_watch.observe_disk_watch", return_value=detail):
                    model = model_from_root(self.root, now=NOW)
                for frame in (render_plain_dump(model, width=120),
                              render_frame(model, 120, 40, selected=0, color=False)):
                    self.assertIn(line, frame)
                for frame in (render_plain_dump(model, width=80),
                              render_frame(model, 80, 40, selected=0, color=False)):
                    self.assertIn(free_figure, frame)

    def test_partial_free_space_uses_binary_units_without_rounding_up(self) -> None:
        """Wrong unit selection, raw bytes or upward rounding loses measured precision."""
        from floati.doctor_disk_watch import disk_watch_line

        for free_bytes, expected in (
            (0, "0.0 MiB"),
            (512 * 1024 ** 2, "512.0 MiB"),
            (1024 ** 3 - 1, "1023.9 MiB"),
            (1024 ** 3, "1.0 GiB"),
            (2 * 1024 ** 3 - 1, "1.9 GiB"),
            (308_932_784_128, "287.7 GiB"),
        ):
            with self.subTest(free_bytes=free_bytes):
                line = disk_watch_line({"exit": 2, "free_bytes": free_bytes, "trees": []})
                self.assertIn(expected, line)

    def test_complete_and_unmeasured_board_lines_keep_their_exact_bytes(self) -> None:
        from floati.doctor_disk_watch import disk_watch_line

        for exit_code, free_bytes, expected in (
            (0, FLEET_FLOOR, None),
            (1, 4096, "! HOST DISK BELOW FLEET FLOOR"),
            (2, None, "! HOST DISK CANNOT BE MEASURED"),
        ):
            with self.subTest(exit=exit_code):
                observation = {"exit": exit_code, "free_bytes": free_bytes,
                               "partial": free_bytes is None, "trees": []}
                self.assertEqual(expected, disk_watch_line(observation))

    def test_seat_board_report_names_the_floor_when_below(self) -> None:
        from floati.tui_render import DISK_WATCH_ALERT

        with mock.patch("os.statvfs", return_value=_statvfs(4096)):
            result = self.board()
        self.assertIn("disk_watch", result)
        self.assertEqual(1, result["disk_watch"]["exit"])
        self.assertEqual(DISK_WATCH_ALERT, result["disk_watch_line"])

    def test_wake_status_report_names_the_floor_when_below(self) -> None:
        import contextlib
        import io

        from floati.cli import main
        from floati.tui_render import DISK_WATCH_ALERT

        output = io.StringIO()
        with mock.patch("os.statvfs", return_value=_statvfs(4096)):
            with contextlib.redirect_stdout(output):
                rc = main(
                    [
                        "wake",
                        "status",
                        "--root",
                        str(self.root.path),
                        "--as",
                        ids.builder("floati"),
                        "--session",
                        "new-session",
                    ]
                )
        artifact = json.loads(output.getvalue())
        self.assertEqual(0, rc, artifact)
        evidence = artifact["evidence"]
        self.assertIn("disk_watch", evidence)
        self.assertEqual(1, evidence["disk_watch"]["exit"])
        self.assertEqual(DISK_WATCH_ALERT, evidence["disk_watch_line"])


if __name__ == "__main__":
    unittest.main()
