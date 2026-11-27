"""Integration with the journal's charge and close-out paths and the bound gate."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from secrets import token_hex
from typing import Callable, Optional, Protocol

from .cross_anchor import Anchor
from .cross_coordinates import semantic
from .framing import encode_frame
from .cross_authority import Capability, check
from .cross_pair import FinalOutput
from .cross_gate import GateImage, GateOwner, replace_gate
from .cross_reserve import Budget, admit, project
from .errors import ProtocolRefusal


_HEX16 = re.compile(r"[0-9a-f]{32}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass(frozen=True)
class Opening:
    opening_id: str
    token: str
    enable_id: str
    enable_generation: int
    role: str

    def scope(self) -> dict:
        return dict(ceremony_id=self.opening_id, ceremony_token=self.token,
                    enable_id=self.enable_id, enable_generation=self.enable_generation)

    def capability(self) -> Capability:
        return Capability("A", verb="pair_show" if self.role == "S" else "pair_join", ceremony_token=self.token)


def _published(result) -> None:
    if result.status != "committed":
        raise ProtocolRefusal(result.code or "cross_pairing_publication_unknown",
                              "The pairing change could not be verified.")


class JournalOpenings:
    """Low-level charge/close operations. The caller holds ceremony exclusion."""

    def __init__(self, journal):
        self.journal = journal

    def open(self, role: str) -> Opening:
        if role not in ("S", "T"):
            raise ValueError("invalid local pairing role")
        journal = self.journal
        if not journal.directory.is_dir():
            raise ProtocolRefusal("cross_disabled", "Cross-machine pairing is disabled.")
        with journal._lock(exclusive=True):
            journal._maintain_as(Capability("A", verb="pair_show" if role == "S" else "pair_join"))
            loaded = journal._load()
            if loaded is None:
                raise ProtocolRefusal("cross_disabled", "Cross-machine pairing is disabled.")
            rows, _ = journal._verified(loaded[0])
            state = project(rows)
            if "enable_complete" not in state.phases or "disable_started" in state.phases:
                raise ProtocolRefusal("cross_disabled", "Cross-machine pairing is disabled.")
            start = next(row for row in reversed(rows) if row.get("phase") == "enable_started")
            opening = Opening(token_hex(16), token_hex(16), start["enable_id"], start["enable_generation"], role)
            if any(row.get("ceremony_id") == opening.opening_id or row.get("ceremony_token") == opening.token
                   for row in rows):
                raise ProtocolRefusal("cross_pairing_collision", "The pairing attempt has a conflicting identity.")
            record = dict(opening.scope(), kind="cross_ceremony_opened", schema_version=1,
                          id=f"cross-ceremony-opened-{opening.opening_id}")
            capability = opening.capability()
            check(capability, journal._validate(record))
            # The journal owns the continuing hold. This preflight prevents
            # exposure when even the initial future activation and review
            # cannot fit in the ordinary budget of the pinned journal.
            checkpoint = loaded[0]
            framed = dict(record, utc_day="2000-01-01", seq=checkpoint["through_seq"] + 1,
                          prev=checkpoint["head_sha256"])
            budget = Budget(journal.budget.bytes - 65536, journal.budget.slots - 2)
            admit(state, framed, len(encode_frame(framed)), budget, journal.data_budget)
            _published(journal._append_with_reserve_file(capability, record))
        return opening

    def check_candidate(self, opening: Opening, pair_id: str, label: str) -> None:
        """Check the settled pre-display state. Activation must recheck under its write lock."""
        view = self.journal.read_committed(require_settled=True)
        state = project(view.records)
        if "enable_complete" not in state.phases or "disable_started" in state.phases:
            raise ProtocolRefusal("cross_disabled", "Cross-machine pairing is disabled.")
        start = next(row for row in reversed(view.records) if row.get("phase") == "enable_started")
        if (opening.opening_id not in state.open_ceremonies
            or not self._owns_opening(view.records, opening)
            or (opening.enable_id, opening.enable_generation) != (start["enable_id"], start["enable_generation"])):
            raise ProtocolRefusal("cross_ceremony_token_invalid", "The local pairing ownership could not be verified.")
        if not isinstance(pair_id, str) or not _HEX16.fullmatch(pair_id):
            raise ProtocolRefusal("cross_pairing_protocol", "The pairing message did not match this step.")
        if any(row.get("pair_id") == pair_id and row["kind"] in {"cross_pair_active", "cross_pair_revoked"}
               for row in view.records):
            raise ProtocolRefusal("cross_pairing_collision", "The pairing attempt has a conflicting identity.")
        if view.generation > 1:
            raise ProtocolRefusal("cross_pair_history_unknown", "The root cannot verify earlier pair identities.")
        if len(state.active_pairs) >= 64:
            raise ProtocolRefusal("cross_pair_capacity", "This root already has 64 active pairs.")
        if any(row.get("label") == label for row in state.active_pairs.values()):
            raise ProtocolRefusal("cross_pair_label_in_use", "This label already belongs to an active pair.")

    @staticmethod
    def _owns_opening(rows, opening: Opening) -> bool:
        scope = opening.scope()
        return any(row["kind"] == "cross_ceremony_opened" and all(row.get(key) == value for key, value in scope.items())
                   for row in rows)

    def close(self, opening: Opening, *, abandoned=False) -> None:
        """Release the charge's live token after cleanup is settled. The charge remains."""
        journal = self.journal
        capability = Capability("M", verb="maintain") if abandoned else opening.capability()
        record = dict(opening.scope(), kind="cross_ceremony_closed", schema_version=2,
                      id=f"cross-ceremony-closed-{opening.opening_id}", cause="crash" if abandoned else "abort")
        if not journal.directory.is_dir():
            raise ProtocolRefusal("cross_pairing_publication_unknown", "The pairing change could not be verified.")
        with journal._lock(exclusive=True):
            loaded = journal._load()
            if loaded is None or journal._maintenance_due():
                raise ProtocolRefusal("cross_publication_pending", "The pairing change could not be verified.")
            rows, _ = journal._verified(loaded[0])
            if opening.opening_id not in project(rows).open_ceremonies:
                return
            if not self._owns_opening(rows, opening):
                raise ProtocolRefusal("cross_ceremony_token_invalid", "The local pairing ownership could not be verified.")
            check(capability, journal._validate(record))
            _published(journal._append_with_reserve_file(capability, record))


