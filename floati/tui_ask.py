"""Compose and display one receipt-backed question inside the live board."""

from __future__ import annotations

import os
from concurrent.futures import Future
from textwrap import wrap
from threading import Event, Thread
from typing import Callable, Mapping, Optional
from unicodedata import east_asian_width

from .copy import register


ASK_HINTS = register("tui.ask.hints", "↑/k ↓/j select  ? ask  enter detail  a ack  q quit", "Live board keys")
ASK_TITLE = register("tui.ask.title", "Ask {recipient}", "Live board ask panel")
ASK_NEXT = register("tui.ask.next", "Enter next  Ctrl-G cancel", "Live board ask editor keys")
ASK_CONFIRM = register("tui.ask.confirm", "Ctrl-D send  Ctrl-G cancel  ↑/↓ scroll", "Live board ask review keys")
ASK_WAITING = register("tui.ask.waiting", "Waiting for the reply…", "Live board ask progress")
ASK_STOP_WAITING = register("tui.ask.stop_waiting", "Ctrl-G stop waiting  q quit", "Live board ask progress keys")
ASK_TOO_LONG = register("tui.ask.too_long", "Input exceeds 1024 characters", "Live board ask editor feedback")
ASK_CLOSE = register("tui.ask.close", "Enter close  ↑/↓ scroll", "Live board ask result keys")
ASK_RECEIPT = register("tui.ask.receipt", "Receipt", "Live board ask result label")
ASK_FIELDS = (
    ("sender", register("tui.ask.sender", "From node", "Live board ask editor field")),
    ("repo", register("tui.ask.repo", "Repository name", "Live board ask editor field")),
    ("sha", register("tui.ask.sha", "Banked commit SHA in this checkout", "Live board ask editor field")),
    ("doc", register("tui.ask.doc", "Repository-relative document", "Live board ask editor field")),
    ("wait_seconds", register("tui.ask.wait_seconds", "Wait seconds (0–86400)", "Live board ask editor field")),
    ("note", register("tui.ask.note", "Question", "Live board ask editor field")),
)

_ENTER = ("ENTER", "\r", "\n")
_CANCEL = ("\x07", "\x1b[103;5u")  # Legacy and disambiguated Ctrl-G.
_SEND = ("\x04", "\x1b[100;5u")  # Legacy and disambiguated Ctrl-D.
_INPUT_LIMIT = 1024

AskCallback = Callable[[str, Mapping[str, str], Event], Mapping[str, object]]


class BoardAskTask:
    """Own the shared ask wait and wake the board once its result is ready."""

    def __init__(self, callback: AskCallback, recipient: str, fields: Mapping[str, str]) -> None:
        self.future: Future[Mapping[str, object]] = Future()
        self.stop_waiting = Event()
        self.descriptor, writer = os.pipe()
        self.thread = Thread(target=self._run, args=(callback, recipient, fields, writer))
        try:
            self.thread.start()
        except BaseException:
            os.close(writer)
            os.close(self.descriptor)
            raise

    def _run(self, callback: AskCallback, recipient: str, fields: Mapping[str, str], writer: int) -> None:
        try:
            self.future.set_result(callback(recipient, fields, self.stop_waiting))
        except BaseException as exc:
            self.future.set_exception(exc)
        finally:
            os.close(writer)

    def close(self) -> None:
        self.stop_waiting.set()
        try:
            self.thread.join()
        finally:
            os.close(self.descriptor)


def _safe_display(value: object) -> str:
    return "".join(char if char.isprintable() else " " for char in str(value))


def _wrap_rows(value: object, width: int) -> list[str]:
    text = _safe_display(value)
    if any(east_asian_width(char) in {"W", "F"} for char in text):
        width //= 2
    return wrap(text, max(1, width), break_on_hyphens=False) or [""]


class BoardAskPanel:
    def __init__(self, recipient: str) -> None:
        self.recipient = recipient
        self.fields: dict[str, str] = {}
        self.draft = ""
        self.excess = 0
        self.submitted = False
        self.result: Optional[Mapping[str, object]] = None
        self.offset = 0

    def handle_key(self, key: str) -> str:
        if key in _CANCEL or (self.result is not None and key in _ENTER):
            return "close"
        if self.submitted and self.result is None:
            return "none"
        if len(self.fields) == len(ASK_FIELDS):
            if key in _SEND and self.result is None:
                self.submitted = True
                self.offset = 0
                return "redraw"
            if key in {"KEY_UP", "KEY_DOWN"}:
                self.offset = max(0, self.offset + (1 if key == "KEY_DOWN" else -1))
                return "redraw"
            return "none"
        name, _ = ASK_FIELDS[len(self.fields)]
        if key in _ENTER:
            value = self.draft.strip() if name != "note" else self.draft
            if self.excess or not value.strip():
                return "none"
            if name == "wait_seconds" and not value.isdecimal():
                return "none"
            self.fields[name] = value
            self.draft = ""
        elif key in {"\x08", "\x7f"}:
            if self.excess:
                self.excess -= 1
            else:
                self.draft = self.draft[:-1]
        elif len(key) == 1 and key.isprintable():
            if len(self.draft) < _INPUT_LIMIT:
                self.draft += key
            else:
                self.excess += 1
        else:
            return "none"
        return "redraw"

    def render(self, board_frame: str, width: int, height: int) -> str:
        width = max(1, width)
        max_body = max(1, height - 3)
        title = _wrap_rows(ASK_TITLE.format(recipient=self.recipient), width)[0]
        if self.result is not None:
            status = [self.result.get("outcome", "UNKNOWN"), self.result.get("code")]
            body = [" ".join(str(part) for part in status if part)]
            for key in ("answer_id", "decline_id", "timeout_id"):
                if receipt := self.result.get(key):
                    body.append(f"{ASK_RECEIPT} {receipt}")
            body.extend(str(self.result[key]) for key in ("answer_note", "reason", "detail") if key in self.result)
            footer = ASK_CLOSE
        elif self.submitted:
            body, footer = [ASK_WAITING], ASK_STOP_WAITING
        elif len(self.fields) == len(ASK_FIELDS):
            body = [f"{label}  {self.fields[name]}" for name, label in ASK_FIELDS]
            footer = ASK_CONFIRM
        else:
            _, label = ASK_FIELDS[len(self.fields)]
            body = [label, *_wrap_rows(self.draft, width)[-max(1, max_body - 1):]]
            if self.excess:
                body.insert(0, ASK_TOO_LONG)
            footer = ASK_NEXT
        rows = [row for item in body for row in _wrap_rows(item, width)]
        self.offset = min(self.offset, max(0, len(rows) - max_body))
        visible = rows[self.offset:self.offset + max_body]
        panel = [title, *visible, _wrap_rows(footer, width)[0]]
        # Occupy the bottom of the board, preserving its header and as much roster as fits.
        board_rows = board_frame.splitlines()[:-1]
        return "\n".join([*board_rows[:max(0, height - len(panel))], *panel])
