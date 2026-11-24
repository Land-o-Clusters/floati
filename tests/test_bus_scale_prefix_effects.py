"""Native report effects must be comparable without accepting extra cache files."""
from __future__ import annotations

from contextlib import redirect_stdout
import copy
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import resource
import shutil
import tempfile
import unittest

from floati import cli, send_prefix
from floati.events import EventLog
from floati.root import FloatiRoot
from tests.bus_scale_lifecycle_fixture import HELPER, fixture, load, raw_delta
from tests.bus_scale_test_profile import StableSourceFixture
from tests.temp_roots import REAL_TEMP_ROOT


class NativeWarmCutCompositionTests(StableSourceFixture, unittest.TestCase):
    def test_copied_fixture_warms_and_independently_certifies_only_the_exact_cut_bank(self):
        from floati.registry import Registry
        from tests.bus_scale_census import _warm_fixture
        from tests.fixtures.bus_scale_census.bus_scale_cut_prefix_effects import CACHE
        from tests.fixtures.bus_scale_census.bus_scale_prefix_effects import verify_warmed_clone

        def inventory(home):
            return {path.relative_to(home).as_posix(): path.read_bytes()
                for path in home.rglob("*") if path.is_file()}

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary).resolve()
            seed, source, clone = (base / name / "fleet" for name in ("seed", "source", "clone"))
            root = FloatiRoot.open_direct_home(seed, create=True)
            for node in ("sender", "recipient"):
                Registry(root).register(node, "worker")
            EventLog(root).send("sender", "recipient", "floati", "a" * 40,
                "note.md", "warm composition", idempotency_key="warm-composition")
            shutil.copytree(seed, source)
            unused = _warm_fixture(source, registry=False)
            self.assertIsNone(unused.get("cut_result"),
                "a case that does not consume event cuts must not build their bank")
            self.assertFalse((source / CACHE).exists())
            self.assertEqual((seed / "events.jsonl").read_bytes(), (source / "events.jsonl").read_bytes())
            initial = _warm_fixture(source, registry=False, event_cut=True)
            self.assertIsNotNone(initial.get("cut_result"), "census preflight must explicitly warm the cut bank")
            frozen = inventory(source)
            shutil.copytree(source, clone)
            warmed = _warm_fixture(clone, registry=False)
            proof = verify_warmed_clone(clone, frozen, full_mail=True)
            self.assertEqual("complete_native_cut_warm_clone", proof["cut_warm_proof"]["kind"])
            self.assertEqual(proof["cut_warm_proof"], warmed["full_mail_warm_proof"]["cut_warm_proof"])
            self.assertEqual(frozen["events.jsonl"], (clone / "events.jsonl").read_bytes())
            for relative in (CACHE + "/nodes/" + "f" * 64, ".cut-prefix-evil"):
                with self.subTest(path=relative):
                    target = clone / relative
                    target.write_bytes(b"unexplained warm payload")
                    target.chmod(0o600)
                    try:
                        retained = inventory(clone)
                        with self.assertRaises(ValueError):
                            verify_warmed_clone(clone, frozen, full_mail=True)
                        self.assertEqual(retained, inventory(clone), "warm proof must preserve rejected evidence")
                    finally:
                        target.unlink()
            bank_path = clone / ".send-prefix/bank.bin"
            bank_raw = bank_path.read_bytes()
            header = len(b"FLOATI-SEND-BANK\x00\x01") + 8
            count = int.from_bytes(bank_raw[header - 8:header], "big")
            members = []
            for index in range(count):
                record = bank_raw[header + index * 44:header + (index + 1) * 44]
                offset = int.from_bytes(record[32:40], "big")
                length = int.from_bytes(record[40:], "big")
                members.append((record[:32], bank_raw[offset:offset + length]))
            extra = b'{"unexplained":"orphan-bank-member"}'
            members.append((hashlib.sha256(extra).digest(), extra))
            directory, values = bytearray(), bytearray()
            offset = header + len(members) * 44
            for digest, raw in sorted(members):
                directory.extend(digest + offset.to_bytes(8, "big") + len(raw).to_bytes(4, "big"))
                values.extend(raw)
                offset += len(raw)
            orphan_bank = bank_raw[:header - 8] + len(members).to_bytes(8, "big") + directory + values
            corruptions = {
                "header": b"!" + bank_raw[1:],
                "count": bank_raw[:header - 8] + len(bank_raw).to_bytes(8, "big") + bank_raw[header:],
                "offset": bank_raw[:header + 32] + bytes(8) + bank_raw[header + 40:],
                "hash": bank_raw[:header] + bytes(32) + bank_raw[header + 32:],
                "truncated": bank_raw[:-1],
                "trailing": bank_raw + b"unexplained",
                "orphan": orphan_bank,
            }
            for fault, raw in corruptions.items():
                with self.subTest(bank_fault=fault):
                    bank_path.write_bytes(raw)
                    try:
                        retained = inventory(clone)
                        with self.assertRaises(ValueError):
                            verify_warmed_clone(clone, frozen, full_mail=True)
                        self.assertEqual(retained, inventory(clone), "warm proof repaired rejected compact-bank bytes")
                    finally:
                        bank_path.write_bytes(bank_raw)


