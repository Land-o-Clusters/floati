"""One native control set for both waiter adapters; no borrowed TestCase setup."""
from pathlib import Path
import tempfile
import unittest

from tests import probe_waiter_projection_memo as probe
from tests.temp_roots import REAL_TEMP_ROOT


class WaiterProjectionMemoTests(unittest.TestCase):
    def test_rolled_projection_allows_same_lane_append_and_revalidates(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import current_thread
        from unittest.mock import patch
        from floati import cursor_presentations, wake_hold
        from floati.errors import IntegrityFailure, ProtocolRefusal
        from floati.events import EventLog
        from floati.jsonl import _locked_path

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            cases = (("codex", "append"), ("cursor", "append"),
                     ("controller", "append"), ("controller", "ack"))
            for adapter, change in cases:
                with self.subTest(adapter=adapter, change=change):
                    fixture = probe.NativeFixture(Path(temporary) / f"{adapter}-{change}",
                        "codex" if adapter == "controller" else adapter)
                    fixture.seed_rolled()
                    original = fixture.send("first-fresh")
                    module = wake_hold if adapter == "controller" else cursor_presentations
                    project = module.project_wake_items
                    locked_path = wake_hold._locked_path
                    reader = current_thread()
                    contributed = []
                    refused = []

                    def write():
                        if adapter == "cursor":
                            relative = Path("state/wake-control") / (fixture.node + ".lock")
                            with fixture.guarded(), _locked_path(fixture.root.resolve_relative(relative),
                                    exclusive=True, relative=relative, timeout_seconds=0, order_tracked=False):
                                pass
                        if change == "ack":
                            return EventLog(fixture.root).drain(
                                fixture.node, acting_session_id=probe.SESSION)[2]
                        return fixture.send("concurrent-fresh")

                    def lock(*args, **kwargs):
                        if current_thread() is not reader:
                            kwargs["timeout_seconds"] = 0
                        return locked_path(*args, **kwargs)

                    def project_then_append(**kwargs):
                        states = project(**kwargs)
                        if current_thread() is reader and not (contributed or refused):
                            try:
                                contributed.append(executor.submit(write).result(timeout=3))
                            except ProtocolRefusal as exc:
                                refused.append(str(exc))
                        return states

                    def evaluate():
                        if adapter == "controller":
                            return str(wake_hold.WakeHoldController(fixture.root).evaluate(
                                fixture.node, idempotency_key="rolled-projection-race"))
                        return fixture.adapter()["stdout"]

                    with ThreadPoolExecutor(max_workers=1) as executor,\
                            patch.object(wake_hold, "_locked_path", lock),\
                            patch.object(module, "project_wake_items", project_then_append):
                        try:
                            output = evaluate()
                        except IntegrityFailure:
                            output = ""
                        except ProtocolRefusal as exc:
                            self.fail(f"same-lane append could not complete during projection: {exc}")
                    self.assertTrue(contributed, refused)
                    if change == "ack":
                        self.assertIn(original["id"], contributed[0]["item_ids"])
                        self.assertNotIn(original["id"], output, "publication used the stale ACK cut")
                        self.assertFalse(any(row.get("idempotency_key") == "rolled-projection-race"
                                             for row in fixture.deliveries()))
                    elif original["id"] in output:
                        self.assertIn(contributed[0]["id"], output, "publication used the stale cut")
                    else:
                        retried = evaluate()
                        self.assertIn(original["id"], retried)
                        self.assertIn(contributed[0]["id"], retried)

    def controls(self, name):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            results = probe.run_controls(Path(temporary), groups=(name,))
        for adapter, result in results.items():
            with self.subTest(adapter=adapter):
                self.assertNotIn("harness_failure", result, result.get("harness_failure"))
                self.assertTrue(result["checks"], "native controls produced no observations")
                for check, passed in result["checks"].items():
                    with self.subTest(check=check):
                        self.assertTrue(passed, result.get("observations"))

    def test_unchanged_rolled_history_reuses_native_admission(self):
        self.controls("unchanged")

    def test_supported_suffixes_extend_without_full_replay(self):
        self.controls("append_suffix")

    def test_rolled_foreign_suffixes_reuse_proven_current_and_historical_state(self):
        self.controls("rolled_foreign_append")

    def test_rolled_foreign_suffixes_readmit_changed_imported_proof_contracts(self):
        self.controls("rolled_foreign_contract")

    def test_public_mutable_graphs_cannot_poison_private_admission(self):
        self.controls("public_aliases")

    def test_every_physical_change_keeps_native_observation(self):
        self.controls("physical_changes")

    def test_byte_reuse_preserves_protected_coordinates_and_public_readback(self):
        self.controls("protected_bytes")

    def test_invalid_suffixes_keep_exact_native_refusal(self):
        self.controls("invalid_suffix")

    def test_current_authority_is_never_memoized(self):
        self.controls("authority_changes")

    def test_changed_semantics_invalidate_prior_admission(self):
        self.controls("semantic_changes")

    def test_imported_hold_proof_is_reissued_for_new_full_source(self):
        self.controls("imported_proof_binding")

    def test_partial_publication_never_retries_old_envelopes(self):
        self.controls("publication_failures")

    def test_controller_admission_is_distinct_and_keeps_refusal_precedence(self):
        self.controls("controller_admission")

    def test_both_native_adapters_preserve_poll_and_owner_lifecycle(self):
        self.controls("adapter_lifecycle")

    def test_inplace_workspace_lifecycle_keeps_original_output_contract(self):
        self.controls("workspace_inplace_lifecycle")

    def test_native_atomic_workspace_withdrawal_is_additional(self):
        self.controls("workspace_atomic_lifecycle")

    def test_inplace_workspace_bytes_force_full_native_admission(self):
        self.controls("workspace_map_admission")


if __name__ == "__main__":
    unittest.main()
