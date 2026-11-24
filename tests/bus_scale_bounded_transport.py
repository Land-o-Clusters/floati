"""Default unit transport for native instrumentation correctness controls.

The real entry, Git inventory, durable operation and proof decoders execute.
Only teardown's external process inventory is a declared empty fixture, and
entry execution is in-process. Vector 5 retains owned children and host census.
These controls never qualify native timing, process startup or host activity.
"""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import resource
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from tests.bus_scale_test_profile import full_scale_enabled, load_fixture_module, bounded_cli_parser, stable_fixture_sources
from tests.temp_roots import REAL_TEMP_ROOT

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py'
RUNNER = ROOT / 'tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py'
ADAPTER = ROOT / 'tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py'


@contextmanager
def bounded_activity():
    if full_scale_enabled():
        yield
    else:
        from floati import seat_teardown_activity
        with patch.object(seat_teardown_activity, '_native_snapshot',
                          return_value=([], [os.getpid()], [])):
            yield


class _UnitUsage:
    RUSAGE_CHILDREN = resource.RUSAGE_CHILDREN
    RUSAGE_SELF = resource.RUSAGE_SELF

    @staticmethod
    def getrusage(_who):
        own = resource.getrusage(resource.RUSAGE_SELF)
        child = resource.getrusage(resource.RUSAGE_CHILDREN)
        return SimpleNamespace(ru_utime=own.ru_utime + child.ru_utime,
                               ru_stime=own.ru_stime + child.ru_stime)


class _UnitOwner:
    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def launch_owned_child(self, argv, *, cwd, env, stdout, stderr):
        cleanup = None
        if len(argv) == 11 and argv[:4] == [sys.executable, '-B', str(ROOT / 'tests/bus_scale_census.py'), 'native-child']:
            cleanup = Path(argv[-1])
            entry_args = argv[4:-1]
        elif len(argv) == 9 and argv[:3] == ['/usr/bin/python3', '-B', str(ENTRY)]:
            entry_args = argv[3:]
        else:
            entry_args = None
        if full_scale_enabled() or entry_args is None or entry_args[-1] != 'owned-census-v1':
            return self.delegate.launch_owned_child(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr)
        previous = Path.cwd()
        output, error = io.StringIO(), io.StringIO()
        environment = dict(env)
        home = None
        if cleanup is not None:
            home = Path(tempfile.mkdtemp(prefix='suite-home-bounded-', dir=REAL_TEMP_ROOT)).resolve()
            environment['HOME'] = str(home)
        entry = load_fixture_module(ENTRY, 'bounded_instrument_entry')
        try:
            os.chdir(cwd)
            with patch.dict(os.environ, environment, clear=True), patch.object(sys, 'argv', [str(ENTRY), *entry_args]), bounded_activity(), bounded_cli_parser(), stable_fixture_sources(), redirect_stdout(output), redirect_stderr(error):
                code = entry.main()
            stdout.write(output.getvalue().encode())
            stderr.write(error.getvalue().encode())
            stdout.flush()
            stderr.flush()
        finally:
            os.chdir(previous)
            if home is not None:
                shutil.rmtree(home)
                cleanup.write_text(json.dumps({'native_home': str(home), 'home_removed': not home.exists(),
                                               'cache_tag': sys.implementation.cache_tag,
                                               'boundary': 'bounded_unit_transport'}) + '\n')
        return SimpleNamespace(pid=os.getpid(), returncode=code, bounded_unit=True)

    def collect_owned_child(self, child, **kwargs):
        if not getattr(child, 'bounded_unit', False):
            return self.delegate.collect_owned_child(child, **kwargs)
        return dict(status='complete', exit_code=child.returncode,
                    child_collected=True, interrupted=False)


def bounded_module(module):
    if full_scale_enabled():
        return module
    path = Path(module.__file__).resolve()
    if path == RUNNER:
        return _UnitOwner(module)
    if path == ADAPTER:
        module.resource = _UnitUsage()
    if path in {ROOT / 'tests/bus_scale_census.py', ROOT / 'tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py'}:
        if getattr(module, '_bounded_unit_transport', False):
            return module
        module._bounded_unit_transport = True
        module.resource = _UnitUsage()
        for name in ('_load', 'expanded_adapter'):
            original = getattr(module, name, None)
            if original is not None:
                def load(*args, _original=original, **kwargs):
                    return bounded_module(_original(*args, **kwargs))
                setattr(module, name, load)
    return module
