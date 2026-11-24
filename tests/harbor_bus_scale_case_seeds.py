"""Native expanded-case prefaces must precede exact one-input padding."""
from pathlib import Path
import importlib.util
import tempfile
import unittest

from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import banked_send_repository
from tests.bus_scale_test_profile import seed_counts

try:
    from tests.bus_scale_case_seeds import make_case_seed_pair
except ModuleNotFoundError as error:
    if error.name != "tests.bus_scale_case_seeds":
        raise
    make_case_seed_pair = None


class CaseSeedPrefaceTests(unittest.TestCase):
    def test_native_selected_mail_is_equal_before_exact_history_padding(self):
        self.assertTrue(callable(make_case_seed_pair),
                        "missing approved native-preface case seed constructor")
        from floati.root import FloatiRoot
        from floati.events import EventLog

        class NativePreface:
            @staticmethod
            def prepare(case, home, workspace, bank):
                root = FloatiRoot.open_direct_home(home)
                sha = (workspace / ".git/refs/remotes/origin/main").read_text().strip()
                message = EventLog(root).send(
                    "scale-worker", "architect", "floati", sha,
                    "note.md", "one selected common-prefix envelope",
                    idempotency_key="case-seed-selected")["message"]
                return {"selected_id": message["id"], "case": case}

        with tempfile.TemporaryDirectory(prefix="case-preface-controls-", dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary).resolve()
            workspace, _sha = banked_send_repository(base)
            workspace.chmod(0o700)
            for family in ("events", "registry", "wait-exhaustion", "deliveries"):
                bank = base / family
                bank.mkdir(mode=0o700)
                pair = make_case_seed_pair(bank, family, "native-preface-control",
                                           NativePreface, workspace, counts=seed_counts())
                relative = pair["ledger_relative"]
                for home in (pair["prepared_home"], pair["1"], pair["10"]):
                    self.assertEqual(0o700, home.parent.stat().st_mode & 0o777,
                                     "native sibling publications require a private parent")
                small = (pair["1"] / relative).read_bytes()
                large = (pair["10"] / relative).read_bytes()
                self.assertEqual(seed_counts()[0], small.count(b"\n"))
                self.assertEqual(seed_counts()[1], large.count(b"\n"))
                self.assertTrue(large.startswith(small))
                inventories = []
                for scale in ("1", "10"):
                    home = pair[scale]
                    files = {path.relative_to(home).as_posix(): path.read_bytes()
                             for path in home.rglob("*") if path.is_file()}
                    del files[relative]
                    inventories.append(files)
                    root = FloatiRoot.open_direct_home(home)
                    pending = set(EventLog(root).unacked_ids("architect"))
                    selected = [row for row in EventLog(root).event_records()
                                if row.get("kind") == "message_envelope"
                                and row.get("recipient") == "architect" and row["id"] in pending]
                    self.assertEqual([pair["context"]["selected_id"]],
                                     [row["id"] for row in selected])
                    self.assertEqual([pair["context"]["selected_id"]],
                                     EventLog(root).unacked_ids("architect"))
                self.assertEqual(inventories[0], inventories[1])

    def test_padding_changes_neither_selected_sender_work_nor_selected_presentation_count(self):
        self.assertTrue(callable(make_case_seed_pair))
        from floati.root import FloatiRoot
        from floati.events import EventLog
        from tests.bus_scale_seeds import _projection

        class NativePreface:
            @staticmethod
            def prepare(case, home, workspace, bank):
                root = FloatiRoot.open_direct_home(home)
                sha = (workspace / ".git/refs/remotes/origin/main").read_text().strip()
                message = EventLog(root).send("scale-worker", "architect", "floati", sha,
                    "note.md", "selected work", idempotency_key="case-selected")["message"]
                return {"selected_id": message["id"], "case": case}

        with tempfile.TemporaryDirectory(prefix="case-selected-controls-", dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary).resolve()
            workspace, _sha = banked_send_repository(base)
            workspace.chmod(0o700)
            for family in ("events", "deliveries"):
                bank = base / family
                bank.mkdir(mode=0o700)
                pair = make_case_seed_pair(bank, family, "selected-work-control", NativePreface, workspace,
                                           counts=seed_counts())
                for scale in ("1", "10"):
                    with self.subTest(family=family, scale=scale):
                        root = FloatiRoot.open_direct_home(pair[scale])
                        sent = [row["id"] for row in EventLog(root).event_records()
                                if row.get("kind") == "message_envelope"
                                and row.get("sender") == "scale-worker"]
                        self.assertEqual([pair["context"]["selected_id"]], sent,
                                         "padding must be unselected by sender as well as recipient")
                        selected = [item for item in _projection(root, "architect")
                                    if item.message["id"] == pair["context"]["selected_id"]]
                        self.assertEqual(1, len(selected))
                        self.assertEqual(("fresh", 0), (selected[0].state, selected[0].presentation_count),
                                         "padding must not present the selected envelope")

    def test_real_chart_timings_preparation_has_native_selected_mail_before_padding(self):
        self.assertTrue(callable(make_case_seed_pair))
        source = Path(__file__).resolve().parents[1] / "tests/fixtures/bus_scale_census/bus-scale-routine-reads-2026-10-15.py"
        spec = importlib.util.spec_from_file_location("case_seed_native_read_controls", source)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-seed-", dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary).resolve()
            workspace, _sha = banked_send_repository(base)
            workspace.chmod(0o700)
            bank = base / "case-seeds"
            bank.mkdir(mode=0o700)
            pair = make_case_seed_pair(bank, "events", "chart-timings", helper, workspace, counts=seed_counts())
            self.assertTrue(pair["context"])
            self.assertEqual(seed_counts()[0], (pair["1"] / pair["ledger_relative"]).read_bytes().count(b"\n"))

    def test_native_node_projection_preface_assigns_every_active_member(self):
        from floati.admin_registry import RegistryAdminBackend
        from floati.root import FloatiRoot
        source = Path(__file__).resolve().parents[1] / "tests/fixtures/bus_scale_census/bus-scale-routine-lifecycle-2026-10-15.py"
        spec = importlib.util.spec_from_file_location("case_seed_native_role_controls", source)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-role-", dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary).resolve()
            workspace, _sha = banked_send_repository(base)
            workspace.chmod(0o700)
            for case in ("node-boot", "node-explain", "node-teardown"):
                bank = base / case
                bank.mkdir(mode=0o700)
                pair = make_case_seed_pair(bank, "registry", case, helper, workspace, counts=seed_counts())
                for scale in ("1", "10"):
                    backend = RegistryAdminBackend(FloatiRoot.open_direct_home(pair[scale]))
                    self.assertEqual("builder", backend.role_record("scale-history")["template_role"])
                    # An even native role preface must not introduce an extra
                    # unassigned parity-padding member into the projection.
                    self.assertIsNone(backend.current_role("scale-history-padding"))
                    self.assertNotIn("scale-history-padding", backend.registry.active_node_ids())
