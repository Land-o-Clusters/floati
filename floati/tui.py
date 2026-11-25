"""Dependency-free keyboard-first terminal loop for the harbor board."""

from __future__ import annotations

import ctypes
import errno
import os
import re
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
from .tui_ask import ASK_HINTS, AskCallback, BoardAskPanel, BoardAskTask
from .tui_graphics import kitty_delete_images, plan_activity_overlays
from .consumption import ConsumptionLedger
from .copy import (
    BOARD_EVENT_ROOT_UNAVAILABLE_DETAIL,
    BOARD_EVENT_SOURCE_UNSUPPORTED_DETAIL,
    BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL,
)
from .errors import FloatiError, ProtocolRefusal, SnapshotRefusal
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


_ASK_READY = object()


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
            if not (path := current_path / name).is_symlink() and not _host_probe_path(root, path)
        )
    return tuple(paths)


def _host_observer_directory(root: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
        return len(parts) == 3 and parts[:2] == ("state", "host-observations")
    except (ValueError, TypeError):
        return False


def _host_probe_path(root: Path, path: Path) -> bool:
    return _host_observer_directory(root, path.parent) and re.fullmatch(
        r"\.floati-host-probe-[0-9a-f]{32}", path.name) is not None


def _observer_directory_snapshot(root: Path, path: Path):
    """Compare meaningful entries/inodes, excluding only owned UUID probes.

    Directory mtime/ctime changes on every transient probe and carry no input
    fact. Real entry metadata, including latest.json's identity, stays bound.
    """
    info = path.lstat()
    entries = []
    with os.scandir(path) as children:
        for index, entry in enumerate(children):
            if index >= 1024:
                raise OSError(errno.EOVERFLOW, "observer watch entry bound exceeded")
            probe = _host_probe_path(root, path / entry.name)
            if probe and entry.is_file(follow_symlinks=False):
                continue
            try:
                value = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                if probe:
                    continue  # A completed transient probe may vanish during the scan.
                raise
            entries.append((entry.name, value.st_dev, value.st_ino, value.st_mode,
                value.st_uid, value.st_gid, value.st_size, value.st_mtime_ns, value.st_ctime_ns))
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, tuple(sorted(entries)))


def _selected_watch_paths(root: Path, paths: tuple[Path, ...]) -> tuple[Path, ...]:
    """Watch named inputs and their existing parents, never discover a tree."""
    selected = {root}
    for target in paths:
        target.relative_to(root)
        chain = []
        while target != root:
            chain.append(target)
            target = target.parent
        for path in reversed(chain):
            if path.is_symlink():
                break
            if path.exists():
                selected.add(path)
    return tuple(selected)


