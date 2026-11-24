"""INSTALL-TRUST-PREFLIGHT-1 R3: only the operator consents to a Stop change.

(a) install/update that would change the Stop-hook definition refuses BEFORE
writing, names the Codex disarm consequence, and proceeds only on the exact
operator pair `<preimage>:<target>`. Replay of any other pair refuses. An
unchanged definition does not prompt. (b) R5.1: gateway attach is run by the
seat, so it accepts no acknowledgement of any kind, forwards none, and
refuses a change (or anything it cannot decide) before staging, naming the
operator's native install command. (c) G2 runs the preflight before saga
receipts and generation staging. (d) an unrecognised installed Stop hook is
not the same subject as no Floati hook installed.
"""

from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import importlib.util

from floati.errors import ProtocolRefusal
from floati.fleet_update_receipts import FleetUpdateReceiptLedger
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.private_artifacts import require_private_artifact
from tests.test_codex_hook_install import expected_stop_block, operator_pair
from tests.test_fu1_g2 import FU1G2Tests, canonical_executable


CONSEQUENCE = (
    "Codex disarms until Settings → Hooks trust, relaunch every seat, then /boardbus"
)

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


class InstallTrustPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = Path(__file__).resolve().parents[1]
        self.bus_home = self.base / "demo-fleet"
        root = FloatiRoot.open_direct_home(self.bus_home, create=True)
        Registry(root).register(public_ids.builder("floati"), "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.hooks_path = self.base / ".codex" / "hooks.json"
        self.hooks_path.parent.mkdir()
        self.hooks_path.write_text(
            json.dumps({"hooks": {"Stop": []}}) + "\n",
            encoding="utf-8",
        )
        self.destination = self.base / ".codex" / "floati-wake"

    def installer(self):
        from floati.codex_hook_install import CodexHookInstaller

        return CodexHookInstaller(
            source_root=self.source,
            bus_home=self.bus_home,
            hooks_path=self.hooks_path,
            destination=self.destination,
        )

    def _planned(self):
        return self.installer().plan_stop_definition(hook_timeout_seconds=10)

    def _install(self, **kwargs):
        return self.installer().install(
            self.workspace,
            public_ids.builder("floati"),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            **kwargs,
        )

    def _acked(self, planned=None, **kwargs):
        planned = self._planned() if planned is None else planned
        return self._install(
            acknowledge_stop_hook_change=f"{planned['preimage']}:{planned['target']}",
            **kwargs,
        )

    def test_unacknowledged_install_refuses_before_writing_hooks(self) -> None:
        from floati.codex_hook_install import CodexHookInstaller

        before = self.hooks_path.read_bytes()
        parameter = inspect.signature(CodexHookInstaller.install).parameters[
            "acknowledge_stop_hook_change"
        ]
        self.assertEqual(parameter.default, None)

        with self.assertRaises(ProtocolRefusal) as caught:
            self._install()

        self.assertEqual("stop_hook_definition_change_unacknowledged", caught.exception.code)
        self.assertIn(str(self.hooks_path), caught.exception.detail)
        self.assertIn(CONSEQUENCE, caught.exception.detail)
        self.assertIn("--acknowledge-stop-hook-change", str(caught.exception.remedy))
        self.assertNotIn("--acknowledge-preimage", str(caught.exception.remedy))
        self.assertNotIn("--acknowledge-target", str(caught.exception.remedy))
        self.assertEqual(before, self.hooks_path.read_bytes())
        self.assertFalse(self.destination.exists())

    def test_boolean_acknowledgement_is_not_consent(self) -> None:
        before = self.hooks_path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            self._install(acknowledge_stop_hook_change=True)
        self.assertEqual("stop_hook_definition_change_unacknowledged", caught.exception.code)
        self.assertEqual(before, self.hooks_path.read_bytes())
        self.assertFalse(self.destination.exists())

    def test_environment_flag_does_not_synthesize_acknowledgement(self) -> None:
        before = self.hooks_path.read_bytes()
        with mock.patch.dict(
            os.environ,
            {
                "FLOATI_ACKNOWLEDGE_STOP_HOOK_CHANGE": "1",
                "ACKNOWLEDGE_STOP_HOOK_CHANGE": "true",
            },
        ):
            with self.assertRaises(ProtocolRefusal) as caught:
                self._install()
        self.assertEqual("stop_hook_definition_change_unacknowledged", caught.exception.code)
        self.assertEqual(before, self.hooks_path.read_bytes())

    def test_acknowledged_install_writes_the_stop_hook(self) -> None:
        from floati.codex_hook_install import EMPTY_STOP_PREIMAGE

        planned = self._planned()
        self.assertEqual(EMPTY_STOP_PREIMAGE, planned["preimage"])
        self.assertEqual(hashlib.sha256(b"").hexdigest(), planned["preimage"])
        self.assertNotEqual(planned["preimage"], planned["target"])
        receipt = self._acked(planned)

        self.assertEqual("installed", receipt["state"])
        installed = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(1, len(installed["hooks"]["Stop"]))
        self.assertIn("floati-codex-wait", installed["hooks"]["Stop"][0]["hooks"][0]["command"])

    def test_stale_or_wrong_pair_refuses_before_writing(self) -> None:
        planned = self._planned()
        before = self.hooks_path.read_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            self._install(
                acknowledge_stop_hook_change=f"{'0' * 64}:{planned['target']}",
            )
        self.assertEqual("stop_hook_definition_change_unacknowledged", caught.exception.code)
        self.assertEqual(before, self.hooks_path.read_bytes())

        with self.assertRaises(ProtocolRefusal) as caught:
            self._install(
                acknowledge_stop_hook_change=f"{planned['preimage']}:{'1' * 64}",
            )
        self.assertEqual("stop_hook_definition_change_unacknowledged", caught.exception.code)
        self.assertEqual(before, self.hooks_path.read_bytes())

    def test_idempotent_install_does_not_require_acknowledgement(self) -> None:
        self._acked()
        before = self.hooks_path.read_bytes()
        planned = self._planned()
        self.assertEqual(planned["preimage"], planned["target"])

        receipt = self._install()

        self.assertEqual("already_installed", receipt["state"])
        self.assertEqual(before, self.hooks_path.read_bytes())

    def test_replayed_acknowledgement_refuses_after_target_changes(self) -> None:
        first = self._planned()
        self._acked(first)
        document = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        document["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 9
        self.hooks_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        before = self.hooks_path.read_bytes()
        second = self._planned()
        self.assertNotEqual(first["preimage"], second["preimage"])
        self.assertNotEqual(first["target"], second["preimage"])

        with self.assertRaises(ProtocolRefusal) as caught:
            self._install(
                acknowledge_stop_hook_change=f"{first['preimage']}:{first['target']}",
            )

        self.assertEqual("stop_hook_definition_change_unacknowledged", caught.exception.code)
        self.assertEqual(before, self.hooks_path.read_bytes())

        receipt = self._acked(second)
        self.assertEqual("replaced", receipt["state"])

    def test_unacknowledged_replace_refuses_before_rewriting_hooks(self) -> None:
        self._acked()
        document = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        document["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 9
        self.hooks_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        before = self.hooks_path.read_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            self._install()

        self.assertEqual("stop_hook_definition_change_unacknowledged", caught.exception.code)
        self.assertIn(CONSEQUENCE, caught.exception.detail)
        self.assertEqual(before, self.hooks_path.read_bytes())

    def test_native_main_prints_typed_refusal_not_traceback(self) -> None:
        from floati.codex_hook_install import main

        argv = [
            "--source",
            str(self.source),
            "--root",
            str(self.bus_home),
            "--hooks",
            str(self.hooks_path),
            "--destination",
            str(self.destination),
            "--workspace",
            str(self.workspace),
            "--node",
            public_ids.builder("floati"),
            "--hook-timeout-seconds",
            "10",
            "--wait-deadline-seconds",
            "2",
        ]
        buffer = __import__("io").StringIO()
        with mock.patch("sys.stdout", buffer):
            code = main(argv)
        self.assertEqual(20, code)
        artifact = json.loads(buffer.getvalue())
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(
            "stop_hook_definition_change_unacknowledged",
            artifact["evidence"]["code"],
        )
        self.assertIn(CONSEQUENCE, artifact["evidence"]["detail"])
        self.assertIn("--acknowledge-stop-hook-change", artifact["evidence"]["remedy"])
        self.assertFalse(self.destination.exists())

    # R3: the native refusal is the operator's whole instruction. It names both
    # hashes as fields and the exact re-run, not a placeholder.
    def test_native_refusal_names_both_hashes_and_the_exact_rerun(self) -> None:
        import io
        import shlex
        import sys

        from floati.codex_hook_install import main

        argv = [
            "--source", str(self.source),
            "--root", str(self.bus_home),
            "--hooks", str(self.hooks_path),
            "--destination", str(self.destination),
            "--workspace", str(self.workspace),
            "--node", public_ids.builder("floati"),
            "--hook-timeout-seconds", "10",
            "--wait-deadline-seconds", "2",
        ]
        planned_block = expected_stop_block(self.destination, self.bus_home, 10)
        pair = operator_pair(None, planned_block)
        preimage, target = pair.split(":")
        before = self.hooks_path.read_bytes()
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            code = main(argv)
        self.assertEqual(20, code)
        evidence = json.loads(buffer.getvalue())["evidence"]
        self.assertEqual("stop_hook_definition_change_unacknowledged", evidence["code"])
        self.assertEqual(preimage, evidence["preimage"])
        self.assertEqual(target, evidence["target"])
        self.assertEqual(str(self.hooks_path), evidence["hooks_path"])
        self.assertEqual(CONSEQUENCE, evidence["consequence"])
        rerun = " ".join(
            [shlex.quote(sys.executable), "-m", "floati.codex_hook_install"]
            + [shlex.quote(part) for part in argv]
            + ["--acknowledge-stop-hook-change", pair]
        )
        self.assertIn(rerun, evidence["remedy"])
        self.assertIn(CONSEQUENCE, evidence["remedy"])
        self.assertEqual(before, self.hooks_path.read_bytes())
        self.assertFalse(self.destination.exists())

        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            code = main(argv + ["--acknowledge-stop-hook-change", pair])
        self.assertEqual(0, code, buffer.getvalue())
        self.assertEqual("installed", json.loads(buffer.getvalue())["state"])

    def _legacy_rebind_fixture(self):
        """A digest-named Floati Stop group whose rebind CHANGES the definition."""

        store = self.base / "waiters"
        old = store / ("1" * 64) / "scripts" / "floati-codex-wait"
        old.parent.mkdir(parents=True)
        old.write_text("#!/bin/sh\n", encoding="utf-8")
        block = {
            "hooks": [
                {
                    "type": "command",
                    "command": f"/usr/bin/python3 {old} --root {self.bus_home}",
                    "timeout": 900,
                    "statusMessage": "Watching Floati bus",
                }
            ]
        }
        self.hooks_path.write_text(
            json.dumps({"hooks": {"Stop": [block]}}) + "\n", encoding="utf-8"
        )
        digest = "2" * 64
        new = store / digest / "scripts" / "floati-codex-wait"
        target_block = json.loads(json.dumps(block))
        target_block["hooks"][0]["command"] = f"/usr/bin/python3 {new} --root {self.bus_home}"
        return store, digest, block, target_block

    def test_commit_waiter_rebind_refuses_without_a_pair(self) -> None:
        from floati.codex_hook_install import commit_waiter_rebind, plan_waiter_rebind

        store, digest, _block, _target = self._legacy_rebind_fixture()
        staged = plan_waiter_rebind(self.hooks_path, store, digest)
        before = self.hooks_path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            commit_waiter_rebind(staged)
        self.assertEqual("stop_hook_definition_change_unacknowledged", caught.exception.code)
        self.assertEqual(before, self.hooks_path.read_bytes())

    def test_commit_waiter_rebind_refuses_a_pair_for_another_target(self) -> None:
        from floati.codex_hook_install import commit_waiter_rebind, plan_waiter_rebind

        store, digest, block, target_block = self._legacy_rebind_fixture()
        staged = plan_waiter_rebind(self.hooks_path, store, digest)
        other = json.loads(json.dumps(target_block))
        other["hooks"][0]["timeout"] = 901
        before = self.hooks_path.read_bytes()
        for pair in (
            operator_pair(block, other),
            operator_pair(None, target_block),
            operator_pair(target_block, block),
        ):
            with self.assertRaises(ProtocolRefusal) as caught:
                commit_waiter_rebind(staged, acknowledge_stop_hook_change=pair)
            self.assertEqual(
                "stop_hook_definition_change_unacknowledged", caught.exception.code
            )
            self.assertEqual(before, self.hooks_path.read_bytes())


class GatewayWaiterTrustSubjectTests(unittest.TestCase):
    def setUp(self) -> None:
        require_private_artifact(self, "tools/codex/codex-fleet-bus.py")
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.store = self.home / ".codex" / "floati-wake"
        self.digest = "a" * 64
        self.launcher = self.store / self.digest / "scripts" / "floati-codex-wait"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("#!/usr/bin/python3\n", encoding="utf-8")
        self.current = self.store / "current"
        self.current.symlink_to(self.digest)
        self.gateway = load_gateway()
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.hooks_path = self.home / ".codex" / "hooks.json"

    def _write_stop(self, command: str, timeout: int = 1800) -> None:
        document = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "timeout": timeout,
                                "command": command,
                                "statusMessage": "Watching Floati bus",
                            }
                        ]
                    }
                ]
            }
        }
        self.hooks_path.write_text(json.dumps(document) + "\n", encoding="utf-8")

    def test_empty_stop_list_names_no_floati_hook(self) -> None:
        self.hooks_path.write_text('{"hooks":{"Stop":[]}}\n', encoding="utf-8")

        with self.assertRaises(self.gateway.ApprovalRequired) as caught:
            self.gateway._require_existing_waiter_trust()

        self.assertEqual("no Floati hook installed", str(caught.exception))
        self.assertNotIn("first global Floati waiter installation", str(caught.exception))

    def test_foreign_stop_hook_names_no_floati_hook(self) -> None:
        self._write_stop("/usr/bin/python3 /opt/example/bin/existing_wait.py --root \x2ftmp/bus")

        with self.assertRaises(self.gateway.ApprovalRequired) as caught:
            self.gateway._require_existing_waiter_trust()

        self.assertEqual("no Floati hook installed", str(caught.exception))

    def test_unrecognised_floati_shape_names_the_installed_path(self) -> None:
        launcher = self.current / "scripts" / "floati-codex-wait"
        self._write_stop(
            f"/usr/bin/python3 {launcher} wait --for fresh-work extra --root {self.home}/bus"
        )

        with self.assertRaises(self.gateway.ApprovalRequired) as caught:
            self.gateway._require_existing_waiter_trust()

        self.assertEqual(
            f"installed Stop hook shape not recognised: {launcher}",
            str(caught.exception),
        )
        self.assertNotIn("first global Floati waiter installation", str(caught.exception))
        self.assertNotIn("no Floati hook installed", str(caught.exception))

    def test_attach_refuses_any_caller_argument(self) -> None:
        profile = {
            "identity": "builder-a",
            "bus_root": self.home / "fleet",
            "workspace_root": self.home / "workspace",
            "allowed_operations": ["attach"],
        }
        with self.assertRaises(self.gateway.ApprovalRequired) as caught:
            self.gateway._parse_arguments(
                ["gateway", "fixture", "attach", "--acknowledge-stop-hook-change"],
                {"fixture": profile},
            )
        self.assertIn("accepts no caller arguments", str(caught.exception))
        with self.assertRaises(self.gateway.ApprovalRequired) as caught:
            self.gateway._parse_arguments(
                [
                    "gateway",
                    "fixture",
                    "attach",
                    "--acknowledge-stop-hook-change",
                    f"{'a' * 64}:{'b' * 64}",
                ],
                {"fixture": profile},
            )
        self.assertIn("accepts no caller arguments", str(caught.exception))

    def test_attach_hash_change_refuses_before_private_root(self) -> None:
        launcher = self.current / "scripts" / "floati-codex-wait"
        bus = self.home / "bus"
        bus.mkdir()
        self._write_stop(
            f"/usr/bin/python3 {launcher} wait --for fresh-work --root {bus}",
            timeout=9,
        )
        profile = {
            "identity": "builder-a",
            "bus_root": bus,
            "workspace_root": self.home / "workspace",
            "allowed_operations": ["attach"],
            "transport": "fixture",
            "git_common_dir": self.home / "workspace" / ".git",
            "send_repository": "floati",
            "send_recipients": ["builder-a"],
        }
        transport = {
            "source_sha": "0" * 40,
            "install_root": self.home / "install",
            "interpreter": Path("/usr/bin/python3"),
        }
        with mock.patch.object(self.gateway, "_validate_wrapper_invocation"), mock.patch.object(
            self.gateway, "_has_git_redirection", return_value=False
        ), mock.patch.object(
            self.gateway, "load_registry", return_value=(
                {"fixture": transport},
                {"fixture": profile},
            )
        ), mock.patch.object(
            self.gateway,
            "repository_identity",
            return_value=(
                self.home / "workspace",
                self.home / "workspace" / ".git",
                self.home / "workspace" / ".git",
            ),
        ), mock.patch.object(
            self.gateway, "_is_primary_or_linked", return_value=True
        ), mock.patch.object(
            self.gateway, "_validate_profile_workspace", return_value=self.home / "workspace"
        ), mock.patch.object(
            self.gateway, "_reader_currency_observation", return_value=None
        ), mock.patch.object(
            self.gateway, "_fixed_directory"
        ), mock.patch.object(
            self.gateway, "_require_existing_waiter_trust"
        ), mock.patch.object(
            self.gateway, "_private_root", side_effect=AssertionError("staged")
        ) as private, mock.patch(
            "sys.stdout", new_callable=lambda: __import__("io").StringIO()
        ) as stdout:
            code = self.gateway.main(["gateway", "fixture", "attach"])
        self.assertEqual(20, code)
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(
            "stop_hook_definition_change_unacknowledged",
            artifact["evidence"]["code"],
        )
        self.assertIn("--acknowledge-stop-hook-change", artifact["evidence"]["remedy"])
        self.assertIn("-m floati.codex_hook_install", artifact["evidence"]["remedy"])
        private.assert_not_called()

    def test_derived_attach_does_not_forward_acknowledgement(self) -> None:
        profile = {
            "identity": "builder-a",
            "bus_root": self.home / "fleet",
            "workspace_root": self.home / "workspace",
            "allowed_operations": ["attach"],
        }
        module, arguments = self.gateway._derived_invocation(
            "attach", {}, profile, self.home / "staged"
        )
        self.assertEqual("floati.codex_hook_install", module)
        self.assertNotIn("--acknowledge-stop-hook-change", arguments)
        self.assertNotIn("--acknowledge-preimage", arguments)
        self.assertNotIn("--acknowledge-target", arguments)

    # R3 (R5.1): the seat runs attach, so attach carries no consent of any kind.
    def test_attach_refuses_every_acknowledgement_spelling(self) -> None:
        profile = {
            "identity": "builder-a",
            "bus_root": self.home / "fleet",
            "workspace_root": self.home / "workspace",
            "allowed_operations": ["attach"],
        }
        pair = f"{'a' * 64}:{'b' * 64}"
        for extra in (
            ["--acknowledge-preimage", "a" * 64, "--acknowledge-target", "b" * 64],
            ["--acknowledge-stop-hook-change", pair],
            [f"--acknowledge-stop-hook-change={pair}"],
            [pair],
            ["--yes"],
            [""],
        ):
            with self.assertRaises(self.gateway.ApprovalRequired, msg=extra):
                self.gateway._parse_arguments(
                    ["gateway", "fixture", "attach", *extra], {"fixture": profile}
                )

    def test_derived_attach_never_forwards_acknowledgement_values(self) -> None:
        profile = {
            "identity": "builder-a",
            "bus_root": self.home / "fleet",
            "workspace_root": self.home / "workspace",
            "allowed_operations": ["attach"],
        }
        pair = f"{'a' * 64}:{'b' * 64}"
        values = {
            "--acknowledge-stop-hook-change": pair,
            "--acknowledge-preimage": "a" * 64,
            "--acknowledge-target": "b" * 64,
        }
        module, arguments = self.gateway._derived_invocation(
            "attach", values, profile, self.home / "staged"
        )
        self.assertEqual("floati.codex_hook_install", module)
        self.assertEqual([], [part for part in arguments if "acknowledge" in part])
        self.assertNotIn(pair, arguments)
        self.assertNotIn("a" * 64, arguments)

    def _attach_main(self, hooks_document: dict):
        """Drive gateway main for attach with the real refusal and real staging.

        Returns (exit, stdout, private_root spy, temp root, run_client mock).
        """

        import io

        bus = self.home / "bus"
        bus.mkdir(exist_ok=True)
        self.hooks_path.write_text(json.dumps(hooks_document) + "\n", encoding="utf-8")
        profile = {
            "identity": "builder-a",
            "bus_root": bus,
            "workspace_root": self.home / "workspace",
            "allowed_operations": ["attach"],
            "transport": "fixture",
            "git_common_dir": self.home / "workspace" / ".git",
            "send_repository": "floati",
            "send_recipients": ["builder-a"],
        }
        transport = {
            "source_sha": "0" * 40,
            "install_root": self.home / "install",
            "interpreter": Path("/usr/bin/python3"),
        }
        temporary_root = self.home / "private-tmp"
        temporary_root.mkdir(exist_ok=True)
        gateway = self.gateway
        stdout = io.StringIO()
        with mock.patch.object(gateway, "_validate_wrapper_invocation"), mock.patch.object(
            gateway, "_has_git_redirection", return_value=False
        ), mock.patch.object(
            gateway, "load_registry",
            return_value=({"fixture": transport}, {"fixture": profile}),
        ), mock.patch.object(
            gateway, "repository_identity",
            return_value=(
                self.home / "workspace",
                self.home / "workspace" / ".git",
                self.home / "workspace" / ".git",
            ),
        ), mock.patch.object(
            gateway, "_is_primary_or_linked", return_value=True
        ), mock.patch.object(
            gateway, "_validate_profile_workspace", return_value=self.home / "workspace"
        ), mock.patch.object(
            gateway, "_reader_currency_observation", return_value=None
        ), mock.patch.object(gateway, "_fixed_directory"), mock.patch.object(
            gateway, "_require_existing_waiter_trust"
        ), mock.patch.object(
            gateway, "_fixed_file", side_effect=lambda path, _label: path
        ), mock.patch.object(
            gateway, "_manifest_entries", return_value=[]
        ), mock.patch.object(
            gateway, "_canonical_temporary_root", return_value=temporary_root
        ), mock.patch.object(
            gateway, "_temporary_parent_rejects", return_value=None
        ), mock.patch.object(
            gateway, "_private_root", wraps=gateway._private_root
        ) as private, mock.patch.object(
            gateway, "_stage_reader_identity"
        ), mock.patch.object(
            gateway, "_run_client", return_value=0
        ) as run_client, mock.patch("sys.stdout", stdout):
            code = gateway.main(["gateway", "fixture", "attach"])
        return code, stdout.getvalue(), private, temporary_root, run_client, profile, transport

    def _stop_document(self, *groups) -> dict:
        return {"hooks": {"Stop": list(groups)}}

    def _floati_group(self, launcher: Path, bus: Path, timeout: int = 86400) -> dict:
        return {
            "hooks": [
                {
                    "type": "command",
                    "command": f"/usr/bin/python3 {launcher} wait --for fresh-work --root {bus}",
                    "timeout": timeout,
                    "statusMessage": "Watching Floati bus",
                }
            ]
        }

    def test_attach_change_refusal_stages_nothing_and_names_the_operator_command(self) -> None:
        import shlex

        from floati.codex_hook_install import CodexHookInstaller

        bus = self.home / "bus"
        installed = self._floati_group(self.launcher, bus)  # digest launcher: a change
        code, out, private, temporary_root, run_client, profile, transport = self._attach_main(
            self._stop_document(installed)
        )
        self.assertEqual(20, code, out)
        private.assert_not_called()
        run_client.assert_not_called()
        self.assertEqual(
            [], [path.name for path in temporary_root.iterdir() if path.name.startswith("codex-fleet-bus-")]
        )
        evidence = json.loads(out)["evidence"]
        planned = expected_stop_block(self.store, bus, 86400)
        pair = operator_pair(installed, planned)
        preimage, target = pair.split(":")
        self.assertEqual("stop_hook_definition_change_unacknowledged", evidence["code"])
        self.assertEqual(preimage, evidence["preimage"])
        self.assertEqual(target, evidence["target"])
        # The gateway's stdlib mirror and the product planner name one target.
        product = CodexHookInstaller(
            source_root=self.home / "install",
            bus_home=bus,
            hooks_path=self.hooks_path,
            destination=self.store,
        ).plan_stop_definition(hook_timeout_seconds=86400)
        self.assertEqual(product["preimage"], evidence["preimage"])
        self.assertEqual(product["target"], evidence["target"])
        install_root = shlex.quote(str(transport["install_root"]))
        command = " ".join([
            "cd", install_root, "&&", "/usr/bin/python3", "-E", "-s", "-B",
            "-m", "floati.codex_hook_install",
            "--source", install_root,
            "--root", shlex.quote(str(bus)),
            "--hooks", shlex.quote(str(self.hooks_path)),
            "--destination", shlex.quote(str(self.store)),
            "--workspace", shlex.quote(str(profile["workspace_root"])),
            "--node", "builder-a",
            "--hook-timeout-seconds", "86400",
            "--wait-deadline-seconds", "86300",
            "--acknowledge-stop-hook-change", pair,
        ])
        self.assertIn(command, evidence["remedy"])
        self.assertIn("Settings → Hooks", evidence["remedy"])
        self.assertIn("relaunch every seat", evidence["remedy"])
        self.assertIn("/boardbus", evidence["remedy"])
        self.assertIn("operator", evidence["remedy"])

    def test_undecidable_stop_definition_refuses_before_staging(self) -> None:
        bus = self.home / "bus"
        current = self.current / "scripts" / "floati-codex-wait"
        for document in (
            # two Floati groups: the installer would refuse after staging
            self._stop_document(
                self._floati_group(current, bus), self._floati_group(self.launcher, bus)
            ),
            # one Floati group whose handler list has no single hashable handler
            self._stop_document({"hooks": [
                self._floati_group(current, bus)["hooks"][0],
                {"type": "command", "command": "true"},
            ]}),
        ):
            code, out, private, temporary_root, run_client, _p, _t = self._attach_main(document)
            self.assertEqual(20, code, out)
            private.assert_not_called()
            run_client.assert_not_called()
            evidence = json.loads(out)["evidence"]
            self.assertEqual("stop_hook_definition_unavailable", evidence["code"])

    def test_unchanged_stop_definition_attaches_without_a_prompt(self) -> None:
        bus = self.home / "bus"
        current = self.current / "scripts" / "floati-codex-wait"
        code, out, private, temporary_root, run_client, _p, _t = self._attach_main(
            self._stop_document(self._floati_group(current, bus))
        )
        self.assertEqual(0, code, out)
        self.assertEqual("", out)
        private.assert_called_once()
        run_client.assert_called_once()
        module, arguments = run_client.call_args.args[2], run_client.call_args.args[3]
        self.assertEqual("floati.codex_hook_install", module)
        self.assertEqual([], [part for part in arguments if "acknowledge" in part])


