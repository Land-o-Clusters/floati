"""Dependency-free keyboard-first terminal loop for the harbor board."""

from __future__ import annotations

import ctypes
import errno
import os
import select
import signal
import shutil
import sys
import termios
import tty
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, TextIO

from .tui_render import (
    HarborBoardModel,
    node_activity_positions,
    node_row_positions,
    render_frame,
    render_plain_dump,
)
from .tui_protocol import (
    MouseEvent,
    TerminalInput,
    TerminalInputDecoder,
    decode_terminal_input,
    kitty_buoy_image,
    kitty_delete_image,
    kitty_keyboard_mode,
    mouse_tracking,
    synchronized_output_frame,
)
from .tui_capabilities import probe_terminal_capabilities
from .tui_activity import board_activity
from .tui_graphics import kitty_delete_images, plan_activity_overlays
from .consumption import ConsumptionLedger
from .copy import (
    BOARD_EVENT_ROOT_UNAVAILABLE_DETAIL,
    BOARD_EVENT_SOURCE_UNSUPPORTED_DETAIL,
    BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL,
)
from .errors import ProtocolRefusal, SnapshotRefusal
from .jsonl import read_records_snapshot
from .cursor import SparseCursor
from .projection import EffectStatusProjection, FleetProjection
from .root import FloatiRoot
from .snapshot import SnapshotStore
from .workers import WorkerReceipts


@dataclass(frozen=True)
class BoardAction:
    kind: str
    node_id: Optional[str] = None
    message_id: Optional[str] = None


@dataclass(frozen=True)
class BoardModelEvent:
    """One upstream-projected state change for the otherwise blocking Board."""

    model: HarborBoardModel


@dataclass(frozen=True)
class BoardResizeEvent:
    """A terminal viewport change delivered through the resize self-pipe."""


@dataclass(frozen=True)
class BoardFilesystemEvent:
    """A durable-root change delivered by the host filesystem notifier."""


class _BoardResizeWakeup:
    """Turn SIGWINCH into a selectable byte without installing a timer."""

    def __init__(self) -> None:
        self.read_descriptor, self._write_descriptor = os.pipe()
        os.set_blocking(self.read_descriptor, False)
        os.set_blocking(self._write_descriptor, False)
        self._previous_handler: object = None
        self._installed = False
        self._closed = False

    def install(self) -> None:
        self._previous_handler = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, self._handle_signal)
        self._installed = True

    def _handle_signal(self, signum: int, frame: object) -> None:
        del signum, frame
        try:
            os.write(self._write_descriptor, b"r")
        except (BlockingIOError, OSError):
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._installed:
            signal.signal(signal.SIGWINCH, self._previous_handler)
        os.close(self.read_descriptor)
        os.close(self._write_descriptor)


def _board_watch_directories(root: Path) -> tuple[Path, ...]:
    directories = []
    for current, names, _files in os.walk(root, followlinks=False):
        current_path = Path(current)
        names[:] = [
            name for name in names if not (current_path / name).is_symlink()
        ]
        directories.append(current_path)
    return tuple(directories)


def _board_watch_paths(root: Path) -> tuple[Path, ...]:
    paths = []
    for current, names, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        names[:] = [
            name for name in names if not (current_path / name).is_symlink()
        ]
        paths.append(current_path)
        paths.extend(
            path
            for name in files
            if not (path := current_path / name).is_symlink()
        )
    return tuple(paths)


