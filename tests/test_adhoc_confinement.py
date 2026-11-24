"""W1 requires real kernel denial and positive provider writes on this host."""
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.worker_bootstrap_protocol import BuiltInAdapterSpec, require_borrowed_native_readiness
from floati.worker_isolation import cleanup_worker_isolation, prepare_borrowed_worker_isolation
from tests.fixtures.adhoc_stage1 import AdhocFixture
from tests.test_worker_isolation import _child_result


def _kernel_host():
    """The hosts with a borrowed kernel backend; any other host refuses by name."""
    if sys.platform == "darwin":
        return "darwin"
    return "linux" if sys.platform.startswith("linux") else None


class AdhocConfinementTests(AdhocFixture, unittest.TestCase):
    def test_fake_provider_can_write_only_the_row_and_its_slot(self):
        # The row already holds a tracked file and a tracked directory, as a
        # provider's real task tree does; the provider edits paths beneath them.
        self.commit_row_entries(self.workspace, {"notes/keep.txt": "row note\n"})
        # This positive fixture requires a safe owned borrowed tree even
        # under umask 002; preserve the production replacement fence.
        for path in [self.workspace, *self.workspace.rglob("*")]:
            self.assertFalse(path.is_symlink())
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o022)
        profile = self.profile()
        binding = {key: profile[key] for key in ("lane_opening_id", "node_lease_id", "workspace")}
        binding["profile_id"] = profile["id"]
        (self.root.tenant_home / "effects").mkdir(mode=0o700)
        host = _kernel_host()
        if host is None:
            for harness in ("codex", "claude", "pi"):
                with self.assertRaises(ProtocolRefusal) as caught:
                    require_borrowed_native_readiness(BuiltInAdapterSpec(harness, ("/usr/bin/false",), "borrowed"))
                self.assertEqual("adhoc_write_confinement_unsupported", caught.exception.code)
                self.assertIn(harness, caught.exception.detail)
            return
        policy = prepare_borrowed_worker_isolation(self.root, binding, "hostile-provider")
        self.addCleanup(cleanup_worker_isolation, policy)
        slot = policy.provider_state
        notes = self.workspace / "notes"
        targets = {
            "sibling": str(Path(self.foreign_opened["workspace"]) / "tracked.txt"),
            "bus": str(self.root.tenant_home / "state/hostile-write"),
            "home": str(Path(os.environ["HOME"]) / "hostile-write"),
            "scratch": str(policy.scratch / "hostile-write"),
            "git_pointer": str(self.workspace / ".git"),
            "git_pointer_replaced": {"path": str(self.workspace / ".git"),
                                     "rename_from": str(notes / "replacement-pointer")},
            "git_common": str(self.repository / ".git/config"),
            "slot_owner": str(slot / "floati-adhoc-owner.json"),
            "slot_parent": str(slot.parent / "hostile-write"),
            "row_tracked_file": str(self.workspace / "tracked.txt"),
            "row_directory": str(notes / "allowed-write"),
            "row_root_entry": str(self.workspace / "allowed-write"),
            "slot": str(slot / "allowed-write"),
        }
        allowed = {"row_tracked_file", "row_directory", "slot"}
        # Each host's kernel boundary is exact, and differs only where its
        # backend can express it: Landlock only adds rights, so on Linux the
        # row root's own entries are fixed (that is what keeps .git out of
        # reach) and the slot is writable whole, its owner marker guarded by
        # content at every profile pin instead.
        allowed |= {"darwin": {"row_root_entry"}, "linux": {"slot_owner"}}[host]

        def target_path(target):
            return Path(target["path"] if isinstance(target, dict) else target)

        before = {key: target_path(target).read_bytes() if target_path(target).exists() else None
                  for key, target in targets.items()}
        # Also prove the boundary follows path resolution for links created by
        # the provider after activation, rather than trusting a layout scan.
        provider = Path(__file__).parent / "fixtures/adhoc-native/write_probe.py"

        def payload():
            alias = notes / "escape-link"
            alias.symlink_to(Path(targets["home"]).parent, target_is_directory=True)
            attempted = dict(targets, symlink=str(alias / "hostile-write"))
            completed = subprocess.run([sys.executable, "-B", str(provider.resolve())],
                input=json.dumps(attempted), capture_output=True, text=True, timeout=5)
            return {"exit": completed.returncode, "stderr": completed.stderr,
                    "writes": json.loads(completed.stdout)}

        result = _child_result(policy, payload)
        self.assertEqual("ready", result["status"], result)
        backend = "macos-sandbox" if host == "darwin" else "linux-landlock-v"
        self.assertTrue(result["backend"].startswith(backend), result["backend"])
        self.assertEqual(0, result["payload"]["exit"], result)
        writes = result["payload"]["writes"]
        self.assertEqual(set(targets) | {"symlink"}, set(writes))
        for key in sorted(writes):
            with self.subTest(target=key, host=host):
                if key in allowed:
                    self.assertEqual({"status": "ok"}, writes[key])
                    continue
                self.assertEqual("refused", writes[key]["status"])
                self.assertIn(writes[key]["errno"], {errno.EACCES, errno.EPERM})
                if key in targets:
                    path = target_path(targets[key])
                    self.assertEqual(before[key], path.read_bytes() if path.exists() else None)
        if host == "linux":
            # The slot marker is the one boundary Linux guards by content: a
            # changed marker is refused before the profile can be used again.
            with self.assertRaises(ProtocolRefusal) as caught:
                self.profiles().get(profile["id"])
            self.assertEqual("adhoc_binding_changed", caught.exception.code)
        else:
            self.assertEqual(profile["id"], self.profiles().get(profile["id"])["id"])


class AdhocReadinessBoundaryTests(unittest.TestCase):
    def test_unsupported_host_names_each_harness_before_provider_launch(self):
        for harness in ("codex", "claude", "pi"):
            with self.subTest(harness=harness), mock.patch("sys.platform", "unsupported-host"):
                with self.assertRaises(ProtocolRefusal) as caught:
                    require_borrowed_native_readiness(BuiltInAdapterSpec(harness, ("/usr/bin/false",), "borrowed"))
                self.assertEqual("adhoc_write_confinement_unsupported", caught.exception.code)
                self.assertIn(harness, caught.exception.detail)
