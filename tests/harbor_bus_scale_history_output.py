"""Native export cardinality witnesses, not performance classifications."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from floati import cli
from floati.framing import decode_frames
from floati.root import FloatiRoot
from tests.bus_scale_test_profile import banked_send_repository
from tests.bus_scale_case_seeds import make_case_seed_pair
from tests.bus_scale_test_profile import seed_counts
from tests.bus_scale_workloads import _CASES
from tests.temp_roots import REAL_TEMP_ROOT


ROOT = Path(__file__).resolve().parents[1]


class NativeHistoryOutputTests(unittest.TestCase):
    def test_complete_confluence_bundle_grows_with_exported_history(self):
        specification = importlib.util.spec_from_file_location("native_export_cardinality_writes",
            ROOT / "tests/fixtures/bus_scale_census/bus-scale-routine-writes-2026-10-15.py")
        helper = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(helper)
        witnesses = []
        with tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-export-", dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary).resolve()
            workspace, _sha = banked_send_repository(base)
            workspace.chmod(0o700)
            for family, adjustment in (("events", -2), ("deliveries", 3)):
                expected_counts = tuple(count + adjustment for count in seed_counts())
                bank = base / family
                bank.mkdir(mode=0o700)
                pair = make_case_seed_pair(bank, family, "confluence-bundle", helper, workspace, counts=seed_counts())
                arms = []
                for scale, expected_count in zip(("1", "10"), expected_counts):
                    home = pair[scale]
                    home.parent.chmod(0o700)
                    root = FloatiRoot.open_direct_home(home, create=False)
                    before = {path.relative_to(home).as_posix(): path.read_bytes()
                              for path in home.rglob("*") if path.is_file()}
                    argv = helper.arguments("confluence-bundle", home, workspace, pair["context"])
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = cli.main(argv)
                    artifact = json.loads(output.getvalue())
                    self.assertEqual((0, "ok"), (code, artifact["status"]), artifact)
                    native = helper.witness("confluence-bundle", home, before, artifact, pair["context"], {})
                    self.assertEqual({"exit_code": 0, "status": "ok", "effect": {
                        "kind": "confluence_bundle_written", "count": 1}}, native)
                    publication = Path(artifact["evidence"]["out"])
                    raw = publication.read_bytes()
                    document = json.loads(raw)
                    self.assertEqual(expected_count, len(document["entries"]))
                    self.assertEqual(expected_count, artifact["evidence"]["entries"])
                    self.assertEqual(list(range(1, expected_count + 1)),
                                     [entry["sequence"] for entry in document["entries"]])
                    # Every exported source ordinal must retain the full exact
                    # native record. This is an actual complete publication,
                    # not an exporter size estimate or a mock response.
                    source_rows = {}
                    for entry in document["entries"]:
                        relative = entry["source"]
                        if relative not in source_rows:
                            source_rows[relative] = decode_frames(root.resolve_relative(relative).read_bytes())
                        self.assertEqual(source_rows[relative][entry["source_ordinal"] - 1], entry["record"])
                    source_counts = {source: sum(entry["source"] == source for entry in document["entries"])
                                     for source in source_rows}
                    expected_sources = ({"events.jsonl": expected_count} if family == "events" else {
                        "events.jsonl": 2, "receipts/deliveries/architect.jsonl": dict(zip(("1", "10"), seed_counts()))[scale],
                        "receipts/acks/architect.jsonl": 1})
                    self.assertEqual(expected_sources, source_counts)
                    self.assertEqual(before, {path.relative_to(home).as_posix(): path.read_bytes()
                                              for path in home.rglob("*") if path.is_file()})
                    arms.append({"scale": scale, "export_rows": expected_count, "export_bytes": len(raw),
                                 "export_sha256": hashlib.sha256(raw).hexdigest(), "sources": source_counts,
                                 "selected_publications": native["effect"]["count"]})
                self.assertEqual(seed_counts()[1] - seed_counts()[0], arms[1]["export_rows"] - arms[0]["export_rows"])
                self.assertGreater(arms[1]["export_bytes"], arms[0]["export_bytes"])
                witnesses.append({"family": family, "case": "confluence-bundle", "arms": arms})
        print(json.dumps({"control": "native-confluence-history-output", "witnesses": witnesses}, sort_keys=True))
        self.assertIs(True, _CASES["confluence-bundle"]["history_output"],
                      "complete native bundle output grows by 9000 exported rows; declare history output")
