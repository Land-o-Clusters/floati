"""Deterministic synthetic fleet for the reviewer live-polish gate."""

from __future__ import annotations

import argparse
import os
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Union

from .cursor import SparseCursor
from .errors import ProtocolRefusal
from .events import EventLog
from . import fixture_ids
from .planes import AuthorityGrantStore, LivenessPresenceStore, MutualExclusionHoldStore
from .registry import Registry
from .root import FloatiRoot
from .tui import model_from_root, run_board
from .tui_approval import (
    ApprovalOption,
    ApprovalPanelController,
    ApprovalPanelRequest,
)
from .tui_chart import LiveHarborMapController
from .tui_chart_render import render_live_harbor_map
from .tui_replay_render import record_replay_cinema_gif as _record_replay_cinema_gif
from .tui_render import HarborBoardModel, render_frame
from .work import WorkLog
from .workers import WorkerReceipts


DEMO_NOW = datetime(2026, 7, 31, 12, 0, 10, tzinfo=timezone.utc)
_REVIEW_DOC = f"docs/evidence/{fixture_ids.reviewer().upper()}-TUI.md"


def _time(hour: int, minute: int, second: int) -> datetime:
    return datetime(2026, 7, 31, hour, minute, second, tzinfo=timezone.utc)


def seed_demo(path: Union[Path, str]) -> FloatiRoot:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ProtocolRefusal("root_not_absolute", "demo root must be absolute")
    if candidate.exists() and any(candidate.iterdir()):
        raise ProtocolRefusal("demo_root_not_empty", "demo root must be empty")
    root = FloatiRoot.open_direct_home(candidate, create=True)
    registry = Registry(root)
    for node, role in (("builder-review", "Claude"), ("builder-app", "Codex"), ("builder-core", "Codex")):
        registry.register(node, role)

    liveness = LivenessPresenceStore(root)
    liveness.observe("builder-review", 60, _time(11, 58, 0))
    liveness.observe("builder-app", 60, _time(11, 59, 40))
    liveness.observe("builder-core", 60, _time(12, 0, 0))

    authority = AuthorityGrantStore(root)
    authority.claim("app-build", "builder-app", 60, 60, _time(11, 58, 0))
    work_grant = authority.claim("work-claims", "builder-core", 60, 60, _time(12, 0, 0))
    mutex = MutualExclusionHoldStore(root)
    mutex.acquire("builder-review-workspace", "builder-review", 60, 60, _time(11, 58, 0))
    mutex.acquire("app-workspace", "builder-app", 60, 60, _time(12, 0, 0))

    work = WorkLog(root)
    work.add("review provisional copy", "builder-review", [], now=_time(12, 0, 0))
    claimed = work.add("polish harbor board", "builder-core", [], now=_time(12, 0, 1))
    work.claim(claimed["id"], "builder-core", "work-claims", work_grant["epoch"], now=_time(12, 0, 2))
    completed = work.add("verify protocol gate", "builder-core", [], now=_time(12, 0, 3))
    work.claim(completed["id"], "builder-core", "work-claims", work_grant["epoch"], now=_time(12, 0, 4))

    worker_receipts = WorkerReceipts(root)
    degraded_session = "worker-018f0f23abcd71238000000000000000"
    for transition in ("claim", "spawn", "drive"):
        worker_receipts.append(
            degraded_session, claimed["id"], "builder-core", "codex",
            transition, None, [], now=_time(12, 0, 6),
        )
    worker_receipts.append(
        degraded_session, claimed["id"], "builder-core", "codex",
        "degrade", "process_died", [], now=_time(12, 0, 6),
    )
    complete_session = "worker-018f0f23abce71238000000000000000"
    for transition in ("claim", "spawn", "drive", "bind_artifact"):
        worker_receipts.append(
            complete_session, completed["id"], "builder-core", "acp",
            transition, None, [], now=_time(12, 0, 7),
        )
    work.complete(completed["id"], "builder-core", [], now=_time(12, 0, 7))
    worker_receipts.append(
        complete_session, completed["id"], "builder-core", "acp",
        "complete", None, [], now=_time(12, 0, 7),
    )
    claim_item = work.add("claim worker item", "builder-core", [], now=_time(12, 0, 6))
    work.claim(claim_item["id"], "builder-core", "work-claims", work_grant["epoch"], now=_time(12, 0, 7))
    worker_receipts.append(
        "worker-018f0f23abcf71238000000000000000",
        claim_item["id"], "builder-core", "codex", "claim", None, [], now=_time(12, 0, 7),
    )
    driving_item = work.add("drive worker item", "builder-core", [], now=_time(12, 0, 7))
    work.claim(driving_item["id"], "builder-core", "work-claims", work_grant["epoch"], now=_time(12, 0, 8))
    driving_session = "worker-018f0f23abd071238000000000000000"
    for transition in ("claim", "spawn", "drive"):
        worker_receipts.append(
            driving_session, driving_item["id"], "builder-core", "codex",
            transition, None, [], now=_time(12, 0, 9),
        )

    events = EventLog(root)
    accepted = events.send(
        "builder-core", "builder-review", "floati", "a" * 40,
        "docs/evidence/HM1-PHASE-C.md", "checkpoint", idempotency_key="demo-accepted",
    )
    events.present("builder-review")
    SparseCursor(root).ack(
        "builder-review", [accepted["id"]], acting_session_id="demo-session"
    )
    events.send(
        "builder-review", "builder-core", "floati", "b" * 40,
        _REVIEW_DOC, "review", idempotency_key="demo-review",
    )
    events.present("builder-core")
    try:
        events.send(
            "builder-review", "builder-core", "floati", "b" * 40,
            _REVIEW_DOC, "refused", idempotency_key="demo-review",
        )
    except ProtocolRefusal as exc:
        if exc.code != "idempotency_conflict":
            raise
    return root


