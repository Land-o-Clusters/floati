#!/usr/bin/env python3
"""Verify the exact Linux shard union and return the combined verdict."""

import argparse
import json
from pathlib import Path
import sys

from parallel_plan import reconcile_shards


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", required=True, type=int)
    parser.add_argument("--head", required=True)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("receipts", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        reports = [json.loads(path.read_text(encoding="utf-8")) for path in args.receipts]
        summary = reconcile_shards(reports, args.shards, args.head, plan_only=args.plan_only)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"invalid shard census: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["successful"] else 1


if __name__ == "__main__":
    sys.exit(main())
