"""Offline README and marketing capture tests."""

from tests import (
    harbor_capture_readme_real_ledgers,
    harbor_capture_shot1_locf1,
    harbor_capture_tui_moments,
    harbor_demo_capture_assets,
    harbor_mk_d1_repeatability,
    harbor_wall,
)


def load_tests(loader, standard_tests, pattern):
    modules = (
        harbor_capture_readme_real_ledgers,
        harbor_capture_shot1_locf1,
        harbor_capture_tui_moments,
        harbor_demo_capture_assets,
        harbor_mk_d1_repeatability,
        harbor_wall,
    )
    return loader.suiteClass(loader.loadTestsFromModule(module) for module in modules)
