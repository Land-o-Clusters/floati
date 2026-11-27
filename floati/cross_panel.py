"""Current-attempt ownership for a qualified ceremony surface."""

from __future__ import annotations

from threading import RLock

from .errors import ProtocolRefusal


class PanelLifecycle:
    """The surface adapter supplies isolation and owns the actual input routes."""

    def __init__(self):
        self._lock = RLock()
        self._current = None
        self._surface = None
        self._role = None
        self._input_used = False
        self._entry_open = False
        self._matched = False
        self._shown = False

    def begin(self, surface, role: str):
        with self._lock:
            if self._current is not None or role not in ("S", "T") or surface.isolated is not True:
                raise ProtocolRefusal("cross_panel_unavailable", "the selected surface cannot isolate this attempt")
            surface.clear()
            self._current = object()
            self._surface = surface
            self._role = role
            self._input_used = self._entry_open = self._matched = self._shown = False
            return self._current

    def single_entry(self, attempt, compare):
        with self._lock:
            if attempt is not self._current or self._current is None or self._entry_open or self._input_used:
                raise ProtocolRefusal("cross_panel_input_refused", "this attempt has no available comparison entry")
            self._entry_open = True

        def enter(value):
            with self._lock:
                if attempt is not self._current or not self._entry_open:
                    return False
                self._entry_open = False
                self._input_used = True
                self._matched = compare(value) is True
                return self._matched

        return enter

    def render_b(self, attempt, rendered: str) -> None:
        with self._lock:
            if attempt is not self._current or self._role != "T" or not self._matched or self._shown:
                raise ProtocolRefusal("cross_panel_output_refused", "the current local comparison has not authorized this display")
            self._shown = True
            self._surface.draw(rendered)

    def end(self, attempt) -> None:
        with self._lock:
            if attempt is self._current and self._current is not None:
                surface = self._surface
                self._current = self._surface = self._role = None
                self._input_used = self._entry_open = self._matched = self._shown = False
                surface.clear()
