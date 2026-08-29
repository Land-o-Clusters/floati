#!/usr/bin/env python3
"""Render docs/capability-matrix.v0.json as README grid markdown (stdlib)."""
from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional

# Compaction column = each harness's OWN native compact verb (not floati's).
# Dataset columns[].title for id "compaction" must match this constant.
COMPACTION_COLUMN_ID = "compaction"
COMPACTION_HEADER = "native compact verb"


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("dataset must be a JSON object")
    if int(payload.get("schema_version", -1)) != 0:
        raise SystemExit("unsupported schema_version")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise SystemExit("dataset.records must be a non-empty list")
    return payload


def _cell(record: dict) -> str:
    value = record["value"]
    receipt = record["receipt_path"]
    if not isinstance(value, str) or not isinstance(receipt, str):
        raise SystemExit("each record needs string value and receipt_path")
    if not value or not receipt:
        raise SystemExit("uncitable empty cell: value and receipt_path are required")
    return "[{0}]({1})".format(value, receipt)


def render(dataset: dict) -> str:
    columns = dataset["columns"]
    col_ids = [column["id"] for column in columns]
    index = {}
    for record in dataset["records"]:
        key = (record["harness"], record["surface"], record["capability"])
        if key in index:
            raise SystemExit("duplicate record: {0}".format(key))
        index[key] = record

    rows = dataset.get("row_order")
    if not rows:
        seen = OrderedDict()
        for record in dataset["records"]:
            seen[(record["harness"], record["surface"])] = True
        rows = list(seen)

    lines = []
    universal = dataset.get("universal", "").strip()
    if universal:
        lines.append(universal)
        lines.append("")

    titles = []
    for column in columns:
        title = column["title"]
        if column["id"] == COMPACTION_COLUMN_ID:
            if title != COMPACTION_HEADER:
                raise SystemExit(
                    "compaction column title must be {0!r}, got {1!r}".format(
                        COMPACTION_HEADER, title
                    )
                )
            title = COMPACTION_HEADER
        titles.append(title)
    header = ["harness / surface"] + titles
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    for harness, surface in rows:
        cells = ["{0} / {1}".format(harness, surface)]
        for cap in col_ids:
            record = index.get((harness, surface, cap))
            if record is None:
                raise SystemExit("missing record: {0}/{1}/{2}".format(harness, surface, cap))
            cells.append(_cell(record))
        lines.append("| " + " | ".join(cells) + " |")

    notes = dataset.get("provider_notes") or []
    if notes:
        lines.append("")
        lines.append("Provider notes (wiring matrix; not grid columns):")
        lines.append("")
        for note in notes:
            lines.append(
                "- {0} / {1} — [{2}]({3}): {4}".format(
                    note["harness"],
                    note["surface"],
                    note["value"],
                    note["receipt_path"],
                    note["text"],
                )
            )

    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Render the capability matrix markdown grid.")
    parser.add_argument(
        "--dataset",
        default="docs/capability-matrix.v0.json",
        help="Path to capability-matrix.v0.json",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Write markdown here, or - for stdout",
    )
    args = parser.parse_args(argv)
    dataset_path = Path(args.dataset)
    markdown = render(_load(dataset_path))
    if args.output == "-":
        sys.stdout.write(markdown)
    else:
        Path(args.output).write_text(markdown, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
