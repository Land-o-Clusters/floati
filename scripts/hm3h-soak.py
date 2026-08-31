#!/usr/bin/env python3
"""Build exact HM-3H scale fixtures and fail closed on published budgets."""

from __future__ import annotations

import gc
import json
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from floati.doctor import Doctor
from floati.events import EventLog
from floati import fixture_ids
from floati.framing import encode_frame
from floati.projection import FleetProjection
from floati.replay import ReplayTimeline
from floati.replay_render import render_replay_plain
from floati.root import FloatiRoot
from floati.tui import model_from_root
from floati.tui_render import render_plain_dump


WORK_ITEMS = 10_000
EVENTS = 100_000
REPEATS = 3
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
_PRIMARY_WORKER = fixture_ids.worker("alpha")
BUDGETS_MS = {
    "status": 150.0,
    "inbox": 100.0,
    "replay_render_start": 300.0,
    "board_full_redraw": 250.0,
    "doctor": 2_000.0,
}


def _uuid7(index: int) -> str:
    digits = list(f"{index:032x}")
    digits[12] = "7"
    digits[16] = "8"
    return "".join(digits)


def _common(tenant: str, prefix: str, kind: str, index: int) -> dict[str, object]:
    return {
        "schema_version": 0,
        "id": f"{prefix}{_uuid7(index)}",
        "tenant_id": tenant,
        "timestamp": "2026-08-01T12:00:00.000Z",
        "kind": kind,
    }


def _write_frames(path: Path, records: Iterable[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        for record in records:
            stream.write(encode_frame(record))


def _registry_records(tenant: str) -> Iterable[Mapping[str, object]]:
    for index, node in enumerate((_PRIMARY_WORKER, "bob"), start=1):
        yield {
            **_common(tenant, "registry-", "registry_entry", index),
            "node_id": node,
            "role": "worker",
            "state": "active",
        }


def _work_records(tenant: str) -> Iterable[Mapping[str, object]]:
    for index in range(1, WORK_ITEMS + 1):
        yield {
            **_common(tenant, "work-", "work_item", index),
            "title": f"soak item {index:05d}",
            "owner": _PRIMARY_WORKER,
            "artifact_bindings": [],
        }


def _message_records(tenant: str) -> Iterable[Mapping[str, object]]:
    for index in range(1, EVENTS + 1):
        yield {
            **_common(tenant, "msg-", "message_envelope", index),
            "sender": _PRIMARY_WORKER,
            "recipient": "bob",
            "repo": "slipway",
            "sha": "a" * 40,
            "doc": "docs/evidence/HM3H-GAUNTLET.md",
            "note": f"soak event {index:06d}",
            "idempotency_key": f"soak-{index:06d}",
        }


def _denial_records(tenant: str) -> Iterable[Mapping[str, object]]:
    for index in range(1, EVENTS + 1):
        yield {
            **_common(tenant, "denial-", "denial_receipt", index),
            "attempt_id": f"attempt-{_uuid7(index)}",
            "claimed_sender": _PRIMARY_WORKER,
            "claimed_recipient": "bob",
            "reason_code": "unknown_sender",
        }


def _build_mail_profile(path: Path) -> FloatiRoot:
    root = FloatiRoot.open_direct_home(path, create=True)
    _write_frames(root.resolve_relative("registry/entries.jsonl"), _registry_records(root.tenant_id))
    _write_frames(root.resolve_relative("work/items.jsonl"), _work_records(root.tenant_id))
    _write_frames(root.resolve_relative("events.jsonl"), _message_records(root.tenant_id))
    return root


def _build_replay_profile(path: Path) -> FloatiRoot:
    root = FloatiRoot.open_direct_home(path, create=True)
    _write_frames(root.resolve_relative("registry/entries.jsonl"), _registry_records(root.tenant_id))
    _write_frames(root.resolve_relative("work/items.jsonl"), _work_records(root.tenant_id))
    _write_frames(root.resolve_relative("receipts/denials.jsonl"), _denial_records(root.tenant_id))
    return root


def _measure(action: Callable[[], object], budget_ms: float) -> dict[str, object]:
    action()
    samples = []
    for _ in range(REPEATS):
        gc.collect()
        started = time.perf_counter()
        action()
        samples.append((time.perf_counter() - started) * 1_000)
    median = statistics.median(samples)
    return {
        "budget_ms": budget_ms,
        "median_ms": round(median, 3),
        "samples_ms": [round(sample, 3) for sample in samples],
        "passed": median < budget_ms,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="slipway-hm3h-soak-") as directory:
        base = Path(directory)
        mail = _build_mail_profile(base / "mail-soak")
        replay = _build_replay_profile(base / "replay-soak")

        metrics = {
            "status": _measure(
                lambda: FleetProjection(mail).status_artifact(NOW),
                BUDGETS_MS["status"],
            ),
            "inbox": _measure(
                lambda: EventLog(mail).present("bob"),
                BUDGETS_MS["inbox"],
            ),
            "replay_render_start": _measure(
                lambda: render_replay_plain(ReplayTimeline.from_root(replay).artifact()),
                BUDGETS_MS["replay_render_start"],
            ),
            "board_full_redraw": _measure(
                lambda: render_plain_dump(model_from_root(mail)),
                BUDGETS_MS["board_full_redraw"],
            ),
            "doctor": _measure(
                lambda: Doctor(REPO_ROOT, mail.path, ref="HEAD").artifact(),
                BUDGETS_MS["doctor"],
            ),
        }
        passed = all(bool(metric["passed"]) for metric in metrics.values())
        artifact = {
            "artifact_version": 0,
            "command": "hm3h-soak",
            "status": "passed" if passed else "budget_failed",
            "scale": {
                "mail_profile": {"work_items": WORK_ITEMS, "message_events": EVENTS},
                "replay_profile": {"work_items": WORK_ITEMS, "replay_events": EVENTS},
                "repeats": REPEATS,
                "statistic": "median_after_one_warmup",
            },
            "metrics": metrics,
        }
        print(json.dumps(artifact, sort_keys=True, separators=(",", ":")))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