class _KqueueBoardFilesystemWakeup:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._queue = select.kqueue()
        self._descriptors: dict[Path, int] = {}
        self._closed = False
        try:
            self._refresh()
        except BaseException:
            self.close()
            raise

    def fileno(self) -> int:
        return self._queue.fileno()

    def _refresh(self) -> None:
        current = set(_board_watch_paths(self._root))
        if self._root not in current:
            raise ProtocolRefusal(
                "board_event_watch_unavailable",
                BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL,
            )
        for path in tuple(self._descriptors):
            if path not in current:
                os.close(self._descriptors.pop(path))
        flags = (
            select.KQ_NOTE_WRITE
            | select.KQ_NOTE_EXTEND
            | select.KQ_NOTE_ATTRIB
            | select.KQ_NOTE_LINK
            | select.KQ_NOTE_RENAME
            | select.KQ_NOTE_DELETE
            | select.KQ_NOTE_REVOKE
        )
        for path in sorted(current - self._descriptors.keys()):
            try:
                descriptor = os.open(
                    path,
                    getattr(os, "O_EVTONLY", os.O_RDONLY) | os.O_NONBLOCK,
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ProtocolRefusal(
                    "board_event_watch_unavailable",
                    BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL,
                ) from exc
            event = select.kevent(
                descriptor,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=flags,
            )
            try:
                self._queue.control([event], 0, 0)
            except OSError as exc:
                os.close(descriptor)
                raise ProtocolRefusal(
                    "board_event_watch_unavailable",
                    BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL,
                ) from exc
            self._descriptors[path] = descriptor

    def drain(self) -> None:
        events = self._queue.control(
            [], max(1, len(self._descriptors)), 0
        )
        invalid = {
            event.ident
            for event in events
            if event.fflags
            & (select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE | select.KQ_NOTE_REVOKE)
        }
        for path, descriptor in tuple(self._descriptors.items()):
            if descriptor in invalid:
                os.close(descriptor)
                del self._descriptors[path]
        self._refresh()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in self._descriptors.values():
            os.close(descriptor)
        self._descriptors.clear()
        self._queue.close()


class _InotifyBoardFilesystemWakeup:
    _MASK = (
        0x00000002
        | 0x00000004
        | 0x00000008
        | 0x00000040
        | 0x00000080
        | 0x00000100
        | 0x00000200
        | 0x00000400
        | 0x00000800
    )

    def __init__(self, root: Path) -> None:
        self._root = root
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_init1.restype = ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        self._descriptor = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._descriptor < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._watches: dict[Path, int] = {}
        self._closed = False
        try:
            self._refresh()
        except BaseException:
            os.close(self._descriptor)
            raise

    def fileno(self) -> int:
        return self._descriptor

    def _refresh(self) -> None:
        current = set(_board_watch_directories(self._root))
        if self._root not in current:
            raise ProtocolRefusal(
                "board_event_watch_unavailable",
                BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL,
            )
        watched = set(self._watches)
        for path in watched - current:
            del self._watches[path]
        # Reapply every watch: inotify binds to an inode, so a directory that
        # is deleted and recreated at the same path invalidates the old watch
        # even though the path remains present in our mapping.
        for path in sorted(current):
            watch = self._libc.inotify_add_watch(
                self._descriptor,
                os.fsencode(path),
                self._MASK,
            )
            if watch < 0:
                error = ctypes.get_errno()
                if error == errno.ENOENT:
                    continue
                raise ProtocolRefusal(
                    "board_event_watch_unavailable",
                    BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL,
                ) from OSError(error, "inotify_add_watch failed")
            self._watches[path] = watch

    def drain(self) -> None:
        while True:
            try:
                if not os.read(self._descriptor, 65536):
                    break
            except BlockingIOError:
                break
        self._refresh()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._descriptor)
        self._watches.clear()


