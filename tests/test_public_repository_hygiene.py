from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TAXONOMY = (
    "area:board",
    "area:bus",
    "area:docs",
    "area:doctor",
    "area:ledger",
    "area:orchestrate",
    "area:replay",
    "area:roles",
    "area:schemas",
    "area:wake",
    "blocked",
    "needs-repro",
    "receipts",
    "ruled",
    "security",
    "tech-debt",
)


class PublicRepositoryHygieneTests(unittest.TestCase):
    def load_json_yaml(self, relative: str) -> Any:
        path = REPOSITORY_ROOT / relative
        self.assertTrue(path.is_file(), f"missing repository hygiene surface: {relative}")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_case_law_explains_private_workshop_citations_exactly(self) -> None:
        """The record-link waiver must remain visible to a public reader."""

        expected = (
            "Some dated records here cite files that live in the private workshop they "
            "were written in. Those citations are kept as written — rewriting history "
            "to tidy its links would forge it. A link that does not resolve in this "
            "repository is a pointer into the workshop, not a missing file."
        )
        case_law = (REPOSITORY_ROOT / "docs/CASE-LAW.md").read_text(encoding="utf-8")

        self.assertEqual(1, case_law.count(expected))

    def test_automatic_labels_are_drawn_from_the_live_taxonomy(self) -> None:
        """Removing taxonomy membership or inventing an automatic label is rejected."""

        taxonomy = self.load_json_yaml(".github/labels.v0.json")
        mappings = self.load_json_yaml(".github/labeler.yml")

        self.assertEqual(0, taxonomy["schema_version"])
        self.assertEqual(list(EXPECTED_TAXONOMY), taxonomy["labels"])
        self.assertTrue(mappings)
        self.assertLessEqual(set(mappings), set(EXPECTED_TAXONOMY))
        self.assertEqual(
            {"blocked", "needs-repro", "tech-debt"},
            set(EXPECTED_TAXONOMY) - set(mappings),
        )
        for label, rules in mappings.items():
            with self.subTest(label=label):
                self.assertIsInstance(rules, list)
                self.assertTrue(rules)
                for rule in rules:
                    self.assertEqual({"any-glob-to-any-file"}, set(rule))
                    self.assertTrue(rule["any-glob-to-any-file"])

    def test_labeler_never_executes_pull_request_code(self) -> None:
        """Adding checkout or broader permissions to pull_request_target is rejected."""

        workflow = self.load_json_yaml(".github/workflows/labeler.yml")

        self.assertEqual(
            {"pull_request_target": {"types": ["opened", "synchronize", "reopened"]}},
            workflow["on"],
        )
        self.assertEqual(
            {"contents": "read", "pull-requests": "write"},
            workflow["permissions"],
        )
        steps = workflow["jobs"]["label-pull-request"]["steps"]
        self.assertEqual(1, len(steps))
        self.assertEqual("actions/labeler@v5", steps[0]["uses"])
        self.assertEqual(
            {
                "repo-token": "${{ secrets.GITHUB_TOKEN }}",
                "configuration-path": ".github/labeler.yml",
                "sync-labels": "true",
            },
            steps[0]["with"],
        )

    def test_narrow_intake_routes_only_to_committed_posture_files(self) -> None:
        """Disabling issues or routing around the ruled files is rejected."""

        config = self.load_json_yaml(".github/ISSUE_TEMPLATE/config.yml")

        self.assertIs(config["blank_issues_enabled"], True)
        urls = [entry["url"] for entry in config["contact_links"]]
        self.assertIn("https://github.com/Land-o-Clusters/floati/security/policy", urls)
        for url in urls:
            self.assertTrue(
                url.startswith("https://github.com/Land-o-Clusters/"),
                f"contact link routes off the governed org: {url}",
            )
        pull_request = (REPOSITORY_ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("../SECURITY.md", pull_request)
        self.assertIn("../CONTRIBUTING.md", pull_request)
        self.assertNotIn("DRAFT", pull_request)
        self.assertTrue((REPOSITORY_ROOT / "CONTRIBUTING.md").is_file())
        self.assertTrue((REPOSITORY_ROOT / "SECURITY.md").is_file())

    def test_release_gate_runs_full_selftest_and_public_fences(self) -> None:
        """Dropping a platform or allowing fence success without selftest is rejected."""

        workflow = self.load_json_yaml(".github/workflows/release-gate.yml")

        self.assertEqual(
            {"pull_request": {}, "workflow_call": {}, "workflow_dispatch": {}},
            workflow["on"],
        )
        self.assertEqual({"contents": "read"}, workflow["permissions"])
        selftest = workflow["jobs"]["full-selftest"]
        self.assertEqual(
            "github.repository == 'Land-o-Clusters/floati'",
            selftest["if"],
        )
        self.assertEqual(
            {"fail-fast": False, "matrix": {"os": ["ubuntu-latest", "macos-latest"]}},
            selftest["strategy"],
        )
        self.assertEqual("${{ matrix.os }}", selftest["runs-on"])
        self.assertEqual({"PYTHONDONTWRITEBYTECODE": "1"}, selftest["env"])
        self.assertEqual(
            ["actions/checkout@v4", "actions/setup-python@v5"],
            [step["uses"] for step in selftest["steps"] if "uses" in step],
        )
        self.assertEqual(
            ["python3 -m floati.selftest"],
            [step["run"] for step in selftest["steps"] if "run" in step],
        )

        fences = workflow["jobs"]["public-fences"]
        self.assertEqual(
            "github.repository == 'Land-o-Clusters/floati'",
            fences["if"],
        )
        self.assertEqual("full-selftest", fences["needs"])
        self.assertEqual("ubuntu-latest", fences["runs-on"])
        self.assertEqual({"PYTHONDONTWRITEBYTECODE": "1"}, fences["env"])
        self.assertEqual(
            [
                "python3 -c 'from pathlib import Path; from floati.scrub import scan_generated_tree; hits=scan_generated_tree(Path.cwd()); print(hits); raise SystemExit(bool(hits))'",
                "python3 scripts/public_name_fence.py .",
                "python3 -m floati.release --repository-root .",
            ],
            [step["run"] for step in fences["steps"] if "run" in step],
        )

    def test_export_workflow_is_harbor_only_sole_writer(self) -> None:
        """Broad dispatch, public lane pushes, or non-merge handoff are rejected."""

        workflow_path = REPOSITORY_ROOT / ".github/workflows/export-public.yml"
        policy_path = REPOSITORY_ROOT / ".github/public-export-policy.v0.json"
        if not workflow_path.exists():
            self.assertFalse(policy_path.exists())
            self.assertFalse(
                (REPOSITORY_ROOT / "scripts/prepare_public_export.py").exists()
            )
            return
        workflow = self.load_json_yaml(".github/workflows/export-public.yml")

        self.assertEqual({"workflow_dispatch": {}}, workflow["on"])
        self.assertEqual({"contents": "read"}, workflow["permissions"])
        job = workflow["jobs"]["export-public"]
        self.assertEqual(
            (
                "github.repository == 'Land-o-Clusters/floati-harbor' && "
                "github.ref == 'refs/heads/main'"
            ),
            job["if"],
        )
        self.assertEqual("ubuntu-latest", job["runs-on"])
        self.assertEqual({"PYTHONDONTWRITEBYTECODE": "1"}, job["env"])

        steps = job["steps"]
        checkouts = [step for step in steps if step.get("uses") == "actions/checkout@v4"]
        self.assertEqual(2, len(checkouts))
        self.assertEqual("harbor", checkouts[0]["with"]["path"])
        self.assertEqual("${{ github.sha }}", checkouts[0]["with"]["ref"])
        self.assertEqual("Land-o-Clusters/floati", checkouts[1]["with"]["repository"])
        self.assertEqual("public", checkouts[1]["with"]["path"])
        self.assertEqual("main", checkouts[1]["with"]["ref"])
        self.assertEqual(
            "${{ secrets.FLOATI_PUBLIC_EXPORT_TOKEN }}",
            checkouts[1]["with"]["token"],
        )
        self.assertIn(
            "actions/upload-artifact@v4",
            [step.get("uses") for step in steps],
        )

        runs = "\n".join(step["run"] for step in steps if "run" in step)
        for step in steps:
            if "run" not in step:
                continue
            checked = subprocess.run(
                ["bash", "-n", "-c", step["run"]],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, checked.returncode, checked.stderr)
        self.assertIn("harbor/scripts/export_public.py", runs)
        self.assertIn("harbor/scripts/prepare_public_export.py", runs)
        self.assertIn("export/harbor-", runs)
        self.assertIn("git -C public push", runs)
        self.assertIn("gh pr create", runs)
        self.assertIn("gh pr edit", runs)
        self.assertIn("gh pr merge --merge", runs)
        for forbidden in ("--squash", "--rebase", "gh release", "git tag"):
            self.assertNotIn(forbidden, runs)


if __name__ == "__main__":
    unittest.main()
