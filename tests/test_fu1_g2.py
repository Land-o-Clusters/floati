"""G2 verified shared-install and waiter-rebinding coverage."""

from __future__ import annotations

import unittest
import atexit
import json
import hashlib
import inspect
import multiprocessing
import os
import shutil
import subprocess
import threading
from unittest import mock
from pathlib import Path
import tempfile

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.fleet_update_receipts import FleetUpdateReceiptLedger, authenticate_plan
from floati.jsonl import append_record
from floati.records import validate_record
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_fu1_g0 import REPOSITORY_ROOT, _tree_bytes
from tests.test_fu1_g1 import FU1G1Tests


_CANONICAL_EXECUTABLE: str | None = None


def canonical_executable() -> str:
    """One real, non-symlinked, already-resolved executable file for the G2 seam.

    `_explicit_executable` (floati/fleet_update.py:794) accepts exactly ONE
    caller-supplied executable and refuses anything that is not absolute, is a
    symlink, is not a regular file, does not resolve to itself, or is not
    executable — fleet updates never resolve PATH.

    These fixtures used to name `/usr/bin/python3`. That is a real file on
    macOS and a SYMLINK to a versioned interpreter on Debian and Ubuntu, so on
    the ubuntu runner the product refused it exactly as designed, and fifty
    tests in this module plus one in test_fu1_g4 died at the fixture's own
    first argument:

      ProtocolRefusal: fleet_update_installer_invalid:
        executable must be an explicit canonical executable

    ⇒ A FIXTURE THAT NAMES A SYSTEM PATH IS ASSERTING A HOST FACT, NOT THE
    CONTRACT IT MEANT TO EXERCISE.

    The predicate is right and is not touched (see ruling R-C: the product is
    right about symlinked system binaries). The fixture MAKES what the seam
    requires instead of hoping the host has one lying around: a regular,
    executable file under the platform's real temporary root, resolved at
    creation so `path.resolve(strict=True) == path` holds on a host whose temp
    root is reached through a symlink.

    It is deliberately NOT named `floati`: the installer's parent directory
    becomes a PATH component for the deployment writer's installer-shadow
    enumeration, and a file with that name there would be a shadow.

    One per process, shared by every fixture that needs it, removed at exit.
    """

    global _CANONICAL_EXECUTABLE
    if _CANONICAL_EXECUTABLE is None:
        directory = Path(
            tempfile.mkdtemp(prefix="fu1-g2-canonical-", dir=REAL_TEMP_ROOT)
        ).resolve(strict=True)
        atexit.register(shutil.rmtree, str(directory), True)
        executable = directory / "installer"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        _CANONICAL_EXECUTABLE = str(executable)
    return _CANONICAL_EXECUTABLE


def _release_inherited_fleet_update_guard(guard: object, sender: object) -> None:
    """Report whether a fork child could release its parent's guard."""

    try:
        guard.release()
    except ProtocolRefusal as exc:
        outcome = ("refused", exc.code)
    except BaseException as exc:
        outcome = ("error", type(exc).__name__)
    else:
        outcome = ("ok", None)
    try:
        sender.send(outcome)
    finally:
        sender.close()


def _attempt_fleet_update_guard(root_path: str, sender: object) -> None:
    """Acquire one root guard from a fresh, non-inheriting process."""

    from floati.fleet_update_receipts import _FleetUpdateExecutionGuard
    from floati.root import FloatiRoot

    try:
        root = FloatiRoot.open_direct_home(Path(root_path), create=False)
        with _FleetUpdateExecutionGuard(root):
            outcome = ("ok", None)
    except ProtocolRefusal as exc:
        outcome = ("refused", exc.code)
    except BaseException as exc:
        outcome = ("error", type(exc).__name__)
    try:
        sender.send(outcome)
    finally:
        sender.close()


