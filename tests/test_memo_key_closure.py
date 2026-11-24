"""The memo key covers reached modules without importing or calling them."""
import sys
import types
import unittest
from unittest.mock import patch

from floati.memo_key_closure import binding_snapshot, projection_binding_snapshot


def _module(name, source):
    module = types.ModuleType(name)
    module.__package__ = ""
    exec(source, module.__dict__)
    return module


def _values(snapshot):
    found = []

    def walk(item):
        found.append(item)
        if isinstance(item, tuple):
            for child in item:
                walk(child)

    walk(snapshot)
    return found


class MemoKeyClosureTests(unittest.TestCase):
    def test_indirect_namespace_bindings_keep_semantic_changes_visible(self):
        sources = {
            "container": 'ALIASES = [None]\ndef root(): return ALIASES[0]["RULE"]',
            "class": 'class Holder: NAMESPACE = None\ndef root(): return Holder.NAMESPACE["RULE"]',
            "default": 'def root(namespace=None): return namespace["RULE"]',
            "closure": '''
def make():
    namespace = None
    def root(): return namespace["RULE"]
    return root
root = make()
''',
        }
        for kind, source in sources.items():
            with self.subTest(kind=kind):
                owner = _module("floati.memo_indirect_owner", 'RULE = "original"')
                replacement = _module("floati.memo_indirect_replacement", 'RULE = "replacement"')
                consumer = _module("floati.memo_indirect_consumer", source)

                def bind(namespace):
                    if kind == "container":
                        consumer.ALIASES[0] = namespace
                    elif kind == "class":
                        consumer.Holder.NAMESPACE = namespace
                    elif kind == "default":
                        consumer.root.__defaults__ = (namespace,)
                    else:
                        consumer.root.__closure__[0].cell_contents = namespace

                bind(owner.__dict__)
                with patch.dict(sys.modules, {owner.__name__: owner,
                        replacement.__name__: replacement, consumer.__name__: consumer}):
                    original = binding_snapshot([consumer.root])
                    owner.RULE = "changed"
                    self.assertNotEqual(binding_snapshot([consumer.root]), original)
                    bind(replacement.__dict__)
                    rebound = binding_snapshot([consumer.root])
                    replacement.RULE = "changed replacement"
                    self.assertNotEqual(binding_snapshot([consumer.root]), rebound,
                        "rebound namespace hid its subsequent semantic changes")

    def test_namespace_alias_excludes_derived_storage_but_tracks_bindings(self):
        owner = _module("floati.memo_namespace_owner", """
DERIVED_CACHE = {}
RULE = "original"
NAMESPACE = globals()
""")
        consumer = _module("floati.memo_namespace_consumer", """
def root():
    return NAMESPACE["RULE"]
""")
        consumer.NAMESPACE = owner.__dict__
        replacement = _module("floati.memo_namespace_replacement", 'RULE = "replacement"')
        with patch.dict(sys.modules, {owner.__name__: owner, consumer.__name__: consumer,
                replacement.__name__: replacement}):
            original = binding_snapshot([consumer.root])
            owner.DERIVED_CACHE["new native observation"] = object()
            self.assertEqual(binding_snapshot([consumer.root]), original,
                "namespace alias made derived cache contents semantic inputs")
            owner.RULE = "changed"
            self.assertNotEqual(binding_snapshot([consumer.root]), original,
                "namespace alias lost the owner's semantic bindings")
            owner.RULE = "original"
            self.assertEqual(binding_snapshot([consumer.root]), original)
            consumer.NAMESPACE = replacement.__dict__
            rebound = binding_snapshot([consumer.root])
            self.assertNotEqual(rebound, original)
            replacement.RULE = "changed replacement"
            self.assertNotEqual(binding_snapshot([consumer.root]), rebound,
                "warm discovery lost the replacement namespace's bindings")
            consumer.NAMESPACE = owner
            self.assertNotEqual(binding_snapshot([consumer.root]), original,
                "namespace dictionary and module bindings must stay distinct")

    def test_in_place_archive_proof_getter_change_invalidates_snapshot(self):
        from floati.epoch_carry import _VerifiedHoldProofs

        descriptor = _VerifiedHoldProofs.__dict__["origins"]
        getter, setter, deleter, doc = (
            descriptor.fget, descriptor.fset, descriptor.fdel, descriptor.__doc__)
        original = projection_binding_snapshot()

        def replacement(_proof):
            return {}

        try:
            descriptor.__init__(replacement, setter, deleter, doc)
            self.assertIs(_VerifiedHoldProofs.__dict__["origins"], descriptor)
            self.assertIs(descriptor.fget, replacement)
            self.assertTrue(projection_binding_snapshot() != original,
                "in-place proof getter change reused the binding snapshot")
        finally:
            descriptor.__init__(getter, setter, deleter, doc)
        self.assertEqual(projection_binding_snapshot(), original)

    def test_archive_and_readiness_bindings_are_in_the_derived_closure(self):
        """The incoming archive/readiness owners must survive recomposition."""
        from floati import (bus_epoch, cursor_presentations, epoch_carry,
                            epoch_prefix, readiness_contract, recipient_mechanisms)
        from floati.mail_health import RecipientReadiness

        owners = (
            (cursor_presentations, ("_presentation_witness_data", "_presentation_groups",
                "_presentation_cuts", "_validate_presentation_groups")),
            (epoch_carry.ArchiveProofResolver, ("_source_contract", "archives", "epoch_homes")),
            (epoch_carry, ("verified_hold_proofs",)),
            (epoch_prefix, ("_projection_contract",)),
            (readiness_contract, ("validate_readiness_record", "validate_readiness",
                "MECHANISMS", "READINESS_REASONS")),
            (RecipientReadiness, ("_observe_already_guarded",)),
            (recipient_mechanisms, ("observe_mechanisms",)),
            (bus_epoch, ("observe_epoch_roll_state", "_epoch_archive_snapshot",
                "_assert_epoch_archive_snapshot", "_selected_member_identity")),
        )
        # Discovery supplies the closure; this list only checks its contents.
        # Keep tuple objects as well as their entries so tuple-valued bindings
        # are checked by identity too.
        found = _values(projection_binding_snapshot())
        for owner, names in owners:
            for name in names:
                with self.subTest(case=(owner.__name__, name)):
                    # A classmethod's binding is its stored descriptor, not
                    # the transient bound method created by getattr.
                    binding = vars(owner)[name]
                    self.assertTrue(any(value is binding for value in found),
                        (owner.__name__, name))

    def test_global_read_two_calls_deep_is_included(self):
        module = _module("memo_closure_depth", """
USED = "used-constant"
UNUSED = "unused-constant"
def leaf():
    return USED
def mid():
    return leaf()
def root():
    return mid()
""")
        found = _values(binding_snapshot([module.root]))
        self.assertIn(module.USED, found)
        # An unread global of a reached module is still a binding. The key
        # over-includes it rather than depending on a bytecode walk.
        self.assertIn(module.UNUSED, found)

    def test_unresolvable_attribute_over_includes_owner_globals(self):
        module = _module("memo_closure_over", """
SIBLING = "sibling-constant"
def helper():
    return object()
def root():
    return helper().attr
""")
        found = _values(binding_snapshot([module.root]))
        self.assertIn(module.SIBLING, found)
        self.assertIn(module.helper, found)

    def test_snapshot_is_deterministic(self):
        module = _module("memo_closure_order", """
LEFT = "left-constant"
RIGHT = "right-constant"
def left():
    return LEFT
def right():
    return RIGHT
def root():
    return left()
""")
        first = binding_snapshot([module.root, module.right])
        second = binding_snapshot([module.right, module.root])
        third = binding_snapshot([module.root, module.right])
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_walker_does_not_import_or_execute(self):
        module = _module("memo_closure_quiet", """
calls = []
MARKER = "leaf-constant"
def leaf():
    calls.append("leaf")
    return MARKER
def root():
    import memo_closure_absent_module_zz
    return leaf()
""")
        before = set(sys.modules)
        found = _values(binding_snapshot([module.root]))
        self.assertEqual(module.calls, [])
        self.assertNotIn("memo_closure_absent_module_zz", sys.modules)
        self.assertEqual(set(sys.modules), before)
        self.assertIn(module.MARKER, found)


if __name__ == "__main__":
    unittest.main()
