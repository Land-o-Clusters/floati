"""Contracts for the non-authoritative one-shot wake boundary."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest import mock

import floati.wake as wake_module
from floati.contracts import TaskContract, contract_digest
from floati.errors import ProtocolRefusal
from floati.identity_fence import RETIRED_PRODUCT_NAME
from floati.ids import uuid7_hex
from floati.root import FloatiRoot
from floati.runtruth import RunLedger
from floati.scheduler import RetryPolicy, RunScheduler
from floati.wake import (
    FakeOneShotWakeRegistrar,
    OneShotWakeRegistrar,
    OneShotWakeRequest,
    replay_one_shot_wake,
)
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


NOW = "2099-01-02T03:04:05.000Z"
WAKE_AT = "2099-01-02T03:05:06.000Z"
FIRED_AT = "2000-01-02T03:05:06.000Z"
RUN_ID = "run-018f7e9b3c117abc8def0123456789ab"
ITEM_ID = "work-018f7e9b3c117abc8def0123456789ab"
DIGEST = "a" * 64


def _record(kind: str, **fields: object) -> dict:
    prefixes = {
        "run_created": "run-created-",
        "task_contract": "task-contract-",
        "run_policy_bound": "run-policy-bound-",
        "worker_pool_bound": "run-worker-pool-bound-",
    }
    return {
        "schema_version": 0,
        "id": prefixes[kind] + uuid7_hex(),
        "tenant_id": "alpha",
        "timestamp": NOW,
        "kind": kind,
        **fields,
    }


def _seed_open_attempt(root: FloatiRoot) -> tuple[RunLedger, RunScheduler, dict]:
    ledger = RunLedger(root)
    policy = RetryPolicy(max_attempts=2, base_delay_ms=100, cap_delay_ms=1000)
    ledger.append(_record("run_created", run_id=RUN_ID, plan_digest=DIGEST,
                          item_ids=[ITEM_ID], dependency_edges=[]))
    contract = TaskContract.create(
        objective="wake one current retry", non_goals=["no wake record"],
        areas_to_avoid=[{"path": "floati/runtruth.py", "region": "all"}],
        input_hashes={"brief": DIGEST}, acceptance_checks={"tests.unit": "python3 -m unittest"},
        constraints={"network": "dark"}, risk_class="high",
        retry_policy={"max_attempts": 2, "backoff": {
            "base_delay_ms": 100, "cap_delay_ms": 1000, "strategy": "exponential",
        }}, dependencies=[],
    )
    ledger.append(_record("task_contract", run_id=RUN_ID, item_id=ITEM_ID,
                          **contract.canonical(), contract_digest=contract_digest(contract)))
    ledger.append(_record("run_policy_bound", run_id=RUN_ID, policy_digest=DIGEST))
    ledger.append(_record("worker_pool_bound", run_id=RUN_ID, worker_ids=["worker-a"]))
    scheduler = RunScheduler(ledger)
    return ledger, scheduler, scheduler.open_attempt(RUN_ID, ITEM_ID, policy, 7, now=NOW)


class _OneShotWakePathTestCase(unittest.TestCase):
    """Real callback files for descriptor-bound wake testimony contracts."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name) / "root", "alpha")
        self.callback = Path(self.temp.name) / "callback"
        self.callback.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.callback.chmod(0o700)
        self.request = OneShotWakeRequest(
            root=self.root, run_id=RUN_ID, item_id=ITEM_ID,
            attempt_id="attempt-018f7e9b3c117abc8def0123456789ab", wake_at=WAKE_AT,
            scheduler_epoch=7, fence_token=DIGEST,
        )

    def registrar(self, callback: Optional[Path] = None) -> OneShotWakeRegistrar:
        return OneShotWakeRegistrar(
            callback_path=self.callback if callback is None else callback,
            now=lambda: datetime(2099, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )

    def _written(self, registrar: OneShotWakeRegistrar) -> tuple[dict, Path]:
        preview = registrar.preview(self.request)
        registrar.register(self.request, preview["digest"])
        return preview, Path(preview["path"])

    @staticmethod
    def _recompute_semantic_digest(plist: dict) -> None:
        identity = dict(plist["FloatiRegistrationIdentity"])
        identity.pop("semantic_preview_digest", None)
        semantic = dict(plist)
        semantic["FloatiRegistrationIdentity"] = identity
        plist["FloatiRegistrationIdentity"]["semantic_preview_digest"] = hashlib.sha256(
            plistlib.dumps(semantic, fmt=plistlib.FMT_XML, sort_keys=True)
        ).hexdigest()


class OneShotWakePathBindingTests(_OneShotWakePathTestCase):
    def test_register_restores_replacement_raced_between_quarantine_check_and_rename(self) -> None:
        """Catches failure quarantine moving a post-check replacement off its deterministic path."""
        root = FloatiRoot.open(Path(self.temp.name) / "root", "alpha-quarantine-rename-race")
        request = replace(self.request, root=root)
        registrar = self.registrar()
        preview = registrar.preview(request)
        leaf = Path(preview["path"])
        leaf.parent.mkdir(mode=0o700, exist_ok=True)
        replacement = leaf.with_name(leaf.name + ".replacement")
        real_rename = os.rename

        def rename(source: str, destination: str, **kwargs: object) -> None:
            if source == leaf.name and destination.startswith(f".{leaf.name}.failure-"):
                replacement.write_bytes(b"post-check replacement must remain")
                replacement.chmod(0o600)
                os.replace(replacement, leaf)
            real_rename(source, destination, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("floati.wake.os.write", side_effect=OSError("forced write failure")))
            stack.enter_context(mock.patch("floati.wake.os.rename", side_effect=rename))
            with self.assertRaises(ProtocolRefusal):
                registrar.register(request, preview["digest"])

        self.assertTrue(leaf.exists())
        self.assertEqual(b"post-check replacement must remain", leaf.read_bytes())
        quarantines = list(leaf.parent.glob(f".{leaf.name}.failure-*"))
        self.assertEqual(1, len(quarantines))
        self.assertEqual(b"post-check replacement must remain", quarantines[0].read_bytes())
        self.assertEqual(leaf.stat().st_ino, quarantines[0].stat().st_ino)

        leaf.unlink()
        retry = registrar.register(request, preview["digest"])
        self.assertEqual("written_unloaded", retry["state"])
        registrar.cleanup(str(preview["label"]))
        self.assertFalse(leaf.exists())

    def test_register_recovers_first_fstat_identity_for_quarantine_and_exact_retry(self) -> None:
        """Catches the first post-create fstat failure stranding the deterministic leaf."""
        real_fstat = os.fstat
        for replace_leaf in (False, True):
            with self.subTest(replace_leaf=replace_leaf):
                suffix = "replaced" if replace_leaf else "created"
                root = FloatiRoot.open(Path(self.temp.name) / "root", f"alpha-first-fstat-{suffix}")
                request = replace(self.request, root=root)
                registrar = self.registrar()
                preview = registrar.preview(request)
                leaf = Path(preview["path"])
                leaf.parent.mkdir(mode=0o700, exist_ok=True)
                replacement = leaf.with_name(leaf.name + ".replacement")
                injected = False

                def fstat(descriptor: int) -> os.stat_result:
                    nonlocal injected
                    metadata = real_fstat(descriptor)
                    if (
                        not injected
                        and stat.S_ISREG(metadata.st_mode)
                        and metadata.st_size == 0
                        and leaf.exists()
                        and metadata.st_ino == leaf.stat().st_ino
                    ):
                        injected = True
                        if replace_leaf:
                            replacement.write_bytes(b"replacement must remain")
                            replacement.chmod(0o600)
                            os.replace(replacement, leaf)
                        raise OSError("forced first post-create fstat failure")
                    return metadata

                with mock.patch("floati.wake.os.fstat", side_effect=fstat):
                    with self.assertRaises(ProtocolRefusal):
                        registrar.register(request, preview["digest"])

                quarantines = list(leaf.parent.glob(f".{leaf.name}.failure-*"))
                if replace_leaf:
                    self.assertEqual(b"replacement must remain", leaf.read_bytes())
                    self.assertEqual([], quarantines)
                    leaf.unlink()
                else:
                    self.assertFalse(leaf.exists())
                    self.assertEqual(1, len(quarantines))

                retry = registrar.register(request, preview["digest"])
                self.assertEqual("written_unloaded", retry["state"])
                registrar.cleanup(str(preview["label"]))
                self.assertFalse(leaf.exists())

    def test_register_quarantines_every_identity_bound_partial_write_and_allows_exact_retry(self) -> None:
        """Catches pre-fstat write failures stranding the deterministic leaf or deleting a replacement."""
        real_write = os.write
        real_fstat = os.fstat
        real_fsync = os.fsync
        seams = (
            "short-write", "write-error", "fstat", "file-fsync",
            "parent-fsync", "replacement", "disappearance",
        )
        for seam in seams:
            with self.subTest(seam=seam):
                root = FloatiRoot.open(Path(self.temp.name) / "root", f"alpha-{seam}")
                request = replace(self.request, root=root)
                registrar = self.registrar()
                preview = registrar.preview(request)
                leaf = Path(preview["path"])
                leaf.parent.mkdir(mode=0o700, exist_ok=True)
                replacement = leaf.with_name(leaf.name + ".replacement")

                def write(descriptor: int, payload: bytes) -> int:
                    if seam == "short-write":
                        return real_write(descriptor, payload[: len(payload) // 2])
                    if seam == "write-error":
                        raise OSError("forced write failure")
                    return real_write(descriptor, payload)

                def fstat(descriptor: int) -> os.stat_result:
                    metadata = real_fstat(descriptor)
                    if (
                        seam == "fstat"
                        and stat.S_ISREG(metadata.st_mode)
                        and metadata.st_size > 0
                        and leaf.exists()
                        and metadata.st_ino == leaf.stat().st_ino
                    ):
                        raise OSError("forced fstat failure")
                    return metadata

                def fsync(descriptor: int) -> None:
                    metadata = real_fstat(descriptor)
                    is_leaf = (
                        stat.S_ISREG(metadata.st_mode)
                        and leaf.exists()
                        and metadata.st_ino == leaf.stat().st_ino
                    )
                    if is_leaf and seam in {"file-fsync", "replacement", "disappearance"}:
                        if seam == "replacement":
                            replacement.write_bytes(b"replacement must remain")
                            replacement.chmod(0o600)
                            os.replace(replacement, leaf)
                        elif seam == "disappearance":
                            leaf.unlink()
                        raise OSError("forced file fsync failure")
                    if seam == "parent-fsync" and stat.S_ISDIR(metadata.st_mode) and leaf.exists():
                        raise OSError("forced parent fsync failure")
                    real_fsync(descriptor)

                with ExitStack() as stack:
                    stack.enter_context(mock.patch("floati.wake.os.write", side_effect=write))
                    stack.enter_context(mock.patch("floati.wake.os.fstat", side_effect=fstat))
                    stack.enter_context(mock.patch("floati.wake.os.fsync", side_effect=fsync))
                    with self.assertRaises(ProtocolRefusal):
                        registrar.register(request, preview["digest"])

                if seam == "replacement":
                    self.assertEqual(b"replacement must remain", leaf.read_bytes())
                    leaf.unlink()
                else:
                    self.assertFalse(leaf.exists())
                quarantines = list(leaf.parent.glob(f".{leaf.name}.failure-*"))
                if seam not in {"replacement", "disappearance"}:
                    self.assertEqual(1, len(quarantines))

                retry = registrar.register(request, preview["digest"])
                self.assertEqual("written_unloaded", retry["state"])
                self.assertEqual("written_unloaded", registrar.status(request)["path_state"])
                registrar.cleanup(str(preview["label"]))
                self.assertFalse(leaf.exists())

    def test_register_contains_the_wake_leaf_and_retains_a_raced_replacement_on_durability_failure(self) -> None:
        """Catches registration following a replaced parent or path-deleting a new leaf on failure."""
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        registrar = self.registrar()
        preview = registrar.preview(self.request)
        wake_parent = Path(preview["path"]).parent
        wake_parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ProtocolRefusal):
            registrar.register(self.request, preview["digest"])
        self.assertFalse(any(outside.iterdir()))
        wake_parent.unlink()

        for seam in ("leaf-fsync", "parent-fsync"):
            with self.subTest(seam=seam):
                registrar = self.registrar()
                preview = registrar.preview(self.request)
                leaf = Path(preview["path"])
                leaf.parent.mkdir(mode=0o700, exist_ok=True)
                replacement = leaf.with_name(leaf.name + ".replacement")
                real_fsync = os.fsync
                def fail_after_replacement(descriptor: int) -> None:
                    mode = os.fstat(descriptor).st_mode
                    should_fail = stat.S_ISREG(mode) if seam == "leaf-fsync" else (
                        stat.S_ISDIR(mode)
                    )
                    if should_fail:
                        replacement.write_bytes(b"replacement must remain")
                        replacement.chmod(0o600)
                        os.replace(replacement, leaf)
                        raise OSError("forced fsync failure")
                    real_fsync(descriptor)

                with mock.patch("floati.wake.os.fsync", side_effect=fail_after_replacement):
                    with self.assertRaises(ProtocolRefusal):
                        registrar.register(self.request, preview["digest"])
                self.assertEqual(b"replacement must remain", leaf.read_bytes())
                leaf.unlink()

    def test_preview_binds_a_real_held_descriptor_identity_and_semantic_digest(self) -> None:
        """Catches a preview whose approval omits callback inode, mode, bytes, or path."""
        preview = self.registrar().preview(self.request)
        metadata = self.callback.stat()

        self.assertEqual(str(self.callback), preview["callback_identity"]["canonical_path"])
        self.assertEqual(metadata.st_dev, preview["callback_identity"]["device"])
        self.assertEqual(metadata.st_ino, preview["callback_identity"]["inode"])
        self.assertEqual(stat.S_IMODE(metadata.st_mode), preview["callback_identity"]["mode"])
        self.assertEqual(metadata.st_size, preview["callback_identity"]["size"])
        self.assertEqual(hashlib.sha256(self.callback.read_bytes()).hexdigest(),
                         preview["callback_identity"]["sha256"])
        self.assertEqual(64, len(preview["digest"]))

    def test_preview_refuses_unsafe_callback_paths_before_creating_local_testimony(self) -> None:
        """Catches callback validation that follows links or accepts unsafe execution names."""
        directory = Path(self.temp.name) / "directory"
        directory.mkdir()
        linked = Path(self.temp.name) / "linked"
        linked.symlink_to(self.callback)
        non_executable = Path(self.temp.name) / "not-executable"
        non_executable.write_text("callback\n", encoding="utf-8")
        non_executable.chmod(0o600)
        unreadable = Path(self.temp.name) / "unreadable"
        unreadable.write_text("callback\n", encoding="utf-8")
        unreadable.chmod(0o100)
        unsafe = Path(self.temp.name) / "unsafe\u202e-name"
        unsafe.write_text("callback\n", encoding="utf-8")
        unsafe.chmod(0o700)
        cases = (
            Path("relative-callback"), Path(self.temp.name) / "missing", linked,
            directory, non_executable, unreadable, unsafe,
            Path("/") / ("x" * 5000),
        )
        for callback in cases:
            with self.subTest(callback=str(callback)):
                with self.assertRaises(ProtocolRefusal):
                    self.registrar(callback).preview(self.request)
        self.assertFalse((self.root.tenant_home / "wake").exists())

    def test_preview_maps_unencodable_surrogate_to_a_typed_path_refusal(self) -> None:
        """Catches a terminal-unsafe surrogate escaping as a raw UnicodeEncodeError."""
        with self.assertRaises(ProtocolRefusal) as caught:
            self.registrar(Path("\x2fprivate/tmp/callback-\ud800")).preview(self.request)
        self.assertEqual("wake_callback_invalid", caught.exception.code)

    def test_preview_accepts_a_lawful_unicode_callback_path(self) -> None:
        """Catches path validation rejecting an encodable non-ASCII executable name."""
        callback = Path(self.temp.name) / "callback-caf\u00e9"
        callback.write_bytes(b"#!/bin/sh\nexit 0\n")
        callback.chmod(0o700)

        preview = self.registrar(callback).preview(self.request)

        self.assertEqual(str(callback), preview["callback_identity"]["canonical_path"])

    def test_register_refuses_all_preview_to_write_callback_races_without_a_plist(self) -> None:
        """Catches registration that re-previews changed callback bytes instead of enforcing approval."""
        mutations = ("bytes", "chmod", "replace", "symlink", "missing")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                callback = Path(self.temp.name) / f"callback-{mutation}"
                callback.write_bytes(b"#!/bin/sh\nexit 0\n")
                callback.chmod(0o700)
                registrar = self.registrar(callback)
                preview = registrar.preview(self.request)
                if mutation == "bytes":
                    with callback.open("r+b") as stream:
                        stream.seek(0)
                        stream.write(b"!")
                elif mutation == "chmod":
                    callback.chmod(0o744)
                elif mutation == "replace":
                    replacement = callback.with_name(callback.name + ".replacement")
                    replacement.write_bytes(b"#!/bin/sh\necho replaced\n")
                    replacement.chmod(0o700)
                    os.replace(replacement, callback)
                elif mutation == "symlink":
                    replacement = callback.with_name(callback.name + ".target")
                    replacement.write_bytes(b"#!/bin/sh\nexit 0\n")
                    replacement.chmod(0o700)
                    callback.unlink()
                    callback.symlink_to(replacement)
                else:
                    callback.unlink()
                with self.assertRaises(ProtocolRefusal):
                    registrar.register(self.request, preview["digest"])
                self.assertFalse(Path(preview["path"]).exists())

    def test_registration_revalidates_the_exact_snapshot_and_writes_private_identity_once(self) -> None:
        """Catches a plist whose registration identity is absent, current-path-derived, or self-inconsistent."""
        registrar = self.registrar()
        preview, plist_path = self._written(registrar)
        written = plistlib.loads(plist_path.read_bytes())
        identity = written["FloatiRegistrationIdentity"]

        self.assertEqual(preview["callback_identity"]["canonical_path"], identity["canonical_path"])
        self.assertEqual(preview["callback_identity"]["sha256"], identity["sha256"])
        self.assertEqual(preview["digest"], identity["semantic_preview_digest"])
        self.assertEqual(1, plist_path.read_bytes().count(b"<key>FloatiRegistrationIdentity</key>"))
        self.assertEqual(0o600, plist_path.stat().st_mode & 0o777)


class OneShotWakeHonestyTests(_OneShotWakePathTestCase):
    def test_status_reads_stored_identity_after_callback_replacement_or_loss(self) -> None:
        """Catches status reopening today's callback and relabeling it as old registration testimony."""
        registrar = self.registrar()
        preview, _ = self._written(registrar)
        registered = dict(preview["callback_identity"])
        replacement = self.callback.with_name("replacement")
        replacement.write_bytes(b"#!/bin/sh\necho replacement\n")
        replacement.chmod(0o700)
        os.replace(replacement, self.callback)

        status = OneShotWakeRegistrar(root=self.root, callback_path=self.callback).status(self.request)

        self.assertEqual("written_unloaded", status["path_state"])
        self.assertEqual(registered["sha256"], status["registered_callback_sha256"])
        self.assertNotEqual(hashlib.sha256(self.callback.read_bytes()).hexdigest(),
                            status["registered_callback_sha256"])
        self.callback.unlink()
        self.assertEqual(registered["inode"],
                         OneShotWakeRegistrar(root=self.root, callback_path=self.callback)
                         .status(self.request)["registered_callback_inode"])

    def test_status_refuses_forged_missing_duplicate_or_digest_mismatched_identity(self) -> None:
        """Catches malformed local registration testimony being reported as a truthful wake path."""
        for corruption in ("missing", "forged", "duplicate", "digest"):
            with self.subTest(corruption=corruption):
                registrar = self.registrar()
                _, plist_path = self._written(registrar)
                encoded = plist_path.read_bytes()
                if corruption == "duplicate":
                    encoded = encoded.replace(
                        b"<key>FloatiRegistrationIdentity</key>",
                        b"<key>FloatiRegistrationIdentity</key><dict></dict><key>FloatiRegistrationIdentity</key>",
                        1,
                    )
                else:
                    plist = plistlib.loads(encoded)
                    if corruption == "missing":
                        del plist["FloatiRegistrationIdentity"]
                    elif corruption == "forged":
                        plist["FloatiRegistrationIdentity"]["inode"] = "not-an-integer"
                    else:
                        plist["FloatiRegistrationIdentity"]["semantic_preview_digest"] = "0" * 64
                    encoded = plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)
                plist_path.write_bytes(encoded)
                with self.assertRaises(ProtocolRefusal) as caught:
                    registrar.status(self.request)
                self.assertEqual("wake_local_testimony_invalid", caught.exception.code)
                plist_path.unlink(missing_ok=True)

    def test_status_refuses_self_consistent_identity_and_request_drift(self) -> None:
        """Catches a recomputed semantic digest blessing callback or request testimony never registered."""
        mutations = {
            "callback_path": lambda plist: plist["FloatiRegistrationIdentity"].__setitem__(
                "canonical_path", "/forged-callback"
            ),
            "callback_overlong": lambda plist: plist["FloatiRegistrationIdentity"].__setitem__(
                "canonical_path", "/" + ("x" * 5000)
            ),
            "mode": lambda plist: plist["FloatiRegistrationIdentity"].__setitem__("mode", 0o600),
            "size": lambda plist: plist["FloatiRegistrationIdentity"].__setitem__("size", 0),
            "sha": lambda plist: plist["FloatiRegistrationIdentity"].__setitem__("sha256", "x" * 64),
            "arguments": lambda plist: plist["ProgramArguments"].__setitem__(
                plist["ProgramArguments"].index("--item-id") + 1,
                "work-018f7e9b3c117abc8def0123456789ac",
            ),
            "calendar": lambda plist: plist["StartCalendarInterval"].__setitem__("Second", 7),
            "identity_extra": lambda plist: plist["FloatiRegistrationIdentity"].__setitem__("extra", True),
            "identity_missing": lambda plist: plist["FloatiRegistrationIdentity"].pop("device"),
            "plist_extra": lambda plist: plist.__setitem__("KeepAlive", True),
            "plist_missing": lambda plist: plist.pop("StartCalendarInterval"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                registrar = self.registrar()
                _, plist_path = self._written(registrar)
                plist = plistlib.loads(plist_path.read_bytes())
                mutate(plist)
                if "FloatiRegistrationIdentity" in plist:
                    self._recompute_semantic_digest(plist)
                plist_path.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))

                try:
                    with self.assertRaises(ProtocolRefusal) as caught:
                        registrar.status(self.request)
                    self.assertEqual("wake_local_testimony_invalid", caught.exception.code)
                finally:
                    plist_path.unlink(missing_ok=True)

    def test_closed_status_artifacts_are_schema_valid_and_never_claim_activation(self) -> None:
        """Catches wake-path output that overstates a local plist as live, deliverable, or self-waking."""
        registrar = self.registrar()
        absent = registrar.status(self.request)
        preview, _ = self._written(registrar)
        written = registrar.status(self.request)
        schema = Path("schemas/v1/wake-path-status-artifact.schema.json")

        for artifact in (absent, written):
            validate_json_schema(artifact, schema)
            self.assertNotIn(artifact["is_live"], {"live", "dead"})
            self.assertNotIn(artifact["deliverability"], {"deliverable", "undeliverable"})
            self.assertNotEqual("will_self_wake", artifact["self_wake"])
            self.assertNotEqual("activation_unverified", artifact["path_state"])
        self.assertEqual("absent", absent["path_state"])
        self.assertEqual("written_unloaded", written["path_state"])
        self.assertEqual(hashlib.sha256(Path(preview["path"]).read_bytes()).hexdigest(), written["plist_digest"])

    def test_schema_refuses_every_nonproduction_status_vocabulary_substitution(self) -> None:
        """Catches schema/runtime drift that would admit an activation claim this registrar cannot prove."""
        registrar = self.registrar()
        _, _ = self._written(registrar)
        lawful = registrar.status(self.request)
        schema = Path("schemas/v1/wake-path-status-artifact.schema.json")
        substitutions = {
            "path_state": "activation_unverified",
            "is_live": "live",
            "deliverability": "deliverable",
            "self_wake": "will_self_wake",
            "reason": "activated",
        }
        for key, value in substitutions.items():
            with self.subTest(key=key):
                changed = dict(lawful)
                changed[key] = value
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(changed, schema)

    def test_cleanup_is_local_only_and_returns_no_disarm_claim(self) -> None:
        """Catches cleanup reporting a launchd disarm result instead of only removing its plist."""
        registrar = self.registrar()
        preview, plist_path = self._written(registrar)
        result = registrar.cleanup(str(preview["label"]))

        self.assertIsNone(result)
        self.assertFalse(plist_path.exists())
        self.assertEqual("absent", registrar.status(self.request)["path_state"])

    def test_cleanup_contract_limits_success_to_rename_observations(self) -> None:
        """Catches cleanup prose claiming durable control of same-UID quarantine pathname state."""
        cleanup_contract = OneShotWakeRegistrar.cleanup.__doc__ or ""

        self.assertIn("identity match at the rename observation", cleanup_contract)
        self.assertIn("no durable quarantine-retention", cleanup_contract)

    def test_cleanup_has_no_path_delete_call_and_status_remains_absent(self) -> None:
        """Catches cleanup deleting a quarantine pathname or returning non-absent path testimony."""
        source = (Path(__file__).resolve().parents[1] / "floati" / "wake.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        registrar_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "OneShotWakeRegistrar"
        )
        cleanup = next(
            node for node in registrar_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "cleanup"
        )
        path_delete_calls = [
            node for node in ast.walk(cleanup)
            if isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Attribute) and node.func.attr in {"unlink", "remove"})
                or (isinstance(node.func, ast.Name) and node.func.id in {"unlink", "remove"})
            )
        ]
        self.assertEqual([], path_delete_calls)

        registrar = self.registrar()
        preview, plist_path = self._written(registrar)
        self.assertIsNone(registrar.cleanup(str(preview["label"])))
        self.assertFalse(plist_path.exists())
        self.assertEqual("absent", registrar.status(self.request)["path_state"])

    def test_fresh_registrar_cleanup_validates_complete_testimony_before_unlink(self) -> None:
        """Catches cleanup deleting label-only or malformed local files that were never lawful registrations."""
        corruptions = {
            "label_only": lambda plist: {"Label": plist["Label"]},
            "unrelated": lambda plist: {
                "Label": plist["Label"], "ProgramArguments": ["/unrelated"],
                "StartCalendarInterval": {}, "FloatiRegistrationIdentity": {},
            },
            "extra": lambda plist: dict(plist, KeepAlive=True),
            "identity_digest": lambda plist: dict(
                plist,
                FloatiRegistrationIdentity=dict(
                    plist["FloatiRegistrationIdentity"], semantic_preview_digest="0" * 64,
                ),
            ),
        }
        for name, corrupt in corruptions.items():
            with self.subTest(name=name):
                registrar = self.registrar()
                preview = registrar.preview(self.request)
                plist_path = Path(preview["path"])
                plist_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                payload = corrupt(dict(preview["plist"]))
                encoded = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
                plist_path.write_bytes(encoded)
                fresh = OneShotWakeRegistrar(root=self.root, callback_path=self.callback)

                with self.assertRaises(ProtocolRefusal) as caught:
                    fresh.cleanup(str(preview["label"]))

                self.assertEqual("wake_local_testimony_invalid", caught.exception.code)
                self.assertEqual(encoded, plist_path.read_bytes())
                plist_path.unlink()

    def test_fresh_registrar_cleanup_accepts_the_exact_registered_plist(self) -> None:
        """Catches cleanup requiring ephemeral preview state instead of deterministic lawful testimony."""
        registrar = self.registrar()
        preview, plist_path = self._written(registrar)
        fresh = OneShotWakeRegistrar(root=self.root, callback_path=self.callback)
        real_same_file_identity = wake_module._same_file_identity
        identity_observations = []

        def observe_identity(opened: os.stat_result, quarantined: os.stat_result) -> bool:
            matches = real_same_file_identity(opened, quarantined)
            identity_observations.append(matches)
            quarantines = list(plist_path.parent.glob(f".{preview['label']}.cleanup-*"))
            self.assertEqual(1, len(quarantines))
            self.assertRegex(
                quarantines[0].name,
                rf"^\.{re.escape(str(preview['label']))}\.cleanup-[0-9a-f]{{32}}$",
            )
            self.assertLessEqual(len(os.fsencode(quarantines[0].name)), 255)
            return matches

        with mock.patch("floati.wake._same_file_identity", side_effect=observe_identity):
            result = fresh.cleanup(str(preview["label"]))

        self.assertIsNone(result)
        self.assertEqual([True], identity_observations)
        self.assertFalse(plist_path.exists())
        self.assertEqual("absent", fresh.status(self.request)["path_state"])
        self.assertIsNone(fresh.cleanup(str(preview["label"])))

    def test_fresh_status_and_cleanup_refuse_a_symlinked_deterministic_leaf(self) -> None:
        """Catches fresh observation or cleanup following the plist leaf to another local file."""
        registrar = self.registrar()
        preview, plist_path = self._written(registrar)
        target = plist_path.with_name("other.plist")
        os.replace(plist_path, target)
        target_bytes = target.read_bytes()
        plist_path.symlink_to(target.name)
        fresh = OneShotWakeRegistrar(root=self.root, callback_path=self.callback)

        with self.assertRaises(ProtocolRefusal) as status_refusal:
            fresh.status(self.request)
        with self.assertRaises(ProtocolRefusal) as cleanup_refusal:
            fresh.cleanup(str(preview["label"]))

        self.assertEqual("wake_local_testimony_invalid", status_refusal.exception.code)
        self.assertEqual("wake_local_testimony_invalid", cleanup_refusal.exception.code)
        self.assertTrue(plist_path.is_symlink())
        self.assertTrue(target.is_file())
        self.assertEqual(target_bytes, target.read_bytes())

    def test_cleanup_preserves_a_replacement_installed_after_validation(self) -> None:
        """Catches path unlink deleting bytes that were swapped in after the validated read."""
        registrar = self.registrar()
        preview, plist_path = self._written(registrar)
        replacement = plist_path.with_name("replacement.plist")
        replacement_bytes = b"replacement must survive\n"
        replacement.write_bytes(replacement_bytes)
        real_loads = plistlib.loads

        def replace_after_validation(encoded: bytes) -> dict:
            parsed = real_loads(encoded)
            os.replace(replacement, plist_path)
            return parsed

        fresh = OneShotWakeRegistrar(root=self.root, callback_path=self.callback)
        with mock.patch("floati.wake.plistlib.loads", side_effect=replace_after_validation):
            with self.assertRaises(ProtocolRefusal) as caught:
                fresh.cleanup(str(preview["label"]))

        self.assertEqual("wake_local_testimony_invalid", caught.exception.code)
        self.assertEqual(replacement_bytes, plist_path.read_bytes())

    def test_cleanup_does_not_delete_post_compare_replacement_or_return_a_claim(self) -> None:
        """Catches cleanup deleting a post-comparison replacement or claiming residue retention."""
        registrar = self.registrar()
        preview, plist_path = self._written(registrar)
        replacement = plist_path.with_name("post-compare-replacement.plist")
        replacement_bytes = b"post-compare replacement must survive\n"
        replacement.write_bytes(replacement_bytes)
        real_same_file_identity = wake_module._same_file_identity

        def replace_after_comparison(opened: os.stat_result, quarantined: os.stat_result) -> bool:
            matches = real_same_file_identity(opened, quarantined)
            quarantine = list(plist_path.parent.glob(f".{preview['label']}.cleanup-*"))
            self.assertEqual(1, len(quarantine))
            os.replace(replacement, quarantine[0])
            return matches

        fresh = OneShotWakeRegistrar(root=self.root, callback_path=self.callback)
        with mock.patch(
            "floati.wake._same_file_identity", side_effect=replace_after_comparison,
        ):
            result = fresh.cleanup(str(preview["label"]))

        self.assertIsNone(result)
        self.assertFalse(plist_path.exists())
        self.assertEqual("absent", fresh.status(self.request)["path_state"])
        quarantines = list(plist_path.parent.glob(f".{preview['label']}.cleanup-*"))
        self.assertEqual(1, len(quarantines))
        self.assertEqual(replacement_bytes, quarantines[0].read_bytes())

    def test_cleanup_refuses_and_preserves_a_new_label_created_after_rename(self) -> None:
        """Catches cleanup claiming success while a new deterministic registration leaf exists."""
        registrar = self.registrar()
        preview, plist_path = self._written(registrar)
        replacement_bytes = b"new deterministic leaf must survive\n"
        real_same_file_identity = wake_module._same_file_identity

        def replace_label_after_comparison(
            opened: os.stat_result, quarantined: os.stat_result,
        ) -> bool:
            matches = real_same_file_identity(opened, quarantined)
            plist_path.write_bytes(replacement_bytes)
            return matches

        fresh = OneShotWakeRegistrar(root=self.root, callback_path=self.callback)
        with mock.patch(
            "floati.wake._same_file_identity", side_effect=replace_label_after_comparison,
        ):
            with self.assertRaises(ProtocolRefusal) as caught:
                fresh.cleanup(str(preview["label"]))

        self.assertEqual("wake_local_testimony_invalid", caught.exception.code)
        self.assertEqual(replacement_bytes, plist_path.read_bytes())

    def test_cleanup_maps_post_validation_disappearance_to_a_typed_refusal(self) -> None:
        """Catches a post-validation disappearance escaping as raw FileNotFoundError."""
        registrar = self.registrar()
        preview, plist_path = self._written(registrar)
        real_loads = plistlib.loads

        def disappear_after_validation(encoded: bytes) -> dict:
            parsed = real_loads(encoded)
            plist_path.unlink()
            return parsed

        fresh = OneShotWakeRegistrar(root=self.root, callback_path=self.callback)
        with mock.patch("floati.wake.plistlib.loads", side_effect=disappear_after_validation):
            with self.assertRaises(ProtocolRefusal) as caught:
                fresh.cleanup(str(preview["label"]))

        self.assertEqual("wake_local_testimony_invalid", caught.exception.code)
        self.assertFalse(plist_path.exists())


class OneShotWakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        self.ledger, self.scheduler, self.attempt = _seed_open_attempt(self.root)
        self.request = OneShotWakeRequest(
            root=self.root, run_id=RUN_ID, item_id=ITEM_ID,
            attempt_id=self.attempt["attempt_id"], wake_at=WAKE_AT,
            scheduler_epoch=7, fence_token=self.attempt["fence_token"],
        )

    def test_preview_binds_every_callback_coordinate_and_calendar_plist(self) -> None:
        """Catches a plist that can wake a different root, attempt, epoch, or fence."""
        preview = OneShotWakeRegistrar(calendar_timezone=timezone.utc).preview(self.request)

        expected_arguments = [
            str(Path(__file__).resolve().parents[1] / "scripts" / "floati"),
            "wake-callback", "--root", str(self.root.path), "--tenant", "alpha",
            "--run-id", RUN_ID, "--item-id", ITEM_ID,
            "--attempt-id", self.attempt["attempt_id"], "--wake-at", WAKE_AT,
            "--scheduler-epoch", "7", "--fence-token", self.attempt["fence_token"],
        ]
        self.assertEqual(expected_arguments, preview["plist"]["ProgramArguments"])
        self.assertEqual({"Year": 2099, "Month": 1, "Day": 2, "Hour": 3, "Minute": 5, "Second": 6},
                         preview["plist"]["StartCalendarInterval"])
        expected_label = "com.landoclusters.floati.oneshot." + hashlib.sha256("\0".join((
            RETIRED_PRODUCT_NAME + "-one-shot-wake-v1",
            str(self.root.path), "alpha", RUN_ID, ITEM_ID,
            self.attempt["attempt_id"], WAKE_AT, "7", self.attempt["fence_token"],
        )).encode("utf-8")).hexdigest()
        self.assertEqual(expected_label, preview["label"])
        self.assertEqual(64, len(preview["digest"]))
        self.assertEqual("preview", preview["state"])

    def test_preview_converts_canonical_utc_wake_to_the_host_local_calendar(self) -> None:
        """Catches launchd interpreting canonical UTC calendar components as local wall time."""
        offset_request = OneShotWakeRequest(
            root=self.root, run_id=RUN_ID, item_id=ITEM_ID,
            attempt_id=self.attempt["attempt_id"], wake_at="2099-01-01T22:05:06.000-05:00",
            scheduler_epoch=7, fence_token=self.attempt["fence_token"],
        )
        registrar = OneShotWakeRegistrar(
            calendar_timezone=timezone(timedelta(hours=-5)),
        )

        preview = registrar.preview(offset_request)

        self.assertEqual(WAKE_AT, offset_request.wake_at)
        self.assertEqual(
            {"Year": 2099, "Month": 1, "Day": 1, "Hour": 22, "Minute": 5, "Second": 6},
            preview["plist"]["StartCalendarInterval"],
        )
        arguments = preview["plist"]["ProgramArguments"]
        self.assertEqual(WAKE_AT, arguments[arguments.index("--wake-at") + 1])
        canonical_local_preview = registrar.preview(self.request)
        self.assertEqual(canonical_local_preview["digest"], preview["digest"])
        utc_preview = OneShotWakeRegistrar(calendar_timezone=timezone.utc).preview(self.request)
        self.assertEqual(utc_preview["label"], preview["label"])

        registrar.register(offset_request, preview["digest"])
        written = plistlib.loads(Path(preview["path"]).read_bytes())
        self.assertEqual(
            preview["plist"]["StartCalendarInterval"],
            written["StartCalendarInterval"],
        )

    def test_register_requires_the_exact_preview_digest_before_writing_a_mode_0600_plist(self) -> None:
        """Catches installation without a repeated preview digest or private plist mode."""
        registrar = OneShotWakeRegistrar()
        preview = registrar.preview(self.request)
        plist_path = Path(preview["path"])
        before = self.ledger.records()

        with self.assertRaises(ProtocolRefusal) as caught:
            registrar.register(self.request, "0" * 64)
        self.assertEqual("wake_preview_unapproved", caught.exception.code)
        self.assertFalse(plist_path.exists())
        self.assertEqual(before, self.ledger.records())

        installed = registrar.register(self.request, preview["digest"])
        self.assertEqual("written_unloaded", installed["state"])
        self.assertEqual(preview["label"], installed["label"])
        self.assertEqual(0o600, plist_path.stat().st_mode & 0o777)
        self.assertEqual(before, self.ledger.records())

    def test_register_rechecks_future_time_at_the_persistence_boundary(self) -> None:
        """Catches a previewed wake becoming due before register still being written as a future wake."""
        clock = [datetime(2099, 1, 2, 3, 4, 5, tzinfo=timezone.utc)]
        registrar = OneShotWakeRegistrar(now=lambda: clock[0])
        preview = registrar.preview(self.request)
        clock[0] = datetime(2099, 1, 2, 3, 5, 6, tzinfo=timezone.utc)

        with self.assertRaises(ProtocolRefusal) as caught:
            registrar.register(self.request, preview["digest"])
        self.assertEqual("wake_not_future", caught.exception.code)
        self.assertFalse(Path(preview["path"]).exists())

    def test_request_refuses_a_timestamp_that_is_not_in_the_future(self) -> None:
        """Catches a registrar that writes a launchd job for a due or past wall-clock time."""
        past = OneShotWakeRequest(
            root=self.root, run_id=RUN_ID, item_id=ITEM_ID,
            attempt_id=self.attempt["attempt_id"], wake_at=NOW,
            scheduler_epoch=7, fence_token=self.attempt["fence_token"],
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            OneShotWakeRegistrar(now=lambda: datetime(2099, 1, 2, 3, 4, 5, tzinfo=timezone.utc)).preview(past)
        self.assertEqual("wake_not_future", caught.exception.code)

    def test_callback_refuses_a_stale_fence_after_replaying_current_run_truth(self) -> None:
        """Catches a callback accepting a once-valid attempt after its real successor opened."""
        decision = self.ledger.append({
            "schema_version": 0, "id": "run-dispatch-decision-" + uuid7_hex(),
            "tenant_id": "alpha", "timestamp": NOW, "kind": "dispatch_decision",
            "run_id": RUN_ID, "item_id": ITEM_ID, "attempt_id": self.attempt["attempt_id"],
            "eligible_workers": ["worker-a"], "chosen_worker": "worker-a", "capability_digest": DIGEST,
            "reason_code": "policy.route", "policy_digest": DIGEST, "routing_rank": 0, "scheduler_epoch": 7,
        })
        self.scheduler.start_attempt(RUN_ID, ITEM_ID, self.attempt["attempt_id"], decision["id"], now=NOW)
        self.scheduler.terminal_attempt(RUN_ID, ITEM_ID, self.attempt["attempt_id"], "failed",
                                        "transient", "transient_failure", "idempotent", now=NOW)
        successor = self.scheduler.open_attempt(RUN_ID, ITEM_ID, RetryPolicy(2, 100, 1000), 7, now=NOW)
        before = self.ledger.records()

        with self.assertRaises(ProtocolRefusal) as caught:
            replay_one_shot_wake(self.request, self.scheduler, now=datetime(2099, 1, 2, 3, 5, 6, tzinfo=timezone.utc))
        self.assertEqual("wake_stale_fence", caught.exception.code)
        self.assertNotEqual(self.attempt["attempt_id"], successor["attempt_id"])
        self.assertEqual(before, self.ledger.records())

    def test_callback_refuses_early_then_delegates_to_the_ordinary_scheduler(self) -> None:
        """Catches an early wake claim or a callback that bypasses scheduler reconciliation."""
        before = self.ledger.records()
        with self.assertRaises(ProtocolRefusal) as caught:
            replay_one_shot_wake(self.request, self.scheduler,
                                 now=datetime(2099, 1, 2, 3, 5, 5, tzinfo=timezone.utc))
        self.assertEqual("wake_not_due", caught.exception.code)
        self.assertEqual(before, self.ledger.records())

        result = replay_one_shot_wake(self.request, self.scheduler,
                                      now=datetime(2099, 1, 2, 3, 5, 6, tzinfo=timezone.utc))
        self.assertEqual({"state": "replayed", "records": []}, result)
        self.assertEqual(before, self.ledger.records())

    def test_cleanup_after_firing_removes_only_host_local_testimony(self) -> None:
        """Catches a fired wake that leaves its plist behind or writes a durable cleanup record."""
        registrar = OneShotWakeRegistrar()
        preview = registrar.preview(self.request)
        registrar.register(self.request, preview["digest"])
        before = self.ledger.records()
        replay_one_shot_wake(self.request, self.scheduler,
                             now=datetime(2099, 1, 2, 3, 5, 6, tzinfo=timezone.utc))

        registrar.cleanup(preview["label"])

        self.assertFalse(Path(preview["path"]).exists())
        self.assertEqual(before, self.ledger.records())

    def test_fresh_registrar_cleans_the_deterministic_local_path(self) -> None:
        """Catches cleanup that relies on a dead registrar's in-memory preview map."""
        registrar = OneShotWakeRegistrar()
        preview = registrar.preview(self.request)
        registrar.register(self.request, preview["digest"])

        OneShotWakeRegistrar(root=self.root).cleanup(preview["label"])

        self.assertFalse(Path(preview["path"]).exists())

    def test_installed_script_callback_replays_and_fresh_process_cleans_only_its_plist(self) -> None:
        """Catches a launchd callback command that cannot reconstruct its tenant/root or clean after firing."""
        request = OneShotWakeRequest(
            root=self.root, run_id=RUN_ID, item_id=ITEM_ID,
            attempt_id=self.attempt["attempt_id"], wake_at=FIRED_AT,
            scheduler_epoch=7, fence_token=self.attempt["fence_token"],
        )
        registrar = OneShotWakeRegistrar(now=lambda: datetime(1999, 1, 2, tzinfo=timezone.utc))
        preview = registrar.preview(request)
        registrar.register(request, preview["digest"])
        plist_path = Path(preview["path"])
        unrelated = plist_path.parent / "unrelated.plist"
        unrelated.write_text("unrelated host testimony\n", encoding="utf-8")
        before = self.ledger.records()

        result = subprocess.run([
            str(Path(__file__).resolve().parents[1] / "scripts" / "floati"),
            "wake-callback", "--root", str(self.root.path), "--tenant", "alpha",
            "--run-id", RUN_ID, "--item-id", ITEM_ID, "--attempt-id", self.attempt["attempt_id"],
            "--wake-at", FIRED_AT, "--scheduler-epoch", "7", "--fence-token", self.attempt["fence_token"],
        ], cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=False)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        artifact = json.loads(result.stdout)
        self.assertEqual("ok", artifact["status"])
        self.assertEqual({"state": "replayed", "records": []}, artifact["evidence"])
        self.assertFalse(plist_path.exists())
        self.assertEqual("unrelated host testimony\n", unrelated.read_text(encoding="utf-8"))
        self.assertEqual(before, self.ledger.records())

    def test_refused_script_callback_cleans_its_plist_without_masking_the_stale_fence(self) -> None:
        """Catches a fired stale callback leaving local testimony or replacing its replay refusal."""
        decision = self.ledger.append({
            "schema_version": 0, "id": "run-dispatch-decision-" + uuid7_hex(),
            "tenant_id": "alpha", "timestamp": NOW, "kind": "dispatch_decision",
            "run_id": RUN_ID, "item_id": ITEM_ID, "attempt_id": self.attempt["attempt_id"],
            "eligible_workers": ["worker-a"], "chosen_worker": "worker-a", "capability_digest": DIGEST,
            "reason_code": "policy.route", "policy_digest": DIGEST, "routing_rank": 0, "scheduler_epoch": 7,
        })
        self.scheduler.start_attempt(RUN_ID, ITEM_ID, self.attempt["attempt_id"], decision["id"], now=NOW)
        self.scheduler.terminal_attempt(RUN_ID, ITEM_ID, self.attempt["attempt_id"], "failed",
                                        "transient", "transient_failure", "idempotent", now=NOW)
        successor = self.scheduler.open_attempt(RUN_ID, ITEM_ID, RetryPolicy(2, 100, 1000), 7, now=NOW)
        request = OneShotWakeRequest(
            root=self.root, run_id=RUN_ID, item_id=ITEM_ID,
            attempt_id=self.attempt["attempt_id"], wake_at=FIRED_AT,
            scheduler_epoch=7, fence_token=self.attempt["fence_token"],
        )
        registrar = OneShotWakeRegistrar(now=lambda: datetime(1999, 1, 2, tzinfo=timezone.utc))
        preview = registrar.preview(request)
        registrar.register(request, preview["digest"])
        plist_path = Path(preview["path"])
        unrelated = plist_path.parent / "unrelated-stale.plist"
        unrelated.write_text("unrelated stale host testimony\n", encoding="utf-8")
        before = self.ledger.records()

        result = subprocess.run([
            str(Path(__file__).resolve().parents[1] / "scripts" / "floati"),
            "wake-callback", "--root", str(self.root.path), "--tenant", "alpha",
            "--run-id", RUN_ID, "--item-id", ITEM_ID, "--attempt-id", self.attempt["attempt_id"],
            "--wake-at", FIRED_AT, "--scheduler-epoch", "7", "--fence-token", self.attempt["fence_token"],
        ], cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=False)

        self.assertNotEqual(self.attempt["attempt_id"], successor["attempt_id"])
        self.assertEqual(20, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        artifact = json.loads(result.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("wake_stale_fence", artifact["evidence"]["code"])
        self.assertFalse(plist_path.exists())
        self.assertEqual("unrelated stale host testimony\n", unrelated.read_text(encoding="utf-8"))
        self.assertEqual(before, self.ledger.records())

    def test_fake_registrar_is_deterministic_and_never_claims_installation(self) -> None:
        """Catches a test double that hides host activation or gives changing wake previews."""
        registrar = FakeOneShotWakeRegistrar()
        first = registrar.preview(self.request)
        second = registrar.preview(self.request)

        self.assertEqual(first, second)
        self.assertEqual({"state": "written_unloaded", "label": first["label"], "digest": first["digest"]},
                         registrar.register(self.request, first["digest"]))
        self.assertEqual([first["label"]], registrar.registered_labels)
        registrar.cleanup(first["label"])
        self.assertEqual([], registrar.registered_labels)

    def test_wake_boundary_contains_no_sleep_poll_tcp_or_durable_wake_record(self) -> None:
        """Catches a resident/networked wake implementation or a new ledger record family."""
        source = (Path(__file__).resolve().parents[1] / "floati" / "wake.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        call_names = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        imported_modules = {
            alias.name.split(".")[0] for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }

        self.assertNotIn("sleep", call_names)
        self.assertNotIn("socket", imported_modules)
        self.assertEqual([], [node for node in ast.walk(tree) if isinstance(node, ast.While)])
        self.assertNotIn("wake_record", source)
        self.assertNotIn("ledger.append", source)


if __name__ == "__main__":
    unittest.main()