class BoardFilesystemWakeup:
    """Selectable, timer-free durable-root change source for the live Board."""

    def __init__(self, root: Path) -> None:
        candidate = Path(root)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ProtocolRefusal(
                "board_event_root_unavailable",
                BOARD_EVENT_ROOT_UNAVAILABLE_DETAIL,
            ) from exc
        if (
            not candidate.is_absolute()
            or candidate.is_symlink()
            or not resolved.is_dir()
        ):
            raise ProtocolRefusal(
                "board_event_root_unavailable",
                BOARD_EVENT_ROOT_UNAVAILABLE_DETAIL,
            )
        try:
            if hasattr(select, "kqueue"):
                self._backend = _KqueueBoardFilesystemWakeup(resolved)
            elif sys.platform.startswith("linux"):
                self._backend = _InotifyBoardFilesystemWakeup(resolved)
            else:
                raise ProtocolRefusal(
                    "board_event_source_unsupported",
                    BOARD_EVENT_SOURCE_UNSUPPORTED_DETAIL,
                )
        except ProtocolRefusal:
            raise
        except OSError as exc:
            raise ProtocolRefusal(
                "board_event_watch_unavailable",
                BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL,
            ) from exc

    def fileno(self) -> int:
        return self._backend.fileno()

    def drain(self) -> None:
        self._backend.drain()

    def close(self) -> None:
        self._backend.close()


class BoardController:
    def __init__(self, model: HarborBoardModel) -> None:
        self.model = model
        self.selected = 0
        self.detail_open = False
        self.quit_requested = False

    def update_model(self, model: HarborBoardModel) -> None:
        self.model = model
        self.selected = min(self.selected, max(0, len(model.nodes) - 1))

    def handle_key(self, key: str) -> BoardAction:
        if key in ("j", "KEY_DOWN", "\x1b[B"):
            self.selected = min(max(0, len(self.model.nodes) - 1), self.selected + 1)
            return BoardAction("select")
        if key in ("k", "KEY_UP", "\x1b[A"):
            self.selected = max(0, self.selected - 1)
            return BoardAction("select")
        if key in ("ENTER", "\r", "\n"):
            self.detail_open = not self.detail_open
            return BoardAction("detail")
        if key == "a" and self.model.nodes:
            node = self.model.nodes[self.selected]
            message_id = node.get("visible_message_id")
            if isinstance(message_id, str):
                return BoardAction("ack", str(node.get("node_id")), message_id)
            return BoardAction("none")
        if key == "q":
            self.quit_requested = True
            return BoardAction("quit")
        return BoardAction("none")

    def handle_mouse(
        self,
        event: MouseEvent,
        *,
        node_rows: tuple[int, ...],
        viewport_width: int,
    ) -> BoardAction:
        if (
            not event.pressed
            or event.button != 0
            or not 1 <= event.column <= viewport_width
            or event.row not in node_rows
        ):
            return BoardAction("none")
        self.selected = node_rows.index(event.row)
        self.detail_open = True
        return BoardAction("detail")


def state_signature(model: HarborBoardModel) -> str:
    return repr((
        model.nodes,
        model.work_items,
        model.deliveries,
        model.acknowledgments,
        model.denials,
        model.stale_leases,
        model.workers,
        model.worker_receipts,
        model.consumption,
        model.worker_refusals,
        model.effects,
    ))


def model_from_root(root: FloatiRoot, now: Optional[datetime] = None) -> HarborBoardModel:
    current = datetime.now(timezone.utc) if now is None else now
    current = FleetProjection._current(current)
    store = None
    try:
        store = SnapshotStore(
            root,
            reader="board",
            key="full-redraw",
            discover_sources=FleetProjection(root)._status_sources,
        )
        loaded = store.load()
        return _board_from_snapshot(root, current, loaded.payload, loaded.tails)
    except SnapshotRefusal:
        before_scan = None
        if store is not None:
            try:
                before_scan = store.capture()
            except SnapshotRefusal:
                pass
        model = _model_from_root_full(root, current)
        payload = {
            "model": model.to_snapshot(),
            "work_states": ConsumptionLedger(root).project(),
            "built_at": FleetProjection._timestamp(current),
            "time_sensitive": FleetProjection(root)._status_time_sensitive(),
        }
        if store is not None and before_scan is not None:
            try:
                store.refresh(payload, expected=before_scan)
            except SnapshotRefusal:
                pass
        return model


