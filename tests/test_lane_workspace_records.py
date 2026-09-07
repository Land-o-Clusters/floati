"""Validate durable lane identity before a filesystem path can be acted on."""
from copy import deepcopy
import unittest

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.records import validate_record


class LaneWorkspaceRecordTests(unittest.TestCase):
    def record(self):
        return {
            "schema_version": 0, "kind": "lane_workspace_record",
            "id": "lane-workspace-" + uuid7_hex(), "tenant_id": "fixture",
            "timestamp": "2026-09-06T00:00:00.000Z", "node_id": "builder",
            "row": "ROW-1", "path": "builder/work/ROW-1", "repo": "fixture",
            "base_sha": "a" * 40, "branch": "codex/lane/builder/ROW-1",
            "state": "open", "opened_at": "2026-09-06T00:00:00.000Z",
            "closed_at": None, "closed_by": None, "opened_record_id": None,
            "why": None,
        }

    def validate(self, record, integrity=False):
        return validate_record(record, "fixture", frozenset({"lane_workspace_record"}),
                               integrity=integrity)

    def test_open_and_linked_close_have_valid_durable_shapes(self):
        opened = self.record()
        self.assertEqual(opened, self.validate(opened))
        closed = dict(opened, id="lane-workspace-" + uuid7_hex(), state="closed",
                      opened_record_id=opened["id"], closed_by="builder",
                      closed_at="2026-09-06T00:01:00.000Z", why="Discard fixture work")
        self.assertEqual(closed, self.validate(closed))

    def test_paths_branches_and_close_coordinates_cannot_change_subject(self):
        for change in (
            {"path": "/external/work"}, {"path": "../work"},
            {"path": "another/work/ROW-1"}, {"row": "../../escape"},
            {"branch": "main"}, {"base_sha": "HEAD"},
            {"closed_by": "builder"}, {"why": "unrecorded force"},
            {"state": "closed"}, {"opened_at": "tomorrow"},
        ):
            with self.subTest(change=change):
                record = deepcopy(self.record()); record.update(change)
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.validate(record)
                self.assertEqual("lane_workspace_record_invalid", caught.exception.code)
                self.assertIsInstance(caught.exception.remedy, str)
                with self.assertRaises(IntegrityFailure):
                    self.validate(record, integrity=True)