def _open_selected_watch_path(root: Path, path: Path) -> int:
    """Bind an input watch without following a substituted path component."""
    import stat

    path.relative_to(root)
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    descriptor = None
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(path.name,
                             getattr(os, "O_EVTONLY", os.O_RDONLY) | os.O_NONBLOCK | os.O_NOFOLLOW,
                             dir_fd=parent)
        opened = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (not (stat.S_ISREG(opened.st_mode) or stat.S_ISDIR(opened.st_mode))
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
            raise OSError(errno.ESTALE, "selected watch path changed")
        result, descriptor = descriptor, None
        return result
    finally:
        os.close(parent)
        if descriptor is not None:
            os.close(descriptor)


class _KqueueBoardFilesystemWakeup:
    def __init__(self, root: Path, *, paths=None) -> None:
        self._root = root
        self._paths = paths
        self._queue = select.kqueue()
        self._descriptors: dict[Path, int] = {}
        self._closed = False
        self.coverage_lost = False
        self._observer_snapshots = {}
        try:
            self._refresh()
        except BaseException:
            self.close()
            raise

    def fileno(self) -> int:
        return self._queue.fileno()

    def _refresh(self) -> None:
        current = set(_board_watch_paths(self._root) if self._paths is None
                      else _selected_watch_paths(self._root, self._paths))
        if self._root not in current:
            raise ProtocolRefusal(
                "board_event_watch_unavailable",
                BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL,
            )
        for path in tuple(self._descriptors):
            if path not in current:
                self.coverage_lost = True
                os.close(self._descriptors.pop(path))
            elif self._paths is not None:
                try:
                    current_descriptor = _open_selected_watch_path(self._root, path)
                    try:
                        named = os.fstat(current_descriptor)
                        watched = os.fstat(self._descriptors[path])
                        if (named.st_dev, named.st_ino) != (watched.st_dev, watched.st_ino):
                            self.coverage_lost = True
                            os.close(self._descriptors.pop(path))
                    finally:
                        os.close(current_descriptor)
                except OSError as exc:
                    raise ProtocolRefusal("board_event_watch_unavailable",
                                          BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL) from exc
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
                descriptor = (os.open(path, getattr(os, "O_EVTONLY", os.O_RDONLY) | os.O_NONBLOCK)
                              if self._paths is None else _open_selected_watch_path(self._root, path))
            except FileNotFoundError:
                if self._paths is not None:
                    self.coverage_lost = True
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
        self._observer_snapshots = {
            path: _observer_directory_snapshot(self._root, path)
            for path in self._descriptors
            if _host_observer_directory(self._root, path) and path.is_dir()
        }

    def drain(self) -> bool:
        prior_observers = dict(getattr(self, "_observer_snapshots", {}))
        try:
            events = self._queue.control([], max(1, len(self._descriptors)), 0)
        except OSError:
            self.coverage_lost = True
            raise
        paths_by_descriptor = {descriptor: path for path, descriptor in self._descriptors.items()}
        changed = False
        invalid = {
            event.ident
            for event in events
            if event.fflags
            & (select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE | select.KQ_NOTE_REVOKE)
        }
        if invalid or any(getattr(event, "flags", 0) & select.KQ_EV_ERROR for event in events):
            self.coverage_lost = True
            changed = True
        for event in events:
            path = paths_by_descriptor.get(event.ident)
            if path is None:
                self.coverage_lost = True
                changed = True
            elif self._paths is None:
                if _host_observer_directory(self._root, path):
                    try:
                        previous = getattr(self, "_observer_snapshots", {}).get(path)
                        if previous != _observer_directory_snapshot(self._root, path):
                            changed = True
                    except OSError:
                        self.coverage_lost = True
                        changed = True
                else:
                    changed = True
            elif path in self._paths:
                changed = True
        for path, descriptor in tuple(self._descriptors.items()):
            if descriptor in invalid:
                os.close(descriptor)
                del self._descriptors[path]
        try:
            self._refresh()
        except (OSError, ProtocolRefusal):
            self.coverage_lost = True
            raise
        if self._paths is None and getattr(self, "_observer_snapshots", {}) != prior_observers:
            changed = True
        return changed

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
        | 0x00002000
    )

    def __init__(self, root: Path, *, paths=None) -> None:
        self._root = root
        self._paths = paths
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_init1.restype = ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        if self._paths is not None:
            self._libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
            self._libc.inotify_rm_watch.restype = ctypes.c_int
        self._descriptor = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._descriptor < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._watches: dict[Path, int] = {}
        self._identities: dict[Path, tuple[int, int]] = {}
        self._closed = False
        self.coverage_lost = False
        try:
            self._refresh()
        except BaseException:
            os.close(self._descriptor)
            raise

    def fileno(self) -> int:
        return self._descriptor

    def _refresh(self) -> None:
        current = set(_board_watch_directories(self._root) if self._paths is None
                      else (path for path in _selected_watch_paths(self._root, self._paths)
                            if path.is_dir()))
        if self._root not in current:
            raise ProtocolRefusal(
                "board_event_watch_unavailable",
                BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL,
            )
        watched = set(self._watches)
        for path in watched - current:
            self.coverage_lost = True
            if self._paths is not None:
                self._libc.inotify_rm_watch(self._descriptor, self._watches[path])
                self._identities.pop(path, None)
            del self._watches[path]
        # Reapply every watch: inotify binds to an inode, so a directory that
        # is deleted and recreated at the same path invalidates the old watch
        # even though the path remains present in our mapping.
        for path in sorted(current):
            selected_descriptor = None
            try:
                if self._paths is not None:
                    selected_descriptor = _open_selected_watch_path(self._root, path)
                    value = os.fstat(selected_descriptor)
                    identity = (value.st_dev, value.st_ino)
                    if path in self._watches and self._identities.get(path) != identity:
                        self.coverage_lost = True
                        self._libc.inotify_rm_watch(self._descriptor, self._watches.pop(path))
                    self._identities[path] = identity
                watch = self._libc.inotify_add_watch(
                    self._descriptor,
                    # Linux resolves this owned descriptor to the already
                    # opened directory, even if a parent path is replaced.
                    os.fsencode(path if selected_descriptor is None
                                else "/proc/self/fd/" + str(selected_descriptor)),
                    # A writable read-transaction lock close is not a change.
                    self._MASK if self._paths is None else self._MASK & ~0x00000008,
                )
            except FileNotFoundError:
                if self._paths is not None:
                    self.coverage_lost = True
                continue
            except OSError as exc:
                raise ProtocolRefusal("board_event_watch_unavailable",
                                      BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL) from exc
            finally:
                if selected_descriptor is not None:
                    os.close(selected_descriptor)
            if watch < 0:
                error = ctypes.get_errno()
                if error == errno.ENOENT:
                    if self._paths is not None:
                        self.coverage_lost = True
                    continue
                raise ProtocolRefusal(
                    "board_event_watch_unavailable",
                    BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL,
                ) from OSError(error, "inotify_add_watch failed")
            self._watches[path] = watch

    def drain(self) -> bool:
        import struct
        changed = False
        paths_by_watch = {watch: path for path, watch in self._watches.items()}
        while True:
            try:
                data = os.read(self._descriptor, 65536)
                if not data:
                    break
            except BlockingIOError:
                break
            except OSError:
                self.coverage_lost = True
                raise
            offset = 0
            while offset < len(data):
                if len(data) - offset < 16:
                    self.coverage_lost = True
                    changed = True
                    break
                watch, mask, _cookie, length = struct.unpack_from("iIII", data, offset)
                if length > len(data) - offset - 16:
                    self.coverage_lost = True
                    changed = True
                    break
                name = os.fsdecode(data[offset + 16:offset + 16 + length].split(b"\0", 1)[0])
                parent = paths_by_watch.get(watch)
                if mask & (0x00004000 | 0x00008000 | 0x00002000 | 0x00000400 | 0x00000800) or parent is None:
                    self.coverage_lost = True
                    changed = True  # Overflow/lost watch never certifies quiet.
                elif self._paths is None and (
                        mask & 0x40000000 or not _host_probe_path(self._root, parent / name)):
                    changed = True
                elif self._paths is not None and parent / name in self._paths:
                    changed = True
                offset += 16 + length
            if self._paths is not None:
                break  # One bounded read; an event flood cannot hold a waiter.
        try:
            self._refresh()
        except (OSError, ProtocolRefusal):
            self.coverage_lost = True
            raise
        return changed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._descriptor)
        self._watches.clear()


