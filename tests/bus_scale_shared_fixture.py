"""Retain native effect fixtures without rewriting authoritative source files.

Only disposable cache trees may be restored. Any changed source identity or
byte invalidates reuse; in particular, ctime is never synthesized or ignored.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import stat


class RetainedNativeFixture:
    def __init__(self, home, cache_names, observation, state, temporary):
        self.home = Path(home)
        self.cache_names = frozenset(cache_names)
        self.observation = copy.deepcopy(observation)
        self.state = copy.deepcopy(state)
        self.temporary = temporary
        self.sources = self._sources()
        self.caches = self._caches()

    def _is_cache(self, relative):
        return relative.split("/", 1)[0] in self.cache_names

    def _ordinary(self, path):
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        if (info.st_uid != os.getuid()
                or not (directory or stat.S_ISREG(info.st_mode))
                or not directory and info.st_nlink != 1):
            raise ValueError("retained test fixture contains an alias or foreign/shared object")
        return info, directory

    def _sources(self):
        values = {}
        for path in self.home.rglob("*"):
            relative = path.relative_to(self.home).as_posix()
            if self._is_cache(relative):
                continue
            info, directory = self._ordinary(path)
            if not directory:
                values[relative] = ((info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns), path.read_bytes())
        return values

    def _caches(self):
        values = {}
        for name in sorted(self.cache_names):
            root = self.home / name
            if not root.exists() and not root.is_symlink():
                continue
            for path in (root, *root.rglob("*")):
                info, directory = self._ordinary(path)
                values[path.relative_to(self.home).as_posix()] = {
                    "directory": directory, "mode": stat.S_IMODE(info.st_mode),
                    "mtime_ns": info.st_mtime_ns,
                    "raw": None if directory else path.read_bytes(),
                }
        return values

    def restore(self):
        """Restore only cache state, or refuse reuse without touching sources."""
        try:
            if self._sources() != self.sources:
                return False
            current = self._caches()
            for relative in sorted(current, key=lambda name: (name.count("/"), name), reverse=True):
                if relative not in self.caches or current[relative]["directory"] != self.caches[relative]["directory"]:
                    path = self.home / relative
                    path.rmdir() if current[relative]["directory"] else path.unlink()
            for relative in sorted(self.caches, key=lambda name: (name.count("/"), name)):
                value = self.caches[relative]
                path = self.home / relative
                if value["directory"]:
                    path.mkdir(exist_ok=True)
                elif not path.exists() or path.read_bytes() != value["raw"]:
                    path.write_bytes(value["raw"])
                if stat.S_IMODE(path.lstat().st_mode) != value["mode"]:
                    path.chmod(value["mode"])
            # Creating/removing children changes directory mtimes. Restore
            # these last, from leaves to roots; source files remain untouched.
            for relative in sorted(self.caches, key=lambda name: (name.count("/"), name), reverse=True):
                path = self.home / relative
                expected = self.caches[relative]["mtime_ns"]
                info = path.lstat()
                if info.st_mtime_ns != expected:
                    os.utime(path, ns=(info.st_atime_ns, expected))
            return self._sources() == self.sources and self._caches() == self.caches
        except (OSError, ValueError):
            return False

    def close(self):
        self.temporary.cleanup()