def build_demo_model(root: FloatiRoot) -> HarborBoardModel:
    model = model_from_root(root, DEMO_NOW)
    activity = {
        "builder-review": "2026-07-31T12:00:06.000Z",
        "builder-app": "2026-07-31T11:59:40.000Z",
        "builder-core": "2026-07-31T12:00:09.000Z",
    }
    nodes = tuple({**node, "last_activity": activity[str(node["node_id"])]} for node in model.nodes)
    stable_work_ids = {
        str(item["id"]): f"demo-work-{index}"
        for index, item in enumerate(model.work_items, start=1)
    }
    workers = tuple(
        {
            **worker,
            "work_item_id": stable_work_ids.get(
                str(worker.get("work_item_id")), "demo-work-unknown"
            ),
        }
        for worker in model.workers
    )
    return replace(model, nodes=nodes, workers=workers)


def demo_model_loader(root: FloatiRoot) -> Callable[[], HarborBoardModel]:
    return lambda: build_demo_model(root)


def capture_demo(*, color: bool) -> str:
    with tempfile.TemporaryDirectory(prefix="floati-demo-") as temporary:
        root = seed_demo(Path(temporary) / "synthetic-fleet")
        return render_frame(build_demo_model(root), 100, 30, selected=0, color=color) + "\n"