def _model_from_root_full(root: FloatiRoot, current: datetime) -> HarborBoardModel:
    snapshot = FleetProjection(root).snapshot(current)
    snapshot["effects"] = EffectStatusProjection(root).summary()
    from .events import EVENT_KINDS

    events = [
        record
        for record in read_records_snapshot(
            root, "events.jsonl", allowed_kinds=set(EVENT_KINDS)
        )
        if record["kind"] == "message_envelope"
    ]
    deliveries = []
    acknowledgments = []
    acked = set()
    for node in snapshot["nodes"]:
        node_id = str(node["node_id"])
        node_acks = read_records_snapshot(
            root, Path("receipts/acks") / f"{node_id}.jsonl", allowed_kinds={"ack_receipt"}
        )
        node_deliveries = read_records_snapshot(
            root, Path("receipts/deliveries") / f"{node_id}.jsonl", allowed_kinds={"delivery_receipt", "wake_hold_receipt"}
        )
        acknowledgments.extend(node_acks)
        deliveries.extend(row for row in node_deliveries if row["kind"] == "delivery_receipt")
        acked.update(str(item_id) for record in node_acks for item_id in record["item_ids"])
    nodes = []
    for source in snapshot["nodes"]:
        node = dict(source)
        node["visible_message_id"] = next(
            (
                str(event["id"]) for event in events
                if event["recipient"] == node["node_id"] and event["id"] not in acked
            ),
            None,
        )
        nodes.append(node)
    snapshot = dict(snapshot)
    snapshot["nodes"] = nodes
    denials = read_records_snapshot(root, "receipts/denials.jsonl", allowed_kinds={"denial_receipt"})
    receipts = {
        "deliveries": deliveries,
        "acks": acknowledgments,
        "denials": denials,
        "workers": WorkerReceipts(root).records(),
    }
    work_items = list(ConsumptionLedger(root).project().values())
    return HarborBoardModel.from_projection(snapshot, work_items, receipts)


def _board_from_snapshot(
    root: FloatiRoot,
    current: datetime,
    payload: dict[str, object],
    tails: dict[str, tuple[dict[str, object], ...]],
) -> HarborBoardModel:
    if set(payload) != {"model", "work_states", "built_at", "time_sensitive"}:
        raise SnapshotRefusal(
            "snapshot_payload_invalid", "board snapshot fields are invalid"
        )
    raw_model = payload["model"]
    work_states = payload["work_states"]
    built_at = payload["built_at"]
    time_sensitive = payload["time_sensitive"]
    if (
        not isinstance(raw_model, dict)
        or not isinstance(work_states, dict)
        or not isinstance(built_at, str)
        or not isinstance(time_sensitive, bool)
    ):
        raise SnapshotRefusal(
            "snapshot_payload_invalid", "board snapshot payload is malformed"
        )
    if time_sensitive and FleetProjection._timestamp(current) != built_at:
        raise SnapshotRefusal(
            "snapshot_clock_boundary", "time-sensitive board must be reprojected"
        )
    model = replace(
        HarborBoardModel.from_snapshot(raw_model),
        observed_at=FleetProjection._timestamp(current),
    )
    unsupported = set()
    work_tail = ()
    for path, records in tails.items():
        if not records:
            continue
        if path == ConsumptionLedger.relative_path.as_posix():
            work_tail = records
        elif path == "events.jsonl":
            nodes = [dict(row) for row in model.nodes]
            by_id = {str(row["node_id"]): row for row in nodes}
            for record in records:
                if record.get("kind") in {
                    "delivery_claim", "ledger_repair_receipt",
                }:
                    continue
                if record.get("kind") != "message_envelope":
                    raise SnapshotRefusal(
                        "snapshot_tail_history_required",
                        "event tail needs full ledger history",
                    )
                if record.get("reply_to") is not None:
                    raise SnapshotRefusal(
                        "snapshot_tail_history_required",
                        "reply tail needs omitted causal history",
                    )
                sender = by_id.get(str(record["sender"]))
                recipient = by_id.get(str(record["recipient"]))
                if sender is not None:
                    sender["last_activity"] = record["timestamp"]
                if recipient is not None:
                    recipient["last_activity"] = record["timestamp"]
                    recipient["inbox_depth"] += 1
                    if recipient.get("visible_message_id") is None:
                        recipient["visible_message_id"] = record["id"]
            model = replace(model, nodes=tuple(nodes))
        elif path == "receipts/denials.jsonl":
            model = replace(
                model,
                denials=model.denials + tuple(dict(record) for record in records),
            )
        elif path.startswith("receipts/deliveries/"):
            model = replace(
                model,
                deliveries=model.deliveries
                + tuple(dict(record) for record in records),
            )
        elif path.startswith("receipts/acks/"):
            visible = {
                str(node.get("visible_message_id"))
                for node in model.nodes
                if node.get("visible_message_id") is not None
            }
            if any(
                str(item_id) in visible
                for record in records
                for item_id in record["item_ids"]
            ):
                raise SnapshotRefusal(
                    "snapshot_tail_history_required",
                    "acknowledgment exposes omitted visible-message history",
                )
            model = replace(
                model,
                acknowledgments=model.acknowledgments
                + tuple(dict(record) for record in records),
            )
        elif path == "receipts/worker-refusals.jsonl":
            consumption = dict(model.consumption)
            if any(record["reason_code"] == "worker_work_absent" for record in records):
                consumption["wake_state"] = "unsatisfied_wake"
            model = replace(
                model,
                worker_refusals=model.worker_refusals
                + tuple(dict(record) for record in records),
                consumption=consumption,
            )
        else:
            unsupported.add(path)
    if unsupported:
        raise SnapshotRefusal(
            "snapshot_tail_history_required",
            "board tail needs omitted projection history",
        )
    if work_tail:
        projected = ConsumptionLedger(root).project_tail(work_states, work_tail)
        counts = {
            state: sum(1 for item in projected.values() if item["state"] == state)
            for state in ("open", "claimed", "completed")
        }
        consumption = dict(model.consumption)
        consumption["counts"] = counts
        consumption["state"] = "work_available" if counts["open"] else "caught_up"
        model = replace(
            model,
            work_items=tuple(projected.values()),
            consumption=consumption,
        )
    return model


