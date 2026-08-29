#!/usr/bin/env python3
"""Local-only Claude print-mode fixture; never contacts a provider."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture-mode",
        choices=("complete", "approval", "failed", "malformed", "oversized", "hang"),
        default="complete",
    )
    parser.add_argument("-p", "--print", action="store_true")
    parser.add_argument("--input-format", required=True)
    parser.add_argument("--output-format", required=True)
    parser.add_argument("--permission-mode", required=True)
    parser.add_argument("--no-session-persistence", action="store_true")
    parser.add_argument("--tools", nargs="+", required=True)
    parser.add_argument("prompt")
    args = parser.parse_args()

    workspace = Path.cwd()
    evidence = workspace / ".floati"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "claude-fixture.json").write_text(
        json.dumps(
            {
                "cwd": str(workspace),
                "input_format": args.input_format,
                "output_format": args.output_format,
                "permission_mode": args.permission_mode,
                "no_session_persistence": args.no_session_persistence,
                "print": args.print,
                "prompt": args.prompt,
                "tools": args.tools,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if args.fixture_mode == "hang":
        time.sleep(30)
    if args.fixture_mode == "malformed":
        sys.stdout.write("not-json\n")
        return 0
    if args.fixture_mode == "oversized":
        sys.stdout.write("x" * (1024 * 1024 + 1))
        return 0
    if args.fixture_mode == "approval":
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "Permission required for unattended tool use",
                    "session_id": "019fbb00-0000-7000-8000-000000000002",
                },
                separators=(",", ":"),
            )
        )
        return 1
    if args.fixture_mode == "failed":
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "reference provider failure",
                    "session_id": "019fbb00-0000-7000-8000-000000000003",
                },
                separators=(",", ":"),
            )
        )
        return 1

    if not (
        args.print
        and args.input_format == "text"
        and args.output_format == "json"
        and args.permission_mode == "dontAsk"
        and args.no_session_persistence
        and args.tools == ["Read,Write,Edit"]
    ):
        return 64
    (workspace / "PROOF.txt").write_text(
        "FLOATI Claude fixture proof\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Created PROOF.txt",
                "session_id": "019fbb00-0000-7000-8000-000000000001",
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
