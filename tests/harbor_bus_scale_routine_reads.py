"""Native read-workload correctness, never performance qualification."""
from __future__ import annotations

import copy
from tests.bus_scale_test_profile import clone_fixture
import base64
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import stat
import tempfile
import unittest
from tests.bus_scale_test_profile import StableSourceFixture

from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import full_scale_enabled, load_fixture_module, bounded_cli_parser, stable_fixture_sources


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tests/fixtures/bus_scale_census/bus-scale-routine-reads-2026-10-15.py"
QUOTA_PROJECTION = ROOT / "tests/fixtures/bus-scale-census/native-codex-quota-2026-10-15.json"
QUOTA_PROJECTION_SHA256 = "a391ffe65265dd6f4028a26c8c54c3cff255641f103f37f04f70b699fa626801"
CASES = frozenset({
    "describe", "verify", "overlap-report", "sweep", "sent", "effects", "effect-show",
    "graph", "supervise", "presence-show", "sequencer-status", "work-show",
    "net-route-list", "role-list", "role-show", "role-validate", "quota-show", "chart",
    "chart-timings", "context-status", "context-turnover", "context-policy-show",
    "wake-status", "wake-daemon-status", "watch",
})


def _default_cases():
    from tests.bus_scale_execution_paths import representative_cases
    return sorted(set(representative_cases(CASES)) | {"net-route-list"})