def model_from_orchestration_frame(frame: dict[str, object]) -> HarborBoardModel:
    """Build one board frame solely from durable orchestration projections."""

    work_items = list(frame.get("work", []))
    snapshot = {
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "nodes": [],
        "stale_leases": [],
        "workers": list(frame.get("workers", [])),
        "consumption": {
            "coordinate": "work/items.jsonl",
            "state": "work_available"
            if any(item.get("readiness") in {"ready", "blocked"} for item in work_items)
            else "caught_up",
            "wake_state": "none",
        },
        "worker_refusals": [],
    }
    receipts = {
        "deliveries": [],
        "acks": [],
        "denials": [],
        "workers": list(frame.get("receipts", [])),
    }
    return HarborBoardModel.from_projection(snapshot, work_items, receipts)


def acknowledge_visible(
    root: FloatiRoot, action: BoardAction, *, acting_session_id: str
) -> None:
    if action.kind != "ack" or action.node_id is None or action.message_id is None:
        return
    SparseCursor(root).ack(
        action.node_id,
        [action.message_id],
        acting_session_id=acting_session_id,
    )


def _interactive(stream: TextIO) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def _color_tier() -> str:
    if os.environ.get("NO_COLOR"):
        return "mono"
    term = os.environ.get("TERM", "")
    colorterm = os.environ.get("COLORTERM", "")
    if "256color" in term or colorterm.casefold() in {"truecolor", "24bit"}:
        return "256"
    return "16"