class FU1G2Tests(FU1G1Tests):
    def _guard_process_outcome(
        self, context: object, target: object, *args: object
    ) -> tuple[str, str | None]:
        receiving, sending = context.Pipe(duplex=False)
        process = context.Process(target=target, args=(*args, sending))
        process.start()
        sending.close()
        try:
            self.assertTrue(
                receiving.poll(10), "guard probe process produced no result"
            )
            outcome = receiving.recv()
        finally:
            receiving.close()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(10)
        self.assertEqual(0, process.exitcode)
        return outcome

    @staticmethod
    def _rehash_plan(plan: dict[str, object]) -> dict[str, object]:
        """Recompute the public plan hash after one hostile semantic mutation."""

        changed = json.loads(json.dumps(plan))
        body = {
            key: value
            for key, value in changed.items()
            if key not in {"plan_digest", "apply_argv"}
        }
        changed["plan_digest"] = hashlib.sha256(
            json.dumps(
                body,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        return changed

    def _forge_owner_review_fact(
        self, plan: dict[str, object], *, field: str, value: object
    ) -> dict[str, object]:
        """Keep every declared consequence formula coherent around one false fact."""

        from floati.fleet_update_receipts import owner_review_batch_digest

        changed = json.loads(json.dumps(plan))
        consequence = changed["seat_binding_consequences"][0]
        consequence[field] = value
        rotated = consequence["current_hook_hash"] != consequence["target_hook_hash"]
        review = consequence["target_hook_hash"] != consequence["observed_trusted_hash"]
        enable = consequence["observed_enabled"] is not True
        relaunch = rotated or review or enable
        remedies = []
        if enable:
            remedies.append("enable the exact Stop hook in Codex settings")
        if review:
            remedies.append("review and trust the exact Stop hook in Codex settings")
        if relaunch:
            remedies.append("relaunch the affected session")
        consequence.update(
            trust_rotated_by_update=rotated,
            review_required_after_update=review,
            enable_required_after_update=enable,
            relaunch_required_after_update=relaunch,
            reachability_after_update=(
                "unknown_until_review_and_relaunch" if relaunch else "not_observed"
            ),
            remedy=";".join(remedies) if remedies else None,
        )
        changed["owner_review_batch_digest"] = owner_review_batch_digest(
            changed["reader_consequences"],
            changed["seat_binding_consequences"],
            changed["seat_exclusions"],
        )
        return self._rehash_plan(changed)

    def _prepare_real_g2_plan(
        self, *, trust_state: bytes | None = None
    ) -> dict[str, object]:
        """Build one real detached source whose ignored manifest names its HEAD.

        The manifest is deliberately excluded through Git's repository-local
        exclude file.  That avoids a self-referential commit while keeping
        both explicit-Git cleanliness checks and the governed writer real.
        """

        manifest = self.target_source / "bundle-manifest.v0.json"
        manifest.unlink()
        # The inherited fixture carries an unowned LICENSE convenience copy.
        # Remove it so this real update proves an authentic journal ``create``
        # path instead of colliding with foreign bytes.
        (self.destination / "LICENSE").unlink()
        created = self.target_source / "floati" / "fu1_created.py"
        created.write_bytes(b"CREATED_BY_FU1 = True\n")
        nested_created = self.target_source / "floati" / "fu1_new" / "created.py"
        nested_created.parent.mkdir()
        nested_created.write_bytes(b"NESTED_CREATED_BY_FU1 = True\n")
        for argv in (
            ("/usr/bin/git", "init", "--quiet"),
            ("/usr/bin/git", "config", "user.name", "Floati Test"),
            ("/usr/bin/git", "config", "user.email", "floati-test@example.invalid"),
        ):
            subprocess.run(argv, cwd=self.target_source, check=True)
        (self.target_source / ".git" / "info" / "exclude").write_text(
            "/bundle-manifest.v0.json\n", encoding="utf-8"
        )
        subprocess.run(("/usr/bin/git", "add", "."), cwd=self.target_source, check=True)
        subprocess.run(
            ("/usr/bin/git", "commit", "--quiet", "-m", "target fixture"),
            cwd=self.target_source,
            check=True,
        )
        subprocess.run(
            ("/usr/bin/git", "checkout", "--quiet", "--detach"),
            cwd=self.target_source,
            check=True,
        )
        target_sha = subprocess.run(
            ("/usr/bin/git", "rev-parse", "HEAD"),
            cwd=self.target_source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self._write_manifest(self.target_source, target_sha)
        manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
        for relative in (
            "LICENSE",
            "floati/fu1_created.py",
            "floati/fu1_new/created.py",
        ):
            payload = self.target_source / relative
            manifest_document["files"].append({
                "path": relative,
                "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
            })
        manifest_document["files"].sort(key=lambda row: row["path"])
        manifest.write_text(
            json.dumps(manifest_document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        status = subprocess.run(
            ("/usr/bin/git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=self.target_source,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual("", status.stdout)

        trust = self.hooks_path.with_name("config.toml")
        trust.write_bytes(
            b"# fixture trust state must remain byte-identical\n"
            if trust_state is None
            else trust_state
        )
        from floati.fleet_update import preview_fleet_update

        plan = preview_fleet_update(
            root=self.root,
            actor=self.actor,
            destination=self.destination,
            target_source=self.target_source,
            target_source_sha=target_sha,
            channel=self.channel,
            version=self.version,
            binding_path=self.binding_path,
            transport_registry=self.registry_path,
            transport_name=self.transport,
        )
        self._consent()
        return plan

    def _commit_real_g2(
        self,
        plan: dict[str, object],
        key: str,
        fault_hook: object = None,
    ) -> dict[str, object]:
        from floati.fleet_update import commit_fleet_update_g2

        self.assertIn(
            "_fault_hook",
            inspect.signature(commit_fleet_update_g2).parameters,
            "G2 needs one explicit default-off internal crash seam for production-path recovery tests",
        )
        return commit_fleet_update_g2(
            plan=plan,
            root=self.root,
            actor=self.actor,
            idempotency_key=key,
            target_source=self.target_source,
            installer_executable=canonical_executable(),
            git_executable="/usr/bin/git",
            _fault_hook=fault_hook,
        )

    def _prepare_two_binding_post_pre_saga(
        self, key: str
    ) -> tuple[dict[str, object], str]:
        """Crash after binding zero reaches post while binding one remains pre."""

        self._two_by_two()
        plan = self._prepare_real_g2_plan()
        tripped = False

        def interrupt_first_waiter(event: str) -> None:
            nonlocal tripped
            if event == "after_waiter_trust_observation" and not tripped:
                tripped = True
                raise OSError("fixture first waiter response loss")

        with self.assertRaisesRegex(OSError, "fixture first waiter response loss"):
            self._commit_real_g2(plan, key, interrupt_first_waiter)
        self.assertTrue(tripped)
        rows = self._related_g2_rows(key)
        self.assertEqual(
            ["fleet_update_started", "fleet_update_step"],
            [row["kind"] for row in rows],
        )
        for index, binding in enumerate(plan["waiter_bindings"]):
            observed = hashlib.sha256(
                Path(str(binding["configuration"])).read_bytes()
            ).hexdigest()
            self.assertEqual(
                binding[
                    "target_configuration_sha256"
                    if index == 0
                    else "configuration_sha256"
                ],
                observed,
            )
        return plan, key

    def _related_g2_rows(self, key: str) -> list[dict[str, object]]:
        return [
            row
            for row in FleetUpdateReceiptLedger(self.root).rows(self.actor)
            if row.get("idempotency_key") == key
        ]

    def _assert_real_g2_chain(
        self,
        plan: dict[str, object],
        key: str,
        *,
        shared_disposition: str,
        waiter_disposition: str,
    ) -> list[dict[str, object]]:
        """Pin exact physical receipt and journal formulas after recovery."""

        from floati import wiring_journal
        from floati.fleet_update import _shared_install_join_id

        rows = self._related_g2_rows(key)
        self.assertEqual(
            ["fleet_update_started", "fleet_update_step", "fleet_update_step"],
            [row["kind"] for row in rows],
        )
        self.assertEqual([None, 1, 2], [row["step_ordinal"] for row in rows])
        self.assertEqual(
            [None, rows[0]["id"], rows[1]["id"]],
            [row["predecessor_receipt_id"] for row in rows],
        )
        self.assertEqual(len(rows), len({row["id"] for row in rows}))

        shared, waiter = rows[1:]
        self.assertEqual("shared_install", shared["step_kind"])
        self.assertEqual(str(plan["current_manifest_sha256"]), shared["pre_digest"])
        self.assertEqual(str(plan["target_manifest_sha256"]), shared["post_digest"])
        self.assertEqual(shared_disposition, shared["commit_disposition"])
        self.assertEqual(
            {
                "kind": "shared_install",
                "destination": str(self.destination),
                "metadata_relative": ".floati-install/manifest.v0.json",
            },
            shared["step_coordinate"],
        )
        join_id = _shared_install_join_id(str(plan["plan_digest"]), self.actor, key)
        self.assertEqual(join_id, shared["step_evidence"]["join_id"])
        joined = [
            entry
            for entry in wiring_journal.read_entries(wiring_journal.journal_path(self.destination))
            if entry.payload.get("join_id") == join_id
        ]
        target_manifest = json.loads(
            (self.target_source / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(target_manifest["files"]) + 1, len(joined))
        self.assertEqual(
            list(range(joined[0].ordinal, joined[-1].ordinal + 1)),
            [entry.ordinal for entry in joined],
        )
        self.assertEqual(
            [entry.payload["entryHash"] for entry in joined],
            shared["step_evidence"]["entry_hashes"],
        )
        self.assertEqual(
            plan["shared_install_intents"],
            [
                {
                    "kind": entry.payload["kind"],
                    "op": entry.payload["op"],
                    "path": entry.payload["path"],
                    "sha256": entry.payload["sha256"],
                }
                for entry in joined
            ],
        )
        # The terminal metadata is now post-state, so pin create operations
        # from the authenticated journal itself instead of inferring them
        # from the current inventory.
        self.assertEqual(
            {
                "LICENSE",
                "floati/fu1_created.py",
                "floati/fu1_new/created.py",
            },
            {
                Path(str(entry.payload["path"])).relative_to(self.destination).as_posix()
                for entry in joined[:-1]
                if entry.payload["op"] == "create"
            },
        )

        binding = plan["waiter_bindings"][0]
        self.assertEqual("waiter_binding", waiter["step_kind"])
        self.assertEqual(binding["configuration_sha256"], waiter["pre_digest"])
        self.assertEqual(binding["target_configuration_sha256"], waiter["post_digest"])
        self.assertEqual(waiter_disposition, waiter["commit_disposition"])
        self.assertEqual(
            {
                "kind": "waiter_binding",
                "index": 0,
                "configuration": str(self.hooks_path),
                "store": str(self.waiter_store),
                "trust_key": None,
            },
            waiter["step_coordinate"],
        )
        observation = waiter["step_evidence"]["hook_post_observation"]
        self.assertNotIn("hook_armed", observation)
        return rows

    def _exercise_real_shared_fault(self, event: str) -> None:
        """Crash one real shared-writer/receipt seam and resume the same saga."""

        key = "fu1-g2-real-shared-" + event.replace("_", "-")
        plan = self._prepare_real_g2_plan()
        target_manifest = json.loads(
            (self.target_source / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )
        target_files = target_manifest["files"]
        previous_files = {
            row["path"]: row["sha256"]
            for row in json.loads(
                (self.destination / ".floati-install" / "manifest.v0.json").read_text(
                    encoding="utf-8"
                )
            )["files"]
        }
        changed_or_created = sum(
            previous_files.get(row["path"]) != row["sha256"] for row in target_files
        )
        hooks_before = self.hooks_path.read_bytes()
        old_waiter_before = _tree_bytes(self.current_waiter)
        trust_path = self.hooks_path.with_name("config.toml")
        trust_before = trust_path.read_bytes()
        events: list[str] = []
        tripped = False

        def inject(observed: str) -> None:
            nonlocal tripped
            events.append(observed)
            if observed == event and not tripped:
                tripped = True
                raise OSError("fixture shared interruption at " + event)

        with self.assertRaisesRegex(OSError, "fixture shared interruption"):
            self._commit_real_g2(plan, key, inject)
        self.assertTrue(tripped, f"production path did not reach {event}")
        rows_before_retry = self._related_g2_rows(key)
        self.assertEqual(
            2 if event == "after_shared_step_receipt" else 1,
            len(rows_before_retry),
        )
        self.assertEqual(hooks_before, self.hooks_path.read_bytes())
        from floati.fleet_update import _shared_install_join_id, _shared_install_recovery_evidence
        join_id = _shared_install_join_id(str(plan["plan_digest"]), self.actor, key)
        recovery_before_retry = _shared_install_recovery_evidence(
            self.destination,
            self.target_source,
            str(plan["current_manifest_sha256"]),
            str(plan["target_manifest_sha256"]),
            join_id,
            plan["shared_install_intents"],
        )
        from floati import wiring_journal
        joined_before_retry = [
            entry
            for entry in wiring_journal.read_entries(
                wiring_journal.journal_path(self.destination)
            )
            if entry.payload.get("join_id") == join_id
        ]
        self.assertEqual(
            1 if event.startswith("after_file_") else len(target_files) + 1,
            len(joined_before_retry),
        )
        self.assertEqual(
            list(range(joined_before_retry[0].ordinal, joined_before_retry[-1].ordinal + 1)),
            [entry.ordinal for entry in joined_before_retry],
        )
        first_target = Path(str(joined_before_retry[0].payload["path"]))
        if event in {"after_file_journal", "after_file_file_fsync"}:
            self.assertFalse(first_target.exists())
        else:
            self.assertEqual(
                str(joined_before_retry[0].payload["sha256"]),
                hashlib.sha256(first_target.read_bytes()).hexdigest(),
            )
        self.assertEqual(
            [],
            [
                path.relative_to(self.destination).as_posix()
                for path in self.destination.rglob(".*.floati-*")
            ],
        )
        self.assertEqual(
            "post" if event in {
                "after_metadata_replace", "after_metadata_directory_fsync",
                "after_metadata_readback", "after_shared_commit_readback",
                "after_shared_step_receipt",
            } else "partial",
            recovery_before_retry["phase"],
        )
        receipt_bytes_before_retry = (
            self.root.path / FleetUpdateReceiptLedger.relative(self.actor)
        ).read_bytes()
        result = self._commit_real_g2(plan, key, inject)

        post_without_second_run = event in {
            "after_metadata_replace",
            "after_metadata_directory_fsync",
            "after_metadata_readback",
            "after_shared_commit_readback",
            "after_shared_step_receipt",
        }
        self.assertEqual(1 if post_without_second_run else 2, events.count("before_shared_writer_run"))
        self.assertEqual(
            changed_or_created if event.startswith("after_file_") else len(target_files),
            events.count("after_file_replace"),
        )
        self.assertEqual(1, events.count("after_metadata_replace"))
        self.assertEqual(hooks_before.split(b'"Stop"', 1)[0], self.hooks_path.read_bytes().split(b'"Stop"', 1)[0])
        self.assertEqual(old_waiter_before, _tree_bytes(self.current_waiter))
        self.assertEqual(trust_before, trust_path.read_bytes())
        self.assertEqual(
            [],
            [
                path.relative_to(self.destination).as_posix()
                for path in self.destination.rglob(".*.floati-*")
            ],
        )
        self.assertEqual("g2_committed_pending_later_gates", result["state"])
        rows = self._assert_real_g2_chain(
            plan,
            key,
            shared_disposition=(
                "recovered_post_state"
                if post_without_second_run and event != "after_shared_step_receipt"
                else "applied"
            ),
            waiter_disposition="applied",
        )
        if event == "after_shared_step_receipt":
            self.assertEqual([row["id"] for row in rows_before_retry], [row["id"] for row in rows[:2]])
            self.assertEqual(receipt_bytes_before_retry, (
                self.root.path / FleetUpdateReceiptLedger.relative(self.actor)
            ).read_bytes()[:len(receipt_bytes_before_retry)])

    def _exercise_real_waiter_fault(self, event: str) -> None:
        """Crash one real waiter atomic/receipt seam and prove exact recovery."""

        from floati.codex_hook_install import plan_waiter_rebind

        key = "fu1-g2-real-waiter-" + event.replace("_", "-")
        plan = self._prepare_real_g2_plan()
        before = self.hooks_path.read_bytes()
        target = plan_waiter_rebind(
            self.hooks_path, self.waiter_store, str(plan["target_waiter_digest"])
        )["after"]
        old_waiter_before = _tree_bytes(self.current_waiter)
        trust_path = self.hooks_path.with_name("config.toml")
        trust_before = trust_path.read_bytes()
        events: list[str] = []
        tripped = False

        def inject(observed: str) -> None:
            nonlocal tripped
            events.append(observed)
            if observed == event and not tripped:
                tripped = True
                raise OSError("fixture waiter interruption at " + event)

        with self.assertRaisesRegex(OSError, "fixture waiter interruption"):
            self._commit_real_g2(plan, key, inject)
        self.assertTrue(tripped, f"production path did not reach {event}")
        pre_replace_events = {
            "before_waiter_generation_replace",
            "after_waiter_generation_replace", "after_waiter_generation_store_fsync",
            "after_waiter_generation_readback",
            "before_waiter_temp_create", "before_waiter_write",
            "before_waiter_file_fsync", "before_waiter_replace",
        }
        self.assertEqual(before if event in pre_replace_events else target, self.hooks_path.read_bytes())
        waiter_rows_before = [
            row for row in self._related_g2_rows(key)
            if row.get("step_kind") == "waiter_binding"
        ]
        self.assertEqual(
            3 if event == "after_waiter_step_receipt" else 2,
            len(self._related_g2_rows(key)),
        )
        target_tree = self.waiter_store / str(plan["target_waiter_digest"])
        target_absent_frontier = event == "before_waiter_generation_replace"
        self.assertEqual(not target_absent_frontier, target_tree.is_dir())
        self.assertFalse(
            any(
                path.name.startswith(".floati-fleet-stage-")
                for path in self.waiter_store.iterdir()
            )
        )
        if target_tree.is_dir():
            from floati.waiter_bundle import waiter_runtime_digest
            self.assertEqual(
                str(plan["target_waiter_digest"]),
                waiter_runtime_digest(target_tree),
            )
        target_inode = target_tree.stat().st_ino if target_tree.is_dir() else None
        hook_temps = tuple(self.hooks_path.parent.glob(f".{self.hooks_path.name}.floati-*"))
        self.assertEqual((), hook_temps)
        receipt_bytes_before_retry = (
            self.root.path / FleetUpdateReceiptLedger.relative(self.actor)
        ).read_bytes()

        result = self._commit_real_g2(plan, key, inject)
        self.assertEqual(target, self.hooks_path.read_bytes())
        self.assertEqual(1, events.count("after_waiter_replace"))
        if target_inode is not None:
            self.assertEqual(target_inode, target_tree.stat().st_ino)
        self.assertFalse(any(path.name.startswith(".floati-fleet-stage-") for path in self.waiter_store.iterdir()))
        self.assertEqual(old_waiter_before, _tree_bytes(self.current_waiter))
        self.assertEqual(trust_before, trust_path.read_bytes())
        self.assertEqual((), tuple(self.hooks_path.parent.glob(f".{self.hooks_path.name}.floati-*")))
        self.assertEqual("g2_committed_pending_later_gates", result["state"])
        rows = self._assert_real_g2_chain(
            plan,
            key,
            shared_disposition="applied",
            waiter_disposition=(
                "applied"
                if event in pre_replace_events or event == "after_waiter_step_receipt"
                else "recovered_post_state"
            ),
        )
        if event == "after_waiter_step_receipt":
            self.assertEqual(1, len(waiter_rows_before))
            self.assertEqual(waiter_rows_before[0]["id"], rows[2]["id"])
            self.assertEqual(receipt_bytes_before_retry, (
                self.root.path / FleetUpdateReceiptLedger.relative(self.actor)
            ).read_bytes())
            self.assertEqual(rows[2]["id"], result["waiter_steps"][0]["receipt"]["id"])

    def test_g2_26_shared_file_journal_fault_recovers_through_real_commit(self) -> None:
        self._exercise_real_shared_fault("after_file_journal")

    def test_g2_27_shared_file_fsync_fault_recovers_through_real_commit(self) -> None:
        self._exercise_real_shared_fault("after_file_file_fsync")

    def test_g2_28_shared_file_replace_fault_recovers_through_real_commit(self) -> None:
        self._exercise_real_shared_fault("after_file_replace")

    def test_g2_29_shared_file_directory_fsync_fault_recovers_through_real_commit(self) -> None:
        self._exercise_real_shared_fault("after_file_directory_fsync")

    def test_g2_30_shared_file_readback_fault_recovers_through_real_commit(self) -> None:
        self._exercise_real_shared_fault("after_file_readback")

    def test_g2_31_shared_metadata_journal_fault_recovers_through_real_commit(self) -> None:
        self._exercise_real_shared_fault("after_metadata_journal")

    def test_g2_32_shared_metadata_fsync_fault_recovers_through_real_commit(self) -> None:
        self._exercise_real_shared_fault("after_metadata_file_fsync")

    def test_g2_33_shared_metadata_replace_fault_replays_durability_without_replace(self) -> None:
        self._exercise_real_shared_fault("after_metadata_replace")

    def test_g2_34_shared_metadata_directory_fsync_fault_replays_without_replace(self) -> None:
        self._exercise_real_shared_fault("after_metadata_directory_fsync")

    def test_g2_35_shared_metadata_readback_fault_replays_without_replace(self) -> None:
        self._exercise_real_shared_fault("after_metadata_readback")

    def test_g2_36_shared_post_commit_pre_receipt_fault_recovers_without_writer(self) -> None:
        self._exercise_real_shared_fault("after_shared_commit_readback")

    def test_g2_recovery_rejects_later_malformed_sibling_before_any_write(self) -> None:
        """Catches response-loss recovery staging past a later invalid receipt."""

        key = "fu1-g2-later-sibling-recovery"
        plan = self._prepare_real_g2_plan()

        def interrupt(event: str) -> None:
            if event == "after_shared_commit_readback":
                raise OSError("fixture shared response loss")

        with self.assertRaisesRegex(OSError, "response loss"):
            self._commit_real_g2(plan, key, interrupt)
        corrupt = self.root.path / "receipts" / "fleet-update" / "zz-corrupt.jsonl"
        corrupt.write_text("{not json}\n", encoding="utf-8")
        before = self._zero_write_snapshot(self.base)

        with self.assertRaises(IntegrityFailure) as caught:
            self._commit_real_g2(plan, key)

        self.assertEqual("fleet_update_receipt_invalid", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.base))

    def test_g2_37_shared_receipt_response_loss_reuses_the_appended_receipt(self) -> None:
        self._exercise_real_shared_fault("after_shared_step_receipt")

    def test_g2_38_waiter_generation_replace_fault_replays_store_durability(self) -> None:
        self._exercise_real_waiter_fault("after_waiter_generation_replace")

    def test_g2_39_waiter_generation_store_fsync_fault_replays_readback(self) -> None:
        self._exercise_real_waiter_fault("after_waiter_generation_store_fsync")

    def test_g2_39a_waiter_generation_pre_replace_fault_cleans_stage(self) -> None:
        self._exercise_real_waiter_fault("before_waiter_generation_replace")

    def test_g2_39b_waiter_generation_readback_fault_replays_without_rename(self) -> None:
        self._exercise_real_waiter_fault("after_waiter_generation_readback")

    def test_g2_40_waiter_before_temp_fault_leaves_exact_pre_and_recovers(self) -> None:
        self._exercise_real_waiter_fault("before_waiter_temp_create")

    def test_g2_41_waiter_before_write_fault_cleans_temp_and_recovers(self) -> None:
        self._exercise_real_waiter_fault("before_waiter_write")

    def test_g2_42_waiter_before_fsync_fault_cleans_temp_and_recovers(self) -> None:
        self._exercise_real_waiter_fault("before_waiter_file_fsync")

    def test_g2_43_waiter_before_replace_fault_cleans_temp_and_recovers(self) -> None:
        self._exercise_real_waiter_fault("before_waiter_replace")

    def test_g2_44_waiter_after_replace_fault_replays_durability_without_replace(self) -> None:
        self._exercise_real_waiter_fault("after_waiter_replace")

    def test_g2_45_waiter_after_directory_fsync_fault_replays_readback(self) -> None:
        self._exercise_real_waiter_fault("after_waiter_directory_fsync")

    def test_g2_46_waiter_after_readback_fault_reobserves_trust_without_replace(self) -> None:
        self._exercise_real_waiter_fault("after_waiter_readback")

    def test_g2_47_waiter_after_trust_fault_recovers_post_without_replace(self) -> None:
        self._exercise_real_waiter_fault("after_waiter_trust_observation")

    def test_g2_48_waiter_receipt_response_loss_reuses_exact_receipt(self) -> None:
        self._exercise_real_waiter_fault("after_waiter_step_receipt")

    def test_g2_49_real_shared_ambiguous_join_state_refuses_without_mutation(self) -> None:
        """Catches authenticated recovery overwriting a foreign terminal shared byte."""

        key = "fu1-g2-real-shared-ambiguous"
        plan = self._prepare_real_g2_plan()
        tripped = False

        def inject(event: str) -> None:
            nonlocal tripped
            if event == "after_file_journal" and not tripped:
                tripped = True
                raise OSError("fixture shared ambiguous frontier")

        with self.assertRaisesRegex(OSError, "ambiguous frontier"):
            self._commit_real_g2(plan, key, inject)
        first = json.loads(
            (self.target_source / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )["files"][0]["path"]
        target = self.destination / first
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"foreign terminal bytes\n")
        before = self._zero_write_snapshot(self.base)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._commit_real_g2(plan, key)
        self.assertEqual("fleet_update_install_recovery_invalid", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.base))

    def test_g2_50_real_waiter_ambiguous_post_state_refuses_without_mutation(self) -> None:
        """Catches a post-replace retry rewriting a hook that changed again externally."""

        key = "fu1-g2-real-waiter-ambiguous"
        plan = self._prepare_real_g2_plan()
        tripped = False

        def inject(event: str) -> None:
            nonlocal tripped
            if event == "after_waiter_replace" and not tripped:
                tripped = True
                raise OSError("fixture waiter ambiguous frontier")

        with self.assertRaisesRegex(OSError, "ambiguous frontier"):
            self._commit_real_g2(plan, key, inject)
        document = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        document["unrelated_foreign_change"] = True
        self.hooks_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        before = self._zero_write_snapshot(self.base)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._commit_real_g2(plan, key)
        self.assertEqual("fleet_update_plan_drift", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.base))

    @staticmethod
    def _zero_write_snapshot(root: Path) -> tuple[dict[str, bytes], tuple[tuple[str, str, str], ...]]:
        """Capture bytes and every entry name without dereferencing link targets.

        D3a refusals have to leave both product evidence and otherwise invisible
        staging directory names unchanged.  Capturing the whole fixture is more
        stringent than a hand-maintained list of the governed surfaces.
        """

        entries: list[tuple[str, str, str]] = []
        for path in sorted(root.rglob("*"), key=lambda candidate: candidate.as_posix()):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append((relative, "symlink", os.readlink(path)))
            elif path.is_dir():
                entries.append((relative, "directory", ""))
            else:
                entries.append((relative, "file", hashlib.sha256(path.read_bytes()).hexdigest()))
        return _tree_bytes(root), tuple(entries)

    def _assert_g2_authorization_refusal(
        self, plan: dict[str, object], expected_code: str, key: str
    ) -> None:
        """Drive the G2 entry point through pure staging and pin its first write."""

        from floati.fleet_update import commit_fleet_update_g2

        before = self._zero_write_snapshot(self.base)
        staged = {
            "source": str(self.target_source),
            "installer_executable": canonical_executable(),
            "git_executable": "/usr/bin/git",
            "install": {"pre_digest": plan["current_manifest_sha256"], "post_digest": plan["target_manifest_sha256"]},
            "waiters": [],
        }
        with mock.patch("floati.fleet_update.stage_fleet_update", return_value=staged) as stage,\
             mock.patch("floati.fleet_update._require_detached_source") as recheck:
            with self.assertRaises(ProtocolRefusal) as caught:
                commit_fleet_update_g2(
                    plan=plan, root=self.root, actor=self.actor, idempotency_key=key,
                    target_source=self.target_source, installer_executable=canonical_executable(),
                    git_executable="/usr/bin/git",
                )
        self.assertEqual(expected_code, caught.exception.code)
        self.assertEqual(1, stage.call_count, "G2 must finish pure preflight before its consent join")
        self.assertEqual(1, recheck.call_count, "G2 must recheck source identity before the first durable action")
        self.assertEqual(before, self._zero_write_snapshot(self.base))
        self.assertEqual([], FleetUpdateReceiptLedger(self.root).rows(self.actor))

    def test_g2_stage_refuses_stale_managed_removal_before_start_or_any_target_write(self) -> None:
        """Catches G2 deleting an old shared file before a recoverable join exists."""

        from floati.fleet_update import commit_fleet_update_g2
        from floati.deploy import DeploymentWriter

        manifest_path = self.target_source / "bundle-manifest.v0.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        removed = manifest["files"].pop()
        self.assertTrue(isinstance(removed, dict) and isinstance(removed.get("path"), str))
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        plan = self._preview()
        self._consent()
        before = self._zero_write_snapshot(self.base)

        with (
            mock.patch("floati.fleet_update._require_detached_source"),
            mock.patch.object(DeploymentWriter, "stage", return_value={"source_sha": plan["target_source_sha"]}),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            commit_fleet_update_g2(
                plan=plan, root=self.root, actor=self.actor,
                idempotency_key="fu1-g2-stale-removal", target_source=self.target_source,
                installer_executable=canonical_executable(), git_executable="/usr/bin/git",
            )

        self.assertEqual("fleet_update_install_stale_removal_unsupported", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.base))
        self.assertEqual([], FleetUpdateReceiptLedger(self.root).rows(self.actor))

    def test_g2_13_authorization_refusals_leave_every_governed_surface_identical(self) -> None:
        """Catches G2 appending start or materializing after absent, revoked, or wrong consent."""

        self._two_by_two()
        missing = self._preview()
        self._assert_g2_authorization_refusal(missing, "update_consent_missing", "fu1-g2-d3a-missing")

        self._consent()
        from floati.update_consent import UpdateConsentLedger
        UpdateConsentLedger(self.destination).revoke(
            channel=self.channel, idempotency_key="fu1-g2-d3a-revoke"
        )
        revoked = self._preview()
        self._assert_g2_authorization_refusal(revoked, "update_consent_revoked", "fu1-g2-d3a-revoked")

        self._consent()
        self.channel = "https://updates.example.invalid/wrong-channel"
        wrong_channel = self._preview()
        self._assert_g2_authorization_refusal(wrong_channel, "update_consent_mismatch", "fu1-g2-d3a-channel")

    def test_g2_14_workspace_mapping_refusals_are_pure_before_any_start(self) -> None:
        """Catches malformed, unregistered, and unresolved active maps touching G2 surfaces."""

        mapping_path = self.root.path / "codex-wait" / "workspaces.v0.json"
        self._map([])
        mapping_path.write_text("{}", encoding="utf-8")
        before = self._zero_write_snapshot(self.base)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()
        self.assertEqual("fleet_update_mapping_invalid", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.base))

        self._map([(self.base / "missing-workspace", "ghost")])
        before = self._zero_write_snapshot(self.base)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()
        self.assertEqual("fleet_update_mapping_invalid", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.base))

        workspace = self.base / "known-workspace"; workspace.mkdir()
        self._map([(workspace, "ghost")])
        before = self._zero_write_snapshot(self.base)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()
        self.assertEqual("fleet_update_mapping_unregistered", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.base))

    def test_g2_15_binding_inventory_refusals_are_pure_and_keep_mapping_evidence(self) -> None:
        """Catches omitted, ambiguous, or foreign-root binding inventories reaching start."""

        cases: list[tuple[str, object, str]] = [
            ("omitted", [], "fleet_update_binding_invalid"),
            ("ambiguous", [
                {"kind": "codex_stop_hook", "configuration": str(self.hooks_path), "store": str(self.waiter_store)},
                {"kind": "codex_stop_hook", "configuration": str(self.hooks_path), "store": str(self.waiter_store)},
            ], "fleet_update_binding_invalid"),
        ]
        for name, bindings, expected_code in cases:
            with self.subTest(name=name):
                self.binding_path.write_text(
                    json.dumps({"schema_version": 0, "bindings": bindings}, sort_keys=True),
                    encoding="utf-8",
                )
                before = self._zero_write_snapshot(self.base)
                with self.assertRaises(ProtocolRefusal) as caught:
                    self._preview()
                self.assertEqual(expected_code, caught.exception.code)
                self.assertEqual(before, self._zero_write_snapshot(self.base))

        foreign = self.base / "foreign-root"; foreign.mkdir()
        hooks, store = self._binding(91, root=foreign)
        self.binding_path.write_text(json.dumps({"schema_version": 0, "bindings": [{
            "kind": "codex_stop_hook", "configuration": str(hooks), "store": str(store),
        }]}, sort_keys=True), encoding="utf-8")
        before = self._zero_write_snapshot(self.base)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()
        self.assertEqual("fleet_update_waiter_binding_invalid", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.base))

    def test_g2_16_hook_consequences_and_post_observation_cover_all_trust_states(self) -> None:
        """Catches G2 inventing trust/armed reachability or reordering user remedies."""

        from floati.codex_hook_install import commit_waiter_rebind, plan_waiter_rebind, stage_waiter_runtime
        from floati.codex_hook_trust import observe_codex_waiter_hooks

        from floati.registry import Registry
        Registry(self.root).register("seat-state", "Codex")
        workspace = self.base / "state-workspace"; workspace.mkdir()
        self._map([(workspace, "seat-state")])

        def consequence() -> dict[str, object]:
            rows = self._preview()["seat_binding_consequences"]
            self.assertEqual(1, len(rows))
            return rows[0]

        changed_untrusted = consequence()
        self.assertEqual(
            (True, True, False, True, "unknown_until_review_and_relaunch",
             "review and trust the exact Stop hook in Codex settings;relaunch the affected session"),
            tuple(changed_untrusted[field] for field in (
                "trust_rotated_by_update", "review_required_after_update", "enable_required_after_update",
                "relaunch_required_after_update", "reachability_after_update", "remedy",
            )),
        )

        config = self.hooks_path.with_name("config.toml")
        current = observe_codex_waiter_hooks(self.hooks_path)[0]
        config.write_text(
            "[hooks.state." + json.dumps(current["hook_trust_key"]) + "]\n"
            "enabled = false\ntrusted_hash = " + json.dumps(current["hook_trust_current_hash"]) + "\n",
            encoding="utf-8",
        )
        disabled = consequence()
        self.assertEqual(
            (True, True, True, True, "unknown_until_review_and_relaunch",
             "enable the exact Stop hook in Codex settings;review and trust the exact Stop hook in Codex settings;relaunch the affected session"),
            tuple(disabled[field] for field in (
                "trust_rotated_by_update", "review_required_after_update", "enable_required_after_update",
                "relaunch_required_after_update", "reachability_after_update", "remedy",
            )),
        )

        target_digest = str(self._preview()["target_waiter_digest"])
        old_tree = self.current_waiter
        old_tree_before = _tree_bytes(old_tree)
        stage_waiter_runtime(self.target_source, self.waiter_store, target_digest)
        self.assertEqual(old_tree_before, _tree_bytes(old_tree), "old generation must remain immutable")
        document = self.hooks_path.read_text(encoding="utf-8")
        document = document.replace(str(self.current_waiter / "scripts" / "floati-codex-wait"), str(self.waiter_store / target_digest / "scripts" / "floati-codex-wait"))
        self.hooks_path.write_text(document, encoding="utf-8")
        target = observe_codex_waiter_hooks(self.hooks_path)[0]
        config.write_text(
            "[hooks.state." + json.dumps(target["hook_trust_key"]) + "]\n"
            "enabled = true\ntrusted_hash = " + json.dumps(target["hook_trust_current_hash"]) + "\n",
            encoding="utf-8",
        )
        trust_before = config.read_bytes()
        unchanged_trusted = consequence()
        self.assertEqual(
            (False, False, False, False, "not_observed", None),
            tuple(unchanged_trusted[field] for field in (
                "trust_rotated_by_update", "review_required_after_update", "enable_required_after_update",
                "relaunch_required_after_update", "reachability_after_update", "remedy",
            )),
        )
        self.assertEqual(trust_before, config.read_bytes(), "planning must never write Codex trust state")

        config.unlink()
        unchanged_untrusted = consequence()
        self.assertEqual(
            (False, True, False, True, "unknown_until_review_and_relaunch",
             "review and trust the exact Stop hook in Codex settings;relaunch the affected session"),
            tuple(unchanged_untrusted[field] for field in (
                "trust_rotated_by_update", "review_required_after_update", "enable_required_after_update",
                "relaunch_required_after_update", "reachability_after_update", "remedy",
            )),
        )
        staged = plan_waiter_rebind(self.hooks_path, self.waiter_store, target_digest)
        staged["hook_trust_key"] = target["hook_trust_key"]
        staged["pre_trust_observation"] = {
            "hook_trust_key": target["hook_trust_key"], "current_hook_hash": target["hook_trust_current_hash"],
            "observed_trusted_hash": None, "observed_enabled": True,
        }
        result = commit_waiter_rebind(staged)
        self.assertEqual(target["hook_trust_key"], result["trust_observation"]["hook_trust_key"])
        self.assertFalse(result["trust_observation"]["hook_armed"])
        self.assertFalse(config.exists(), "G2 must observe, never create or write trust configuration")

    def test_g2_17_exact_retry_skips_writer_generation_hook_write_and_receipt_append(self) -> None:
        """Catches an exact G2 retry doing any second shared or waiter durable action."""

        from floati import codex_hook_install
        from floati.codex_hook_install import (
            commit_waiter_rebind, plan_waiter_rebind, stage_waiter_runtime,
        )
        from floati.codex_hook_trust import observe_codex_waiter_hooks
        from floati.fleet_update import (
            _shared_install_join_id, commit_fleet_update_g2,
        )

        self._consent(); plan = self._preview(); binding = plan["waiter_bindings"][0]
        staged_waiter = plan_waiter_rebind(
            Path(str(binding["configuration"])), Path(str(binding["store"])), str(plan["target_waiter_digest"])
        )
        observed = observe_codex_waiter_hooks(self.hooks_path)[0]
        staged_waiter["hook_trust_key"] = observed["hook_trust_key"]
        staged_waiter["pre_trust_observation"] = {
            "hook_trust_key": observed["hook_trust_key"], "current_hook_hash": observed["hook_trust_current_hash"],
            "observed_trusted_hash": observed["hook_trust_observed_hash"], "observed_enabled": observed["hook_enabled"],
        }
        evidence = {
            "kind": "shared_install", "journal_path": str(self.destination / ".floati-install" / "wiring-journal.v1.jsonl"),
            "join_id": _shared_install_join_id(
                str(plan["plan_digest"]), self.actor, "fu1-g2-d3b-retry",
            ), "predecessor_ordinal": None, "predecessor_entry_hash": None,
            "first_ordinal": 1,
            "last_ordinal": len(plan["shared_install_intents"]),
            "entry_hashes": ["e" * 64] * len(plan["shared_install_intents"]),
        }
        staged = {"source": str(self.target_source), "installer_executable": canonical_executable(), "git_executable": "/usr/bin/git", "waiters": [staged_waiter]}
        calls = {"shared_writer": 0, "generation": 0, "hook": 0, "atomic": 0}
        original_stage, original_commit, original_atomic = stage_waiter_runtime, commit_waiter_rebind, codex_hook_install._write_atomic

        def counted_stage(*args: object, **kwargs: object) -> object:
            calls["generation"] += 1; return original_stage(*args, **kwargs)
        def counted_commit(*args: object, **kwargs: object) -> object:
            calls["hook"] += 1; return original_commit(*args, **kwargs)
        def counted_atomic(*args: object, **kwargs: object) -> object:
            calls["atomic"] += 1; return original_atomic(*args, **kwargs)

        with (
            mock.patch("floati.fleet_update.stage_fleet_update", return_value=staged),
            mock.patch("floati.fleet_update._require_detached_source"),
            mock.patch("floati.fleet_update._shared_install_recovery_evidence", side_effect=[
                {"phase": "pre", "evidence": None}, {"phase": "post", "evidence": evidence},
                {"phase": "post", "evidence": evidence}, {"phase": "post", "evidence": evidence},
                {"phase": "post", "evidence": evidence},
            ]),
            mock.patch("floati.fleet_update.DeploymentWriter") as writer,
            mock.patch("floati.fleet_update.stage_waiter_runtime", side_effect=counted_stage),
            mock.patch("floati.fleet_update.commit_waiter_rebind", side_effect=counted_commit),
            mock.patch("floati.codex_hook_install._write_atomic", side_effect=counted_atomic),
        ):
            writer.return_value.run.side_effect = lambda: calls.__setitem__("shared_writer", calls["shared_writer"] + 1)
            first = commit_fleet_update_g2(plan=plan, root=self.root, actor=self.actor, idempotency_key="fu1-g2-d3b-retry", target_source=self.target_source, installer_executable=canonical_executable(), git_executable="/usr/bin/git")
            receipt_path = self.root.path / FleetUpdateReceiptLedger.relative(self.actor)
            first_receipt_bytes = receipt_path.read_bytes()
            second = commit_fleet_update_g2(plan=plan, root=self.root, actor=self.actor, idempotency_key="fu1-g2-d3b-retry", target_source=self.target_source, installer_executable=canonical_executable(), git_executable="/usr/bin/git")
        self.assertEqual(first["last_receipt_id"], second["last_receipt_id"])
        self.assertEqual(first, second)
        self.assertEqual({"shared_writer": 1, "generation": 1, "hook": 1, "atomic": 1}, calls)
        self.assertEqual(first_receipt_bytes, receipt_path.read_bytes())
        waiter_rows = [row for row in FleetUpdateReceiptLedger(self.root).rows(self.actor) if row["kind"] == "fleet_update_step"]
        self.assertNotIn(
            "hook_armed",
            waiter_rows[-1]["step_evidence"]["hook_post_observation"],
        )

    def test_g2_18_two_by_two_consequences_are_carried_by_each_physical_step(self) -> None:
        """Catches a G2 step dropping one binding consequence from its receipt batch."""

        self._two_by_two(); self._consent(); plan = self._preview()
        self.assertEqual(4, len(plan["seat_binding_consequences"]))
        ledger = FleetUpdateReceiptLedger(self.root); key = "fu1-g2-d3b-2x2"
        started = ledger.start(plan, self.actor, key)
        predecessor = str(started["id"])
        steps = []
        for ordinal, kind in enumerate(("shared_install", "waiter_binding", "waiter_binding"), start=1):
            step = self._append_observed_step(
                ledger, plan, self.actor, key, predecessor
            )
            self.assertEqual(plan["seat_binding_consequences"], step["seat_binding_consequences"])
            self.assertEqual(4, len(step["seat_binding_consequences"]))
            predecessor = str(step["id"]); steps.append(step)
        self.assertEqual([1, 2, 3], [step["step_ordinal"] for step in steps])

    def test_g2_19_waiter_post_write_pre_receipt_failure_recovers_without_second_write(self) -> None:
        """Catches a receipt-boundary crash rewriting a hook already proven at post-state."""

        from floati import codex_hook_install
        from floati.codex_hook_install import plan_waiter_rebind
        from floati.codex_hook_trust import observe_codex_waiter_hooks
        from floati.fleet_update import (
            _shared_install_join_id, commit_fleet_update_g2,
        )

        self._consent(); plan = self._preview(); binding = plan["waiter_bindings"][0]
        staged_waiter = plan_waiter_rebind(
            Path(str(binding["configuration"])), Path(str(binding["store"])), str(plan["target_waiter_digest"])
        )
        trust = observe_codex_waiter_hooks(self.hooks_path)[0]
        staged_waiter["hook_trust_key"] = trust["hook_trust_key"]
        staged_waiter["pre_trust_observation"] = {
            "hook_trust_key": trust["hook_trust_key"], "current_hook_hash": trust["hook_trust_current_hash"],
            "observed_trusted_hash": trust["hook_trust_observed_hash"], "observed_enabled": trust["hook_enabled"],
        }
        shared_evidence = {
            "kind": "shared_install", "journal_path": str(self.destination / ".floati-install" / "wiring-journal.v1.jsonl"),
            "join_id": _shared_install_join_id(
                str(plan["plan_digest"]), self.actor, "fu1-g2-d3b-crash",
            ), "predecessor_ordinal": None, "predecessor_entry_hash": None,
            "first_ordinal": 1,
            "last_ordinal": len(plan["shared_install_intents"]),
            "entry_hashes": ["d" * 64] * len(plan["shared_install_intents"]),
        }
        staged = {"source": str(self.target_source), "installer_executable": canonical_executable(), "git_executable": "/usr/bin/git", "waiters": [staged_waiter]}
        original_step = FleetUpdateReceiptLedger._step_guarded
        original_atomic = codex_hook_install._write_atomic
        calls = {"step": 0, "atomic": 0}

        def fail_one_waiter_receipt(ledger: object, *args: object, **kwargs: object) -> object:
            calls["step"] += 1
            if calls["step"] == 2:
                raise OSError("fixture receipt interruption")
            return original_step(ledger, *args, **kwargs)

        def counted_atomic(*args: object, **kwargs: object) -> object:
            calls["atomic"] += 1
            return original_atomic(*args, **kwargs)

        with (
            mock.patch("floati.fleet_update.stage_fleet_update", return_value=staged),
            mock.patch("floati.fleet_update._require_detached_source"),
            mock.patch("floati.fleet_update._shared_install_recovery_evidence", side_effect=[
                {"phase": "pre", "evidence": None}, {"phase": "post", "evidence": shared_evidence},
                {"phase": "post", "evidence": shared_evidence}, {"phase": "post", "evidence": shared_evidence},
                {"phase": "post", "evidence": shared_evidence},
            ]),
            mock.patch("floati.fleet_update.DeploymentWriter"),
            mock.patch.object(
                FleetUpdateReceiptLedger,
                "_step_guarded",
                new=fail_one_waiter_receipt,
            ),
            mock.patch("floati.codex_hook_install._write_atomic", side_effect=counted_atomic),
        ):
            with self.assertRaises(OSError):
                commit_fleet_update_g2(plan=plan, root=self.root, actor=self.actor, idempotency_key="fu1-g2-d3b-crash", target_source=self.target_source, installer_executable=canonical_executable(), git_executable="/usr/bin/git")
            self.assertEqual(staged_waiter["after"], self.hooks_path.read_bytes())
            recovered = commit_fleet_update_g2(plan=plan, root=self.root, actor=self.actor, idempotency_key="fu1-g2-d3b-crash", target_source=self.target_source, installer_executable=canonical_executable(), git_executable="/usr/bin/git")
        self.assertEqual("g2_committed_pending_later_gates", recovered["state"])
        self.assertEqual(1, calls["atomic"], "post-write retry must append evidence without a second hook rewrite")
        rows = [row for row in FleetUpdateReceiptLedger(self.root).rows(self.actor) if row["kind"] == "fleet_update_step"]
        self.assertEqual("recovered_post_state", rows[-1]["commit_disposition"])

    def test_g2_20_waiter_prewrite_failure_is_byte_preserving_and_ruled_block_is_surgical(self) -> None:
        """Catches a failed atomic rebind touching either its hook or unrelated hook bytes."""

        from floati.codex_hook_install import commit_waiter_rebind, plan_waiter_rebind
        from floati.codex_hook_trust import observe_codex_waiter_hooks

        plan = self._preview(); before = self.hooks_path.read_bytes()
        self.assertIn(b'"command": "true"', before, "fixture carries an unrelated SessionStart byte sequence")
        staged = plan_waiter_rebind(self.hooks_path, self.waiter_store, str(plan["target_waiter_digest"]))
        observed = observe_codex_waiter_hooks(self.hooks_path)[0]
        staged["hook_trust_key"] = observed["hook_trust_key"]
        staged["pre_trust_observation"] = {
            "hook_trust_key": observed["hook_trust_key"], "current_hook_hash": observed["hook_trust_current_hash"],
            "observed_trusted_hash": observed["hook_trust_observed_hash"], "observed_enabled": observed["hook_enabled"],
        }
        with mock.patch("floati.codex_hook_install._write_atomic", side_effect=OSError("fixture before commit")):
            with self.assertRaises(OSError):
                commit_waiter_rebind(staged)
        self.assertEqual(before, self.hooks_path.read_bytes())
        commit_waiter_rebind(staged)
        after = self.hooks_path.read_bytes()
        self.assertIn(b'"command": "true"', after)
        self.assertEqual(before.split(b'"Stop"', 1)[0], after.split(b'"Stop"', 1)[0])

    def test_g2_21_prefixed_trust_hash_normalizes_through_plan_start_and_malformed_refuses(self) -> None:
        """Catches external Codex sha256 testimony leaking into receipts or being silently accepted malformed."""

        from floati.codex_hook_trust import observe_codex_waiter_hooks
        from floati.registry import Registry

        Registry(self.root).register("seat-prefix", "Codex")
        workspace = self.base / "prefix-workspace"; workspace.mkdir()
        self._map([(workspace, "seat-prefix")])
        observed = observe_codex_waiter_hooks(self.hooks_path)[0]
        config = self.hooks_path.with_name("config.toml")
        config.write_text(
            "[hooks.state." + json.dumps(observed["hook_trust_key"]) + "]\n"
            "trusted_hash = " + json.dumps("sha256:" + observed["hook_trust_current_hash"]) + "\n",
            encoding="utf-8",
        )
        plan = self._preview()
        self.assertEqual(observed["hook_trust_current_hash"], plan["seat_binding_consequences"][0]["observed_trusted_hash"])
        self._consent()
        started = FleetUpdateReceiptLedger(self.root).start(plan, self.actor, "fu1-g2-prefix")
        self.assertEqual(observed["hook_trust_current_hash"], started["seat_binding_consequences"][0]["observed_trusted_hash"])

        config.write_text(
            "[hooks.state." + json.dumps(observed["hook_trust_key"]) + "]\ntrusted_hash = \"SHA256:" + observed["hook_trust_current_hash"] + "\"\n",
            encoding="utf-8",
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self._preview()
        self.assertEqual("codex_wait_hook_trust_unavailable", caught.exception.code)

    def test_g2_22_every_public_ledger_entry_reauthenticates_before_history_access(self) -> None:
        """Catches direct receipt calls accepting a body coordinate changed under its old digest."""

        self._consent(); plan = self._preview(); ledger = FleetUpdateReceiptLedger(self.root)
        start = ledger.start(plan, self.actor, "fu1-g2-ledger-auth")
        receipt = self.root.path / ledger.relative(self.actor)
        before = receipt.read_bytes()
        forged = json.loads(json.dumps(plan))
        forged["current_source_sha"] = "f" * 40

        for operation in ("start", "exact_retry", "step", "complete"):
            with self.subTest(operation=operation):
                with self.assertRaises(ProtocolRefusal) as caught:
                    if operation == "start":
                        ledger.start(forged, self.actor, "fu1-g2-ledger-forged-start")
                    elif operation == "exact_retry":
                        ledger.exact_retry(forged, self.actor, "fu1-g2-ledger-auth")
                    elif operation == "step":
                        ledger.step(
                            forged, self.actor, "fu1-g2-ledger-auth",
                            str(start["id"]),
                        )
                    else:
                        ledger.complete(forged, self.actor, "fu1-g2-ledger-auth", str(start["id"]))
                self.assertEqual("fleet_update_plan_invalid", caught.exception.code)
                self.assertEqual(before, receipt.read_bytes())

    def test_g2_23_phase_shape_history_ordinal_and_armed_formula_are_strict(self) -> None:
        """Catches generic phase objects, gapped ordinals, or invented armed testimony."""

        self._consent(); plan = self._preview(); ledger = FleetUpdateReceiptLedger(self.root)
        start = ledger.start(plan, self.actor, "fu1-g2-phase-parity")
        step = self._append_observed_step(
            ledger, plan, self.actor, "fu1-g2-phase-parity", str(start["id"])
        )
        terminal_mutation = dict(start, step_ordinal=1)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(terminal_mutation, REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json")
        coordinate_mutation = json.loads(json.dumps(step)); coordinate_mutation["step_coordinate"].pop("metadata_relative")
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(coordinate_mutation, REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json")

        receipt = self.root.path / ledger.relative(self.actor)
        gapped = json.loads(json.dumps(step)); gapped["step_ordinal"] = 2
        receipt.write_text("\n".join(json.dumps(row, sort_keys=True) for row in (start, gapped)) + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityFailure) as caught:
            ledger.rows(self.actor)
        self.assertEqual("fleet_update_receipt_order_invalid", caught.exception.code)

        armed = json.loads(json.dumps(step))
        armed["step_kind"] = "waiter_binding"
        armed["step_coordinate"] = {"kind": "waiter_binding", "index": 0, "configuration": str(self.hooks_path), "store": str(self.waiter_store), "trust_key": None}
        armed["step_evidence"] = {"kind": "waiter_binding", "hook_post_observation": {"hook_trust_key": "fixture", "current_hook_hash": "a" * 64, "observed_trusted_hash": None, "observed_enabled": True, "hook_armed": True}}
        with self.assertRaises(ProtocolRefusal) as caught:
            validate_record(armed, self.root.tenant_id, frozenset({"fleet_update_step"}), integrity=False)
        self.assertEqual("fleet_update_step_invalid", caught.exception.code)

    def test_g2_fix3_preview_carries_complete_semantic_and_writer_witnesses(self) -> None:
        """Catches a self-hash being the only authority for encoder, pins, or writer intent."""

        plan = self._prepare_real_g2_plan()

        def encoder_digest(root: Path) -> str:
            digest = hashlib.sha256()
            for relative in ("floati/events.py", "floati/records.py"):
                digest.update(
                    relative.encode("ascii")
                    + b"\0"
                    + hashlib.sha256((root / relative).read_bytes()).digest()
                )
            return digest.hexdigest()

        self.assertEqual(
            encoder_digest(self.destination), plan["current_encoder_sha256"]
        )
        self.assertEqual(
            encoder_digest(self.target_source), plan["target_encoder_sha256"]
        )
        self.assertEqual(
            plan["requires_epoch_roll"],
            plan["current_encoder_sha256"] != plan["target_encoder_sha256"],
        )

        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        pinned = registry["transports"][self.transport]
        self.assertEqual(
            {
                "manifest_sha256": pinned["manifest_sha256"],
                "source_sha": pinned["source_sha"],
            },
            plan["current_transport_pins"],
        )
        self.assertEqual(
            {
                "manifest_sha256": plan["target_manifest_sha256"],
                "source_sha": plan["target_source_sha"],
            },
            plan["target_transport_pins"],
        )

        current_metadata = json.loads(
            self._installed_metadata_path().read_text(encoding="utf-8")
        )
        current_paths = {row["path"] for row in current_metadata["files"]}
        target_manifest = json.loads(
            (self.target_source / "bundle-manifest.v0.json").read_text(
                encoding="utf-8"
            )
        )
        expected_intents = [
            {
                "kind": "file",
                "op": "replace" if row["path"] in current_paths else "create",
                "path": str(self.destination / row["path"]),
                "sha256": row["sha256"],
            }
            for row in target_manifest["files"]
        ]
        expected_intents.append(
            {
                "kind": "file",
                "op": "replace",
                "path": str(self._installed_metadata_path()),
                "sha256": plan["target_manifest_sha256"],
            }
        )
        self.assertEqual(expected_intents, plan["shared_install_intents"])
        self.assertEqual(
            ["create", "create", "create"],
            [
                row["op"]
                for row in plan["shared_install_intents"]
                if row["path"].endswith(
                    ("/LICENSE", "/fu1_created.py", "/fu1_new/created.py")
                )
            ],
        )

    def test_g2_fix3_public_plan_auth_rejects_freshly_self_hashed_impossibilities(self) -> None:
        """Catches semantic mutations reaching history or consent merely because they self-hash."""

        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        receipt_path = self.root.path / ledger.relative(self.actor)

        hostile_plans: list[tuple[str, dict[str, object]]] = []

        invalid_sha_and_move = json.loads(json.dumps(plan))
        invalid_sha_and_move["current_source_sha"] = "not-a-git-sha"
        invalid_sha_and_move["moves"][0]["path"] = "relative"
        hostile_plans.append(
            ("invalid-current-sha-invalid-move", self._rehash_plan(invalid_sha_and_move))
        )

        wrong_move_from = json.loads(json.dumps(plan))
        wrong_move_from["moves"][0]["from"] = "0" * 64
        hostile_plans.append(("wrong-move-from", self._rehash_plan(wrong_move_from)))

        wrong_epoch_formula = json.loads(json.dumps(plan))
        wrong_epoch_formula["requires_epoch_roll"] = not plan["requires_epoch_roll"]
        hostile_plans.append(
            ("wrong-epoch-formula", self._rehash_plan(wrong_epoch_formula))
        )

        wrong_stale_pins = json.loads(json.dumps(plan))
        wrong_stale_pins["stale_pins"] = []
        hostile_plans.append(("wrong-stale-pins", self._rehash_plan(wrong_stale_pins)))

        wrong_waiter_tree = json.loads(json.dumps(plan))
        wrong_waiter_tree["waiter_bindings"][0]["current_tree"] = str(
            self.base / "invented-waiter-tree"
        )
        hostile_plans.append(("wrong-waiter-tree", self._rehash_plan(wrong_waiter_tree)))

        wrong_intent_op = json.loads(json.dumps(plan))
        first_intent = wrong_intent_op["shared_install_intents"][0]
        first_intent["op"] = (
            "create" if first_intent["op"] == "replace" else "replace"
        )
        hostile_plans.append(("wrong-intent-op", self._rehash_plan(wrong_intent_op)))

        for ordinal, (label, forged) in enumerate(hostile_plans):
            with self.subTest(label=label):
                before = receipt_path.read_bytes() if receipt_path.exists() else None
                with self.assertRaises(ProtocolRefusal) as caught:
                    ledger.start(
                        forged,
                        self.actor,
                        f"fu1-g2-fix3-hostile-{ordinal}",
                    )
                self.assertEqual("fleet_update_plan_invalid", caught.exception.code)
                self.assertEqual(
                    before,
                    receipt_path.read_bytes() if receipt_path.exists() else None,
                )

    def test_g2_fix3_coupled_preinventory_and_op_forgery_refuses_before_start(self) -> None:
        """Catches a coherent self-hash whose pre-inventory disagrees with physical metadata."""

        plan = self._prepare_real_g2_plan()
        self.assertIn("current_managed_paths", plan)
        forged = json.loads(json.dumps(plan))
        created_intent = next(
            row
            for row in forged["shared_install_intents"]
            if row["op"] == "create"
        )
        relative = Path(str(created_intent["path"])).relative_to(
            self.destination
        ).as_posix()
        forged["current_managed_paths"] = sorted(
            [*forged["current_managed_paths"], relative]
        )
        created_intent["op"] = "replace"
        forged = self._rehash_plan(forged)
        destination_before = _tree_bytes(self.destination)
        key = "fu1-g2-fix3-forged-current-inventory"

        with self.assertRaises(ProtocolRefusal) as caught:
            self._commit_real_g2(forged, key)
        self.assertEqual("fleet_update_plan_drift", caught.exception.code)
        self.assertEqual([], self._related_g2_rows(key))
        self.assertEqual(destination_before, _tree_bytes(self.destination))

    def test_g2_fix3_terminal_retry_revalidates_plan_derived_steps(self) -> None:
        """Catches terminal replay returning before its persisted steps are re-derived."""

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        key = "fu1-g2-fix3-terminal-revalidation"
        started = ledger.start(plan, self.actor, key)
        steps = self._append_canonical_steps(
            ledger, plan, self.actor, key, str(started["id"])
        )
        completed = ledger.complete(
            plan, self.actor, key, str(steps[-1]["id"])
        )
        receipt_path = self.root.path / ledger.relative(self.actor)
        rows = [
            json.loads(line)
            for line in receipt_path.read_text(encoding="utf-8").splitlines()
        ]
        rows[1]["pre_digest"] = "f" * 64
        receipt_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

        with self.assertRaises(IntegrityFailure) as caught:
            ledger.complete(plan, self.actor, key, str(steps[-1]["id"]))
        self.assertEqual("fleet_update_step_invalid", caught.exception.code)
        self.assertEqual("fleet_update_completed", completed["kind"])

    def test_g2_fix3_incomplete_saga_excludes_other_keys_until_completion(self) -> None:
        """Catches a second start stranding the actor ledger's physical predecessor chain."""

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        key_a = "fu1-g2-fix3-saga-a"
        key_b = "fu1-g2-fix3-saga-b"
        started_a = ledger.start(plan, self.actor, key_a)
        before = (self.root.path / ledger.relative(self.actor)).read_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.start(plan, self.actor, key_b)
        self.assertEqual("fleet_update_receipt_order_invalid", caught.exception.code)
        self.assertEqual(
            before, (self.root.path / ledger.relative(self.actor)).read_bytes()
        )

        steps = self._append_canonical_steps(
            ledger, plan, self.actor, key_a, str(started_a["id"])
        )
        completed_a = ledger.complete(
            plan, self.actor, key_a, str(steps[-1]["id"])
        )
        started_b = ledger.start(plan, self.actor, key_b)
        self.assertEqual(completed_a["id"], started_b["predecessor_receipt_id"])

    def test_g2_fix3_failed_step_consumes_its_prepared_observation(self) -> None:
        """Catches a failed append reusing stale pre-state disposition testimony."""

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        key = "fu1-g2-fix3-one-attempt-prepare"
        started = ledger.start(plan, self.actor, key)
        predecessor = str(started["id"])
        with mock.patch.object(
            ledger, "_observe_step", return_value={"phase": "pre", "evidence": None}
        ):
            prepared = ledger.prepare_step(plan, self.actor, key, predecessor)
        evidence = self._synthetic_step_evidence(
            plan, self.actor, key, prepared
        )

        with (
            mock.patch.object(
                ledger,
                "_observe_step",
                return_value={"phase": "divergent", "evidence": None},
            ),
            self.assertRaises(ProtocolRefusal),
        ):
            ledger.step(plan, self.actor, key, predecessor)
        with (
            mock.patch.object(
                ledger,
                "_observe_step",
                return_value={"phase": "post", "evidence": evidence},
            ),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            ledger.step(plan, self.actor, key, predecessor)
        self.assertEqual("fleet_update_step_evidence_missing", caught.exception.code)

        with mock.patch.object(
            ledger,
            "_observe_step",
            side_effect=[
                {"phase": "post", "evidence": evidence},
                {"phase": "post", "evidence": evidence},
            ],
        ):
            refreshed = ledger.prepare_step(plan, self.actor, key, predecessor)
            receipt = ledger.step(plan, self.actor, key, predecessor)
        self.assertEqual("post", refreshed["initial_phase"])
        self.assertEqual("recovered_post_state", receipt["commit_disposition"])

    def test_g2_fix4_start_physically_rejects_coherent_false_hook_testimony(self) -> None:
        """Catches a fresh self-hash turning invented owner-review facts into authority."""

        self._two_by_two()
        self._consent()
        plan = self._preview()
        forged = self._forge_owner_review_fact(
            plan,
            field="observed_enabled",
            value=not plan["seat_binding_consequences"][0]["observed_enabled"],
        )
        ledger = FleetUpdateReceiptLedger(self.root)
        before = self._zero_write_snapshot(self.root.path)

        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.start(forged, self.actor, "fu1-g2-fix4-false-start")
        self.assertEqual("fleet_update_owner_review_invalid", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.root.path))
        self.assertEqual([], ledger.rows(self.actor))

    def test_g2_fix4_exact_retry_observes_false_hook_testimony_before_return(self) -> None:
        """Catches exact_retry returning None before authenticating physical raw facts."""

        self._two_by_two()
        plan = self._preview()
        consequence = plan["seat_binding_consequences"][0]
        forged = self._forge_owner_review_fact(
            plan,
            field="observed_trusted_hash",
            value=consequence["current_hook_hash"],
        )
        before = self._zero_write_snapshot(self.root.path)

        with self.assertRaises(ProtocolRefusal) as caught:
            FleetUpdateReceiptLedger(self.root).exact_retry(
                forged, self.actor, "fu1-g2-fix4-false-retry"
            )
        self.assertEqual("fleet_update_owner_review_invalid", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.root.path))

    def test_g2_fix4_exact_retry_reobserves_unfinished_hook_raw_facts(self) -> None:
        """Catches a durable start making stale pre-hook testimony permanent."""

        self._two_by_two()
        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        key = "fu1-g2-fix4-stale-pre-hook"
        started = ledger.start(plan, self.actor, key)
        consequence = plan["seat_binding_consequences"][0]
        config = Path(str(consequence["configuration"])).with_name("config.toml")
        config.write_text(
            "[hooks.state."
            + json.dumps(consequence["hook_trust_key"])
            + "]\nenabled = false\n",
            encoding="utf-8",
        )
        before = (self.root.path / ledger.relative(self.actor)).read_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.exact_retry(plan, self.actor, key)
        self.assertEqual("fleet_update_owner_review_invalid", caught.exception.code)
        self.assertEqual(before, (self.root.path / ledger.relative(self.actor)).read_bytes())
        self.assertEqual("fleet_update_started", started["kind"])

    def test_g2_fix4_completed_waiter_evidence_must_equal_plan_post_projection(self) -> None:
        """Catches a schema-valid waiter receipt preserving invented post-hook facts."""

        from floati.fleet_update_receipts import _step_spec
        from floati.registry import Registry

        Registry(self.root).register("seat-review", "Codex")
        workspace = self.base / "review-workspace"
        workspace.mkdir()
        self._map([(workspace, "seat-review")])
        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        key = "fu1-g2-fix4-false-post-evidence"
        started = ledger.start(plan, self.actor, key)
        shared = self._append_observed_step(
            ledger, plan, self.actor, key, str(started["id"])
        )
        spec = _step_spec(plan, plan["seat_binding_consequences"], 2)
        consequence = plan["seat_binding_consequences"][0]
        false_evidence = {
            "kind": "waiter_binding",
            "hook_post_observation": {
                "hook_trust_key": consequence["hook_trust_key"],
                "current_hook_hash": consequence["target_hook_hash"],
                "observed_trusted_hash": consequence["target_hook_hash"],
                "observed_enabled": consequence["observed_enabled"],
            },
        }
        forged = ledger._base(
            kind="fleet_update_step",
            plan_digest=str(plan["plan_digest"]),
            actor=self.actor,
            key=key,
            consent={"id": started["consent_receipt_id"]},
            predecessor=str(shared["id"]),
            readers=list(plan["reader_consequences"]),
            consequences=list(plan["seat_binding_consequences"]),
            exclusions=list(plan["seat_exclusions"]),
            batch=str(plan["owner_review_batch_digest"]),
            operation="step",
            step_kind="waiter_binding",
            pre_digest=str(spec["pre_digest"]),
            post_digest=str(spec["post_digest"]),
            state="completed",
            step_ordinal=int(spec["step_ordinal"]),
            step_coordinate=dict(spec["step_coordinate"]),
            commit_disposition="applied",
            step_evidence=false_evidence,
        )
        forged["tenant_id"] = self.root.tenant_id
        append_record(
            self.root,
            ledger.relative(self.actor),
            forged,
            allowed_kinds=frozenset(
                {"fleet_update_started", "fleet_update_step", "fleet_update_completed"}
            ),
        )

        with self.assertRaises(IntegrityFailure) as caught:
            ledger.exact_retry(plan, self.actor, key)
        self.assertEqual("fleet_update_step_invalid", caught.exception.code)

    def test_g2_fix4_completed_retry_accepts_plan_pre_facts_via_exact_post_evidence(self) -> None:
        """Proves a valid post receipt is reconciled without reclassifying it as stale pre."""

        from floati.registry import Registry

        Registry(self.root).register("seat-complete", "Codex")
        workspace = self.base / "complete-workspace"
        workspace.mkdir()
        self._map([(workspace, "seat-complete")])
        plan = self._prepare_real_g2_plan()
        key = "fu1-g2-fix4-completed-post"
        result = self._commit_real_g2(plan, key)
        started = FleetUpdateReceiptLedger(self.root).exact_retry(
            plan, self.actor, key
        )

        self.assertIsNotNone(started)
        self.assertEqual(result["started_receipt"]["id"], started["id"])
        consequence = plan["seat_binding_consequences"][0]
        waiter = next(
            row
            for row in self._related_g2_rows(key)
            if row.get("step_kind") == "waiter_binding"
        )
        self.assertEqual(
            {
                "hook_trust_key": consequence["hook_trust_key"],
                "current_hook_hash": consequence["target_hook_hash"],
                "observed_trusted_hash": consequence["observed_trusted_hash"],
                "observed_enabled": consequence["observed_enabled"],
            },
            waiter["step_evidence"]["hook_post_observation"],
        )

    def test_g2_fix4_completed_two_binding_retry_accepts_the_receipted_post_frontier(self) -> None:
        """Catches pure staging treating a fully receipted multi-post retry as unstarted."""

        self._two_by_two()
        plan = self._prepare_real_g2_plan()
        key = "fu1-g2-fix4-completed-two-binding-retry"
        completed = self._commit_real_g2(plan, key)
        before = self._related_g2_rows(key)

        retried = self._commit_real_g2(plan, key)

        self.assertEqual(completed["last_receipt_id"], retried["last_receipt_id"])
        self.assertEqual(before, self._related_g2_rows(key))
        self.assertEqual(
            [2, 3],
            [
                row["step_ordinal"]
                for row in before
                if row.get("step_kind") == "waiter_binding"
            ],
        )

    def test_g2_fix4_completed_retry_survives_later_seat_retirement(self) -> None:
        """Pins terminal retry to persisted plan/post facts, not evolved eligibility."""

        from floati.registry import Registry

        registry = Registry(self.root)
        registry.register("seat-completed-retired", "Codex")
        workspace = self.base / "completed-retired-workspace"
        workspace.mkdir()
        self._map([(workspace, "seat-completed-retired")])
        plan = self._prepare_real_g2_plan()
        key = "fu1-g2-fix4-completed-retired-retry"
        completed = self._commit_real_g2(plan, key)
        before = self._related_g2_rows(key)
        registry.retire("seat-completed-retired")

        retried = self._commit_real_g2(plan, key)

        self.assertEqual(completed["last_receipt_id"], retried["last_receipt_id"])
        self.assertEqual(before, self._related_g2_rows(key))

    def test_g2_fix4_completed_retry_allows_later_trust_and_enable_remediation(self) -> None:
        """Pins terminal evidence to its plan while allowing later owner remediation."""

        from floati.registry import Registry

        Registry(self.root).register("seat-remediated", "Codex")
        workspace = self.base / "remediated-workspace"
        workspace.mkdir()
        self._map([(workspace, "seat-remediated")])
        trust_key = f"{self.hooks_path}:stop:0:0"
        plan = self._prepare_real_g2_plan(
            trust_state=(
                "[hooks.state."
                + json.dumps(trust_key)
                + "]\nenabled = false\n"
            ).encode("utf-8")
        )
        consequence = plan["seat_binding_consequences"][0]
        self.assertFalse(consequence["observed_enabled"])
        self.assertIsNone(consequence["observed_trusted_hash"])
        key = "fu1-g2-fix4-completed-remediated-retry"
        completed = self._commit_real_g2(plan, key)
        before = self._related_g2_rows(key)
        self.hooks_path.with_name("config.toml").write_text(
            "[hooks.state."
            + json.dumps(consequence["hook_trust_key"])
            + "]\nenabled = true\ntrusted_hash = "
            + json.dumps(consequence["target_hook_hash"])
            + "\n",
            encoding="utf-8",
        )

        retried = self._commit_real_g2(plan, key)

        self.assertEqual(completed["last_receipt_id"], retried["last_receipt_id"])
        self.assertEqual(before, self._related_g2_rows(key))

    def test_g2_fix4_two_binding_post_pre_response_loss_resumes_exactly(self) -> None:
        """Proves the sole legal mixed phase prefix survives response loss."""

        plan, key = self._prepare_two_binding_post_pre_saga(
            "fu1-g2-fix4-post-pre-resume"
        )
        result = self._commit_real_g2(plan, key)
        waiter_rows = [
            row
            for row in self._related_g2_rows(key)
            if row.get("step_kind") == "waiter_binding"
        ]

        self.assertEqual("g2_committed_pending_later_gates", result["state"])
        self.assertEqual([2, 3], [row["step_ordinal"] for row in waiter_rows])
        self.assertEqual(
            ["recovered_post_state", "applied"],
            [row["commit_disposition"] for row in waiter_rows],
        )

    def test_g2_fix4_post_prefix_raw_trust_drift_refuses_before_pre_suffix(self) -> None:
        """Catches a post prefix laundering changed trust facts into its missing receipt."""

        plan, key = self._prepare_two_binding_post_pre_saga(
            "fu1-g2-fix4-post-prefix-drift"
        )
        first = plan["seat_binding_consequences"][0]
        config = Path(str(first["configuration"])).with_name("config.toml")
        config.write_text(
            "[hooks.state."
            + json.dumps(first["hook_trust_key"])
            + "]\nenabled = false\n",
            encoding="utf-8",
        )
        before = self._zero_write_snapshot(self.base)

        with self.assertRaises(ProtocolRefusal) as caught:
            self._commit_real_g2(plan, key)
        self.assertEqual("fleet_update_owner_review_invalid", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.base))

    def test_g2_fix4_pre_suffix_raw_trust_drift_refuses_before_rebind(self) -> None:
        """Catches fresh staging silently replacing the plan's pre-suffix testimony."""

        plan, key = self._prepare_two_binding_post_pre_saga(
            "fu1-g2-fix4-pre-suffix-drift"
        )
        second_configuration = plan["waiter_bindings"][1]["configuration"]
        second = next(
            row
            for row in plan["seat_binding_consequences"]
            if row["configuration"] == second_configuration
        )
        config = Path(str(second["configuration"])).with_name("config.toml")
        config.write_text(
            "[hooks.state."
            + json.dumps(second["hook_trust_key"])
            + "]\nenabled = false\n",
            encoding="utf-8",
        )
        before = self._zero_write_snapshot(self.base)

        with self.assertRaises(ProtocolRefusal) as caught:
            self._commit_real_g2(plan, key)
        self.assertEqual("fleet_update_owner_review_invalid", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.base))

    def test_g2_fix4_pre_post_waiter_topology_refuses_before_start(self) -> None:
        """Catches a later binding reaching post before its physical predecessor."""

        from floati.codex_hook_install import plan_waiter_rebind, stage_waiter_runtime

        self._two_by_two()
        plan = self._prepare_real_g2_plan()
        key = "fu1-g2-fix4-pre-post-order"
        started = FleetUpdateReceiptLedger(self.root).start(plan, self.actor, key)
        second = plan["waiter_bindings"][1]
        stage_waiter_runtime(
            self.target_source,
            Path(str(second["store"])),
            str(plan["target_waiter_digest"]),
        )
        staged = plan_waiter_rebind(
            Path(str(second["configuration"])),
            Path(str(second["store"])),
            str(plan["target_waiter_digest"]),
        )
        Path(str(second["configuration"])).write_bytes(staged["after"])
        before = self._zero_write_snapshot(self.base)

        with self.assertRaises(ProtocolRefusal) as caught:
            self._commit_real_g2(plan, key)
        self.assertEqual("fleet_update_owner_review_invalid", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.base))
        self.assertEqual([started], self._related_g2_rows(key))

    def test_g2_fix4_start_reobserves_owner_review_eligibility(self) -> None:
        """Catches an active consequence surviving a physical registry retirement."""

        from floati.registry import Registry

        registry = Registry(self.root)
        registry.register("seat-retired", "Codex")
        workspace = self.base / "retired-after-preview"
        workspace.mkdir()
        self._map([(workspace, "seat-retired")])
        plan = self._preview()
        self._consent()
        registry.retire("seat-retired")
        before = self._zero_write_snapshot(self.root.path)

        with self.assertRaises(ProtocolRefusal) as caught:
            FleetUpdateReceiptLedger(self.root).start(
                plan, self.actor, "fu1-g2-fix4-retired-review"
            )
        self.assertEqual("fleet_update_owner_review_invalid", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.root.path))

    def test_g2_fix4_stage_compares_hook_observation_to_plan_consequences(self) -> None:
        """Catches staging comparing two fresh observers while ignoring the plan witness."""

        from floati.deploy import DeploymentWriter
        from floati.fleet_update import stage_fleet_update

        self._two_by_two()
        plan = self._preview()
        forged = self._forge_owner_review_fact(
            plan,
            field="observed_enabled",
            value=not plan["seat_binding_consequences"][0]["observed_enabled"],
        )
        before = self._zero_write_snapshot(self.base)

        with (
            mock.patch("floati.fleet_update._require_detached_source"),
            mock.patch.object(
                DeploymentWriter,
                "stage",
                return_value={"source_sha": str(plan["target_source_sha"])},
            ),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            stage_fleet_update(
                plan=forged,
                target_source=self.target_source,
                installer_executable=canonical_executable(),
                git_executable="/usr/bin/git",
            )
        self.assertEqual("fleet_update_owner_review_invalid", caught.exception.code)
        self.assertEqual(before, self._zero_write_snapshot(self.base))

    def test_g2_fix4_final_reobservation_rejects_hook_drift_before_start(self) -> None:
        """Catches hook trust changing after pure stage but before the durable boundary."""

        from floati import fleet_update
        from floati.registry import Registry

        Registry(self.root).register("seat-final", "Codex")
        workspace = self.base / "final-workspace"
        workspace.mkdir()
        self._map([(workspace, "seat-final")])
        plan = self._prepare_real_g2_plan()
        consequence = plan["seat_binding_consequences"][0]
        config = Path(str(consequence["configuration"])).with_name("config.toml")
        destination_before = _tree_bytes(self.destination)
        hooks_before = Path(str(consequence["configuration"])).read_bytes()
        original = fleet_update._require_detached_source
        calls = 0

        def drift_after_final_git(*args: object, **kwargs: object) -> object:
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 2:
                config.write_text(
                    "[hooks.state."
                    + json.dumps(consequence["hook_trust_key"])
                    + "]\nenabled = false\n",
                    encoding="utf-8",
                )
            return result

        key = "fu1-g2-fix4-final-hook-drift"
        with (
            mock.patch(
                "floati.fleet_update._require_detached_source",
                side_effect=drift_after_final_git,
            ),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            self._commit_real_g2(plan, key)
        self.assertEqual(2, calls)
        self.assertEqual("fleet_update_owner_review_invalid", caught.exception.code)
        self.assertEqual([], self._related_g2_rows(key))
        self.assertEqual(destination_before, _tree_bytes(self.destination))
        self.assertEqual(
            hooks_before, Path(str(consequence["configuration"])).read_bytes()
        )

    def test_g2_fix4_post_observer_rejects_trust_drift_before_waiter_receipt(self) -> None:
        """Catches the physical step observer appending facts that differ from the plan."""

        from floati.registry import Registry

        Registry(self.root).register("seat-post-observer", "Codex")
        workspace = self.base / "post-observer-workspace"
        workspace.mkdir()
        self._map([(workspace, "seat-post-observer")])
        plan = self._prepare_real_g2_plan()
        consequence = plan["seat_binding_consequences"][0]
        trust = Path(str(consequence["configuration"])).with_name("config.toml")
        tripped = False

        def drift_after_observation(event: str) -> None:
            nonlocal tripped
            if event == "after_waiter_trust_observation" and not tripped:
                tripped = True
                trust.write_text(
                    "[hooks.state."
                    + json.dumps(consequence["hook_trust_key"])
                    + "]\nenabled = false\n",
                    encoding="utf-8",
                )

        key = "fu1-g2-fix4-post-observer-drift"
        with self.assertRaises(ProtocolRefusal) as caught:
            self._commit_real_g2(plan, key, drift_after_observation)
        self.assertTrue(tripped)
        self.assertEqual("fleet_update_owner_review_invalid", caught.exception.code)
        rows = self._related_g2_rows(key)
        self.assertEqual(
            ["fleet_update_started", "fleet_update_step"],
            [row["kind"] for row in rows],
        )
        self.assertEqual("shared_install", rows[-1]["step_kind"])

    def test_g2_fix4_same_key_execution_is_root_exclusive_and_retries_after_release(self) -> None:
        """Catches one exact retry entering the writer while its predecessor still runs."""

        plan = self._prepare_real_g2_plan()
        key = "fu1-g2-fix4-same-key-exclusion"
        entered = threading.Event()
        release = threading.Event()
        first_results: list[dict[str, object]] = []
        first_errors: list[BaseException] = []

        def hold_first(event: str) -> None:
            if event == "before_shared_writer_run":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("first execution was not released")

        def run_first() -> None:
            try:
                first_results.append(self._commit_real_g2(plan, key, hold_first))
            except BaseException as exc:  # captured for deterministic main-thread evidence
                first_errors.append(exc)

        first = threading.Thread(target=run_first)
        first.start()
        self.assertTrue(entered.wait(10), "first execution did not reach its held writer seam")

        def reject_overlap(event: str) -> None:
            if event == "before_shared_writer_run":
                raise ProtocolRefusal(
                    "test_execution_overlap",
                    "a second execution reached the writer while the first was held",
                )

        try:
            with self.assertRaises(ProtocolRefusal) as caught:
                self._commit_real_g2(plan, key, reject_overlap)
            self.assertEqual("fleet_update_execution_contended", caught.exception.code)
        finally:
            release.set()
            first.join(20)
        self.assertFalse(first.is_alive(), "first execution did not terminate after release")
        self.assertEqual([], first_errors)
        self.assertEqual(1, len(first_results))

        retried = self._commit_real_g2(plan, key)
        self.assertEqual(first_results[0]["last_receipt_id"], retried["last_receipt_id"])

    def test_g2_fix4_different_actor_execution_is_root_exclusive_before_start(self) -> None:
        """Catches actor-local receipt locks permitting two writers on one Floati root."""

        from floati.fleet_update import commit_fleet_update_g2, preview_fleet_update

        first_plan = self._prepare_real_g2_plan()
        other_actor = "worker-other"
        second_plan = preview_fleet_update(
            root=self.root,
            actor=other_actor,
            destination=self.destination,
            target_source=self.target_source,
            target_source_sha=str(first_plan["target_source_sha"]),
            channel=self.channel,
            version=self.version,
            binding_path=self.binding_path,
            transport_registry=self.registry_path,
            transport_name=self.transport,
        )
        entered = threading.Event()
        release = threading.Event()
        first_errors: list[BaseException] = []

        def hold_first(event: str) -> None:
            if event == "before_shared_writer_run":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("first execution was not released")

        def run_first() -> None:
            try:
                self._commit_real_g2(
                    first_plan, "fu1-g2-fix4-first-actor", hold_first
                )
            except BaseException as exc:  # captured for deterministic main-thread evidence
                first_errors.append(exc)

        first = threading.Thread(target=run_first)
        first.start()
        self.assertTrue(entered.wait(10), "first actor did not reach its held writer seam")

        def reject_overlap(event: str) -> None:
            if event == "before_shared_writer_run":
                raise ProtocolRefusal(
                    "test_execution_overlap",
                    "another actor reached the writer while the first was held",
                )

        try:
            with self.assertRaises(ProtocolRefusal) as caught:
                commit_fleet_update_g2(
                    plan=second_plan,
                    root=self.root,
                    actor=other_actor,
                    idempotency_key="fu1-g2-fix4-second-actor",
                    target_source=self.target_source,
                    installer_executable=canonical_executable(),
                    git_executable="/usr/bin/git",
                    _fault_hook=reject_overlap,
                )
            self.assertEqual("fleet_update_execution_contended", caught.exception.code)
        finally:
            release.set()
            first.join(20)
        self.assertFalse(first.is_alive(), "first actor did not terminate after release")
        self.assertEqual([], first_errors)
        self.assertEqual([], FleetUpdateReceiptLedger(self.root).rows(other_actor))

    def test_g2_fix4_execution_guard_rejects_a_replaced_root_identity(self) -> None:
        """Catches the root-wide guard following a symlinked root after validation."""

        plan = self._prepare_real_g2_plan()
        original = self.root.path.with_name(self.root.path.name + "-original")
        replacement = self.root.path.with_name(self.root.path.name + "-replacement")
        self.root.path.rename(original)
        replacement.mkdir()
        self.root.path.symlink_to(replacement, target_is_directory=True)
        try:
            with self.assertRaises(ProtocolRefusal) as caught:
                self._commit_real_g2(
                    plan, "fu1-g2-fix4-symlinked-execution-root"
                )
            self.assertEqual("fleet_update_execution_lock_invalid", caught.exception.code)
            self.assertEqual([], list(replacement.iterdir()))
        finally:
            if self.root.path.is_symlink():
                self.root.path.unlink()
            original.rename(self.root.path)

    def test_g2_fix4_public_ledger_entrypoints_share_the_root_execution_guard(self) -> None:
        """Catches direct start/retry bypassing the root-wide execution exclusion."""

        from floati.fleet_update import preview_fleet_update

        first_plan = self._prepare_real_g2_plan()
        other_actor = "worker-public"
        other_plan = preview_fleet_update(
            root=self.root,
            actor=other_actor,
            destination=self.destination,
            target_source=self.target_source,
            target_source_sha=str(first_plan["target_source_sha"]),
            channel=self.channel,
            version=self.version,
            binding_path=self.binding_path,
            transport_registry=self.registry_path,
            transport_name=self.transport,
        )
        key = "fu1-g2-fix4-public-held"
        entered = threading.Event()
        release = threading.Event()
        first_errors: list[BaseException] = []

        def hold_first(event: str) -> None:
            if event == "before_shared_writer_run":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("held public-entry execution was not released")

        def run_first() -> None:
            try:
                self._commit_real_g2(first_plan, key, hold_first)
            except BaseException as exc:
                first_errors.append(exc)

        first = threading.Thread(target=run_first)
        first.start()
        self.assertTrue(entered.wait(10), "held execution did not reach writer seam")
        codes: list[str | None] = []
        ledger = FleetUpdateReceiptLedger(self.root)
        try:
            for operation in (
                lambda: ledger.exact_retry(first_plan, self.actor, key),
                lambda: ledger.start(
                    other_plan, other_actor, "fu1-g2-fix4-public-other"
                ),
            ):
                try:
                    operation()
                except ProtocolRefusal as exc:
                    codes.append(exc.code)
                else:
                    codes.append(None)
        finally:
            release.set()
            first.join(20)
        self.assertFalse(first.is_alive())
        self.assertEqual([], first_errors)
        self.assertEqual(
            ["fleet_update_execution_contended", "fleet_update_execution_contended"],
            codes,
        )
        self.assertEqual([], FleetUpdateReceiptLedger(self.root).rows(other_actor))

    def test_g2_fix4_execution_guard_releases_after_commit_exception(self) -> None:
        """Proves a failed owner releases the root coordinate for exact recovery."""

        plan = self._prepare_real_g2_plan()
        key = "fu1-g2-fix4-lock-release"
        tripped = False

        def interrupt(event: str) -> None:
            nonlocal tripped
            if event == "before_shared_writer_run" and not tripped:
                tripped = True
                raise OSError("fixture guarded execution interruption")

        with self.assertRaisesRegex(OSError, "fixture guarded execution interruption"):
            self._commit_real_g2(plan, key, interrupt)
        recovered = self._commit_real_g2(plan, key)
        self.assertTrue(tripped)
        self.assertEqual("g2_committed_pending_later_gates", recovered["state"])

    def test_g2_fix4_fork_child_cannot_release_parent_execution_guard(self) -> None:
        """Catches a fork child unlocking its parent's shared open-file description."""

        from floati.fleet_update_receipts import _FleetUpdateExecutionGuard

        fork_context = multiprocessing.get_context("fork")
        spawn_context = multiprocessing.get_context("spawn")
        guard = _FleetUpdateExecutionGuard(self.root)
        guard.acquire()
        try:
            child_release = self._guard_process_outcome(
                fork_context, _release_inherited_fleet_update_guard, guard
            )
            while_parent_holds = self._guard_process_outcome(
                spawn_context, _attempt_fleet_update_guard, str(self.root.path)
            )
        finally:
            guard.release()
        after_parent_release = self._guard_process_outcome(
            spawn_context, _attempt_fleet_update_guard, str(self.root.path)
        )

        self.assertEqual(
            ("refused", "fleet_update_execution_lock_invalid"), child_release
        )
        self.assertEqual(
            ("refused", "fleet_update_execution_contended"), while_parent_holds
        )
        self.assertEqual(("ok", None), after_parent_release)

    def test_g2_fix4_wrong_thread_cannot_release_execution_guard(self) -> None:
        """Catches a same-process non-owner thread dropping the root exclusion."""

        from floati.fleet_update_receipts import _FleetUpdateExecutionGuard

        outcomes: list[tuple[str, str | None]] = []

        def release_from_wrong_thread() -> None:
            try:
                guard.release()
            except ProtocolRefusal as exc:
                outcomes.append(("refused", exc.code))
            else:
                outcomes.append(("ok", None))

        guard = _FleetUpdateExecutionGuard(self.root)
        guard.acquire()
        releaser = threading.Thread(target=release_from_wrong_thread)
        releaser.start()
        releaser.join(10)
        self.assertFalse(releaser.is_alive())
        try:
            while_owner_holds = self._guard_process_outcome(
                multiprocessing.get_context("spawn"),
                _attempt_fleet_update_guard,
                str(self.root.path),
            )
        finally:
            guard.release()
        after_owner_release = self._guard_process_outcome(
            multiprocessing.get_context("spawn"),
            _attempt_fleet_update_guard,
            str(self.root.path),
        )

        self.assertEqual(
            [("refused", "fleet_update_execution_lock_invalid")], outcomes
        )
        self.assertEqual(
            ("refused", "fleet_update_execution_contended"), while_owner_holds
        )
        self.assertEqual(("ok", None), after_owner_release)

    def test_g2_fix4_parent_guard_survives_root_leaf_replacement(self) -> None:
        """Catches a replaced root leaf splitting cooperating execution locks."""

        plan = self._prepare_real_g2_plan()
        displaced = self.root.path.with_name(self.root.path.name + "-displaced")
        destination_before = _tree_bytes(self.destination)
        hook_before = self.hooks_path.read_bytes()
        contender: list[tuple[str, str | None]] = []
        replacement_contents: list[str] = []
        replaced = False

        def replace_root_at_writer(event: str) -> None:
            nonlocal replaced
            if event != "before_shared_writer_run" or replaced:
                return
            replaced = True
            self.root.path.rename(displaced)
            self.root.path.mkdir()
            contender.append(
                self._guard_process_outcome(
                    multiprocessing.get_context("spawn"),
                    _attempt_fleet_update_guard,
                    str(self.root.path),
                )
            )

        try:
            with self.assertRaises(ProtocolRefusal) as caught:
                self._commit_real_g2(
                    plan,
                    "fu1-g2-fix4-root-leaf-replaced",
                    replace_root_at_writer,
                )
            self.assertEqual(
                "fleet_update_execution_lock_invalid", caught.exception.code
            )
            replacement_contents = sorted(
                path.relative_to(self.root.path).as_posix()
                for path in self.root.path.rglob("*")
            )
        finally:
            if replaced:
                shutil.rmtree(self.root.path)
                displaced.rename(self.root.path)

        self.assertTrue(replaced)
        self.assertEqual(
            [("refused", "fleet_update_execution_contended")], contender
        )
        self.assertEqual([], replacement_contents)
        self.assertEqual(destination_before, _tree_bytes(self.destination))
        self.assertEqual(hook_before, self.hooks_path.read_bytes())

    def test_g2_fix4_shared_unchanged_is_rejected_by_schema_and_runtime(self) -> None:
        """Catches runtime admitting a shared disposition the standard schema forbids."""

        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import ValidationError

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        key = "fu1-g2-fix4-shared-unchanged"
        started = ledger.start(plan, self.actor, key)
        shared = self._append_observed_step(
            ledger, plan, self.actor, key, str(started["id"])
        )
        hostile = json.loads(json.dumps(shared))
        hostile["commit_disposition"] = "unchanged"
        schema = json.loads(
            (REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)

        with self.assertRaises(ValidationError):
            validator.validate(hostile)
        with self.assertRaises((ProtocolRefusal, IntegrityFailure)):
            validate_record(
                hostile,
                self.root.tenant_id,
                frozenset({"fleet_update_step"}),
                integrity=True,
            )

    def test_g2_fix4_jsonl_rejects_shared_unchanged_before_history_projection(self) -> None:
        """Catches the shared JSONL reader preserving the runtime/schema mismatch."""

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        key = "fu1-g2-fix4-jsonl-unchanged"
        started = ledger.start(plan, self.actor, key)
        shared = self._append_observed_step(
            ledger, plan, self.actor, key, str(started["id"])
        )
        hostile = json.loads(json.dumps(shared))
        hostile["commit_disposition"] = "unchanged"
        path = self.root.path / ledger.relative(self.actor)
        path.write_text(
            json.dumps(started, sort_keys=True)
            + "\n"
            + json.dumps(hostile, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaises((ProtocolRefusal, IntegrityFailure)):
            ledger.rows(self.actor)

    def test_g2_fix4_epoch_and_pin_discriminators_are_exact_schema_branches(self) -> None:
        """Catches independent enum sets accepting crossed non-G2 receipt shapes."""

        from jsonschema import Draft202012Validator

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        key = "fu1-g2-fix4-step-discriminators"
        started = ledger.start(plan, self.actor, key)
        steps = self._append_canonical_steps(
            ledger, plan, self.actor, key, str(started["id"])
        )
        by_kind = {
            row["step_kind"]: row
            for row in steps
            if row["step_kind"] in {"epoch_roll", "transport_pins"}
        }
        self.assertEqual({"epoch_roll", "transport_pins"}, set(by_kind))
        schema = json.loads(
            (REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)

        for kind, valid in by_kind.items():
            peer = "transport_pins" if kind == "epoch_roll" else "epoch_roll"
            validator.validate(valid)
            validate_record(
                valid,
                self.root.tenant_id,
                frozenset({"fleet_update_step"}),
                integrity=True,
            )
            for field in ("step_kind", "step_coordinate", "step_evidence"):
                hostile = json.loads(json.dumps(valid))
                if field == "step_kind":
                    hostile[field] = peer
                else:
                    hostile[field]["kind"] = peer
                with self.subTest(kind=kind, field=field):
                    runtime_refused = False
                    try:
                        validate_record(
                            hostile,
                            self.root.tenant_id,
                            frozenset({"fleet_update_step"}),
                            integrity=True,
                        )
                    except (ProtocolRefusal, IntegrityFailure):
                        runtime_refused = True
                    self.assertTrue(runtime_refused)
                    self.assertTrue(
                        list(validator.iter_errors(hostile)),
                        "official Draft 2020-12 schema accepted crossed discriminators",
                    )

    def test_g2_fix3_real_receipts_normalize_losslessly_to_raw_standard_shapes(self) -> None:
        """Proves old derived claims and new raw facts project to identical observations."""

        from jsonschema import Draft202012Validator
        from floati.fleet_update_receipts import normalize_fleet_update_receipt

        plan = self._prepare_real_g2_plan()
        key = "fu1-g2-fix3-real-receipt-migration"
        self._commit_real_g2(plan, key)
        new_rows = self._related_g2_rows(key)
        old_rows = json.loads(json.dumps(new_rows))

        new_shared = next(
            row
            for row in new_rows
            if row.get("kind") == "fleet_update_step"
            and row.get("step_kind") == "shared_install"
        )
        self.assertEqual(
            ".floati-install/manifest.v0.json",
            new_shared["step_coordinate"]["metadata_relative"],
        )
        self.assertNotIn("metadata", new_shared["step_coordinate"])
        old_shared = next(
            row
            for row in old_rows
            if row.get("kind") == "fleet_update_step"
            and row.get("step_kind") == "shared_install"
        )
        old_shared["step_coordinate"].pop("metadata_relative")
        old_shared["step_coordinate"]["metadata"] = str(
            self._installed_metadata_path()
        )

        new_waiter = next(
            row
            for row in new_rows
            if row.get("kind") == "fleet_update_step"
            and row.get("step_kind") == "waiter_binding"
        )
        new_observation = new_waiter["step_evidence"]["hook_post_observation"]
        self.assertNotIn("hook_armed", new_observation)
        old_waiter = next(
            row
            for row in old_rows
            if row.get("kind") == "fleet_update_step"
            and row.get("step_kind") == "waiter_binding"
        )
        old_observation = old_waiter["step_evidence"]["hook_post_observation"]
        old_observation["hook_armed"] = (
            old_observation["observed_enabled"] is True
            and old_observation["observed_trusted_hash"]
            == old_observation["current_hook_hash"]
        )

        self.assertEqual(
            [normalize_fleet_update_receipt(row) for row in new_rows],
            [normalize_fleet_update_receipt(row) for row in old_rows],
        )

        schema_path = REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        for row in new_rows:
            validator.validate(row)
            validate_json_schema(row, schema_path)
            validate_record(
                row,
                self.root.tenant_id,
                frozenset(
                    {
                        "fleet_update_started",
                        "fleet_update_step",
                        "fleet_update_completed",
                    }
                ),
                integrity=True,
            )

        receipt_path = self.root.path / FleetUpdateReceiptLedger.relative(self.actor)
        receipt_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in new_rows),
            encoding="utf-8",
        )
        self.assertEqual(new_rows, FleetUpdateReceiptLedger(self.root).rows(self.actor))

        for label, mutation in (
            (
                "null-phase",
                lambda row: row.update(
                    step_ordinal=None,
                    step_coordinate=None,
                    commit_disposition=None,
                    step_evidence=None,
                ),
            ),
            (
                "invented-metadata",
                lambda row: row["step_coordinate"].update(
                    metadata=str(self.base / "invented-metadata.json")
                ),
            ),
        ):
            hostile = json.loads(json.dumps(new_shared))
            mutation(hostile)
            with self.subTest(label=label):
                with self.assertRaises(Exception):
                    validator.validate(hostile)
                with self.assertRaises((ProtocolRefusal, IntegrityFailure)):
                    validate_record(
                        hostile,
                        self.root.tenant_id,
                        frozenset({"fleet_update_step"}),
                        integrity=False,
                    )

        invented_armed = json.loads(json.dumps(new_waiter))
        invented_armed["step_evidence"]["hook_post_observation"]["hook_armed"] = True
        with self.assertRaises(Exception):
            validator.validate(invented_armed)
        with self.assertRaises((ProtocolRefusal, IntegrityFailure)):
            validate_record(
                invented_armed,
                self.root.tenant_id,
                frozenset({"fleet_update_step"}),
                integrity=False,
            )

        for enabled, trusted in (
            (True, new_observation["current_hook_hash"]),
            (True, None),
            (False, new_observation["current_hook_hash"]),
            (None, new_observation["current_hook_hash"]),
        ):
            truth_row = json.loads(json.dumps(new_waiter))
            observation = truth_row["step_evidence"]["hook_post_observation"]
            observation["observed_enabled"] = enabled
            observation["observed_trusted_hash"] = trusted
            facts = normalize_fleet_update_receipt(truth_row)
            self.assertEqual(
                enabled is True and trusted == observation["current_hook_hash"],
                facts["step_evidence"]["hook_post_observation"]["hook_armed"],
            )

        wrong_legacy = json.loads(json.dumps(old_waiter))
        wrong_legacy["step_evidence"]["hook_post_observation"]["hook_armed"] = (
            not old_observation["hook_armed"]
        )
        with self.assertRaises(ProtocolRefusal):
            normalize_fleet_update_receipt(wrong_legacy)

    def test_g2_fix3_public_step_refuses_caller_selected_state_and_evidence(self) -> None:
        """Catches the public ledger recording caller testimony without physical proof."""

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        key = "fu1-g2-fix3-caller-testimony"
        started = ledger.start(plan, self.actor, key)
        receipt_path = self.root.path / ledger.relative(self.actor)
        before = receipt_path.read_bytes()
        invented = {
            "kind": "shared_install",
            "journal_path": str(
                self.destination
                / ".floati-install"
                / "invented-wiring-journal.v1.jsonl"
            ),
            "join_id": "3" * 64,
            "predecessor_ordinal": None,
            "predecessor_entry_hash": None,
            "first_ordinal": 1,
            "last_ordinal": 1,
            "entry_hashes": ["4" * 64],
        }
        with self.assertRaises((ProtocolRefusal, TypeError)):
            ledger.step(
                plan,
                self.actor,
                key,
                "shared_install",
                "1" * 64,
                "2" * 64,
                str(started["id"]),
                commit_disposition="unchanged",
                step_evidence=invented,
            )
        self.assertEqual(before, receipt_path.read_bytes())

    def test_g2_fix3_shared_recovery_requires_exact_planned_operation(self) -> None:
        """Catches complete and partial restart accepting replace for a planned create."""

        from floati import wiring_journal
        from floati.deploy import render_install_metadata
        from floati.fleet_update import _shared_install_recovery_evidence

        plan = self._prepare_real_g2_plan()
        current = json.loads(
            self._installed_metadata_path().read_text(encoding="utf-8")
        )
        current_paths = {row["path"] for row in current["files"]}
        target = json.loads(
            (self.target_source / "bundle-manifest.v0.json").read_text(
                encoding="utf-8"
            )
        )
        planned_metadata = render_install_metadata(
            destination=self.destination,
            source_ref="HEAD",
            source_sha=str(plan["target_source_sha"]),
            entries=target["files"],
            entrypoint_sha256=next(
                row["sha256"]
                for row in target["files"]
                if row["path"] == "scripts/floati"
            ),
            previous_ownership=current["ownership"],
        )
        intents = [
            {
                "kind": "file",
                "op": "replace" if row["path"] in current_paths else "create",
                "path": str(self.destination / row["path"]),
                "sha256": row["sha256"],
            }
            for row in target["files"]
        ] + [
            {
                "kind": "file",
                "op": "replace",
                "path": str(self._installed_metadata_path()),
                "sha256": hashlib.sha256(planned_metadata).hexdigest(),
            }
        ]
        self.assertEqual("create", intents[0]["op"])
        join_id = "5" * 64

        def append_intent(intent: dict[str, object], *, op: str | None = None) -> None:
            wiring_journal.append_entry(
                self.destination,
                {
                    "v": 1,
                    "ts": "2026-08-30T00:00:00Z",
                    "actor": {"command": "update", "floatiVersion": "fixture"},
                    "action": "update",
                    "kind": intent["kind"],
                    "path": intent["path"],
                    "op": op or intent["op"],
                    "sha256": intent["sha256"],
                    "join_id": join_id,
                },
            )

        append_intent(intents[0], op="replace")
        partial = _shared_install_recovery_evidence(
            self.destination,
            self.target_source,
            str(plan["current_manifest_sha256"]),
            str(plan["target_manifest_sha256"]),
            join_id,
            intents,
        )
        self.assertEqual("divergent", partial["phase"])

        for row in target["files"]:
            path = self.destination / row["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.target_source / row["path"], path)
        self._installed_metadata_path().write_bytes(planned_metadata)
        for intent in intents[1:]:
            append_intent(intent)
        complete = _shared_install_recovery_evidence(
            self.destination,
            self.target_source,
            str(plan["current_manifest_sha256"]),
            str(plan["target_manifest_sha256"]),
            join_id,
            intents,
        )
        self.assertEqual("divergent", complete["phase"])

    def test_g2_fix3_explicit_git_ignores_decoy_ambient_repository(self) -> None:
        """Catches GIT_DIR and GIT_WORK_TREE redirecting any explicit Git observer."""

        from floati.deploy import _git as deploy_git
        from floati.fleet_update import _require_detached_source
        from floati.manifest import _git_tracked_files

        source = self.base / "git-source"
        decoy = self.base / "git-decoy"

        def repository(path: Path, relative: str, message: str) -> str:
            path.mkdir()
            for argv in (
                ("/usr/bin/git", "init", "--quiet"),
                ("/usr/bin/git", "config", "user.name", "Floati Test"),
                (
                    "/usr/bin/git",
                    "config",
                    "user.email",
                    "floati-test@example.invalid",
                ),
            ):
                subprocess.run(argv, cwd=path, check=True)
            member = path / relative
            member.parent.mkdir(parents=True)
            member.write_text("tracked\n", encoding="utf-8")
            subprocess.run(("/usr/bin/git", "add", "."), cwd=path, check=True)
            subprocess.run(
                ("/usr/bin/git", "commit", "--quiet", "-m", message),
                cwd=path,
                check=True,
            )
            subprocess.run(
                ("/usr/bin/git", "checkout", "--quiet", "--detach"),
                cwd=path,
                check=True,
            )
            return subprocess.run(
                ("/usr/bin/git", "rev-parse", "HEAD"),
                cwd=path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        source_sha = repository(source, "floati/source.py", "source")
        decoy_sha = repository(decoy, "floati/decoy.py", "decoy")
        self.assertNotEqual(source_sha, decoy_sha)
        poisoned = {
            "GIT_DIR": str(decoy / ".git"),
            "GIT_WORK_TREE": str(source),
            "GIT_INDEX_FILE": str(decoy / ".git" / "index"),
            "GIT_OBJECT_DIRECTORY": str(decoy / ".git" / "objects"),
        }
        with mock.patch.dict(os.environ, poisoned, clear=False):
            with self.subTest(surface="detached-source"):
                _require_detached_source(source, "/usr/bin/git", source_sha)
            with self.subTest(surface="deployment"):
                self.assertEqual(
                    source_sha,
                    deploy_git(
                        source,
                        ("rev-parse", "--verify", "HEAD^{commit}"),
                        executable="/usr/bin/git",
                    ),
                )
            with self.subTest(surface="manifest"):
                self.assertEqual(
                    {"floati/source.py"},
                    _git_tracked_files(source, git_executable="/usr/bin/git"),
                )

    def test_g2_fix3_dangling_waiter_target_refuses_in_pure_stage(self) -> None:
        """Catches lexists-false dangling targets crossing the zero-write start fence."""

        from floati.deploy import DeploymentWriter
        from floati.fleet_update import stage_fleet_update

        plan = self._preview()
        store = Path(str(plan["waiter_bindings"][0]["store"]))
        target = store / str(plan["target_waiter_digest"])
        missing = store / "missing-waiter-generation"
        target.symlink_to(missing, target_is_directory=True)
        before = _tree_bytes(self.base)
        with (
            mock.patch("floati.fleet_update._require_detached_source"),
            mock.patch.object(
                DeploymentWriter,
                "stage",
                return_value={"source_sha": str(plan["target_source_sha"])},
            ),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            stage_fleet_update(
                plan=plan,
                target_source=self.target_source,
                installer_executable=canonical_executable(),
                git_executable="/usr/bin/git",
            )
        self.assertEqual("fleet_update_waiter_invalid", caught.exception.code)
        self.assertTrue(target.is_symlink())
        self.assertEqual(str(missing), os.readlink(target))
        self.assertEqual(before, _tree_bytes(self.base))
        self.assertEqual([], FleetUpdateReceiptLedger(self.root).rows(self.actor))

    def test_g2_fix3_final_prestart_recheck_rejects_injected_dangling_target(self) -> None:
        """Catches a target injected during the last Git check reaching start or writer."""

        from floati.fleet_update import commit_fleet_update_g2
        import floati.fleet_update as fleet_update

        plan = self._prepare_real_g2_plan()
        store = Path(str(plan["waiter_bindings"][0]["store"]))
        target = store / str(plan["target_waiter_digest"])
        missing = store / "missing-final-recheck-target"
        destination_before = _tree_bytes(self.destination)
        original = fleet_update._require_detached_source
        calls = 0

        def inject_after_final_check(source: Path, git: str, expected: str) -> None:
            nonlocal calls
            original(source, git, expected)
            calls += 1
            if calls == 2:
                target.symlink_to(missing, target_is_directory=True)

        with (
            mock.patch(
                "floati.fleet_update._require_detached_source",
                side_effect=inject_after_final_check,
            ),
            mock.patch.object(
                FleetUpdateReceiptLedger,
                "start",
                side_effect=AssertionError("start crossed final waiter target fence"),
            ),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            commit_fleet_update_g2(
                plan=plan,
                root=self.root,
                actor=self.actor,
                idempotency_key="fu1-g2-fix3-final-target",
                target_source=self.target_source,
                installer_executable=canonical_executable(),
                git_executable="/usr/bin/git",
            )
        self.assertEqual("fleet_update_waiter_invalid", caught.exception.code)
        self.assertEqual(2, calls)
        self.assertTrue(target.is_symlink())
        self.assertEqual(str(missing), os.readlink(target))
        self.assertEqual(destination_before, _tree_bytes(self.destination))
        self.assertEqual([], FleetUpdateReceiptLedger(self.root).rows(self.actor))
        self.assertFalse(
            any(path.name.startswith(".floati-fleet-stage-") for path in store.iterdir())
        )

    def test_g2_fix3_waiter_generation_replace_race_is_typed_and_no_overwrite(self) -> None:
        """Catches a post-start materialization race escaping raw or replacing a foreign link."""

        from floati.codex_hook_install import stage_waiter_runtime

        plan = self._preview()
        store = Path(str(plan["waiter_bindings"][0]["store"]))
        target = store / str(plan["target_waiter_digest"])
        missing = store / "raced-missing-generation"

        def race(event: str) -> None:
            if event == "before_waiter_generation_replace":
                target.symlink_to(missing, target_is_directory=True)

        with self.assertRaises(ProtocolRefusal) as caught:
            stage_waiter_runtime(
                self.target_source,
                store,
                str(plan["target_waiter_digest"]),
                _fault_hook=race,
            )
        self.assertEqual("fleet_update_waiter_invalid", caught.exception.code)
        self.assertTrue(target.is_symlink())
        self.assertEqual(str(missing), os.readlink(target))
        self.assertFalse(
            any(path.name.startswith(".floati-fleet-stage-") for path in store.iterdir())
        )

    def test_g2_24_preexisting_later_target_divergence_refuses_before_any_generation(self) -> None:
        """Catches a valid first waiter materializing before a later existing target is verified."""

        from floati.deploy import DeploymentWriter
        from floati.fleet_update import stage_fleet_update

        self._two_by_two(); plan = self._preview()
        first, second = (Path(str(row["store"])) for row in plan["waiter_bindings"])
        target = str(plan["target_waiter_digest"])
        (second / target).mkdir(); (second / target / "partial").write_text("bad\n", encoding="utf-8")
        first_before = self._zero_write_snapshot(first)
        with (
            mock.patch("floati.fleet_update._require_detached_source"),
            mock.patch.object(DeploymentWriter, "stage", return_value={"source_sha": "b" * 40}),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            stage_fleet_update(plan=plan, target_source=self.target_source, installer_executable=canonical_executable(), git_executable="/usr/bin/git")
        self.assertEqual("fleet_update_waiter_invalid", caught.exception.code)
        self.assertEqual(first_before, self._zero_write_snapshot(first))
        self.assertFalse((first / target).exists())
        self.assertEqual([], FleetUpdateReceiptLedger(self.root).rows(self.actor))

    def test_g2_25_dirty_detached_source_refuses_before_start(self) -> None:
        """Catches a target dirtied after staging reaching the G2 durable start boundary."""

        from floati.fleet_update import _require_detached_source

        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "source"; source.mkdir()
            for argv in (("git", "init", "--quiet"), ("git", "config", "user.name", "test"), ("git", "config", "user.email", "test@example.invalid")):
                subprocess.run(argv, cwd=source, check=True)
            (source / "one").write_text("one\n", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=source, check=True)
            subprocess.run(("git", "commit", "--quiet", "-m", "one"), cwd=source, check=True)
            subprocess.run(("git", "checkout", "--quiet", "--detach"), cwd=source, check=True)
            sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=source, check=True, text=True, capture_output=True).stdout.strip()
            (source / "one").write_text("dirty\n", encoding="utf-8")
            with self.assertRaises(ProtocolRefusal) as caught:
                _require_detached_source(source, "/usr/bin/git", sha)
            self.assertEqual("fleet_update_source_sha_invalid", caught.exception.code)

    def test_g2_00_exposes_a_non_mutating_staging_boundary(self) -> None:
        """Catches G2 committing a target before every named target is verified."""

        import floati.fleet_update as fleet_update

        self.assertTrue(
            callable(getattr(fleet_update, "stage_fleet_update", None)),
            "G2 must expose an explicit non-mutating staging boundary",
        )

    def test_g2_01_two_by_two_nullable_trust_testimony_reaches_start_receipt(self) -> None:
        """Catches receipt validation rejecting the actual nullable Codex trust observation."""

        self._two_by_two()
        self._consent()
        plan = self._preview()
        started = FleetUpdateReceiptLedger(self.root).start(
            plan, self.actor, "fu1-g2-nullable-trust"
        )

        self.assertEqual("fleet_update_started", started["kind"])
        self.assertEqual(4, len(started["seat_binding_consequences"]))
        self.assertIsNone(started["seat_binding_consequences"][0]["observed_trusted_hash"])

    def test_g2_02_receipt_rejects_a_tampered_plan_body_with_its_old_digest(self) -> None:
        """Catches receipt authorization trusting caller plan fields beyond the digest."""

        self._consent()
        plan = self._preview()
        forged = json.loads(json.dumps(plan))
        forged["inputs"]["destination"] = str(self.base / "other-destination")

        with self.assertRaises(ProtocolRefusal) as caught:
            authenticate_plan(forged, self.actor)
        self.assertEqual("fleet_update_plan_invalid", caught.exception.code)

    def test_g2_03_start_receipt_carries_required_null_step_identity_fields(self) -> None:
        """Catches DRAFT receipt rows omitting the phase-aware step contract."""

        self._consent()
        started = FleetUpdateReceiptLedger(self.root).start(
            self._preview(), self.actor, "fu1-g2-step-shape"
        )
        self.assertEqual(
            (None, None, None, None),
            (started["step_ordinal"], started["step_coordinate"], started["commit_disposition"], started["step_evidence"]),
        )

    def test_g2_04_shared_post_recovery_requires_one_exact_contiguous_join(self) -> None:
        """Catches a post-install retry accepting missing, partial, or unrelated journal evidence."""

        from floati import wiring_journal
        from floati.fleet_update import _shared_install_recovery_evidence

        plan = self._preview()
        join_id = "c" * 64
        destination = self.destination
        source = self.target_source
        self.assertEqual(
            "pre",
            _shared_install_recovery_evidence(
                destination, source, str(plan["current_manifest_sha256"]),
                str(plan["target_manifest_sha256"]), join_id,
                plan["shared_install_intents"],
            )["phase"],
        )

        current = json.loads(
            (destination / ".floati-install" / "manifest.v0.json").read_text(encoding="utf-8")
        )
        target = json.loads((source / "bundle-manifest.v0.json").read_text(encoding="utf-8"))
        from floati.deploy import render_install_metadata
        planned = render_install_metadata(
            destination=destination,
            source_ref="HEAD",
            source_sha=str(plan["target_source_sha"]),
            entries=target["files"],
            entrypoint_sha256=next(row["sha256"] for row in target["files"] if row["path"] == "scripts/floati"),
            previous_ownership=current["ownership"],
        )
        for row in target["files"]:
            relative = row["path"]
            target_path = destination / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target_path)
        metadata_path = destination / ".floati-install" / "manifest.v0.json"
        metadata_path.write_bytes(planned)

        self.assertEqual(
            "divergent",
            _shared_install_recovery_evidence(
                destination, source, str(plan["current_manifest_sha256"]),
                str(plan["target_manifest_sha256"]), join_id,
                plan["shared_install_intents"],
            )["phase"],
        )
        for row in target["files"]:
            wiring_journal.append_entry(destination, {
                "v": 1, "ts": "2026-08-30T00:00:00Z", "actor": {"command": "update", "floatiVersion": "fixture"},
                "action": "update", "kind": "file", "path": str(destination / row["path"]),
                "op": "replace", "sha256": row["sha256"], "join_id": join_id,
            })
        wiring_journal.append_entry(destination, {
            "v": 1, "ts": "2026-08-30T00:00:00Z", "actor": {"command": "update", "floatiVersion": "fixture"},
            "action": "update", "kind": "file", "path": str(metadata_path),
            "op": "replace", "sha256": hashlib.sha256(planned).hexdigest(), "join_id": join_id,
        })
        recovered = _shared_install_recovery_evidence(
            destination, source, str(plan["current_manifest_sha256"]),
            str(plan["target_manifest_sha256"]), join_id,
            plan["shared_install_intents"],
        )
        self.assertEqual("post", recovered["phase"])
        self.assertEqual(join_id, recovered["evidence"]["join_id"])

    def test_g2_05_shared_step_persists_and_retries_exact_join_evidence(self) -> None:
        """Catches a shared-install receipt that loses its recovery proof on retry."""

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        key = "fu1-g2-shared-evidence"
        started = ledger.start(plan, self.actor, key)
        first = self._append_observed_step(
            ledger, plan, self.actor, key, str(started["id"])
        )
        retried = self._append_observed_step(
            ledger, plan, self.actor, key, str(started["id"])
        )
        self.assertEqual(first, retried)
        self.assertEqual(
            self._synthetic_step_evidence(
                plan,
                self.actor,
                key,
                {
                    "step_kind": first["step_kind"],
                    "step_coordinate": first["step_coordinate"],
                },
            ),
            first["step_evidence"],
        )

    def test_g2_06_shared_writer_runs_once_and_receipt_retry_skips_it(self) -> None:
        """Catches retry writing a second shared install instead of proving its receipt post-state."""

        from floati.deploy import DeploymentWriter
        from floati.fleet_update import commit_fleet_update_g2, _shared_install_join_id

        self._consent()
        (self.destination / "LICENSE").write_bytes(b"current fixture license\n")
        current_metadata_path = self.destination / ".floati-install" / "manifest.v0.json"
        current_metadata = json.loads(current_metadata_path.read_text(encoding="utf-8"))
        current_metadata["files"].append({
            "path": "LICENSE",
            "sha256": hashlib.sha256((self.destination / "LICENSE").read_bytes()).hexdigest(),
        })
        current_metadata["files"].sort(key=lambda row: row["path"])
        current_metadata_path.write_text(
            json.dumps(current_metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        registry["transports"]["floati-installed-v0"]["manifest_sha256"] = hashlib.sha256(
            current_metadata_path.read_bytes()
        ).hexdigest()
        self.registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_path = self.target_source / "bundle-manifest.v0.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append({
            "path": "LICENSE",
            "sha256": hashlib.sha256((self.target_source / "LICENSE").read_bytes()).hexdigest(),
        })
        manifest["files"].sort(key=lambda row: row["path"])
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        plan = self._preview()
        for argv in (
            ("git", "init", "--quiet"),
            ("git", "config", "user.name", "Floati Test"),
            ("git", "config", "user.email", "floati-test@example.invalid"),
            ("git", "add", "."),
            ("git", "commit", "--quiet", "-m", "target"),
        ):
            subprocess.run(argv, cwd=self.target_source, check=True)
        key = "fu1-g2-writer-once"
        writer_joins: list[object] = []
        original_run = DeploymentWriter.run

        def counted_run(writer: object) -> object:
            writer_joins.append(getattr(writer, "join_id", None))
            return original_run(writer)

        waiter_result = {
            "pre_digest": "1" * 64,
            "post_digest": "2" * 64,
            "trust_observation": {},
        }
        with (
            mock.patch.object(DeploymentWriter, "run", new=counted_run),
            mock.patch.object(DeploymentWriter, "_check_currency", return_value="b" * 40),
            mock.patch("floati.fleet_update._require_detached_source"),
            mock.patch("floati.deploy.enumerate_installer_shadow", return_value={
                "outcome": "affirmative_none", "reason": "fixture", "enumerated_roots": [], "found": [],
            }),
            mock.patch("floati.fleet_update.stage_fleet_update", return_value={
                "source": str(self.target_source), "git_executable": "/usr/bin/git",
                "installer_executable": canonical_executable(), "waiters": [],
            }),
            mock.patch("floati.fleet_update.stage_waiter_runtime"),
            mock.patch("floati.fleet_update.commit_waiter_rebind", return_value=waiter_result),
        ):
            first = commit_fleet_update_g2(
                plan=plan, root=self.root, actor=self.actor, idempotency_key=key,
                target_source=self.target_source, installer_executable=canonical_executable(),
                git_executable="/usr/bin/git",
            )
            second = commit_fleet_update_g2(
                plan=plan, root=self.root, actor=self.actor, idempotency_key=key,
                target_source=self.target_source, installer_executable=canonical_executable(),
                git_executable="/usr/bin/git",
            )

        self.assertEqual("g2_committed_pending_later_gates", first["state"])
        self.assertEqual(first["last_receipt_id"], second["last_receipt_id"])
        self.assertEqual(
            [_shared_install_join_id(str(plan["plan_digest"]), self.actor, key)], writer_joins
        )

    def test_g2_07_shared_journal_evidence_has_an_exact_runtime_and_schema_shape(self) -> None:
        """Catches receipt evidence that accepts an incomplete or ambiguous journal join."""

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        started = ledger.start(plan, self.actor, "fu1-g2-journal-shape")
        receipt = self._append_observed_step(
            ledger, plan, self.actor, "fu1-g2-journal-shape",
            str(started["id"]),
        )
        malformed = json.loads(json.dumps(receipt))
        malformed["step_evidence"].pop("entry_hashes")
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            validate_record(
                malformed, self.root.tenant_id,
                frozenset({"fleet_update_started", "fleet_update_step", "fleet_update_completed"}),
                integrity=True,
            )
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(
                malformed,
                REPOSITORY_ROOT / "schemas/v1/fleet-update-receipt.schema.json",
            )

    def test_g2_08_retry_refuses_a_tampered_shared_step_coordinate(self) -> None:
        """Catches exact retry accepting a receipt moved to another installation destination."""

        self._consent()
        plan = self._preview()
        ledger = FleetUpdateReceiptLedger(self.root)
        key = "fu1-g2-coordinate-tamper"
        started = ledger.start(plan, self.actor, key)
        self._append_observed_step(
            ledger, plan, self.actor, key, str(started["id"])
        )
        receipt_path = self.root.path / ledger.relative(self.actor)
        rows = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines()]
        rows[1]["step_coordinate"]["destination"] = str(self.base / "forged")
        receipt_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        with self.assertRaises(IntegrityFailure) as caught:
            self._append_observed_step(
                ledger, plan, self.actor, key, str(started["id"])
            )
        self.assertEqual("fleet_update_step_invalid", caught.exception.code)

    def test_g2_09_waiter_recovery_classifies_exact_pre_and_post_without_writing(self) -> None:
        """Catches a crash-seam retry guessing hook state instead of byte-classifying it."""

        from floati.codex_hook_install import plan_waiter_rebind
        from floati.codex_hook_trust import observe_codex_waiter_hooks
        from floati.fleet_update import _waiter_recovery_evidence

        plan = self._preview()
        binding = plan["waiter_bindings"][0]
        staged = plan_waiter_rebind(
            Path(str(binding["configuration"])), Path(str(binding["store"])),
            str(plan["target_waiter_digest"]),
        )
        staged["hook_trust_key"] = observe_codex_waiter_hooks(
            Path(str(binding["configuration"]))
        )[0]["hook_trust_key"]
        self.assertEqual("pre", _waiter_recovery_evidence(staged)["phase"])
        Path(str(staged["configuration"])).write_bytes(staged["after"])
        post = _waiter_recovery_evidence(staged)
        self.assertEqual("post", post["phase"])
        self.assertEqual("waiter_binding", post["evidence"]["kind"])
        self.assertIn("hook_post_observation", post["evidence"])

    def test_g2_10_later_invalid_store_leaves_all_store_trees_and_entries_unchanged(self) -> None:
        """Catches materializing an earlier waiter generation before all stores preflight."""

        from floati.deploy import DeploymentWriter
        from floati.fleet_update import stage_fleet_update

        self._two_by_two()
        plan = self._preview()
        second = Path(str(plan["waiter_bindings"][1]["store"]))
        before = {
            str(Path(str(row["store"]))): sorted(
                path.relative_to(Path(str(row["store"]))).as_posix()
                for path in Path(str(row["store"])).rglob("*")
            )
            for row in plan["waiter_bindings"]
        }
        second.rename(second.with_name("invalid-store"))
        second.symlink_to(second.with_name("invalid-store"), target_is_directory=True)
        with (
            mock.patch("floati.fleet_update._require_detached_source"),
            mock.patch.object(DeploymentWriter, "stage", return_value={"source_sha": "b" * 40}),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            stage_fleet_update(
                plan=plan, target_source=self.target_source,
                installer_executable=canonical_executable(), git_executable="/usr/bin/git",
            )
        self.assertEqual("fleet_update_waiter_binding_invalid", caught.exception.code)
        for store, entries in before.items():
            path = Path(store)
            if path.is_symlink():
                path = path.resolve(strict=True)
            self.assertEqual(entries, sorted(item.relative_to(path).as_posix() for item in path.rglob("*")))
            self.assertFalse(any(item.name.startswith(".floati-fleet-stage-") for item in path.iterdir()))

    def test_g2_11_attached_source_is_refused_by_the_explicit_git_coordinate(self) -> None:
        """Catches a branch checkout reaching G2 mutation planning."""

        from floati.fleet_update import _require_detached_source

        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "source"
            source.mkdir()
            for argv in (("git", "init", "--quiet"), ("git", "config", "user.name", "test"), ("git", "config", "user.email", "test@example.invalid")):
                subprocess.run(argv, cwd=source, check=True)
            (source / "one").write_text("one\n", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=source, check=True)
            subprocess.run(("git", "commit", "--quiet", "-m", "one"), cwd=source, check=True)
            sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=source, check=True, text=True, capture_output=True).stdout.strip()
            before = {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file() and ".git" not in path.parts}
            with self.assertRaises(ProtocolRefusal) as caught:
                _require_detached_source(source, "/usr/bin/git", sha)
            self.assertEqual("fleet_update_source_sha_invalid", caught.exception.code)
            self.assertEqual(before, {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file() and ".git" not in path.parts})

    def test_g2_12_detached_source_head_drift_refuses_before_commit_boundary(self) -> None:
        """Catches a target changing after pure staging but before the mutating boundary."""

        from floati.fleet_update import _require_detached_source

        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "source"
            source.mkdir()
            for argv in (("git", "init", "--quiet"), ("git", "config", "user.name", "test"), ("git", "config", "user.email", "test@example.invalid")):
                subprocess.run(argv, cwd=source, check=True)
            (source / "one").write_text("one\n", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=source, check=True)
            subprocess.run(("git", "commit", "--quiet", "-m", "one"), cwd=source, check=True)
            staged_sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=source, check=True, text=True, capture_output=True).stdout.strip()
            subprocess.run(("git", "checkout", "--quiet", "--detach"), cwd=source, check=True)
            (source / "one").write_text("drift\n", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=source, check=True)
            subprocess.run(("git", "commit", "--quiet", "-m", "drift"), cwd=source, check=True)
            before = (source / "one").read_bytes()
            with self.assertRaises(ProtocolRefusal) as caught:
                _require_detached_source(source, "/usr/bin/git", staged_sha)
            self.assertEqual("fleet_update_source_sha_invalid", caught.exception.code)
            self.assertEqual(before, (source / "one").read_bytes())


if __name__ == "__main__":
    unittest.main()
