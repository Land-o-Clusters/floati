"""Seat31 section26 rider: carried work at a threshold never loops or refuses."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati import bus_epoch, doctor, epoch_rotation, registry
from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.framing import decode_frames
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


class CarriedThresholdRiderTests(unittest.TestCase):
    def policy(self, root, threshold):
        path = root.path / "state/ledger-policy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 0, "max_bytes": threshold, "max_age_days": 7}))

    def send(self, root, key):
        return EventLog(root).send("sender", "recipient", "floati", "a" * 40,
            "AGENTS.md", "bounded pending carried-set rider " + key, idempotency_key=key)

    def archives(self, root):
        return tuple(sorted(path.name for path in root.path.glob("archive-*")))

    @contextmanager
    def clock(self, aged):
        if not aged:
            yield
            return
        future = datetime.now(timezone.utc) + timedelta(days=8)

        class ClockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(future.timestamp(), tz=tz)

        with mock.patch.object(epoch_rotation, "datetime", ClockDateTime),\
                mock.patch.object(doctor, "datetime", ClockDateTime),\
                mock.patch.object(registry, "utc_now", return_value=future.isoformat(timespec="milliseconds").replace("+00:00", "Z")):
            yield

    def test_carried_threshold_rider_never_loops_or_refuses_and_reclaims_changed_set(self):
        for case, exact, aged in (("exact", True, False), ("above", False, False), ("bytes-and-age", False, True)):
            with self.subTest(case=case):
                temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
                self.addCleanup(temporary.cleanup)
                root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
                for node in ("sender", "recipient"):
                    Registry(root).register(node, "Codex")
                pending = [self.send(root, "seed-%d" % index)["id"] for index in range(3)]
                self.policy(root, 1)
                pending.append(self.send(root, "initial-roll")["id"])
                self.assertEqual(1, len(self.archives(root)))
                events = root.path / "events.jsonl"
                carried = events.read_bytes()
                anchor = decode_frames(carried)[0]
                self.assertEqual("bus_epoch_roll_receipt", anchor["kind"])
                threshold = len(carried) if exact else len(carried) - 1
                self.assertGreater(threshold, 1)
                self.policy(root, threshold)
                with self.clock(aged):
                    moment = epoch_rotation.datetime.now(timezone.utc)
                    observation = epoch_rotation.observe_policy(root, now=moment)
                    self.assertEqual(len(carried), observation["observed_bytes"])
                    self.assertEqual({"bytes", "age"} if aged else {"bytes"}, set(observation["due_reasons"]))
                    result = epoch_rotation.maintenance(root, actor="sender", trigger="send")
                    self.assertEqual({"changed": False, "action": "nonreclaimable"}, result)
                    self.assertTrue(epoch_rotation._nonreclaimable_hint(root))
                    archive_names = self.archives(root)
                    for _ in range(3):
                        self.assertEqual(result, epoch_rotation.maintenance(root, actor="sender", trigger="send"))
                        self.assertTrue(epoch_rotation._nonreclaimable_hint(root))
                        self.assertEqual(carried, events.read_bytes())
                        self.assertEqual(archive_names, self.archives(root))
                        self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(root)["classification"])
                    doctor_before = events.read_bytes()
                    finding = doctor.project_ledger_roll_policy_finding(root)
                    self.assertEqual("ledger_roll_policy", finding["code"])
                    self.assertEqual("warning", finding["severity"])
                    self.assertIn("%d bytes" % len(carried), finding["detail"])
                    self.assertIn("policy %d," % threshold, finding["detail"])
                    self.assertIn("carried nonreclaimable work stays live", finding["remediation"])
                    self.assertEqual(doctor_before, events.read_bytes())
                    self.assertEqual(archive_names, self.archives(root))

                    measured = []
                    native_maintenance = epoch_rotation.maintenance

                    def observe_maintenance(current, **kwargs):
                        before = self.archives(current)
                        hint = epoch_rotation._nonreclaimable_hint(current)
                        outcome = native_maintenance(current, **kwargs)
                        after = self.archives(current)
                        measured.append({"trigger": kwargs["trigger"], "hint": hint,
                            "before": before, "after": after, "outcome": outcome})
                        return outcome

                    diagnostic = io.StringIO()
                    with mock.patch.object(epoch_rotation, "maintenance", side_effect=observe_maintenance),\
                            mock.patch("sys.stderr", diagnostic):
                        for index in range(3):
                            pending.append(self.send(root, "writer-%d" % index)["id"])
                            self.assertEqual(archive_names, self.archives(root))
                            self.assertEqual(anchor["id"], decode_frames(events.read_bytes())[0]["id"])
                            self.assertEqual(set(pending), set(EventLog(root).unacked_ids("recipient")))
                        self.assertEqual(3, len(measured))
                        self.assertTrue(all(not item["hint"] and item["outcome"]["action"] == "nonreclaimable" for item in measured))
                        presented, _receipt = EventLog(root).present("recipient")
                        self.assertEqual(set(pending), {item["id"] for item in presented})
                        # Present may add retained delivery facts. Attribute its
                        # actual archive delta separately from the later ACK.
                        present = measured[-1]
                        self.assertEqual("present", present["trigger"])
                        self.assertLessEqual(len(present["after"]) - len(present["before"]), 1)
                        keeper = self.send(root, "unpresented-keeper")
                        self.assertEqual("send", measured[-1]["trigger"])
                        self.assertEqual(archive_names, self.archives(root))
                        SparseCursor(root).ack("recipient", [pending[0]], acting_session_id="threshold-rider")
                        partial = measured[-1]
                        self.assertEqual("ack", partial["trigger"])
                        self.assertFalse(partial["hint"])
                        self.assertEqual({"changed": False, "action": "nonreclaimable"}, partial["outcome"])
                        self.assertEqual(partial["before"], partial["after"])
                        self.assertEqual(set(pending[1:]) | {keeper["id"]}, set(EventLog(root).unacked_ids("recipient")))
                        SparseCursor(root).ack("recipient", pending[1:], acting_session_id="threshold-rider-close-batch")
                        acknowledged = measured[-1]
                        self.assertEqual("ack", acknowledged["trigger"])
                        self.assertFalse(acknowledged["hint"])
                        self.assertTrue(acknowledged["outcome"]["changed"])
                        self.assertEqual("rotated", acknowledged["outcome"]["action"])
                        self.assertEqual(1, len(acknowledged["after"]) - len(acknowledged["before"]))
                    self.assertEqual("", diagnostic.getvalue())
                    self.assertEqual([keeper["id"]], EventLog(root).unacked_ids("recipient"))
                    self.assertNotEqual(anchor["id"], decode_frames(events.read_bytes())[0]["id"])
                    final_bytes, final_archives = events.read_bytes(), self.archives(root)
                    for _ in range(3):
                        repeated = epoch_rotation.maintenance(root, actor="sender", trigger="send")
                        self.assertFalse(repeated["changed"])
                        self.assertIn(repeated["action"], {"nonreclaimable", "not_due"})
                        self.assertEqual(final_bytes, events.read_bytes())
                        self.assertEqual(final_archives, self.archives(root))
                    for name in final_archives:
                        bus_epoch.verify_epoch_archive(root.path / name)
