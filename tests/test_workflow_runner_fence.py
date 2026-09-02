"""One fixture per numbered runs-on shape the projection detector must handle.

Shapes 1-12 are the ruled shape list; the verdicts are OUR allowlist's, which
admits only hosted Linux and refuses self-hosted. RED-first: every one of these
failed with ModuleNotFoundError before floati/workflow_runner_fence.py existed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from floati.workflow_runner_fence import (
    ALLOWED_RUNNER_LABELS,
    REFUSAL_CODE,
    scan_tree,
    scan_workflow_source,
)
from tests.private_artifacts import require_private_artifact


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/probe.yml"

# The harbor's own CI at 481afae4, verbatim, before it was renamed out of the
# projection. It is the reason this fence exists, so it is pinned as bytes here
# rather than read from a path the public projection deliberately does not carry.
HARBOR_SELFTEST_481AFAE4 = r"""name: selftest

# 2026-09-02 (org incident): the [ubuntu, macos] matrix on every push to every
# branch burned the enterprise's whole monthly Actions pool in two days (macOS
# minutes bill at 10x). HOSTED macOS is BANNED in this org. Runner labels are
# repository VARIABLES so a flip is a settings change, not a workflow edit (the
# org push ruleset blocks workflow edits): FLOATI_CI_RUNNER_LINUX (default
# hosted ubuntu; the on-prem box once its org runners appear) and
# FLOATI_CI_RUNNER_MACOS, whose DEFAULT is the self-hosted Mac and can never
# resolve to hosted macOS. The self-hosted default is a deliberate TRADE: it
# fails into a queue, never into spend, and because a job's timeout starts only
# when the job STARTS, a job aimed at an offline Mac would sit the full 24 h
# queue looking like a slow run. The `preflight` job is the instrument: ~10 s of
# Linux that refuses to queue when the Mac runner is not online, failing CLOSED
# and SAYING WHY (absent secret / API failure / offline are three different lines).
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

concurrency:
  group: selftest-${{ github.ref }}
  cancel-in-progress: true   # a superseded run renders as CANCELLED, not failed: two landings in a row run the suite once

permissions:
  contents: read