def harbor_map_demo_artifact(*, include_envelope: bool) -> dict[str, object]:
    artifact: dict[str, object] = {
        "schema_version": 0,
        "source": "synthetic_declared_roots_and_ledgers",
        "buses": [
            {
                "bus_id": "demo-fleet",
                "architect_node": "demo-architect",
                "last_activity_age_seconds": 12,
                "ledger_event_count": 41,
                "nodes": [
                    {
                        "id": "demo-architect",
                        "role": "Architect",
                        "last_activity_age_seconds": 12,
                        "inbox_count": 1,
                        "receipt_count": 20,
                    },
                    {
                        "id": "builder-core",
                        "role": "Codex",
                        "last_activity_age_seconds": 75,
                        "inbox_count": 0,
                        "receipt_count": 13,
                    },
                ],
                "downstream": ["regatta"],
            },
            {
                "bus_id": "regatta",
                "architect_node": "harbor-master",
                "last_activity_age_seconds": 305,
                "ledger_event_count": 11,
                "nodes": [
                    {
                        "id": "harbor-master",
                        "role": "Architect",
                        "last_activity_age_seconds": 305,
                        "inbox_count": 0,
                        "receipt_count": 4,
                    },
                    {
                        "id": "builder-six",
                        "role": "Codex",
                        "last_activity_age_seconds": 31,
                        "inbox_count": 2,
                        "receipt_count": 7,
                    },
                ],
                "downstream": [],
            },
        ],
        "relationships": [{"source": "demo-fleet", "target": "regatta"}],
        "envelopes": [],
    }
    if include_envelope:
        artifact["envelopes"] = [
            {
                "id": "envelope-demo-002",
                "source_bus": "demo-fleet",
                "sender": "builder-core",
                "target_bus": "regatta",
                "recipient": "builder-six",
            }
        ]
    return artifact


def capture_harbor_map(*, color: bool) -> str:
    controller = LiveHarborMapController(
        harbor_map_demo_artifact(include_envelope=False)
    )
    controller.update(
        harbor_map_demo_artifact(include_envelope=True), observed_at=10.0
    )
    for _ in range(5):
        controller.handle_key("KEY_DOWN")
    controller.handle_key("ENTER")
    rendered = render_live_harbor_map(
        controller.artifact,
        selected=controller.selected_target,
        detail_open=controller.detail_open,
        pulses=(),
        width=100,
        height=30,
        color_tier="256" if color else "mono",
    )
    return rendered.text + "\n"


def capture_regatta_r3(*, color: bool) -> str:
    """Return the deterministic R3 activity-twin evidence surface."""

    artifact = harbor_map_demo_artifact(include_envelope=True)
    rendered = render_live_harbor_map(
        artifact,
        selected=LiveHarborMapController(artifact).selected_target,
        detail_open=False,
        pulses=(),
        width=100,
        height=30,
        color_tier="256" if color else "mono",
        activity_by_node={
            "demo-fleet/demo-architect": (0, 1, 2, 3, 4),
            "demo-fleet/builder-core": (4, 3, 2, 1, 0),
            "regatta/harbor-master": (0, 0, 0, 0, 0),
            "regatta/builder-six": (0, 2, 1, 3, 4),
        },
    )
    return rendered.text + "\n"


def capture_approval_panel(*, color: Optional[bool] = None) -> str:
    """Return the deterministic R4 approval-panel evidence surface."""

    enabled = not bool(os.environ.get("NO_COLOR")) if color is None else color
    controller = ApprovalPanelController(
        session_id="demo-human-session", composer_text="floati effect confirm"
    )
    controller.enqueue(
        ApprovalPanelRequest(
            request_id="approval-request-demo",
            record={
                "kind": "effect_intent",
                "id": "effect-intent-demo",
                "effect_type": "git_remote_ref_update",
                "target": "refs/heads/main",
                "request_digest": "a" * 64,
            },
            options=(
                ApprovalOption(
                    "refuse-once", "Refuse once", "request"
                ),
                ApprovalOption("allow-once", "Allow once", "request"),
                ApprovalOption(
                    "allow-session",
                    "Allow for this session",
                    "session",
                ),
            ),
        )
    )
    return controller.render(
        width=100,
        color_tier="256" if enabled else "mono",
    ).text + "\n"


