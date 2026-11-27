"""Local entry points for the attended cross-machine lifecycle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sys

from .cross_attended import approved_completion, approved_start, verified_start
from .cross_authority import Capability
from .cross_coordinates import MAX_INTEGER, semantic
from .cross_journal import CrossJournal, TeardownProgress
from .cross_keystore import KeychainStore, ReadOutcome, SecretServiceStore
from .errors import ProtocolRefusal
from .inbound_hold import _answer, _say, _seat_context, _terminal
from .root import FloatiRoot


_ENABLE = Capability("A", verb="enable")
_DISABLE = Capability("L", verb="disable")
_ITEMS = ("configuration", "ballast", "gate", "sentinel", "anchor")


def register_cross(commands):
    family = commands.add_parser("cross", floati_mcp_exposure="never")
    verbs = family.add_subparsers(dest="cross_verb", required=True)
    enable = verbs.add_parser("enable", floati_mcp_exposure="never")
    enable.add_argument("--root", required=True, metavar="ROOT")
    enable.add_argument("--listen", action="append", metavar="ADDRESS")
    mode = enable.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--resume", action="store_true")
    enable.set_defaults(handler=_run_enable)
    disable = verbs.add_parser("disable", floati_mcp_exposure="never")
    disable.add_argument("--root", required=True, metavar="ROOT")
    disable.add_argument("--dry-run", action="store_true")
    disable.set_defaults(handler=_run_disable)


def outside_seat(root):
    if _seat_context(root, Path.cwd().resolve()) is not None:
        raise ProtocolRefusal("cross_human_act_unavailable", "Run enable in your own terminal outside a seat workspace.")


def confirm(start):
    try:
        descriptor = _terminal()
    except ProtocolRefusal:
        raise ProtocolRefusal("cross_human_act_unavailable", "Enable requires a controlling terminal.") from None
    try:
        code = secrets.token_hex(6)
        panel = json.dumps(_preview(start["plan"]), sort_keys=True, ensure_ascii=True)
        _say(descriptor, f"Cross-machine enable plan\n{panel}\nType {code} to enable: ")
        return secrets.compare_digest(_answer(descriptor).encode("utf-8"), code.encode("ascii"))
    finally:
        os.close(descriptor)


def open_backend(root, kind):
    adapter = {"keychain": KeychainStore, "secret-service": SecretServiceStore}[kind]
    adapter.probe(root)
    raise ProtocolRefusal("cross_key_store_unavailable", "This key store has yet to qualify for cross-machine writes.")


def _plan(root):
    if sys.platform not in {"darwin", "linux"}:
        raise ProtocolRefusal("cross_key_store_unavailable", "Cross-machine key storage is unavailable on this platform.")
    return {"listen": [], "root": str(root.path),
            "store": "keychain" if sys.platform == "darwin" else "secret-service", "unit": "none"}


def _preview(plan):
    return {"state": "preview", "plan": plan, "items": list(_ITEMS), "unit_requested": False}


def _latest_start(records):
    return next((semantic(row) for row in reversed(records) if row.get("phase") == "enable_started"), None)


def _phases(records, start):
    if start is None:
        return set()
    return {row.get("phase") for row in records if row.get("enable_id") == start["enable_id"]}


def _inventory(records, start):
    return [
        {key: row[key] for key in ("item", "phase", "token", "proof_sha256")}
        for row in records if row.get("kind") == "cross_inventory_item"
        and row.get("enable_id") == start["enable_id"]
    ] if start is not None else []


class _EnableInventory:
    def __init__(self, effects):
        self.effects = effects

    def expected(self, item, start):
        return self.effects.expected(item, start)

    def read(self, item, start):
        if item == "ballast":
            return self.journal.inspect_enable_ballast(start)
        return self.effects.read(item, start, self.expected(item, start))

    def create(self, item, start):
        if item == "ballast":
            self.journal.provision_enable_ballast(start)
        else:
            self.effects.create(item, start)


def _journal(root, store, effects, *, approve):
    inventory = _EnableInventory(effects)
    journal = CrossJournal.for_root(root, provenance=store, anchor_store=store.anchor, quota_store=store.quota,
                                    enable_inventory=inventory, enable_approval=approve)
    inventory.journal = journal
    return journal, inventory


def _pending(code, **evidence):
    return "degraded", {"state": "pending", "code": code, **evidence}, 35


def _completed_enable(journal, inventory, start, view):
    completion = approved_completion(start)
    if view.pending or _latest_start(view.records) != start or "disable_started" in _phases(view.records, start):
        return None
    if not any(semantic(row) == completion for row in view.records):
        return None
    proofs = {row["item"]: row["proof_sha256"] for row in _inventory(view.records, start)
              if row["phase"] == "created" and row["token"] == start["token"]}
    if set(proofs) != {*_ITEMS, "unit"}:
        return None
    for item, proof in proofs.items():
        if item != "anchor" and inventory.read(item, start) != ("owned", proof):
            return None
    # Recheck the anchored position after measuring material. A stop, restore,
    # or unresolved independent write during observation invalidates the result.
    if journal.read_committed(require_settled=True) != view:
        return None
    return view


def _run_enable(args):
    root = FloatiRoot.open_direct_home(args.root)
    disk = CrossJournal.for_root(root)
    plan = _plan(root)
    if args.listen:
        raise ProtocolRefusal("cross_listener_unavailable", "Listener configuration requires cross-machine listener support.")
    if args.preview:
        return "ok", _preview(plan), 0
    outside_seat(root)

    observation = disk.inspect_disk()
    prior = _latest_start(observation.records)
    phases = _phases(observation.records, prior)
    store, inventory = open_backend(root, plan["store"])

    def approve(phase, body):
        if body != start or CrossJournal.for_root(root).directory != disk.directory:
            return False
        if phase in {"start", "enable_started"}:
            return (
                not args.resume
                and (prior is not None or store.prove_unbound())
                and store.observe("anchor").outcome is ReadOutcome.CONFIRMED_ABSENT
            )
        return phase == "enable_complete"

    journal, enable_inventory = _journal(root, store, inventory, approve=approve)
    if args.resume:
        if prior is None or prior.get("schema_version") not in (1, 2) or prior.get("plan") != plan or "disable_started" in phases:
            raise ProtocolRefusal("cross_enable_approval_invalid", "Resume requires the original enable plan and a current continuation.")
        start = prior
    else:
        if prior is not None and "disable_complete" not in phases:
            raise ProtocolRefusal("cross_enable_approval_invalid", "Resume the pending enable or disable it before starting another.")
        if prior is None:
            qualified = journal.read_committed()
            provisional = (observation.generation == 1 and observation.head_sha256 is not None
                           and observation.through_seq == 0 and not observation.records)
            if not store.prove_unbound() or (qualified.state != "absent" and not provisional):
                return _pending("cross_publication_pending", step="provenance")
        if prior is not None and (journal.read_committed().pending or prior.get("schema_version") not in (1, 2)):
            return _pending("cross_publication_pending", step="provenance")
        generation = 1 if prior is None else prior["enable_generation"] + 1
        if generation > MAX_INTEGER:
            raise ProtocolRefusal("cross_enable_generation_invalid", "The enable generation is exhausted.")
        if store.observe("anchor").outcome is not ReadOutcome.CONFIRMED_ABSENT:
            return _pending("cross_trust_store_pending")
        start = approved_start(secrets.token_hex(16), generation, secrets.token_hex(16), plan, schema_version=2)
        if confirm(start) is not True:
            raise ProtocolRefusal("cross_human_act_unavailable", "Enable was declined.")
        outside_seat(root)

    try:
        view = None
        if args.resume and "enable_complete" in phases:
            view = _completed_enable(journal, enable_inventory, start, journal.read_committed())
        if view is None:
            if not args.resume:
                journal.create(start["enable_id"], enable_generation=start["enable_generation"], attended=start)
            result = journal.append(_ENABLE, approved_completion(start))
            if result.status not in {"committed", "existing"}:
                return _pending(result.code or "cross_publication_pending", step="enable_complete")
            view = journal.read_committed(require_settled=True)
    except (OSError, ProtocolRefusal) as exc:
        return _pending(getattr(exc, "code", "cross_publication_pending"), step="enable")
    if _latest_start(view.records) != start or "disable_started" in _phases(view.records, start):
        return _pending("cross_enable_approval_invalid", step="enable")
    return "ok", {"state": "enabled", "enable_id": start["enable_id"],
                  "inventory": _inventory(view.records, start)}, 0


def _run_disable(args):
    root = FloatiRoot.open_direct_home(args.root)
    disk = CrossJournal.for_root(root)
    view = disk.read_committed() if args.dry_run else disk.inspect_disk()
    start = _latest_start(view.records)
    if args.dry_run:
        return "ok", {"state": "preview", "journal": view.state,
                      "inventory": _inventory(view.records, start)}, 0
    if start is None:
        raise ProtocolRefusal("cross_closeout_unproved", "Local history does not establish a scoped teardown.")
    if start.get("schema_version") not in (1, 2):
        raise ProtocolRefusal("cross_closeout_unproved", "Local history requires a qualified legacy teardown.")
    if verified_start(start)["plan"]["root"] != str(root.tenant_home):
        raise ProtocolRefusal("cross_closeout_attribution_unproved",
                              "The recorded enable belongs to a different root and key-store namespace.")
    store, inventory = open_backend(root, start["plan"]["store"])
    journal, _ = _journal(root, store, inventory, approve=lambda phase, body: False)
    progress = TeardownProgress()
    retry = ["floati", "cross", "disable", "--root", str(root.path)]
    try:
        result = journal.teardown_disable(_DISABLE, start, effects=inventory, progress=progress)
        # A retired anchor cannot make an admission read settled. The scoped
        # teardown verifies publication under exclusion before we read its receipt.
        view = journal.inspect_disk()
        anchor_absent = store.observe("anchor").outcome is ReadOutcome.CONFIRMED_ABSENT
    except (OSError, ProtocolRefusal) as exc:
        return _pending(getattr(exc, "code", "cross_closeout_unproved"), step="disable",
                        journal="pending", independent_denial="unknown", physical_teardown="pending",
                        retry=retry, **progress.evidence())
    phases = _phases(view.records, start)
    stop = _latest_start(view.records) == start and "disable_started" in phases
    if result["state"] == "complete" and anchor_absent and _latest_start(view.records) == start:
        receipt = next((row for row in reversed(view.records) if row["kind"] == "cross_teardown_receipt"
                        and row.get("enable_id") == start["enable_id"]
                        and row.get("enable_generation") == start["enable_generation"]
                        and row.get("outcome") == "complete"), None)
        if receipt is not None and "disable_complete" in phases:
            return "ok", {"state": "complete", "enable_id": start["enable_id"],
                          "independent_denial": "confirmed", "physical_teardown": "complete",
                          "receipt": semantic(receipt)}, 0
    return _pending(result.get("code", "cross_closeout_record_pending"),
                    step=result.get("step", "inventory"),
                    limiting_resource=result.get("limiting_resource"),
                    journal="published" if stop else "pending",
                    independent_denial="confirmed" if stop and anchor_absent else "unknown",
                    physical_teardown="pending", retry=retry,
                    enable_id=start["enable_id"], through_seq=view.through_seq,
                    **progress.evidence())
