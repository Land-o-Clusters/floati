"""Floati phase-1 tests.

FENCE-H1: importing this package pins the test process away from the
operator's real account. ``HOME`` and the XDG roots move to a scratch
directory under the real temp root before any test module, runner, or
library default resolves a path, so a test — or product code a test
exercises — can never read or delete anything under the real home.
The pre-pin home is recorded in ``REAL_HOME_BEFORE_PIN`` for fences
that must name what they kept the suite out of; ``HOME_PIN_ROOT`` is
the scratch the process was moved into.

ONE NAMED EXCEPTION, carried deliberately: the operator's per-user
Python package directory (``site.getusersitepackages()`` — e.g. the
optionally installed Pillow the capture scripts need) is appended to
``sys.path`` and ``PYTHONPATH`` before the pin, so the suite can still
IMPORT the account's installed code libraries. Every path RESOLUTION
through ``HOME``/XDG still lands in the scratch; nothing under the
operator's data trees is reachable. A test that genuinely needs a
fixture directory uses ``tests.temp_roots.REAL_TEMP_ROOT``, which is
not home-derived and is unaffected.
"""

import os
import site
import sys
import tempfile

from tests.temp_roots import REAL_TEMP_ROOT

REAL_HOME_BEFORE_PIN = os.environ.get("HOME", "")

REAL_USER_SITE = site.getusersitepackages()

HOME_PIN_ROOT = tempfile.mkdtemp(prefix="suite-home-", dir=REAL_TEMP_ROOT)

os.environ["HOME"] = HOME_PIN_ROOT
os.environ["XDG_CONFIG_HOME"] = os.path.join(HOME_PIN_ROOT, ".config")
os.environ["XDG_DATA_HOME"] = os.path.join(HOME_PIN_ROOT, ".local", "share")
os.environ["XDG_CACHE_HOME"] = os.path.join(HOME_PIN_ROOT, ".cache")

# FL-1b: the pinned HOME is also the one lever every fixture-spawned git
# child reads even when a test strips GIT_* from the env, so one gitconfig
# here stops git from detaching a background maintenance child that would
# outlive the fixture and race its TemporaryDirectory cleanup (FL-1's
# measured mechanism, ~35 files share the spawn shape).
with open(os.path.join(HOME_PIN_ROOT, ".gitconfig"), "w") as _gitconfig:
    _gitconfig.write("[gc]\n\tauto = 0\n\tautoDetach = false\n")

if os.path.isdir(REAL_USER_SITE) and REAL_USER_SITE not in sys.path:
    sys.path.append(REAL_USER_SITE)

_carried_pythonpath = os.environ.get("PYTHONPATH", "")
if REAL_USER_SITE not in _carried_pythonpath.split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (_carried_pythonpath, REAL_USER_SITE)
        if part
    )
