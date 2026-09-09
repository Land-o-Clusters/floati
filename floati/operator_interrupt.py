"""Restore operator SIGINT after an ignoring parent crossed exec."""

from __future__ import annotations

import signal


def restore_operator_sigint() -> None:
    """Honor SIGINT even when a parent left it ignored across exec.

    POSIX copies an ignored disposition across exec. CPython's
    ``subprocess.restore_signals`` restores ``SIGPIPE``, ``SIGXFZ``, and
    ``SIGXFSZ`` only, and the interpreter skips installing
    ``default_int_handler`` when it starts under ``SIG_IGN``. Watch,
    selftest, and supervise therefore restore SIGINT at process entry.

    This is not ``preexec_fn``: those three verbs *are* the child, not
    children floati itself Popen's. ``preexec_fn`` cannot run in the
    constructed RED (the parent is unittest). CPython also documents
    ``preexec_fn`` as unsafe in a multithreaded parent. A re-exec wrapper
    would land after exec at the same place this function already runs.

    ``SIG_DFL`` clears the inherited ignore; ``default_int_handler`` is
    then installed so Python raises KeyboardInterrupt. Watch's existing
    contract is SIGINT → exit 0, not terminate with ``-SIGINT``.
    """

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.default_int_handler)