def _read_terminal_input(
    stream: TextIO,
    timeout: Optional[float],
    decoder: TerminalInputDecoder,
    pending: list[TerminalInput],
    resize_descriptor: Optional[int] = None,
    model_descriptor: Optional[int] = None,
) -> object:
    if pending:
        return pending.pop(0)
    descriptor = stream.fileno()
    descriptors = [descriptor]
    if resize_descriptor is not None:
        descriptors.append(resize_descriptor)
    if model_descriptor is not None:
        descriptors.append(model_descriptor)
    readable, _, _ = select.select(descriptors, [], [], timeout)
    if not readable:
        return ""
    if resize_descriptor is not None and resize_descriptor in readable:
        try:
            os.read(resize_descriptor, 4096)
        except BlockingIOError:
            pass
        return BoardResizeEvent()
    if model_descriptor is not None and model_descriptor in readable:
        return BoardFilesystemEvent()
    pending.extend(decoder.feed(os.read(descriptor, 64)))
    return "" if not pending else pending.pop(0)


def run_board(
    *,
    model_loader: Callable[[], HarborBoardModel],
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    ack_callback: Optional[Callable[[BoardAction], None]] = None,
    capability_receipt_sink: Optional[
        Callable[[Mapping[str, object]], None]
    ] = None,
    read_event: Optional[Callable[[Optional[float]], object]] = None,
    model_root: Optional[Path] = None,
    model_wakeup: Optional[BoardFilesystemWakeup] = None,
    no_animation: bool = False,
    terminal_response: Optional[bytes] = None,
) -> int:
    model = model_loader()
    if no_animation or not _interactive(input_stream) or not _interactive(output_stream) or os.environ.get("TERM") == "dumb":
        output_stream.write(render_plain_dump(model))
        output_stream.flush()
        return 0

    if read_event is not None and model_wakeup is not None:
        raise ValueError("injected Board events cannot also use a filesystem wakeup")
    if model_root is not None and model_wakeup is not None:
        raise ValueError("Board model root and filesystem wakeup are mutually exclusive")
    owns_model_wakeup = False

    descriptor = input_stream.fileno()
    prior_termios = termios.tcgetattr(descriptor)
    controller = BoardController(model)
    color_tier = _color_tier()
    previous_frame: Optional[str] = None
    previous_signature = state_signature(model)
    image_sent = False
    activity_overlays = ()
    activity_image_ids: set[int] = set()
    decoder = TerminalInputDecoder()
    pending_inputs: list[TerminalInput] = []
    screen_entered = False
    cbreak_attempted = False
    mouse_enabled = False
    kitty_keyboard_enabled = False
    resize_wakeup: Optional[_BoardResizeWakeup] = None
    try:
        if read_event is None and model_wakeup is None and model_root is not None:
            model_wakeup = BoardFilesystemWakeup(model_root)
            owns_model_wakeup = True
        screen_entered = True
        output_stream.write("\x1b[?1049h\x1b[?25l")
        output_stream.flush()
        cbreak_attempted = True
        tty.setcbreak(descriptor)
        capability_receipt, remainder = probe_terminal_capabilities(
            input_stream,
            output_stream,
            environment=os.environ,
            preloaded_response=terminal_response,
        )
        pending_inputs.extend(decoder.feed(remainder))
        if capability_receipt_sink is not None:
            capability_receipt_sink(capability_receipt.to_dict())
        image = (
            kitty_buoy_image()
            if color_tier != "mono"
            and capability_receipt.enabled("kitty_graphics")
            else b""
        )
        mouse_enabled = True
        output_stream.write(mouse_tracking(True).decode("ascii"))
        if capability_receipt.enabled("kitty_keyboard"):
            kitty_keyboard_enabled = True
            output_stream.write(kitty_keyboard_mode(True).decode("ascii"))
        output_stream.flush()
        if read_event is None:
            resize_wakeup = _BoardResizeWakeup()
            resize_wakeup.install()
        needs_render = True
        while not controller.quit_requested:
            if needs_render:
                size = shutil.get_terminal_size((120, 40))
                activity = board_activity(model)
                frame = render_frame(
                    model,
                    size.columns,
                    size.lines,
                    selected=controller.selected,
                    color=color_tier != "mono",
                    color_tier=color_tier,
                    animation_progress=1.0,
                    detail_open=controller.detail_open,
                    activity_by_node=activity,
                )
                overlay_plan = plan_activity_overlays(
                    activity_by_target=activity,
                    visible_positions=node_activity_positions(
                        model,
                        size.columns,
                        size.lines,
                        activity,
                    ),
                    capability_receipt=capability_receipt,
                    color_tier=color_tier,
                    previous=activity_overlays,
                )
                if frame != previous_frame or overlay_plan.payload:
                    overlay = (
                        (image if image and not image_sent else b"")
                        + overlay_plan.payload
                    )
                    output_stream.write(
                        synchronized_output_frame(frame, image=overlay).decode("utf-8")
                    )
                    output_stream.flush()
                    image_sent = image_sent or bool(image and not image_sent)
                    activity_image_ids.update(
                        overlay_item.image_id for overlay_item in overlay_plan.overlays
                    )
                    activity_overlays = overlay_plan.overlays
                    previous_frame = frame
                needs_render = False
            event = (
                read_event(None)
                if read_event is not None
                else _read_terminal_input(
                    input_stream,
                    None,
                    decoder,
                    pending_inputs,
                    None if resize_wakeup is None else resize_wakeup.read_descriptor,
                    None if model_wakeup is None else model_wakeup.fileno(),
                )
            )
            if isinstance(event, BoardFilesystemEvent):
                if model_wakeup is None:
                    raise RuntimeError("Board filesystem event has no source")
                model_wakeup.drain()
                event = BoardModelEvent(model_loader())
            if isinstance(event, BoardModelEvent):
                signature = state_signature(event.model)
                if signature != previous_signature:
                    model = event.model
                    previous_signature = signature
                    controller.update_model(model)
                    needs_render = True
                continue
            if isinstance(event, BoardResizeEvent):
                needs_render = True
                continue
            if isinstance(event, MouseEvent):
                action = controller.handle_mouse(
                    event,
                    node_rows=node_row_positions(
                        model, size.columns, size.lines
                    ),
                    viewport_width=size.columns,
                )
            else:
                action = controller.handle_key(event)
            if action.kind == "ack" and ack_callback is not None:
                ack_callback(action)
                refreshed = model_loader()
                signature = state_signature(refreshed)
                if signature != previous_signature:
                    model = refreshed
                    previous_signature = signature
                    controller.update_model(model)
                    needs_render = True
            if action.kind in {"select", "detail"}:
                needs_render = True
        return 0
    finally:
        primary_error = sys.exc_info()[1]
        restore_error: Optional[BaseException] = None
        cleanup_error: Optional[BaseException] = None
        resize_error: Optional[BaseException] = None
        model_wakeup_error: Optional[BaseException] = None
        if resize_wakeup is not None:
            try:
                resize_wakeup.close()
            except BaseException as exc:
                resize_error = exc
        if owns_model_wakeup and model_wakeup is not None:
            try:
                model_wakeup.close()
            except BaseException as exc:
                model_wakeup_error = exc
        if cbreak_attempted:
            try:
                termios.tcsetattr(descriptor, termios.TCSADRAIN, prior_termios)
            except BaseException as exc:
                restore_error = exc
        try:
            if image_sent:
                output_stream.write(kitty_delete_image().decode("ascii"))
            if activity_image_ids:
                output_stream.write(
                    kitty_delete_images(sorted(activity_image_ids)).decode("ascii")
                )
            if kitty_keyboard_enabled:
                output_stream.write(kitty_keyboard_mode(False).decode("ascii"))
            if mouse_enabled:
                output_stream.write(mouse_tracking(False).decode("ascii"))
            if screen_entered:
                output_stream.write("\x1b[?25h\x1b[?1049l")
            output_stream.flush()
        except BaseException as exc:
            cleanup_error = exc
        if primary_error is None:
            if restore_error is not None:
                raise restore_error
            if resize_error is not None:
                raise resize_error
            if model_wakeup_error is not None:
                raise model_wakeup_error
            if cleanup_error is not None:
                raise cleanup_error
