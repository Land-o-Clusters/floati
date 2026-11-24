"""Class-owned genuine prefix preludes, with honest per-case restoration.

Only unit fixtures use this helper. Existing ledgers are restored as private
fixture data, never assigned an old ctime. Native warm factories bind caches
to any changed source identity; a genuinely cold initial bank stays cold.
"""
from __future__ import annotations

import copy
from functools import wraps
import os
from pathlib import Path
import shutil
import stat

from floati import cursor_prefix, inbox_prefix, registry_prefix, send_prefix
from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.root import FloatiRoot
from tests.bus_scale_test_profile import full_scale_enabled, register_fixture_cleanup


_BASELINES = {}
_CACHE_NAMES = {".send-prefix", ".registry-prefix", ".inbox-prefix"}
_CACHE_STARTS = (".cursor-prefix-", ".consumed-prefix-", ".receipt-prefix-")


def _cache(name):
    return name in _CACHE_NAMES or name.startswith(_CACHE_STARTS)


def _remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class PrefixBaseline:
    def __init__(self, owner, node):
        self.owner, self.node = owner, node
        self.home = owner.root.tenant_home
        self.entries = {}
        self.sources = {}
        self.revision = send_prefix._revision()
        self.caches = {path.name for path in self.home.iterdir() if _cache(path.name)}
        if any(name.startswith((".consumed-prefix-", ".receipt-prefix-")) for name in self.caches):
            raise ValueError("this prelude requires an additional native warm factory")
        for path in (self.home, *self.home.rglob("*")):
            info = path.lstat()
            directory = stat.S_ISDIR(info.st_mode)
            if (info.st_uid != os.getuid() or not (directory or stat.S_ISREG(info.st_mode))
                    or not directory and info.st_nlink != 1):
                raise ValueError("prefix prelude contains an alias or shared/foreign entry")
            relative = path.relative_to(self.home).as_posix()
            raw = None if directory else path.read_bytes()
            self.entries[relative] = (directory, stat.S_IMODE(info.st_mode), info.st_mtime_ns, raw)
            if not directory and relative.endswith(".jsonl") and not _cache(relative.split("/", 1)[0]):
                self.sources[relative] = (_identity(info), raw)
        self.values = {name: copy.deepcopy(value) for name, value in vars(owner).items()
                       if not name.startswith("_") and name not in {"root", "log", "events", "cursor"}}
        self.has_log, self.has_events = hasattr(owner, "log"), hasattr(owner, "events")
        self.has_cursor = hasattr(owner, "cursor")

    def close(self):
        self.owner.doCleanups()

    def _plain_source(self, relative):
        path = self.home / relative
        for parent in (path, *path.parents):
            info = parent.lstat()
            if info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode):
                return None
            if parent == self.home:
                break
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        return _identity(info), path.read_bytes()

    def _drop_extras(self, *, warmed=False):
        for path in sorted(self.home.rglob("*"), key=lambda value: len(value.parts), reverse=True):
            relative = path.relative_to(self.home).as_posix()
            if warmed and relative.split("/", 1)[0] in self.caches:
                continue
            expected = self.entries.get(relative)
            info = path.lstat()
            directory = stat.S_ISDIR(info.st_mode)
            if (expected is None or directory != expected[0] or stat.S_ISLNK(info.st_mode)
                    or not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
                _remove(path)

    def restore(self):
        try:
            info = self.home.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or self.home.resolve(strict=True) != self.home):
                return False
            changed = send_prefix._revision() != self.revision
            changed = changed or any(self._plain_source(relative) != value
                                     for relative, value in self.sources.items())
            self._drop_extras()
            for relative, (directory, mode, _mtime, raw) in sorted(
                    self.entries.items(), key=lambda item: len(Path(item[0]).parts)):
                path = self.home if relative == "." else self.home / relative
                if directory:
                    path.mkdir(exist_ok=True)
                else:
                    if path.exists() and stat.S_IMODE(path.lstat().st_mode) != mode:
                        path.chmod(mode | 0o200)
                    if not path.exists() or path.read_bytes() != raw:
                        path.write_bytes(raw)
                if stat.S_IMODE(path.lstat().st_mode) != mode:
                    path.chmod(mode)
            self._restore_mtimes()
            if changed:
                # No copied seal is evidence about a changed source inode or
                # ctime. Recreate only the originally present bank families.
                for name in self.caches:
                    _remove(self.home / name)
                root = FloatiRoot.open_direct_home(self.home)
                for name, warm in ((".send-prefix", send_prefix.warm),
                                   (".registry-prefix", registry_prefix.warm),
                                   (".inbox-prefix", inbox_prefix.warm)):
                    if name in self.caches and warm(root) is None:
                        return False
                if any(name.startswith(".cursor-prefix-") for name in self.caches):
                    if cursor_prefix.warm(root, self.node) is None:
                        return False
                # Warming may make absent-source locks and a cold ACK bank.
                # Drop those new private auxiliaries after all guards release.
                self._drop_extras(warmed=True)
                self._restore_mtimes(include_caches=False)
            for relative, (directory, mode, _mtime, raw) in self.entries.items():
                if not directory and not _cache(relative.split("/", 1)[0]):
                    path = self.home / relative
                    if path.read_bytes() != raw or stat.S_IMODE(path.stat().st_mode) != mode:
                        return False
            return True
        except (OSError, ValueError):
            return False

    def _restore_mtimes(self, *, include_caches=True):
        for relative, (_directory, _mode, mtime, _raw) in sorted(
                self.entries.items(), key=lambda item: len(Path(item[0]).parts), reverse=True):
            if not include_caches and _cache(relative.split("/", 1)[0]):
                continue
            path = self.home if relative == "." else self.home / relative
            info = path.stat()
            if info.st_mtime_ns != mtime:
                os.utime(path, ns=(info.st_atime_ns, mtime))

    def assign(self, test):
        test.root = FloatiRoot.open_direct_home(self.home)
        if self.has_log:
            test.log = EventLog(test.root)
        if self.has_events:
            test.events = EventLog(test.root)
        if self.has_cursor:
            test.cursor = SparseCursor(test.root)
        for name, value in self.values.items():
            setattr(test, name, copy.deepcopy(value))


def shared_prefix_baseline(*, node="recipient"):
    def decorate(prepare):
        @wraps(prepare)
        def set_up(test):
            if full_scale_enabled():
                return prepare(test)
            cls = type(test)
            baseline = _BASELINES.get(cls)
            if baseline is not None and not baseline.restore():
                baseline.close()
                _BASELINES.pop(cls, None)
                baseline = None
            if baseline is None:
                owner = cls()
                try:
                    prepare(owner)
                    baseline = PrefixBaseline(owner, node)
                except BaseException:
                    owner.doCleanups()
                    raise
                _BASELINES[cls] = baseline
                def close():
                    fixture = _BASELINES.pop(cls, None)
                    if fixture is not None:
                        fixture.close()
                cls.addClassCleanup(close)
                register_fixture_cleanup(cls, close)
            baseline.assign(test)
        return set_up
    return decorate