class G2InstallTrustPreflightTests(FU1G2Tests):
    def test_unacknowledged_g2_commit_writes_no_receipt_generation_or_hooks(self) -> None:
        from floati.codex_hook_install import plan_waiter_rebind
        from floati.fleet_update import commit_fleet_update_g2

        plan = self._prepare_real_g2_plan()
        binding = plan["waiter_bindings"][0]
        staged = plan_waiter_rebind(
            Path(str(binding["configuration"])),
            Path(str(binding["store"])),
            str(plan["target_waiter_digest"]),
        )
        self.assertNotEqual(staged["current_hook_hash"], staged["target_hook_hash"])
        hooks = Path(str(binding["configuration"]))
        store = Path(str(binding["store"]))
        before_hooks = hooks.read_bytes()
        before_names = {path.name for path in store.iterdir()}
        before_receipts = FleetUpdateReceiptLedger(self.root).rows(self.actor)

        with self.assertRaises(ProtocolRefusal) as caught:
            commit_fleet_update_g2(
                plan=plan,
                root=self.root,
                actor=self.actor,
                idempotency_key="preflight-r2-unacked",
                target_source=self.target_source,
                installer_executable=canonical_executable(),
                git_executable="/usr/bin/git",
            )

        self.assertEqual("stop_hook_definition_change_unacknowledged", caught.exception.code)
        self.assertIn(CONSEQUENCE, caught.exception.detail)
        self.assertEqual(before_hooks, hooks.read_bytes())
        after_names = {path.name for path in store.iterdir()}
        self.assertEqual(before_names, after_names)
        self.assertFalse(any(name.startswith(".floati-fleet-stage-") for name in after_names))
        self.assertEqual(before_receipts, FleetUpdateReceiptLedger(self.root).rows(self.actor))

    def test_g2_replayed_acknowledgement_is_refused(self) -> None:
        from floati.codex_hook_install import EMPTY_STOP_PREIMAGE, plan_waiter_rebind
        from floati.fleet_update import commit_fleet_update_g2

        plan = self._prepare_real_g2_plan()
        binding = plan["waiter_bindings"][0]
        staged = plan_waiter_rebind(
            Path(str(binding["configuration"])),
            Path(str(binding["store"])),
            str(plan["target_waiter_digest"]),
        )
        hooks = Path(str(binding["configuration"]))
        store = Path(str(binding["store"]))
        before_hooks = hooks.read_bytes()
        before_names = {path.name for path in store.iterdir()}

        with self.assertRaises(ProtocolRefusal) as caught:
            commit_fleet_update_g2(
                plan=plan,
                root=self.root,
                actor=self.actor,
                idempotency_key="preflight-r2-replay",
                target_source=self.target_source,
                installer_executable=canonical_executable(),
                git_executable="/usr/bin/git",
                stop_hook_acknowledgements=[
                    {
                        "preimage": EMPTY_STOP_PREIMAGE,
                        "target": str(staged["target_hook_hash"]),
                    }
                ],
            )

        self.assertEqual("stop_hook_definition_change_unacknowledged", caught.exception.code)
        self.assertEqual(before_hooks, hooks.read_bytes())
        self.assertEqual(before_names, {path.name for path in store.iterdir()})
        self.assertEqual([], FleetUpdateReceiptLedger(self.root).rows(self.actor))


def load_tests(loader, tests, pattern):
    """Keep this module from re-running the inherited FU1-G2 suite."""

    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(InstallTrustPreflightTests))
    suite.addTests(loader.loadTestsFromTestCase(GatewayWaiterTrustSubjectTests))
    suite.addTest(
        G2InstallTrustPreflightTests(
            "test_unacknowledged_g2_commit_writes_no_receipt_generation_or_hooks"
        )
    )
    suite.addTest(
        G2InstallTrustPreflightTests("test_g2_replayed_acknowledgement_is_refused")
    )
    return suite


if __name__ == "__main__":
    unittest.main()
