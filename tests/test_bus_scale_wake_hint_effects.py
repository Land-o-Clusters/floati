"""Native writer hints qualify only with exact routing, bytes and metadata."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import stat
import tempfile
import unittest

from tests.fixtures.bus_scale_census.bus_scale_cursor_prefix_effects import raw_candidate_delta
from tests.fixtures.bus_scale_census.bus_scale_wake_hint_effects import verify_wake_hint_effects
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_control import WakeController
from tests.temp_roots import REAL_TEMP_ROOT


class NativeWakeHintEffectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bus-scale-owned-hint-", dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.bank = Path(self.temporary.name).resolve()
        self.home = self.bank / "fleet"
        self.home.mkdir(mode=0o700)
        self.root = FloatiRoot.open_direct_home(self.home)
        Registry(self.root).register("actor", "codex")
        self.before = self.snapshot()
        WakeController(self.root).pause("actor", "native-hint-session", idempotency_key="native-hint-pause")
        self.hint = "state/wake-hints/actor/generation"
        self.path = self.home / self.hint
        self.kinds = {"receipts/wake-control/actor.jsonl": {"wake_control_receipt"}}

    def snapshot(self):
        return {path.relative_to(self.home).as_posix(): path.read_bytes()
                for path in self.home.rglob("*") if path.is_file()}

    def proof(self, *, before=None, after=None, delta=None, kinds=None):
        before = self.before if before is None else before
        after = self.snapshot() if after is None else after
        delta = raw_candidate_delta(before, after) if delta is None else delta
        return verify_wake_hint_effects(self.root, before, after, delta,
            ledger_kinds=self.kinds if kinds is None else kinds)

    def test_actual_pause_retains_its_exact_empty_hint_and_is_read_only(self):
        after = self.snapshot()
        self.assertEqual({self.hint: {"operation": "create_empty_hint", "kind": "native_wake_hint"}}, self.proof())
        self.assertEqual(after, self.snapshot())
        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))

    def test_foreign_recipient_and_sibling_cannot_borrow_native_pause_receipt(self):
        for relative in ("state/wake-hints/foreign/generation", "state/wake-hints/actor/other"):
            with self.subTest(relative=relative):
                path = self.home / relative
                path.parent.mkdir(mode=0o700, exist_ok=True)
                path.touch(mode=0o600)
                try:
                    with self.assertRaisesRegex(ValueError, "unreviewed native hint coordinate"):
                        self.proof()
                finally:
                    path.unlink()

    def test_hint_requires_its_actual_selected_ledger_append(self):
        with self.assertRaisesRegex(ValueError, "unreviewed native hint coordinate"):
            self.proof(kinds={})
        before = dict(self.before)
        ledger = "receipts/wake-control/actor.jsonl"
        before[ledger] = self.snapshot()[ledger]
        with self.assertRaisesRegex(ValueError, "unreviewed native hint coordinate"):
            self.proof(before=before)

    def test_nonempty_or_rewritten_hint_is_refused_with_truthful_raw_delta(self):
        self.path.write_bytes(b"foreign hint bytes")
        with self.assertRaisesRegex(ValueError, "absent-to-empty"):
            self.proof()
        self.path.write_bytes(b"")
        before = dict(self.before, **{self.hint: b"prior foreign bytes"})
        with self.assertRaisesRegex(ValueError, "absent-to-empty"):
            self.proof(before=before)

    def test_hint_deletion_or_omitted_raw_change_is_refused(self):
        after = self.snapshot()
        delta = raw_candidate_delta(self.before, after)
        delta.pop(self.hint)
        with self.assertRaisesRegex(ValueError, "population differs"):
            self.proof(after=after, delta=delta)
        before = dict(self.before, **{self.hint: b""})
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "absent-to-empty"):
            self.proof(before=before)

    def test_every_raw_hint_field_is_bound_including_its_type(self):
        after = self.snapshot()
        original = raw_candidate_delta(self.before, after)
        for field, value in (("operation", "replace"), ("before_sha256", "f" * 64),
                             ("after_sha256", "f" * 64), ("before_bytes", 1),
                             ("after_bytes", False), ("payload_base64", "AA==")):
            with self.subTest(field=field):
                forged = copy.deepcopy(original)
                forged[self.hint][field] = value
                with self.assertRaisesRegex(ValueError, "exact native empty bytes"):
                    self.proof(after=after, delta=forged)

    def test_wrong_modes_hardlinks_and_aliases_cannot_qualify_empty_bytes(self):
        for path, mode in ((self.path, 0o644), (self.path.parent, 0o755),
                           (self.home / "state", 0o770)):
            with self.subTest(path=path, mode=mode):
                original = stat.S_IMODE(path.stat().st_mode)
                path.chmod(mode)
                try:
                    with self.assertRaises(ValueError):
                        self.proof()
                finally:
                    path.chmod(original)
        link = self.bank / "hardlink"
        os.link(self.path, link)
        try:
            with self.assertRaises(ValueError):
                self.proof()
        finally:
            link.unlink()
        target = self.bank / "alias-target"
        target.touch(mode=0o600)
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.proof()

    def test_existing_unchanged_empty_hint_does_not_invent_a_raw_effect(self):
        from floati.wake_hints import publish_recipient_hint
        before = self.snapshot()
        self.assertTrue(publish_recipient_hint(self.root,"actor"))
        self.assertEqual({}, self.proof(before=before))