def _fixture_identity(base):
    """Detect live-root edits without restoring or inventing native metadata."""
    return tuple((path.relative_to(base).as_posix(), metadata.st_dev, metadata.st_ino,
                  metadata.st_mode, metadata.st_uid, metadata.st_gid, metadata.st_nlink,
                  metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
                 for path in [base, *sorted(base.rglob("*"))]
                 for metadata in [path.lstat()])


def verification_delta(before, after):
    """Retain every actual native verification write, including empty locks."""
    result = {}
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        old, new = before.get(path, b""), after.get(path)
        if new is None:
            raise AssertionError("native verification removed retained input: " + path)
        if not new and path.endswith(".lock"):
            operation, payload = "create_lock", b""
        elif path.endswith(".json"):
            operation, payload = "replace", new
        else:
            if not new.startswith(old):
                raise AssertionError("native verification rewrote retained input: " + path)
            operation, payload = "append", new[len(old):]
        result[path] = dict(operation=operation, before_bytes=len(old), after_bytes=len(new),
            before_sha256=hashlib.sha256(old).hexdigest(), after_sha256=hashlib.sha256(new).hexdigest(),
            payload_base64=base64.b64encode(payload).decode())
    return result


class RoutineReadWorkloadTests(StableSourceFixture, unittest.TestCase):
    _completed_pairs = {}
    _workspace_template = None

    @classmethod
    def tearDownClass(cls):
        for temporary, _record, _identity in cls._completed_pairs.values():
            temporary.cleanup()
        cls._completed_pairs.clear()

    @contextmanager
    def completed_pair(self, case):
        """Independent copies of genuine completed observations at live roots."""
        cache = type(self)._completed_pairs
        source = tuple((str(path), hashlib.sha256(path.read_bytes()).hexdigest())
                       for path in (HELPER, ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py"))
        key = full_scale_enabled(), case, source
        entry = cache.get(key)
        if entry is not None and _fixture_identity(entry[1]["base"]) != entry[2]:
            cache.pop(key)
            entry[0].cleanup()
            entry = None
        if entry is None:
            temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-")
            try:
                record = self._make_completed_pair(Path(temporary.name).resolve(), case)
                entry = temporary, record, _fixture_identity(record["base"])
                cache[key] = entry
            except BaseException:
                temporary.cleanup()
                raise
        try:
            yield copy.deepcopy(entry[1]) if full_scale_enabled() else clone_fixture(entry[1])
        finally:
            if _fixture_identity(entry[1]["base"]) != entry[2]:
                cache.pop(key, None)
                entry[0].cleanup()

    def assert_preparation_ledgers(self, case, context, protected, prepared):
        if case == "verify":
            self.assertTrue(prepared["events.jsonl"].startswith(protected["events.jsonl"]),
                            "native claim preface must preserve the existing history prefix")
            self.assertEqual(protected["registry/entries.jsonl"], prepared["registry/entries.jsonl"])
        elif case == "net-route-list":
            self.assertEqual(protected["events.jsonl"], prepared["events.jsonl"])
            previous = protected["registry/entries.jsonl"]
            registry = prepared["registry/entries.jsonl"]
            self.assertTrue(registry.startswith(previous))
            self.assertEqual([context["route"]], [json.loads(line) for line in registry[len(previous):].splitlines()])
        else:
            self.assertEqual(protected, prepared, "preparation must preserve the scaled ledgers")

    @stable_fixture_sources()
    def _make_completed_pair(self, base, case):
        from floati import cli
        from tests.fixtures.bus_scale_census.bus_scale_registry_prefix_effects import capture_registry_prefix_input
        from tests.fixtures.bus_scale_census.bus_scale_cursor_prefix_effects import raw_candidate_delta
        helper = self.helper()
        entry = load_fixture_module(ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py",
                                    "routine_read_pair_entry")
        workspace = self.workspace(base)
        seed, bank = self.fixture(base, case)
        protected = {name: (seed / name).read_bytes() for name in
                     ("events.jsonl", "registry/entries.jsonl")}
        context = helper.prepare(case, seed, workspace, bank)
        prepared = {name: (seed / name).read_bytes() for name in protected}
        self.assert_preparation_ledgers(case, context, protected, prepared)
        self.assertEqual(context, json.loads(json.dumps(context)))
        observations = []
        for mode in ("cpu", "diagnostic"):
            home = base / case / mode / "scale-fleet"
            shutil.copytree(seed, home)
            for binding in context["path_bindings"]:
                path = home / binding["relative"]
                value = json.loads(path.read_text())
                target = value
                parts = binding["pointer"].strip("/").split("/")
                for part in parts[:-1]:
                    target = target[int(part)] if isinstance(target, list) else target[part]
                self.assertEqual(str(seed), target[parts[-1]])
                target[parts[-1]] = str(home)
                path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            before = {path.relative_to(home).as_posix(): path.read_bytes()
                      for path in home.rglob("*") if path.is_file()}
            registry_input = capture_registry_prefix_input(home, before)
            output, error = io.StringIO(), io.StringIO()
            begin = datetime.now(timezone.utc)
            argv = entry.arguments(case, home, workspace, context)
            with redirect_stdout(output), redirect_stderr(error), bounded_cli_parser():
                code = cli.main(argv)
            end = datetime.now(timezone.utc)
            artifact = json.loads(output.getvalue())
            self.assertEqual(0, code, artifact)
            self.assertEqual("", error.getvalue())
            after = {path.relative_to(home).as_posix(): path.read_bytes()
                     for path in home.rglob("*") if path.is_file()}
            changes = raw_candidate_delta(before, after)
            witness = helper.witness(case, home, before, artifact, context, {},
                                     registry_prefix_input=registry_input)
            normal = helper.normalize(case, changes, artifact, context, home=home,
                wall_begin=begin, wall_end=end, before_raw_files=before, registry_prefix_input=registry_input)
            observed = dict(home=home, before=before, after=after, begin=begin, end=end,
                code=code, artifact=artifact, delta=changes, normal=normal, registry_input=registry_input,
                witness=witness, argv=argv, mode=mode,
                bounds=dict(home=home, wall_begin=begin, wall_end=end, before_raw_files=before,
                            registry_prefix_input=registry_input))
            observations.append(observed)
        return dict(base=base, workspace=workspace, bank=bank, seed=seed, context=context,
                    observations=observations, primary=observations[-1])

    def assert_completed_positive(self, case, record):
        from tests.bus_scale_workloads import _NATIVE
        helper = self.helper()
        observation = record["primary"]
        home, context = observation["home"], record["context"]
        before_files, after_files = observation["before"], observation["after"]
        artifact, normal = observation["artifact"], observation["normal"]
        saved = copy.deepcopy(artifact)
        self.assertEqual(0, observation["code"], artifact)
        self.assertEqual("ok", artifact["status"])
        if case == "overlap-report":
            self.assertTrue(artifact["evidence"]["signals"])
        witness = helper.witness(case, home, before_files, artifact, context, {},
                                 registry_prefix_input=observation["registry_input"])
        self.assertEqual({"exit_code": 0, "status": "ok", "effect": {
            "kind": _NATIVE[case][2], "count": 1}}, witness)
        self.assertEqual({"artifact", "delta", "normalizations"}, set(normal))
        # A genuinely independent clone can rebuild its inherited registry
        # bank. Authenticate that exact named semantic effect before checking
        # the command's remaining authoritative delta.
        authoritative = dict(normal["delta"])
        cache_summary = "@cache/registry-prefix"
        if cache_summary in authoritative:
            self.assertEqual({"operation": "native_cache_rebuild",
                              "kind": "complete_native_registry_prefix"},
                             authoritative.pop(cache_summary))
        if case == "verify":
            self.assertIn("receipts/verifications.jsonl", authoritative)
        elif case == "chart":
            self.assertIn(cache_summary, normal["delta"])
            self.assertEqual({}, authoritative)
        elif case in {"graph", "sweep"}:
            selected = {"graph": ("effects/acceptance.lock", "runs/events.jsonl.lock"),
                        "sweep": ("state/lane-workspaces.lock",)}[case]
            for relative in selected:
                self.assertNotIn(relative, before_files,
                                 "the fixture must exercise native cold lock creation")
                self.assertEqual(b"", after_files[relative])
                self.assertEqual("create_lock", observation["delta"][relative]["operation"])
            self.assertEqual({relative: observation["delta"][relative] for relative in selected}, authoritative,
                             "exact native coordination lock testimony must remain literal")
        else:
            self.assertEqual({}, authoritative)
        self.assertEqual(saved, artifact)
        for audit in normal["normalizations"]:
            self.assertTrue(audit["pointer"].startswith(("/evidence/", "/delta/")))
        protected = {name: before_files[name] for name in ("events.jsonl", "registry/entries.jsonl")}
        self.assertEqual(protected, {name: (home / name).read_bytes() for name in protected})
        self.assertEqual(after_files, {path.relative_to(home).as_posix(): path.read_bytes()
                                      for path in home.rglob("*") if path.is_file()})
        wrong = copy.deepcopy(artifact)
        wrong["evidence"]["unreviewed_extension"] = "must survive or refuse"
        with self.assertRaises(ValueError):
            helper.witness(case, home, before_files, wrong, context, {},
                           registry_prefix_input=observation["registry_input"])

    @contextmanager
    def verified_observation(self):
        if not full_scale_enabled():
            with self.completed_pair("verify") as record:
                observation = record["primary"]
                yield dict(record, **observation, sent={"claim": record["context"]["claim"]})
            return
        helper = self.helper()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-") as temporary:
            home, workspace, bank, sent = self.verify_fixture(Path(temporary).resolve(), "verify")
            context = helper.prepare("verify", home, workspace, bank)
            before = {path.relative_to(home).as_posix(): path.read_bytes()
                      for path in home.rglob("*") if path.is_file()}
            begin = datetime.now(timezone.utc)
            code, artifact = self.native_verify(home, sent["claim"]["id"])
            end = datetime.now(timezone.utc)
            after = {path.relative_to(home).as_posix(): path.read_bytes()
                     for path in home.rglob("*") if path.is_file()}
            yield dict(home=home, workspace=workspace, bank=bank, sent=sent, context=context,
                       before=before, after=after, begin=begin, end=end, code=code, artifact=artifact)

    def helper(self):
        self.assertTrue(HELPER.is_file(),
                        "missing native read-workload helper for the ruled expanded census")
        return load_fixture_module(HELPER, "routine_read_controls")

    def test_closed_native_read_population_and_required_entry_interfaces(self):
        helper = self.helper()
        self.assertEqual(CASES, helper.CASES)
        for name in ("prepare", "arguments", "witness", "normalize"):
            self.assertTrue(callable(getattr(helper, name, None)), name)

    @staticmethod
    def stock_verify_output(elapsed):
        return b".\n" + b"-" * 70 + b"\nRan 1 test in " + elapsed.encode("ascii") + b"s\n\nOK\n"

    def output_prover(self):
        proof = getattr(self.helper(), "_verify_output_proof", None)
        self.assertTrue(callable(proof), "verification equivalence requires a bounded exact output-hash preimage proof")
        return proof

    def test_verify_output_hash_proof_preserves_zero_and_one_millisecond_transcripts(self):
        prove = self.output_prover()
        canonical = self.stock_verify_output("<bounded-native-unittest-duration>")
        expected = hashlib.sha256(canonical).hexdigest()
        for milliseconds in (0, 1):
            with self.subTest(milliseconds=milliseconds):
                raw = self.stock_verify_output("0.%03d" % milliseconds)
                digest = hashlib.sha256(raw).hexdigest()
                proof = prove(digest, "0.010000")
                self.assertEqual("exact_bounded_native_unittest_output", proof["kind"])
                self.assertEqual(digest, proof["observed_output_sha256"])
                self.assertEqual(milliseconds, proof["elapsed_milliseconds"])
                self.assertEqual(98, proof["transcript_bytes"])
                self.assertEqual(expected, proof["canonical_output_sha256"])

    def test_verify_output_hash_proof_refuses_foreign_or_arbitrary_output(self):
        prove = self.output_prover()
        stock = self.stock_verify_output("0.001")
        for digest in ("0" * 64, hashlib.sha256(stock + b"foreign output\n").hexdigest(),
                       hashlib.sha256(stock.replace(b"OK", b"FAILED")).hexdigest()):
            with self.subTest(digest=digest):
                with self.assertRaisesRegex(ValueError, "verification.*output.*preimage"):
                    prove(digest, "0.010000")

    def test_verify_output_hash_proof_refuses_elapsed_outside_observed_duration_or_deadline(self):
        prove = self.output_prover()
        for elapsed, duration in (("0.020", "0.010000"), ("0.001", "0.000400"),
                                  ("15.001", "15.000000")):
            with self.subTest(elapsed=elapsed, duration=duration):
                with self.assertRaisesRegex(ValueError, "verification.*output.*preimage"):
                    prove(hashlib.sha256(self.stock_verify_output(elapsed)).hexdigest(), duration)

    def test_native_verify_output_hash_audits_cover_complete_artifact_and_receipt(self):
        helper = self.helper()
        with self.verified_observation() as observed:
            home, context = observed["home"], observed["context"]
            before, after = observed["before"], observed["after"]
            begin, end, artifact = observed["begin"], observed["end"], observed["artifact"]
            self.assertEqual(0, observed["code"], artifact)
            saved = copy.deepcopy(artifact)
            changes = verification_delta(before, after) if full_scale_enabled() else observed["delta"]
            normal = helper.normalize("verify", changes, artifact, context,
                home=home, wall_begin=begin, wall_end=end, before_raw_files=before,
                registry_prefix_input=observed.get("registry_input"))
            expected = {"/evidence/output_sha256", "/evidence/measurement/output_sha256",
                        "/delta/receipts~1verifications.jsonl/frames/0/output_sha256",
                        "/delta/receipts~1verifications.jsonl/frames/0/measurement/output_sha256"}
            audits = {row["pointer"]: row for row in normal["normalizations"] if row["pointer"] in expected}
            self.assertEqual(expected, set(audits), "all four complete-artifact hash leaves require reversible proof audits")
            canonical_digest = hashlib.sha256(self.stock_verify_output("<bounded-native-unittest-duration>")).hexdigest()
            for audit in audits.values():
                self.assertEqual(artifact["evidence"]["output_sha256"], audit["before"])
                self.assertEqual(canonical_digest, audit["after"])
            self.assertEqual(saved, artifact, "native output hash must remain literal in retained raw evidence")
            self.assertEqual(after, {path.relative_to(home).as_posix(): path.read_bytes()
                                    for path in home.rglob("*") if path.is_file()})

    def fixture(self, base, case, *, role=True):
        from floati.root import FloatiRoot
        from floati.registry import Registry
        from floati.events import EventLog
        home = base / case / "scale-fleet"
        home.mkdir(parents=True, mode=0o700)
        root = FloatiRoot.open_direct_home(home)
        for node in ("scale-worker", "architect"):
            Registry(root).register(node, "codex")
        if role:
            from floati.role_assignment import RoleStepWizard
            from floati.role_library import RoleTemplateLibrary
            from floati.admin_registry import RegistryAdminBackend
            from floati.ids import uuid7_hex
            RoleStepWizard(root, RegistryAdminBackend(root), RoleTemplateLibrary(root).templates(),
                id_factory=uuid7_hex).assign_from_keys(
                    ["scale-worker", "builder", "floati", "foreign", "architect"], io.StringIO())
        EventLog(root).send("scale-worker", "architect", "floati", "a" * 40,
            "docs/evidence/native-read.md", "common native prefix", idempotency_key="prefix",
            now=datetime.now(timezone.utc))
        bank = base / case / "bank"
        bank.mkdir(mode=0o700)
        return home, bank

    def workspace(self, base):
        """Independent Git roots from immutable genuinely authored bytes.

        Only the default fixture authoring is shared. Each use writes new
        ordinary object, index and working-tree files; their new identities
        are bound by the subsequent native operations and proofs. No template
        directory survives authoring and no ctime is restored or invented.
        """
        if full_scale_enabled():
            return self._fresh_workspace(base)
        cls=type(self)
        if cls._workspace_template is None:
            with tempfile.TemporaryDirectory(prefix="bus-scale-read-git-template-",dir=REAL_TEMP_ROOT) as directory:
                template=self._fresh_workspace(Path(directory).resolve())
                entries=[]
                for path in sorted(template.rglob("*")):
                    info=path.lstat()
                    is_directory=stat.S_ISDIR(info.st_mode)
                    if (info.st_uid!=os.getuid() or not (is_directory or stat.S_ISREG(info.st_mode))
                            or not is_directory and info.st_nlink!=1):
                        raise AssertionError("native read Git template contains an alias or shared/foreign file")
                    entries.append((path.relative_to(template).as_posix(),is_directory,
                        stat.S_IMODE(info.st_mode),info.st_mtime_ns,
                        None if is_directory else path.read_bytes()))
                immutable=tuple(entries)
            cls._workspace_template=immutable
        workspace=base/"workspace"
        workspace.mkdir(mode=0o700)
        for relative,is_directory,mode,_mtime_ns,raw in cls._workspace_template:
            path=workspace/relative
            if is_directory:
                path.mkdir(mode=mode,parents=True,exist_ok=True)
            else:
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(raw)
            path.chmod(mode)
        # Child creation changes parent mtimes, so restore those last.
        for relative,_is_directory,_mode,mtime_ns,_raw in reversed(cls._workspace_template):
            path=workspace/relative
            os.utime(path,ns=(path.stat().st_atime_ns,mtime_ns))
        return workspace

    def _fresh_workspace(self, base):
        from floati.git_process import fixed_git_command, fixed_git_environment
        workspace = base / "workspace"
        workspace.mkdir(mode=0o700)
        for argv in (("init", "--quiet"), ("config", "user.name", "Owned Native Fixture"),
                     ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(fixed_git_command("/usr/bin/git", workspace, argv),
                env=fixed_git_environment("/usr/bin/git"), check=True, capture_output=True)
        (workspace / "note.md").write_text("owned native read fixture\n")
        for argv in (("add", "note.md"), ("commit", "--quiet", "-m", "native fixture")):
            subprocess.run(fixed_git_command("/usr/bin/git", workspace, argv),
                env=fixed_git_environment("/usr/bin/git"), check=True, capture_output=True)
        return workspace

    def verify_fixture(self, base, case):
        """Author a real banked test module and native claim before padding."""
        from floati.events import EventLog
        from floati.git_process import fixed_git_command, fixed_git_environment
        from floati.root import FloatiRoot
        workspace = self.workspace(base)
        home, bank = self.fixture(base, case)
        tests = workspace / "tests"
        tests.mkdir(mode=0o700)
        (tests / "__init__.py").write_text("")
        artifact_digest = hashlib.sha256((workspace / "note.md").read_bytes()).hexdigest()
        (tests / "test_census_delivery.py").write_text(
            "import hashlib\nfrom pathlib import Path\nimport unittest\n\n"
            "class DeliveryArtifact(unittest.TestCase):\n"
            "    def test_owned_artifact(self):\n"
            "        self.assertEqual(" + repr(artifact_digest) + ", "
            "hashlib.sha256(Path('note.md').read_bytes()).hexdigest())\n")
        for argv in (("add", "tests"), ("commit", "--quiet", "-m", "Native delivery bank")):
            subprocess.run(fixed_git_command("/usr/bin/git", workspace, argv),
                           env=fixed_git_environment("/usr/bin/git"), check=True, capture_output=True)
        sha = subprocess.run(fixed_git_command("/usr/bin/git", workspace, ("rev-parse", "HEAD")),
                             env=fixed_git_environment("/usr/bin/git"), check=True,
                             capture_output=True, text=True).stdout.strip()
        subprocess.run(fixed_git_command("/usr/bin/git", workspace,
                       ("update-ref", "refs/remotes/origin/main", sha)),
                       env=fixed_git_environment("/usr/bin/git"), check=True, capture_output=True)
        payload = dict(kind="delivery_claim", schema_version=0, sha=sha, repo_path=str(workspace),
            bank=["tests.test_census_delivery"], declared=dict(ran=1, result="OK"),
            artifacts=[dict(path="note.md", sha256=artifact_digest)], deadline_seconds=15)
        sent = EventLog(FloatiRoot.open_direct_home(home)).send(
            "scale-worker", "architect", "floati", sha, "note.md", "native verified delivery",
            idempotency_key="native-verification-control", claim=payload)
        return home, workspace, bank, sent

    def native_verify(self, home, claim_id):
        from floati import cli
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["verify", "--root", str(home), "--as", "architect",
                             "--claim", claim_id, "--json"])
        self.assertEqual("", stderr.getvalue())
        return code, json.loads(stdout.getvalue())

    def test_native_verify_claim_reproduces_actual_banked_artifact_and_receipts_success(self):
        from floati.events import EventLog
        from floati.jsonl import read_records
        from floati.root import FloatiRoot
        with self.verified_observation() as observed:
            home, sent = observed["home"], observed["sent"]
            code, artifact = observed["code"], observed["artifact"]
            self.assertEqual(0, code, artifact)
            self.assertEqual("ok", artifact["status"])
            evidence = artifact["evidence"]
            evidence.pop("scope", None)
            self.assertEqual("verified_match", evidence["outcome"])
            self.assertEqual(sent["claim"], evidence["claim"])
            self.assertEqual(dict(ran=1, result="OK"), evidence["measurement"]["declared"])
            self.assertEqual([dict(row, present=True) for row in sent["claim"]["artifacts"]],
                             evidence["measurement"]["artifacts"])
            self.assertEqual(0, evidence["measurement"]["returncode"])
            self.assertEqual(evidence["measurement"]["output_sha256"], evidence["output_sha256"])
            self.assertEqual(True, evidence["scratch"]["created"])
            self.assertEqual(True, evidence["scratch"]["destroyed"])
            self.assertFalse(Path(evidence["scratch"]["path"]).exists())
            root = FloatiRoot.open_direct_home(home)
            self.assertEqual([evidence], read_records(root, "receipts/verifications.jsonl",
                                                     allowed_kinds={"verification_receipt"}))
            self.assertIn(sent["claim"], EventLog(root).event_records())

    def test_verify_adapter_routes_native_claim_and_refuses_forged_success_testimony(self):
        helper = self.helper()
        from floati import cli
        from floati.events import EventLog
        from floati.root import FloatiRoot
        with self.verified_observation() as observed:
            home, workspace, bank = observed["home"], observed["workspace"], observed["bank"]
            context, before = observed["context"], observed["before"]
            code, artifact = observed["code"], observed["artifact"]
            self.assertIn("verify", helper.CASES,
                          "expanded read adapter must route genuine native delivery verification")
            claim = context["claim"]
            self.assertIn(claim, EventLog(FloatiRoot.open_direct_home(home)).event_records())
            self.assertEqual(str(workspace), claim["repo_path"])
            self.assertEqual(["tests.test_census_delivery"], claim["bank"])
            self.assertEqual("verify", helper.arguments("verify", home, workspace, context)[0])
            self.assertEqual(0, code, artifact)
            self.assertEqual("verified_match", artifact["evidence"]["outcome"])
            self.assertEqual({"exit_code": 0, "status": "ok", "effect": {
                "kind": "delivery_claim_verified", "count": 1}},
                helper.witness("verify", home, before, artifact, context, {}))
            for field in ("claim", "measurement", "repo_path", "outcome"):
                with self.subTest(field=field):
                    wrong = copy.deepcopy(artifact)
                    if field == "claim": wrong["evidence"]["claim"]["declared"]["ran"] += 1
                    elif field == "measurement": wrong["evidence"]["measurement"]["artifacts"][0]["sha256"] = "0" * 64
                    elif field == "repo_path": wrong["evidence"]["repo_path"] = str(bank)
                    else: wrong["evidence"]["outcome"] = "verified_mismatch"
                    with self.assertRaises(ValueError):
                        helper.witness("verify", home, before, wrong, context, {})

    def test_verify_native_unknown_claim_foreign_root_and_unbanked_git_never_count_as_work(self):
        from floati.git_process import fixed_git_command, fixed_git_environment
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-") as temporary:
            base = Path(temporary).resolve()
            home, workspace, bank, sent = self.verify_fixture(base, "verify-refusals")
            foreign, _bank = self.fixture(base, "foreign-verification-root")
            for selected_home, claim_id in ((home, "delivery-claim-" + "0" * 32),
                                             (foreign, sent["claim"]["id"])):
                code, artifact = self.native_verify(selected_home, claim_id)
                self.assertEqual(20, code, artifact)
                self.assertEqual("claim_unknown", artifact["evidence"]["code"])
            subprocess.run(fixed_git_command("/usr/bin/git", workspace,
                           ("update-ref", "-d", "refs/remotes/origin/main")),
                           env=fixed_git_environment("/usr/bin/git"), check=True, capture_output=True)
            code, artifact = self.native_verify(home, sent["claim"]["id"])
            self.assertEqual(20, code, artifact)
            self.assertEqual("verification_unrunnable", artifact["evidence"]["outcome"])
            self.assertEqual("sha_unbanked", artifact["evidence"]["code"])
            self.assertFalse(artifact["evidence"]["scratch"]["created"])
            helper = self.helper()
            self.assertIn("verify", helper.CASES,
                          "typed native refusals cannot substitute for the missing positive verify adapter")

    def test_native_cli_reads_positive_work_and_preserves_scaled_ledger_bytes(self):
        if not full_scale_enabled():
            for case in sorted(_default_cases()):
                with self.subTest(case=case), self.completed_pair(case) as record:
                    self.assert_completed_positive(case, record)
            return
        helper = self.helper()
        from floati import cli
        from tests.bus_scale_workloads import _NATIVE
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-") as temporary:
            base = Path(temporary).resolve()
            workspace = self.workspace(base)
            for case in sorted(CASES):
                with self.subTest(case=case):
                    home, bank = self.fixture(base, case)
                    protected = {name: (home / name).read_bytes() for name in
                                 ("events.jsonl", "registry/entries.jsonl")}
                    context = helper.prepare(case, home, workspace, bank)
                    prepared = {name: (home / name).read_bytes() for name in protected}
                    self.assert_preparation_ledgers(case, context, protected, prepared)
                    protected = prepared
                    self.assertEqual(context, json.loads(json.dumps(context)))
                    before_files = {path.relative_to(home).as_posix(): path.read_bytes()
                                    for path in home.rglob("*") if path.is_file()}
                    from tests.fixtures.bus_scale_census.bus_scale_registry_prefix_effects import capture_registry_prefix_input
                    registry_input = capture_registry_prefix_input(home, before_files)
                    stdout, stderr = io.StringIO(), io.StringIO()
                    before = datetime.now(timezone.utc)
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = cli.main(helper.arguments(case, home, workspace, context))
                    after = datetime.now(timezone.utc)
                    artifact = json.loads(stdout.getvalue())
                    self.assertEqual(0, code, artifact)
                    self.assertEqual("ok", artifact["status"])
                    self.assertEqual("", stderr.getvalue())
                    if case == "overlap-report":
                        self.assertTrue(artifact["evidence"]["signals"], "overlap fixture must compare actual edited refs")
                    witness = helper.witness(case, home, before_files, artifact, context, {},
                                             registry_prefix_input=registry_input)
                    self.assertEqual({"exit_code": 0, "status": "ok", "effect": {
                        "kind": _NATIVE[case][2], "count": 1}}, witness)
                    saved = copy.deepcopy(artifact)
                    after_files = {path.relative_to(home).as_posix(): path.read_bytes()
                                   for path in home.rglob("*") if path.is_file()}
                    delta = verification_delta(before_files, after_files) if case == "verify" else {}
                    if case == "chart":
                        from tests.fixtures.bus_scale_census.bus_scale_cursor_prefix_effects import raw_candidate_delta
                        delta = raw_candidate_delta(before_files, after_files)
                    normal = helper.normalize(case, delta, artifact, context,
                                               home=home, wall_begin=before, wall_end=after,
                                               before_raw_files=before_files, registry_prefix_input=registry_input)
                    self.assertEqual({"artifact", "delta", "normalizations"}, set(normal))
                    if case == "verify":
                        self.assertIn("receipts/verifications.jsonl", normal["delta"])
                    elif case == "chart":
                        self.assertEqual({"@cache/registry-prefix": {
                            "operation": "native_cache_rebuild",
                            "kind": "complete_native_registry_prefix"}}, normal["delta"])
                    else:
                        self.assertEqual({}, normal["delta"])
                    self.assertEqual(saved, artifact)
                    for audit in normal["normalizations"]:
                        self.assertTrue(audit["pointer"].startswith(("/evidence/", "/delta/")))
                    self.assertEqual(protected, {name: (home / name).read_bytes() for name in protected})
                    wrong = copy.deepcopy(artifact)
                    wrong["evidence"]["unreviewed_extension"] = "must survive or refuse"
                    with self.assertRaises(ValueError):
                        helper.witness(case, home, before_files, wrong, context, {},
                                       registry_prefix_input=registry_input)

    def test_unsized_role_preface_is_native_and_preserves_existing_registry_prefix(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-") as temporary:
            base = Path(temporary).resolve()
            workspace = self.workspace(base)
            home, bank = self.fixture(base, "missing-role", role=False)
            before = (home / "registry/entries.jsonl").read_bytes()
            try:
                helper.prepare("context-turnover", home, workspace, bank)
            except helper.UnresolvedRead as error:
                self.fail("ruled unsized native role preface remains unresolved: " + str(error))
            after = (home / "registry/entries.jsonl").read_bytes()
            self.assertTrue(after.startswith(before))
            from floati.admin_registry import RegistryAdminBackend
            from floati.root import FloatiRoot
            role = RegistryAdminBackend(FloatiRoot.open_direct_home(home)).role_record("scale-worker")
            self.assertEqual("builder", role["template_role"])
            self.assertEqual("registry_role_record", role["kind"])

    def test_independent_clones_preserve_complete_audited_native_read_equivalence(self):
        if not full_scale_enabled():
            for case in sorted(_default_cases()):
                with self.subTest(case=case), self.completed_pair(case) as record:
                    results = [row["normal"] for row in record["observations"]]
                    self.assertEqual(results[0]["artifact"], results[1]["artifact"])
                    self.assertEqual(results[0]["delta"], results[1]["delta"])
            return
        helper = self.helper()
        from floati import cli
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-") as temporary:
            base = Path(temporary).resolve()
            workspace = self.workspace(base)
            for case in sorted(CASES):
                with self.subTest(case=case):
                    seed, bank = self.fixture(base, case)
                    context = helper.prepare(case, seed, workspace, bank)
                    results = []
                    for mode in ("cpu", "diagnostic"):
                        home = base / case / mode / "scale-fleet"
                        shutil.copytree(seed, home)
                        # Exact native identity binding exception, outside the
                        # invocation and before the retained before inventory.
                        for binding in context["path_bindings"]:
                            path = home / binding["relative"]
                            value = json.loads(path.read_text())
                            target = value
                            parts = binding["pointer"].strip("/").split("/")
                            for part in parts[:-1]:
                                target = target[int(part)] if isinstance(target, list) else target[part]
                            self.assertEqual(str(seed), target[parts[-1]])
                            target[parts[-1]] = str(home)
                            path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
                        before_files = {path.relative_to(home).as_posix(): path.read_bytes()
                                        for path in home.rglob("*") if path.is_file()}
                        from tests.fixtures.bus_scale_census.bus_scale_registry_prefix_effects import capture_registry_prefix_input
                        from tests.fixtures.bus_scale_census.bus_scale_cursor_prefix_effects import raw_candidate_delta
                        registry_input = capture_registry_prefix_input(home,before_files)
                        stdout, stderr = io.StringIO(), io.StringIO()
                        begin = datetime.now(timezone.utc)
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            code = cli.main(helper.arguments(case, home, workspace, context))
                        end = datetime.now(timezone.utc)
                        artifact = json.loads(stdout.getvalue())
                        self.assertEqual(0, code, artifact)
                        helper.witness(case, home, before_files, artifact, context, {},
                                       registry_prefix_input=registry_input)
                        after_files = {path.relative_to(home).as_posix(): path.read_bytes()
                                       for path in home.rglob("*") if path.is_file()}
                        delta = raw_candidate_delta(before_files,after_files)
                        results.append(helper.normalize(case, delta, artifact, context,
                                                        home=home, wall_begin=begin, wall_end=end,
                                                        before_raw_files=before_files,
                                                        registry_prefix_input=registry_input))
                    self.assertEqual(results[0]["artifact"], results[1]["artifact"])
                    self.assertEqual(results[0]["delta"], results[1]["delta"])

    def test_world_writable_owned_fixture_is_refused_before_preparation(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-") as temporary:
            base = Path(temporary).resolve()
            workspace = self.workspace(base)
            home, bank = self.fixture(base, "permissions")
            base.chmod(0o777)
            try:
                with self.assertRaisesRegex(ValueError, "private"):
                    helper.prepare("describe", home, workspace, bank)
            finally:
                base.chmod(0o700)

    def test_supervise_uses_authoritative_observation_with_native_waiting_and_delivery(self):
        helper = self.helper()
        from floati.events import EventLog
        from floati.root import FloatiRoot
        from floati.supervisor import Supervisor
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-") as temporary:
            base = Path(temporary).resolve()
            workspace = self.workspace(base)
            home, bank = self.fixture(base, "supervise-clock")
            root = FloatiRoot.open_direct_home(home)
            context = helper.prepare("supervise", home, workspace, bank)
            messages, delivery = EventLog(root).present("architect", now=datetime(
                2026, 10, 15, 17, 0, 0, 789000, tzinfo=timezone.utc))
            self.assertEqual(1, len(messages))
            self.assertIsNotNone(delivery, "fixture must author a genuine native delivery")
            EventLog(root).send("architect", "scale-worker", "floati", "b" * 40,
                "docs/evidence/native-waiting-read.md", "genuine waiting mail",
                idempotency_key="supervise-waiting", now=datetime(
                    2026, 10, 15, 17, 30, 0, 789000, tzinfo=timezone.utc))
            # Explicit native projection time is not a replaced clock. Retain
            # sub-millisecond input so derived age cannot silently become the
            # authoritative published observation timestamp.
            observed = datetime(2026, 10, 15, 18, 0, 0, 123456, tzinfo=timezone.utc)
            # Compare clocks within the same native overdue state. Crossing
            # the 15-minute threshold changes a fact, which must still refuse.
            context["canonical_time"] = "2026-10-15T18:00:01.123456+00:00"
            evidence = Supervisor(root).snapshot(observed)
            nodes = {row["node_id"]: row for row in evidence["nodes"]}
            self.assertEqual(0, nodes["architect"]["seat_attention"]["undelivered_count"])
            self.assertEqual(1, nodes["architect"]["message_progress"]["delivered"])
            self.assertEqual(1, nodes["scale-worker"]["seat_attention"]["undelivered_count"])
            self.assertIsNotNone(nodes["scale-worker"]["seat_attention"]["oldest_waiting_at"])
            artifact = dict(artifact_version=0, command="supervise", status="ok", evidence=evidence)
            self.assertEqual("ok", helper.witness("supervise", home, {}, artifact, context, {})["status"])
            authoritative = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
            self.assertEqual(authoritative, helper._observation_time("supervise", evidence),
                "derived waiting age must not replace the native published observation timestamp")
            normal = helper.normalize("supervise", {}, artifact, context, home=home,
                                      wall_begin=observed, wall_end=observed)
            self.assertEqual({"artifact", "delta", "normalizations"}, set(normal))

    def test_waiting_read_equivalence_audits_native_nested_observation_clocks(self):
        helper = self.helper()
        from floati.root import FloatiRoot
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-") as temporary:
            base = Path(temporary).resolve()
            workspace = self.workspace(base)
            for case in ("supervise", "watch"):
                with self.subTest(case=case):
                    home, bank = self.fixture(base, case)
                    context = helper.prepare(case, home, workspace, bank)
                    # Native explicit projection times force a second boundary
                    # without sleeping, replacing clocks or changing evaluators.
                    context["canonical_time"] = "2026-10-15T18:00:00.123000+00:00"
                    observed = datetime(2026, 10, 15, 18, 0, 3, 456789, tzinfo=timezone.utc)
                    evidence = helper._evidence(case, FloatiRoot.open_direct_home(home), context, observed)
                    artifact = dict(artifact_version=0, command=case, status="ok", evidence=evidence)
                    helper.witness(case, home, {}, artifact, context, {})
                    normal = helper.normalize(case, {}, artifact, context, home=home,
                                              wall_begin=observed, wall_end=observed)
                    pointers = {audit["pointer"] for audit in normal["normalizations"]}
                    base_pointer = "/evidence/" + ("delta/snapshot/" if case == "watch" else "")
                    self.assertIn(base_pointer + "nodes/1/oldest_unread/observed_at", pointers)
                    if case == "watch":
                        self.assertIn(base_pointer + "nodes/1/wake_health/observed_at", pointers)
                        self.assertIn(base_pointer + "nodes/1/wake_health/oldest_unread/observed_at", pointers)
                    # These clocks retain microseconds (watch window_end and
                    # supervise's reconstructed native waiting age). Neither
                    # a future observation nor an earlier one gets a second
                    # of unexplained tolerance from chart's integer age.
                    for offset in (-2, 2):
                        with self.subTest(outside_window_milliseconds=offset):
                            boundary = observed + timedelta(milliseconds=offset)
                            with self.assertRaisesRegex(ValueError, "outside its collected invocation"):
                                helper.normalize(case, {}, artifact, context, home=home,
                                    wall_begin=boundary, wall_end=boundary)

    def test_read_normalization_refuses_nonexistent_lock_delta(self):
        self.assert_invalid_lock_deltas({"nonexistent": (False, {})})

    def test_read_normalization_refuses_false_lock_hashes_and_prior_length(self):
        self.assert_invalid_lock_deltas({
            "wrong-after-hash": (True, {"after_sha256": "0" * 64}),
            "wrong-before-hash": (True, {"before_sha256": "0" * 64}),
            "false-prior-length": (True, {"before_bytes": 1}),
        })

    def test_read_normalization_refuses_inexact_lock_delta_fields(self):
        self.assert_invalid_lock_deltas({
            "missing-before-hash": (True, {"before_sha256": None}),
            "unreviewed-field": (True, {"unreviewed_extension": "must refuse"}),
        })

    def test_read_normalization_refuses_false_current_lock_bytes_type_and_permissions(self):
        self.assert_invalid_lock_deltas({
            "nonempty-current-lock": (True, {}),
            "symlink-current-lock": (True, {}),
            "world-writable-current-lock": (True, {}),
        })

    def test_graph_and_sweep_preserve_their_exact_native_coordination_lock_delta(self):
        helper = self.helper()
        from floati import cli
        from tests.fixtures.bus_scale_census.bus_scale_registry_prefix_effects import (
            SUMMARY, TAG, capture_registry_prefix_input,
        )
        selected = {"graph": "effects/acceptance.lock", "sweep": "state/lane-workspaces.lock"}
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-") as temporary:
            base = Path(temporary).resolve()
            workspace = self.workspace(base)
            for case, relative in selected.items():
                with self.subTest(case=case):
                    home, bank = self.fixture(base, case)
                    context = helper.prepare(case, home, workspace, bank)
                    before = {path.relative_to(home).as_posix(): path.read_bytes()
                              for path in home.rglob("*") if path.is_file()}
                    registry_input = capture_registry_prefix_input(home, before)
                    self.assertNotIn(relative, before, "the fixture must exercise native cold lock creation")
                    output, error = io.StringIO(), io.StringIO()
                    begin = datetime.now(timezone.utc)
                    with redirect_stdout(output), redirect_stderr(error):
                        code = cli.main(helper.arguments(case, home, workspace, context))
                    end = datetime.now(timezone.utc)
                    artifact = json.loads(output.getvalue())
                    self.assertEqual(0, code, artifact)
                    self.assertEqual("", error.getvalue())
                    after = {path.relative_to(home).as_posix(): path.read_bytes()
                             for path in home.rglob("*") if path.is_file()}
                    delta = verification_delta(before, after)
                    self.assertIn(relative, delta)
                    self.assertEqual(b"", after[relative])
                    self.assertEqual("create_lock", delta[relative]["operation"])
                    helper.witness(case, home, before, artifact, context, {},
                                   registry_prefix_input=registry_input)
                    normal = helper.normalize(case, delta, artifact, context, home=home,
                        before_raw_files=before, wall_begin=begin, wall_end=end,
                        registry_prefix_input=registry_input)
                    expected_delta = delta if case == "graph" else {relative: delta[relative], SUMMARY: TAG}
                    self.assertEqual(expected_delta, normal["delta"],
                        "exact native lock testimony must remain literal beside the fully proved cache")
                    saved = (home / relative).read_bytes()
                    for label, wrong in (
                        ("wrong-hash", dict(delta[relative], after_sha256="0" * 64)),
                        ("wrong-prior-length", dict(delta[relative], before_bytes=1)),
                        ("extra-field", dict(delta[relative], unreviewed_extension=True)),
                    ):
                        with self.subTest(case=case, malformed=label):
                            with self.assertRaises(ValueError):
                                helper.normalize(case, {relative: wrong}, artifact, context, home=home,
                                                 wall_begin=begin, wall_end=end)
                            self.assertEqual(saved, (home / relative).read_bytes())
                    other = selected["sweep" if case == "graph" else "graph"]
                    for unknown in (other, relative.replace(".lock", "-unreviewed.lock")):
                        with self.subTest(case=case, unselected=unknown):
                            path = home / unknown
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(b"")
                            path.chmod(0o600)
                            with self.assertRaises(ValueError):
                                helper.normalize(case, {unknown: dict(delta[relative])}, artifact, context,
                                                 home=home, wall_begin=begin, wall_end=end)
                            self.assertEqual(b"", path.read_bytes(), "refusal must preserve unreviewed lock bytes")

    def assert_invalid_lock_deltas(self, variants):
        helper = self.helper()
        # The closed lock decoder is shared by these reads. Default poison
        # variants use its bounded direct-state projection; the explicit
        # profile retains the complete parser-description projection.
        case = "describe" if full_scale_enabled() else "sequencer-status"
        from floati import cli
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-") as temporary:
            base = Path(temporary).resolve()
            workspace = self.workspace(base)
            home, bank = self.fixture(base, "lock-delta")
            context = helper.prepare(case, home, workspace, bank)
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(helper.arguments(case, home, workspace, context))
            artifact = json.loads(stdout.getvalue())
            self.assertEqual(0, code, artifact)
            self.assertEqual("", stderr.getvalue())
            self.assertEqual({}, helper.normalize(case, {}, artifact, context, home=home)["delta"])
            empty_digest = hashlib.sha256(b"").hexdigest()
            for name, (exists, changes) in variants.items():
                with self.subTest(case=name):
                    relative = "receipts/" + name + ".jsonl.lock"
                    path = home / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if exists:
                        path.write_bytes(b"")
                        path.chmod(0o600)
                    if name == "nonempty-current-lock":
                        path.write_bytes(b"unreviewed current bytes")
                    elif name == "symlink-current-lock":
                        path.unlink()
                        target = bank / "owned-empty-target"
                        target.write_bytes(b"")
                        path.symlink_to(target)
                    elif name == "world-writable-current-lock":
                        path.chmod(0o666)
                    self.assertEqual(exists, path.exists())
                    change = dict(operation="create_lock", before_bytes=0, after_bytes=0,
                        before_sha256=empty_digest, after_sha256=empty_digest, payload_base64="")
                    change.update(changes)
                    if name == "missing-before-hash":
                        del change["before_sha256"]
                    saved = copy.deepcopy(change)
                    with self.assertRaises(ValueError,
                            msg="raw lock testimony must be bound to exact fields, bytes and hashes: " + name):
                        helper.normalize(case, {relative: change}, artifact, context, home=home)
                    self.assertEqual(saved, change)

    def test_quota_preface_derives_consumption_from_retained_native_usage_projection(self):
        helper = self.helper()
        from floati.quota import QuotaLedger
        from floati.root import FloatiRoot
        provided_raw = QUOTA_PROJECTION.read_bytes()
        self.assertEqual(QUOTA_PROJECTION_SHA256, hashlib.sha256(provided_raw).hexdigest())
        provided = json.loads(provided_raw)
        self.assertEqual("mcp__codex_app__get_usage_limits", provided["source_tool"])
        self.assertEqual(["rateLimitsByLimitId.codex.primary.usedPercent",
                          "rateLimitsByLimitId.codex.primary.windowDurationMins",
                          "rateLimitsByLimitId.codex.primary.resetsAt"], provided["source_fields"])
        expected_fraction = Decimal(str(provided["primary"]["usedPercent"])) / Decimal(100)
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-read-controls-") as temporary:
            base = Path(temporary).resolve()
            workspace = self.workspace(base)
            home, bank = self.fixture(base, "quota-provenance")
            helper.prepare("quota-show", home, workspace, bank)
            record, receipt = QuotaLedger(FloatiRoot.open_direct_home(home)).latest_record("openai_codex")
            self.assertIsNotNone(record)
            self.assertEqual(1, len(receipt.facts))
            fact = receipt.facts[0]
            self.assertEqual("consumed_fraction", fact.state.kind)
            self.assertEqual(expected_fraction, Decimal(fact.state.value),
                "quota workload must derive 13/100 from the retained native provider projection")
            self.assertEqual("DERIVED", fact.stamp,
                "provided native API projection is retained input, not a new measured provider observation")
            self.assertEqual(QUOTA_PROJECTION_SHA256, fact.evidence_digest)
            self.assertIn(provided["source_tool"], fact.source)
            self.assertEqual(datetime.strptime(provided["captured_at"], "%Y-%m-%d %H:%M:%S UTC").replace(
                tzinfo=timezone.utc), datetime.fromisoformat(fact.observed_at.replace("Z", "+00:00")))
            self.assertEqual(datetime.fromtimestamp(provided["primary"]["resetsAt"], timezone.utc),
                             datetime.fromisoformat(fact.resets_at.replace("Z", "+00:00")))
            retained = [path for path in bank.rglob("*.json") if path.read_bytes() == provided_raw]
            self.assertEqual(1, len(retained), "native quota provenance bytes must be retained exactly once")


if __name__ == "__main__":
    unittest.main()
