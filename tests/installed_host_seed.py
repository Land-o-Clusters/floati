"""Keep a lazily built installed-host fixture at its original coordinates."""

from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from tests.git_fixture_cache import _clone
from tests.temp_roots import REAL_TEMP_ROOT


_seeds = {}


def restore(key, build):
    """Return the active base and the immutable value produced by ``build``.

    Callers clean up the active base after each test. The process owns the
    snapshot, which preserves absolute references without sharing writable bytes.
    """
    if key not in _seeds:
        storage = TemporaryDirectory(prefix="installed-host-", dir=REAL_TEMP_ROOT)
        base = Path(storage.name) / "active"
        try:
            base.mkdir()
            value = build(base)
            _clone(base, base.with_name("snapshot"))
        except BaseException:
            storage.cleanup()
            raise
        _seeds[key] = storage, value
        return base, value

    storage, value = _seeds[key]
    base = Path(storage.name) / "active"
    if base.is_symlink() or not base.is_dir():
        base.unlink(missing_ok=True)
    else:
        shutil.rmtree(base)
    try:
        _clone(base.with_name("snapshot"), base)
    except BaseException:
        shutil.rmtree(base, ignore_errors=True)
        raise
    return base, value
