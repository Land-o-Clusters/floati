"""Read-open bookkeeping must preserve the complete epoch mutation witness."""
from __future__ import annotations

import builtins
import io
import inspect
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from floati.root import FloatiRoot
from tests import test_bus_epoch_roll as epoch
from tests.temp_roots import REAL_TEMP_ROOT


class EpochProbeReadOpenTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        self.payload = self.root.tenant_home / "payload.bin"
        self.payload.write_bytes(b"native read-open control\n")
        self.other = self.root.tenant_home / "unrelated.bin"
        self.other.write_bytes(b"complete-root testimony\n")

    def probe(self):
        return epoch._MutationProbe(self.root, stack_prefix=__name__)

    def snapshots(self):
        original = epoch._MutationProbe.snapshot
        calls = []
        def observed(root):
            calls.append(root.tenant_home)
            return original(root)
        return calls, patch.object(epoch._MutationProbe, "snapshot", staticmethod(observed))

    def test_plain_native_stream_reads_do_not_reread_the_root_or_add_ordinals(self):
        before = epoch._MutationProbe.snapshot(self.root)
        for name in ("builtin", "io"):
            for mode in ("r", "rb", "rt"):
                with self.subTest(opener=name, mode=mode):
                    probe = self.probe()
                    calls, observed = self.snapshots()
                    with observed, probe.installed():
                        # Resolve the installed callable, not a captured alias.
                        current = builtins.open if name == "builtin" else io.open
                        with current(self.payload, mode) as stream:
                            self.assertTrue(stream.read())
                    self.assertEqual([], probe.events)
                    self.assertEqual([], calls,
                        "a pure native read-open reread the complete unrelated root")
        self.assertEqual(before, epoch._MutationProbe.snapshot(self.root))

    def test_plain_pathlib_read_uses_the_same_nonmutating_native_open_contract(self):
        before = epoch._MutationProbe.snapshot(self.root)
        probe = self.probe()
        calls, observed = self.snapshots()
        with observed, probe.installed():
            self.assertEqual(b"native read-open control\n", self.payload.read_bytes())
        self.assertEqual([], probe.events)
        self.assertEqual([], calls,
            "Path.open's ordinary default argument tail caused root snapshot rereads")
        self.assertEqual(before, epoch._MutationProbe.snapshot(self.root))

    def test_readonly_os_open_keeps_fd_identity_without_open_snapshot_rereads(self):
        combinations = [os.O_RDONLY]
        flags = os.O_RDONLY
        for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
            flags |= getattr(os, name, 0)
        combinations.append(flags)
        before = epoch._MutationProbe.snapshot(self.root)
        for flags in combinations:
            with self.subTest(flags=flags):
                probe = self.probe()
                calls, observed = self.snapshots()
                with observed, probe.installed():
                    descriptor = os.open(self.payload, flags)
                    try:
                        after_open = len(calls)
                        self.assertEqual(self.payload, probe._fds[descriptor])
                        self.assertEqual(b"native read-open control\n", os.read(descriptor, 1000))
                    finally:
                        # close remains fully observed; isolate the open cost.
                        os.close(descriptor)
                    self.assertNotIn(descriptor, probe._fds)
                self.assertEqual(0, after_open,
                    "a known readonly os.open reread the complete root")
                self.assertEqual([], probe.events)
        self.assertEqual(before, epoch._MutationProbe.snapshot(self.root))

    def test_readonly_dirfd_open_retains_root_relative_descriptor_association(self):
        probe = self.probe()
        calls, observed = self.snapshots()
        with observed, probe.installed():
            directory = os.open(self.root.tenant_home, os.O_RDONLY | os.O_DIRECTORY)
            try:
                before_open = len(calls)
                descriptor = os.open(self.payload.name, os.O_RDONLY, dir_fd=directory)
                try:
                    after_open = len(calls)
                    self.assertEqual(self.payload, probe._fds[descriptor])
                finally:
                    os.close(descriptor)
            finally:
                os.close(directory)
        self.assertEqual(before_open, after_open,
            "a known native readonly dir_fd open reread the complete root")
        self.assertEqual({}, probe._fds)
        self.assertEqual([], probe.events)

    def test_mutating_stream_modes_retain_complete_capture(self):
        for name in ("builtin", "io"):
            for mode in ("w", "wb", "w+", "a", "ab", "a+", "x", "xb", "x+", "r+"):
                with self.subTest(opener=name, mode=mode):
                    target = self.root.tenant_home / (name + "-" + mode.replace("+", "update"))
                    if mode.startswith(("w", "r")):
                        target.write_text("must retain truncation/update testimony\n")
                    before = epoch._MutationProbe.snapshot(self.root)
                    probe = self.probe()
                    calls, observed = self.snapshots()
                    with observed, probe.installed():
                        opener = builtins.open if name == "builtin" else io.open
                        with opener(target, mode) as stream:
                            stream.write(b"changed" if "b" in mode else "changed")
                            stream.flush()
                    after = epoch._MutationProbe.snapshot(self.root)
                    self.assertGreaterEqual(len(calls), 2)
                    mutations = [row for row in probe.events if isinstance(row, epoch._MutationEvent)]
                    self.assertTrue(mutations, "a writable open/write lost its concrete mutation")
                    self.assertEqual(before, mutations[0].before)
                    self.assertEqual(after, mutations[-1].after)
                    for left, right in zip(mutations, mutations[1:]):
                        self.assertEqual(left.after, right.before)
                    self.assertTrue(all(any(row[0] == "unrelated.bin" for row in event.before)
                        for event in mutations), "capture was narrowed to the opened target")

    def test_readonly_create_append_and_unknown_flags_do_not_claim_the_pure_open_path(self):
        cases = (("readonly-create", os.O_RDONLY | os.O_CREAT, True),
                 ("readonly-append", os.O_RDONLY | os.O_APPEND, False),
                 ("unknown-flags", os.O_RDONLY | (1 << 30), False))
        for name, flags, creates in cases:
            with self.subTest(flags=name):
                target = self.root.tenant_home / name if creates else self.payload
                before = epoch._MutationProbe.snapshot(self.root)
                probe = self.probe()
                calls, observed = self.snapshots()
                refused = None
                with observed, probe.installed():
                    try:
                        descriptor = os.open(target, flags, 0o600)
                    except OSError as failure:
                        refused = failure.errno
                        after_open = len(calls)
                    else:
                        try:
                            after_open = len(calls)
                        finally:
                            os.close(descriptor)
                self.assertGreaterEqual(after_open, 1,
                    "unreviewed native flags bypassed complete-root observation")
                if refused is None:
                    self.assertGreaterEqual(after_open, 2)
                if creates:
                    self.assertIsNone(refused)
                    mutations = [row for row in probe.events if isinstance(row, epoch._MutationEvent)]
                    self.assertTrue(mutations, "O_RDONLY|O_CREAT lost its actual creation")
                    self.assertEqual(before, mutations[0].before)
                    self.assertEqual(epoch._MutationProbe.snapshot(self.root), mutations[-1].after)
                else:
                    self.assertEqual(before, epoch._MutationProbe.snapshot(self.root))

    def test_custom_read_opener_retains_its_complete_callback_mutation(self):
        native_open = os.open
        native_io_open = epoch._UNPATCHED_IO_OPEN
        sentinel = self.root.tenant_home / "custom-opener-mutation"
        def opener(path, flags):
            # Deliberately bypass the nested wrappers. Only the outer open's
            # complete before/after observation can witness this callback.
            with native_io_open(sentinel, "wb") as stream:
                stream.write(b"the custom opener is not a pure native read\n")
            return native_open(path, flags)
        before = epoch._MutationProbe.snapshot(self.root)
        probe = self.probe()
        calls, observed = self.snapshots()
        with observed, probe.installed():
            with builtins.open(self.payload, "rb", opener=opener) as stream:
                self.assertTrue(stream.read())
        mutations = [row for row in probe.events if isinstance(row, epoch._MutationEvent)]
        self.assertEqual(1, len(mutations))
        self.assertEqual("builtins.open", mutations[0].primitive)
        self.assertEqual(before, mutations[0].before)
        self.assertEqual(epoch._MutationProbe.snapshot(self.root), mutations[0].after)
        self.assertGreaterEqual(len(calls), 2)

    def test_replaced_path_fspath_keeps_native_open_callback_mutations_observed(self):
        native_fspath = Path.__fspath__
        native_io_open = epoch._UNPATCHED_IO_OPEN
        sentinel = self.root.tenant_home / "replaced-path-fspath"
        sentinel_name = str(sentinel)
        conversions = []
        def replacement(receiver):
            if receiver is self.payload:
                conversions.append(len(conversions) + 1)
                # A captured native opener over a string deliberately avoids
                # nested wrappers: the enclosing native read-open must retain
                # this callback's actual mutation, not infer it from its name.
                with native_io_open(sentinel_name, "wb") as stream:
                    stream.write(("conversion-" + str(conversions[-1])).encode())
            return native_fspath(receiver)
        baseline = epoch._MutationProbe.snapshot(self.root)
        probe = self.probe()
        calls, observed = self.snapshots()
        with patch.object(Path, "__fspath__", replacement), observed, probe.installed():
            with builtins.open(self.payload, "rb") as stream:
                self.assertEqual(b"native read-open control\n", stream.read())
        mutations = [row for row in probe.events if isinstance(row, epoch._MutationEvent)]
        self.assertEqual(1, len(mutations),
            "an exact Path type with a replaced protocol callback bypassed observation")
        self.assertEqual("builtins.open", mutations[0].primitive)
        before = {row[0]: row for row in mutations[0].before}
        after = {row[0]: row for row in mutations[0].after}
        self.assertNotEqual(before.get(sentinel.name), after.get(sentinel.name),
            "native read-open lost the callback's incrementing physical mutation")
        self.assertTrue(after[sentinel.name][-1].startswith(b"conversion-"))
        self.assertEqual({row[0]: row for row in baseline},
            {name: row for name, row in before.items() if name != sentinel.name},
            "callback capture omitted unrelated pre-open root evidence")
        self.assertEqual({row[0]: row for row in baseline},
            {name: row for name, row in after.items() if name != sentinel.name},
            "callback capture omitted unrelated post-open root evidence")
        self.assertGreaterEqual(len(calls), 2)
        # Legacy invoke() also classifies paths before/after its snapshots.
        # Those conversions may change the sentinel again; this property
        # deliberately judges the callback inside the native open itself.

    def test_replaced_path_str_keeps_native_open_callback_mutations_observed(self):
        native_str = Path.__str__
        native_io_open = epoch._UNPATCHED_IO_OPEN
        sentinel = self.root.tenant_home / "replaced-path-str"
        sentinel_name = str(sentinel)
        conversions = []
        # A changed protocol must stay observed even with a cached string.
        str(self.payload)
        def replacement(receiver):
            if receiver is self.payload:
                conversions.append(len(conversions) + 1)
                with native_io_open(sentinel_name, "wb") as stream:
                    stream.write(("conversion-" + str(conversions[-1])).encode())
            return native_str(receiver)
        baseline = epoch._MutationProbe.snapshot(self.root)
        probe = self.probe()
        calls, observed = self.snapshots()
        with patch.object(Path, "__str__", replacement), observed, probe.installed():
            with builtins.open(self.payload, "rb") as stream:
                self.assertEqual(b"native read-open control\n", stream.read())
        mutations = [row for row in probe.events if isinstance(row, epoch._MutationEvent)]
        self.assertEqual(1, len(mutations),
            "native __fspath__ must not hide a replaced __str__ callback")
        self.assertEqual("builtins.open", mutations[0].primitive)
        before = {row[0]: row for row in mutations[0].before}
        after = {row[0]: row for row in mutations[0].after}
        self.assertNotEqual(before.get(sentinel.name), after.get(sentinel.name))
        self.assertTrue(after[sentinel.name][-1].startswith(b"conversion-"))
        expected = {row[0]: row for row in baseline}
        self.assertEqual(expected,
            {name: row for name, row in before.items() if name != sentinel.name})
        self.assertEqual(expected,
            {name: row for name, row in after.items() if name != sentinel.name})
        self.assertGreaterEqual(len(calls), 2)
        # Judge native open, allowing legacy classifications outside capture.

    def test_generic_invoke_never_trusts_a_read_looking_callback_name(self):
        native_io_open = epoch._UNPATCHED_IO_OPEN
        target = self.root.tenant_home / "generic-callback-mutation"
        def callback(*_args, **_kwargs):
            with native_io_open(target, "wb") as stream:
                stream.write(b"arbitrary invoke callbacks retain full capture\n")
        before = epoch._MutationProbe.snapshot(self.root)
        probe = self.probe()
        with probe.installed():
            probe.invoke("builtins.open", self.payload, callback, self.payload, "rb")
        mutations = [row for row in probe.events if isinstance(row, epoch._MutationEvent)]
        self.assertEqual(1, len(mutations))
        self.assertEqual(before, mutations[0].before)
        self.assertEqual(epoch._MutationProbe.snapshot(self.root), mutations[0].after)

    def test_native_path_opener_bound_to_a_different_path_stays_fully_observed(self):
        opener = getattr(self.other, "_opener", None)
        if opener is None:
            self.assertIsNone(getattr(epoch, "_UNPATCHED_PATH_OPENER", None),
                "stdlib_path_opener_absent must not gain an admission exception")
            return
        before = epoch._MutationProbe.snapshot(self.root)
        probe = self.probe()
        calls, observed = self.snapshots()
        with observed, probe.installed():
            with io.open(self.payload, "rb", opener=opener) as stream:
                self.assertEqual(b"complete-root testimony\n", stream.read())
        self.assertGreaterEqual(len(calls), 2,
            "a native opener for another receiver bypassed complete observation")
        self.assertEqual([], probe.events)
        self.assertEqual(before, epoch._MutationProbe.snapshot(self.root))

    def test_replaced_path_opener_function_retains_its_callback_mutation(self):
        if getattr(Path, "_opener", None) is None:
            self.assertIsNone(getattr(epoch, "_UNPATCHED_PATH_OPENER", None),
                "stdlib_path_opener_absent must not gain an admission exception")
            return
        native_io_open, native_os_open = epoch._UNPATCHED_IO_OPEN, os.open
        sentinel = self.root.tenant_home / "replaced-path-opener"
        def replacement(receiver, _name, flags, mode=0o666):
            with native_io_open(sentinel, "wb") as stream:
                stream.write(b"a replaced Path opener remains observable\n")
            return native_os_open(receiver, flags, mode)
        before = epoch._MutationProbe.snapshot(self.root)
        probe = self.probe()
        with patch.object(Path, "_opener", replacement), probe.installed():
            with self.payload.open("rb") as stream:
                self.assertTrue(stream.read())
        mutations = [row for row in probe.events if isinstance(row, epoch._MutationEvent)]
        self.assertEqual(1, len(mutations))
        self.assertEqual(before, mutations[0].before)
        self.assertEqual(epoch._MutationProbe.snapshot(self.root), mutations[0].after)

    def test_path_accessor_class_and_instance_replacements_retain_complete_capture(self):
        accessor = getattr(self.payload, "_accessor", None)
        if accessor is None:
            self.assertIsNone(getattr(epoch, "_UNPATCHED_PATH_ACCESSOR", None),
                "stdlib_path_accessor_absent must not gain an admission exception")
            return
        native_io_open, native_os_open = epoch._UNPATCHED_IO_OPEN, os.open
        for placement in ("instance", "class"):
            with self.subTest(placement=placement):
                sentinel = self.root.tenant_home / ("path-accessor-" + placement)
                def replacement(path, flags, mode=0o666):
                    with native_io_open(sentinel, "wb") as stream:
                        stream.write(b"a replaced Path accessor remains observable\n")
                    return native_os_open(path, flags, mode)
                target = accessor if placement == "instance" else type(accessor)
                value = replacement if placement == "instance" else staticmethod(replacement)
                before = epoch._MutationProbe.snapshot(self.root)
                probe = self.probe()
                with patch.object(target, "open", value), probe.installed():
                    with self.payload.open("rb") as stream:
                        self.assertTrue(stream.read())
                mutations = [row for row in probe.events if isinstance(row, epoch._MutationEvent)]
                self.assertEqual(1, len(mutations))
                self.assertEqual(before, mutations[0].before)
                self.assertEqual(epoch._MutationProbe.snapshot(self.root), mutations[0].after)

    def test_replaced_path_accessor_descriptor_runs_only_inside_complete_capture(self):
        slot = inspect.getattr_static(Path, "_accessor", None)
        if slot is None:
            self.assertIsNone(getattr(epoch, "_UNPATCHED_PATH_ACCESSOR_SLOT", None),
                "stdlib_path_accessor_absent must not gain an admission exception")
            return
        native_io_open = epoch._UNPATCHED_IO_OPEN
        sentinel = self.root.tenant_home / "path-accessor-descriptor"
        def get_accessor(receiver):
            value = slot.__get__(receiver, type(receiver))
            if receiver is self.payload:
                with native_io_open(sentinel, "wb") as stream:
                    stream.write(b"the accessor descriptor is a caller callback\n")
            return value
        def set_accessor(receiver, value):
            slot.__set__(receiver, value)
        before = epoch._MutationProbe.snapshot(self.root)
        probe = self.probe()
        with patch.object(Path, "_accessor", property(get_accessor, set_accessor)), probe.installed():
            with self.payload.open("rb") as stream:
                self.assertTrue(stream.read())
        mutations = [row for row in probe.events if isinstance(row, epoch._MutationEvent)]
        self.assertEqual(1, len(mutations))
        self.assertEqual("io.open", mutations[0].primitive)
        self.assertEqual(before, mutations[0].before,
            "accessor admission executed the descriptor before its native open snapshot")
        self.assertEqual(epoch._MutationProbe.snapshot(self.root), mutations[0].after)

    def test_replaced_read_callable_is_not_admitted_as_a_pure_native_open(self):
        native_io_open = epoch._UNPATCHED_IO_OPEN
        candidates = ((builtins, "open", builtins.open, "builtin"),
                      (io, "open", io.open, "io"),
                      (os, "open", os.open, "os"))
        for module, name, native, label in candidates:
            with self.subTest(callable=label):
                sentinel = self.root.tenant_home / ("replaced-" + label)
                def replacement(*args, _native=native, **kwargs):
                    with native_io_open(sentinel, "wb") as stream:
                        stream.write(b"a replaced read callable is not a pure native open\n")
                    return _native(*args, **kwargs)
                before = epoch._MutationProbe.snapshot(self.root)
                probe = self.probe()
                with patch.object(module, name, replacement), probe.installed():
                    if module is os:
                        descriptor = os.open(self.payload, os.O_RDONLY)
                        os.close(descriptor)
                    else:
                        opener = builtins.open if module is builtins else io.open
                        with opener(self.payload, "rb") as stream:
                            self.assertTrue(stream.read())
                mutations = [row for row in probe.events if isinstance(row, epoch._MutationEvent)]
                self.assertEqual(1, len(mutations))
                self.assertEqual(before, mutations[0].before)
                self.assertEqual(epoch._MutationProbe.snapshot(self.root), mutations[0].after)

    def test_readonly_descriptor_fsync_retains_its_exact_durability_ordinal(self):
        probe = self.probe()
        before = epoch._MutationProbe.snapshot(self.root)
        with probe.installed():
            descriptor = os.open(self.payload, os.O_RDONLY)
            try:
                self.assertEqual(self.payload, probe._fds[descriptor])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        mutations = [row for row in probe.events if isinstance(row, epoch._MutationEvent)]
        self.assertEqual(1, len(mutations))
        self.assertEqual(("os.fsync", "payload.bin", True),
            (mutations[0].primitive, mutations[0].target, mutations[0].durability))
        self.assertEqual(before, mutations[0].before)
        self.assertEqual(before, mutations[0].after)
        self.assertEqual({}, probe._fds)