jobs:
  linux-selftest:
    runs-on: ${{ fromJSON(vars.FLOATI_CI_RUNNER_LINUX || '["ubuntu-latest"]') }}
    timeout-minutes: 40
    steps:
      # The selftest installs the repository into a destination through the
      # product's own committed-tree writer, and that writer REFUSES a shallow
      # source (`deployment_shallow_repository`) because a truncated history
      # cannot answer "is this working tree current with its commit". A default
      # actions/checkout is depth 1, so the runner handed the product exactly
      # the repository shape it is built to refuse. Fetch the whole history;
      # the refusal is correct and stays.
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      # CI-GREEN-18: a branch run checks out that branch only - the landing
      # classifier (tests/test_landing_classify.py) diffs this tree against
      # refs/remotes/origin/main, which a branch checkout never carries, so
      # the suite died in ExportRefusal on every non-main run. Fetch main
      # explicitly; the classifier's refusal is correct once main is here.
      - name: Fetch origin/main for the landing classifier
        shell: bash
        run: git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main
      - uses: actions/setup-python@v5
        with:
          python-version: "3.9"
      # The suite is an artifact selftest, not a unit-test run: several of its
      # constants are only meaningful against the real tools. minisign in
      # particular is asserted present on purpose - a signing constant
      # "verified" by a mock is not verified - so these are installed rather
      # than skipped around.
      - name: Install the real tools the selftest asserts against
        shell: bash
        run: |
          set -euo pipefail
          python3 -m pip install --disable-pip-version-check --quiet pillow jsonschema
          if [ "${RUNNER_OS}" = "Linux" ]; then
            sudo apt-get update -qq
            sudo apt-get install -y -qq minisign
          else
            brew install minisign
          fi
          minisign -v
      - name: Run full artifact selftest
        run: python3 -m floati.selftest

  preflight:
    # Mac runner online, or refuse to queue. Not on pull_request: the Mac leg never runs fork or branch code.
    if: github.event_name != 'pull_request'
    # Pinned to hosted ubuntu ON PURPOSE (~10 s, cents): the observer stays outside the system it observes. On the
    # box's runner class it would share the box's failure modes (no `gh` binary -> rc=127 reads like a token problem).
    runs-on: ubuntu-latest
    timeout-minutes: 2
    steps:
      - name: Read the runners API (fail closed on any failure, and say which failure)
        env:
          # GITHUB_TOKEN cannot read the runners endpoint (Administration: read, which it can never carry).
          # RUNNER_STATUS_TOKEN is a fine-grained PAT the OWNER created with exactly that permission on this repo.
          GH_TOKEN: ${{ secrets.RUNNER_STATUS_TOKEN }}
        shell: bash
        run: |
          set +e   # NOT -e: a failing $(...) assignment under bash -e exits BEFORE the verdict line prints (Puddle run 33644994593)
          set -u
          if [ -z "${GH_TOKEN:-}" ]; then echo "runner-api=skipped(secret RUNNER_STATUS_TOKEN absent) verdict=refusing-to-queue"; exit 1; fi
          out=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runners" --jq '.runners[] | select(.name=="floati-mac") | .status' 2>&1); rc=$?
          if [ $rc -ne 0 ]; then echo "runner-api=failed(rc=$rc) verdict=refusing-to-queue: $out"; exit 1; fi
          case "$out" in
            online) echo "runner-api=ok runner=floati-mac status=online verdict=runner-online";;
            *) echo "runner-api=ok runner=floati-mac status=${out:-absent} verdict=runner-offline: refusing to queue"; exit 1;;
          esac

  macos-selftest:
    # Self-hosted Mac only (the variable's default); the runner's pre-job hook queues behind the build seat's lock.
    needs: preflight
    runs-on: ${{ fromJSON(vars.FLOATI_CI_RUNNER_MACOS || '["self-hosted","macOS","floati-mac"]') }}
    timeout-minutes: 40
    steps:
      - name: First line - load average (a red under contention carries its own control)
        run: echo "loadavg=$(sysctl -n vm.loadavg)"
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Fetch origin/main for the landing classifier
        shell: bash
        run: git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main
      - name: Tools the selftest asserts against (assert only - this job mutates nothing on the Mac)
        shell: bash
        run: |
          set -euo pipefail
          python3 -m pip install --disable-pip-version-check --quiet --user pillow jsonschema
          command -v minisign >/dev/null || { echo "minisign ABSENT on the self-hosted Mac: install it by hand (measured present 2026-09-02)"; exit 1; }
          minisign -v
      - name: Run full artifact selftest
        run: python3 -m floati.selftest
"""

# The live harbor CI is the 481afae4 bytes above under EXACTLY THREE named
# substitutions, and the pin below names all three rather than tolerating a
# diff. Two are the rename out of the projection. The third is a fix: ci-box-8
# (run 33653526683) died in this step at `sudo apt-get` with "sudo: a terminal
# is required to read the password", because passwordless sudo is refused on
# that box on the record. A PIN THAT IGNORES A CHANGED STEP IS A LOOSENING, so
# the new step text is pinned as an exact literal and the old one is asserted
# ABSENT — a substitution that stops matching must red, not quietly no-op.
HARBOR_NAME_481AFAE4 = "name: selftest\n"
HARBOR_NAME_LIVE = "name: harbor-selftest\n"

HARBOR_CONCURRENCY_481AFAE4 = "group: selftest-${{ github.ref }}"
HARBOR_CONCURRENCY_LIVE = "group: harbor-selftest-${{ github.ref }}"

HARBOR_INSTALL_STEP_481AFAE4 = r"""          python3 -m pip install --disable-pip-version-check --quiet pillow jsonschema
          if [ "${RUNNER_OS}" = "Linux" ]; then
            sudo apt-get update -qq
            sudo apt-get install -y -qq minisign
"""

HARBOR_INSTALL_STEP_LIVE = r"""          # ci-box-8, run 33653526683: this step assumed hosted ubuntu and ran
          # `sudo apt-get`, which on the self-hosted box died with "sudo: a
          # terminal is required to read the password". The install was never
          # the point - the ASSERTION was - so assert first and install only
          # where installing is possible, and when it is not, say which runner
          # is missing what rather than failing as a password prompt.
          python3 -m pip install --disable-pip-version-check --quiet --user pillow jsonschema
          if [ "${RUNNER_OS}" = "Linux" ]; then
            if command -v minisign >/dev/null 2>&1; then
              echo "minisign present on ${RUNNER_NAME:-unknown}: nothing to install"
            elif sudo -n true 2>/dev/null; then
              sudo apt-get update -qq
              sudo apt-get install -y -qq minisign
            else
              echo "minisign ABSENT on ${RUNNER_NAME:-unknown} and no passwordless sudo: install it on the runner by hand (measured 2026-09-02, run 33653526683)"
              exit 1
            fi
