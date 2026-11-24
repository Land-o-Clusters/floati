"""Tests for the unshipped locks package."""

from tests import (
    harbor_locks_cleanup,
    harbor_locks_handoffs,
    harbor_locks_ledger,
    harbor_locks_provisioning,
    harbor_locks_queue,
    harbor_shallow_repository,
)


def load_tests(loader, standard_tests, pattern):
    modules = (
        harbor_locks_cleanup,
        harbor_locks_handoffs,
        harbor_locks_ledger,
        harbor_locks_provisioning,
        harbor_locks_queue,
        harbor_shallow_repository,
    )
    return loader.suiteClass(loader.loadTestsFromModule(module) for module in modules)
