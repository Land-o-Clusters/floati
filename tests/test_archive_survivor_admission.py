"""Cold admission from actual native roll/plan and scratch committed bytes."""
from copy import deepcopy
import copy
from datetime import datetime, timezone
import hashlib
import os
import re
import shutil
import tempfile
import types
from pathlib import Path
import unittest
from unittest import mock

from floati import archive_survivor_admission as admission, archive_catalog as commits
from floati import archive_catalog_entries as catalog, archive_source_pool as pool
from floati import archive_survivor_pool, archive_survivor_bundle as bundle, bus_epoch
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.framing import decode_frames, encode_frame
from floati.ids import uuid7_hex
from floati.records import validate_record
from floati.root import FloatiRoot
from floati.cursor import SparseCursor
from floati import consumption, events, workers, worker_bootstrap_protocol, root as root_module
from floati import readiness_contract
from tests import test_archive_catalog_entries as fixture
from tests.temp_roots import REAL_TEMP_ROOT


class ArchiveSurvivorAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = cls()
        cls.addClassCleanup(cls.template.doCleanups)
        cls.template.set_up_native()
        cls.bank = cls.template.fixture.fixture.base / "admission-template"
        shutil.copytree(cls.template.root.path, cls.bank)

    def setUp(self):
        if self._testMethodName == "test_absent_generation_is_none_without_initializing_state":
            temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
            self.addCleanup(temporary.cleanup)
            self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
            return
        template = self.template
        shutil.rmtree(template.root.path)
        shutil.copytree(self.bank, template.root.path)
        self.fixture = copy.copy(template.fixture)
        self.fixture.fixture = copy.copy(template.fixture.fixture)
        self.root = template.root
        self.sources = template.sources
        self.catalog = template.catalog
        self.survivors = template.survivors
        self.ledger = template.ledger
        self.record = deepcopy(template.record)

    def set_up_native(self):
        self.fixture = fixture.ArchiveCatalogEntryTests()
        self.fixture.set_up_native()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.sources = archive_survivor_pool.encode_survivor_source_pool(self.fixture.plan)
        self.catalog = catalog.encode_catalog(self.fixture.entries, self.fixture.sources)
        self.survivors = bundle.encode_survivor_bundle(self.sources, self.root.tenant_id, self.fixture.plan)
        self.ledger = self.root.path / commits.LEDGER
        self.install()

    def install(self, *, catalog_chunks=None, source_chunks=None, survivor_chunks=None, previous=None):
        chunks = {"catalog": self.catalog if catalog_chunks is None else catalog_chunks,
                  "sources": self.sources if source_chunks is None else source_chunks,
                  "survivor": self.survivors if survivor_chunks is None else survivor_chunks}
        descriptors = []
        for domain in sorted(chunks):
            for ordinal, raw in enumerate(chunks[domain]):
                sha = hashlib.sha256(raw).hexdigest()
                path = self.root.path / commits.CHUNK_DIRECTORY / (sha + ".jsonl")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                descriptors.append({"domain": domain, "ordinal": ordinal, "byte_length": len(raw), "sha256": sha})
        live = (self.root.path / "events.jsonl").read_bytes()
        row = {"schema_version": 1, "kind": commits.KIND,
            "id": "epoch-history-commit-" + uuid7_hex(), "tenant_id": self.root.tenant_id,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "actor": "sender", "trigger": "ack", "verifier_version": 1,
            "policy": {"schema_version": 1, "keep_forever": False},
            "live_anchor": {"byte_length": len(live), "sha256": hashlib.sha256(live).hexdigest()},
            "previous_generation": previous["generation"] if previous is not None else None,
            "source_commitment": {"chunk_count": len(chunks["sources"]),
                "sha256": hashlib.sha256(commits._SOURCES_DOMAIN + b"".join(chunks["sources"])).hexdigest()},
            "chunks": descriptors}
        row["generation"] = commits.generation_digest(row)
        validate_record(row, self.root.tenant_id, {commits.KIND}, integrity=True)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_bytes((encode_frame(previous) if previous is not None else b"") + encode_frame(row))
        self.record = row
        return row

    def refused(self, operation, *, unsupported=False):
        error = None
        try:
            operation()
        except Exception as caught:
            error = caught
        self.assertIsInstance(error, ProtocolRefusal if unsupported else IntegrityFailure)
        self.assertEqual("archive_survivor_admission_unsupported" if unsupported else
                         "archive_survivor_admission_invalid", error.code)

    def test_cold_real_generation_replays_without_original_archive_reads(self):
        with mock.patch.object(bus_epoch, "_read_epoch_archive_payloads", side_effect=AssertionError("cold read touched original archive")):
            handle = admission.admit_committed_survivors(self.root)
            data = handle.read()
            self.assertEqual(self.record["generation"], handle.generation)
            self.assertEqual(self.record, data["commit"])
            self.assertEqual(2, len(data["catalog"]))
            self.assertEqual(3, len(data["survivors"]["projection"]["epochs"]))
            self.assertNotIn("authority", data)
            data["catalog"].clear()
            self.assertEqual(2, len(handle.read()["catalog"]))
        # Restoring a patched native callable changes the captured contract.
        self.refused(handle.read)

    def test_integral_float_chunk_lengths_preserve_full_cold_generation(self):
        for descriptor in self.record["chunks"]:
            descriptor["byte_length"] = float(descriptor["byte_length"])
        self.record["generation"] = commits.generation_digest(self.record)
        validate_record(self.record, self.root.tenant_id, {commits.KIND}, integrity=False)
        wire = encode_frame(self.record)
        self.ledger.write_bytes(wire)
        try:
            handle = admission.admit_committed_survivors(self.root)
            result = handle.read()
        except IntegrityFailure as failure:
            self.fail("accepted integral chunk lengths lost cold survivor admission: " + failure.code)
        self.assertEqual(wire, encode_frame(result["commit"]))
        self.assertEqual(self.record["generation"], handle.generation)
        self.assertEqual(2, len(result["catalog"]))
        self.assertEqual(wire, self.ledger.read_bytes())

    def test_absent_generation_is_none_without_initializing_state(self):
        before = sorted(str(path) for path in self.root.path.rglob("*"))
        self.assertIsNone(admission.admit_committed_survivors(self.root))
        self.assertEqual(before, sorted(str(path) for path in self.root.path.rglob("*")))

    def test_cold_admission_preserves_complete_source_axes_as_detached_data(self):
        handle = admission.admit_committed_survivors(self.root)
        result = handle.read()
        self.assertIn("sources", result,
            "cold admission discarded exact source axes needed by history queries")
        actual = {entry["key"]: entry for entry in result["sources"]}
        self.assertEqual(set(self.fixture.sources.keys), set(actual))
        for key in self.fixture.sources.keys:
            self.assertEqual(self.fixture.sources.entry(key), actual[key])
        self.assertEqual({"frame", "epoch", "axis", "cut"},
                         {entry["tag"] for entry in result["sources"]})
        result["sources"].clear()
        self.assertEqual(len(actual), len(handle.read()["sources"]))

    def test_source_data_is_read_from_initially_bound_native_chunks(self):
        # An admission must use its own directly observed chunks. A supplied
        # PoolData or a later reparse is never a replacement native source.
        native = pool.read_pool
        reads = []
        def observed(chunks, tenant):
            chunks = tuple(chunks)
            reads.append(chunks)
            return native(chunks, tenant)
        with mock.patch.object(pool, "read_pool", observed):
            handle = admission.admit_committed_survivors(self.root)
            before = len(reads)
            result = handle.read()
            self.assertIn("sources", result,
                "registered cold result does not retain complete source data")
            self.assertEqual(before, len(reads))
            self.assertEqual(set(self.fixture.sources.keys),
                             {entry["key"] for entry in result["sources"]})

    def test_caller_data_byte_handle_and_unregistered_handles_never_admit(self):
        byte_handle = commits.read_committed_generation(self.root)
        for supplied in (self.record, byte_handle, {"root": self.root}):
            self.refused(lambda: admission.admit_committed_survivors(supplied))
        handle = admission.admit_committed_survivors(self.root)
        for supplied in ({"generation": handle.generation}, byte_handle, object.__new__(type(handle))):
            self.refused(lambda: admission._admission_identity(self.root, supplied))

    def test_handle_copy_subclass_and_cross_root_are_not_registered(self):
        handle = admission.admit_committed_survivors(self.root)
        try:
            duplicate = copy.copy(handle)
        except (TypeError, IntegrityFailure):
            duplicate = object.__new__(type(handle))
        class Child(type(handle)):
            pass
        for supplied in (duplicate, object.__new__(Child)):
            self.refused(lambda: admission._admission_identity(self.root, supplied))
        other = FloatiRoot.open_direct_home(self.fixture.fixture.base / "other-home", create=True)
        self.refused(lambda: admission._admission_identity(other, handle))

    def test_same_bytes_ledger_or_chunk_replacement_invalidates_handle(self):
        for target in (self.ledger, self.root.path / commits.CHUNK_DIRECTORY /
                       (self.record["chunks"][0]["sha256"] + ".jsonl")):
            with self.subTest(target=target.name):
                handle = admission.admit_committed_survivors(self.root)
                replacement = self.fixture.fixture.base / "replacement"
                replacement.write_bytes(target.read_bytes())
                replacement.replace(target)
                self.refused(handle.read)

    def test_runtime_verifier_contract_change_invalidates_handle(self):
        handle = admission.admit_committed_survivors(self.root)
        for owner, name in (
            (bundle, "verify_survivor_bundle"),
            (admission, "_bound"),
            (bus_epoch, "_epoch_holds"),
            (bus_epoch, "_epoch_descriptor"),
            (bus_epoch, "_acquire_epoch_descriptor"),
        ):
            with self.subTest(helper=name), mock.patch.object(
                owner, name, side_effect=AssertionError("changed verifier must not run")
            ) as candidate:
                self.refused(handle.read)
                candidate.assert_not_called()

    def test_missing_leaf_or_entire_survivor_domain_cannot_borrow_byte_commit(self):
        for chunks in (self.survivors[:-1], ()):
            self.install(survivor_chunks=chunks)
            self.refused(lambda: admission.admit_committed_survivors(self.root))

    def test_dangling_catalog_frame_and_missing_catalog_epoch_refuse(self):
        rows = deepcopy(self.fixture.entries)
        rows[0]["roll_frame"] = "f" * 64
        rows[0]["key"] = catalog.entry_key(rows[0])
        raw = (b"".join(encode_frame(row) for row in sorted(rows, key=lambda row: row["key"])),)
        self.install(catalog_chunks=raw)
        self.refused(lambda: admission.admit_committed_survivors(self.root))
        self.install(catalog_chunks=(encode_frame(self.fixture.entries[0]),))
        self.refused(lambda: admission.admit_committed_survivors(self.root))

    def test_off_lineage_source_roll_requires_exact_retry_catalog_census(self):
        rolled = next(row for row in (self.fixture.sources.entry(key)["record"]
            for key in self.fixture.sources.by_tag("frame"))
            if row["kind"] == "bus_epoch_roll_receipt" and "automatic" in row)
        orphan = self.root.path / "archive-admission-off-lineage-fixture"
        orphan.mkdir()
        (orphan / "events.jsonl").write_bytes(encode_frame(rolled))
        message = self.fixture.fixture.send("admission-new-genesis")
        (self.root.path / "events.jsonl").write_bytes(encode_frame(dict(message["message"])))
        self.fixture.refresh()
        self.sources = archive_survivor_pool.encode_survivor_source_pool(self.fixture.plan)
        self.catalog = catalog.encode_catalog(self.fixture.entries, self.fixture.sources)
        self.survivors = bundle.encode_survivor_bundle(self.sources, self.root.tenant_id, self.fixture.plan)
        self.assertTrue(all(row["scope"] == "off_lineage" for row in self.fixture.entries))
        omitted = tuple(encode_frame(row) for row in sorted(self.fixture.entries[1:], key=lambda row: row["key"]))
        self.install(catalog_chunks=omitted)
        self.refused(lambda: admission.admit_committed_survivors(self.root))

    def test_aggregate_ancestry_bound_refuses_before_eager_chunk_reader(self):
        first = deepcopy(self.record)
        self.install(previous=first)
        total = sum(row["byte_length"] for row in first["chunks"])
        for controls in ({"MAX_INPUT_BYTES": total}, {"MAX_REFERENCES": 1}, {"MAX_COMMIT_RECORDS": 1}):
            with self.subTest(controls=controls), mock.patch.multiple(admission, **controls),\
                    mock.patch.object(commits, "read_committed_generation", side_effect=AssertionError("bulk chunk reader reached before ancestry quota")):
                self.refused(lambda: admission.admit_committed_survivors(self.root), unsupported=True)

    def test_changed_ancestor_only_chunk_invalidates_latest_handle(self):
        previous = deepcopy(self.record)
        old = self.root.path / commits.CHUNK_DIRECTORY / (previous["chunks"][0]["sha256"] + ".jsonl")
        # A new catalog encoding is unnecessary: distinct survivor coverage
        # testimony yields a different valid latest bundle while prior chunks
        # must still remain identity-bound.
        rows = decode_frames(self.survivors[0])
        rows[0]["source_witness"][0]["identity"][0] += 1
        changed = (encode_frame(rows[0]),) + self.survivors[1:]
        self.install(survivor_chunks=changed, previous=previous)
        handle = admission.admit_committed_survivors(self.root)
        replacement = self.fixture.fixture.base / "ancestor-replacement"
        replacement.write_bytes(old.read_bytes())
        replacement.replace(old)
        self.refused(handle.read)

    def test_changed_namespace_parent_invalidates_unchanged_leaf_commitment(self):
        handle = admission.admit_committed_survivors(self.root)
        parent = self.root.path / commits.CHUNK_DIRECTORY
        parent.chmod(0o700 if parent.stat().st_mode & 0o077 else 0o755)
        self.refused(handle.read)

    def test_native_reader_final_ledger_observation_is_precharged(self):
        cap = 2 * sum(row["byte_length"] for row in self.record["chunks"]) + 2 * len(self.ledger.read_bytes())
        with mock.patch.object(admission, "MAX_INPUT_BYTES", cap), mock.patch.object(
                commits, "read_committed_generation", side_effect=AssertionError("third ledger read not charged")):
            self.refused(lambda: admission.admit_committed_survivors(self.root), unsupported=True)

    def test_dependency_change_during_detached_read_refuses(self):
        handle = admission.admit_committed_survivors(self.root)
        native = admission.json.loads
        def changed(data, *args, **kwargs):
            self.ledger.write_bytes(self.ledger.read_bytes() + b"\n")
            return native(data, *args, **kwargs)
        with mock.patch.object(admission.json, "loads", changed):
            self.refused(handle.read)

    def test_cached_handle_rechecks_own_limits_and_accessor_contract(self):
        handle = admission.admit_committed_survivors(self.root)
        with self.subTest(change="limit"), mock.patch.object(admission, "MAX_RESULT_BYTES", 1):
            self.refused(lambda: admission._admission_identity(self.root, handle))
        with self.subTest(change="read"), mock.patch.object(type(handle), "read", lambda self: {}):
            self.refused(lambda: admission._admission_identity(self.root, handle))
        with self.subTest(change="generation"), mock.patch.object(type(handle), "generation", property(lambda self: "forged")):
            self.refused(lambda: admission._admission_identity(self.root, handle))

    def test_replaced_contract_function_cannot_replay_old_snapshot(self):
        handle = admission.admit_committed_survivors(self.root)
        old = admission._contract()
        with mock.patch.object(admission, "_contract", lambda: old):
            self.refused(lambda: admission._admission_identity(self.root, handle))

    def test_contract_replacement_during_verify_refuses_before_candidate_or_registration(self):
        native_verify, original_contract = admission._verify, admission._contract
        candidates = []
        def replace_after_verify(root, prepared):
            result = native_verify(root, prepared)
            old = original_contract()
            def replay_old_contract():
                candidates.append("untrusted contract invoked")
                return old
            admission._contract = replay_old_contract
            return result
        try:
            with mock.patch.object(admission, "_verify", replace_after_verify):
                self.refused(lambda: admission.admit_committed_survivors(self.root))
        finally:
            admission._contract = original_contract
        self.assertEqual([], candidates)

    def test_in_place_native_default_or_grammar_change_invalidates_handle(self):
        handle = admission.admit_committed_survivors(self.root)
        defaults = admission.jsonl._decode_path_records.__kwdefaults__
        with self.subTest(change="kwdefault"), mock.patch.dict(defaults, max_bytes=1):
            self.refused(lambda: admission._admission_identity(self.root, handle))
        with self.subTest(change="grammar"), mock.patch.dict(admission.records._SPECS, unexpected_history_kind=({}, set())):
            self.refused(lambda: admission._admission_identity(self.root, handle))

    def test_dynamic_native_identity_dependencies_refuse_before_candidate_executes(self):
        handle = admission.admit_committed_survivors(self.root)
        for name in ("_selected_member_identity", "_require_owned_epoch_guard"):
            with self.subTest(helper=name), mock.patch.object(bus_epoch, name,
                    side_effect=AssertionError("changed dynamic native helper executed")) as candidate:
                self.refused(lambda: admission._admission_identity(self.root, handle))
                candidate.assert_not_called()

    def test_dynamic_native_identity_code_change_invalidates_cached_handle(self):
        handle = admission.admit_committed_survivors(self.root)
        native = bus_epoch._selected_member_identity
        old_code = native.__code__
        def changed(status):
            raise AssertionError("changed native identity code executed")
        try:
            native.__code__ = changed.__code__
            self.refused(lambda: admission._admission_identity(self.root, handle))
        finally:
            native.__code__ = old_code

    def test_contract_checker_replacement_or_code_change_cannot_skip_cached_comparison(self):
        handle = admission.admit_committed_survivors(self.root)
        native = admission._check_contract
        code = native.__code__
        def noop(state):
            return None
        for mode in ("object", "code"):
            with self.subTest(change=mode):
                pool._COMMON.add("unreviewed_history_field")
                try:
                    if mode == "object":
                        with mock.patch.object(admission, "_check_contract", noop):
                            self.refused(handle.read)
                    else:
                        native.__code__ = noop.__code__
                        self.refused(handle.read)
                finally:
                    pool._COMMON.remove("unreviewed_history_field")
                    native.__code__ = code

    def test_registry_accessor_replacement_cannot_rebase_cached_state(self):
        handle = admission.admit_committed_survivors(self.root)
        original = admission._state
        code = original.__code__
        # This code runs with the actual admission module's globals in both
        # replacement modes, exposing the private saved-state trust boundary.
        def fabricated(handle):
            saved = _REGISTERED[handle]
            return _State(saved.root, saved.scope, saved.generation,
                          saved.dependencies, _contract(), b'{"forged":true}')
        candidate = types.FunctionType(fabricated.__code__, vars(admission))
        operations = (handle.read, lambda: handle.generation,
                      lambda: admission._admission_identity(self.root, handle))
        for mode in ("object", "code"):
            for operation in operations:
                with self.subTest(change=mode, operation=operation):
                    try:
                        if mode == "object":
                            with mock.patch.object(admission, "_state", candidate):
                                self.refused(operation)
                        else:
                            original.__code__ = candidate.__code__
                            self.refused(operation)
                    finally:
                        original.__code__ = code

    def test_factory_state_constructor_hooks_cannot_register_forged_data(self):
        native_type = admission._State
        initializer = native_type.__init__
        code = initializer.__code__
        def forged_type(*args):
            return native_type(*args[:-1], b'{"forged":true}')
        def initializer_factory():
            native_object = object
            def forged(self, root, scope, generation, dependencies, contract, data):
                for key, value in (("root", root), ("scope", scope), ("generation", generation),
                        ("dependencies", dependencies), ("contract", contract),
                        ("data", b'{"forged":true}')):
                    native_object.__setattr__(self, key, value)
            return forged
        forged_init = initializer_factory()
        def forged_new(cls, root, scope, generation, dependencies, contract, data):
            return types.SimpleNamespace(root=root, scope=scope, generation=generation,
                dependencies=dependencies, contract=contract, data=b'{"forged":true}')
        for mode in ("class_binding", "init_binding", "init_code", "new_binding"):
            with self.subTest(change=mode):
                try:
                    if mode == "class_binding":
                        with mock.patch.object(admission, "_State", forged_type):
                            self.refused(lambda: admission.admit_committed_survivors(self.root))
                    elif mode == "init_binding":
                        with mock.patch.object(native_type, "__init__", forged_init):
                            self.refused(lambda: admission.admit_committed_survivors(self.root))
                    elif mode == "init_code":
                        self.assertEqual(len(initializer.__closure__), len(forged_init.__closure__))
                        initializer.__code__ = forged_init.__code__
                        self.refused(lambda: admission.admit_committed_survivors(self.root))
                    else:
                        # Mutating/removing an inherited __new__ on the real
                        # type leaves CPython's allocator slot changed after
                        # mock cleanup. A replacement class carrying the same
                        # forged allocator tests this factory hook without
                        # mutating interpreter type internals across fixtures.
                        changed_type = type("ChangedState", (native_type,), {"__new__": staticmethod(forged_new)})
                        with mock.patch.object(admission, "_State", changed_type):
                            self.refused(lambda: admission.admit_committed_survivors(self.root))
                finally:
                    initializer.__code__ = code

    def test_native_sparse_cursor_session_grammar_change_invalidates_cached_handle(self):
        handle = admission.admit_committed_survivors(self.root)
        with mock.patch.object(SparseCursor, "_session_component", staticmethod(lambda worker: "forged")):
            self.refused(handle.read)

    def test_semantic_entry_trampoline_cannot_skip_factory_or_cached_guards(self):
        handle = admission.admit_committed_survivors(self.root)
        original, code = admission._semantic, admission._semantic.__code__
        def fabricated(operation):
            return {"forged": True}
        operations = (handle.read, lambda: admission._admission_identity(self.root, handle),
                      lambda: admission.admit_committed_survivors(self.root))
        for mode in ("object", "code"):
            for operation in operations:
                with self.subTest(change=mode, operation=operation):
                    try:
                        if mode == "object":
                            with mock.patch.object(admission, "_semantic", fabricated):
                                self.refused(operation)
                        else:
                            original.__code__ = fabricated.__code__
                            self.refused(operation)
                    finally:
                        original.__code__ = code

    def test_generation_checks_saved_contract_before_identity_delegation(self):
        handle = admission.admit_committed_survivors(self.root)
        original, code = admission._admission_identity, admission._admission_identity.__code__
        def noop(root, handle):
            return None
        try:
            with self.subTest(change="object"), mock.patch.object(admission, "_admission_identity", noop):
                self.refused(lambda: handle.generation)
            with self.subTest(change="code"):
                original.__code__ = noop.__code__
                self.refused(lambda: handle.generation)
        finally:
            original.__code__ = code

    def test_imported_native_frontdoor_methods_and_owner_bindings_invalidate_handle(self):
        handle = admission.admit_committed_survivors(self.root)
        owners = ((consumption.ConsumptionLedger, "project_tail"),
                  (events.EventLog, "_send_receipt"), (events.EventLog, "_saved_readiness"),
                  (root_module, "validate_identifier"),
                  (worker_bootstrap_protocol, "validate_isolation_backend"))
        for owner, name in owners:
            with self.subTest(owner=str(owner), method=name), mock.patch.object(owner, name,
                    lambda *args, **kwargs: None):
                self.refused(handle.read)

    def test_native_worker_readiness_and_class_domain_grammar_changes_refuse(self):
        handle = admission.admit_committed_survivors(self.root)
        grammars = ((workers, "WORKER_KINDS"), (consumption, "WORK_KINDS"),
                    (events, "EVENT_KINDS"), (readiness_contract, "MECHANISMS"),
                    (readiness_contract, "READINESS_REASONS"))
        for owner, name in grammars:
            current = getattr(owner, name)
            changed = current + ("unreviewed_history_field",) if type(current) is tuple else current | {"unreviewed_history_field"}
            with self.subTest(owner=owner.__name__, grammar=name), mock.patch.object(owner, name,
                    changed):
                self.refused(handle.read)
        with self.subTest(grammar="worker_next"):
            workers.WorkerReceipts._NEXT["claim"].add("unreviewed_history_field")
            try:
                self.refused(handle.read)
            finally:
                workers.WorkerReceipts._NEXT["claim"].remove("unreviewed_history_field")
        with self.subTest(grammar="worker_states"), mock.patch.dict(workers.WorkerReceipts._STATES,
                unreviewed_history_field="complete"):
            self.refused(handle.read)
        for name in ("_EVENT_DOMAIN", "_DELIVERY_DOMAIN", "_ACK_DOMAIN"):
            with self.subTest(grammar=name), mock.patch.object(admission.wake_hold.WakeHoldController,
                    name, "unreviewed-history-domain"):
                self.refused(handle.read)

    def test_imported_native_class_alias_rebinding_refuses(self):
        handle = admission.admit_committed_survivors(self.root)
        with mock.patch.object(admission.archive_projection_replay, "WakeHoldController", type("Fake", (), {})):
            self.refused(handle.read)

    def test_contract_change_in_last_native_dependency_check_refuses_before_return(self):
        native = admission._bound
        control = {"armed": False, "calls": 0, "target": 1}
        def changed(root, scope, dependencies, pending=None):
            native(root, scope, dependencies, pending)
            if control["armed"]:
                control["calls"] += 1
                if control["calls"] == control["target"]:
                    pool._COMMON.add("unreviewed_history_field")
        with mock.patch.object(admission, "_bound", changed):
            handle = admission.admit_committed_survivors(self.root)
            operations = (("read", handle.read, 2), ("generation", lambda: handle.generation, 1),
                          ("identity", lambda: admission._admission_identity(self.root, handle), 1))
            for name, operation, target in operations:
                with self.subTest(path=name):
                    control.update(armed=True, calls=0, target=target)
                    try:
                        self.refused(operation)
                    finally:
                        pool._COMMON.discard("unreviewed_history_field")
                        control["armed"] = False

    def test_contextmanager_generator_code_change_invalidates_cached_handle(self):
        handle = admission.admit_committed_survivors(self.root)
        native = commits._opened_capture.__wrapped__
        old_code = native.__code__
        def changed(root, relative, expected, *, strict=True, directory=False):
            raise AssertionError("changed descriptor generator executed")
            yield
        try:
            native.__code__ = changed.__code__
            self.refused(handle.read)
        finally:
            native.__code__ = old_code

    def test_actual_contextmanager_callable_closure_change_refuses(self):
        handle = admission.admit_committed_survivors(self.root)
        native = commits._opened_capture.__wrapped__
        cell = next(cell for cell in commits._opened_capture.__closure__
                    if cell.cell_contents is native)
        def changed(*args, **kwargs):
            raise AssertionError("changed actual contextmanager callable executed")
            yield
        try:
            cell.cell_contents = changed
            self.refused(handle.read)
        finally:
            cell.cell_contents = native

    def test_native_record_and_wake_enum_mutations_invalidate_cached_handle(self):
        handle = admission.admit_committed_survivors(self.root)
        for module, name in ((admission.wake_hold, "WAKE_EVENT_KINDS"),
                (admission.wake_hold, "_UNINTERPRETED_EVENT_KINDS"),
                (admission.records, "WORKER_REFUSAL_CODES"),
                (admission.records, "HOOK_BURN_EFFECT_CLASSES")):
            with self.subTest(grammar=name), mock.patch.object(module, name,
                    getattr(module, name) | {"unreviewed_history_field"}):
                self.refused(lambda: admission._admission_identity(self.root, handle))
        with mock.patch.dict(admission.records._EFFECT_TARGET_BY_TYPE,
                             unreviewed_history_effect="operator"):
            self.refused(lambda: admission._admission_identity(self.root, handle))

    def test_closed_source_and_leaf_regex_replacement_invalidates_cached_handle(self):
        handle = admission.admit_committed_survivors(self.root)
        owners = ((commits, "_DIGEST"), (catalog, "_HEX"), (pool, "_HEX"),
                  (bundle, "_HEX"), (admission.archive_execution_domain, "_HEX"),
                  (admission.archive_presentation_domain, "_HEX"))
        for module, name in owners:
            with self.subTest(owner=module.__name__), mock.patch.object(module, name, re.compile("never")):
                self.refused(handle.read)

    def test_native_presentation_vocabularies_invalidate_cached_handle(self):
        handle = admission.admit_committed_survivors(self.root)
        for module, name in ((admission.cursor_presentations, "_IMMUTABLE"),
                (admission.cursor_presentations, "KINDS"),
                (admission.archive_presentation_replay, "KINDS")):
            with self.subTest(owner=module.__name__, grammar=name), mock.patch.object(module, name,
                    getattr(module, name) | {"unreviewed_history_field"}):
                self.refused(handle.read)

    def test_source_and_leaf_closed_grammar_mutations_invalidate_cached_handle(self):
        handle = admission.admit_committed_survivors(self.root)
        grammars = (pool._COMMON, pool._FIELDS["frame"],
            admission.archive_execution_domain._FIELDS["event_batch"],
            admission.archive_consumption_domain._COMMON,
            admission.archive_presentation_domain._WITNESS, bundle._HEADER)
        for grammar in grammars:
            with self.subTest(grammar=sorted(grammar)):
                grammar.add("unreviewed_history_field")
                try:
                    self.refused(lambda: admission._admission_identity(self.root, handle))
                finally:
                    grammar.remove("unreviewed_history_field")

    def test_preflight_replacement_is_refused_before_foreign_chunk_bytes(self):
        target = self.root.path / commits.CHUNK_DIRECTORY / (self.record["chunks"][0]["sha256"] + ".jsonl")
        native_verify, native_read = admission._verify, os.read
        observed = []
        replacement_inode = []
        def replace_then_verify(root, prepared):
            replacement = self.fixture.fixture.base / "between-preflight-replacement"
            replacement.write_bytes(target.read_bytes())
            replacement.replace(target)
            replacement_inode.append(target.stat().st_ino)
            return native_verify(root, prepared)
        def watch(fd, count):
            raw = native_read(fd, count)
            if replacement_inode and os.fstat(fd).st_ino == replacement_inode[0]:
                observed.append(len(raw))
            return raw
        with mock.patch.object(admission, "_verify", replace_then_verify), mock.patch.object(os, "read", watch):
            self.refused(lambda: admission.admit_committed_survivors(self.root))
        self.assertEqual(0, sum(observed), "replacement was read before old preflight identity refused")
