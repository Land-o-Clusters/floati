"""BL-11: the Stop-hook trust guard and the stable Stop-hook launcher.

The ruling (docs/rulings/2026-09-13-boardbus-is-a-turnover-not-an-onboarding.md)
rules two properties: (a) `/boardbus` must stop BEFORE `board` with the ruled
banner and a typed `stop_hook_untrusted` refusal while the Codex Stop hook is
not armed, and (b) the registered Stop-hook command must be a stable launcher
path that resolves the current bundle internally, so a bundle update never
changes the hook's bytes nor invalidates Codex trust.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati import fixture_ids as public_ids

import json as _json

from floati.codex_hook_trust import observe_codex_waiter_hooks
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.waiter_bundle import waiter_runtime_digest, waiter_runtime_files
from tests.temp_roots import REAL_TEMP_ROOT
from tests.private_artifacts import require_private_artifact
from tests.test_codex_hook_install import expected_stop_block, operator_pair

import importlib.util

# EXCL-1: this test file ships in the public export, so it may not import the
# private vendor helper; the loader below is the whole seam it needs.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_VENDORED = _REPOSITORY_ROOT / "tools" / "codex" / "codex-fleet-bus.py"


def load_gateway():
    if not _VENDORED.is_file():
        raise AssertionError("VD-1 gateway module is missing")
    spec = importlib.util.spec_from_file_location("codex_fleet_bus", _VENDORED)
    if spec is None or spec.loader is None:
        raise AssertionError("VD-1 gateway module is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

BANNER_POSITION = 3
BANNER = (
    "**Codex has not trusted Puddle's Stop hook.** Codex → Settings → Hooks → "
    "trust `floati-codex-wait` (Stop hook 3), relaunch this thread, run "
    "`/boardbus` again."
)


def fixed_transport_interpreter() -> Path:
    """The host's real interpreter, reached without any symlink component.

    The gateway refuses a transport interpreter whose path contains a symlink
    (codex-fleet-bus.py ``_assert_no_symlink_components``), and
    ``/usr/bin/python3`` is a real file on some hosts and a symlink on others,
    so the fixture asserts the precondition by name instead of assuming it.
    """
    literal = Path("/usr/bin/python3")
    resolved_running = Path(sys.executable).resolve(strict=True)
    for candidate in (literal, resolved_running):
        walked = Path(candidate.anchor)
        clean = True
        for component in candidate.parts[1:]:
            walked = walked / component
            if walked.is_symlink():
                clean = False
                break
        if clean and walked.is_file():
            return candidate
    raise AssertionError(
        "fixture precondition failed: no symlink-free interpreter for the "
        "transport fixture on this host (tried "
        f"{literal} and {resolved_running}); the gateway refuses a transport "
        "interpreter whose path contains a symlink"
    )


def floati_waiter_block(bus_root: Path, launcher: Path) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": (
                    f"/usr/bin/python3 {launcher} wait --for fresh-work "
                    f"--root {bus_root}"
                ),
                "timeout": 1800,
                "statusMessage": "Watching Floati bus",
            }
        ]
    }


class StopHookBoardGuardTests(unittest.TestCase):
    """Property (a): the gateway's board operation stops before seat board."""

    def setUp(self) -> None:
        require_private_artifact(self, "tools/codex/codex-fleet-bus.py")
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.codex = self.base / ".codex"
        (self.codex / "floati-wake").mkdir(parents=True)
        self.bus_root = self.base / "bus"
        self.bus_root.mkdir()
        # The box's measured shape: three foreign Stop groups, the Floati
        # waiter fourth (group index 3), and no Codex trust state at all.
        foreign = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"'{self.base}/bridge' --source codex",
                        "timeout": 5,
                    }
                ]
            },
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"/usr/bin/python3 {self.base}/wait.py",
                        "timeout": 600,
                    }
                ]
            },
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"/usr/bin/python3 {self.base}/other_bus_wait.py",
                        "timeout": 1800,
                    }
                ]
            },
        ]
        waiter = floati_waiter_block(
            self.bus_root, self.codex / "floati-wake" / ("a" * 64) / "scripts" / "floati-codex-wait"
        )
        launcher_file = (
            self.codex / "floati-wake" / ("a" * 64) / "scripts" / "floati-codex-wait"
        )
        launcher_file.parent.mkdir(parents=True)
        launcher_file.write_text("#!/usr/bin/python3\n", encoding="utf-8")
        self.hooks_path = self.codex / "hooks.json"
        self.hooks_path.write_text(
            _json.dumps({"hooks": {"Stop": foreign + [waiter]}}, indent=2) + "\n",
            encoding="utf-8",
        )
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.gateway = load_gateway()

    def test_board_proceeds_when_the_stop_hook_is_trusted(self) -> None:
        # Green control: an armed Stop hook must not be blocked by the guard.
        rows = observe_codex_waiter_hooks(self.hooks_path)
        key = rows[0]["hook_trust_key"]
        state_key = json.dumps(key, ensure_ascii=False)
        (self.codex / "config.toml").write_text(
            f"[hooks.state.{state_key}]\nenabled = true\n"
            f'trusted_hash = "sha256:{rows[0]["hook_trust_current_hash"]}"\n',
            encoding="utf-8",
        )

        profile = {
            "transport": "fixture",
            "identity": "worker",
            "workspace_root": self.workspace,
            "git_common_dir": self.workspace / ".git",
            "bus_root": self.bus_root,
            "allowed_operations": ["attach", "board"],
        }
        transport = {
            "source_sha": "0" * 40,
            "install_root": self.base / "installed",
            "interpreter": fixed_transport_interpreter(),
        }

        def run_client(interpreter, private_root, module, arguments):
            self.assertEqual("floati", module)
            self.assertEqual(("seat", "board"), tuple(arguments[:2]))
            return 0

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(self.gateway, "_validate_wrapper_invocation"))
        stack.enter_context(mock.patch.object(self.gateway, "_has_git_redirection", return_value=False))
        stack.enter_context(mock.patch.object(self.gateway, "load_registry", return_value=({"fixture": transport}, {"fixture": profile})))
        stack.enter_context(mock.patch.object(self.gateway, "repository_identity", return_value=(self.workspace, self.workspace / ".git", self.workspace / ".git")))
        stack.enter_context(mock.patch.object(self.gateway, "_is_primary_or_linked", return_value=True))
        stack.enter_context(mock.patch.object(self.gateway, "_validate_profile_workspace", return_value=self.workspace))
        stack.enter_context(mock.patch.object(self.gateway, "_reader_currency_observation", return_value=None))
        stack.enter_context(mock.patch.object(self.gateway, "_host_path", side_effect=self.host_path))
        stack.enter_context(mock.patch.object(self.gateway, "_manifest_entries", return_value=[]))
        repo = Path(__file__).resolve().parents[1]

        def stage_real_reader(entries, root):
            shutil.copytree(repo / "floati", Path(root) / "floati", dirs_exist_ok=True)

        stack.enter_context(mock.patch.object(self.gateway, "_stage", side_effect=stage_real_reader))
        run_client_mock = stack.enter_context(mock.patch.object(self.gateway, "_run_client", side_effect=run_client))
        output = io.StringIO()
        with contextlib.ExitStack() as nested:
            nested.enter_context(mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "019f7000-0000-7000-8000-00000000000a"}, clear=False))
            nested.enter_context(contextlib.redirect_stdout(output))
            status = self.gateway.main(["gateway", "fixture", "board"])
        self.assertEqual(0, status)
        self.assertNotIn("stop_hook_untrusted", output.getvalue())
        run_client_mock.assert_called_once()

    def host_path(self, relative: str) -> Path:
        mapping = {
            "hooks.json": self.hooks_path,
            "floati-wake": self.codex / "floati-wake",
            "fleet-bus-profiles.json": self.base / "fleet-bus-profiles.json",
        }
        return mapping[relative]

    def test_board_stops_before_seat_board_when_stop_hook_is_untrusted(self) -> None:
        profile = {
            "transport": "fixture",
            "identity": "worker",
            "workspace_root": self.workspace,
            "git_common_dir": self.workspace / ".git",
            "bus_root": self.bus_root,
            "allowed_operations": ["attach", "board"],
        }
        transport = {
            "source_sha": "0" * 40,
            "install_root": self.base / "installed",
            "interpreter": fixed_transport_interpreter(),
        }
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(self.gateway, "_validate_wrapper_invocation")
        )
        stack.enter_context(
            mock.patch.object(
                self.gateway, "_has_git_redirection", return_value=False
            )
        )
        stack.enter_context(
            mock.patch.object(
                self.gateway,
                "load_registry",
                return_value=({"fixture": transport}, {"fixture": profile}),
            )
        )
        stack.enter_context(
            mock.patch.object(
                self.gateway,
                "repository_identity",
                return_value=(
                    self.workspace,
                    self.workspace / ".git",
                    self.workspace / ".git",
                ),
            )
        )
        stack.enter_context(
            mock.patch.object(
                self.gateway, "_is_primary_or_linked", return_value=True
            )
        )
        stack.enter_context(
            mock.patch.object(
                self.gateway,
                "_validate_profile_workspace",
                return_value=self.workspace,
            )
        )
        stack.enter_context(
            mock.patch.object(
                self.gateway, "_reader_currency_observation", return_value=None
            )
        )
        stack.enter_context(
            mock.patch.object(
                self.gateway,
                "_manifest_entries",
                return_value=[],
            )
        )
        repo = Path(__file__).resolve().parents[1]

        def stage_real_reader(entries, root):
            shutil.copytree(repo / "floati", Path(root) / "floati", dirs_exist_ok=True)

        stack.enter_context(
            mock.patch.object(self.gateway, "_stage", side_effect=stage_real_reader)
        )
        stack.enter_context(
            mock.patch.object(self.gateway, "_host_path", side_effect=self.host_path)
        )
        run_client = stack.enter_context(
            mock.patch.object(
                self.gateway,
                "_run_client",
                side_effect=AssertionError("seat board ran past the guard"),
            )
        )
        environment = dict(os.environ, CODEX_THREAD_ID="019f7000-0000-7000-8000-00000000000a")
        output = io.StringIO()
        with contextlib.ExitStack() as nested:
            nested.enter_context(mock.patch.dict(os.environ, environment, clear=False))
            nested.enter_context(contextlib.redirect_stdout(output))
            status = self.gateway.main(["gateway", "fixture", "board"])
        self.assertEqual(20, status)
        artifact = json.loads(output.getvalue())
        self.assertEqual("refused", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual("stop_hook_untrusted", evidence["code"])
        self.assertEqual(BANNER, evidence["banner"])
        self.assertEqual(
            str(self.hooks_path) + ":stop:3:0", evidence["hook_trust_key"]
        )
        run_client.assert_not_called()


class StableStopLauncherTests(unittest.TestCase):
    """Property (b): one install, one update — hook bytes and trust stand still."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = Path(__file__).resolve().parents[1]
        self.bus_home = self.base / "demo-fleet"
        root = FloatiRoot.open_direct_home(self.bus_home, create=True)
        Registry(root).register(public_ids.builder("floati"), "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.codex = self.base / ".codex"
        self.hooks_path = self.codex / "hooks.json"
        self.hooks_path.parent.mkdir()
        self.hooks_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "'/opt/example/bin/bridge' --source codex",
                                        "timeout": 5,
                                    }
                                ]
                            }
                        ]
                    }
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.destination = self.codex / "floati-wake"

    def variant_source(self, marker: bytes) -> Path:
        source = self.base / f"source-{marker.hex()}"
        if source.exists():
            return source
        for path in waiter_runtime_files(self.repo):
            target = source / path.relative_to(self.repo)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        probe = source / "floati" / "codex_wait.py"
        probe.write_bytes(probe.read_bytes() + marker)
        return source

    def installer(self, source: Path):
        from floati.codex_hook_install import CodexHookInstaller

        return CodexHookInstaller(
            source_root=source,
            bus_home=self.bus_home,
            hooks_path=self.hooks_path,
            destination=self.destination,
        )

    def fresh_pair(self, timeout: int) -> str:
        """Operator pair for this fixture's first install: no Floati group yet."""

        return operator_pair(
            None, expected_stop_block(self.destination, self.bus_home, timeout)
        )

    def install(self, source: Path, *, changes_stop: bool) -> dict:
        """Callers name whether this path changes the Stop definition.

        A changing path names the operator pair derived from fixture
        coordinates; an unchanged path names none and must not need one.
        """

        extra = {"acknowledge_stop_hook_change": self.fresh_pair(10)} if changes_stop else {}
        return self.installer(source).install(
            self.workspace,
            public_ids.builder("floati"),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            **extra,
        )

    def grant_trust(self) -> None:
        rows = observe_codex_waiter_hooks(self.hooks_path)
        self.assertEqual(1, len(rows))
        key = rows[0]["hook_trust_key"]
        state_key = json.dumps(key, ensure_ascii=False)
        (self.codex / "config.toml").write_text(
            f"[hooks.state.{state_key}]\nenabled = true\n"
            f'trusted_hash = "sha256:{rows[0]["hook_trust_current_hash"]}"\n',
            encoding="utf-8",
        )

    def test_fresh_install_writes_the_stable_current_launcher(self) -> None:
        receipt = self.install(self.variant_source(b"\n# variant-a\n"), changes_stop=True)

        installed = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        command = installed["hooks"]["Stop"][-1]["hooks"][0]["command"]
        self.assertIn(
            str(self.destination / "current" / "scripts" / "floati-codex-wait"), command,
            "constructed precondition: the registered command must name the"
            " stable current launcher, not one digest-named bundle generation",
        )
        self.assertNotIn(receipt["bundle_digest"], command)

    def test_native_registered_stop_command_is_admitted_by_gateway(self) -> None:
        """R2: product registration and gateway admission must agree without rewriting either."""
        require_private_artifact(self, "tools/codex/codex-fleet-bus.py")
        with mock.patch.dict(os.environ, {"HOME": str(self.base)}):
            receipt = self.installer(self.repo).install(
                self.workspace, public_ids.builder("floati"),
                hook_timeout_seconds=1800, wait_deadline_seconds=1700,
                acknowledge_stop_hook_change=self.fresh_pair(1800),
            )
            before = self.hooks_path.read_bytes()
            self.assertEqual(receipt["bundle_digest"], os.readlink(self.destination / "current"))
            gateway = load_gateway()
            try:
                gateway._require_existing_waiter_trust()
            except gateway.ApprovalRequired as exc:
                self.fail(f"native product/gateway waiter contract drifted: {exc}")
            self.assertEqual(before, self.hooks_path.read_bytes())

    def test_fleet_update_replan_of_a_stable_install_repoints_without_rewriting_bytes(self) -> None:
        from floati.codex_hook_install import (
            commit_waiter_rebind,
            plan_waiter_rebind,
            stage_waiter_runtime,
        )

        self.install(self.variant_source(b"\n# variant-a\n"), changes_stop=True)
        self.grant_trust()
        before = self.hooks_path.read_bytes()
        variant_b = self.variant_source(b"\n# variant-b\n")
        target_digest = waiter_runtime_digest(variant_b)
        stage_waiter_runtime(variant_b, self.destination, target_digest)

        staged = plan_waiter_rebind(self.hooks_path, self.destination, target_digest)
        self.assertEqual(staged["after"], staged["before"])
        self.assertEqual(staged["target_hook_hash"], staged["current_hook_hash"])
        # The apply flow pins the trust coordinate before committing, exactly
        # as fleet_update does for its own rebind steps.
        staged["hook_trust_key"] = observe_codex_waiter_hooks(self.hooks_path)[0][
            "hook_trust_key"
        ]

        commit_waiter_rebind(staged)
        self.assertEqual(self.hooks_path.read_bytes(), before)
        self.assertEqual(
            os.readlink(self.destination / "current"), target_digest
        )
        rows = observe_codex_waiter_hooks(self.hooks_path)
        self.assertEqual("trusted", rows[0]["hook_trust_status"])

    def test_bundle_update_leaves_hooks_bytes_and_trust_stable(self) -> None:
        self.install(self.variant_source(b"\n# variant-a\n"), changes_stop=True)
        self.grant_trust()
        before = self.hooks_path.read_bytes()

        self.install(self.variant_source(b"\n# variant-b\n"), changes_stop=False)

        self.assertEqual(
            before, self.hooks_path.read_bytes(),
            "constructed precondition: a bundle update rewrites the registered"
            " Stop-hook command, so Codex trust is consumed on every update",
        )
        rows = observe_codex_waiter_hooks(self.hooks_path)
        self.assertEqual("trusted", rows[0]["hook_trust_status"])


if __name__ == "__main__":
    unittest.main()
