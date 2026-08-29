"""Human-only approval panel state and dependency-free terminal rendering."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Mapping, MutableMapping, Optional, Sequence

from .errors import ProtocolRefusal
from .tui_render import APPROVAL_HEADER, APPROVAL_HINTS, APPROVAL_RECORD


MULTI_CLICK_WINDOW_MS = 300
FOCUS_COLOR = "\x1b[38;5;208m"
RESET = "\x1b[0m"


@dataclass(frozen=True)
class ApprovalOption:
    option_id: str
    label: str
    scope: str

    def __post_init__(self) -> None:
        if not self.option_id or not self.label or not self.scope:
            raise ProtocolRefusal(
                "approval_option_invalid",
                "approval options require stable ids, labels, and scopes",
            )

    @property
    def broad(self) -> bool:
        return self.scope != "request"


@dataclass(frozen=True)
class ApprovalPanelRequest:
    request_id: str
    record: MutableMapping[str, object]
    options: Sequence[ApprovalOption]

    def __post_init__(self) -> None:
        if not self.request_id or not isinstance(self.record, MutableMapping):
            raise ProtocolRefusal(
                "approval_panel_request_invalid",
                "approval panels require one identified mutable record",
            )
        option_ids = tuple(option.option_id for option in self.options)
        if not option_ids or len(option_ids) != len(set(option_ids)):
            raise ProtocolRefusal(
                "approval_option_invalid",
                "approval option ids must be present and unique",
            )


@dataclass(frozen=True)
class ApprovalPanelFrame:
    text: str
    record_body: str
    record_digest: str
    option_rows: Mapping[str, int]


@dataclass(frozen=True)
class ApprovalPanelAction:
    kind: str
    option_id: Optional[str] = None
    receipt: Optional[Mapping[str, object]] = None
    note_code: Optional[str] = None


def _record_body(record: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolRefusal(
            "approval_record_unrenderable",
            "approval record must be finite JSON data",
        ) from exc


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def render_approval_panel(
    request: ApprovalPanelRequest,
    *,
    focused_option_id: str,
    width: int,
    color_tier: str,
) -> ApprovalPanelFrame:
    """Render one complete panel; returned rows are the sole mouse hit map."""

    if width < 40:
        raise ProtocolRefusal(
            "approval_panel_width_invalid",
            "approval panels require at least 40 terminal columns",
        )
    if color_tier not in {"256", "16", "mono"}:
        raise ProtocolRefusal(
            "approval_color_tier_invalid",
            "approval color tier must be 256, 16, or mono",
        )

    body = _record_body(request.record)
    content_width = width - 4
    top = "┌" + "─" * (width - 2) + "┐"
    bottom = "└" + "─" * (width - 2) + "┘"
    rows = [top]

    def panel_line(value: str = "") -> None:
        rows.append("│ " + value.ljust(content_width) + " │")

    panel_line(APPROVAL_HEADER)
    panel_line()
    panel_line(APPROVAL_RECORD)
    for line in body.splitlines():
        if len(line) <= content_width:
            panel_line(line)
        else:
            for start in range(0, len(line), content_width):
                panel_line(line[start : start + content_width])
    panel_line()

    option_rows: dict[str, int] = {}
    for index, option in enumerate(request.options, start=1):
        focused = option.option_id == focused_option_id
        marker = ">" if focused else " "
        visible = f"{marker} {index}  {option.label}"
        option_rows[option.option_id] = len(rows) + 1
        panel_line(visible)
        if focused and color_tier != "mono":
            rows[-1] = rows[-1].replace(
                f"{marker} {index}",
                FOCUS_COLOR + f"{marker} {index}" + RESET,
                1,
            )
    panel_line()
    panel_line(APPROVAL_HINTS)
    rows.append(bottom)
    return ApprovalPanelFrame(
        text="\n".join(rows),
        record_body=body,
        record_digest=_digest(body),
        option_rows=option_rows,
    )


class ApprovalPanelController:
    """FIFO approval state whose only resolving inputs are Esc, Enter, or mouse."""

    def __init__(
        self,
        *,
        session_id: str,
        composer_text: str = "",
        receipt_sink: Optional[Callable[[Mapping[str, object]], None]] = None,
    ) -> None:
        if not session_id:
            raise ProtocolRefusal(
                "approval_session_invalid",
                "approval panels require the acting human session",
            )
        self.session_id = session_id
        self.composer_text = composer_text
        self._stashed_composer: Optional[str] = None
        self._queue: list[ApprovalPanelRequest] = []
        self._focused_index = 0
        self._last_click: Optional[tuple[str, int]] = None
        self._rendered_request_id: Optional[str] = None
        self._rendered_record_digest: Optional[str] = None
        self._render_width = 72
        self._render_color_tier = "mono"
        self._resolved_receipts: list[Mapping[str, object]] = []
        self._receipt_sink = receipt_sink

    @property
    def active_request_id(self) -> Optional[str]:
        return self._queue[0].request_id if self._queue else None

    @property
    def focused_option_id(self) -> Optional[str]:
        if not self._queue:
            return None
        return self._queue[0].options[self._focused_index].option_id

    @property
    def resolved_receipts(self) -> list[Mapping[str, object]]:
        return [dict(receipt) for receipt in self._resolved_receipts]

    def enqueue(self, request: ApprovalPanelRequest) -> None:
        if any(row.request_id == request.request_id for row in self._queue):
            raise ProtocolRefusal(
                "approval_panel_duplicate",
                "an approval request can appear only once in the panel queue",
            )
        if not self._queue:
            self._stashed_composer = self.composer_text
            self.composer_text = ""
        self._queue.append(request)
        if len(self._queue) == 1:
            self._reset_front()

    def render(
        self,
        *,
        width: int = 72,
        color_tier: str = "mono",
    ) -> ApprovalPanelFrame:
        request = self._front()
        frame = render_approval_panel(
            request,
            focused_option_id=str(self.focused_option_id),
            width=width,
            color_tier=color_tier,
        )
        self._rendered_request_id = request.request_id
        self._rendered_record_digest = frame.record_digest
        self._render_width = width
        self._render_color_tier = color_tier
        return frame

    def handle_key(self, key: str) -> ApprovalPanelAction:
        request = self._front()
        if key in {"ESC", "\x1b"}:
            refuse_index = next(
                (
                    index
                    for index, option in enumerate(request.options)
                    if option.option_id == "refuse-once"
                ),
                None,
            )
            if refuse_index is None:
                raise ProtocolRefusal(
                    "approval_refuse_option_missing",
                    "Esc requires the stable refuse-once option",
                )
            return self._commit(refuse_index)
        if key in {"ENTER", "\r", "\n"}:
            return self._commit(self._focused_index)
        if key in {"KEY_DOWN", "j", "\x1b[B"}:
            self._focused_index = min(
                len(request.options) - 1, self._focused_index + 1
            )
            self._last_click = None
            return ApprovalPanelAction("focused", self.focused_option_id)
        if key in {"KEY_UP", "k", "\x1b[A"}:
            self._focused_index = max(0, self._focused_index - 1)
            self._last_click = None
            return ApprovalPanelAction("focused", self.focused_option_id)
        if len(key) == 1 and key in "123456789":
            index = int(key) - 1
            if index < len(request.options):
                self._focused_index = index
                self._last_click = None
                return ApprovalPanelAction("focused", self.focused_option_id)
        return ApprovalPanelAction("none")

    def handle_mouse(self, option_id: str, *, now_ms: int) -> ApprovalPanelAction:
        request = self._front()
        try:
            index = next(
                index
                for index, option in enumerate(request.options)
                if option.option_id == option_id
            )
        except StopIteration:
            self._last_click = None
            return ApprovalPanelAction("none")
        if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
            raise ProtocolRefusal(
                "approval_mouse_time_invalid",
                "mouse activation time must be non-negative milliseconds",
            )

        option = request.options[index]
        previous = self._last_click
        self._focused_index = index
        self._last_click = (option_id, now_ms)
        if (
            previous is None
            or previous[0] != option_id
            or now_ms < previous[1]
            or now_ms - previous[1] > MULTI_CLICK_WINDOW_MS
        ):
            return ApprovalPanelAction("focused", option_id)
        self._last_click = None
        if option.broad:
            return ApprovalPanelAction(
                "refused",
                option_id,
                note_code="approval_mouse_broad_forbidden",
            )
        return self._commit(index)

    def resolve_non_human(self, *, source: str) -> None:
        del source
        raise ProtocolRefusal(
            "approval_human_act_required",
            "approval panels resolve only from a human keypress or pointer gesture",
        )

    def _front(self) -> ApprovalPanelRequest:
        if not self._queue:
            raise ProtocolRefusal(
                "approval_panel_empty", "there is no approval request under review"
            )
        return self._queue[0]

    def _reset_front(self) -> None:
        request = self._front()
        self._focused_index = next(
            (
                index
                for index, option in enumerate(request.options)
                if option.option_id == "refuse-once"
            ),
            min(
                range(len(request.options)),
                key=lambda index: (
                    request.options[index].broad,
                    index,
                ),
            ),
        )
        self._last_click = None
        self._rendered_request_id = None
        self._rendered_record_digest = None

    def _commit(self, option_index: int) -> ApprovalPanelAction:
        request = self._front()
        if self._rendered_request_id != request.request_id:
            self.render(
                width=self._render_width,
                color_tier=self._render_color_tier,
            )
        current_digest = _digest(_record_body(request.record))
        if current_digest != self._rendered_record_digest:
            raise ProtocolRefusal(
                "approval_panel_stale",
                "the record changed after it was rendered; review a fresh panel",
            )

        option = request.options[option_index]
        receipt: Mapping[str, object] = {
            "kind": "approval_panel_decision_receipt",
            "request_id": request.request_id,
            "option_id": option.option_id,
            "option_scope": option.scope,
            "decision": (
                "denied" if option.option_id == "refuse-once" else "approved"
            ),
            "acting_session": self.session_id,
            "rendered_record_digest": current_digest,
        }
        if self._receipt_sink is not None:
            self._receipt_sink(receipt)
        self._resolved_receipts.append(dict(receipt))
        self._queue.pop(0)
        if self._queue:
            self._reset_front()
        else:
            self.composer_text = self._stashed_composer or ""
            self._stashed_composer = None
            self._last_click = None
            self._rendered_request_id = None
            self._rendered_record_digest = None
        return ApprovalPanelAction("resolved", option.option_id, receipt)
