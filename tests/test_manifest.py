from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


_FROZEN_PROTOCOL_ASSET_ROOTS = (
    "schemas/v0",
    "schemas/v1",
    "bundle/c7.1",
    "bundle/c7.2",
)
_FLOATI_FROZEN_PROTOCOL_ASSET_COUNT = 151
_FLOATI_FROZEN_PROTOCOL_PATHS_SHA256 = (
    "aad91335a71ce7e9a775e58ff87f8cc59544dc778d14a7d822aead9fad12b530"
)
_FLOATI_FROZEN_PROTOCOL_SNAPSHOT_SHA256 = (
    "0918081e2e09eef6365af76bacb66c539462b8c507d42cc01ee4f9e63925a35a"
)


def _frozen_protocol_snapshot() -> tuple[int, str, str]:
    'Return the exact reviewer-approved Floati frozen protocol snapshot.'

    root = Path.cwd()
    paths: list[Path] = []
    for relative_root in _FROZEN_PROTOCOL_ASSET_ROOTS:
        asset_root = root / relative_root
        if not asset_root.is_dir() or asset_root.is_symlink():
            raise AssertionError(f"frozen protocol root is not a regular directory: {relative_root}")
        for path in asset_root.rglob("*"):
            relative = path.relative_to(root)
            if path.is_symlink():
                raise AssertionError(f"frozen protocol asset is a symlink: {relative}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise AssertionError(f"frozen protocol asset is not a regular file: {relative}")
            if path.suffix == ".json":
                paths.append(relative)
    paths = sorted(paths)
    paths_sha256 = hashlib.sha256(
        "\n".join(path.as_posix() for path in paths).encode("utf-8")
    )
    snapshot_sha256 = hashlib.sha256()
    for path in paths:
        relative = path.as_posix().encode("utf-8")
        snapshot_sha256.update(relative + b"\0" + (root / path).read_bytes() + b"\0")
    return len(paths), paths_sha256.hexdigest(), snapshot_sha256.hexdigest()

try:
    from floati.manifest import verify_manifest
except ModuleNotFoundError:
    verify_manifest = None

try:
    from floati import selftest as selftest_module
except ImportError:
    selftest_module = None


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(verify_manifest, "floati.manifest must verify deployed bundle identity")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "floati").mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "schemas" / "v0").mkdir(parents=True)
        (self.root / "floati" / "core.py").write_bytes(b"CORE\n")
        (self.root / "scripts" / "floati").write_bytes(b"#!/bin/sh\nexit 0\n")
        (self.root / "scripts" / "floati").chmod(0o755)
        (self.root / "schemas" / "v0" / "record.json").write_bytes(b"{}\n")

    def entry(self, relative: str) -> dict:
        digest = hashlib.sha256((self.root / relative).read_bytes()).hexdigest()
        return {"path": relative, "sha256": digest}

    def write_manifest(self, **overrides) -> None:
        payload = {
            "schema_version": 0,
            "protocol_version": "0",
            "canonical_ref": "refs/heads/lane/hm0",
            "files": [
                self.entry("floati/core.py"),
                self.entry("schemas/v0/record.json"),
                self.entry("scripts/floati"),
            ],
        }
        payload.update(overrides)
        (self.root / "bundle-manifest.v0.json").write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )

    def test_exact_bundle_passes(self) -> None:
        self.write_manifest()
        self.assertEqual([], verify_manifest(self.root))

    def test_wrong_protocol_or_canonical_ref_is_distinguishable(self) -> None:
        self.write_manifest(protocol_version="1")
        self.assertIn("protocol_version_mismatch", verify_manifest(self.root))
        self.write_manifest(canonical_ref="refs/heads/main")
        self.assertIn("canonical_ref_mismatch", verify_manifest(self.root))

    def test_missing_extra_and_wrong_digest_are_distinguishable(self) -> None:
        self.write_manifest(files=[self.entry("floati/core.py")])
        self.assertIn("tracked_set_mismatch", verify_manifest(self.root))
        (self.root / "floati" / "extra.py").write_bytes(b"EXTRA\n")
        self.write_manifest(
            files=[
                self.entry("schemas/v0/record.json"),
                self.entry("floati/core.py"),
            ]
        )
        self.assertIn("tracked_set_mismatch", verify_manifest(self.root))
        (self.root / "floati" / "extra.py").unlink()
        self.write_manifest(
            files=[
                self.entry("schemas/v0/record.json"),
                {"path": "floati/core.py", "sha256": "0" * 64},
            ]
        )
        self.assertIn("digest_mismatch:floati/core.py", verify_manifest(self.root))

    def test_duplicate_or_escaping_paths_refuse(self) -> None:
        duplicate = self.entry("floati/core.py")
        self.write_manifest(files=[duplicate, duplicate, self.entry("schemas/v0/record.json")])
        self.assertIn("duplicate_manifest_path:floati/core.py", verify_manifest(self.root))
        self.write_manifest(files=[{"path": "../outside.py", "sha256": "0" * 64}])
        self.assertIn("manifest_path_invalid:../outside.py", verify_manifest(self.root))

    def test_launcher_byte_drift_is_a_digest_mismatch(self) -> None:
        self.write_manifest()
        (self.root / "scripts" / "floati").write_bytes(b"#!/bin/sh\nexit 1\n")

        self.assertEqual(
            ["digest_mismatch:scripts/floati"],
            verify_manifest(self.root),
        )

    def test_versioned_schema_families_are_all_deployable(self) -> None:
        """A later schema family cannot be silently omitted from the exact bundle."""
        version_one = self.root / "schemas" / "v1"
        version_one.mkdir()
        (version_one / "artifact.json").write_bytes(b"{}\n")
        self.write_manifest()

        self.assertIn("tracked_set_mismatch", verify_manifest(self.root))

    def test_dark_locks_package_is_not_part_of_the_deployable_set(self) -> None:
        """Catches private F10 code forcing partial bundle activation."""

        locks = self.root / "floati" / "locks"
        locks.mkdir()
        (locks / "private.py").write_bytes(b"PRIVATE\n")
        self.write_manifest()

        self.assertEqual([], verify_manifest(self.root))

    def test_untracked_workspace_files_are_not_the_deployable_set(self) -> None:
        """A dirty workspace must not freeze untracked files into the bundle.

        The AD-1 GREEN fail: globbing the lane tree admitted t3/devin/antigravity
        stubs that git ls-tree did not contain. tracked_set_mismatch is the name.
        """

        self.write_manifest()
        self._git("init", "--quiet", "--initial-branch=lane/hm0")
        self._git("add", "floati/core.py", "schemas/v0/record.json", "scripts/floati", "bundle-manifest.v0.json")
        self._git("commit", "--quiet", "-m", "tracked bundle")
        (self.root / "floati" / "t3.py").write_bytes(b"STUB\n")

        self.assertEqual([], verify_manifest(self.root))

        self.write_manifest(
            files=[
                self.entry("floati/core.py"),
                self.entry("floati/t3.py"),
                self.entry("schemas/v0/record.json"),
                self.entry("scripts/floati"),
            ]
        )
        self.assertIn("tracked_set_mismatch", verify_manifest(self.root))

    def _git(self, *args: str) -> None:
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "Floati Test",
                "GIT_AUTHOR_EMAIL": "floati-test@example.invalid",
                "GIT_COMMITTER_NAME": "Floati Test",
                "GIT_COMMITTER_EMAIL": "floati-test@example.invalid",
            }
        )
        completed = subprocess.run(
            ["git", *args],
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, completed.returncode)

    def test_repository_manifest_matches_current_deployable_tree(self) -> None:
        self.assertEqual([], verify_manifest(Path.cwd()))

    def test_repository_manifest_includes_authority_grant_surface(self) -> None:
        """Catches an installed grant command missing its exact authority substrate."""

        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        entries = manifest["files"]
        paths = [entry["path"] for entry in entries]
        required = {
            "floati/admin_registry.py",
            "floati/cli.py",
            "floati/grants.py",
            "floati/planes.py",
            "floati/records.py",
            "floati/registry.py",
            "floati/work.py",
            "schemas/v0/authority-grant-record.schema.json",
        }
        self.assertEqual(sorted(paths), paths)
        self.assertTrue(required <= set(paths), required - set(paths))
        by_path = {entry["path"]: entry["sha256"] for entry in entries}
        for relative in sorted(required):
            with self.subTest(path=relative):
                self.assertEqual(
                    hashlib.sha256(Path(relative).read_bytes()).hexdigest(),
                    by_path.get(relative),
                )

    def test_repository_manifest_names_cli_and_license_artifacts(self) -> None:
        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertTrue(
            {
                "LICENSE",
                "schemas/LICENSE",
                "bundle/c7.1/LICENSE",
                "bundle/c7.2/LICENSE",
                "scripts/floati",
                "floati/__main__.py",
                "floati/cli.py",
            }
            <= paths
        )
        self.assertTrue(
            {
                "schemas/v1/doctor-artifact.schema.json",
                "schemas/v1/fleet-status-artifact.schema.json",
                "schemas/v1/watch-artifact.schema.json",
            }
            <= paths
        )
        self.assertTrue(Path("scripts/floati").stat().st_mode & 0o111)

    def test_repository_manifest_tracks_only_the_floati_code_identity(self) -> None:
        """Catches shipping a retired package or launcher after the clean-break rename."""

        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertIn("scripts/floati", paths)
        self.assertNotIn("scripts/slip", paths)
        self.assertTrue(any(path.startswith("floati/") for path in paths))
        self.assertFalse(any(path.startswith("slip/") for path in paths))

    def test_repository_manifest_includes_runtime_c7_contract_assets(self) -> None:
        """Catches an installed reader importing a package that deployment did not copy."""
        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertTrue(
            {
                "bundle/c7.1/README.md",
                "bundle/c7.1/bundle-index.json",
                "bundle/c7.1/schema-catalog.json",
                "bundle/c7.1/schemas/c7-read-bundle.schema.json",
                "bundle/c7.1/schemas/canonical-projection.schema.json",
            }
            <= paths
        )

    def test_repository_manifest_includes_runtime_c7_2_contract_assets(self) -> None:
        """Catches the C7.2 reader shipping without its sibling static contract."""
        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertTrue(
            {
                "bundle/c7.2/README.md",
                "bundle/c7.2/bundle-index.json",
                "bundle/c7.2/schema-catalog.json",
                "bundle/c7.2/schemas/c7-read-bundle.schema.json",
                "bundle/c7.2/schemas/canonical-projection.schema.json",
                "schemas/v1/attempt-harness-session-bound-record.schema.json",
                "floati/c7_2_bundle.py",
            }
            <= paths
        )

    def test_repository_manifest_includes_post_v1_sequencer_runtime(self) -> None:
        """Catches a deployed sequencer missing any post-v1 runtime or schema dependency."""
        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        paths = [entry["path"] for entry in manifest["files"]]
        required = {
            "schemas/v1/run-admission-bound-record.schema.json",
            "schemas/v1/run-segment-opened-record.schema.json",
            "schemas/v1/run-segment-sealed-record.schema.json",
            "schemas/v1/sequencer-epoch-record.schema.json",
            "floati/run_limits.py",
            "floati/run_segments.py",
            "floati/sequencer.py",
            "floati/sequencer_epoch.py",
            "floati/sequencer_scale.py",
            "floati/wake.py",
        }
        self.assertEqual(sorted(paths), paths)
        self.assertTrue(required <= set(paths), required - set(paths))

    def test_repository_manifest_includes_current_approval_suspension_runtime(self) -> None:
        """Catches shipping any approval-suspension frame without its exact runtime bytes."""
        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        entries = manifest["files"]
        paths = [entry["path"] for entry in entries]
        required = {
            "schemas/v1/approval-request-record.schema.json",
            "schemas/v1/approval-decision-record.schema.json",
            "schemas/v1/attempt-suspended-for-approval-record.schema.json",
            "schemas/v1/approval-consumed-for-resume-record.schema.json",
            "floati/suspension.py",
        }
        self.assertEqual(sorted(paths), paths)
        self.assertTrue(required <= set(paths), required - set(paths))
        for entry in entries:
            with self.subTest(path=entry["path"]):
                self.assertEqual(
                    hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest(),
                    entry["sha256"],
                )

    def test_repository_manifest_includes_complete_spawn_groups_runtime(self) -> None:
        """Catches shipping any spawn frame without its exact runtime and schema set."""

        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        entries = manifest["files"]
        paths = [entry["path"] for entry in entries]
        required = {
            "floati/spawn_groups.py",
            "schemas/v1/attempt-cancelled-before-start-record.schema.json",
            "schemas/v1/attempt-spawn-policy-bound-record.schema.json",
            "schemas/v1/child-admitted-record.schema.json",
            "schemas/v1/child-rejected-record.schema.json",
            "schemas/v1/descendant-observation-closed-record.schema.json",
            "schemas/v1/exact-items-cancel-requested-record.schema.json",
            "schemas/v1/exact-items-cancel-scope-resolved-record.schema.json",
            "schemas/v1/run-spawn-admission-enabled-record.schema.json",
            "schemas/v1/spawn-child-cancelled-without-attempt-record.schema.json",
            "schemas/v1/spawn-group-aborted-record.schema.json",
            "schemas/v1/spawn-group-closed-record.schema.json",
            "schemas/v1/spawn-group-created-record.schema.json",
            "schemas/v1/spawn-late-result-disposition-record.schema.json",
            "schemas/v1/spawn-plan-amendment-record.schema.json",
            "schemas/v1/untracked-descendant-record.schema.json",
        }
        self.assertEqual(sorted(paths), paths)
        self.assertTrue(required <= set(paths), required - set(paths))
        by_path = {entry["path"]: entry["sha256"] for entry in entries}
        for relative in sorted(required):
            with self.subTest(path=relative):
                self.assertEqual(
                    hashlib.sha256(Path(relative).read_bytes()).hexdigest(),
                    by_path.get(relative),
                )

    def test_repository_manifest_includes_complete_effect_ledger_runtime(self) -> None:
        """Catches an Effect install missing authority, observer, Worker, or operator bytes."""

        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        entries = manifest["files"]
        paths = [entry["path"] for entry in entries]
        required = {
            "schemas/v1/compensation-executed-record.schema.json",
            "schemas/v1/compensation-proposed-record.schema.json",
            "schemas/v1/effect-acknowledged-record.schema.json",
            "schemas/v1/effect-confirmed-record.schema.json",
            "schemas/v1/effect-dispatched-record.schema.json",
            "schemas/v1/effect-failed-record.schema.json",
            "schemas/v1/effect-intent-record.schema.json",
            "schemas/v1/effect-reconciled-record.schema.json",
            "schemas/v1/effect-record-common.schema.json",
            "schemas/v1/effect-status-artifact.schema.json",
            "schemas/v1/effect-unknown-record.schema.json",
            "schemas/v1/run-result-accepted-record.schema.json",
            "floati/adapters/claude.py",
            "floati/adapters/codex_live.py",
            "floati/adapters/pi.py",
            "floati/approvals.py",
            "floati/cli.py",
            "floati/copy.py",
            "floati/effect_reconciliation_exec.py",
            "floati/effect_reconciliation_observer.py",
            "floati/effect_reconciliation_protocol.py",
            "floati/effects.py",
            "floati/helptext.py",
            "floati/jsonl.py",
            "floati/policy.py",
            "floati/projection.py",
            "floati/records.py",
            "floati/run_limits.py",
            "floati/runtruth.py",
            "floati/sequencer.py",
            "floati/spawn_groups.py",
            "floati/supervisor.py",
            "floati/tui.py",
            "floati/tui_render.py",
            "floati/worker_adapter_runtime.py",
            "floati/worker_bootstrap.py",
            "floati/worker_bootstrap_protocol.py",
            "floati/worker_errors.py",
            "floati/worker_exec.py",
            "floati/worker_isolation.py",
            "floati/workers.py",
        }
        self.assertEqual(sorted(paths), paths)
        self.assertTrue(required <= set(paths), required - set(paths))
        by_path = {entry["path"]: entry["sha256"] for entry in entries}
        for relative in sorted(required):
            with self.subTest(path=relative):
                self.assertEqual(
                    hashlib.sha256(Path(relative).read_bytes()).hexdigest(),
                    by_path.get(relative),
                )

    def test_effect_schema_family_is_complete_and_sorted(self) -> None:
        """Catches a partial or unordered Effect schema family in an installed bundle."""

        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        entries = manifest["files"]
        expected = [
            "schemas/v1/compensation-executed-record.schema.json",
            "schemas/v1/compensation-proposed-record.schema.json",
            "schemas/v1/effect-acknowledged-record.schema.json",
            "schemas/v1/effect-confirmed-record.schema.json",
            "schemas/v1/effect-dispatched-record.schema.json",
            "schemas/v1/effect-failed-record.schema.json",
            "schemas/v1/effect-intent-record.schema.json",
            "schemas/v1/effect-reconciled-record.schema.json",
            "schemas/v1/effect-record-common.schema.json",
            "schemas/v1/effect-status-artifact.schema.json",
            "schemas/v1/effect-unknown-record.schema.json",
        ]
        actual_entries = [
            entry
            for entry in entries
            if entry["path"].startswith(
                ("schemas/v1/compensation-", "schemas/v1/effect-")
            )
        ]
        actual = [entry["path"] for entry in actual_entries]
        self.assertEqual(expected, actual)
        for entry in actual_entries:
            with self.subTest(path=entry["path"]):
                self.assertEqual(
                    hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest(),
                    entry["sha256"],
                )

    def test_repository_manifest_includes_complete_thread_observer_contract(self) -> None:
        """Catches an install missing Thread Observation runtime/schema/help bytes."""

        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        entries = manifest["files"]
        paths = [entry["path"] for entry in entries]
        required = {
            "schemas/v1/thread-attachment-detached-record.schema.json",
            "schemas/v1/thread-attachment-registered-record.schema.json",
            "schemas/v1/thread-observation-recorded-record.schema.json",
            "schemas/v1/thread-observation-status-artifact.schema.json",
            "floati/cli.py",
            "floati/copy.py",
            "floati/helptext.py",
            "floati/jsonl.py",
            "floati/projection.py",
            "floati/records.py",
            "floati/thread_observations.py",
            "floati/thread_source.py",
        }
        contract_tests = {
            "tests/fixtures/codex-thread-observer/reference_harness.py",
            "tests/test_thread_cli.py",
            "tests/test_thread_observations.py",
            "tests/test_thread_source.py",
        }
        self.assertEqual(sorted(paths), paths)
        self.assertTrue(required <= set(paths), required - set(paths))
        by_path = {entry["path"]: entry["sha256"] for entry in entries}
        for relative in sorted(required):
            with self.subTest(path=relative):
                self.assertEqual(
                    hashlib.sha256(Path(relative).read_bytes()).hexdigest(),
                    by_path.get(relative),
                )
        self.assertEqual(contract_tests, {path for path in contract_tests if Path(path).is_file()})

    def test_repository_manifest_includes_complete_wake_hold_runtime_contract(self) -> None:
        """Catches an installed Wake/Hold decision missing a runtime or v1 schema dependency."""
        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        entries = manifest["files"]
        paths = [entry["path"] for entry in entries]
        required = {
            "schemas/v1/wake-decision-artifact.schema.json",
            "schemas/v1/wake-hold-receipt-record.schema.json",
            "schemas/v1/wake-path-status-artifact.schema.json",
            "floati/cli.py",
            "floati/cursor.py",
            "floati/events.py",
            "floati/jsonl.py",
            "floati/records.py",
            "floati/wake.py",
            "floati/wake_hold.py",
        }
        self.assertEqual(sorted(paths), paths)
        self.assertTrue(required <= set(paths), required - set(paths))
        by_path = {entry["path"]: entry["sha256"] for entry in entries}
        for relative in sorted(required):
            with self.subTest(path=relative):
                self.assertEqual(
                    hashlib.sha256(Path(relative).read_bytes()).hexdigest(),
                    by_path.get(relative),
                )

    def test_wake_hold_whole_review_base_and_changed_inventory_are_exact(self) -> None:
        """Catches closure evidence that omits the whole-review base or any changed Wake/Hold path."""
        if not Path(".github/public-export-policy.v0.json").is_file():
            self.skipTest("harbor-only history contract")
        base = "1b4b5639b946574e0fd196348b5c57a739c1b2a2"
        candidate = "097867580d79d6fc7874d0dc55a689b4f4ab1669"
        completed = subprocess.run(
            ["/usr/bin/git", "diff", "--name-only", base, candidate],
            cwd=Path.cwd(), text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        evidence = Path("docs/evidence/POST-V1-WAKE-HOLD.md").read_text(encoding="utf-8")
        self.assertIn(base, evidence)
        self.assertIn(candidate, evidence)
        self.assertEqual(
            sorted(path for path in completed.stdout.splitlines() if path),
            sorted(re.findall(r"^- `([^`]+)`$", evidence.split("Changed paths against the whole-review base:", 1)[1].split("##", 1)[0], re.MULTILINE)),
        )

    def test_frozen_protocol_json_bytes_match_the_floati_rebaseline(self) -> None:
        """Catches unruled byte drift across both schema versions and C7 assets."""

        self.assertEqual(
            (
                _FLOATI_FROZEN_PROTOCOL_ASSET_COUNT,
                _FLOATI_FROZEN_PROTOCOL_PATHS_SHA256,
                _FLOATI_FROZEN_PROTOCOL_SNAPSHOT_SHA256,
            ),
            _frozen_protocol_snapshot(),
        )

    def test_frozen_protocol_json_inventory_matches_the_floati_rebaseline(self) -> None:
        """Catches additions, removals, links, or path drift in all frozen JSON assets."""

        self.assertEqual(
            (
                _FLOATI_FROZEN_PROTOCOL_ASSET_COUNT,
                _FLOATI_FROZEN_PROTOCOL_PATHS_SHA256,
                _FLOATI_FROZEN_PROTOCOL_SNAPSHOT_SHA256,
            ),
            _frozen_protocol_snapshot(),
        )

    def test_manifest_rejects_a_symlinked_parent_component(self) -> None:
        """Catches a manifest path that reaches a regular file through a redirected directory."""
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "core.py").write_bytes(b"CORE\n")
        (self.root / "slip-link").symlink_to(outside, target_is_directory=True)
        digest = hashlib.sha256((outside / "core.py").read_bytes()).hexdigest()
        self.write_manifest(
            files=[
                self.entry("schemas/v0/record.json"),
                {"path": "slip-link/core.py", "sha256": digest},
            ]
        )

        self.assertIn("file_symlink:slip-link/core.py", verify_manifest(self.root))

    def test_repository_policy_module_is_deployable_but_repository_input_is_not(self) -> None:
        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertIn("floati/policy.py", paths)
        self.assertNotIn("FLOATI.toml", paths)

    def test_selftest_preserves_test_and_manifest_exit_classes(self) -> None:
        outcome_for = getattr(selftest_module, "outcome_for", None)
        self.assertIsNotNone(outcome_for, "selftest must classify its own artifact exits")
        self.assertEqual(10, outcome_for(False, []))
        self.assertEqual(34, outcome_for(True, ["digest_mismatch:floati/core.py"]))
        self.assertEqual(0, outcome_for(True, []))


if __name__ == "__main__":
    unittest.main()
