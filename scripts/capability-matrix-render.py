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


def _coordinate(record: dict) -> str:
    return "{0}/{1}".format(record["harness"], record["surface"])


def _declared_current_versions(dataset: dict) -> dict:
    declared = dataset.get("declared_current_versions") or {}
    if not isinstance(declared, dict):
        raise SystemExit("declared_current_versions must be an object")
    for coordinate, current in declared.items():
        if not isinstance(coordinate, str) or not isinstance(current, dict):
            raise SystemExit("each declared current version needs a coordinate and object")
        for field in ("version", "measured_at", "receipt_path"):
            if not isinstance(current.get(field), str) or not current[field]:
                raise SystemExit(
                    "declared current version {0!r} needs string {1}".format(
                        coordinate, field
                    )
                )
    return declared


def _version_is_stale(dataset: dict, record: dict) -> bool:
    current = _declared_current_versions(dataset).get(_coordinate(record))
    if current is None:
        return False
    versions = record.get("versions")
    if not isinstance(versions, dict):
        raise SystemExit("each record needs a versions object")
    measured = versions.get(record["surface"])
    if not isinstance(measured, str) or not measured:
        raise SystemExit(
            "record {0}/{1} needs a measured version for its surface".format(
                _coordinate(record), record["capability"]
            )
        )
    return measured != current["version"]


def _version_honesty(dataset: dict) -> str:
    parts = []
    declared = _declared_current_versions(dataset)
    records = dataset["records"]
    for coordinate, current in sorted(declared.items()):
        measured = {}
        for record in records:
            if _coordinate(record) != coordinate or not _version_is_stale(dataset, record):
                continue
            version = record["versions"][record["surface"]]
            measured.setdefault(version, set()).add(record["measured_at"])
        if not measured:
            continue
        measured_text = "; ".join(
            "{0} at {1}".format(version, " and ".join(sorted(measured_at)))
            for version, measured_at in sorted(measured.items())
        )
        parts.append(
            "{0} declared current [{1} at {2}]({3}); cells marked "
            "`version_stale: true` were measured at {4} and keep those receipt-bound "
            "stamps.".format(
                coordinate,
                current["version"],
                current["measured_at"],
                current["receipt_path"],
                measured_text,
            )
        )
    if not parts:
        return ""
    return "Version honesty: " + " ".join(parts)


def _cell(dataset: dict, record: dict) -> str:
    value = record["value"]
    receipt = record["receipt_path"]
    if not isinstance(value, str) or not isinstance(receipt, str):
        raise SystemExit("each record needs string value and receipt_path")
    if not value or not receipt:
        raise SystemExit("uncitable empty cell: value and receipt_path are required")
    cell = "[{0}]({1})".format(value, receipt)
    if _version_is_stale(dataset, record):
        cell += " · `version_stale: true`"
    return cell


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
    orchestrator = dataset.get("orchestrator_note", "").strip()
    if orchestrator:
        lines.append(orchestrator)
        lines.append("")
    surface_scope = dataset.get("surface_scope_note", "").strip()
    if surface_scope:
        lines.append(surface_scope)
        lines.append("")
    version_honesty = _version_honesty(dataset)
    if version_honesty:
        lines.append(version_honesty)
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
            cells.append(_cell(dataset, record))
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




_GRADE_MARKS = {"measured": "●", "classified": "○"}


def _compact_cell(dataset: dict, record: dict) -> str:
    cell = _cell(dataset, record)
    grade = record.get("grade")
    if grade:
        mark = _GRADE_MARKS.get(grade)
        if mark is None:
            raise SystemExit("unknown grade: {0!r}".format(grade))
        cell += " " + mark
    return cell


def render_compact(dataset: dict) -> str:
    """README projection: feature-parity tables, CLI split from desktop/GUI.

    Every cell stays receipt-linked; wake cells carry their measurement grade;
    harness-specific deep integrations move to a notes paragraph so one
    harness's head start does not render as everyone else's gap. The full grid
    lives in docs/capability-matrix.md (mode=full).
    """

    index = {}
    for record in dataset["records"]:
        index[(record["harness"], record["surface"], record["capability"])] = record
    rows = dataset.get("row_order")
    if not rows:
        seen = OrderedDict()
        for record in dataset["records"]:
            seen[(record["harness"], record["surface"])] = True
        rows = list(seen)
    row_notes = dataset.get("row_notes", {})

    lines = []
    universal = dataset.get("universal", "").strip()
    if universal:
        lines.append(universal)
        lines.append("")
    orchestrator = dataset.get("orchestrator_note", "").strip()
    if orchestrator:
        lines.append(orchestrator)
        lines.append("")
    surface_scope = dataset.get("surface_scope_note", "").strip()
    if surface_scope:
        lines.append(surface_scope)
        lines.append("")
    version_honesty = _version_honesty(dataset)
    if version_honesty:
        lines.append(version_honesty)
        lines.append("")

    lines.append("**CLI surfaces**")
    lines.append("")
    lines.append("| harness | bus | work | wake | notes |")
    lines.append("|---|---|---|---|---|")
    for harness, surface in rows:
        if surface != "cli":
            continue
        cells = [harness]
        for cap in ("bus", "work", "wake"):
            record = index.get((harness, surface, cap))
            if record is None:
                raise SystemExit("missing record: {0}/{1}/{2}".format(harness, surface, cap))
            cells.append(_compact_cell(dataset, record))
        cells.append(row_notes.get("{0}/{1}".format(harness, surface), ""))
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")
    lines.append("**Desktop / GUI surfaces**")
    lines.append("")
    lines.append("| harness / surface | wake | notes |")
    lines.append("|---|---|---|")
    for harness, surface in rows:
        if surface == "cli":
            continue
        record = index.get((harness, surface, "wake"))
        if record is None or record["value"] == "—":
            continue
        cells = [
            "{0} / {1}".format(harness, surface),
            _compact_cell(dataset, record),
            row_notes.get("{0}/{1}".format(harness, surface), ""),
        ]
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")
    lines.append(
        "● measured live · ○ classified from surfaces (the unexercised probe is named "
        "in the receipt) · — no receipt yet: we do not claim what we have not measured."
    )

    deep = []
    for cap, label in (("boot", "session boot"), ("managed_send", "managed send")):
        record = index.get(("codex", "cli", cap))
        if record is not None and record["value"] not in ("—", "n/a"):
            deep.append("[{0}]({1})".format(label, record["receipt_path"]))
    if deep:
        lines.append("")
        lines.append(
            "**Deep integrations (codex):** "
            + " · ".join(deep)
            + " — receipt-linked notes rather than grid columns, so one harness's "
            + "head start does not read as everyone else's gap. The full "
            + "{0}-surface grid, every cell receipt-linked, lives in ".format(len(rows))
            + "[docs/capability-matrix.md](docs/capability-matrix.md)."
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
    parser.add_argument(
        "--mode",
        choices=("compact", "full"),
        default="compact",
        help="compact = README projection (default); full = the complete grid",
    )
    args = parser.parse_args(argv)
    dataset_path = Path(args.dataset)
    dataset = _load(dataset_path)
    markdown = render_compact(dataset) if args.mode == "compact" else render(dataset)
    if args.output == "-":
        sys.stdout.write(markdown)
    else:
        Path(args.output).write_text(markdown, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
