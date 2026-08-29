#!/usr/bin/env python3
"""Run the exact one-million-record local sequencer hostile-scale gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from floati.sequencer_scale import ScaleConfig, run_scale_fixture


def main() -> int:
    artifact = run_scale_fixture(
        ScaleConfig(
            max_records=10_000,
            batch_size=50,
            client_count=100,
            item_count=10_000,
            lifecycle_record_count=1_000_000,
            restart_batch_ordinals=(4_000, 10_000, 16_000),
        )
    )
    print(json.dumps(artifact, sort_keys=True, separators=(",", ":")))
    return 0 if artifact["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
