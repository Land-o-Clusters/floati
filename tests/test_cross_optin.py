"""Cross command, attended confirmation and store integration.

The journal tests exercise the storage API directly. This module measures the
public command against that API with a disposable store and inventory.
"""
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
from errno import EIO
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch

from floati.cli import main
from floati.cross_anchor import Anchor, EnableCreate
from floati.cross_journal import CrossJournal, Storage
from floati.framing import encode_frame
from tests.temp_roots import REAL_TEMP_ROOT


_audit_events = None
_audit_root = None


def _observe(event, args):
    if _audit_events is None:
        return
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.exec", "os.posix_spawn"}:
        _audit_events.append(event)
    elif event == "open" and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
        _observe_path(event, args[:1])
    elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.chown", "os.truncate"}:
        _observe_path(event, args[:1])
    elif event in {"os.rename", "os.link", "os.symlink"}:
        _observe_path(event, args[:2])


def _observe_path(event, names):
    for name in names:
        if isinstance(name, (str, bytes)):
            path = Path(os.fsdecode(name))
            if path == _audit_root or _audit_root in path.parents:
                _audit_events.append(event)
                return


sys.addaudithook(_observe)


_PROMPT = re.compile(rb"Type ([0-9a-f]{12}) to enable: ")
_BOUND = 20


def _panel_preimage(template, start):
    def lp(value):
        return len(value).to_bytes(4, "big") + value

    return (lp(template.encode("ascii")) + lp(bytes.fromhex(start["enable_id"]))
            + start["enable_generation"].to_bytes(8, "big") + lp(bytes.fromhex(start["token"]))
            + lp(encode_frame(start["plan"])[:-1]))


def _published_enable_vectors():
    excerpt = (Path(__file__).with_name("fixtures") / "cross-enable-panel-vectors.v0.txt").read_text(encoding="utf-8")
    registry, = re.findall(r"^\| ENABLE-PANEL \| (.*)$", excerpt, re.M)
    v7_start, = re.findall(r"^start = (\{.*\})$", excerpt, re.M)
    yield "floati-enable-panel-v7", json.loads(v7_start), None
    for template, appendix in zip(re.findall(r"`(floati-enable-panel-[^`]+)`", registry),
                                  ("V12", "V12A1", "V12A3", "V12A4")):
        rows = [line for line in excerpt.splitlines() if line.startswith(f"| {appendix}-E")]
        size = int(re.search(r"\b\d{3}\b", rows[0]).group())
        digests = tuple(re.search(r"`([^`]+)`", line).group(1) for line in rows[1:])
        start = json.loads(v7_start)
        start["human_act"]["rendered_digest"] = digests[0]
        yield template, start, (size, *digests)


def _operator_result(root):
    from floati import cross_optin
    from floati.cross_keystore import FixtureStore

    store, inventory, output = FixtureStore(), Inventory(), StringIO()
    with patch.object(cross_optin, "open_backend", return_value=(store, inventory)), redirect_stdout(output):
        code = main(["cross", "enable", "--root", str(root)])
    print(json.dumps({"code": code, "evidence": json.loads(output.getvalue())["evidence"],
                      "anchor": isinstance(store.anchor.read(), Anchor), "items": sorted(inventory.items)}))


class Inventory:
    def __init__(self):
        self.items = {}

    def expected(self, item, start):
        return sha256(encode_frame({"item": item, "start": start})).hexdigest()

    def read(self, item, start, proof=None):
        if item not in self.items:
            return "safe_absent", None
        return "owned", self.items[item]

    def create(self, item, start):
        self.items[item] = self.expected(item, start)

    def inventory(self, start, records):
        return {}

    def remove(self, item, start, proof):
        if self.read(item, start, proof) != ("owned", proof):
            raise AssertionError("removal must be limited to the original item")
        del self.items[item]

    def checks(self, start):
        no_listening = not start["plan"]["listen"]
        return {"unit_absent": "unit" not in self.items, "unit_not_loadable": "unit" not in self.items,
                "no_listener": no_listening, "no_live_endpoint": no_listening}


class CrossOptinTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "fleet"
        self.root.mkdir()

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(["cross", *args, "--root", str(self.root)])
            except SystemExit as exc:
                self.fail(f"cross command exited during parsing ({exc.code}): {err.getvalue()}")
        return code, json.loads(out.getvalue())["evidence"]

    def test_fresh_root_previews_and_help_leave_complete_inventory_and_network_untouched(self):
        global _audit_events, _audit_root
        def census():
            return [(p.relative_to(self.root), p.stat().st_dev, p.stat().st_ino)
                    for p in (self.root, *self.root.rglob("*"))]

        before = census()
        _audit_root = self.root
        _audit_events = []
        try:
            with patch("floati.cross_optin.open_backend", side_effect=AssertionError("unexpected store access")):
                for args in (("enable", "--preview"), ("disable", "--dry-run")):
                    with self.subTest(args=args):
                        code, evidence = self.cli(*args)
                        self.assertEqual(0, code, evidence)
                        self.assertEqual("preview", evidence["state"])
                for args in (("cross", "--help"), ("cross", "enable", "--help"), ("cross", "disable", "--help")):
                    with redirect_stdout(StringIO()):
                        self.assertEqual(0, main(args))
            self.assertEqual(before, census())
            self.assertEqual([], _audit_events)
        finally:
            _audit_events = None
            _audit_root = None

    def fixture(self):
        from floati.cross_keystore import FixtureStore
        from floati import cross_optin
        store = FixtureStore()
        inventory = Inventory()
        self.enterContext(patch.object(cross_optin, "open_backend", return_value=(store, inventory)))
        self.enterContext(patch.object(cross_optin, "outside_seat"))
        return store, inventory

    def enterContext(self, manager):
        # Floati's oldest supported Python predates TestCase.enterContext.
        result = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return result

    def reader(self, store):
        return CrossJournal(self.root / "cross", anchor_store=store.anchor, quota_store=store.quota,
                            enable_approval=lambda phase, body: False)

    def operator_terminal(self, cwd, answer):
        import pty

        pid, descriptor = pty.fork()
        if pid == 0:
            try:
                os.chdir(cwd)
                _operator_result(self.root)
                sys.stdout.flush()
            except BaseException:
                import traceback
                traceback.print_exc()
                os._exit(1)
            os._exit(0)
        screen, replied = b"", False
        began = monotonic()
        finished = False
        try:
            while True:
                remaining = _BOUND - (monotonic() - began)
                readable, _, _ = select.select([descriptor], [], [], max(0, remaining))
                self.assertTrue(readable, f"terminal bound={_BOUND}s elapsed={monotonic()-began:.2f}s load={os.getloadavg()}")
                try:
                    data = os.read(descriptor, 65536)
                except OSError as exc:
                    if exc.errno != EIO:
                        raise
                    data = b""
                if not data:
                    finished = True
                    break
                screen += data
                prompt = _PROMPT.search(screen)
                if prompt and not replied:
                    os.write(descriptor, answer(screen, prompt.group(1)) + b"\n")
                    replied = True
        finally:
            os.close(descriptor)
            if not finished:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            _, status = os.waitpid(pid, 0)
        self.assertEqual(0, status, screen)
        result, = (json.loads(line) for line in screen.splitlines() if line.startswith(b'{"code":'))
        return result, replied

    def test_controlling_terminal_approves_exact_inventory_only_outside_a_seat(self):
        from floati.seat_declaration import SEAT_MARKER

        outside = self.root.parent / "operator"
        seat = self.root.parent / "seat"
        outside.mkdir()
        seat.mkdir()
        (seat / SEAT_MARKER).write_text("{}", encoding="ascii")

        def check_panel(screen, challenge):
            plan, = (json.loads(line) for line in screen.splitlines() if line.startswith(b'{"items":'))
            self.assertEqual({"listen": [], "root": str(self.root),
                              "store": "keychain" if sys.platform == "darwin" else "secret-service",
                              "unit": "none"}, plan["plan"])
            self.assertCountEqual(["anchor", "ballast", "configuration", "gate", "sentinel"], plan["items"])
            self.assertFalse(plan["unit_requested"])
            self.assertEqual([], list(self.root.iterdir()))
            return challenge

        without_terminal = subprocess.run(
            [sys.executable, "-B", "-c",
             "from tests.test_cross_optin import _operator_result; import sys; _operator_result(sys.argv[1])",
             str(self.root)], cwd=outside, start_new_session=True,
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
            input=b"yes\n", capture_output=True, timeout=_BOUND, check=True,
        )
        detached = json.loads(without_terminal.stdout)
        in_seat, prompted = self.operator_terminal(seat, check_panel)
        self.assertFalse(prompted)
        declined, prompted = self.operator_terminal(outside, lambda screen, code: b"wrong")
        self.assertTrue(prompted)
        for result in (detached, in_seat, declined):
            self.assertEqual(20, result["code"], result)
            self.assertEqual("cross_human_act_unavailable", result["evidence"]["code"])
            self.assertFalse(result["anchor"])
            self.assertEqual([], result["items"])
        self.assertEqual([], list(self.root.iterdir()))

        accepted, prompted = self.operator_terminal(outside, check_panel)
        self.assertTrue(prompted)
        self.assertEqual(0, accepted["code"], accepted)
        self.assertTrue(accepted["anchor"])
        self.assertIn("sentinel", accepted["items"])
        self.assertIn("anchor", [item["item"] for item in accepted["evidence"]["inventory"]])
        records = CrossJournal(self.root / "cross").inspect_disk().records
        enable = [row for row in records if row.get("phase") in {"enable_started", "enable_complete"}]
        self.assertEqual([("enable_started", 2), ("enable_complete", 2)],
                         [(row["phase"], row["schema_version"]) for row in enable])
        template, _, _ = tuple(_published_enable_vectors())[-1]
        start, complete = enable
        self.assertEqual(sha256(_panel_preimage(template, start)).hexdigest(),
                         start["human_act"]["rendered_digest"])
        from floati.cross_coordinates import semantic
        self.assertEqual(sha256(encode_frame(semantic(start))[:-1]).hexdigest(), complete["approval_sha256"])

    def test_completion_and_review_verify_the_published_issuing_panel_vectors(self):
        from floati.cross_attended import approved_completion, review_item
        from floati.errors import ProtocolRefusal

        for template, start, vectors in _published_enable_vectors():
            with self.subTest(template=template):
                preimage = _panel_preimage(template, start)
                rendered = start["human_act"]["rendered_digest"]
                self.assertEqual(rendered, sha256(preimage).hexdigest())
                try:
                    complete = approved_completion(start)
                except ProtocolRefusal as exc:
                    self.fail(f"Published approval for {template} refused with {exc.code}")
                review = review_item(start, complete)
                self.assertEqual(rendered, review["rendered_digest"])
                if vectors is not None:
                    self.assertEqual(vectors, (len(preimage), rendered, complete["approval_sha256"],
                                               review["transition_sha256"], review["id"],
                                               sha256(encode_frame(review)[:-1]).hexdigest()))

    def test_resume_preserves_historical_terminal_approval_in_both_record_formats(self):
        from floati import cross_optin
        from floati.cross_keystore import FixtureStore
        from tests.cross_journal_harness import CrossJournal as HistoricalJournal

        template, fixture, _ = next(_published_enable_vectors())
        for version in (1, 2):
            with self.subTest(version=version):
                self.root = self.root.parent / f"historical-{version}"
                self.root.mkdir()
                store, inventory = FixtureStore(), Inventory()
                start = dict(fixture, schema_version=version, enable_generation=1, plan=cross_optin._plan(
                    cross_optin.FloatiRoot.open_direct_home(self.root)))
                start["human_act"] = dict(fixture["human_act"],
                                          rendered_digest=sha256(_panel_preimage(template, start)).hexdigest())
                effects = cross_optin._EnableInventory(inventory)
                journal = HistoricalJournal(self.root / "cross", anchor_store=store.anchor, quota_store=store.quota,
                                            enable_inventory=effects, enable_approval=lambda phase, body: body == start)
                effects.journal = journal
                def historical_writer(enable_id, generation, token, plan, *, schema_version):
                    body = dict(start, enable_id=enable_id, enable_generation=generation, token=token,
                                plan=plan, schema_version=schema_version)
                    body["human_act"] = dict(start["human_act"],
                                             rendered_digest=sha256(_panel_preimage(template, body)).hexdigest())
                    return body

                # Issue through the old panel encoder only to create the historical input.
                with patch("floati.cross_journal.approved_start", side_effect=historical_writer):
                    journal.create(start["enable_id"], enable_generation=1, attended=start)
                with patch.object(cross_optin, "open_backend", return_value=(store, inventory)), (
                    patch.object(cross_optin, "confirm", side_effect=AssertionError("resume cannot ask again"))
                ):
                    code, evidence = self.cli("enable", "--resume")
                    self.assertEqual(0, code, evidence)
                    review, = journal.attended_reviews()
                    self.assertEqual(start["human_act"]["rendered_digest"], review["rendered_digest"])
                    self.assertEqual(sha256(encode_frame(start)[:-1]).hexdigest(), review["approval_sha256"])
                    self.assertEqual(0, self.cli("enable", "--resume")[0])
                    self.assertEqual((review,), journal.attended_reviews())

    def review_mutations(self):
        """Run the attended witness against the three review mutations on demand."""
        from floati import cross_optin
        from floati.errors import ProtocolRefusal

        outside_refusal = ProtocolRefusal("cross_human_act_unavailable", "fixture refusal")
        mutations = (
            ("O1", patch.object(cross_optin, "confirm", return_value=False)),
            ("O2", patch.object(cross_optin, "outside_seat", side_effect=outside_refusal)),
            ("O4", patch.object(cross_optin, "_ITEMS", tuple(item for item in cross_optin._ITEMS if item != "anchor"))),
        )
        for name, mutation in mutations:
            with self.subTest(mutation=name), mutation:
                result = unittest.TestResult()
                witness = CrossOptinTests("test_controlling_terminal_approves_exact_inventory_only_outside_a_seat")
                witness.run(result)
                self.assertEqual(1, result.testsRun)
                self.assertEqual([], result.errors)
                self.assertEqual(1, len(result.failures), f"{name} survived")
                print(f"{name} KILLED by {witness.id()}")

    def test_terminal_refusal_writes_nothing_and_affirmative_enable_has_one_review(self):
        store, _ = self.fixture()
        with patch("floati.cross_optin.confirm", return_value=False):
            code, evidence = self.cli("enable")
        self.assertEqual(20, code, evidence)
        self.assertEqual([], list(self.root.iterdir()))
        self.assertIsNone(store.anchor.read())
        with patch("floati.cross_optin.confirm", return_value=True):
            code, evidence = self.cli("enable")
        self.assertEqual(0, code, evidence)
        self.assertEqual("enabled", evidence["state"])
        self.assertIsInstance(store.anchor.read(), Anchor)
        reader = self.reader(store)
        self.assertEqual(1, len(reader.attended_reviews()))
        self.assertEqual(0, self.cli("enable", "--resume")[0])
        self.assertEqual(1, len(reader.attended_reviews()))

    def test_resume_resolves_the_original_create_after_an_outstanding_return(self):
        from floati.cross_keystore import WriteOutcome
        store, _ = self.fixture()
        store.interrupt_next(WriteOutcome.OUTSTANDING)
        with patch("floati.cross_optin.confirm", return_value=True):
            code, evidence = self.cli("enable")
        self.assertEqual(35, code, evidence)
        self.assertEqual(35, self.cli("enable", "--resume")[0])
        store.settle(applied=True)
        self.assertIsInstance(store.anchor.read(), EnableCreate)
        code, evidence = self.cli("enable", "--resume")
        self.assertEqual(0, code, evidence)
        self.assertEqual(1, len(self.reader(store).attended_reviews()))

    def test_resume_at_anchor_installation_and_completion_keeps_original_review(self):
        from floati.cross_keystore import FixtureStore, WriteOutcome
        from floati import cross_optin

        for count in (1, 2):
            for applied in (False, True):
                with self.subTest(count=count, applied=applied):
                    self.root = self.root.parent / f"fleet-{count}-{int(applied)}"
                    self.root.mkdir()
                    store, inventory = FixtureStore(), Inventory()
                    replace = store.replace
                    interrupted = False

                    def lose_return(slot, expected, successor):
                        nonlocal interrupted
                        if not interrupted and isinstance(successor, Anchor) and successor.trust[2] == count:
                            interrupted = True
                            return replace(slot, expected, successor, outcome=WriteOutcome.UNKNOWN)
                        return replace(slot, expected, successor)

                    with patch.object(cross_optin, "open_backend", return_value=(store, inventory)), (
                        patch.object(cross_optin, "outside_seat")
                    ), patch.object(cross_optin, "confirm", return_value=True):
                        with patch.object(store, "replace", side_effect=lose_return):
                            code, evidence = self.cli("enable")
                            self.assertEqual(35, code, evidence)
                            self.assertTrue(interrupted)
                            self.assertEqual(35, self.cli("enable", "--resume")[0])
                        store.settle(applied=applied)
                        code, evidence = self.cli("enable", "--resume")
                        self.assertEqual(0, code, evidence)
                        reader = self.reader(store)
                        old_review, = reader.attended_reviews()
                        self.assertEqual(0, self.cli("enable", "--resume")[0])
                        self.assertEqual((old_review,), reader.attended_reviews())

    def test_disable_retires_the_journal_stop_and_reports_pending_teardown(self):
        store, inventory = self.fixture()
        with patch("floati.cross_optin.confirm", return_value=True):
            self.assertEqual(0, self.cli("enable")[0])
        original_remove = inventory.remove
        checks = {"unit_absent": True, "unit_not_loadable": True, "no_listener": False, "no_live_endpoint": True}
        with self.subTest(interruption="key removal"):
            def fail_key_removal(item, start, proof):
                if item == "sentinel":
                    raise OSError(EIO, "fixture store item removal failed")
                original_remove(item, start, proof)

            with patch.object(inventory, "remove", side_effect=fail_key_removal):
                code, evidence = self.cli("disable")
            self.assertEqual(35, code, evidence)
            self.assertEqual("confirmed", evidence["independent_denial"])
            self.assertEqual("pending", evidence.get("physical_teardown"))
            outcomes = {item["item"]: item["class"] for item in evidence["inventory"]}
            self.assertIn("sentinel", inventory.items)
            self.assertEqual({"anchor": "confirmed_absent", "ballast": "confirmed_absent",
                              "configuration": "confirmed_absent", "gate": "confirmed_absent",
                              "sentinel": "unproved", "unit": "unproved"}, outcomes)
            self.assertEqual({key: None for key in checks}, evidence["checks"])
        with patch.object(inventory, "checks", return_value=checks):
            code, evidence = self.cli("disable")
        self.assertEqual(35, code, evidence)
        self.assertEqual("cross_closeout_unproved", evidence["code"])
        self.assertEqual("receipt", evidence["step"])
        self.assertEqual("published", evidence["journal"])
        self.assertEqual("confirmed", evidence["independent_denial"])
        self.assertEqual("pending", evidence.get("physical_teardown"))
        self.assertEqual(checks, evidence["checks"])
        self.assertEqual({"confirmed_absent"}, {item["class"] for item in evidence["inventory"]})
        self.assertEqual({}, inventory.items)
        phases = [row.get("phase") for row in self.reader(store).inspect_disk().records]
        self.assertIn("disable_started", phases)
        self.assertNotIn("disable_complete", phases)
        self.assertIsNone(store.anchor.read())

    def test_disable_confirms_measured_removal_and_replays_its_receipt(self):
        store, inventory = self.fixture()
        with patch("floati.cross_optin.confirm", return_value=True):
            self.assertEqual(0, self.cli("enable")[0])
        ballast = self.root / "cross" / "closeout.ballast"
        self.assertTrue(ballast.is_file())
        expected = {"anchor", "ballast", *inventory.items}
        code, evidence = self.cli("disable")
        self.assertEqual(0, code, evidence)
        self.assertIsNone(store.anchor.read())
        self.assertEqual({}, inventory.items)
        self.assertFalse(ballast.exists())
        self.assertEqual("complete", evidence["state"])
        receipt = evidence["receipt"]
        self.assertEqual(expected, {item["item"] for item in receipt["items"]})
        self.assertEqual({"confirmed_absent"}, {item["class"] for item in receipt["items"]})
        self.assertEqual({"removed_and_verified"}, {item["cause"] for item in receipt["items"]})
        self.assertTrue(all(receipt["checks"].values()))
        code, replay = self.cli("disable")
        self.assertEqual(0, code, replay)
        self.assertEqual(receipt, replay["receipt"])

    def test_disable_refuses_history_from_another_root_before_cleanup(self):
        from floati.cross_keystore import FixtureStore
        from tests.cross_journal_harness import materialize, snapshot

        origin = self.root
        foreign = origin.parent / "foreign"
        foreign.mkdir()
        source_store, source_inventory = FixtureStore(), Inventory()
        target_store, target_inventory = FixtureStore(), Inventory()
        stores = {str(origin): (source_store, source_inventory), str(foreign): (target_store, target_inventory)}
        with patch("floati.cross_optin.open_backend", side_effect=lambda root, kind: stores[str(root.tenant_home)]), (
            patch("floati.cross_optin.confirm", return_value=True)
        ):
            self.assertEqual(0, self.cli("enable")[0])
            for complete in (False, True):
                with self.subTest(complete=complete):
                    self.root = origin
                    with patch.object(source_inventory, "checks",
                                      wraps=source_inventory.checks) as checks:
                        if not complete:
                            checks.return_value = {}
                        code, evidence = self.cli("disable")
                    self.assertEqual(0 if complete else 35, code, evidence)
                    self.assertEqual("confirmed", evidence["independent_denial"])
                    image = snapshot(origin / "cross")
                    materialize(image, foreign / "cross")
                    self.root = foreign
                    with patch.object(target_inventory, "inventory", wraps=target_inventory.inventory) as measure:
                        code, evidence = self.cli("disable")
                    self.assertEqual(20, code, evidence)
                    self.assertEqual("cross_closeout_attribution_unproved", evidence["code"])
                    self.assertNotEqual("complete", evidence.get("state"))
                    self.assertNotEqual("confirmed", evidence.get("independent_denial"))
                    measure.assert_not_called()
                    self.assertEqual(image, snapshot(foreign / "cross"))

    def test_fresh_attendance_adopts_only_an_unbound_genesis(self):
        from tests.cross_journal_harness import snapshot

        store, _ = self.fixture()
        directory = self.root / "cross"
        with patch("floati.cross_optin.confirm", return_value=True):
            with patch.object(Storage, "append", side_effect=OSError(EIO, "fixture interruption")):
                code, evidence = self.cli("enable")
            self.assertEqual(35, code, evidence)
            genesis = CrossJournal(directory).inspect_disk()
            image = snapshot(directory)
            self.assertIsNotNone(genesis.journal_id)
            self.assertEqual((), genesis.records)
            self.assertIsNone(store.anchor.read())
            self.assertEqual(20, self.cli("enable", "--resume")[0])
            code, evidence = self.cli("enable")
        self.assertEqual(0, code, evidence)
        reader = self.reader(store)
        self.assertNotEqual(genesis.journal_id, reader.read_committed(require_settled=True).journal_id)
        self.assertEqual(1, len(reader.attended_reviews()))
        self.assertEqual(0, self.cli("disable")[0])
        for name in snapshot(directory):
            (directory / name).unlink()
        for name, payload in image.items():
            (directory / name).write_bytes(payload)
        with patch("floati.cross_optin.confirm", return_value=True):
            code, evidence = self.cli("enable")
        self.assertEqual(35, code, evidence)
        self.assertEqual("provenance", evidence["step"])
        self.assertIsNone(store.anchor.read())
        self.assertEqual(image, snapshot(directory))

    def test_fresh_attendance_adopts_directory_with_independent_new_root_proof(self):
        store, _ = self.fixture()
        (self.root / "cross").mkdir()
        with patch("floati.cross_optin.confirm", return_value=True):
            code, evidence = self.cli("enable")
        self.assertEqual(0, code, evidence)
        self.assertIsInstance(store.anchor.read(), Anchor)

    def test_disable_disposes_partial_enable_inventory_under_the_same_stop(self):
        from floati.cross_keystore import WriteOutcome

        store, inventory = self.fixture()
        replace = store.replace

        def interrupt_initial_anchor(slot, expected, successor):
            if isinstance(expected, EnableCreate) and isinstance(successor, Anchor):
                return replace(slot, expected, successor, outcome=WriteOutcome.UNAPPLIED)
            return replace(slot, expected, successor)

        with patch.object(store, "replace", side_effect=interrupt_initial_anchor), (
            patch("floati.cross_optin.confirm", return_value=True)
        ):
            self.assertEqual(35, self.cli("enable")[0])
        self.assertIsInstance(store.anchor.read(), EnableCreate)
        self.assertIn("sentinel", inventory.items)
        code, evidence = self.cli("disable")
        self.assertEqual(0, code, evidence)
        self.assertIsNone(store.anchor.read())
        self.assertEqual({}, inventory.items)
        self.assertEqual(20, self.cli("enable", "--resume")[0])

    def test_completed_enable_retries_after_lawful_pair_activation(self):
        from floati import cross_optin
        from floati.cross_pair_storage import JournalOpenings
        from floati.root import FloatiRoot
        from tests.cross_pair_fixtures import seed_activation

        store, inventory = self.fixture()
        with patch("floati.cross_optin.confirm", return_value=True):
            code, initial = self.cli("enable")
        self.assertEqual(0, code, initial)
        root = FloatiRoot.open_direct_home(self.root)
        _, enable_inventory = cross_optin._journal(root, store, inventory, approve=lambda phase, start: False)
        journal = CrossJournal.for_root(
            root, provenance=store, anchor_store=store.anchor, quota_store=store.quota,
            enable_inventory=enable_inventory, enable_approval=lambda phase, body: False,
            prove_pair_material=lambda record, *, phase: True,
        )
        seed_activation(journal, JournalOpenings(journal).open("S"), "a" * 32, "peer")
        before = journal.read_committed(require_settled=True)
        self.assertGreater(store.anchor.read().trust[2], 2)
        original_review = journal.attended_reviews()
        code, retry = self.cli("enable", "--resume")
        self.assertEqual(0, code, retry)
        self.assertEqual(initial, retry)
        self.assertEqual(before, journal.read_committed(require_settled=True))
        self.assertEqual(original_review, journal.attended_reviews())

    def test_missing_journal_after_anchor_removal_cannot_become_a_fresh_enable(self):
        store, _ = self.fixture()
        with patch("floati.cross_optin.confirm", return_value=True):
            self.assertEqual(0, self.cli("enable")[0])
            self.assertTrue(store.anchor.compare_and_set(store.anchor.read(), None))
            (self.root / "cross").rename(self.root.parent / "saved-cross")
            code, evidence = self.cli("enable")
        self.assertIn(code, {20, 35}, evidence)
        self.assertEqual([], list(self.root.iterdir()))
        self.assertIsNone(store.anchor.read())

    def test_store_outcomes_fence_following_effects_until_independent_settlement(self):
        from floati.cross_keystore import FixtureStore, ReadOutcome, WriteOutcome
        for outcome in WriteOutcome:
            with self.subTest(outcome=outcome):
                store = FixtureStore()
                self.assertIs(outcome, store.replace("sentinel", None, b"fixture", outcome=outcome))
                expected = {
                    WriteOutcome.APPLIED: ReadOutcome.PRESENT,
                    WriteOutcome.UNAPPLIED: ReadOutcome.CONFIRMED_ABSENT,
                    WriteOutcome.OUTSTANDING: ReadOutcome.OUTSTANDING,
                    WriteOutcome.UNKNOWN: ReadOutcome.UNKNOWN,
                }
                self.assertIs(expected[outcome], store.observe("sentinel").outcome)
                if outcome in {WriteOutcome.OUTSTANDING, WriteOutcome.UNKNOWN}:
                    self.assertIs(outcome, store.replace("anchor", None, b"must not apply"))
                    store.settle(applied=True)
                    self.assertEqual(b"fixture", store.observe("sentinel").value)
                    self.assertIs(ReadOutcome.CONFIRMED_ABSENT, store.observe("anchor").outcome)
