#!/usr/bin/env python3
"""Refresh the standalone gateway's install limits and its vendored digest."""

import argparse
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
GATEWAY = Path("tools/codex/codex-fleet-bus.py")
BEGIN = b"# BEGIN GENERATED INSTALL LIMITS\n"
END = b"# END GENERATED INSTALL LIMITS\n"


def render_gateway(source, limits):
    before, begin, generated = source.partition(BEGIN)
    _, end, after = generated.partition(END)
    if not begin or not end or source.count(BEGIN) != 1 or source.count(END) != 1:
        raise ValueError("expected one generated install limits block")
    definitions = "".join(f"{name} = {getattr(limits, name)}\n" for name in (
        "MAX_INSTALL_FILES", "MAX_INSTALL_FILE_BYTES", "MAX_INSTALL_TOTAL_BYTES",
        "MAX_MANIFEST_BYTES",
    ))
    comment = b"# Regenerate from floati/install_limits.py with scripts/regen_codex_gateway_limits.py.\n"
    return before + BEGIN + comment + definitions.encode("ascii") + END + after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from floati import install_limits

    gateway = ROOT / GATEWAY
    current = gateway.read_bytes()
    wanted = render_gateway(current, install_limits)
    pin = gateway.with_suffix(".sha256.json")
    record = {
        "path": GATEWAY.as_posix(), "schema_version": 0,
        "sha256": hashlib.sha256(wanted).hexdigest(),
    }
    wanted_pin = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if current == wanted and pin.read_text(encoding="utf-8") == wanted_pin:
        return 0
    if not args.write:
        print("Codex gateway limits or digest drifted. Run with --write.")
        return 1
    gateway.write_bytes(wanted)
    pin.write_text(wanted_pin, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