class EpochProbeFullTraceTests(unittest.TestCase):
    def native_trace(self, *, forwarding_opens):
        harness = epoch.GovernedBusEpochRollTests(
            "test_00_successful_roll_trace_is_derived_from_real_mutations_and_boundaries")
        def cleanup():
            self.assertTrue(harness.doCleanups(), "native trace harness cleanup failed")
            temporary = getattr(harness, "temporary", None)
            if temporary is not None:
                self.assertFalse(Path(temporary.name).exists())
        self.addCleanup(cleanup)
        harness.setUp()
        original_probe = epoch._MutationProbe
        instances = []
        class RecordingProbe(original_probe):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                instances.append(self)
        native_builtin, native_io, native_os = builtins.open, io.open, os.open
        def forwarding_builtin(*args, **kwargs):
            return native_builtin(*args, **kwargs)
        def forwarding_io(*args, **kwargs):
            return native_io(*args, **kwargs)
        def forwarding_os(*args, **kwargs):
            return native_os(*args, **kwargs)
        from contextlib import ExitStack
        with ExitStack() as stack:
            # Python forwarding callbacks force the conservative observation
            # path. They must not qualify as exact native open callables.
            if forwarding_opens:
                stack.enter_context(patch.object(builtins, "open", forwarding_builtin))
                stack.enter_context(patch.object(io, "open", forwarding_io))
                stack.enter_context(patch.object(os, "open", forwarding_os))
            stack.enter_context(patch.object(epoch, "_MutationProbe", RecordingProbe))
            harness.test_00_successful_roll_trace_is_derived_from_real_mutations_and_boundaries()
        self.assertEqual(1, len(instances))
        events = instances[0].events
        # The original case independently proves exact whole-root adjacency,
        # every concrete delta, unchanged non-selected state and final receipt.
        # Fresh roots have different inodes/UUIDs; compare the concrete native
        # primitive/durability and boundary sequence across both observations.
        return [("mutation", event.primitive, event.durability)
                if isinstance(event, epoch._MutationEvent) else ("boundary", event.name)
                for event in events]

    def test_native_and_conservative_open_paths_keep_the_same_complete_roll_trace(self):
        conservative = self.native_trace(forwarding_opens=True)
        native = self.native_trace(forwarding_opens=False)
        self.assertTrue(conservative)
        self.assertEqual(conservative, native,
            "read-open bookkeeping changed a native mutation ordinal or fault boundary")
