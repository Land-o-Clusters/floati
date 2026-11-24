"""A temporary node past its lease is visible without waiting on the wall clock.

The census case is one root, a 1-minute lease, and a read 65 seconds later.
Status, doctor, and the seat board must name that node, the recorded expiry,
and the retire command the temporary add already projected. Status rows and
graph nodes must tell that node from a permanent neighbour.
"""

from __future__ import annotations

import io
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.admin_registry import RegistryAdminBackend
from floati.doctor import Doctor, project_node_lease_findings
from floati.graph import HarborGraph
from floati.ids import uuid7_hex
from floati.node_wizard import NodeWizard
from floati.projection import FleetProjection
from floati.root import FloatiRoot
from floati.tui import model_from_root
from floati.tui_render import render_frame, render_plain_dump
from tests.temp_roots import REAL_TEMP_ROOT

import tempfile


ADDED_AT = datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)
STILL_LEASED_AT = ADDED_AT + timedelta(seconds=30)
PAST_LEASE_AT = ADDED_AT + timedelta(seconds=65)
TEMPORARY_NODE = "temp-a"
PERMANENT_NODE = "perm-a"


def _flat(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_flat(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_flat(item) for item in value)
    return ""


class NodeLeaseExpiryVisibleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        wizard = NodeWizard(
            self.root,
            RegistryAdminBackend(self.root),
            id_factory=uuid7_hex,
            now=lambda: ADDED_AT,
        )
        output = io.StringIO()
        permanent = wizard.add_from_keys(
            [PERMANENT_NODE, "Codex", "permanent"], output
        )
        temporary = wizard.add_from_keys(
            [TEMPORARY_NODE, "Codex", "temporary", "1"], output
        )
        self.assertIsNone(permanent["teardown_command"])
        self.teardown_command = temporary["teardown_command"]
        self.expires_at = next(
            row["expires_at"]
            for row in temporary["records"]
            if row["kind"] == "node_lease"
        )
        self.assertGreater(PAST_LEASE_AT, ADDED_AT + timedelta(minutes=1))
        self.assertLess(STILL_LEASED_AT, ADDED_AT + timedelta(minutes=1))

    def _status(self, now: datetime) -> dict:
        return FleetProjection(self.root).status_artifact(now)

    def _expired_lease_rows(self, stale: object) -> list:
        rows = stale if isinstance(stale, list) else []
        return [
            row
            for row in rows
            if self.expires_at in _flat(row) and TEMPORARY_NODE in _flat(row)
        ]

    def _doctor_findings(self, now: datetime) -> list:
        doctor = Doctor(
            Path(__file__).resolve().parents[1],
            self.root.path,
            ref="HEAD",
            no_sandbox=True,
        )
        artifact, _rc = doctor.artifact(now=now)
        return artifact["findings"]

    def _board_text(self, now: datetime) -> str:
        model = model_from_root(self.root, now=now)
        plain = render_plain_dump(model, width=80)
        frame = render_frame(
            model,
            80,
            40,
            selected=0,
            color=False,
            animation_progress=1.0,
        )
        return plain + "\n" + frame

    def test_status_names_the_expired_lease_after_sixty_five_seconds(self) -> None:
        """A 1-minute lease read at 30s stays quiet and at 65s names the node,
        the recorded expiry, and the retire command."""

        early = self._status(STILL_LEASED_AT)
        self.assertEqual([], self._expired_lease_rows(early["stale_leases"]))

        late = self._status(PAST_LEASE_AT)
        named = self._expired_lease_rows(late["stale_leases"])
        self.assertTrue(
            named,
            f"stale_leases: {late['stale_leases']!r}",
        )
        self.assertIn(self.teardown_command, _flat(named))
        self.assertNotIn(PERMANENT_NODE, _flat(named))

    def test_doctor_names_the_expired_lease_after_sixty_five_seconds(self) -> None:
        """Doctor stays silent while the lease holds and names it once past."""

        early = project_node_lease_findings(self.root, STILL_LEASED_AT)
        self.assertEqual([], self._expired_lease_rows(early))

        late = self._doctor_findings(PAST_LEASE_AT)
        named = self._expired_lease_rows(late)
        self.assertTrue(named, "doctor silent")
        self.assertIn(self.teardown_command, _flat(named))
        self.assertTrue(
            all(isinstance(row.get("code"), str) and row["code"] for row in named)
        )
        self.assertNotIn(PERMANENT_NODE, _flat(named))

    def test_seat_board_names_the_expired_lease_after_sixty_five_seconds(self) -> None:
        """The seat board stays quiet at 30s and names the lease at 65s."""

        early = self._board_text(STILL_LEASED_AT).replace("\n", " ")
        self.assertNotIn(self.expires_at, early)

        late = self._board_text(PAST_LEASE_AT).replace("\n", " ")
        self.assertIn(TEMPORARY_NODE, late)
        self.assertIn(self.expires_at, late)
        self.assertIn(self.teardown_command, late)

    def test_status_rows_tell_temporary_from_permanent(self) -> None:
        rows = {
            row["node_id"]: row for row in self._status(STILL_LEASED_AT)["nodes"]
        }
        self.assertEqual("temporary", rows[TEMPORARY_NODE].get("lifetime"))
        self.assertEqual("permanent", rows[PERMANENT_NODE].get("lifetime"))

    def test_graph_nodes_tell_temporary_from_permanent(self) -> None:
        rows = {row["id"]: row for row in HarborGraph(self.root).artifact()["nodes"]}
        self.assertEqual("temporary", rows[TEMPORARY_NODE].get("lifetime"))
        self.assertEqual("permanent", rows[PERMANENT_NODE].get("lifetime"))
