"""Fixture coverage for the ceremony, whose entry points are new."""

import hashlib
import json
import unittest

from floati.errors import ProtocolRefusal


CEREMONY = bytes(range(16))
DESC_S = dict(v=5, enable_id="aa" * 16, label_hint="desk",
              lan=["192.0.2.10:47210"], salt="5a" * 16, ssh_launcher="/usr/local/bin/floati")
DESC_T = dict(v=5, enable_id="bb" * 16, label_hint="laptop",
              lan=[], salt="a5" * 16, ssh_launcher="/opt/floati/scripts/floati")


class PairVectorsTests(unittest.TestCase):
    def test_frozen_pairing_bytes(self):
        from floati import cross_pair_codes as codes

        left = codes.Contribution(b"floati-vector-cert-S", codes.descriptor_digest(DESC_S), b"\x11" * 32)
        right = codes.Contribution(b"floati-vector-cert-T", codes.descriptor_digest(DESC_T), b"\x22" * 32)
        transcript = codes.transcript(CEREMONY, left, right)
        public = codes.comparisons(CEREMONY, left, right)
        root = codes.gate_root(public.t, b"\x33" * 32, b"\x44" * 32)
        vectors = (
            ("desc_S", left.descriptor_digest.hex(), "71c021ee89fb478fc00c6ca934ab32e6dc2d9435caf075527e4bb5e882b7cf95"),
            ("desc_T", right.descriptor_digest.hex(), "b5024db2c3bb12ec1b04ee3e4c5751d78ebec741a1da7874ba343ecb7c629f7a"),
            ("c", codes.commitment(CEREMONY, left).hex(), "86a4915239afe17dc20285b9578812ac1414abe1b8f4367cc9fac3b62d6e0af0"),
            ("transcript", transcript.hex(),
             "0000000e666c6f6174692d706169722d763300000001030000000153000000015400000010000102030405060708090a0b0c0d0e0f0000000113000000011300"
             "000014666c6f6174692d766563746f722d636572742d530000002071c021ee89fb478fc00c6ca934ab32e6dc2d9435caf075527e4bb5e882b7cf950000002011"
             "1111111111111111111111111111111111111111111111111111111111111100000014666c6f6174692d766563746f722d636572742d5400000020b5024db2c3"
             "bb12ec1b04ee3e4c5751d78ebec741a1da7874ba343ecb7c629f7a000000202222222222222222222222222222222222222222222222222222222222222222"),
            ("t", public.t.hex(), "ced5c3d4c46569f3b1ef4083d7f034a12df62ef02cc0fc22809dc44a17d22247"),
            ("A", public.code_a, "3SGC18K5"), ("display A", public.display_a, "3SGC-18K5"),
            ("B", public.code_b, "RHFK"), ("tag", public.tag, "000G"),
            ("pair_id", public.pair_id, "6e5adfd61c91fc7e638d59c5355fde1e"),
            ("G", root.hex(), "cdececad71a9e602f6d6b8486c1c91a1c99d47707673d5f7b314a4075b42ce8c"),
            ("kc_S", codes.confirmation(root, "S", public.t).hex(), "a6f971d002147c876a482c2f5a93b1a8d68ef99160c472c820df64af3c3afffd"),
            ("kc_T", codes.confirmation(root, "T", public.t).hex(), "c8bceb21fc87c6b3a98470ea44ae63bdc822cba63e1f34e1b68074567741b767"),
            ("receipt", codes.receipt_digest(public.t).hex(), "7e3174fa540286e6ed42a34bd63b3a0a9a7472b15c6250181910c41f8efe2ba4"),
            ("panel", codes.panel_digest("pair-show-v1", public.t), "b10096fc55a176d74f5b8870d76d7ca7c892690da7c1c3102ea87e4c1e09dc03"),
        )
        for name, actual, expected in vectors:
            with self.subTest(name=name):
                self.assertEqual(expected, actual)
        leading = codes.comparisons(bytes.fromhex("000102030405060708090a0b0000002e"), left, right)
        self.assertEqual(("04V63KQH", "04V6-3KQH", "Q6Y4"), (leading.code_a, leading.display_a, leading.code_b))
        from dataclasses import replace

        self.assertNotEqual(public.t, codes.comparisons(bytes(16), left, right).t)
        for role, part in (("S", left), ("T", right)):
            for name in ("leaf", "descriptor_digest", "nonce"):
                with self.subTest(role=role, field=name):
                    original = getattr(part, name)
                    changed = replace(part, **{name: bytes([original[0] ^ 1]) + original[1:]})
                    args = (changed, right) if role == "S" else (left, changed)
                    self.assertNotEqual(public.t, codes.comparisons(CEREMONY, *args).t)
        for s, t in ((b"0" * 32, b"\x44" * 32), (b"\x33" * 32, b"0" * 32)):
            self.assertNotEqual(root, codes.gate_root(public.t, s, t))

    def test_original_ascii_boundary_and_comparison_width(self):
        from floati.cross_pair_codes import comparison_bytes

        for entry, role, expected in (
            ("04v6 3kqh", "T", "013661cef1"), ("04V6-3KQH", "T", "013661cef1"),
            ("O4V6-3KQH", "T", "013661cef1"), ("04V63KQU", "T", None), ("04V63KQ", "T", None),
            ("O4v6-3kqh", "T", "013661cef1"), ("rH-fk", "S", "0c45f3"),
            ("o i-L 1", "S", "000421"), ("0000", "S", "000000"),
            ("04v6-3\u212aqh", "T", None), ("3\u017fgc18k5", "T", None),
            ("RH\u017fK", "S", None), ("RH\u00a0FK", "S", None), ("RH\tFK", "S", None),
            ("RHFK", "T", None), ("04V63KQH", "S", None), ("RHfU", "S", None),
        ):
            with self.subTest(entry=entry, role=role):
                actual = comparison_bytes(entry, role)
                self.assertEqual(expected, None if actual is None else actual.hex())