"""

# (from-481afae4, to-live, what it is) — the whole substitution set, in one
# place, so the pin cannot name two of three.
HARBOR_SUBSTITUTIONS = (
    (HARBOR_NAME_481AFAE4, HARBOR_NAME_LIVE, "the workflow name"),
    (
        HARBOR_CONCURRENCY_481AFAE4,
        HARBOR_CONCURRENCY_LIVE,
        "the concurrency group prefix",
    ),
    (
        HARBOR_INSTALL_STEP_481AFAE4,
        HARBOR_INSTALL_STEP_LIVE,
        "the Linux install step (ci-box-8 assert-first remedy, run 33653526683)",
    ),
)


def one_job(runs_on: str, *, extra: str = "") -> str:
    """A minimal single-job workflow whose runs-on is the shape under test."""

    return (
        "name: probe\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  probe-job:\n"
        f"{extra}"
        f"    runs-on: {runs_on}\n"
        "    steps:\n"
        "      - run: echo hello\n"
    )


class WorkflowRunnerFenceTests(unittest.TestCase):
    def scan(self, text: str, *, path: str = WORKFLOW, **kwargs) -> list[dict]:
        return scan_workflow_source(path, text, **kwargs)

    def reasons(self, findings) -> list[str]:
        return sorted(finding["reason"] for finding in findings)

    def assertAccepted(self, text: str, **kwargs) -> None:
        findings = self.scan(text, **kwargs)
        self.assertEqual([], findings, json.dumps(findings, indent=2, sort_keys=True))

    def assertRefused(self, text: str, reason: str, **kwargs) -> list[dict]:
        findings = self.scan(text, **kwargs)
        self.assertTrue(findings, "an unresolvable or unhosted runner was accepted")
        self.assertIn(reason, self.reasons(findings))
        for finding in findings:
            self.assertEqual(WORKFLOW, finding["path"])
            self.assertIn("job", finding)
            self.assertIn("runs_on", finding)
            self.assertIn("detail", finding)
        return findings

    # ---- the allowlist itself -------------------------------------------

    def test_allowlist_is_exactly_the_three_known_hosted_linux_images(self) -> None:
        """Widening the allowlist by one label is rejected."""

        self.assertEqual(
            ("ubuntu-22.04", "ubuntu-24.04", "ubuntu-latest"),
            tuple(sorted(ALLOWED_RUNNER_LABELS)),
        )
        self.assertEqual("public_workflow_runner_unhosted", REFUSAL_CODE)

    # ---- shape 1: literal scalar ----------------------------------------

    def test_shape_01_literal_scalar_label_is_checked(self) -> None:
        """A bare label is read as a label: allowlisted passes, macos-14 refuses."""

        for label in ALLOWED_RUNNER_LABELS:
            with self.subTest(label=label):
                self.assertAccepted(one_job(label))
        refused = self.assertRefused(one_job("macos-14"), "runner_hosted_macos")
        self.assertEqual(["macos-14"], [finding["label"] for finding in refused])
        self.assertEqual(["probe-job"], [finding["job"] for finding in refused])
        self.assertRefused(one_job("floati-mac"), "runner_label_unknown")
        self.assertRefused(one_job("windows-latest"), "runner_hosted_windows")
        self.assertRefused(one_job("self-hosted"), "runner_self_hosted")
        self.assertRefused(one_job("Ubuntu-Latest"), "runner_label_unknown")

    # ---- shape 2: YAML sequence -----------------------------------------

    def test_shape_02_sequence_runs_on_checks_every_item(self) -> None:
        """A label set is checked item by item, not waved through or refused wholesale."""

        self.assertAccepted(one_job("[ubuntu-latest]"))
        refused = self.assertRefused(one_job("[self-hosted, Linux]"), "runner_self_hosted")
        self.assertEqual(
            ["Linux", "self-hosted"],
            sorted(finding["label"] for finding in refused),
        )
        self.assertRefused(one_job("[ubuntu-latest, macos-latest]"), "runner_hosted_macos")

    # ---- shape 3: mapping group/labels form ------------------------------

    def test_shape_03_mapping_group_labels_form(self) -> None:
        """group+labels resolves through labels; a group with no labels is unresolved."""

        self.assertAccepted(
            one_job("")
            .replace(
                "    runs-on: \n",
                "    runs-on:\n      group: hosted\n      labels: [ubuntu-latest]\n",
            )
        )
        self.assertAccepted(
            one_job("")
            .replace(
                "    runs-on: \n",
                '    runs-on:\n      group: hosted\n      labels: "ubuntu-24.04"\n',
            )
        )
        self.assertRefused(
            one_job("").replace("    runs-on: \n", "    runs-on:\n      group: hosted\n"),
            "runner_unresolved",
        )
        self.assertRefused(
            one_job("")
            .replace(
                "    runs-on: \n",
                "    runs-on:\n      group: hosted\n      labels: [self-hosted]\n",
            ),
            "runner_self_hosted",
        )

    # ---- shape 4: matrix -------------------------------------------------

    def test_shape_04_matrix_key_resolves_through_matrix_and_include(self) -> None:
        """The top-level list AND every include entry carrying the key are checked."""

        def with_matrix(matrix: str) -> str:
            return one_job("${{ matrix.os }}", extra=f"    strategy:\n      matrix:\n{matrix}")

        self.assertAccepted(with_matrix("        os: [ubuntu-latest]\n"))
        self.assertAccepted(with_matrix("        os: [ubuntu-22.04, ubuntu-24.04]\n"))

        refused = self.assertRefused(
            with_matrix("        os: [ubuntu-latest, macos-latest]\n"),
            "runner_hosted_macos",
        )
        self.assertEqual(["macos-latest"], [finding["label"] for finding in refused])
        self.assertEqual(["${{ matrix.os }}"], [finding["runs_on"] for finding in refused])

        include_refused = self.assertRefused(
            with_matrix(
                "        os: [ubuntu-latest]\n"
                "        include:\n"
                "          - os: macos-14\n"
                "            flavour: fruit\n"
            ),
            "runner_hosted_macos",
        )
        self.assertEqual(["macos-14"], [finding["label"] for finding in include_refused])

        # No matrix at all, and a matrix that is itself an expression: UNRESOLVED.
        self.assertRefused(one_job("${{ matrix.os }}"), "runner_unresolved")
        self.assertRefused(
            with_matrix("        os: ${{ fromJSON(vars.OS_MATRIX) }}\n"),
            "runner_unresolved",
        )
        self.assertRefused(with_matrix("        arch: [x64]\n"), "runner_unresolved")

    # ---- shape 5: expression carrying a JSON array literal ---------------

    def test_shape_05_expression_json_array_literals_are_extracted(self) -> None:
        """An inline default IS in the file, so its labels are read and checked."""

        self.assertAccepted(one_job("${{ fromJSON('[\"ubuntu-latest\"]') }}"))
        self.assertAccepted(
            one_job("${{ fromJSON(vars.RUNNER_LINUX || '[\"ubuntu-latest\"]') }}")
        )
        refused = self.assertRefused(
            one_job(
                "${{ fromJSON(vars.RUNNER_MACOS || "
                "'[\"self-hosted\",\"macOS\",\"floati-mac\"]') }}"
            ),
            "runner_self_hosted",
        )
        self.assertEqual(
            ["floati-mac", "macOS", "self-hosted"],
            sorted(finding["label"] for finding in refused),
        )

    # ---- shape 6: a variable with no inline default ----------------------

    def test_shape_06_fromjson_variable_without_inline_default_is_unresolved(self) -> None:
        """The value lives in repository settings, not in the file: refuse."""

        refused = self.assertRefused(
            one_job("${{ fromJSON(vars.FLOATI_CI_RUNNER_LINUX) }}"), "runner_unresolved"
        )
        self.assertEqual(1, len(refused))
        self.assertIn("vars.FLOATI_CI_RUNNER_LINUX", refused[0]["runs_on"])
        self.assertRefused(one_job("${{ vars.RUNNER }}"), "runner_unresolved")
        self.assertRefused(one_job("${{ env.RUNNER }}"), "runner_unresolved")

    # ---- shape 7: event-split expression ---------------------------------

    def test_shape_07_event_split_expression_checks_both_branches(self) -> None:
        """Both arms of a cond && A || B expression are labels, and both are read."""

        self.assertAccepted(
            one_job(
                "${{ github.event_name == 'pull_request' "
                "&& fromJSON('[\"ubuntu-latest\"]') "
                "|| fromJSON('[\"ubuntu-24.04\"]') }}"
            )
        )
        refused = self.assertRefused(
            one_job(
                "${{ github.event_name == 'pull_request' "
                "&& fromJSON('[\"ubuntu-latest\"]') "
                "|| fromJSON('[\"self-hosted\",\"macOS\"]') }}"
            ),
            "runner_self_hosted",
        )
        self.assertEqual(
            ["macOS", "self-hosted"],
            sorted(finding["label"] for finding in refused),
        )

    # ---- shape 8: a bare quoted label inside an expression ---------------

    def test_shape_08_bare_quoted_label_inside_expression_is_checked(self) -> None:
        """The round-1 bypass: a label need not be wrapped in fromJSON to be a label."""

        self.assertAccepted(
            one_job("${{ github.event_name == 'push' && 'ubuntu-latest' || 'ubuntu-22.04' }}")
        )
        refused = self.assertRefused(
            one_job("${{ github.event_name == 'push' && 'ubuntu-latest' || 'macos-latest' }}"),
            "runner_hosted_macos",
        )
        self.assertEqual(["macos-latest"], [finding["label"] for finding in refused])

    # ---- shape 9: quoted event tokens vs other quoted tokens -------------

    def test_shape_09_known_event_tokens_ignored_other_tokens_unresolved(self) -> None:
        """Exactly the five event names are non-labels; anything else is unresolved."""

        for event in (
            "merge_group",
            "pull_request",
            "push",
            "schedule",
            "workflow_dispatch",
        ):
            with self.subTest(event=event):
                self.assertAccepted(
                    one_job(
                        f"${{{{ github.event_name == '{event}' "
                        "&& 'ubuntu-latest' || 'ubuntu-latest' }}"
                    )
                )
        self.assertRefused(
            one_job(
                "${{ github.ref == 'refs/heads/main' && 'ubuntu-latest' || 'ubuntu-latest' }}"
            ),
            "runner_unresolved",
        )
        self.assertRefused(
            one_job("${{ github.event_name == 'release' && 'ubuntu-latest' || 'ubuntu-latest' }}"),
            "runner_label_unknown",
        )

    # ---- shape 10: reusable-workflow caller jobs -------------------------

    def test_shape_10_reusable_workflow_caller_jobs(self) -> None:
        """A local callee is scanned on its own; an EXTERNAL callee is refused."""

        caller = (
            "name: probe\n"
            "on:\n"
            "  push:\n"
            "    branches: [main]\n"
            "jobs:\n"
            "  probe-job:\n"
            "    uses: {target}\n"
        )
        self.assertAccepted(
            caller.format(target="./.github/workflows/release-gate.yml"),
            local_workflows=frozenset({".github/workflows/release-gate.yml"}),
        )
        self.assertRefused(
            caller.format(target="./.github/workflows/absent.yml"),
            "runner_unresolved",
            local_workflows=frozenset({".github/workflows/release-gate.yml"}),
        )
        self.assertRefused(
            caller.format(target="other-org/other-repo/.github/workflows/ci.yml@v1"),
            "runner_external_reusable_workflow",
        )
        self.assertRefused(
            one_job("").replace("    runs-on: \n", ""), "runner_absent"
        )

    # ---- shape 11: unparseable, non-mapping, empty population ------------

    def test_shape_11_unparseable_nonmapping_and_empty_population_refuse(self) -> None:
        """An empty population satisfies every must-be-zero, so it is a refusal."""

        self.assertRefused("name: probe\n\tjobs: broken\n", "workflow_unparsed")
        self.assertRefused("", "workflow_not_a_mapping")
        self.assertRefused("# only a comment\n", "workflow_not_a_mapping")
        self.assertRefused("- a\n- b\n", "workflow_not_a_mapping")
        self.assertRefused("name: probe\non:\n  push:\n", "workflow_jobs_invalid")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertEqual(
                ["workflows_directory_empty"],
                self.reasons(scan_tree(root)),
                "a projection with no workflows directory must refuse, not pass",
            )
            (root / ".github" / "workflows").mkdir(parents=True)
            self.assertEqual(
                ["workflows_directory_empty"], self.reasons(scan_tree(root))
            )
            (root / ".github" / "workflows" / "ok.yml").write_text(
                one_job("ubuntu-latest"), encoding="utf-8"
            )
            self.assertEqual([], scan_tree(root))

    # ---- shape 12: a double-quoted literal inside an expression ----------

    def test_shape_12_double_quoted_literal_in_expression_is_refused(self) -> None:
        """GitHub's grammar rejects it and the extractor cannot see it: refuse outright."""

        refused = self.assertRefused(
            one_job('${{ github.event_name == "push" && \'ubuntu-latest\' || \'ubuntu-latest\' }}'),
            "runner_expression_double_quoted_literal",
        )
        self.assertEqual(1, len(refused))

    # ---- the two files this row exists for -------------------------------

    def test_the_481afae4_harbor_selftest_is_refused_by_name(self) -> None:
        """The projection that carried a self-hosted macOS runner into a fork-PR repo."""

        findings = scan_workflow_source(
            ".github/workflows/selftest.yml", HARBOR_SELFTEST_481AFAE4
        )
        self.assertTrue(findings, "the harbor's own CI must not pass the public fence")
        self.assertEqual(
            ["macos-selftest"], sorted({finding["job"] for finding in findings})
        )
        self.assertEqual(
            ["floati-mac", "macOS", "self-hosted"],
            sorted(finding["label"] for finding in findings),
        )
        self.assertIn("runner_self_hosted", self.reasons(findings))

    def test_the_projected_public_selftest_is_accepted(self) -> None:
        """The inert public workflow this repository authors must pass its own fence."""

        relative = ".github/workflows/selftest.yml"
        text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
        self.assertEqual([], scan_workflow_source(relative, text))
        self.assertIn("AUTHORED ON THE HARBOR", text)
        self.assertIn("github.repository == 'Land-o-Clusters/floati'", text)

    def test_every_workflow_in_this_tree_passes_or_is_private(self) -> None:
        """The harbor's own tree is scanned, so a new unhosted workflow reds here."""

        findings = scan_tree(REPOSITORY_ROOT)
        private = {".github/workflows/harbor-selftest.yml"}
        self.assertEqual(
            [],
            [finding for finding in findings if finding["path"] not in private],
        )

    def test_the_renamed_harbor_workflow_is_the_481afae4_bytes(self) -> None:
        """The harbor's live CI is the 481afae4 bytes under three NAMED substitutions."""

        relative = ".github/workflows/harbor-selftest.yml"
        require_private_artifact(self, relative)
        live = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")

        # Every substitution is asserted from BOTH sides before it is applied:
        # the 481afae4 text is in the fixture and ABSENT from the live file, and
        # the live text is present. A replace() whose needle has drifted away is
        # a silent no-op, so a pin built out of replace() alone goes green while
        # measuring nothing.
        # self.fail() rather than assertIn/assertNotIn on purpose: those print
        # the whole 6 KB haystack and bury the sentence that says WHICH
        # substitution moved. The first line of the literal is locator enough.
        for old, new, what in HARBOR_SUBSTITUTIONS:
            if old not in HARBOR_SELFTEST_481AFAE4:
                self.fail(
                    f"{what}: this pin substitutes text absent from the 481afae4"
                    f" fixture, beginning {old.splitlines()[0]!r}"
                )
            if new not in live:
                self.fail(
                    f"{what}: the live {relative} no longer carries the text this"
                    f" pin names, beginning {new.splitlines()[0]!r}"
                )
            if old in live:
                self.fail(
                    f"{what}: the 481afae4 text is BACK in the live {relative},"
                    f" beginning {old.splitlines()[0]!r}"
                )

        restored = live
        for old, new, _what in HARBOR_SUBSTITUTIONS:
            restored = restored.replace(new, old, 1)
        self.assertEqual(HARBOR_SELFTEST_481AFAE4, restored)


if __name__ == "__main__":
    unittest.main()
