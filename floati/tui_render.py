"""Pure viewport model and renderer for the Floati harbor board."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Sequence

from .copy import register
from .errors import SnapshotRefusal
from .tui_activity import activity_braille


BUOY_ORANGE = "\x1b[38;5;208m"
BUOY_ORANGE_16 = "\x1b[93m"
RESET = "\x1b[0m"

HEADER = register("tui.header", "FLOATI // HARBOR BOARD", "TUI header")
PLAIN_PREFIX = register("tui.plain_prefix", "PLAIN DUMP", "TUI degraded mode")
NODE_COLUMN = register("tui.node_column", "NODE", "TUI node table")
NODE_COLUMN_TAIL = register("tui.node_column_tail", "LIVE       AUTH       MUTEX      INBOX  LAST", "TUI node table")
OLDER_DENIALS_SUFFIX = register("tui.denial_older", "older denials · floati log to list", "TUI refusal alert")
WORK_HEADER = register("tui.work_header", "WORK DAG", "TUI work panel")
RECEIPTS_HEADER = register("tui.receipts_header", "RECEIPTS", "TUI receipts ticker")
DENIAL_LABEL = register("tui.denial", "! DENIAL", "TUI refusal alert")
HINT_BAR = register("tui.hints", "↑/k ↓/j select  enter detail  a ack  q quit", "TUI hint bar")
OBSERVED_LABEL = register("tui.observed", "OBSERVED", "TUI observation label")
STALE_LABEL = register("tui.stale", "! STALE", "TUI stale lease alert")
NO_NODES_LABEL = register("tui.no_nodes", "NO NODES", "TUI empty node state")
NONE_LABEL = register("tui.none", "NONE", "TUI empty value")
DELIVERY_LABEL = register("tui.delivery", "DELIVERY", "TUI delivery receipt label")
ACK_LABEL = register("tui.ack", "ACK", "TUI acknowledgment receipt label")
ITEM_LABEL = register("tui.item", "ITEM", "TUI singular receipt item")
ITEMS_LABEL = register("tui.items", "ITEMS", "TUI plural receipt items")
HOLDER_LABEL = register("tui.holder", "holder:", "TUI work holder label")
DETAIL_LABEL = register("tui.detail", "DETAIL", "TUI selected node detail")
ROLE_LABEL = register("tui.role", "ROLE", "TUI node role label")
VISIBLE_MAIL_LABEL = register("tui.visible_mail", "VISIBLE MAIL", "TUI visible mail label")
UNKNOWN_LABEL = register("tui.unknown", "UNKNOWN", "TUI unknown value")
LIVE_MAP_HEADER = register(
    "tui.live_map.header",
    "FLOATI // LIVE HARBOR MAP",
    "Live Harbor Map header",
)
LIVE_MAP_ESTATE = register(
    "tui.live_map.estate", "ESTATE", "Live Harbor Map summary"
)
LIVE_MAP_PIER = register(
    "tui.live_map.pier", "PIER", "Live Harbor Map pier label"
)
LIVE_MAP_VESSEL = register(
    "tui.live_map.vessel", "VESSEL", "Live Harbor Map vessel label"
)
LIVE_MAP_CHANNELS = register(
    "tui.live_map.channels", "CHANNELS", "Live Harbor Map channel panel"
)
LIVE_MAP_ENVELOPE = register(
    "tui.live_map.envelope", "ENVELOPE", "Live Harbor Map envelope pulse"
)
LIVE_MAP_LEDGER = register(
    "tui.live_map.ledger", "LEDGER EVENTS", "Live Harbor Map ledger count"
)
LIVE_MAP_LAST = register(
    "tui.live_map.last", "LAST", "Live Harbor Map activity age"
)
LIVE_MAP_HINTS = register(
    "tui.live_map.hints",
    "↑/k ↓/j select  enter detail  q quit",
    "Live Harbor Map hint bar",
)
WORKERS_HEADER = register("tui.workers_header", "WORKERS", "TUI worker panel")
CONSUMPTION_HEADER = register("tui.consumption_header", "CONSUMPTION", "TUI consumption panel")
COORDINATE_LABEL = register("tui.coordinate", "coordinate:", "TUI consumption coordinate")
UNSATISFIED_WAKE_LABEL = register("tui.unsatisfied_wake", "UNSATISFIED WAKE", "TUI consumption warning")
WORKER_CLAIM = register("tui.worker_claim", "CLAIM", "TUI worker claim state")
WORKER_DRIVING = register("tui.worker_driving", "DRIVING", "TUI worker driving state")
WORKER_DEGRADED = register("tui.worker_degraded", "DEGRADED", "TUI worker degraded state")
WORKER_COMPLETE = register("tui.worker_complete", "COMPLETE", "TUI worker complete state")
WORKER_PROCESS_DIED = register("tui.worker_process_died", "PROCESS DIED", "TUI worker process death")
WORKER_PROCESS_TIMEOUT = register("tui.worker_process_timeout", "PROCESS TIMEOUT", "TUI worker timeout")
WORKER_PROCESS_START_FAILED = register("tui.worker_process_start_failed", "PROCESS START FAILED", "TUI worker process start failure")
WORKER_ADAPTER_ERROR = register("tui.worker_adapter_error", "ADAPTER ERROR", "TUI worker adapter error")
WORKER_ADAPTER_MALFORMED = register("tui.worker_adapter_malformed", "MALFORMED OUTPUT", "TUI worker malformed output")
WORKER_BOUNDARY_UNRULED = register("tui.worker_boundary_unruled", "BOUNDARY UNRULED", "TUI worker boundary refusal")
WORK_READY = register("tui.work_ready", "READY", "TUI work readiness")
WORK_BLOCKED = register("tui.work_blocked", "BLOCKED", "TUI work readiness")
WORK_CLAIMED = register("tui.work_claimed", "CLAIMED", "TUI work readiness")
WORK_DONE = register("tui.work_done", "DONE", "TUI work readiness")
NEEDS_LABEL = register("tui.needs", "needs:", "TUI dependency edge label")
WORKER_RECEIPT_LABEL = register("tui.worker_receipt", "WORKER", "TUI worker receipt label")
WORKER_PROCESS_CANCELLED = register("tui.worker_process_cancelled", "PROCESS CANCELLED", "TUI worker cancellation")
WORKER_AUTHORITY_CHANGED = register("tui.worker_authority_changed", "AUTHORITY CHANGED", "TUI worker authority expiry")
WORKER_AUTHORITY_EXPIRED = register("tui.worker_authority_expired", "AUTHORITY EXPIRED", "TUI worker authority expiry")
EFFECTS_HEADER = register("tui.effects_header", "EFFECTS", "TUI effect status")
EFFECT_CONFIRMED_LABEL = register("tui.effect_confirmed", "confirmed:", "TUI effect status")
EFFECT_COMPENSATION_LABEL = register("tui.effect_compensation", "compensation:", "TUI effect status")
EFFECT_PROPOSED_LABEL = register("tui.effect_proposed", "proposed:", "TUI effect status")
EFFECT_EXECUTED_LABEL = register("tui.effect_executed", "executed:", "TUI effect status")
EFFECT_UNKNOWN_ALERT = register("tui.effect_unknown", "!! EFFECT UNKNOWN", "TUI effect alert")
EFFECT_INCOMPLETE_ALERT = register("tui.effect_incomplete", "!! EFFECT INCOMPLETE", "TUI effect alert")
EFFECT_FAILED_ALERT = register("tui.effect_failed", "! EFFECT FAILED", "TUI effect alert")
TIDE_LABEL = register("tui.tide", "TIDE", "TUI tide turnover state")
APPROVAL_HEADER = register(
    "tui.approval.header",
    "APPROVAL REQUIRED",
    "Interactive approval panel header",
)
APPROVAL_RECORD = register(
    "tui.approval.record",
    "EXACT RECORD UNDER DECISION",
    "Interactive approval panel record label",
)
APPROVAL_HINTS = register(
    "tui.approval.hints",
    "Esc refuse · digits/arrows move · Enter commit",
    "Interactive approval panel hint bar",
)


@dataclass(frozen=True)
class HarborBoardModel:
    observed_at: str
    nodes: Sequence[Mapping[str, object]]
    work_items: Sequence[Mapping[str, object]]
    deliveries: Sequence[Mapping[str, object]]
    acknowledgments: Sequence[Mapping[str, object]]
    denials: Sequence[Mapping[str, object]]
    stale_leases: Sequence[Mapping[str, object]]
    workers: Sequence[Mapping[str, object]] = ()
    consumption: Mapping[str, object] = field(default_factory=dict)
    worker_refusals: Sequence[Mapping[str, object]] = ()
    worker_receipts: Sequence[Mapping[str, object]] = ()
    effects: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_projection(
        cls,
        snapshot: Mapping[str, object],
        work_items: Sequence[Mapping[str, object]],
        receipts: Mapping[str, Sequence[Mapping[str, object]]],
    ) -> "HarborBoardModel":
        return cls(
            observed_at=str(snapshot.get("observed_at", "unknown")),
            nodes=tuple(dict(node) for node in snapshot.get("nodes", [])),
            work_items=tuple(dict(item) for item in work_items),
            deliveries=tuple(dict(item) for item in receipts.get("deliveries", [])),
            acknowledgments=tuple(dict(item) for item in receipts.get("acks", [])),
            denials=tuple(dict(item) for item in receipts.get("denials", [])),
            stale_leases=tuple(dict(item) for item in snapshot.get("stale_leases", [])),
            workers=tuple(dict(item) for item in snapshot.get("workers", [])),
            consumption=dict(snapshot.get("consumption", {})),
            worker_refusals=tuple(
                dict(item) for item in snapshot.get("worker_refusals", [])
            ),
            worker_receipts=tuple(dict(item) for item in receipts.get("workers", [])),
            effects=dict(snapshot.get("effects", {})),
        )

    def to_snapshot(self) -> Dict[str, object]:
        return {
            "observed_at": self.observed_at,
            "nodes": [dict(item) for item in self.nodes],
            "work_items": [dict(item) for item in self.work_items],
            "deliveries": [dict(item) for item in self.deliveries],
            "acknowledgments": [dict(item) for item in self.acknowledgments],
            "denials": [dict(item) for item in self.denials],
            "stale_leases": [dict(item) for item in self.stale_leases],
            "workers": [dict(item) for item in self.workers],
            "consumption": dict(self.consumption),
            "worker_refusals": [dict(item) for item in self.worker_refusals],
            "worker_receipts": [dict(item) for item in self.worker_receipts],
            "effects": dict(self.effects),
        }

    @classmethod
    def from_snapshot(cls, payload: Mapping[str, object]) -> "HarborBoardModel":
        sequence_fields = {
            "nodes",
            "work_items",
            "deliveries",
            "acknowledgments",
            "denials",
            "stale_leases",
            "workers",
            "worker_refusals",
            "worker_receipts",
        }
        if set(payload) != sequence_fields | {"observed_at", "consumption", "effects"}:
            raise SnapshotRefusal(
                "snapshot_payload_invalid", "board model fields are invalid"
            )
        if not isinstance(payload["observed_at"], str) or not isinstance(
            payload["consumption"], dict
        ) or not isinstance(
            payload["effects"], dict
        ):
            raise SnapshotRefusal(
                "snapshot_payload_invalid", "board model scalar fields are invalid"
            )
        if payload["effects"] and "attention" not in payload["effects"]:
            raise SnapshotRefusal(
                "snapshot_payload_invalid",
                "board model scalar fields are invalid",
            )
        for field_name in sequence_fields:
            value = payload[field_name]
            if not isinstance(value, list) or not all(
                isinstance(item, dict) for item in value
            ):
                raise SnapshotRefusal(
                    "snapshot_payload_invalid",
                    f"board model {field_name} rows are invalid",
                )
        return cls(
            observed_at=str(payload["observed_at"]),
            nodes=tuple(dict(item) for item in payload["nodes"]),
            work_items=tuple(dict(item) for item in payload["work_items"]),
            deliveries=tuple(dict(item) for item in payload["deliveries"]),
            acknowledgments=tuple(
                dict(item) for item in payload["acknowledgments"]
            ),
            denials=tuple(dict(item) for item in payload["denials"]),
            stale_leases=tuple(dict(item) for item in payload["stale_leases"]),
            workers=tuple(dict(item) for item in payload["workers"]),
            consumption=dict(payload["consumption"]),
            worker_refusals=tuple(
                dict(item) for item in payload["worker_refusals"]
            ),
            worker_receipts=tuple(
                dict(item) for item in payload["worker_receipts"]
            ),
            effects=dict(payload["effects"]),
        )


def honest_percent(value: float) -> int:
    return round(max(0.0, min(1.0, float(value))) * 100)


def settle_geometry(target: float, progress: float) -> float:
    """Two-segment settle phrase: geometry may overshoot; testimony never does."""

    destination = max(0.0, min(1.0, float(target)))
    phase = max(0.0, min(1.0, float(progress)))
    peak = min(1.0, destination * 1.06)
    if phase <= 0.70:
        normalized = phase / 0.70
        eased = 1 - (1 - normalized) ** 3
        return peak * eased
    normalized = (phase - 0.70) / 0.30
    eased = normalized * normalized * (3 - 2 * normalized)
    return peak + (destination - peak) * eased


def _clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[: width - 1] + "…"


def _short_work_id(value: object) -> str:
    identifier = str(value)
    if len(identifier) <= 18:
        return identifier
    prefix = identifier.split("-", 1)[0] + "-" if "-" in identifier else ""
    return prefix + "…" + identifier[-6:]


def _node_width(model: HarborBoardModel) -> int:
    return max([18, *(len(str(node.get("node_id", UNKNOWN_LABEL))) for node in model.nodes)])


def _node_columns(node_width: int, width: int) -> str:
    return _clip(f"{NODE_COLUMN:<{node_width}} {NODE_COLUMN_TAIL}", width)


def _node_line(
    node: Mapping[str, object],
    selected: bool,
    width: int,
    node_width: int,
    activity_by_node: Mapping[str, Sequence[int]] | None = None,
) -> str:
    marker = ">" if selected else " "
    node_id = str(node.get("node_id", UNKNOWN_LABEL))
    last = str(node.get("last_activity", UNKNOWN_LABEL))
    if "T" in last:
        last = last.split("T", 1)[1].removesuffix("Z")
    tide = node.get("tide")
    tide_flag = ""
    if isinstance(tide, Mapping) and tide.get("turnover_state") in {
        "directed", "state_flushed"
    }:
        tide_flag = f"  {TIDE_LABEL} {str(tide['turnover_state']).upper()}"
    activity = ""
    if activity_by_node is not None and node_id in activity_by_node:
        activity = "  " + activity_braille(activity_by_node[node_id])
    line = (
        f"{marker} {node_id:<{node_width}} "
        f"{str(node.get('liveness', UNKNOWN_LABEL)).upper():<10} "
        f"{str(node.get('authority', 'none')).upper():<10} "
        f"{str(node.get('mutex', 'none')).upper():<10} "
        f"{int(node.get('inbox_depth', 0)):>5}  {last}{tide_flag}{activity}"
    )
    return _clip(line, width)


def _work_lines(model: HarborBoardModel, width: int, animation_progress: float) -> List[str]:
    total = len(model.work_items)
    completed = sum(
        1
        for item in model.work_items
        if item.get("readiness") == "done" or item.get("state") == "completed"
    )
    target = completed / total if total else 0.0
    geometry = settle_geometry(target, animation_progress)
    cells = 10
    filled = min(cells, round(geometry * cells))
    gauge = "▓" * filled + "░" * (cells - filled)
    lines = [_clip(f"{WORK_HEADER}  [{gauge}] {honest_percent(target):>3}%", width)]
    states = {
        "ready": WORK_READY,
        "blocked": WORK_BLOCKED,
        "claimed": WORK_CLAIMED,
        "done": WORK_DONE,
        "open": WORK_READY,
        "completed": WORK_DONE,
    }
    for item in model.work_items:
        holder = item.get("holder") or "-"
        readiness = str(item.get("readiness", item.get("state", "unknown")))
        label = states.get(readiness, UNKNOWN_LABEL)
        needs = item.get("needs", [])
        edges = "" if not needs else f"  {NEEDS_LABEL}{','.join(str(value) for value in needs)}"
        lines.append(
            _clip(
                f"  {label:<9} {item.get('title', '')}  {HOLDER_LABEL}{holder}{edges}",
                width,
            )
        )
    return lines


def _receipt_lines(model: HarborBoardModel, width: int) -> List[str]:
    combined = [
        *reversed(model.deliveries),
        *reversed(model.acknowledgments),
        *reversed(model.worker_receipts),
    ]
    lines = [RECEIPTS_HEADER]
    surfaced_outcomes = {
        (str(worker.get("node_id")), str(worker.get("outcome_code")))
        for worker in model.workers
        if worker.get("state") == "degraded" and worker.get("outcome_code") is not None
    }
    for receipt in combined[:4]:
        if receipt.get("kind") == "worker_receipt":
            outcome = receipt.get("outcome_code")
            repeated = (str(receipt.get("node_id")), str(outcome)) in surfaced_outcomes
            suffix = "" if outcome is None or repeated else " " + str(outcome).replace("_", " ").upper()
            lines.append(
                _clip(
                    f"  {WORKER_RECEIPT_LABEL:<8} {receipt.get('node_id', UNKNOWN_LABEL)} "
                    f"{str(receipt.get('transition', UNKNOWN_LABEL)).upper()}{suffix}",
                    width,
                )
            )
        else:
            label = DELIVERY_LABEL if receipt.get("kind") == "delivery_receipt" else ACK_LABEL
            count = len(receipt.get("item_ids", []))
            noun = ITEM_LABEL if count == 1 else ITEMS_LABEL
            lines.append(_clip(f"  {label:<8} {count} {noun}", width))
    if len(lines) == 1:
        lines.append("  " + NONE_LABEL)
    return lines


def _worker_lines(model: HarborBoardModel, width: int) -> List[str]:
    labels = {
        "claim": WORKER_CLAIM,
        "driving": WORKER_DRIVING,
        "degraded": WORKER_DEGRADED,
        "complete": WORKER_COMPLETE,
    }
    outcomes = {
        "process_died": WORKER_PROCESS_DIED,
        "process_timeout": WORKER_PROCESS_TIMEOUT,
        "process_start_failed": WORKER_PROCESS_START_FAILED,
        "adapter_error": WORKER_ADAPTER_ERROR,
        "adapter_malformed_output": WORKER_ADAPTER_MALFORMED,
        "credential_network_boundary_unruled": WORKER_BOUNDARY_UNRULED,
        "process_cancelled": WORKER_PROCESS_CANCELLED,
        "worker_authority_changed": WORKER_AUTHORITY_CHANGED,
        "authority_expired_mid_claim": WORKER_AUTHORITY_EXPIRED,
    }
    lines = [WORKERS_HEADER]
    titles = {
        str(item.get("id")): str(item.get("title", ""))
        for item in model.work_items
        if item.get("id") is not None
    }
    for worker in model.workers:
        state = str(worker.get("state", "unknown"))
        label = labels.get(state, UNKNOWN_LABEL)
        outcome = worker.get("outcome_code")
        suffix = "" if outcome is None else " " + outcomes.get(
            str(outcome), str(outcome).replace("_", " ").upper()
        )
        work_id = worker.get("work_item_id", UNKNOWN_LABEL)
        title = titles.get(str(work_id), "")
        work = _short_work_id(work_id)
        pairing = f"{title} [{work}]" if title else work
        lines.append(_clip(
            f"  {label:<9} {worker.get('node_id', UNKNOWN_LABEL)} "
            f"{worker.get('adapter', UNKNOWN_LABEL)} {pairing}{suffix}",
            width,
        ))
    if len(lines) == 1:
        lines.append("  " + NONE_LABEL)
    return lines


def _consumption_lines(model: HarborBoardModel, width: int) -> List[str]:
    state = str(model.consumption.get("state", UNKNOWN_LABEL)).replace("_", " ").upper()
    coordinate = str(model.consumption.get("coordinate", UNKNOWN_LABEL))
    lines = [_clip(f"{CONSUMPTION_HEADER}  {state}", width)]
    lines.append(_clip(f"  {COORDINATE_LABEL} {coordinate}", width))
    return lines


def _effect_lines(model: HarborBoardModel, width: int) -> List[str]:
    attention = model.effects.get("attention", ())
    compensation = model.effects.get("compensation_counts", {})
    if (
        not isinstance(attention, (list, tuple))
        or not isinstance(compensation, Mapping)
    ):
        return []
    proposed = int(compensation.get("proposed", 0))
    executed = int(compensation.get("executed", 0))
    lines = []
    confirmed = 0
    labels = {
        "unknown": EFFECT_UNKNOWN_ALERT,
        "incomplete": EFFECT_INCOMPLETE_ALERT,
        "failed": EFFECT_FAILED_ALERT,
    }
    for row in attention:
        if not isinstance(row, Mapping):
            return []
        state = row.get("state")
        count = row.get("count")
        if (
            state not in {"unknown", "incomplete", "failed", "confirmed"}
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            return []
        if state == "confirmed":
            confirmed = count
        elif count:
            lines.append(_clip(f"{labels[state]} {count}", width))
    if not any((lines, confirmed, proposed, executed)):
        return []
    lines.append(_clip(
        f"{EFFECTS_HEADER} {EFFECT_CONFIRMED_LABEL}{confirmed} "
        f"{EFFECT_COMPENSATION_LABEL}{EFFECT_PROPOSED_LABEL}{proposed} "
        f"{EFFECT_EXECUTED_LABEL}{executed}",
        width,
    ))
    return lines


def _detail_lines(model: HarborBoardModel, width: int, selected: int) -> List[str]:
    if not model.nodes:
        return []
    index = max(0, min(selected, len(model.nodes) - 1))
    node = model.nodes[index]
    visible = node.get("visible_message_id") or NONE_LABEL
    lines = [
        _clip(f"{DETAIL_LABEL} {node.get('node_id', UNKNOWN_LABEL)}", width),
        _clip(f"  {ROLE_LABEL} {node.get('role', UNKNOWN_LABEL)}", width),
        _clip(f"  {VISIBLE_MAIL_LABEL} {visible}", width),
    ]
    tide = node.get("tide")
    if isinstance(tide, Mapping) and tide.get("turnover_state") in {
        "directed", "state_flushed"
    }:
        lines.append(
            _clip(f"  {TIDE_LABEL} {str(tide['turnover_state']).upper()}", width)
        )
    return lines


def _denial_lines(model: HarborBoardModel, width: int) -> List[str]:
    counts: Dict[tuple[str, str, str], int] = {}
    newest: Dict[tuple[str, str, str], Mapping[str, object]] = {}
    order: List[tuple[str, str, str]] = []
    for denial in reversed(model.denials):
        key = (
            str(denial.get("reason_code", "unknown")),
            str(denial.get("claimed_sender", "?")),
            str(denial.get("claimed_recipient", "?")),
        )
        counts[key] = counts.get(key, 0) + 1
        if key in newest:
            continue
        newest[key] = denial
        order.append(key)

    lines = []
    for key in order[:2]:
        denial = newest[key]
        count = counts[key]
        suffix = "" if count == 1 else f" ×{count}"
        lines.append(_clip(
            f"{DENIAL_LABEL} {str(denial.get('reason_code', 'unknown')).upper()} "
            f"{denial.get('claimed_sender', '?')} → {denial.get('claimed_recipient', '?')}{suffix}",
            width,
        ))
    hidden = sum(counts[key] for key in order[2:])
    if hidden:
        lines.append(_clip(f"+{hidden} {OLDER_DENIALS_SUFFIX}", width))
    return lines


def _raw_lines(
    model: HarborBoardModel,
    width: int,
    selected: int,
    animation_progress: float,
    detail_open: bool,
    activity_by_node: Mapping[str, Sequence[int]] | None = None,
) -> List[str]:
    lines = [
        _clip(HEADER, width),
        _clip(f"{OBSERVED_LABEL} {model.observed_at}", width),
    ]
    lines.extend(_effect_lines(model, width))
    lines.extend(_denial_lines(model, width))
    if model.consumption.get("wake_state") == "unsatisfied_wake":
        lines.append(_clip(UNSATISFIED_WAKE_LABEL, width))
    for lease in model.stale_leases[:3]:
        lines.append(_clip(
            f"{STALE_LABEL} {str(lease.get('plane', 'plane')).upper()} "
            f"{lease.get('subject_id', '?')} {HOLDER_LABEL}{lease.get('holder', '?')}",
            width,
        ))
    node_width = _node_width(model)
    lines.extend(("", _node_columns(node_width, width)))
    if model.nodes:
        lines.extend(
            _node_line(
                node,
                index == selected,
                width,
                node_width,
                activity_by_node,
            )
            for index, node in enumerate(model.nodes)
        )
    else:
        lines.append("  " + NO_NODES_LABEL)
    if detail_open:
        lines.extend(("", *_detail_lines(model, width, selected)))
    quiet = (
        not model.work_items
        and not model.workers
        and not model.deliveries
        and not model.acknowledgments
        and not model.worker_receipts
        and not model.denials
        and not model.worker_refusals
        and not _effect_lines(model, width)
        and model.consumption.get("state") == "caught_up"
    )
    if quiet:
        work = _work_lines(model, width, animation_progress)[0]
        calm = (
            f"{work}  {CONSUMPTION_HEADER} CAUGHT UP  "
            f"{WORKERS_HEADER} {NONE_LABEL}  {RECEIPTS_HEADER} {NONE_LABEL}"
        )
        lines.extend(("", _clip(calm, width)))
        return lines
    lines.append("")
    lines.extend(_work_lines(model, width, animation_progress))
    lines.append("")
    lines.extend(_consumption_lines(model, width))
    lines.append("")
    lines.extend(_worker_lines(model, width))
    lines.append("")
    lines.extend(_receipt_lines(model, width))
    return lines


def _accent_line(line: str, accent: str) -> str:
    if line.startswith((
        DENIAL_LABEL,
        STALE_LABEL,
        EFFECT_UNKNOWN_ALERT,
        EFFECT_INCOMPLETE_ALERT,
        EFFECT_FAILED_ALERT,
        "+",
    )):
        return accent + line + RESET
    if line.startswith((f"  {WORKER_DRIVING} ", f"  {WORKER_DEGRADED} ")):
        return accent + line + RESET
    if line.startswith("> "):
        line = accent + ">" + RESET + line[1:]
    for token in ("EXPIRED", "SILENT"):
        line = line.replace(token, accent + token + RESET)
    if "▓" in line:
        line = line.replace("▓", accent + "▓" + RESET)
    return line


def render_frame(
    model: HarborBoardModel,
    width: int,
    height: int,
    *,
    selected: int,
    color: bool,
    color_tier: str = "256",
    animation_progress: float = 1.0,
    detail_open: bool = False,
    activity_by_node: Mapping[str, Sequence[int]] | None = None,
) -> str:
    viewport_width = max(20, int(width))
    viewport_height = max(4, int(height))
    content = _raw_lines(
        model,
        viewport_width,
        selected,
        animation_progress,
        detail_open,
        activity_by_node,
    )
    if content:
        content[0] = _clip("⊙ " + content[0], viewport_width)
    content = content[: max(0, viewport_height - 1)]
    content.append(_clip(HINT_BAR, viewport_width))
    if color:
        accent = BUOY_ORANGE_16 if color_tier == "16" else BUOY_ORANGE
        content = [_accent_line(line, accent) for line in content]
    return "\n".join(content)


def node_row_positions(
    model: HarborBoardModel, width: int, height: int | None = None
) -> tuple[int, ...]:
    """Return one-based terminal rows occupied by vessel records."""

    viewport_width = max(20, int(width))
    raw = _raw_lines(model, viewport_width, 0, 1.0, False)
    node_width = _node_width(model)
    positions = []
    cursor = 0
    for index, node in enumerate(model.nodes):
        target = _node_line(node, index == 0, viewport_width, node_width)
        cursor = raw.index(target, cursor)
        row = cursor + 1
        if height is None or row <= max(0, int(height) - 1):
            positions.append(row)
        cursor += 1
    return tuple(positions)


def node_activity_positions(
    model: HarborBoardModel,
    width: int,
    height: int,
    activity_by_node: Mapping[str, Sequence[int]],
) -> Dict[str, tuple[int, int]]:
    """Return exact visible row/column coordinates of rendered braille twins."""

    viewport_width = max(20, int(width))
    viewport_height = max(4, int(height))
    raw = _raw_lines(
        model,
        viewport_width,
        0,
        1.0,
        False,
        activity_by_node,
    )
    node_width = _node_width(model)
    positions: Dict[str, tuple[int, int]] = {}
    cursor = 0
    for index, node in enumerate(model.nodes):
        node_id = str(node.get("node_id", UNKNOWN_LABEL))
        if node_id not in activity_by_node:
            continue
        target = _node_line(
            node,
            index == 0,
            viewport_width,
            node_width,
            activity_by_node,
        )
        cursor = raw.index(target, cursor)
        row = cursor + 1
        glyphs = activity_braille(activity_by_node[node_id])
        column = target.find(glyphs) + 1
        if row <= viewport_height - 1 and column > 0:
            positions[node_id] = (row, column)
        cursor += 1
    return positions


def render_plain_dump(model: HarborBoardModel, width: int = 120) -> str:
    viewport_width = max(20, int(width))
    lines = _raw_lines(model, viewport_width, 0, 1.0, False)
    if lines:
        lines = lines[1:]
    return PLAIN_PREFIX + "\n" + "\n".join(lines) + "\n"