def require_gate_owner(image: GateImage, owner: GateOwner, direction: str) -> tuple:
    """Match both slots after the caller has established settled activation."""
    fields = owner.fields()
    # Reparse the whole value, including retained pairs, before using one row.
    validated = GateImage.decode(image.encoded())
    entries = json.loads(validated.encoded())["entries"]
    if (owner.journal_id != validated.journal_id or direction not in {"S-to-T", "T-to-S"}
        or any(key not in entries or any(entries[key].get(k) != v for k, v in fields.items()) for key in owner.keys())):
        raise ProtocolRefusal("cross_gate_ownership_unproved", "The gate does not prove this pair's ownership.")
    selected = entries[f"{owner.pair_id}/{direction}"]
    return tuple(selected[key] for key in ("out_next", "in_hwm", "max_session"))


class PairGate:
    """Gate changes under the caller's ceremony and journal exclusion."""

    def __init__(self, storage):
        self.storage = storage

    def _image(self) -> GateImage:
        raw = self.storage.read("gate-state")
        if raw is None:
            raise ProtocolRefusal("cross_gate_ownership_unproved", "The bound gate is unavailable.")
        return GateImage.decode(raw)

    def provision(self, owner: GateOwner) -> None:
        initial = self._image()
        replace_gate(self.storage, initial, initial.provision(owner))

    def collect(self, owner: GateOwner, *, abandoned: bool, activated: bool) -> None:
        if abandoned and not activated:
            initial = self._image()
            successor = initial.collect(owner, abandoned=True, activated=False)
            temporary = self.storage.read(".gate-state.tmp")
            if temporary is not None:
                # Only a complete, exact replacement attributable to this
                # abandoned token can be disposed. Torn or foreign bytes fence.
                if temporary == initial.encoded() or (
                    temporary != successor.encoded() and temporary != initial.provision(owner).encoded()
                ):
                    raise ProtocolRefusal("cross_gate_ownership_unproved", "The unfinished gate replacement is unproved.")
                self.storage.unlink(".gate-state.tmp")
                self.storage.sync_directory()
            replace_gate(self.storage, initial, successor)

    def require(self, owner: GateOwner) -> None:
        if self.storage.read(".gate-state.tmp") is not None:
            raise ProtocolRefusal("cross_gate_ownership_unproved", "The gate replacement is still unfinished.")
        require_gate_owner(self._image(), owner, "S-to-T")


