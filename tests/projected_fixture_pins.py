"""Exact byte pins for recorded fixtures before and after public exposure.

The exporter preserves decoded JSON path values while escaping their literal
spelling. A source digest therefore cannot also identify the projected bytes.
The source anchors below stay fixed; the separate projected pins are derived
by ``tests/regen_projected_fixture_pins.py`` through the real exporter.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from tests.export_inventory import export_policy_is_present


ROOT = Path(__file__).resolve().parents[1]
PINS_RELATIVE = "tests/fixtures/projected-fixture-pins.v0.json"
SOURCE_FIXTURE_DIGESTS = {
    "tests/fixtures/bus-scale-census/native-command-contract-dca44932.json":
        "cf70f08cec6725a53cb58066833495fa68a4b68e16c81bf5b0507b9b2f31889b",
    "tests/fixtures/bus-scale-census/native-command-contract-1f2f40f6.json":
        "a1fb6f496762b9c56164ecef3eb6d6c189f3698cd9c0c38b27206fd2328560f8",
    "tests/fixtures/bus-scale-census/native-command-contract-f971841e.json":
        "81ebacc8fa0a9661d3e4e8fb268eac3fd88ee4a4b3f544bb0964556572502faf",
    "tests/fixtures/bus-scale-census/native-command-amendment-7d0c404e.json":
        "0aae1d48ec08b1ffb4bf8997e96bdc37e890bdc152819cf7af297bd7026c45b8",
    "tests/fixtures/bus-scale-census/native-command-amendment-d6263c86.json":
        "49ebe13c9764e4f7653466a05364301615d15e13f23675f4517df80e4abf3f49",
    "tests/fixtures/bus-scale-census/native-command-amendment-7faae5cd.json":
        "f97fca08deced2ecd016a7b849e4367e5118fdd5a3be6268ad99b36f68fcfc7a",
    "tests/fixtures/bus-scale-census/native-command-amendment-4aa55427.json":
        "c19992536c3309ca5dfa69ab1bf0ad89220bc6ace61b95d54c301c3ed0c85c3a",
    "tests/fixtures/bus-scale-census/native-command-amendment-0aeb5d0a.json":
        "1d564e1d88343cd278a709e56a90063b59615ec9bedae81c29b645872bb21140",
    "tests/fixtures/node-spinup-command-contract.v0.json":
        "57c5fd86638fff7456513814f152d5abfe2102253e55fc60aeaa6093087d1e80",
    "tests/fixtures/update-prepare-command-contract.v0.json":
        "0b7be914b7be262b9337d65e27e09483d701095f4a1e4b6e33b53a62f91d76e1",
    "tests/fixtures/g1-witness-artifact/native-completed.json":
        "9542c2030e6a4a08412e46ebac89205144768333e2bb0f64ceea3f9025cac30d",
}
_DIGEST = re.compile(r"[0-9a-f]{64}")


def expected_fixture_sha256(path: Path) -> str:
    """Choose one exact pin for this tree, never bless the bytes being read."""

    relative = path.relative_to(ROOT).as_posix()
    source_digest = SOURCE_FIXTURE_DIGESTS[relative]
    document = json.loads((ROOT / PINS_RELATIVE).read_text(encoding="utf-8"))
    if (type(document) is not dict or set(document) != {"schema_version", "fixtures"}
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 0
            or type(document["fixtures"]) is not dict
            or set(document["fixtures"]) != set(SOURCE_FIXTURE_DIGESTS)):
        raise ValueError("projected fixture pin document has an invalid population")
    for name, row in document["fixtures"].items():
        if (type(row) is not dict
                or set(row) != {"source_sha256", "projected_sha256", "adapters"}
                or row["source_sha256"] != SOURCE_FIXTURE_DIGESTS[name]
                or type(row["projected_sha256"]) is not str
                or not _DIGEST.fullmatch(row["projected_sha256"])
                or type(row["adapters"]) is not list
                or any(type(item) is not str for item in row["adapters"])):
            raise ValueError("projected fixture pin document changed its source binding")
    if export_policy_is_present(ROOT):
        return source_digest
    return document["fixtures"][relative]["projected_sha256"]
