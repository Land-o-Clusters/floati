"""The new raw-domain bank needs its own primitive and writer controls."""
from __future__ import annotations

import hashlib
import random
import tempfile
import unittest
from pathlib import Path


class SHA256MidstateTests(unittest.TestCase):
    def test_persisted_state_matches_hashlib_at_random_split_points(self):
        from floati.sha256_midstate import SHA256
        rng = random.Random(361)
        for size in (0, 1, 55, 56, 63, 64, 65, 127, 420, 2048):
            data = bytes(rng.randrange(256) for _ in range(size))
            points = sorted(set([0, size] + [rng.randrange(size + 1) for _ in range(8)]))
            for split in points:
                with self.subTest(size=size, split=split):
                    digest = SHA256()
                    digest.update(data[:split])
                    self.assertEqual(hashlib.sha256(data[:split]).hexdigest(), digest.hexdigest())
                    restored = SHA256(state=digest.state, length=digest.length, tail=digest.tail)
                    restored.update(data[split:])
                    self.assertEqual(hashlib.sha256(data).hexdigest(), restored.hexdigest())
                    self.assertEqual(hashlib.sha256(data[:split]).hexdigest(), digest.hexdigest(),
                        "finalization must not consume the append midstate")


class CutPrefixTests(unittest.TestCase):
    def test_native_writers_keep_every_raw_cut_and_retained_node(self):
        from floati.cut_prefix import CutPrefix, warm
        from floati.events import EventLog
        from floati.registry import Registry
        from floati.root import FloatiRoot
        from floati.wake_hold import WakeHoldController
        with tempfile.TemporaryDirectory() as temporary:
            root = FloatiRoot.open_direct_home(Path(temporary).resolve() / "fleet", create=True)
            for node in ("sender", "bob"):
                Registry(root).register(node, "Codex")
            events = EventLog(root)
            self.assertIsNotNone(warm(root))
            for index in range(3):
                before = CutPrefix(root)
                before.load()
                retained = {path.name: path.read_bytes() for path in before.nodes.iterdir()}
                events.send("sender", "bob", "floati", "a" * 40, "note.md", "cut append",
                    idempotency_key="cut-" + str(index))
                prefix = CutPrefix(root)
                prefix.load()
                for name, raw in retained.items():
                    self.assertEqual(raw, (prefix.nodes / name).read_bytes())
                digest = hashlib.sha256(WakeHoldController._EVENT_DOMAIN.encode("ascii") + b"\0")
                frames = root.resolve_relative("events.jsonl").read_bytes().splitlines(keepends=True)
                for ordinal, frame in enumerate(frames, 1):
                    digest.update(frame)
                    self.assertEqual(ordinal, prefix.get("cut", digest.hexdigest()))
                self.assertEqual(digest.hexdigest(), prefix.payload["folds"]["cut"]["digest"])