class BoardFilesystemWakeup:
    """Selectable, timer-free durable-root change source for the live Board."""

    def __init__(self, root: Path, *, _paths=None) -> None:
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
                self._backend = _KqueueBoardFilesystemWakeup(resolved, paths=_paths)
            elif sys.platform.startswith("linux"):
                self._backend = _InotifyBoardFilesystemWakeup(resolved, paths=_paths)
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

    def drain(self) -> bool:
        return self._backend.drain()

    @property
    def coverage_lost(self) -> bool:
        return self._backend.coverage_lost

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
        model.fleet_presence,
    ))


def _with_car_execution(root, model, current):
    from .seat_execution import SeatExecutionController
    from .wake_health import WakeHealthProjection
    controller = SeatExecutionController(root)
    health = WakeHealthProjection(root)
    return replace(model, nodes=tuple(dict(node, car_execution=controller.project(node["node_id"]),
        stop_waiting=health.stop_waiting(node["node_id"], current))
        for node in model.nodes))


def _with_project_holds(root, model, current):
    from .holds import hold_projection
    from .fleet_presence import (AWAY_LINE, needs_you_return_reference, presence_line,
                                 presence_status, presence_status_line)
    from .inbound_hold import with_inbound_hold
    presence = presence_status(root)
    mode = presence["mode"]
    banner = AWAY_LINE if mode == "AWAY" else presence_status_line(presence)
    needs_you = needs_you_return_reference(root) if mode == "PRESENT" and presence["away_since"] else ""
    return replace(model,
        fleet_presence=banner if mode != "UNDECLARED" else "",
        nodes=tuple(with_inbound_hold(root, dict(node, project_holds=hold_projection(root, node['node_id'], current),
                                                 fleet_presence=presence_line(root, node['node_id'], presence=presence,
                                                                              needs_you=needs_you)), current)
                    for node in model.nodes))