@dataclass(frozen=True)
class PairBinding:
    owner: GateOwner
    activation_sha256: str
    local_der_sha256: str

    @classmethod
    def from_record(cls, owner: GateOwner, record: dict) -> PairBinding:
        body = semantic(record)
        fields = owner.fields()
        local = body.get("opening_id")
        wire = body.get("wire_ceremony_id", body.get("ceremony_id"))
        if (not all(isinstance(value, str) and _HEX16.fullmatch(value) for value in (local, wire))
            or local == wire or body.get("kind") != "cross_pair_active" or body.get("pair_id") != owner.pair_id
            or any(body.get(key) != value for key, value in fields.items())
            or body.get("role") not in ("S", "T")
            or not isinstance(body.get("human_act"), dict)
            or body["human_act"].get("channel") != "controlling_terminal"
            or not all(isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) for value in (
                body.get("local_der_sha256"), body.get("transcript_receipt_digest"),
                body["human_act"].get("rendered_digest"),
            ))):
            raise _material_unknown()
        return cls(owner, sha256(encode_frame(body)).hexdigest(), body["local_der_sha256"])


@dataclass(frozen=True, repr=False)
class IndependentPairItem:
    binding: PairBinding
    gate_root: bytes


@dataclass(frozen=True)
class PairFiles:
    owner: GateOwner
    leaf_sha256: str
    key_leaf_sha256: str


def _material_unknown():
    return ProtocolRefusal("cross_pairing_material_unknown", "The pair's local material could not be verified.")


class PairMaterial(Protocol):
    """Read independent item and file/key evidence in the exact owned scope."""

    def sync_staging(self, owner: GateOwner, local_der: bytes) -> None: ...
    def create_item(self, item: IndependentPairItem) -> None: ...
    def promote(self, owner: GateOwner) -> None: ...
    def read_item(self, owner: GateOwner) -> Optional[IndependentPairItem]: ...
    def read_files(self, owner: GateOwner) -> Optional[PairFiles]: ...
    def discard(self, owner: GateOwner) -> str: ...


class PairEffects:
    """Proof and effects under the owning ceremony and journal exclusion."""

    def __init__(self, gate: PairGate, material: PairMaterial):
        self.gate = gate
        self.material = material

    def activate(self, binding: PairBinding, local_der: bytes, gate_root: bytes, *,
                 publish: Callable[[], None], advance_anchor: Callable[[], None]) -> None:
        owner = binding.owner
        if sha256(local_der).hexdigest() != binding.local_der_sha256:
            raise _material_unknown()
        self.material.sync_staging(owner, local_der)
        self.material.create_item(IndependentPairItem(binding, gate_root))
        self.material.promote(owner)
        self.gate.provision(owner)
        self.require(binding, gate_root)
        publish()
        self.require(binding, gate_root)
        advance_anchor()
        self.require(binding, gate_root)

    def require(self, binding: PairBinding, gate_root: Optional[bytes] = None) -> None:
        owner = binding.owner
        item = self.material.read_item(owner)
        files = self.material.read_files(owner)
        if (type(item) is not IndependentPairItem or item.binding != binding
            or type(item.gate_root) is not bytes or len(item.gate_root) != 32
            or (gate_root is not None and item.gate_root != gate_root)
            or type(files) is not PairFiles
            or files != PairFiles(owner, binding.local_der_sha256, binding.local_der_sha256)):
            raise _material_unknown()
        self.gate.require(owner)

    def require_record(self, journal_id: str, record: dict, gate_root: Optional[bytes] = None) -> PairBinding:
        owner = GateOwner(journal_id, record.get("enable_id"), record.get("enable_generation"),
                          record.get("ceremony_token"), record.get("pair_id"))
        binding = PairBinding.from_record(owner, record)
        self.require(binding, gate_root)
        return binding

    def collect(self, owner: GateOwner, *, abandoned: bool, publication: str) -> bool:
        """Remove only the independently inventoried owner of an absent activation."""
        if not abandoned or publication != "absent":
            return False
        self.gate.collect(owner, abandoned=True, activated=False)
        return self.material.discard(owner) == "confirmed_absent"


