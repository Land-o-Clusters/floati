from __future__ import annotations

from floati import fixture_ids as public_ids

import tempfile
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot


NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
SHA_A = "a" * 40
SHA_B = "b" * 40


class CanonicalFramingTests(unittest.TestCase):
    def test_sorted_compact_utf8_frame_is_shared_for_any_record(self) -> None:
        from floati.framing import encode_frame

        mail = {"z": 1, "kind": "message_envelope", "label": "buoy"}
        work = {"z": 1, "kind": "work_item", "label": "buoy"}

        self.assertEqual(
            b'{"kind":"message_envelope","label":"buoy","z":1}\n',
            encode_frame(mail),
        )
        self.assertEqual(
            b'{"kind":"work_item","label":"buoy","z":1}\n',
            encode_frame(work),
        )

    def test_decode_refuses_incomplete_blank_and_malformed_frames_distinctly(self) -> None:
        from floati.framing import FrameError, decode_frames

        cases = (
            (b'{"a":1}', "incomplete_frame"),
            (b"\n", "blank_frame"),
            (b"not-json\n", "malformed_json"),
        )
        for data, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(FrameError) as caught:
                    decode_frames(data)
                self.assertEqual(code, caught.exception.code)


class WorkLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.registry = Registry(self.root)
        self.registry.register(public_ids.worker('alpha'), "Codex")
        self.registry.register("bravo", "Codex")

    @staticmethod
    def binding(sha: str, doc: str) -> dict[str, str]:
        return {"repo": "slipway", "sha": sha, "doc": doc}

    def test_work_ledger_is_distinct_append_only_and_completes_sparse_items(self) -> None:
        from floati.jsonl import read_records
        from floati.work import WorkLog

        work = WorkLog(self.root)
        item_a = work.add("frame mail", public_ids.worker('alpha'), [self.binding(SHA_A, "docs/a.md")])
        item_b = work.add("build board", public_ids.worker('alpha'), [])
        item_c = work.add("study acp", "bravo", [])

        grant = AuthorityGrantStore(self.root).claim(
            "work-claims", public_ids.worker('alpha'), 60, 60, NOW
        )
        claimed = work.claim(
            item_b["id"],
            public_ids.worker('alpha'),
            "work-claims",
            grant["epoch"],
            now=NOW,
        )
        completed = work.complete(
            item_b["id"],
            public_ids.worker('alpha'),
            [self.binding(SHA_B, "docs/evidence/b.md")],
            now=NOW,
        )

        self.assertEqual("work_transition", claimed["kind"])
        self.assertEqual("claim", claimed["action"])
        self.assertEqual("complete", completed["action"])
        states = {item["id"]: item for item in work.show()}
        self.assertEqual("open", states[item_a["id"]]["state"])
        self.assertEqual("completed", states[item_b["id"]]["state"])
        self.assertEqual("open", states[item_c["id"]]["state"])
        self.assertEqual(
            [self.binding(SHA_B, "docs/evidence/b.md")],
            states[item_b["id"]]["artifact_bindings"],
        )
        durable = read_records(
            self.root,
            "work/items.jsonl",
            allowed_kinds={"work_item", "work_transition"},
        )
        self.assertEqual(5, len(durable))
        self.assertEqual(
            ["work_item", "work_item", "work_item", "work_transition", "work_transition"],
            [record["kind"] for record in durable],
        )
        self.assertFalse((self.home / "events.jsonl").exists())

    def test_claim_requires_exact_active_authority_plane_binding(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        item = work.add("bind authority", public_ids.worker('alpha'), [])
        grant = AuthorityGrantStore(self.root).claim(
            "work-claims", public_ids.worker('alpha'), 60, 60, NOW
        )
        path = self.home / "work" / "items.jsonl"
        before = path.read_bytes()

        cases = (
            ("bravo", "work-claims", grant["epoch"], "authority_holder_mismatch"),
            (public_ids.worker('alpha'), "work-claims", grant["epoch"] + 1, "authority_epoch_mismatch"),
            (public_ids.worker('alpha'), "other-subject", grant["epoch"], "authority_missing"),
        )
        for actor, subject, epoch, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    work.claim(item["id"], actor, subject, epoch, now=NOW)
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(before, path.read_bytes())

    def test_invalid_work_transition_refuses_without_mutation(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        item = work.add("ordered transitions", public_ids.worker('alpha'), [])
        path = self.home / "work" / "items.jsonl"
        before = path.read_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            work.complete(item["id"], public_ids.worker('alpha'), [], now=NOW)
        self.assertEqual("work_not_claimed", caught.exception.code)
        self.assertEqual(before, path.read_bytes())

    def test_owned_oldest_selection_and_claim_share_one_transaction(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        oldest_alice = work.add(public_ids.compose('oldest ', public_ids.worker('alpha')), public_ids.worker('alpha'), [])
        work.add("bravo work", "bravo", [])
        work.add(public_ids.compose('newer ', public_ids.worker('alpha')), public_ids.worker('alpha'), [])
        grant = AuthorityGrantStore(self.root).claim(
            "work-claims", public_ids.worker('alpha'), 60, 60, NOW
        )

        claimed = work.claim_owned_oldest(
            public_ids.worker('alpha'), "work-claims", grant["epoch"], now=NOW
        )

        self.assertEqual(oldest_alice["id"], claimed["id"])
        self.assertEqual("claimed", claimed["state"])
        states = {item["id"]: item for item in work.show()}
        self.assertEqual("claimed", states[oldest_alice["id"]]["state"])
        self.assertEqual(
            ["claimed", "open"],
            sorted(
                item["state"]
                for item in states.values()
                if item["owner"] == public_ids.worker('alpha')
            ),
        )

    def test_workspace_mapping_is_derived_from_the_new_work_id_and_legacy_items_remain_readable(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        legacy = work.add("legacy sparse work", public_ids.worker('alpha'), [])
        live = work.add(
            "create one local artifact",
            public_ids.worker('alpha'),
            [],
            provision_workspace=True,
        )

        self.assertNotIn("workspace", legacy)
        self.assertEqual(
            f"\x2fprivate\x2ftmp/floati-work/{live['id']}",
            live["workspace"],
        )
        projected = {item["id"]: item for item in work.show()}
        self.assertIsNone(projected[legacy["id"]]["workspace"])
        self.assertEqual(live["workspace"], projected[live["id"]]["workspace"])

    def test_dependency_edges_are_bounded_existing_and_unique(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        prerequisite = work.add("prepare input", public_ids.worker('alpha'), [])

        for needs, code in (
            (["work-018f0f23abcd71238000000000000000"], "work_dependency_unknown"),
            ([prerequisite["id"], prerequisite["id"]], "needs_invalid"),
            (["not-a-work-id"], "needs_invalid"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    work.add("dependent", public_ids.worker('alpha'), [], needs=needs)
                self.assertEqual(code, caught.exception.code)

    def test_blocked_claims_refuse_until_dependencies_complete(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        prerequisite = work.add("prepare input", public_ids.worker('alpha'), [])
        dependent = work.add(
            "consume input", public_ids.worker('alpha'), [], needs=[prerequisite["id"]]
        )
        unrelated = work.add("independent work", public_ids.worker('alpha'), [])
        grant = AuthorityGrantStore(self.root).claim(
            "work-claims", public_ids.worker('alpha'), 60, 60, NOW
        )

        projected = {item["id"]: item for item in work.show()}
        self.assertEqual("ready", projected[prerequisite["id"]]["readiness"])
        self.assertEqual("blocked", projected[dependent["id"]]["readiness"])
        self.assertEqual([prerequisite["id"]], projected[dependent["id"]]["needs"])

        with self.assertRaises(ProtocolRefusal) as caught:
            work.claim(
                dependent["id"], public_ids.worker('alpha'), "work-claims", grant["epoch"], now=NOW
            )
        self.assertEqual("work_dependencies_blocked", caught.exception.code)

        selected = work.claim_owned_oldest(
            public_ids.worker('alpha'), "work-claims", grant["epoch"], now=NOW
        )
        self.assertEqual(prerequisite["id"], selected["id"])
        work.complete(prerequisite["id"], public_ids.worker('alpha'), [], now=NOW)

        projected = {item["id"]: item for item in work.show()}
        self.assertEqual("done", projected[prerequisite["id"]]["readiness"])
        self.assertEqual("ready", projected[dependent["id"]]["readiness"])
        self.assertEqual("ready", projected[unrelated["id"]]["readiness"])

    def test_projection_rejects_future_dependency_edges_in_raw_evidence(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        first = work.add("first", public_ids.worker('alpha'), [])
        second = work.add("second", public_ids.worker('alpha'), [])
        path = self.home / "work" / "items.jsonl"
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        records[0]["needs"] = [second["id"]]
        path.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in records),
            encoding="utf-8",
        )

        with self.assertRaises(IntegrityFailure) as caught:
            work.show(first["id"])

        self.assertEqual("consumption_state_unavailable", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
