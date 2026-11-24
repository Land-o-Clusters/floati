"""Explicit native test sizes; instrument defaults remain full scale.

The ordinary suite uses small legal histories. The explicit census enters
``full_scale()`` before loading/running its retained correctness controls.
No environment variable can silently turn either population into the other.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import atexit
import copy
import importlib.util
import importlib.machinery
import os
from pathlib import Path
import pickle
import shutil
import stat
import sys
import tempfile
from unittest.mock import patch


_FULL_SCALE = ContextVar("bus_scale_full_test_population", default=False)
_COMPILED = {}
_GIT_TEMPLATE = None
_FIXTURE_CLEANUPS = {}
_PARSERS = {}
_SOURCE_FIXTURES = {}


def clone_fixture(value):
    """Independent trusted fixture data, including its internal alias graph.

    These values come from this process's owned fixtures, never a pickle file
    or an external payload. The C codec avoids recursively copying large
    literal populations in Python for each mutation control.
    """
    try:
        return pickle.loads(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
    except (pickle.PickleError, TypeError, AttributeError):
        return copy.deepcopy(value)


@contextmanager
def bounded_cli_parser():
    """Reuse the read-only command tree for bounded handler controls.

    Building the full command tree is separately covered by the parser census.
    Each invocation still parses its actual arguments and calls its real
    handler. Explicit native controls construct the tree normally. Changed or
    patched CLI callables invalidate the template, including handler patches.
    """
    from floati import cli
    original = cli._parser
    if full_scale_enabled() or getattr(original, "_bounded_parser", False):
        yield
        return
    registrars = (cli, *( __import__("floati." + name, fromlist=[name])
        for name in ("admin_cli", "context", "holds", "grants", "bus_epoch", "purge", "archive_backup")))
    key = (original, tuple((module.__name__, tuple((name, id(value))
        for name, value in vars(module).items() if callable(value))) for module in registrars),
           repr(cli._EXIT_CODE_CONTRACT))
    def fresh_parser():
        # argparse creates a new Namespace on every parse. Neither the CLI
        # handlers nor its schema reader mutate the registered command tree.
        # Help rendering adds actions, so only main's immediate argument
        # parse may borrow this tree. Every other caller gets a fresh parser.
        if (full_scale_enabled()
                or sys._getframe(1).f_code is not getattr(cli.main, "__code__", None)):
            return original()
        if key not in _PARSERS:
            _PARSERS[key] = original()
        return _PARSERS[key]

    fresh_parser._bounded_parser = True
    with patch.object(cli, "_parser", fresh_parser):
        yield


@contextmanager
def cached_fixture_compilation():
    """Fresh module execution from current bytes without repeated compilation."""
    loader_type = importlib.machinery.SourceFileLoader
    original = loader_type.get_code
    if full_scale_enabled() or getattr(original, "_fixture_compilation", False):
        yield
        return

    def get_code(loader, fullname):
        if full_scale_enabled() or type(loader) is not loader_type or fullname != loader.name:
            return original(loader, fullname)
        raw = loader.get_data(loader.path)
        key = loader.path, raw
        if key not in _COMPILED:
            _COMPILED[key] = compile(raw, loader.path, "exec", dont_inherit=True)
        return _COMPILED[key]

    get_code._fixture_compilation = True
    with patch.object(loader_type, "get_code", get_code):
        yield


def _class_source_revision(owner, original):
    """A test class shares its unchanged implementation dependency.

    Registration is lazy because some fixture classes are borrowed without
    unittest's class setup. Both normal class cleanup and the receipt runner
    own the same idempotent final check.
    """
    key = owner, original
    if key not in _SOURCE_FIXTURES:
        revision = original()
        _SOURCE_FIXTURES[key] = revision

        def finish():
            expected = _SOURCE_FIXTURES.pop(key, None)
            _FIXTURE_CLEANUPS.pop(("source", key), None)
            if expected is not None and original() != expected:
                raise AssertionError("implementation identity changed during bounded fixture construction")

        owner.addClassCleanup(finish)
        register_fixture_cleanup(("source", key), finish)
    return _SOURCE_FIXTURES[key]


@contextmanager
def stable_fixture_sources(*, owner=None):
    """Reuse one actual implementation fingerprint during fixture construction.

    The real metadata fingerprint is checked before and after the bounded
    operation, or around the shared test class when an owner is supplied.
    Any source identity change fails the fixture. Ledger identities,
    cache sealing, native operations and all refusal checks remain real.
    Explicit native controls retain every original metadata observation.
    """
    from floati import send_prefix
    original = send_prefix._revision
    if full_scale_enabled() or getattr(original, "_fixture_source", False):
        yield
        return
    revision = original() if owner is None else _class_source_revision(owner, original)

    def observed_revision():
        return original() if full_scale_enabled() else revision

    observed_revision._fixture_source = True
    with patch.object(send_prefix, "_revision", observed_revision), cached_fixture_compilation():
        try:
            yield
        finally:
            if owner is None and original() != revision:
                raise AssertionError("implementation identity changed during bounded fixture construction")


class StableSourceFixture:
    """An unchanged real implementation revision is one unit-test dependency.

    Tests can still replace the revision function to exercise unavailable or
    upgraded dependencies. Actual source metadata is checked around the class;
    explicit full controls retain every observation.
    """
    def run(self, result=None):
        with stable_fixture_sources(owner=type(self)), bounded_cli_parser():
            return super().run(result)


def register_fixture_cleanup(owner, cleanup):
    """Retain ownership when a fixture class is borrowed by another module."""
    _FIXTURE_CLEANUPS[owner] = cleanup


def cleanup_bounded_fixtures():
    """Called by the receipt runner, with process-exit fallback for plain unittest."""
    global _GIT_TEMPLATE
    callbacks = list(_FIXTURE_CLEANUPS.values())
    _FIXTURE_CLEANUPS.clear()
    for cleanup in callbacks:
        cleanup()
    if _GIT_TEMPLATE is not None:
        _GIT_TEMPLATE[0].cleanup()
        _GIT_TEMPLATE = None


atexit.register(cleanup_bounded_fixtures)


def _git_identity(root):
    result = {}
    for path in (root, *sorted(root.rglob("*"))):
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        if (info.st_uid != os.getuid() or not (directory or stat.S_ISREG(info.st_mode))
                or not directory and info.st_nlink != 1):
            raise AssertionError("banked fixture template contains a nonordinary file")
        result[path.relative_to(root).as_posix()] = (
            info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            None if directory else path.read_bytes())
    return result


def banked_send_repository(base):
    """Independent Git files from one genuinely authored immutable template."""
    from tests.banked_sha_fixture import banked_send_repository as fresh
    from tests.temp_roots import REAL_TEMP_ROOT
    global _GIT_TEMPLATE
    if full_scale_enabled():
        return fresh(base)
    if _GIT_TEMPLATE is not None and _GIT_TEMPLATE[4] is not fresh:
        _GIT_TEMPLATE[0].cleanup()
        _GIT_TEMPLATE = None
    if _GIT_TEMPLATE is None:
        temporary = tempfile.TemporaryDirectory(prefix="bus-scale-banked-template-", dir=REAL_TEMP_ROOT)
        try:
            repository, sha = fresh(Path(temporary.name).resolve())
            _GIT_TEMPLATE = temporary, repository, sha, _git_identity(repository), fresh
        except BaseException:
            temporary.cleanup()
            raise
    _temporary, repository, sha, identity, _producer = _GIT_TEMPLATE
    if _git_identity(repository) != identity:
        raise AssertionError("genuine banked fixture template changed")
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    destination = base / "banked-send-repo"
    shutil.copytree(repository, destination)
    return destination, sha


def load_fixture_module(path, name):
    """Execute a fresh fixture module, sharing only immutable compiled code.

    The source is read on every call. A changed byte always compiles again;
    module globals, classes and mutable state are never shared. Explicit
    native controls retain the ordinary source loader.
    """
    path = Path(path)
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    if full_scale_enabled():
        specification.loader.exec_module(module)
    else:
        raw = path.read_bytes()
        key = str(path), raw
        code = _COMPILED.get(key)
        if code is None:
            code = compile(raw, str(path), "exec", dont_inherit=True)
            _COMPILED[key] = code
        exec(code, module.__dict__)
    return module


def full_scale_enabled():
    return _FULL_SCALE.get()


def seed_counts():
    return (1000, 10000) if full_scale_enabled() else (8, 16)


@contextmanager
def full_scale():
    token = _FULL_SCALE.set(True)
    try:
        yield
    finally:
        _FULL_SCALE.reset(token)
