"""Shared focus and hit semantics for terminal choice surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .errors import ProtocolRefusal


@dataclass(frozen=True)
class ChoiceAction:
    kind: str
    option_id: Optional[str] = None


class ChoiceFocusController:
    """Bounded focus controller driven by stable option ids."""

    def __init__(
        self, option_ids: Sequence[str], *, initial_option_id: str
    ) -> None:
        ids = tuple(option_ids)
        if (
            not ids
            or any(not isinstance(option_id, str) or not option_id for option_id in ids)
            or len(ids) != len(set(ids))
        ):
            raise ProtocolRefusal(
                "choice_option_invalid",
                "choice options require non-empty unique ids",
            )
        if initial_option_id not in ids:
            raise ProtocolRefusal(
                "choice_initial_option_invalid",
                "the initial choice option must be one of the option ids",
            )
        self._option_ids = ids
        self._focused_index = ids.index(initial_option_id)

    @property
    def focused_option_id(self) -> str:
        return self._option_ids[self._focused_index]

    def handle_key(self, key: str) -> ChoiceAction:
        if key in {"KEY_DOWN", "j", "\x1b[B"}:
            self._focused_index = min(
                len(self._option_ids) - 1, self._focused_index + 1
            )
            return ChoiceAction("focused", self.focused_option_id)
        if key in {"KEY_UP", "k", "\x1b[A"}:
            self._focused_index = max(0, self._focused_index - 1)
            return ChoiceAction("focused", self.focused_option_id)
        if len(key) == 1 and key in "123456789":
            index = int(key) - 1
            if index < len(self._option_ids):
                self._focused_index = index
                return ChoiceAction("focused", self.focused_option_id)
        return ChoiceAction("none")

    def handle_pointer(self, option_id: str, *, activate: bool) -> ChoiceAction:
        if option_id not in self._option_ids:
            return ChoiceAction("none")
        self._focused_index = self._option_ids.index(option_id)
        return ChoiceAction("activated" if activate else "focused", option_id)