def _with_roster(root, model, current):
    from .roster import project_roster

    rows = {str(row["node_id"]): row for row in project_roster(root, current)}
    return replace(
        model,
        nodes=tuple(
            dict(node, roster=rows.get(str(node.get("node_id")), {}))
            for node in model.nodes
        ),
    )


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
        model = _board_from_snapshot(root, current, loaded.payload, loaded.tails)
        return _with_roster(root, _with_project_holds(root, _with_car_execution(root, _with_disk_watch(root, model), current), current), current)
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
            "message_progress_observation_version": FleetProjection.MESSAGE_PROGRESS_OBSERVATION_VERSION,
            "work_states": ConsumptionLedger(root).project(),
            "built_at": FleetProjection._timestamp(current),
            "time_sensitive": FleetProjection(root)._status_time_sensitive(),
        }
        if store is not None and before_scan is not None:
            try:
                store.refresh(payload, expected=before_scan)
            except SnapshotRefusal:
                pass
        return _with_roster(root, _with_project_holds(root, _with_car_execution(root, _with_disk_watch(root, model), current), current), current)


def _model_from_root_full(root: FloatiRoot, current: datetime) -> HarborBoardModel:
    snapshot = FleetProjection(root).snapshot(current)
    snapshot["effects"] = EffectStatusProjection(root).summary()
    from .events import EVENT_KINDS

    frames = read_records_snapshot(root, "events.jsonl", allowed_kinds=set(EVENT_KINDS))
    retracted = {row["retracted_message_id"] for row in frames if row["kind"] == "message_retracted"}
    from .inbound_hold import mask_held_envelopes
    events = mask_held_envelopes(root, [row for row in frames if row["kind"] == "message_envelope"])
    deliveries = []
    acknowledgments = []
    acked = set()
    from .records import WAKE_HOLD_KINDS

    for node in snapshot["nodes"]:
        node_id = str(node["node_id"])
        node_acks = read_records_snapshot(
            root, Path("receipts/acks") / f"{node_id}.jsonl", allowed_kinds={"ack_receipt"}
        )
        node_deliveries = read_records_snapshot(
            root, Path("receipts/deliveries") / f"{node_id}.jsonl", allowed_kinds=set(WAKE_HOLD_KINDS)
        )
        acknowledgments.extend(node_acks)
        deliveries.extend(
            row for row in node_deliveries
            if row["kind"] in ("delivery_receipt", "delivered_unpresented")
        )
        acked.update(str(item_id) for record in node_acks for item_id in record["item_ids"])
    nodes = []
    presented = {item for row in deliveries if row["kind"] == "delivery_receipt" for item in row["item_ids"]}
    for source in snapshot["nodes"]:
        node = dict(source)
        node["pending_at_pause"] = sum(1 for event in events
            if event["recipient"] == node["node_id"] and event.get("message_class") == "fyi"
            and event["id"] not in retracted and event["id"] not in acked and event["id"] not in presented)
        node["visible_message_id"] = next(
            (
                str(event["id"]) for event in events
                if event["recipient"] == node["node_id"] and event["id"] not in acked
                and "inbound_hold" not in event
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


def _with_disk_watch(root: FloatiRoot, model: HarborBoardModel) -> HarborBoardModel:
    from .disk_watch import observe_disk_watch

    observation = observe_disk_watch(root, statvfs=os.statvfs)
    if int(observation["exit"]) == 0:
        return model
    effects = dict(model.effects)
    effects["disk_watch"] = observation
    return replace(model, effects=effects)


def _board_from_snapshot(
    root: FloatiRoot,
    current: datetime,
    payload: dict[str, object],
    tails: dict[str, tuple[dict[str, object], ...]],
) -> HarborBoardModel:
    if set(payload) != {"model", "work_states", "built_at", "time_sensitive", "message_progress_observation_version"}:
        raise SnapshotRefusal(
            "snapshot_payload_invalid", "board snapshot fields are invalid"
        )
    raw_model = payload["model"]
    work_states = payload["work_states"]
    built_at = payload["built_at"]
    time_sensitive = payload["time_sensitive"]
    if (
        type(payload["message_progress_observation_version"]) is not int
        or payload["message_progress_observation_version"] != FleetProjection.MESSAGE_PROGRESS_OBSERVATION_VERSION
        or not isinstance(raw_model, dict)
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
    if any("seat_attention" not in node
           or type(node.get("pending_at_pause")) is not int
           or node["pending_at_pause"] < 0
           or node.get("lifetime") not in {"permanent", "temporary"}
           or not FleetProjection._valid_message_progress(node.get("message_progress"))
           for node in model.nodes):
        raise SnapshotRefusal(
            "snapshot_payload_invalid",
            "board snapshot predates current receipt progress testimony",
        )
    unsupported = set()
    work_tail = ()
    for path, records in tails.items():
        if not records:
            continue
        if path == ConsumptionLedger.relative_path.as_posix():
            work_tail = records
        elif path == "events.jsonl":
            if any("seat_attention" in node for node in model.nodes):
                raise SnapshotRefusal(
                    "snapshot_tail_history_required",
                    "seat attention event tail needs full projection history",
                )
            nodes = [dict(row) for row in model.nodes]
            by_id = {str(row["node_id"]): row for row in nodes}
            for record in records:
                if record.get("kind") in {
                    "delivery_claim", "ledger_repair_receipt", "send_readiness_observation",
                    "seat_teardown_epoch_verification",
                    "ask_question", "ask_presentation", "ask_answer", "ask_decline", "ask_timeout",
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
                    if record.get("message_class") == "fyi":
                        recipient["pending_at_pause"] += 1
                    if recipient.get("visible_message_id") is None:
                        recipient["visible_message_id"] = record["id"]
            model = replace(model, nodes=tuple(nodes))
        elif path == "receipts/denials.jsonl":
            model = replace(
                model,
                denials=model.denials + tuple(dict(record) for record in records),
            )
        elif path.startswith(("receipts/deliveries/", "receipts/acks/", "receipts/consumed/")):
            raise SnapshotRefusal(
                "snapshot_tail_history_required",
                "message progress tail needs validated receipt history",
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
    ask_descriptor: Optional[int] = None,
    prefer_input: bool = False,
) -> object:
    if pending:
        return pending.pop(0)
    descriptor = stream.fileno()
    descriptors = [descriptor]
    if resize_descriptor is not None:
        descriptors.append(resize_descriptor)
    if model_descriptor is not None:
        descriptors.append(model_descriptor)
    if ask_descriptor is not None:
        descriptors.append(ask_descriptor)
    readable, _, _ = select.select(descriptors, [], [], timeout)
    if not readable:
        return ""
    if ask_descriptor in readable:
        return _ASK_READY
    if resize_descriptor is not None and resize_descriptor in readable:
        try:
            os.read(resize_descriptor, 4096)
        except BlockingIOError:
            pass
        return BoardResizeEvent()
    if model_descriptor in readable and (not prefer_input or descriptor not in readable):
        return BoardFilesystemEvent()
    pending.extend(decoder.feed(os.read(descriptor, 64)))
    return "" if not pending else pending.pop(0)


def _seat_attention_timeout(model: HarborBoardModel, now: datetime) -> Optional[float]:
    """Wake at ruled thresholds or active holder checks; idle boards block."""
    deadlines = [attention.get("next_check_at") for node in model.nodes
                 if isinstance(attention := node.get("seat_attention"), Mapping)]
    remaining = [(datetime.fromisoformat(value.replace("Z", "+00:00")) - now).total_seconds()
                 for value in deadlines if isinstance(value, str)]
    for node in model.nodes:
        waiting = node.get("stop_waiting")
        if isinstance(waiting, Mapping) and waiting.get("state") == "STOP_WAITING":
            remaining.append(1.0)  # Physical death need not produce a filesystem event.
            for field in ("next_check_at", "hold_deadline_at"):
                value = waiting.get(field)
                if isinstance(value, str):
                    remaining.append((datetime.fromisoformat(value.replace("Z", "+00:00")) - now).total_seconds())
    if any(node.get('project_holds', {}).get('holds') for node in model.nodes):
        remaining.append(1.0)  # Refresh measured age and expected-release status.
    return max(0.0, min(remaining)) if remaining else None


def run_board(
    *,
    model_loader: Callable[[], HarborBoardModel],
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    ack_callback: Optional[Callable[[BoardAction], None]] = None,
    ask_callback: Optional[AskCallback] = None,
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
    ask_panel: Optional[BoardAskPanel] = None
    ask_task: Optional[BoardAskTask] = None
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
                    hints=ASK_HINTS if ask_callback is not None and model.nodes else None,
                )
                if ask_panel is not None:
                    frame = ask_panel.render(frame, size.columns, size.lines)
                overlay_plan = plan_activity_overlays(
                    activity_by_target=activity,
                    visible_positions={} if ask_panel is not None else node_activity_positions(
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
            if ask_panel is not None and ask_panel.submitted and ask_panel.result is None and ask_task is None:
                assert ask_callback is not None
                ask_task = BoardAskTask(ask_callback, ask_panel.recipient, ask_panel.fields)
            timeout = _seat_attention_timeout(model, datetime.now(timezone.utc))
            if ask_task is not None and ask_task.future.done():
                event = _ASK_READY
            elif read_event is not None:
                event = read_event(timeout)
            else:
                event = _read_terminal_input(
                    input_stream,
                    timeout,
                    decoder,
                    pending_inputs,
                    None if resize_wakeup is None else resize_wakeup.read_descriptor,
                    None if model_wakeup is None else model_wakeup.fileno(),
                    None if ask_task is None else ask_task.descriptor,
                    ask_panel is not None,
                )
            if event is _ASK_READY:
                assert ask_task is not None and ask_panel is not None
                try:
                    ask_panel.result = ask_task.future.result()
                except FloatiError as exc:
                    ask_panel.result = {
                        "outcome": "refused" if isinstance(exc, ProtocolRefusal) else "UNKNOWN",
                        "code": exc.code,
                        "detail": exc.detail,
                    }
                finally:
                    finished, ask_task = ask_task, None
                    finished.close()
                needs_render = True
                continue
            if event == "" and timeout is not None:
                event = BoardModelEvent(model_loader())
            if isinstance(event, BoardFilesystemEvent):
                if model_wakeup is None:
                    raise RuntimeError("Board filesystem event has no source")
                if model_wakeup.drain() is False:
                    continue
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
            if ask_panel is not None:
                if ask_task is not None and event == "q":
                    controller.handle_key("q")
                    continue
                if isinstance(event, str):
                    action = ask_panel.handle_key(event)
                    if action == "close":
                        ask_panel = None
                        if ask_task is not None:
                            stopped, ask_task = ask_task, None
                            stopped.close()
                    needs_render = action != "none"
                continue
            if event == "?" and ask_callback is not None and model.nodes:
                ask_panel = BoardAskPanel(str(model.nodes[controller.selected]["node_id"]))
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
        ask_task_error: Optional[BaseException] = None
        if ask_task is not None:
            try:
                ask_task.close()
            except BaseException as exc:
                ask_task_error = exc
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
            if ask_task_error is not None:
                raise ask_task_error
            if cleanup_error is not None:
                raise cleanup_error