class NativePrefixEffectTests(unittest.TestCase):
    def native_report(self, home, workspace, bank, helper, *, future_kind=False):
        context = helper.prepare("node-prep-clear", home, workspace, bank)
        root = FloatiRoot.open_direct_home(home)
        if future_kind:
            from floati.framing import encode_frame
            from floati.ids import uuid7_hex
            with (home / "events.jsonl").open("ab") as stream:
                stream.write(encode_frame(dict(schema_version=3, kind="future_event_kind",
                    id="future-" + uuid7_hex(), tenant_id=root.tenant_id,
                    timestamp=datetime.now(timezone.utc).isoformat(
                        timespec="milliseconds").replace("+00:00", "Z"))))
        sha = (workspace / ".git/refs/remotes/origin/main").read_text().strip()
        EventLog(root).send("scale-worker", "architect", "floati", sha, "note.md", "native prefix input",
            idempotency_key="prefix-effects-input")
        before = helper.snapshot(home)
        prefix_input = {"ledger_identity": send_prefix._identity(home / "events.jsonl"),
            "checkpoint_sha256": hashlib.sha256(before[".send-prefix/checkpoint.json"]).hexdigest()}
        argv = helper.arguments("node-prep-clear", home, workspace, context)
        stream, previous = io.StringIO(), Path.cwd()
        begin = datetime.now(timezone.utc)
        cpu_begin = resource.getrusage(resource.RUSAGE_SELF)
        children_begin = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            os.chdir(workspace)
            with redirect_stdout(stream):
                code = cli.main(argv)
        finally:
            os.chdir(previous)
        end = datetime.now(timezone.utc)
        cpu_end = resource.getrusage(resource.RUSAGE_SELF)
        children_end = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (cpu_end.ru_utime + cpu_end.ru_stime - cpu_begin.ru_utime - cpu_begin.ru_stime
            + children_end.ru_utime + children_end.ru_stime - children_begin.ru_utime - children_begin.ru_stime)
        artifact = json.loads(stream.getvalue())
        self.assertEqual(0, code, artifact)
        self.assertEqual("ok", artifact["status"])
        after = helper.snapshot(home)
        self.assertNotEqual(before[".send-prefix/checkpoint.json"], after[".send-prefix/checkpoint.json"])
        if not future_kind:
            self.assertEqual("prep_clear_receipted", helper.witness("node-prep-clear", home, before, artifact, context, {})["effect"]["kind"])
        bounds = dict(home=home, workspace=workspace, wall_begin=begin, wall_end=end, child_pid=os.getpid(),
            child_cpu_seconds=cpu, before_raw_files=before, prefix_input=prefix_input)
        return before, after, artifact, context, bounds

    def test_real_native_report_cache_effect_is_accepted_without_dropping_raw_delta(self):
        helper = load(HELPER, "prefix_effect_native_positive")
        for future_kind in (False, True):
            with self.subTest(future_kind=future_kind), fixture("node-prep-clear") as (bank, home, workspace):
                before, after, artifact, context, bounds = self.native_report(
                    home, workspace, bank, helper, future_kind=future_kind)
                delta = raw_delta(before, after)
                if future_kind:
                    # The lifecycle census deliberately admits only known
                    # source kinds. Exercise the event bank's independent
                    # native map/effect proof without widening that boundary.
                    from tests.fixtures.bus_scale_census.bus_scale_prefix_effects import verify_prefix_effect
                    proof = verify_prefix_effect(home, before, after, delta, bounds["prefix_input"])
                    self.assertEqual("verified_prefix_append", proof["semantic"]["operation"])
                else:
                    normalized = helper.normalize("node-prep-clear", delta, artifact, context, **bounds)
                    self.assertEqual("verified_prefix_append", normalized["delta"]["@cache/send-prefix"]["operation"])
                self.assertTrue(any(path.startswith(".send-prefix/nodes/") for path in delta))
                self.assertEqual(after, helper.snapshot(home), "read-only effect proof changed native evidence")
                self.assertEqual(raw_delta(before, after), delta, "normalization changed retained complete raw delta")

    def test_native_report_then_extra_content_addressed_node_is_refused(self):
        helper = load(HELPER, "prefix_effect_native_extra_node")
        with fixture("node-prep-clear") as (bank, home, workspace):
            before, after, artifact, context, bounds = self.native_report(home, workspace, bank, helper)
            # The real native positive must pass first: an always-refusing
            # normalizer cannot satisfy this malformed-effect control.
            helper.normalize("node-prep-clear", raw_delta(before, after), artifact, context, **bounds)
            extra = b'{"unreviewed":"extra-content-addressed-node"}'
            target = home / ".send-prefix/nodes" / hashlib.sha256(extra).hexdigest()
            target.write_bytes(extra)
            target.chmod(0o600)
            forged = helper.snapshot(home)
            with self.assertRaisesRegex(ValueError, "exact native insertion delta"):
                helper.normalize("node-prep-clear", raw_delta(before, forged), artifact, context, **bounds)
            self.assertEqual(forged, helper.snapshot(home), "rejection repaired the forged cache")

    def test_native_report_cannot_hide_residual_private_staging_directories(self):
        helper = load(HELPER, "prefix_effect_native_staging")
        with fixture("node-prep-clear") as (bank, home, workspace):
            before, after, artifact, context, bounds = self.native_report(home, workspace, bank, helper)
            helper.normalize("node-prep-clear", raw_delta(before, after), artifact, context, **bounds)
            staging = home / ".send-prefix/staging"
            staging.mkdir(mode=0o700, exist_ok=True)
            (staging / "unpublished-private-operation").mkdir(mode=0o700)
            forged = helper.snapshot(home)
            with self.assertRaisesRegex(ValueError, "staging"):
                helper.normalize("node-prep-clear", raw_delta(before, forged), artifact, context, **bounds)
            self.assertTrue((staging / "unpublished-private-operation").is_dir(), "rejection must be read-only")

    def test_native_report_refuses_validly_sealed_generation_and_mail_forgery(self):
        helper = load(HELPER, "prefix_effect_native_generation")
        with fixture("node-prep-clear") as (bank, home, workspace):
            before, after, artifact, context, bounds = self.native_report(home, workspace, bank, helper)
            helper.normalize("node-prep-clear", raw_delta(before, after), artifact, context, **bounds)
            path = home / ".send-prefix/checkpoint.json"
            value = json.loads(path.read_bytes())
            secret = (home / ".send-prefix/seal.key").read_bytes()
            nodes = home / ".send-prefix/nodes"
            from tests.fixtures.bus_scale_census.bus_scale_prefix_effects import _bank_nodes
            bank_nodes = _bank_nodes((home / ".send-prefix/bank.bin").read_bytes())

            def read(digest):
                target = nodes / digest
                return json.loads(target.read_bytes()) if target.exists() else copy.deepcopy(bank_nodes[digest])

            def lookup(key):
                digest = value["payload"]["trie"]
                while True:
                    node = read(digest)
                    if node["prefix"] == key:
                        return read(node["value"])
                    digest = node["children"][key[len(node["prefix"])]]

            def put(row):
                raw = send_prefix._bytes(row)
                digest = hashlib.sha256(raw).hexdigest()
                (nodes / digest).write_bytes(raw)
                (nodes / digest).chmod(0o600)
                return digest

            def alter(digest, key, change):
                node = read(digest)
                if node["prefix"] == key:
                    node["value"] = put(change(read(node["value"])))
                else:
                    bit = key[len(node["prefix"])]
                    node["children"][bit] = alter(node["children"][bit], key, change)
                return put(node)

            catalog_key = send_prefix._key("mail-node-v1", "architect")
            tree_root = lookup(catalog_key)["ordinary"]
            self.assertIsNotNone(tree_root)
            first_item = lookup(send_prefix._key("mail-tree-v1", tree_root))["oldest_lex"]
            faults = (
                ("generation", None, None),
                ("baseline", None, None),
                ("completeness", send_prefix._key("mail-valid-v1", "complete"), lambda _old: False),
                ("physical_head", send_prefix._key("mail-first-v1", "physical"), lambda _old: None),
                ("ordinal", send_prefix._key("mail-ordinal-v1", first_item["id"]), lambda old: old + 1),
                ("catalog", catalog_key, lambda old: dict(old, last="1900-01-01T00:00:00Z")),
                ("sender", send_prefix._key("mail-sender-v1", "scale-worker"), lambda _old: None),
                ("membership", send_prefix._key("mail-tree-v1", tree_root),
                    lambda old: dict(old, count=old["count"] + 1)),
            )
            for fault, key, change in faults:
                with self.subTest(fault=fault):
                    value = json.loads(after[".send-prefix/checkpoint.json"])
                    if fault == "generation":
                        original = value["payload"]["generation"]
                        value["payload"]["generation"] = ("0" if original[0] != "0" else "1") + original[1:]
                    elif fault == "baseline":
                        value["payload"]["mail_base_count"] = 0 if value["payload"]["mail_base_count"] else 1
                    else:
                        value["payload"]["trie"] = alter(value["payload"]["trie"], key, change)
                    value["seal"] = hmac.new(secret, send_prefix._bytes(value["payload"]), hashlib.sha256).hexdigest()
                    path.write_bytes(send_prefix._bytes(value))
                    forged = helper.snapshot(home)
                    expected = ("certified event generation" if fault == "generation" else
                        "complete native map|certified mail baseline" if fault == "baseline" else "complete native map")
                    try:
                        with self.assertRaisesRegex(ValueError, expected):
                            helper.normalize("node-prep-clear", raw_delta(before, forged), artifact, context, **bounds)
                        self.assertEqual(forged, helper.snapshot(home), "rejection must preserve forged evidence")
                    finally:
                        path.write_bytes(after[".send-prefix/checkpoint.json"])
                        for target in nodes.iterdir():
                            if target.relative_to(home).as_posix() not in after:
                                target.unlink()