def replay_cinema_demo_artifact() -> dict[str, object]:
    return {
        "replay_schema_version": 0,
        "duration_ms": 1500,
        "sources": [
            "work/items.jsonl",
            "receipts/workers.jsonl",
            "receipts/denials.jsonl",
        ],
        "counts": {
            "claim": 1,
            "turn": 1,
            "degradation": 0,
            "denial": 1,
            "completion": 1,
        },
        "harbor": {
            "buses": [
                {
                    "bus_id": "alpha",
                    "architect_node": "builder-a",
                    "nodes": [{"id": "builder-a", "role": "Codex"}],
                },
                {
                    "bus_id": "beta",
                    "architect_node": "builder-b",
                    "nodes": [{"id": "builder-b", "role": "Codex"}],
                },
            ],
            "relationships": [{"source": "alpha", "target": "beta"}],
        },
        "events": [
            {
                "sequence": 1,
                "elapsed_ms": 0,
                "event_class": "claim",
                "record_kind": "work_transition",
                "node_id": "builder-a",
                "work_item_id": "work-1",
                "transition": "claim",
                "source_bus": "alpha",
                "sender": "builder-a",
                "target_bus": "alpha",
                "recipient": "builder-a",
            },
            {
                "sequence": 2,
                "elapsed_ms": 500,
                "event_class": "turn",
                "record_kind": "worker_receipt",
                "node_id": "builder-b",
                "work_item_id": "work-1",
                "transition": "drive",
                "source_bus": "alpha",
                "sender": "builder-a",
                "target_bus": "beta",
                "recipient": "builder-b",
            },
            {
                "sequence": 3,
                "elapsed_ms": 1000,
                "event_class": "denial",
                "record_kind": "denial_receipt",
                "node_id": "builder-b",
                "work_item_id": "work-1",
                "reason_code": "E_DENIED",
                "source_bus": "beta",
                "sender": "builder-b",
                "target_bus": "alpha",
                "recipient": "builder-a",
            },
            {
                "sequence": 4,
                "elapsed_ms": 1500,
                "event_class": "completion",
                "record_kind": "work_transition",
                "node_id": "builder-a",
                "work_item_id": "work-1",
                "transition": "complete",
                "source_bus": "alpha",
                "sender": "builder-a",
                "target_bus": "alpha",
                "recipient": "builder-a",
            },
        ],
    }


def capture_replay_cinema(*, color: bool) -> str:
    from .replay_render import render_replay_cinema_frame

    return render_replay_cinema_frame(
        replay_cinema_demo_artifact(),
        3,
        width=100,
        height=30,
        speed=4.0,
        color_tier="256" if color else "mono",
        activity=False,
    ) + "\n"


def record_replay_cinema_gif(
    artifact: Optional[Mapping[str, object]] = None,
) -> bytes:
    return _record_replay_cinema_gif(
        replay_cinema_demo_artifact() if artifact is None else artifact
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=True, allow_abbrev=False)
    parser.add_argument(
        "--capture",
        choices=(
            "color",
            "monochrome",
            "harbor-map-color",
            "harbor-map-monochrome",
            "approval-panel-color",
            "approval-panel-monochrome",
            "replay-cinema-color",
            "replay-cinema-monochrome",
        ),
    )
    args = parser.parse_args(argv)
    if args.capture:
        if args.capture.startswith("approval-panel-"):
            print(capture_approval_panel(color=args.capture.endswith("color")), end="")
        elif args.capture.startswith("replay-cinema-"):
            print(capture_replay_cinema(color=args.capture.endswith("color")), end="")
        elif args.capture.startswith("harbor-map-"):
            print(capture_harbor_map(color=args.capture.endswith("color")), end="")
        else:
            print(capture_demo(color=args.capture == "color"), end="")
        return 0
    with tempfile.TemporaryDirectory(prefix="floati-demo-") as temporary:
        root = seed_demo(Path(temporary) / "synthetic-fleet")
        return run_board(model_loader=demo_model_loader(root))


if __name__ == "__main__":
    raise SystemExit(main())
