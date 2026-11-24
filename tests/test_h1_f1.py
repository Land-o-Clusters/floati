"""H1-F1: every tracked reader of scripts/floati-codex-wait, classed.

The script is a digest-bound installed launcher, not a helper to delete.
This census is the map: every git-tracked file that names the path or
basename, by file:line, classed install / update-readback / manifest /
export-baseline / docs. The census files themselves are excluded so the
measurement does not name the thing it is measuring.

A new unclassified path REDs naming it. Counts are printed after the
named-set assertion.

Am.2: the pin is projection-aware. The export policy excludes readers
under .github/, docs/ (unpublished), and tools/, so a pin enumerating
them fails inside a projection, where those files do not exist. The pin
therefore enumerates the INCLUDED readers — the census the export
actually ships — and this test classifies every derived and pinned path
through the exporter's own classifier before comparing. In a projection
the policy is absent by design and classification is the identity, which
is exactly right: every file a projection carries is an included file,
so the same comparison holds there. Separate source and projected pins keep
exact line coordinates when the exporter reserializes the bundle manifest;
neither reader can accept a runtime-derived line offset.
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.export_inventory import (
    classify_inventory,
    export_policy_is_present,
    tracked_files,
)
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NEEDLE = "floati-codex-wait"
CLASSES = (
    "install",
    "update-readback",
    "manifest",
    "export-baseline",
    "docs",
)
CENSUS_PATHS = frozenset(
    {
        "tests/test_h1_f1.py",
        "tests/h1_f1_reader_map.txt",
        "tests/h1_f1_reader_map_projected.txt",
        "docs/evidence/h1-f1-codex-wait-readers-2026-09-05.md",
    }
)

PIN_RELATIVE = "tests/h1_f1_reader_map.txt"
PROJECTED_PIN_RELATIVE = "tests/h1_f1_reader_map_projected.txt"

# Printed pins on this tree (AX 9ee9d60d), Am.2: the included census only —
# the 22 readers the export policy excludes (.github, unpublished docs/,
# tools/) no longer reach a projection and no longer pin counts here.
# BL-11: tests/test_bl_11.py joins the install class (4 hits) — growth only.
# INSTALL-TRUST-PREFLIGHT-1 R3: fixture-derived operator pairs read the
# launcher name — tests/test_fu1_g4.py joins update-readback, more hits in
# test_fu1_g2 / test_install_trust_preflight / test_codex_hook_install. Growth only.
# READER-SOURCE-REMEDY-2: the hooks-only Doctor fixture adds one update-readback hit.
# CURRENT-ALIAS-READER-1: two more update-readback hits in the currency test.
PINNED_FILES = 45
PINNED_HITS = 105
PINNED_CLASS_FILES = {
    "install": 22,
    "update-readback": 9,
    "manifest": 2,
    "export-baseline": 2,
    "docs": 10,
}
PINNED_CLASS_HITS = {
    "install": 59,
    "update-readback": 29,
    "manifest": 2,
    "export-baseline": 2,
    "docs": 13,
}


def classify(path: str) -> str:
    if path.startswith("docs/") or path in (
        "floati/help_copy.py",
        "floati/generated_help.py",
    ):
        return "docs"
    if path in (
        "bundle-manifest.v0.json",
        "floati/manifest.py",
        "floati/uninstall.py",
    ):
        return "manifest"
    if path in (
        ".github/public-export-baseline.v0.json",
        ".github/labeler.yml",
        # the BASELINE-1-F1 Am.1 fixture is the test-side twin of the
        # committed baseline: the same reader, classified the same way
        "tests/fixtures/baseline-1-f1/public-export-baseline.v0.json",
    ):
        return "export-baseline"
    if (
        path == "floati/fleet_update.py"
        # READER-SKEW-1 Am.1: the currency reader observes install ancestry
        # and its test pins the observation - both update-readback readers.
        or path in ("floati/reader_currency.py", "tests/test_reader_currency.py",
                    "tests/test_reader_skew_gateway.py",
                    # Scratch host diagnostics bind the gateway, transport and hook.
                    "tests/test_f2_gateway_takeover.py")
        or path.startswith("tests/test_fu1_")
        # WAITER-IDLE-WINDOW-1: doctor reads the configured hook window;
        # it never invokes or installs the launcher it identifies.
        or path in ("tests/test_doctor.py", "floati/doctor.py")
    ):
        return "update-readback"
    if (
        path.startswith("floati/codex_hook")
        or path
        in (
            "floati/waiter_bundle.py",
            "floati/entrypoint_contract.py",
            # SEAT-ONBOARD-1: prerequisite observation only; this reader never
            # installs the global Stop hook or edits its trust registration.
            "floati/seat_onboarding.py",
            "tools/codex/codex-fleet-bus.py",
            "tests/test_codex_hook_install.py",
            "tests/test_net_setup.py",
            # Supported host observers use the same governed stable launcher.
            "tests/test_host_observer_install.py",
            "tests/test_host_permission_activity_contracts.py",
            "tests/test_cursor_host_install.py",
            # BL-11: the hook-trust guard and stable-launcher gates.
            "tests/test_bl_11.py",
            # BL-11-GATEWAY-1: native waiter-shape trust admission controls.
            "tests/test_codex_gateway_vendor.py",
            # INSTALL-TRUST-PREFLIGHT-1: Stop-hook write preflight and split subjects.
            "tests/test_install_trust_preflight.py",
            # READER-SKEW-1 Am.1: the installed-reader seam and its test.
            "tests/test_installed_reader.py",
            "tests/test_hook_preconditions.py",
            "tests/test_operator_contracts.py",
            "tests/test_deploy.py",
            # ROLLBACK-HOOK-1: host-wiring photograph compared on reverse deploy.
            "tests/test_rollback_hook_1.py",
            "tests/test_au1_s0.py",
            "tests/test_au1_s2.py",
            "tests/test_wait_command.py",
            # Host-tool fixture drives the existing native hook entry only.
            "tests/test_seat_execution_host_tool_native.py",
            # Scratch setup supplies the already trusted Stop prerequisite.
            "tests/test_seat_onboard.py",
        )
    ):
        return "install"
    raise AssertionError("unclassified reader of " + NEEDLE + ": " + path)


def tracked_paths(root: Path) -> tuple[str, ...]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(
        path.decode("utf-8")
        for path in listed.stdout.split(b"\0")
        if path and path.decode("utf-8") not in CENSUS_PATHS
    )


def derive_readers(root: Path) -> tuple[tuple[str, int, str], ...]:
    rows = []
    for relative in tracked_paths(root):
        rows.extend(readers_in_payload(relative, (root / relative).read_bytes()))
    return tuple(rows)


def readers_in_payload(relative: str, payload: bytes) -> tuple[tuple[str, int, str], ...]:
    if NEEDLE.encode("utf-8") not in payload:
        return ()
    kind = classify(relative)
    return tuple(
        (relative, number, kind)
        for number, line in enumerate(
            payload.decode("utf-8", "surrogateescape").splitlines(), 1
        )
        if NEEDLE in line
    )


def projected_reader_payloads(root: Path, rows) -> dict[str, bytes]:
    """Expose the known readers, then use the exporter's manifest writer.

    The adapters do not introduce this launcher basename. Reuse the existing
    source-reader population and adaptation memo rather than performing a
    second whole-tree census. The manifest writer additionally reads its own
    bounded file population. Non-reader payloads can retain their source
    bytes here: their SHA-256 values have fixed width and cannot contain the
    basename, so they cannot change any reader's manifest line coordinate.
    """

    from scripts import export_public as exporter
    from tests.export_inventory import (
        _adapted_bytes_source, export_policy, published_baseline,
    )

    adapt = _adapted_bytes_source(
        root, exporter, export_policy(root), set(published_baseline(root))
    )
    payloads = {
        relative: adapt(relative, (root / relative).read_bytes())
        for relative in sorted({path for path, _line, _kind in rows})
    }
    manifest_relative = "bundle-manifest.v0.json"
    manifest_source = (root / manifest_relative).read_bytes()
    manifest = json.loads(manifest_source)
    projected = {}
    for entry in manifest["files"]:
        relative = entry["path"]
        data = payloads[relative] if relative in payloads else (root / relative).read_bytes()
        projected[relative] = (data, "100644")
    projected[manifest_relative] = (
        payloads.get(manifest_relative, manifest_source), "100644"
    )
    # Only the blob-read boundary is replaced: a regenerator must see current
    # workspace bytes, including an uncommitted regenerated manifest. The real
    # writer owns digest refresh, JSON ordering, indentation and line endings.
    with mock.patch.object(exporter, "_blob", return_value=manifest_source):
        exporter._regenerate_bundle_manifest(
            projected, {manifest_relative: object()}, root, []
        )
    payloads[manifest_relative] = projected[manifest_relative][0]
    return payloads


def projected_reader_census(root: Path, rows) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        row
        for relative, payload in projected_reader_payloads(root, rows).items()
        for row in readers_in_payload(relative, payload)
    )


def load_pin(root: Path) -> tuple[tuple[str, int, str], ...]:
    relative = PIN_RELATIVE if export_policy_is_present(root) else PROJECTED_PIN_RELATIVE
    path = root / relative
    if not path.is_file():
        return ()
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        kind, loc = line.split("\t", 1)
        relative, number = loc.rsplit(":", 1)
        rows.append((relative, int(number), kind))
    return tuple(rows)


def included_reader_census(
    rows: tuple[tuple[str, int, str], ...],
    pinned: tuple[tuple[str, int, str], ...],
    root: Path,
) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """Keep the rows whose paths no export excludes, on both sides alike.

    The population is the UNION of derived and pinned paths, so a reader
    that disappeared from the tree is still classified (and still
    reported missing) rather than silently dropped with the excluded
    half. In a policy-less projection classification is the identity and
    every carried file is an included file, so the same filter is exact
    there too.
    """

    population = sorted(
        {path for path, _line, _kind in rows}
        | {path for path, _line, _kind in pinned}
    )
    included = frozenset(classify_inventory(population, root=root))
    return (
        [row for row in rows if row[0] in included],
        [row for row in pinned if row[0] in included],
    )


class H1F1ReaderMapTests(unittest.TestCase):
    def test_every_tracked_reader_is_classed_and_pinned(self) -> None:
        rows = derive_readers(REPOSITORY_ROOT)
        pinned = load_pin(REPOSITORY_ROOT)
        kept_rows, kept_pinned = included_reader_census(rows, pinned, REPOSITORY_ROOT)
        files = frozenset(path for path, _line, _kind in kept_rows)
        pinned_files = frozenset(path for path, _line, _kind in kept_pinned)
        file_class = {path: kind for path, _line, kind in kept_rows}
        class_files = {kind: 0 for kind in CLASSES}
        class_hits = {kind: 0 for kind in CLASSES}
        for kind in file_class.values():
            class_files[kind] += 1
        for _path, _line, kind in kept_rows:
            class_hits[kind] += 1

        extra = sorted(files - pinned_files)
        missing = sorted(pinned_files - files)
        self.assertEqual(
            extra,
            [],
            "new unbound reader of " + NEEDLE + ": " + ", ".join(extra),
        )
        self.assertEqual(
            missing,
            [],
            "pinned reader of " + NEEDLE + " disappeared: " + ", ".join(missing),
        )
        self.assertEqual(kept_pinned, kept_rows)
        self.assertEqual(PINNED_FILES, len(files))
        self.assertEqual(PINNED_HITS, len(kept_rows))
        self.assertEqual(PINNED_CLASS_FILES, class_files)
        self.assertEqual(PINNED_CLASS_HITS, class_hits)

    def test_the_pin_predicts_a_real_projection(self) -> None:
        """The census the export ships is the census the pin enumerates.

        Builds an included-file fixture, exposes its reader files and uses
        the real manifest writer, then runs this census inside it. RED in the
        a60df682 rehearsal: the pin enumerated 22 readers the policy
        excludes, so the projected suite failed on readers that had
        deliberately not shipped.
        """

        if not export_policy_is_present():
            self.skipTest("no export policy in this tree; classification is identity")

        included = classify_inventory(tracked_files(REPOSITORY_ROOT), root=REPOSITORY_ROOT)
        exposed_readers = projected_reader_payloads(
            REPOSITORY_ROOT, load_pin(REPOSITORY_ROOT)
        )
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary)
            projection = base / "projection"
            projection.mkdir()
            for relative in included:
                target = projection / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative in exposed_readers:
                    target.write_bytes(exposed_readers[relative])
                else:
                    shutil.copyfile(REPOSITORY_ROOT / relative, target)

            def git(*arguments: str) -> None:
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("GIT_")
                }
                subprocess.run(
                    ["/usr/bin/git", *arguments],
                    cwd=projection,
                    env=environment,
                    check=True,
                    capture_output=True,
                )

            git("init", "-q", "--initial-branch=main")
            git("config", "user.name", "fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("add", ".")
            git("commit", "-q", "-m", "projection fixture")

            completed = subprocess.run(
                [sys.executable, "-m", "unittest", "tests.test_h1_f1"],
                cwd=projection,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True,
                text=True,
            )

        self.assertEqual(
            0,
            completed.returncode,
            "the projected census failed against the pin:\n"
            + completed.stderr[-4000:],
        )


class ExcludedHalfPrivatePinTests(unittest.TestCase):
    """NET-FENCE-1-F2: the reader pin's excluded half keeps a private twin.

    Am.2 compares the INCLUDED half, which went quiet on the readers the
    policy hides (.github/, unpublished docs/ such as docs/design/,
    tools/). The private twin pins the EXCLUDED half - and the Am.1 read
    refused the first twin because its pin artifact lived under tests/,
    where the classifier says INCLUDE, leaking the excluded census into
    the projection. The artifact lives where the classifier already says
    private_only (.github/, declared in the policy's own
    private_only_paths); in a policy-less projection the twin is a typed
    skip, because nothing is excluded there.
    """

    def test_the_excluded_readers_are_pinned_by_a_private_pin(self) -> None:
        from tests.export_inventory import (
            classify_inventory_excluded,
            export_policy_is_present,
        )

        if not export_policy_is_present(REPOSITORY_ROOT):
            self.skipTest(
                "export_policy_absent: a policy-less projection carries no "
                "excluded half, so the private half is a typed skip"
            )
        pin_relative = ".github/h1-f1-reader-map-private.v0.txt"
        # The artifact itself must be private: a private pin that the
        # exporter would publish leaks the excluded half's census.
        self.assertEqual(
            [pin_relative],
            list(classify_inventory_excluded([pin_relative], root=REPOSITORY_ROOT)),
            "the private pin artifact must classify private_only, never "
            "INCLUDE - it would land in the projection and leak the census",
        )
        pin_path = REPOSITORY_ROOT / pin_relative
        self.assertTrue(
            pin_path.is_file(),
            "no private pin artifact: " + pin_relative + " is absent, so a "
            "reader planted in an excluded file (docs/design/) is "
            "invisible to the fence",
        )
        rows = derive_readers(REPOSITORY_ROOT)
        pinned = []
        for line in pin_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            kind, loc = line.split("\t", 1)
            relative, number = loc.rsplit(":", 1)
            pinned.append((relative, int(number), kind))
        population = sorted(
            {path for path, _line, _kind in rows}
            | {path for path, _line, _kind in pinned}
        )
        excluded = frozenset(
            classify_inventory_excluded(population, root=REPOSITORY_ROOT)
        )
        self.assertEqual(
            [row for row in pinned if row[0] in excluded],
            [row for row in rows if row[0] in excluded],
            "a reader appeared or vanished in the EXCLUDED half; name it in "
            + pin_relative,
        )
