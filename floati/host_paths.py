"""Host-derived paths that durable Floati records must never pin."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Optional, Union

from .errors import ProtocolRefusal


#: Moves the fixed roots Floati makes directly under the native temporary root
#: (worker workspaces, sequencer endpoints and FCD 20 scratch) beneath
#: one directory the caller owns. Unset, every default below is unchanged. A
#: test process sets it so that it never writes where a real fleet on the same
#: host lives; every child it spawns inherits the same value through the
#: environment, so a server and a client still derive one endpoint.
HOST_TEMP_ROOT_VARIABLE = "FLOATI_HOST_TEMP_ROOT"


def _invalid(detail: str) -> ProtocolRefusal:
    return ProtocolRefusal("host_temp_root_invalid", f"{HOST_TEMP_ROOT_VARIABLE} {detail}")


def host_temp_root_override() -> Optional[Path]:
    """Return the validated override root, or None when the variable is unset.

    A set value is a product input, never a hint: a malformed, missing,
    symlinked, foreign or shared-writable value refuses. It never falls back
    to the native default, which would put a test's roots back beside a real
    fleet's without saying so.
    """

    value = os.environ.get(HOST_TEMP_ROOT_VARIABLE)
    if value is None:
        return None
    if not value or not os.path.isabs(value) or os.path.normpath(value) != value:
        raise _invalid("must be an absolute normalized path")
    candidate = Path(value)
    try:
        # Check every component; resolving a symlink and using its target
        # would let the root move after it was validated.
        for component in (*reversed(candidate.parents), candidate):
            if not stat.S_ISDIR(component.lstat().st_mode):
                raise _invalid("must have only ordinary directory components")
        metadata = candidate.lstat()
    except OSError as exc:
        raise _invalid("must name an existing ordinary directory") from exc
    if metadata.st_uid != os.getuid():
        raise _invalid("must be owned by the current uid")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise _invalid("must not be writable by group or others")
    return candidate


def capture_temporary_parent() -> Path:
    """Return the platform's real system temporary root for public captures."""

    if sys.platform == "darwin":
        return Path("\x2fprivate/tmp")
    return Path("\x2ftmp")


def native_worker_workspace_root() -> Path:
    """Return the platform's native worker root, ignoring any override.

    Published contract text (command examples) names this coordinate, so it
    reads the same whether or not a test moved its own roots.
    """

    if sys.platform == "darwin":
        return Path("\x2fprivate/tmp/floati-work")
    return Path("\x2ftmp/floati-work")


def worker_workspace_root() -> Path:
    """Return the platform's ruled root for ephemeral worker workspaces."""

    override = host_temp_root_override()
    if override is not None:
        return override / "floati-work"
    return native_worker_workspace_root()


class RefusedHostPath:
    """Stands in for a host root whose override refused while a module loaded.

    A module that binds the root at import must not turn a bad value into an
    import traceback for every verb, and must not fall back either: each use
    of this placeholder raises the original typed refusal instead.
    """

    def __init__(self, refusal: ProtocolRefusal) -> None:
        self._refusal = refusal

    def _refuse(self, *_args: object, **_kwargs: object) -> None:
        raise self._refusal.with_traceback(None)

    def __getattr__(self, name: str) -> object:
        if name.startswith("__") or name == "_refusal":
            raise AttributeError(name)
        self._refuse()

    __truediv__ = __rtruediv__ = __fspath__ = __str__ = _refuse

    def __repr__(self) -> str:
        return f"RefusedHostPath({self._refusal.code})"


def deferred_worker_workspace_root() -> Union[Path, RefusedHostPath]:
    """Return the worker root for import-time binding, deferring a refusal to use."""

    try:
        return worker_workspace_root()
    except ProtocolRefusal as refusal:
        return RefusedHostPath(refusal)


def fcd20_scratch_parent() -> Path:
    """Return the host temporary parent for throwaway FCD 20 evidence."""

    override = host_temp_root_override()
    parent = capture_temporary_parent() if override is None else override
    return parent / "floati-fcd20"


def python_executable() -> Path:
    """Return the explicit interpreter path already running this process."""

    return Path(sys.executable)
