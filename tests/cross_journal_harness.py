"""Crash-injection harness for the cross journal storage layer.

The harness models what each failure leaves, separately for names and bytes:

- every file the journal creates gets a token (its inode in the model);
- `current` maps each name to the token the running system sees now;
- `durable` maps each name to the token its directory held at the last
  directory sync;
- `data` holds each token's bytes at its last file sync.

Process death keeps `current` and the live bytes. A power loss keeps only what
was synced. Because names and bytes are modelled apart, an image can bind the
old checkpoint name to the old inode while the new inode's bytes were synced,
which restoring each file's last synced bytes cannot express.

`TracingStorage` performs every operation for real except the sync itself: the
model, not the disk, decides what a power loss keeps, so the matrix stays fast.
"""

from __future__ import annotations

import base64
import errno
import json
import os
import shutil
import signal
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from floati.cross_journal import CrossJournal as ProductionJournal, Storage
from floati.errors import FloatiError


LOCK_SUFFIX = ".lock"


class CrossJournal(ProductionJournal):
    """Journal fixtures for historical codec and simulated filesystem controls."""

    _fixture_mode = True


class SimulatedCrash(BaseException):
    """Process death inside this process: nothing below catches it."""


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode(text: Optional[str]) -> bytes:
    return b"" if text is None else base64.b64decode(text)


def snapshot(directory: Path) -> Dict[str, bytes]:
    """Every journal-owned file, by name. Lock files carry no state."""

    if not directory.is_dir():
        return {}
    return {
        path.name: path.read_bytes()
        for path in sorted(directory.iterdir())
        if path.is_file() and not path.name.endswith(LOCK_SUFFIX)
    }


def materialize(files: Dict[str, bytes], directory: Path) -> None:
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)
    if "journal.commit" in files:
        # A quiescent restore provisions exclusion before releasing the fixture
        # to readers. The stable empty lock carries no journal coordinates.
        (directory / "journal.lock").touch()


def adopted_model(directory: Path) -> dict:
    """The model of a directory found after a reboot: all of it is durable."""

    model = {"current": {}, "durable": {}, "data": {}, "next": 0}
    for name, data in snapshot(directory).items():
        token = str(model["next"])
        model["next"] += 1
        model["current"][name] = token
        model["durable"][name] = token
        model["data"][token] = _encode(data)
    return model


class TracingStorage(Storage):
    """Real operations, modelled syncs, and a stop at one numbered point.

    Point k is the state after k operations. `stop_at=k` stops the process
    before operation k+1 starts: `stop="kill"` sends SIGKILL to this process,
    `stop="raise"` raises `SimulatedCrash`. `fail_at=k` makes operation k+1
    raise `OSError(EIO)` without running, as a failed sync would.
    """

    def __init__(
        self,
        directory: Path,
        *,
        model: Optional[dict] = None,
        model_path: Optional[Path] = None,
        stop_at: Optional[int] = None,
        stop: str = "raise",
        fail_at: Optional[int] = None,
        stop_after: Optional[Callable[[str], bool]] = None,
        fail_when: Optional[Callable[[str, str], bool]] = None,
        fail_errno: int = errno.EIO,
    ) -> None:
        super().__init__(directory)
        self.model = model if model is not None else adopted_model(Path(directory))
        self.model_path = model_path
        self.stop_at = stop_at
        self.stop = stop
        self.fail_at = fail_at
        self.stop_after = stop_after
        self.fail_when = fail_when
        self.fail_errno = fail_errno
        self.operations = 0
        self.labels: List[str] = []
        self.full_syncs: List[str] = []
        self._source: Optional[str] = None

    def before(self, operation: str, name: str) -> None:
        if self.stop_at is not None and self.operations == self.stop_at:
            if self.stop == "kill":
                os.kill(os.getpid(), signal.SIGKILL)
            raise SimulatedCrash(f"stopped before {operation}:{name}")
        if (self.fail_at is not None and self.operations == self.fail_at) or (
            self.fail_when is not None and self.fail_when(operation, name)
        ):
            self.fail_at = None
            self.fail_when = None
            raise OSError(self.fail_errno, f"injected failure at {operation}:{name}")

    def rename(self, source: str, target: str) -> None:
        self._source = source
        super().rename(source, target)

    def _new_token(self) -> str:
        token = str(self.model["next"])
        self.model["next"] += 1
        return token

    def boundary(self, operation: str, name: str) -> None:
        # Another process may have changed the directory since this model was
        # adopted (a race); a name it never saw gets a token when first touched.
        current = self.model["current"]
        if operation == "append" and name not in current:
            current[name] = self._new_token()
        elif operation == "create":
            current[name] = self._new_token()
        elif operation == "sync":
            token = current.setdefault(name, self._new_token())
            self.model["data"][token] = _encode((self.directory / name).read_bytes())
        elif operation == "sync_directory":
            self.model["durable"] = dict(current)
        elif operation == "rename":
            current[name] = current.pop(self._source, None) or self._new_token()
        elif operation == "unlink":
            current.pop(name, None)
        self.operations += 1
        self.labels.append(f"{operation}:{_stable(name)}")
        if self.stop_after is not None and self.stop_after(self.labels[-1]):
            self.stop_after = None
            self.stop_at = self.operations
        if self.model_path is not None:
            temporary = self.model_path.with_name(self.model_path.name + ".tmp")
            temporary.write_text(json.dumps(self.model))
            os.replace(temporary, self.model_path)

    def _sync_descriptor(self, descriptor: int) -> None:
        self.full_syncs.append(str(descriptor))


