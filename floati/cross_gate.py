"""Closed, ceremony-owned cross gate images and atomic replacement."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .cross_coordinates import MAX_INTEGER
from .errors import ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame


_HEX = re.compile(r"[0-9a-f]{32}\Z")
_KEY = re.compile(r"([0-9a-f]{32})/(S-to-T|T-to-S)\Z")
_JOURNAL = re.compile(r"cross-[0-9a-f]{32}-g[1-9][0-9]{0,8}\Z")
_COUNTERS = {"out_next": 1, "in_hwm": 0, "max_session": 0}
DIRECTIONS = ("S-to-T", "T-to-S")


def _refused():
    return ProtocolRefusal("cross_gate_ownership_unproved", "the gate image or its ceremony ownership is unproved")


def _hex(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _integer(value, low):
    return type(value) is int and low <= value <= MAX_INTEGER


@dataclass(frozen=True)
class GateOwner:
    journal_id: str
    enable_id: str
    enable_generation: int
    ceremony_token: str
    pair_id: str

    def fields(self) -> dict:
        if (not all(_hex(identity) for identity in (self.enable_id, self.ceremony_token, self.pair_id))
            or not _integer(self.enable_generation, 1)
            or not isinstance(self.journal_id, str) or not _JOURNAL.fullmatch(self.journal_id)
            or not self.journal_id.startswith(f"cross-{self.enable_id}-g")):
            raise _refused()
        return {
            "enable_id": self.enable_id,
            "enable_generation": self.enable_generation,
            "ceremony_token": self.ceremony_token,
        }

    def keys(self) -> tuple:
        return f"{self.pair_id}/S-to-T", f"{self.pair_id}/T-to-S"


@dataclass(frozen=True)
class GateImage:
    journal_id: str
    _entries: Mapping[str, Mapping[str, object]]

    @classmethod
    def empty(cls, journal_id: str) -> GateImage:
        return cls(journal_id, {})

    @classmethod
    def decode(cls, payload: bytes) -> GateImage:
        try:
            rows = decode_frames(payload + b"\n")
        except FrameError as exc:
            raise _refused() from exc
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise _refused()
        row = rows[0]
        if set(row) != {"v", "journal_id", "entries"} or type(row["v"]) is not int or row["v"] != 7:
            raise _refused()
        journal_id, entries = row["journal_id"], row["entries"]
        if not isinstance(journal_id, str) or not _JOURNAL.fullmatch(journal_id) or not isinstance(entries, dict):
            raise _refused()
        pairs = set()
        for key, entry in entries.items():
            match = _KEY.fullmatch(key)
            if match is None or not isinstance(entry, dict):
                raise _refused()
            pairs.add(match.group(1))
            owner = GateOwner(journal_id, entry.get("enable_id"), entry.get("enable_generation"),
                              entry.get("ceremony_token"), match.group(1))
            if set(entry) != owner.fields().keys() | _COUNTERS.keys() or any(
                not _integer(entry[counter], low) for counter, low in _COUNTERS.items()
            ):
                raise _refused()
        image = cls(journal_id, entries)
        if len(pairs) > 64 or image.encoded() != payload:
            raise _refused()
        return image

    def encoded(self) -> bytes:
        return encode_frame({"v": 7, "journal_id": self.journal_id, "entries": dict(self._entries)})[:-1]

    def _initial(self, owner: GateOwner) -> dict:
        if owner.journal_id != self.journal_id:
            raise _refused()
        return dict(owner.fields(), **_COUNTERS)

    def provision(self, owner: GateOwner) -> GateImage:
        initial = self._initial(owner)
        keys = owner.keys()
        present = [self._entries[key] for key in keys if key in self._entries]
        if present:
            if present != [initial, initial]:
                raise _refused()
            return self
        return self.decode(GateImage(self.journal_id, dict(self._entries, **dict.fromkeys(keys, initial))).encoded())

    def collect(self, owner: GateOwner, *, abandoned: bool, activated: bool) -> GateImage:
        initial = self._initial(owner)
        if not abandoned or activated:
            return self
        keys = owner.keys()
        if any(self._entries[key] != initial for key in keys if key in self._entries):
            raise _refused()
        entries = {key: entry for key, entry in self._entries.items() if key not in keys}
        return GateImage(self.journal_id, entries)

    def advance(self, owner: GateOwner, direction: str, counter: str, value: int) -> GateImage:
        self.counters(owner, direction)
        if counter not in _COUNTERS:
            raise _refused()
        key = f"{owner.pair_id}/{direction}"
        if not _integer(value, self._entries[key][counter] + 1):
            raise _refused()
        return GateImage(self.journal_id, dict(self._entries, **{key: dict(self._entries[key], **{counter: value})}))

    def counters(self, owner: GateOwner, direction: str) -> tuple:
        """Read one counter triple only after matching both direction owners."""
        self._initial(owner)
        fields = owner.fields()
        if direction not in DIRECTIONS or any(
            key not in self._entries or any(self._entries[key].get(k) != v for k, v in fields.items())
            for key in owner.keys()
        ):
            raise _refused()
        row = self._entries[f"{owner.pair_id}/{direction}"]
        return row["out_next"], row["in_hwm"], row["max_session"]

    def check_counter_update(self, successor: GateImage, active_keys) -> None:
        """Limit a transport update to advancing counters of settled owners."""
        if self.journal_id != successor.journal_id or self._entries.keys() != successor._entries.keys():
            raise _refused()
        for key, before in self._entries.items():
            after = successor._entries[key]
            if before == after:
                continue
            if (key not in active_keys or any(after[field] != before[field] for field in before.keys() - _COUNTERS.keys())
                or any(after[counter] < before[counter] for counter in _COUNTERS)):
                raise _refused()


@dataclass(frozen=True)
class GateFloors:
    carried_max_session: int
    retained_max_session: int
    carried_out_next: int
    retained_out_next: int

    @property
    def max_session(self) -> int:
        return max(self.carried_max_session, self.retained_max_session)

    @property
    def outbound_min_next(self) -> int:
        return max(self.carried_out_next, self.retained_out_next)


def gate_history(records, enable_id, enable_generation, pair_ids):
    """Collect separately the carried and retained counters for one enable."""
    carried, retained = {}, {}
    for row in records:
        if row["kind"] == "cross_journal_link" and row.get("schema_version") == 2:
            for key, floors in row["gate_maxima"].items():
                if (key.split("/", 1)[0] in pair_ids
                    and (enable_id, enable_generation) == (floors["enable_id"], floors["enable_generation"])):
                    carried[key] = floors["max_session"], floors["max_out_next"]
        if (row["kind"] != "remote_session_opened" or row.get("schema_version") != 1
            or row.get("pair_id") not in pair_ids
            or (enable_id, enable_generation) != (row.get("enable_id"), row.get("enable_generation"))):
            continue
        counter, side = row.get("gate_counter"), row.get("local_side")
        if (not _hex(row.get("pair_id")) or row.get("direction") not in DIRECTIONS
            or side not in ("listener", "dialler") or not _integer(counter, 1)
            or (side == "dialler" and counter == MAX_INTEGER)):
            raise _refused()
        key = f"{row['pair_id']}/{row['direction']}"
        session, outbound = retained.get(key, (0, 1))
        retained[key] = max(session, counter), max(outbound, counter + 1 if side == "dialler" else 1)
    return {
        key: GateFloors(carried.get(key, (0, 1))[0], retained.get(key, (0, 1))[0],
                        carried.get(key, (0, 1))[1], retained.get(key, (0, 1))[1])
        for key in carried.keys() | retained.keys()
    }


def validate_gate_link(record: Mapping[str, object]) -> None:
    maxima = record.get("gate_maxima")
    if (set(record) != {"id", "kind", "schema_version", "previous_journal_id", "previous_through_seq",
                        "previous_head_sha256", "previous_trust_head", "reason", "gate_maxima"}
        or type(record["schema_version"]) is not int
        or not isinstance(record["previous_journal_id"], str) or not _JOURNAL.fullmatch(record["previous_journal_id"])
        or not _integer(record["previous_through_seq"], 0)
        or any(not isinstance(record[field], str) or not re.fullmatch(r"[0-9a-f]{64}", record[field])
               for field in ("previous_head_sha256", "previous_trust_head"))
        or not isinstance(maxima, dict) or not 1 <= len(maxima) <= 128):
        raise _refused()
    for key, value in maxima.items():
        if (not _KEY.fullmatch(key) or not isinstance(value, dict)
            or set(value) != {"enable_id", "enable_generation", "max_session", "max_out_next"}
            or not _hex(value["enable_id"]) or not _integer(value["enable_generation"], 1)
            or not _integer(value["max_session"], 0) or not _integer(value["max_out_next"], 1)):
            raise _refused()


def replace_gate(storage, expected: GateImage, successor: GateImage) -> GateImage:
    """Replace one exact image while the caller holds its lifecycle and cross locks."""
    name, temporary = "gate-state", ".gate-state.tmp"
    if (successor.journal_id != expected.journal_id or storage.read(name) != expected.encoded()
        or storage.read(temporary) is not None):
        raise _refused()
    expected_bytes = successor.encoded()
    GateImage.decode(expected_bytes)
    if expected_bytes == expected.encoded():
        return expected
    storage.create(temporary, expected_bytes)
    storage.sync(temporary)
    storage.rename(temporary, name)
    storage.sync_directory()
    if storage.read(name) != expected_bytes:
        raise _refused()
    return successor