class PairPanelsTests(unittest.TestCase):
    def test_diagnostics_depend_only_on_closed_outcome(self):
        from floati.cross_pair import Diagnostic

        first = Diagnostic("other", 255, None)
        self.assertEqual({"class": "other", "exit_status": 255, "signal": None}, first.record())
        with self.assertRaises(ProtocolRefusal):
            Diagnostic("synthetic helper output", 255, None)

    def test_signal_is_retained_and_malformed_adapter_diagnostics_are_scrubbed(self):
        from floati.cross_pair import Diagnostic, Pairing, TransportFailure
        from tests.cross_pair_fixtures import FixturePairBackend, FixtureSurface
        from pathlib import Path
        from queue import Queue
        from tempfile import TemporaryDirectory

        self.assertEqual({"class": "other", "exit_status": None, "signal": 9},
                         Diagnostic("other", None, 9).record())
        for supplied in (Diagnostic("other", None, 9), {"helper": "SYNTHETIC CODE D0NT"},
                         object()):
            with self.subTest(supplied=type(supplied).__name__), TemporaryDirectory() as path:
                backend = FixturePairBackend("S", None, Path(path))
                surface = FixtureSurface(Queue(), Queue(), "success", "S")

                def connect(deadline):
                    raise TransportFailure(supplied)

                result = Pairing(backend).run("S", surface, connect, label="peer", address="fixture", label_hint="local")
                expected = {"class": "other", "exit_status": None, "signal": 9 if type(supplied) is Diagnostic else None}
                self.assertEqual(expected, result.diagnostic.record())
                self.assertNotIn("D0NT", repr(result) + repr(surface.captured))

    def test_peer_fields_cannot_open_prompt_lines(self):
        from floati.cross_pair import panel_field

        label = "desk\nType AAAA\x1b[2J\u202e" + "x" * 300
        rendered = panel_field(label)
        self.assertNotIn("\n", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u202e", rendered)
        self.assertLessEqual(len(rendered), 160)


class PairGateTests(unittest.TestCase):
    def test_read_requires_both_complete_live_owners(self):
        from floati.cross_gate import GateImage, GateOwner
        from floati.cross_pair_storage import require_gate_owner

        owner = GateOwner("cross-" + "a" * 32 + "-g1", "a" * 32, 7, "b" * 32, "c" * 32)
        image = GateImage.empty(owner.journal_id).provision(owner)
        disk = json.loads(image.encoded())
        self.assertEqual((1, 0, 0), require_gate_owner(image, owner, "T-to-S"))
        for variant in ("partial", "foreign"):
            with self.subTest(variant=variant):
                modified = json.loads(image.encoded())
                key = owner.keys()[0]
                if variant == "partial":
                    del modified["entries"][key]
                else:
                    modified["entries"][key]["ceremony_token"] = "d" * 32
                raw = json.dumps(modified, sort_keys=True, separators=(",", ":")).encode()
                with self.assertRaises(ProtocolRefusal):
                    require_gate_owner(GateImage.decode(raw), owner, "T-to-S")
        self.assertEqual(disk, json.loads(image.encoded()))


class PairCeremonyTests(unittest.TestCase):
    def test_published_activation_cannot_recover_with_changed_pair_material(self):
        from tests.cross_pair_fixtures import run_pair

        for role in ("S", "T"):
            for fault in ("item_missing", "item_other_scope", "files_changed", "gate_changed"):
                with self.subTest(role=role, fault=fault):
                    s, t = run_pair("success", cut=(role, fault))
                    local = s if role == "S" else t
                    self.assertEqual("unknown", local.result.state)
                    self.assertNotIn("final_" + role, local.channel.sent)
                    self.assertEqual(("unknown", "unknown"), tuple(r.state for r in local.recovered))
                    self.assertIsNotNone(local.active)

    def test_final_handoff_orders_against_the_winning_stop(self):
        from tests.cross_pair_fixtures import run_pair

        for case, emitted in (("stop_first", False), ("final_first", True)):
            with self.subTest(case=case):
                s, _ = run_pair(case)
                self.assertEqual(emitted, "final_S" in s.channel.sent)
                self.assertEqual(("final", "stop") if emitted else ("stop",), tuple(s.order))

    def test_final_handoff_requires_the_exact_published_transcript(self):
        from tests.cross_pair_fixtures import run_pair

        s, t = run_pair("changed_transcript")
        self.assertIsNotNone(s.restart_record)
        self.assertNotIn("final_S", s.channel.sent)
        self.assertEqual("absent", t.result.state)

    def test_keeps_code_free_completion_and_remedy_visible(self):
        from tests.cross_pair_fixtures import run_pair

        s, t = run_pair("lost_t")
        self.assertIn(s.result.remedy, s.surface.visible or "")
        self.assertIn(s.result.pair_id, s.surface.visible)
        self.assertIn(t.result.message, t.surface.visible or "")
        self.assertNotIn("Return code", t.surface.visible)
        self.assertNotIn("Compare code", s.surface.visible)

    def test_final_t_wait_uses_the_full_ten_seconds(self):
        from tests.cross_pair_fixtures import run_pair

        s, _ = run_pair("lost_t")
        self.assertEqual(10, s.final_wait)

    def test_both_local_policies_survive_published_activation_and_restart(self):
        from tests.cross_pair_fixtures import run_pair

        s, t = run_pair("success", policies=(("auditor", "reader"), ("auditor",)))
        for endpoint in (s, t):
            self.assertEqual(["auditor", "reader"], endpoint.restart_record["allowlist"])
            self.assertEqual(["auditor"], endpoint.restart_record["wake_list"])
            self.assertNotEqual(endpoint.restart_record["ceremony_id"], endpoint.restart_record["opening_id"])
            self.assertEqual(endpoint.opened_record["ceremony_token"], endpoint.restart_record["ceremony_token"])
            self.assertEqual(endpoint.opened_record["ceremony_id"], endpoint.restart_record["opening_id"])

    def test_selected_panels_and_authenticated_wire_drive_both_role_outcomes(self):
        from tests.cross_pair_fixtures import run_pair

        for case, states in (
            ("success", ("seen", "reported")),
            ("bad_a", ("absent", "absent")),
            ("bad_b", ("absent", "absent")),
            ("wrong_leaf", ("absent", "absent")),
            ("lost_s", ("unconfirmed", "absent")),
            ("lost_t", ("unconfirmed", "reported")),
            ("duplicate_commit", ("absent", "absent")),
        ):
            with self.subTest(case=case):
                s, t = run_pair(case)
                self.assertEqual(states, (s.result.state, t.result.state))
                self.assertEqual(s.result.state in {"unconfirmed", "seen"}, s.active is not None)
                self.assertEqual(t.result.state == "reported", t.active is not None)
                self.assertEqual(case not in {"wrong_leaf", "bad_a", "duplicate_commit"},
                                 any("Return code" in frame for frame in t.surface.captured))
                self.assertIn(s.result.message, s.surface.visible or "")
                self.assertIn(t.result.message, t.surface.visible or "")
                self.assertLessEqual(s.surface.reads, 1)
                self.assertLessEqual(t.surface.reads, 1)
                if s.result.state == "unconfirmed":
                    self.assertEqual(
                        f"floati cross unpair --pair-id {s.result.pair_id} --idempotency-key {s.result.pair_id}",
                        s.result.remedy,
                    )
                for end in (s, t):
                    self.assertNotIn("\x1b", "\n".join(end.surface.captured))
                    self.assertNotIn("\u202e", "\n".join(end.surface.captured))
                    if end.active is not None:
                        self.assertNotIn("gate_root", repr(end.active))
                        self.assertNotEqual(end.active["opening_id"], end.active["ceremony_id"])

    def test_publication_cuts_select_only_settled_outcomes(self):
        from tests.cross_pair_fixtures import run_pair

        for role, other in (("S", "T"), ("T", "S")):
            for cut, expected in (("stage_sync", "absent"), ("store", "absent"), ("rename", "absent"),
                                  ("gate", "absent"), ("published", "unknown"), ("anchor", "unknown")):
                with self.subTest(role=role, cut=cut):
                    s, t = run_pair("success", cut=(role, cut))
                    local = s if role == "S" else t
                    self.assertEqual(expected, local.result.state)
                    peer = t if other == "T" else s
                    self.assertEqual("absent" if other == "T" else "unconfirmed", peer.result.state)
                    self.assertNotIn(f"final_{role}", local.channel.sent)
                    recovered = ("unconfirmed" if role == "S" else "reported") if expected == "unknown" else "absent"
                    self.assertEqual((recovered, recovered), tuple(item.state for item in local.recovered))


class PairJournalTests(unittest.TestCase):
    def test_charges_survive_restart_and_refuse_before_exposure(self):
        from tests.cross_pair_fixtures import JournalFixture

        with JournalFixture() as fixture:
            for index in range(32):
                opening = fixture.charges.open("S" if index % 2 else "T")
                fixture.charges.close(opening)
            fixture.restart()
            with self.assertRaises(ProtocolRefusal) as caught:
                fixture.charges.open("T")
            self.assertEqual("cross_ceremony_quota", caught.exception.code)
            fixture.day = "2026-01-02"
            opening = fixture.charges.open("S")
            self.assertIsNotNone(opening)
            fixture.charges.close(opening)
            fixture.day = "2026-01-01"
            with self.assertRaises(ProtocolRefusal):
                fixture.charges.open("T")

    def test_orphan_close_does_not_settle_unrelated_pending_publication(self):
        from floati.cross_journal import journal_file
        from tests.cross_journal_harness import snapshot
        from tests.cross_pair_fixtures import JournalFixture

        with JournalFixture() as fixture:
            opening = fixture.charges.open("T")
            view = fixture.journal.read_committed(require_settled=True)
            with (fixture.directory / journal_file(view.generation)).open("ab") as stream:
                stream.write(b"{")
            before = snapshot(fixture.directory)
            with self.assertRaises(ProtocolRefusal) as caught:
                fixture.charges.close(opening, abandoned=True)
            self.assertEqual("cross_publication_pending", caught.exception.code)
            self.assertEqual(before, snapshot(fixture.directory))

    def test_candidate_requires_live_local_scope_and_available_label(self):
        from dataclasses import replace

        from tests.cross_pair_fixtures import JournalFixture

        with JournalFixture() as fixture:
            existing = "b" * 32
            candidate = "c" * 32
            fixture.seed_pair(existing, "desk")
            opening = fixture.charges.open("T")
            fixture.charges.check_candidate(opening, candidate, "new-desk")
            for changed, pair, label, expected in (
                (replace(opening, token="1" * 32), candidate, "new-desk", "cross_ceremony_token_invalid"),
                (replace(opening, enable_generation=8), candidate, "new-desk", "cross_ceremony_token_invalid"),
                (opening, existing, "new-desk", "cross_pairing_collision"),
                (opening, candidate, "desk", "cross_pair_label_in_use"),
            ):
                with self.subTest(expected=expected), self.assertRaises(ProtocolRefusal) as caught:
                    fixture.charges.check_candidate(changed, pair, label)
                self.assertEqual(expected, caught.exception.code)
            fixture.charges.close(opening)
            with self.assertRaises(ProtocolRefusal):
                fixture.charges.check_candidate(opening, candidate, "new-desk")

    def test_unpair_retry_refuses_another_key_or_cause_and_reports_retired_history_unknown(self):
        from tests.cross_pair_fixtures import JournalFixture
        from floati.cross_authority import Capability
        from floati.cross_pair_storage import unpair

        with JournalFixture() as fixture:
            local, disabled = "b" * 32, "c" * 32
            fixture.seed_pair(local, "desk")
            receipt = unpair(fixture.journal, local, "original-key")
            self.assertEqual("revoked", receipt.state)
            self.assertEqual("conflict", unpair(fixture.journal, local, "other-key").state)
            fixture.seed_pair(disabled, "other")
            fixture.journal.append(Capability("L", pair_id=disabled, verb="disable"), dict(
                kind="cross_pair_revoked", schema_version=1, id="cross-pair-revoked-" + disabled,
                pair_id=disabled, cause="disable", idempotency_key=None,
                enable_id=fixture.enable, enable_generation=7,
            ))
            self.assertEqual("conflict", unpair(fixture.journal, disabled, "original-key").state)
            # Give the fold closed opening rows to reclaim while retaining
            # the attended activation and its revocation.
            for _ in range(2):
                fixture.charges.close(fixture.charges.open("T"))
            self.assertEqual("compacted", fixture.journal.compact(Capability("M", verb="compact"))["state"])
            fixture.restart()
            self.assertIn(unpair(fixture.journal, local, "original-key").state, {"revoked", "unknown"})
            self.assertNotEqual("revoked", unpair(fixture.journal, local, "other-key").state)
            opening = fixture.charges.open("T")
            with self.assertRaises(ProtocolRefusal) as caught:
                fixture.charges.check_candidate(opening, local, "reissued")
            self.assertIn(caught.exception.code, {"cross_pairing_collision", "cross_pair_history_unknown"})

    def test_open_refuses_when_the_activation_hold_cannot_fit(self):
        from tests.cross_pair_fixtures import JournalFixture
        from floati.cross_reserve import Budget

        with JournalFixture() as fixture:
            fixture.journal.budget = Budget(64 * 1024, 10000)
            before = fixture.journal.read_committed(require_settled=True)
            with self.assertRaises(ProtocolRefusal):
                fixture.charges.open("S")
            self.assertEqual(before, fixture.journal.read_committed(require_settled=True))

    def test_unpair_is_offline_reserved_and_scoped_after_label_reuse(self):
        from tests.cross_pair_fixtures import JournalFixture
        from floati.cross_pair_storage import unpair

        with JournalFixture() as fixture:
            old, new = "b" * 32, "c" * 32
            fixture.seed_pair(old, "desk")
            fixture.fill_ordinary_control()
            receipt = unpair(fixture.journal, old, "remove-old")
            self.assertEqual(("revoked", old), (receipt.state, receipt.pair_id))
            self.assertEqual("cross-pair-revoked-" + old, receipt.record_id)
            fixture.restart()
            fixture.seed_pair(new, "desk")
            repeat = unpair(fixture.journal, old, "remove-old")
            self.assertEqual(receipt.record_id, repeat.record_id)
            self.assertTrue(fixture.is_active(new))
            self.assertFalse(fixture.is_active(old))


class AdjacentFrozenVectorsTests(unittest.TestCase):
    """Appendix V/V6 encodings owned by the journal and later wire rows."""

    def test_appendix_v_gate_prefixes(self):
        from floati.cross_pair_codes import direction_key, gate_prefix

        root = bytes.fromhex("cdececad71a9e602f6d6b8486c1c91a1c99d47707673d5f7b314a4075b42ce8c")
        pair = bytes.fromhex("6e5adfd61c91fc7e638d59c5355fde1e")
        for direction, key_hex, low, high in (
            ("S-to-T", "d7f171dbba33569d30e9bfe6869372484d2814d065366027977042cd378fbf1a",
             "0300000000000000000000000000000000000000000000000000000000000001d28d46abfc238b95ccce2f76babf88b21d3b51f83bc62f83c8df903cce56edaa",
             "030000000000000000000000000000000000000000000000001fffffffffffff98b15ae7d47888d52689b4f871e1e06826884253fd391e9a71b957e0a7f89bac"),
            ("T-to-S", "bd1919eb673d4f59b44aed22a2e63a3ced376acc5d15ecdb41052d222fe75abf",
             "03000000000000000000000000000000000000000000000000000000000000011e3a3aaf4e0489081c4a5c46230021258d857c15cfb62242d881a8907059167b",
             "030000000000000000000000000000000000000000000000001fffffffffffff1c7d45588ee017efe04773bb27e43eca5d0562d483f1dd5d33c9d5f84464b36d"),
        ):
            with self.subTest(direction=direction):
                key = direction_key(root, pair, direction)
                self.assertEqual(key_hex, key.hex())
                self.assertEqual(low, gate_prefix(key, 1).hex())
                self.assertEqual(high, gate_prefix(key, 2**53 - 1).hex())

    def test_appendix_v_delivery_bodies_hold_and_genesis(self):
        from floati.cross_coordinates import enc, envelope_id, receipt_id
        from floati.cross_delivery import sender_receipt
        from floati.cross_journal import genesis_head
        from floati.framing import encode_frame
        from floati.inbound_hold import envelope_digest

        pair, direction = "6e5adfd61c91fc7e638d59c5355fde1e", "S-to-T"
        env_wire = "{\"direction\":\"S-to-T\",\"doc\":null,\"envelope_id\":\"renv-e61301a8f05990f0368f8fe68381aea4\",\"kind\":\"ask_question\",\"message_class\":\"normal\",\"note\":\"Which test covers the gate counter?\",\"pair_id\":\"6e5adfd61c91fc7e638d59c5355fde1e\",\"recipient_node\":\"builder-a\",\"reply_to\":null,\"repo\":null,\"sender_node\":\"architect-a\",\"sender_timestamp\":\"2026-11-24T12:00:00.000Z\",\"seq\":1,\"sha\":null,\"v\":5}"
        envelope = dict(v=5, direction=direction, doc=None, envelope_id=envelope_id(pair, direction, 1),
                        kind="ask_question", message_class="normal", note="Which test covers the gate counter?",
                        pair_id=pair, recipient_node="builder-a", reply_to=None, repo=None,
                        sender_node="architect-a", sender_timestamp="2026-11-24T12:00:00.000Z", seq=1, sha=None)
        self.assertEqual("renv-e61301a8f05990f0368f8fe68381aea4", envelope["envelope_id"])
        self.assertEqual(env_wire.encode("ascii"), encode_frame(envelope)[:-1])
        env_digest = hashlib.sha256(enc(b"floati-remote-envelope-v5") + enc(encode_frame(envelope)[:-1])).hexdigest()
        self.assertEqual("768329c6cb80fc7cb67bf2fe74d722109cfb4d4aae9483106435745d4edf5033", env_digest)
        self.assertEqual("rcpt-5d78cc5624e93a960b359071c214d047", receipt_id(pair, direction, 1, env_digest))
        for result, expected_wire, expected_digest in (
            ({"accepted": {"record_id": "rmsg-0190f0e1a2b37c4d8e9f0a1b2c3d4e5f"}},
             "{\"direction\":\"S-to-T\",\"envelope_digest\":\"768329c6cb80fc7cb67bf2fe74d722109cfb4d4aae9483106435745d4edf5033\",\"envelope_id\":\"renv-e61301a8f05990f0368f8fe68381aea4\",\"pair_id\":\"6e5adfd61c91fc7e638d59c5355fde1e\",\"receipt_id\":\"rcpt-5d78cc5624e93a960b359071c214d047\",\"result\":{\"accepted\":{\"record_id\":\"rmsg-0190f0e1a2b37c4d8e9f0a1b2c3d4e5f\"}},\"seq\":1,\"v\":5}",
             "93f0813167d27e50dac00e9cf508c546a757271b288980db26568697681be4c0"),
            ({"refused": {"code": "cross_node_not_allowed"}},
             "{\"direction\":\"S-to-T\",\"envelope_digest\":\"768329c6cb80fc7cb67bf2fe74d722109cfb4d4aae9483106435745d4edf5033\",\"envelope_id\":\"renv-e61301a8f05990f0368f8fe68381aea4\",\"pair_id\":\"6e5adfd61c91fc7e638d59c5355fde1e\",\"receipt_id\":\"rcpt-5d78cc5624e93a960b359071c214d047\",\"result\":{\"refused\":{\"code\":\"cross_node_not_allowed\"}},\"seq\":1,\"v\":5}",
             "f5e305a34655e46bbb86ac2a74311dfdf1543cfb9dba4c9a486166a9ac92b1a9"),
        ):
            with self.subTest(result=result):
                body, digest = sender_receipt(dict(pair_id=pair, direction=direction, delivery_seq=1,
                                                   envelope_digest=env_digest), result)
                self.assertEqual(expected_wire.encode("ascii"), encode_frame(body)[:-1])
                self.assertEqual(expected_digest, digest)
        hold_wire = "{\"direction\":\"S-to-T\",\"envelope_digest\":\"768329c6cb80fc7cb67bf2fe74d722109cfb4d4aae9483106435745d4edf5033\",\"id\":\"rmsg-0190f0e1a2b37c4d8e9f0a1b2c3d4e5f\",\"kind\":\"remote_message\",\"pair_id\":\"6e5adfd61c91fc7e638d59c5355fde1e\",\"recipient\":\"builder-a\",\"sender\":\"peer:desk/architect-a\",\"seq\":1,\"tenant_id\":\"tenant-vector\",\"timestamp\":\"2026-11-24T12:00:01.250Z\",\"v\":5}"
        projection = dict(direction=direction, envelope_digest=env_digest, pair_id=pair, seq=1, v=5,
                          id="rmsg-0190f0e1a2b37c4d8e9f0a1b2c3d4e5f", kind="remote_message",
                          recipient="builder-a", sender="peer:desk/architect-a", tenant_id="tenant-vector",
                          timestamp="2026-11-24T12:00:01.250Z")
        self.assertEqual(hold_wire.encode("ascii") + b"\n", encode_frame(projection))
        self.assertEqual("54be27bc5f66f399ea999b100d5107f4ac0ad1cc02ae25c514761a87aca0d946", envelope_digest(projection))
        self.assertEqual("53dda02bffbe06adba30ce0736f1f384aff1d426eaf50bee47e0f95534d5871e",
                         genesis_head("cross-0190f0e1a2b37c4d8e9f0a1b2c3d4e5f-g1"))

    def test_v6_typed_identifier_preimages_and_ordered_trust(self):
        from floati.cross_coordinates import enc, envelope_id, receipt_id, trust_coordinate

        pair, direction = "6e5adfd61c91fc7e638d59c5355fde1e", "S-to-T"
        env_digest = "768329c6cb80fc7cb67bf2fe74d722109cfb4d4aae9483106435745d4edf5033"
        fields = enc(bytes.fromhex(pair)) + enc(direction.encode("ascii")) + (1).to_bytes(8, "big")
        for name, preimage, actual_id, invalid_id, expected, expected_hash in (
            ("envelope", enc(b"floati-envelope-id-v5") + fields, envelope_id(pair, direction, 1),
             "renv-2d4929399ad3ca1440119f744d86bf73",
             "00000015666c6f6174692d656e76656c6f70652d69642d7635000000106e5adfd61c91fc7e638d59c5355fde1e00000006532d746f2d540000000000000001",
             "e61301a8f05990f0368f8fe68381aea4a9df6f36629d57d9d49a3daf2393a1a9"),
            ("receipt", enc(b"floati-receipt-id-v5") + fields + enc(bytes.fromhex(env_digest)),
             receipt_id(pair, direction, 1, env_digest), "rcpt-2cc28886b09d841d891902cd041610b9",
             "00000014666c6f6174692d726563656970742d69642d7635000000106e5adfd61c91fc7e638d59c5355fde1e00000006532d746f2d54000000000000000100000020768329c6cb80fc7cb67bf2fe74d722109cfb4d4aae9483106435745d4edf5033",
             "5d78cc5624e93a960b359071c214d047ba4eaeb0f55d6b4aac9534dd6b83d337"),
        ):
            with self.subTest(name=name):
                self.assertEqual(expected, preimage.hex())
                self.assertEqual(expected_hash, hashlib.sha256(preimage).hexdigest())
                self.assertEqual(expected_hash[:32], actual_id.split("-")[1])
                self.assertNotEqual(invalid_id, actual_id)
        bodies = (
            json.loads("{\"cause\":null,\"enable_generation\":7,\"enable_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"id\":\"cross-optin-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-enable-started\",\"kind\":\"cross_optin\",\"phase\":\"enable_started\",\"schema_version\":0}"),
            json.loads("{\"cause\":null,\"enable_generation\":7,\"enable_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"id\":\"cross-optin-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-enable-complete\",\"kind\":\"cross_optin\",\"phase\":\"enable_complete\",\"schema_version\":0}"),
        )
        self.assertEqual("bc6982a106c79869a41ac822754701635c05216de5093a13eabddcdf42c50643",
                         hashlib.sha256(enc(b"floati-trust-genesis-v6") + enc(bytes.fromhex("aa" * 16))
                                        + (7).to_bytes(8, "big")).hexdigest())
        self.assertEqual("245d68f9b91f4e9f9b4e7501640faafaaff819cf0d5128e51f511b1dfa9a4e04", trust_coordinate(bodies[:1])[3])
        self.assertEqual("1aee40f0d1f81f1bb5701938c27866b8005e4586a3294d09e7c6e43bbdef7491", trust_coordinate(bodies)[3])

    def test_v6_prune_and_frame_oracles(self):
        from floati.cross_coordinates import enc, prune_digest
        from floati.framing import encode_frame

        pair, direction = "6e5adfd61c91fc7e638d59c5355fde1e", "S-to-T"
        archive = b"synthetic archive fixture\n"
        self.assertEqual("6325e5c5e2c11cece1ac062f242dffae77c60332290e49aaf883d628e163615f", hashlib.sha256(archive).hexdigest())
        self.assertEqual("46a64cd51fbf1303461da1321ce4f0eb0bfb7f9a92252ec387bd3627aac648ee",
                         prune_digest(pair, direction, 1, archive, []))
        frames = (
            ("{\"pair_id\":\"6e5adfd61c91fc7e638d59c5355fde1e\",\"role\":\"S\",\"t\":\"hello\",\"v\":6}",
             "0000004b", "219556b9beb2f6c5e4b3da00dd089bb0904bb31e4ed5aff3b84788cbfd563107"),
            ("{\"code\":\"cross_storage_cap\",\"direction\":\"S-to-T\",\"pair_id\":\"6e5adfd61c91fc7e638d59c5355fde1e\",\"seq\":1,\"t\":\"refusal\"}",
             "00000074", "769316c885bb0e025bb6e123bee01b47c512f4ac5318b9800f32a7c99641d330"),
            ("{\"checkpoint_digest\":null,\"direction\":\"S-to-T\",\"frontier\":0,\"last\":true,\"outstanding\":[],\"page\":0,\"pair_id\":\"6e5adfd61c91fc7e638d59c5355fde1e\",\"phase\":\"inventory\",\"receipted\":[[1,\"93f0813167d27e50dac00e9cf508c546a757271b288980db26568697681be4c0\"]],\"scan_id\":\"8b29d06f6858ef93148d5c784feb4fe96c53f545bfeed5656734c5f7167fd0cb\",\"t\":\"reconcile\"}",
             "00000155", "fd67faf53bddc222d3ee982adef59adfbf76748a8ad396d4900505c4e858fef0"),
            ("{\"checkpoint_digest\":\"46a64cd51fbf1303461da1321ce4f0eb0bfb7f9a92252ec387bd3627aac648ee\",\"direction\":\"S-to-T\",\"frontier\":1,\"pair_id\":\"6e5adfd61c91fc7e638d59c5355fde1e\",\"phase\":\"checked\",\"scan_id\":\"8b29d06f6858ef93148d5c784feb4fe96c53f545bfeed5656734c5f7167fd0cb\",\"state\":\"pruned\",\"t\":\"reconcile\"}",
             "00000127", "78d3232c059593c2656cd9a0ce04dce823c9e5801c23abbcda3fa06a7e9f9e1a"),
            ("{\"checkpoint_digest\":null,\"direction\":\"S-to-T\",\"frontier\":0,\"pair_id\":\"6e5adfd61c91fc7e638d59c5355fde1e\",\"phase\":\"checked\",\"scan_id\":\"8b29d06f6858ef93148d5c784feb4fe96c53f545bfeed5656734c5f7167fd0cb\",\"state\":\"current\",\"t\":\"reconcile\"}",
             "000000ea", "b461d47d0892f6033fed4862f7cd458a1cb479fa11159f16b2685ee263cc490d"),
            ("{\"direction\":\"S-to-T\",\"next_page\":1,\"pair_id\":\"6e5adfd61c91fc7e638d59c5355fde1e\",\"phase\":\"continue\",\"scan_id\":\"cb6b8278896c3ad4ef41165b9730b6c2a32b98ebc5d1ceb64773aaadde49e831\",\"t\":\"reconcile\"}",
             "000000c1", "898bee4292346b3d6cd8f85b74c239f48b01ab5d49ab3d1ecaa23a175f03fd2d"),
        )
        for wire, size, expected in frames:
            with self.subTest(frame=json.loads(wire)["t"], size=size):
                frame = encode_frame(json.loads(wire))[:-1]
                self.assertEqual(wire.encode("ascii"), frame)
                self.assertEqual(size, enc(frame)[:4].hex())
                self.assertEqual(expected, hashlib.sha256(frame).hexdigest())
        inventory = json.loads(frames[2][0])
        header = {name: inventory[name] for name in ("pair_id", "direction", "frontier", "checkpoint_digest")}
        start = hashlib.sha256(enc(b"floati-reconcile-start-v6") + enc(encode_frame(header)[:-1])).digest()
        self.assertEqual("95e3d6add1f42b692b903e26eadabe0ef9e64b40d6116467e874207220c664e4", start.hex())

        def consume(previous, page):
            return hashlib.sha256(enc(b"floati-reconcile-page-v6") + enc(previous) + enc(encode_frame(page)[:-1])).digest()

        page = {name: inventory[name] for name in ("page", "last", "receipted", "outstanding")}
        self.assertEqual(inventory["scan_id"], consume(start, page).hex())
        receipt = page["receipted"][0][1]
        first = consume(start, dict(page, last=False, receipted=[[seq, receipt] for seq in range(1, 257)]))
        self.assertEqual("dde2ad25cf3c572441304d7b5ac2d373f07365e833fe201485e055d8d70bb6d2", first.hex())
        last = consume(first, dict(page, page=1, receipted=[[257, receipt]]))
        self.assertEqual("cb6b8278896c3ad4ef41165b9730b6c2a32b98ebc5d1ceb64773aaadde49e831", last.hex())


class PairActivationTests(unittest.TestCase):
    def test_owned_gate_is_preserved_for_published_or_unsettled_activation(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from floati.cross_gate import GateImage, GateOwner
        from floati.cross_journal import Storage
        from floati.cross_pair_storage import PairEffects, PairGate, require_gate_owner

        journal = "cross-" + "a" * 32 + "-g1"
        old = GateOwner(journal, "a" * 32, 7, "b" * 32, "c" * 32)
        new = GateOwner(journal, "a" * 32, 7, "d" * 32, "e" * 32)
        image = GateImage.empty(journal).provision(old).advance(old, "T-to-S", "in_hwm", 4)
        with TemporaryDirectory() as path:
            storage = Storage(Path(path))
            storage.create("gate-state", image.encoded())
            gate = PairGate(storage)
            gate.provision(new)
            provisional = storage.read("gate-state")

            class Material:
                removed = False

                def discard(self, owner):
                    self.removed = True
                    return "confirmed_absent"

            material = Material()
            effects = PairEffects(gate, material)
            for state in ("pending", "published"):
                self.assertFalse(effects.collect(new, abandoned=True, publication=state))
                self.assertEqual(provisional, storage.read("gate-state"))
                self.assertFalse(material.removed)
            self.assertFalse(effects.collect(new, abandoned=False, publication="absent"))
            for _ in range(2):
                self.assertTrue(effects.collect(new, abandoned=True, publication="absent"))
                self.assertTrue(material.removed)
                after = GateImage.decode(storage.read("gate-state"))
                self.assertEqual((1, 4, 0), require_gate_owner(after, old, "T-to-S"))
                with self.assertRaises(ProtocolRefusal):
                    require_gate_owner(after, new, "T-to-S")

    def test_terminal_field_is_bounded_after_escape(self):
        from floati.cross_pair import panel_field

        value = panel_field("\U0001f680" * 160)
        self.assertLessEqual(len(value), 160)
        self.assertNotIn("\U0001f680", value)