def _stable(name: str) -> str:
    stem, marker, _ = name.partition(".tmp-")
    return stem + ".tmp" if marker else name


def power_loss_images(directory: Path, model: dict) -> Dict[str, Dict[str, bytes]]:
    """Each distinct directory a power loss can leave, labelled by its choice.

    The directory either kept its last synced bindings or every later rename.
    Each file's bytes are its last synced bytes, everything written, or the
    synced bytes plus half of what followed (a torn tail).
    """

    inverse = {token: name for name, token in model["current"].items()}
    images: Dict[str, Dict[str, bytes]] = {}
    seen = set()
    for directory_choice in ("durable", "current"):
        for data_choice in ("durable", "current", "torn"):
            files: Dict[str, bytes] = {}
            for name, token in model[directory_choice].items():
                durable = _decode(model["data"].get(token))
                live_name = inverse.get(token)
                live = (directory / live_name).read_bytes() if live_name else durable
                if data_choice == "durable":
                    files[name] = durable
                elif data_choice == "current":
                    files[name] = live
                elif live.startswith(durable) and len(live) > len(durable):
                    files[name] = durable + live[len(durable):len(durable) + (len(live) - len(durable)) // 2]
                else:
                    files[name] = durable
            key = tuple(sorted(files.items()))
            if key not in seen:
                seen.add(key)
                images[f"names={directory_choice},bytes={data_choice}"] = files
    return images


class ReaderProcess:
    """A second process, already running, that reads whenever it is asked."""

    def __init__(self) -> None:
        request_read, self._request = os.pipe()
        self._reply, reply_write = os.pipe()
        self.pid = os.fork()
        if self.pid == 0:  # pragma: no cover - runs in the child
            try:
                os.close(self._request)
                os.close(self._reply)
                requests = os.fdopen(request_read, "r")
                replies = os.fdopen(reply_write, "w")
                for line in requests:
                    replies.write(json.dumps(_read(Path(line.rstrip("\n")))) + "\n")
                    replies.flush()
            finally:
                os._exit(0)
        os.close(request_read)
        os.close(reply_write)
        self._requests = os.fdopen(self._request, "w")
        self._replies = os.fdopen(self._reply, "r")

    def read(self, directory: Path) -> dict:
        self._requests.write(str(directory) + "\n")
        self._requests.flush()
        return json.loads(self._replies.readline())

    def close(self) -> None:
        self._requests.close()
        self._replies.close()
        os.waitpid(self.pid, 0)


def _reply(view) -> dict:
    first = view.records[0] if view.records else None
    link = None
    if first is not None and first.get("kind") == "cross_journal_link":
        link = {
            "previous_journal_id": first["previous_journal_id"],
            "previous_through_seq": first["previous_through_seq"],
        }
    return {
        "ids": [str(record["id"]) for record in view.records],
        "journal_id": view.journal_id,
        "through_seq": view.through_seq,
        "link": link,
        "pending": view.pending,
    }


def _read(directory: Path) -> dict:
    try:
        view = CrossJournal(directory).read_committed()
    except FloatiError as exc:
        return {"error": exc.code}
    return _reply(view)


def committed_ids(directory: Path) -> Tuple[Optional[List[str]], Optional[str]]:
    view, failure = final_view(directory)
    return (None, failure) if view is None else (view["ids"], None)


def count_points(directory: Path, scenario: Callable[[CrossJournal], object]) -> Tuple[int, List[str]]:
    """Run once to completion on a throwaway copy and count its operations."""

    probe = directory.with_name(directory.name + "-probe")
    materialize(snapshot(directory), probe)
    storage = TracingStorage(probe)
    scenario(CrossJournal(probe, storage=storage))
    shutil.rmtree(probe)
    return storage.operations, storage.labels


def kill_at(directory: Path, model: dict, scenario: Callable[[CrossJournal], object], point: int) -> bool:
    """Fork a writer, SIGKILL it before operation `point + 1`, return the model."""

    model_path = directory.with_name(directory.name + ".model.json")
    model_path.write_text(json.dumps(model))
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        try:
            storage = TracingStorage(
                directory, model=model, model_path=model_path, stop_at=point, stop="kill"
            )
            scenario(CrossJournal(directory, storage=storage))
        except BaseException:
            pass
        finally:
            os._exit(0)
    _, status = os.waitpid(pid, 0)
    model.clear()
    model.update(json.loads(model_path.read_text()))
    model_path.unlink()
    return os.WIFSIGNALED(status)


def recover_once(directory: Path) -> Tuple[Optional[Dict[str, bytes]], Optional[str]]:
    try:
        CrossJournal(directory, storage=TracingStorage(directory)).maintain("M")
    except FloatiError as exc:
        return None, exc.code
    except OSError as exc:
        return None, f"OSError:{errno.errorcode.get(exc.errno, exc.errno)}"
    return snapshot(directory), None


def recover_twice(directory: Path) -> Tuple[Optional[Dict[str, bytes]], Optional[str]]:
    """Recover a found directory twice; the second run must change nothing."""

    try:
        CrossJournal(directory, storage=TracingStorage(directory)).maintain("M")
        first = snapshot(directory)
        CrossJournal(directory, storage=TracingStorage(directory)).maintain("M")
    except FloatiError as exc:
        return None, exc.code
    except OSError as exc:
        return None, f"OSError:{errno.errorcode.get(exc.errno, exc.errno)}"
    second = snapshot(directory)
    if first != second:
        return None, "recovery_not_idempotent"
    return second, None


def final_view(directory: Path) -> Tuple[Optional[dict], Optional[str]]:
    """What a reader publishes after recovery, in the reader's own terms."""

    try:
        view = CrossJournal(directory, storage=TracingStorage(directory)).read_committed()
    except FloatiError as exc:
        return None, exc.code
    return _reply(view), None


def lost(observed: dict, final: Optional[dict]) -> List[str]:
    """Records a reader exposed that the final state neither keeps nor retired.

    Compaction retires history on purpose. A missing record is accounted for
    only when the final generation's link proves the exposed prefix was
    committed: it names the exposed journal and covers its sequence.
    """

    final = final or {"ids": [], "link": None}
    kept = set(final["ids"])
    missing = [record_id for record_id in observed.get("ids", []) if record_id not in kept]
    link = final.get("link")
    if (
        missing
        and link is not None
        and link["previous_journal_id"] == observed.get("journal_id")
        and link["previous_through_seq"] >= observed.get("through_seq", 0)
    ):
        return []
    return missing
