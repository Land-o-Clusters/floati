#!/usr/bin/env python3
"""Generate the deterministic FLOATI TUI state wall."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from pathlib import Path
from typing import Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
from floati.brand import render_buoy_mark  # noqa: E402
from floati.graph_render import render_harbor_chart  # noqa: E402

from floati.replay_render import render_replay_frame, render_replay_plain  # noqa: E402
from floati.tui_render import HarborBoardModel, render_frame, render_plain_dump  # noqa: E402


OBSERVED = "2026-08-01T12:00:10.000Z"
STATES = ("idle", "live", "degraded", "replay", "graph", "install", "selftest")
MODES = ("standard", "plain")
THEMES = ("dark", "light")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PALETTES = {
    "dark": {"background": "#12161c", "foreground": "#d8dee9", "accent": "#ff9f43", "dim": "#9aa6b2"},
    "light": {"background": "#f7f3eb", "foreground": "#20252c", "accent": "#853d07", "dim": "#55636f"},
}


def _model(state: str) -> HarborBoardModel:
    if state == "idle":
        return HarborBoardModel(
            observed_at=OBSERVED,
            nodes=(),
            work_items=(),
            deliveries=(),
            acknowledgments=(),
            denials=(),
            stale_leases=(),
            consumption={"coordinate": "work/items.jsonl", "state": "caught_up", "wake_state": "none"},
        )
    worker_state = "driving" if state == "live" else "degraded"
    transition = "drive" if state == "live" else "degrade"
    outcome = None if state == "live" else "process_timeout"
    denial = () if state == "live" else ({
        "reason_code": "unknown_recipient",
        "claimed_sender": "solo",
        "claimed_recipient": "missing",
    },)
    refusal = () if state == "live" else ({
        "reason_code": "worker_work_absent", "node_id": "solo"
    },)
    worker = {
        "session_id": "worker-wall",
        "work_item_id": "work-wall",
        "node_id": "solo",
        "adapter": "codex",
        "transition": transition,
        "state": worker_state,
        "outcome_code": outcome,
        "last_activity": OBSERVED,
    }
    receipt = {
        "kind": "worker_receipt",
        "timestamp": OBSERVED,
        "node_id": "solo",
        "transition": transition,
        "outcome_code": outcome,
    }
    return HarborBoardModel(
        observed_at=OBSERVED,
        nodes=({
            "node_id": "solo",
            "role": "Codex",
            "liveness": "present" if state == "live" else "silent",
            "authority": "active" if state == "live" else "expired",
            "mutex": "none",
            "inbox_depth": 0,
            "last_activity": OBSERVED,
        },),
        work_items=({
            "id": "work-wall",
            "title": "Record this session",
            "owner": "solo",
            "state": "claimed",
            "readiness": "claimed",
            "holder": "solo",
            "needs": [],
        },),
        deliveries=(),
        acknowledgments=(),
        denials=denial,
        stale_leases=(),
        workers=(worker,),
        consumption={
            "coordinate": "work/items.jsonl",
            "state": "caught_up",
            "wake_state": "unsatisfied_wake" if state == "degraded" else "none",
        },
        worker_refusals=refusal,
        worker_receipts=(receipt,),
    )


def _replay() -> Mapping[str, object]:
    events = [
        {"sequence": 1, "timestamp": "2026-08-01T12:00:00.000Z", "elapsed_ms": 0, "event_class": "claim", "record_kind": "work_transition", "node_id": "solo", "work_item_id": "work-a", "transition": "claim", "outcome_code": None, "reason_code": None},
        {"sequence": 2, "timestamp": "2026-08-01T12:00:01.000Z", "elapsed_ms": 1000, "event_class": "turn", "record_kind": "worker_receipt", "node_id": "solo", "work_item_id": "work-a", "transition": "drive", "outcome_code": None, "reason_code": None},
        {"sequence": 3, "timestamp": "2026-08-01T12:00:03.000Z", "elapsed_ms": 3000, "event_class": "completion", "record_kind": "worker_receipt", "node_id": "solo", "work_item_id": "work-a", "transition": "complete", "outcome_code": None, "reason_code": None},
    ]
    return {
        "replay_schema_version": 0,
        "events": events,
        "counts": {"claim": 1, "turn": 1, "degradation": 0, "denial": 0, "completion": 1},
        "duration_ms": 3000,
        "sources": ["receipts/workers.jsonl", "work/items.jsonl"],
    }



def _graph_artifacts() -> tuple[Mapping[str, object], Mapping[str, object]]:
    topology = {
        "schema_version": 0,
        "topology_version": "0",
        "tenant_id": "demo-fleet",
        "nodes": [
            {"id": "lane-floati", "kind": "node", "role": "Codex", "state": "active"},
            {
                "id": "puddle-floati-architect",
                "kind": "node",
                "role": "architect",
                "state": "active",
            },
        ],
        "workers": [
            {
                "id": "worker-00000000000070008000000000000001",
                "kind": "worker",
                "node_id": "lane-floati",
                "work_item_id": "work-00000000000070008000000000000002",
                "adapter": "codex",
                "state": "driving",
                "outcome_code": None,
            }
        ],
        "edges": [],
        "bridge_stubs": [],
    }
    traffic = {
        "schema_version": 0,
        "tenant_id": "demo-fleet",
        "pairs": [
            {
                "sender": "lane-floati",
                "recipient": "puddle-floati-architect",
                "envelope_count": 4,
                "denial_count": 1,
            },
            {
                "sender": "puddle-floati-architect",
                "recipient": "lane-floati",
                "envelope_count": 3,
                "denial_count": 0,
            },
        ],
    }
    return topology, traffic


def _moment(state: str, mode: str) -> str:
    payload = (
        {"artifact_version": 0, "command": "install", "evidence": {"status": "installed"}, "status": "ok"}
        if state == "install"
        else {"canonical_ref": "refs/heads/lane/hm0", "status": "bundle_verified"}
    )
    artifact = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if mode == "plain":
        return artifact + "\n"
    return render_buoy_mark(color=True) + "\n" + artifact + "\n"
def _text(state: str, mode: str) -> str:
    if state == "replay":
        artifact = _replay()
        rendered = (
            render_replay_frame(artifact, len(artifact["events"]), width=120, height=34)
            if mode == "standard"
            else render_replay_plain(artifact, width=120)
        )
    elif state == "graph":
        topology, traffic = _graph_artifacts()
        rendered = render_harbor_chart(topology, traffic, color=mode == "standard")
    elif state in {"install", "selftest"}:
        rendered = _moment(state, mode)
    else:
        model = _model(state)
        rendered = (
            render_frame(model, 120, 34, selected=0, color=False)
            if mode == "standard"
            else render_plain_dump(model, width=120)
        )
    return ANSI.sub("", rendered).rstrip() + "\n"



_SEMANTIC_TOKEN = re.compile(
    r"(▓+|◆|DRIVING|DEGRADED|EXPIRED|SILENT|⊙|│|╱|───|╲|~+|^>)"
)


def _styled_line(line: str, palette: Mapping[str, str], mode: str) -> str:
    if mode == "plain":
        return html.escape(line)
    if line.startswith(
        ("! DENIAL", "! STALE", "!! EFFECT", "! EFFECT", "+")
    ) or line.startswith(("  DRIVING ", "  DEGRADED ")) or (
        "──▶" in line and " envelope" in line
    ):
        return f'<tspan fill="{palette["accent"]}">{html.escape(line)}</tspan>'

    rendered = []
    for piece in _SEMANTIC_TOKEN.split(line):
        if not piece:
            continue
        escaped = html.escape(piece)
        if piece == "───" or piece.startswith("~"):
            rendered.append(f'<tspan fill="{palette["dim"]}">{escaped}</tspan>')
        elif _SEMANTIC_TOKEN.fullmatch(piece):
            rendered.append(f'<tspan fill="{palette["accent"]}">{escaped}</tspan>')
        else:
            rendered.append(escaped)
    return "".join(rendered)
def _svg(text: str, state: str, mode: str, theme: str) -> str:
    palette = PALETTES[theme]
    lines = text.rstrip("\n").splitlines()
    height = 52 + 18 * len(lines)
    tspans = []
    for index, line in enumerate(lines):
        tspans.append(
            f'    <tspan x="28" dy="{0 if index == 0 else 18}" fill="{palette["foreground"]}">{_styled_line(line, palette, mode)}</tspan>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="{height}" viewBox="0 0 1280 {height}" role="img">\n'
        f'  <title>FLOATI {state} {mode} {theme}</title>\n'
        f'  <desc>Deterministic synthetic TUI wall capture.</desc>\n'
        f'  <rect width="1280" height="{height}" fill="{palette["background"]}"/>\n'
        f'  <text x="28" y="32" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="14" xml:space="preserve">\n'
        + "\n".join(tspans)
        + "\n  </text>\n</svg>\n"
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_wall(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    captures = []
    for state in STATES:
        for mode in MODES:
            testimony = _text(state, mode)
            for theme in THEMES:
                stem = f"{state}-{mode}-{theme}"
                text_path = destination / f"{stem}.txt"
                svg_path = destination / f"{stem}.svg"
                text_path.write_text(testimony, encoding="utf-8")
                svg_path.write_text(_svg(testimony, state, mode, theme), encoding="utf-8")
                captures.append({
                    "state": state,
                    "mode": mode,
                    "theme": theme,
                    "svg": svg_path.name,
                    "text": text_path.name,
                    "sha256_svg": _digest(svg_path),
                    "sha256_text": _digest(text_path),
                })
    manifest = {
        "schema_version": 0,
        "generator": "scripts/capture-wall.py",
        "states": list(STATES),
        "modes": list(MODES),
        "themes": list(THEMES),
        "synthetic": True,
        "captures": captures,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "docs" / "evidence" / "wall",
    )
    args = parser.parse_args()
    capture_wall(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
