# Installed cold-index measurement

Run this on Linux with Python 3.9, the interpreter CI uses, from the root of a full clone of this repository with the candidate revision checked out:

```sh
python3 -B -m unittest -v tests.harbor_cold_upgrade_send.InstalledColdUpgradeSendTests.test_bulk_validated_history
```

The clone's history must hold `HEAD` (the candidate), the pre-index revision `1bbd97a0b03fcaabada571eadb6922479e327378`, and for the release reference `65d800341` and `94364cde1`. The driver reads them with `/usr/bin/git` and clones from the checkout. It installs that pre-index revision, validates 9,998 and 99,980 envelope frames with its installed reader, updates the installation to `HEAD`, and times the first two native sends. Its installs use the interpreter that runs the test. Its scratch lives in a temporary directory under `HOME`. The recorded runs allowed 7,200 seconds. Read the `COLD_UPGRADE` records for wall and CPU seconds, compact-bank bytes, loose nodes and epoch-maintenance stderr. Per-process I/O counters are read from `/proc`, so they are collected only on Linux. A passing unittest verifies ledger preservation, the appended envelope and readiness pairs, and reuse of the cold generation. For the release reference, select `test_release_base_and_compact_train_on_identical_histories` in the same class. It measures `65d800341` and `94364cde1` serially on identical histories. To bring the instrument to another branch, copy this README and `tests/harbor_cold_upgrade_send.py` onto it and commit both, then run the command above with that commit checked out.
