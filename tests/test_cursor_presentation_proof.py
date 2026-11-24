"""Native regression controls for write-ahead, delivery and path proof boundaries."""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from unittest import mock

from floati.cursor_presentations import CursorPresentationLedger, limit_selection, notification
from floati.errors import IntegrityFailure
from floati.events import EventLog
from floati.framing import encode_frame
from tests import test_cursor_presentation_history as history
from tests import test_f2_cursor_claimant as claimant


class CursorPresentationProofTests(unittest.TestCase):
    def setUp(self):
        self.h = history.CursorPresentationHistoryTests("runTest")
        self.addCleanup(self.h.doCleanups)
        self.h.setUp()
        self.root, self.node = self.h.root, self.h.node

    def bounded(self, selection):
        return limit_selection(selection, lambda messages: notification(
            self.root.path, self.node, self.h.c.f.base, claimant.A, messages))

    def test_unwitnessed_extra_native_batch_cannot_suppress_fresh_mail(self):
        self.h.publish("witnessed-batch")
        original_witness = self.h.witness.read_bytes()
        fresh = self.h.c.f.send("unwitnessed-extra-batch")
        with self.h.selected() as (plane, selection):
            bounded, body = self.bounded(selection)
            extra = plane.prepare(bounded, body)
        self.assertEqual([fresh["id"]], extra["item_ids"])
        # The extra row came from the actual authorized native producer. Only
        # its required write-ahead testimony is rolled back, isolating the gap.
        self.h.witness.write_bytes(original_witness)
        bytes_before = self.h.history.read_bytes()
        with self.assertRaises(IntegrityFailure) as caught:
            CursorPresentationLedger(self.root).read(self.node)
        self.assertEqual("presentation_history_unavailable", caught.exception.code)
        self.assertIn("write-ahead witness", caught.exception.detail)
        self.assertEqual(bytes_before, self.h.history.read_bytes())

    def test_older_matching_native_delivery_cannot_complete_a_new_batch(self):
        message = self.h.c.f.send("old-manual-delivery")
        shown, older = EventLog(self.root).present(self.node)
        self.assertEqual([message], shown)
        with self.h.selected() as (plane, selection):
            bounded, body = self.bounded(selection)
            prepared = plane.prepare(bounded, body)
            before = self.h.history.read_bytes()
            with self.assertRaises(IntegrityFailure) as caught:
                plane.flushed(prepared, older)
            self.assertIn("immediate native delivery", caught.exception.detail)
            self.assertEqual(before, self.h.history.read_bytes())
            delivery = plane.deliver(prepared, bounded)
            terminal = plane.flushed(prepared, delivery)
        # Preserve exact witness bytes/ancestors while changing the terminal
        # reference to an actual older delivery with the same node and IDs.
        forged = dict(terminal, delivery_receipt_id=older["id"])
        witness = json.loads(self.h.witness.read_bytes())
        witness.update(before_bytes=len(before), before_digest=hashlib.sha256(before).hexdigest(),
            reserved_record=forged)
        self.h.history.write_bytes(before + encode_frame(forged))
        self.h.witness.write_text(json.dumps(witness) + "\n")
        with self.assertRaises(IntegrityFailure) as caught:
            CursorPresentationLedger(self.root).read(self.node)
        self.assertEqual("presentation_history_unavailable", caught.exception.code)
        self.assertIn("exact native delivery", caught.exception.detail)

    def test_retained_append_descriptor_confines_raced_leaf_and_ancestor(self):
        outside = self.root.tenant_home.parent / "presentation-proof-outside"
        outside.mkdir()
        target = outside / self.h.history.name
        target.write_bytes(b"outside ordinary file must remain unchanged\n")
        original_write = os.write
        for coordinate in ("leaf", "ancestor"):
            with self.subTest(coordinate=coordinate):
                self.h.c.f.send("append-race-" + coordinate)
                with self.h.selected() as (plane, selection):
                    bounded, body = self.bounded(selection)
                    raced = []
                    victim = self.h.history if coordinate == "leaf" else self.h.history.parent
                    retained = victim.with_name(victim.name + ".retained")
                    destination = target if coordinate == "leaf" else outside
                    def raced_write(descriptor, data):
                        self.assertFalse(raced, "append boundary was entered twice")
                        victim.rename(retained)
                        victim.symlink_to(destination, target_is_directory=coordinate == "ancestor")
                        raced.append(True)
                        return original_write(descriptor, data)
                    before_target = target.read_bytes()
                    try:
                        with mock.patch("floati.cursor_presentations.os.write", side_effect=raced_write):
                            with self.assertRaises(IntegrityFailure) as caught:
                                plane.prepare(bounded, body)
                        expected = "presentation_history_unavailable" if coordinate == "leaf" else "stop_hold_path_invalid"
                        self.assertEqual(expected, caught.exception.code)
                        self.assertEqual([True], raced)
                        self.assertEqual(before_target, target.read_bytes())
                    finally:
                        if raced:
                            victim.unlink()
                            retained.rename(victim)
                # The append stayed in the retained original ordinary file;
                # restoring its name reproduces the exact durable reservation.
                rows = CursorPresentationLedger(self.root).read(self.node)
                self.assertEqual("prepared", rows[-1]["phase"])

    def test_same_size_changed_prefix_before_append_open_receives_no_new_frame(self):
        self.h.publish("append-prefix-baseline")
        original_open = os.open
        for operation in ("replace", "corrupt"):
            with self.subTest(operation=operation):
                self.h.c.f.send("append-prefix-" + operation)
                with self.h.selected() as (plane, selection):
                    bounded, body = self.bounded(selection)
                    before = self.h.history.read_bytes()
                    original_witness = self.h.witness.read_bytes()
                    changed = b"X" + before[1:]
                    entered = []
                    def changed_open(path, flags, *args, **kwargs):
                        if path == self.h.history.name and flags & os.O_APPEND:
                            self.assertFalse(entered)
                            if operation == "replace":
                                replacement = self.h.history.with_name(self.h.history.name + ".replacement")
                                replacement.write_bytes(changed)
                                replacement.replace(self.h.history)
                            else:
                                self.h.history.write_bytes(changed)
                            entered.append(True)
                        return original_open(path, flags, *args, **kwargs)
                    try:
                        with mock.patch("floati.cursor_presentations.os.open", side_effect=changed_open):
                            with self.assertRaises(IntegrityFailure) as caught:
                                plane.prepare(bounded, body)
                        self.assertEqual("presentation_history_unavailable", caught.exception.code)
                        self.assertIn("retained append prefix", caught.exception.detail)
                        self.assertEqual([True], entered)
                        self.assertEqual(len(before), len(self.h.history.read_bytes()))
                        self.assertEqual(changed, self.h.history.read_bytes(), "changed prefix received a new frame")
                    finally:
                        self.h.history.write_bytes(before)
                        self.h.witness.write_bytes(original_witness)

    def test_source_lock_races_neither_create_nor_mutate_foreign_coordinates(self):
        self.h.publish("source-lock-race-baseline")
        outside = self.root.tenant_home.parent / "source-lock-proof-outside"
        outside.mkdir()
        lock = self.h.history.parent.parent / "deliveries" / (self.node + ".jsonl.lock")
        target = outside / lock.name
        original_open = os.open
        for coordinate, exists in (("leaf", True), ("leaf", False), ("ancestor", True), ("ancestor", False)):
            with self.subTest(coordinate=coordinate, target_exists=exists):
                if exists:
                    target.write_bytes(b"foreign lock target must remain unchanged\n")
                else:
                    target.unlink(missing_ok=True)
                before = {path.name: path.read_bytes() for path in outside.iterdir()}
                victim = lock if coordinate == "leaf" else lock.parent
                retained = victim.with_name(victim.name + ".retained")
                destination = target if coordinate == "leaf" else outside
                raced = []
                def raced_open(path, flags, *args, **kwargs):
                    if path == lock.name and flags & os.O_CREAT:
                        self.assertFalse(raced)
                        victim.rename(retained)
                        victim.symlink_to(destination, target_is_directory=coordinate == "ancestor")
                        raced.append(True)
                    return original_open(path, flags, *args, **kwargs)
                try:
                    with mock.patch("floati.cursor_presentations.os.open", side_effect=raced_open):
                        with self.assertRaises(IntegrityFailure):
                            CursorPresentationLedger(self.root).read(self.node)
                    self.assertEqual([True], raced)
                    self.assertEqual(before, {path.name: path.read_bytes() for path in outside.iterdir()})
                finally:
                    self.assertEqual(before, {path.name: path.read_bytes() for path in outside.iterdir()})
                    if raced:
                        victim.unlink()
                        retained.rename(victim)