class JournalFinal:
    """One locally owned final, with its stream fixed during activation."""

    def __init__(self, journal, opening: Opening, activation, stream, effects: PairEffects, binding: PairBinding):
        self.journal = journal
        self.opening = opening
        self.activation = activation
        self.stream = stream
        self.effects = effects
        self.binding = binding

    def deposit(self, opening: Opening, output: FinalOutput) -> None:
        if (opening is not self.opening or type(output) is not FinalOutput
            or output.stream is not self.stream or output.activation != self.activation):
            raise _material_unknown()
        journal = self.journal
        with journal._lock(exclusive=True):
            loaded = journal._load()
            if loaded is None or journal._maintenance_due():
                return
            checkpoint, raw = loaded
            records, _ = journal._verified(checkpoint)
            state = project(records)
            if "disable_started" in state.phases or "enable_complete" not in state.phases:
                return
            anchor = journal._observed_store()
            if not isinstance(anchor, Anchor) or anchor != journal._anchor_coordinate(checkpoint, raw, records):
                return
            journal._check_pairing_material(records)
            activation = self.activation
            active = state.active_pairs.get(activation.pair_id)
            expected = {
                "opening_id": opening.opening_id,
                "ceremony_token": opening.token,
                "enable_id": opening.enable_id,
                "enable_generation": opening.enable_generation,
                "role": activation.role,
                "local_der_sha256": activation.local_der_sha256,
                "peer_sha256": activation.peer_sha256,
                "transcript_receipt_digest": activation.transcript_receipt_digest,
                "human_act": {"channel": "controlling_terminal", "rendered_digest": activation.rendered_digest},
            }
            if (active is None or any(active.get(key) != value for key, value in expected.items())
                or active.get("wire_ceremony_id", active.get("ceremony_id")) != activation.wire_id
                or PairBinding.from_record(self.binding.owner, active) != self.binding):
                return
            self.effects.require_record(self.binding.owner.journal_id, active)
            output.enqueue()


@dataclass(frozen=True)
class UnpairReceipt:
    state: str
    pair_id: str
    record_id: Optional[str] = None


def unpair(journal, pair_id: str, idempotency_key: str) -> UnpairReceipt:
    """Retire one immutable pair through the journal's reserved local revocation."""
    if (not isinstance(pair_id, str) or not _HEX16.fullmatch(pair_id)
        or not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key)):
        raise ProtocolRefusal("cross_unpair_identity_invalid", "Use the pair ID and idempotency key from the pairing result.")
    view = journal.read_committed(require_settled=True)
    if view.state == "absent":
        return UnpairReceipt("absent", pair_id)
    state = project(view.records)
    activation = next((row for row in reversed(view.records)
                       if row["kind"] == "cross_pair_active" and row.get("pair_id") == pair_id), None)
    revoked = next((row for row in reversed(view.records)
                    if row["kind"] == "cross_pair_revoked" and row.get("pair_id") == pair_id), None)
    if activation is None:
        if revoked is not None and (revoked.get("cause") != "local" or revoked.get("idempotency_key") != idempotency_key):
            return UnpairReceipt("conflict", pair_id)
        covered = view.generation == 1 and revoked is None
        return UnpairReceipt("absent" if covered else "unknown", pair_id)

    original = activation
    if "enable_id" not in original and view.generation == 1:
        original = next((row for row in reversed(view.records)
                         if row.get("phase") == "enable_started" and row["seq"] < activation["seq"]), {})
    identity = original.get("enable_id"), original.get("enable_generation")
    if not isinstance(identity[0], str) or not _HEX16.fullmatch(identity[0]) or type(identity[1]) is not int:
        return UnpairReceipt("unknown", pair_id)
    record = dict(schema_version=1, id=f"cross-pair-revoked-{pair_id}", kind="cross_pair_revoked", pair_id=pair_id,
                  cause="local", idempotency_key=idempotency_key, enable_id=identity[0], enable_generation=identity[1])
    capability = Capability("L", pair_id=pair_id, verb="unpair")
    if pair_id not in state.active_pairs:
        if revoked is None:
            return UnpairReceipt("unknown", pair_id)
        if semantic(revoked) != record:
            return UnpairReceipt("conflict", pair_id)
        return UnpairReceipt("revoked", pair_id, record["id"])
    start = next((row for row in reversed(view.records) if row.get("phase") == "enable_started"), {})
    if identity != (start.get("enable_id"), start.get("enable_generation")):
        return UnpairReceipt("conflict", pair_id)
    result = journal.append(capability, record)
    if result.status in {"committed", "existing"}:
        return UnpairReceipt("revoked", pair_id, result.record_id)
    return UnpairReceipt("unknown", pair_id)
