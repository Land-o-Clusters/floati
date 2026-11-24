"""Native survivor planning; this module currently issues no persisted authority."""

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat

from .errors import IntegrityFailure, ProtocolRefusal
from .framing import encode_frame

MAX_CAPTURED_BYTES = 1024 * 1024 * 1024
MAX_CAPTURED_RECORDS = 1000000
MAX_DERIVED_RECORD_VISITS = 1000000
MAX_DERIVED_CANONICAL_BYTES = 1024 * 1024 * 1024
MAX_ORIGIN_DEPTH = 64


def _failure(detail):
    return IntegrityFailure("archive_checkpoint_invalid", detail)


def _unsupported(detail):
    return ProtocolRefusal("archive_checkpoint_unsupported", detail,
        remedy="retain the original epoch payload; do not retire this unsupported dependency")


class _CollectionBudget:
    """Invocation-owned capture and conservative expanded native work budget."""
    def __init__(self, root, witness=None, *, _opaque_members=False):
        self.root = root
        self.witness = None if witness is None else dict(witness)
        self._opaque_members = _opaque_members
        self.captured_bytes = self.captured_records = 0
        self.visits = self.expanded_bytes = self.depth = 0
        self._admitted, self._raw, self._sizes = set(), {}, {}
        self._account_owned = set()
        self.projections = {}

    def admit(self, members):
        from .jsonl import MAX_LEDGER_BYTES
        extra = 0
        pending = set()
        for path, identity in members:
            if identity is None:
                continue
            key = (path, identity)
            if not self._opaque_members and identity[4] > MAX_LEDGER_BYTES:
                raise _unsupported("captured native member exceeds its ledger byte bound")
            if key not in self._admitted and key not in pending:
                pending.add(key)
                extra += identity[4]
        if self.captured_bytes + extra > MAX_CAPTURED_BYTES:
            raise _unsupported("aggregate captured source byte bound exceeded before read")
        self._admitted.update(pending)
        self.captured_bytes += extra

    def read_member(self, path, status):
        try:
            return self._read_member(path, status)
        except OSError as exc:
            raise _failure("captured native source became unavailable or aliased") from exc

    def _read_member(self, path, status):
        from .bus_epoch import _CompressedStat, _selected_member_identity
        if isinstance(status, _CompressedStat):
            self.captured_records += status.payload.count(b"\n")
            return status.payload
        identity = _selected_member_identity(status)
        if (self.witness is not None
                and self.witness.get(path.relative_to(self.root.tenant_home).as_posix()) != identity):
            raise _failure("captured native source differs from the initial dependency witness")
        self.admit(((path, identity),))
        key = path, identity
        if key in self._raw:
            if _selected_member_identity(path.lstat()) != identity:
                raise _failure("captured source changed before reuse")
            return self._raw[key]
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            def bound(info):
                return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                        and (key not in self._account_owned or info.st_uid == os.getuid())
                        and _selected_member_identity(info) == identity)
            if not bound(os.fstat(descriptor)):
                raise _failure("captured source changed before bounded read")
            blocks, size, records = [], 0, 0
            while True:
                block = os.read(descriptor, min(65536, identity[4] - size + 1))
                if not block:
                    break
                size += len(block)
                records += block.count(b"\n")
                if size > identity[4]:
                    raise _failure("captured source grew beyond its bound identity")
                if self.captured_records + records > MAX_CAPTURED_RECORDS:
                    raise _unsupported("aggregate captured physical record bound exceeded before retention")
                blocks.append(block)
            if size != identity[4] or not bound(os.fstat(descriptor)) or not bound(path.lstat()):
                raise _failure("captured source identity changed during read")
            raw = b"".join(blocks)
            self.captured_records += records
            self._raw[key] = raw
            return raw
        finally:
            os.close(descriptor)

    def selected(self, selected):
        from .bus_epoch import _preflight_selected_members
        self.admit((path, identity) for _relative, path, _plane, identity in selected)
        if _preflight_selected_members(self.root) != selected:
            raise _failure("selected source changed before capture")
        from .bus_epoch import _selected_member_identity
        result = []
        for relative, path, plane, identity in selected:
            if identity is None:
                raw = b""
            else:
                status = path.lstat()
                if _selected_member_identity(status) != identity:
                    raise _failure("selected source identity changed before read")
                raw = self.read_member(path, status)
            result.append((relative, plane, raw))
        if _preflight_selected_members(self.root) != selected:
            raise _failure("selected source changed during capture")
        return tuple(result)

    def archive_payloads(self, home):
        from .bus_epoch import _selected_member_identity
        snapshot = self.archive_snapshot(home)
        self.admit((home / name, _selected_member_identity(status))
                   for name, plane, status in snapshot if plane is not None)
        payloads = tuple((name, plane, self.read_member(home / name, status))
                         for name, plane, status in snapshot if plane is not None)
        self.assert_archive_snapshot(home, snapshot)
        return payloads, snapshot

    def snapshot_bytes(self, relative, maximum, *, account_owned=False):
        path = self.root.tenant_home / relative
        try:
            status = path.lstat()
        except FileNotFoundError:
            if self.witness.get(Path(relative).as_posix()) is not None:
                raise _failure("captured source disappeared before native replay")
            return None
        except OSError as exc:
            raise _failure("captured source became unavailable") from exc
        if account_owned:
            self.account_owned(path, status)
        if status.st_size > maximum:
            raise _unsupported("captured source exceeds its native read bound")
        return self.read_member(path, status)

    def account_owned(self, path, status):
        from .bus_epoch import _selected_member_identity
        if status.st_uid != os.getuid():
            raise _failure("presentation coordinate must be account-owned before capture")
        self._account_owned.add((path, _selected_member_identity(status)))

    def archive_snapshot(self, home):
        """Planner census binds every parent to EX and the initial witness."""
        from .bus_epoch import _selected_member_identity
        relative = home.relative_to(self.root.tenant_home)
        status = home.lstat()
        if self.witness.get(relative.as_posix()) != _selected_member_identity(status):
            raise _failure("archive root differs from captured native namespace")
        if stat.S_ISREG(status.st_mode) and not stat.S_ISLNK(status.st_mode):
            from .bus_epoch import _epoch_archive_snapshot
            return _epoch_archive_snapshot(home)
        if not stat.S_ISDIR(status.st_mode):
            raise _failure("archive root differs from captured native namespace")
        members, pending = [(".", None, status)], [(relative, Path())]
        while pending:
            coordinate, inside = pending.pop()
            for name, status in _directory_entries(self.root, coordinate, self.witness, self):
                child = inside / name
                if stat.S_ISDIR(status.st_mode):
                    if name.endswith(".jsonl"):
                        raise _failure("archive contains a directory at a ledger coordinate")
                    members.append((child.as_posix(), None, status))
                    pending.append((coordinate / name, child))
                    continue
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    raise _failure("archive contains an aliased or special member")
                name_in_archive = child.as_posix()
                plane = ("events" if name_in_archive == "events.jsonl" else
                         "deliveries" if name_in_archive.startswith("receipts/deliveries/") and name_in_archive.endswith(".jsonl") else
                         "acks" if name_in_archive.startswith("receipts/acks/") and name_in_archive.endswith(".jsonl") else None)
                if plane is None:
                    raise _failure("archive contains an out-of-family physical member")
                members.append((name_in_archive, plane, status))
        if sum(plane == "events" for _name, plane, _status in members) != 1:
            raise _failure("archive must contain exactly one events.jsonl")
        return tuple(sorted(members, key=lambda member: member[0].encode("utf-8")))

    def assert_archive_snapshot(self, home, expected):
        from .bus_epoch import _selected_member_identity
        observed = self.archive_snapshot(home)
        before = tuple((name, plane, _selected_member_identity(status)) for name, plane, status in expected)
        after = tuple((name, plane, _selected_member_identity(status)) for name, plane, status in observed)
        if before != after:
            raise _failure("archive membership or identity changed after native capture")

    def source(self, source):
        count = sum(len(source[index]) for index in (0, 2, 4))
        holds = sum(row.get("kind") == "wake_hold_receipt" for row in source[2])
        visits = 4 * count + holds * (len(source[1]) + len(source[3]) + len(source[5]))
        if self.visits + visits > MAX_DERIVED_RECORD_VISITS or self.expanded_bytes >= MAX_DERIVED_CANONICAL_BYTES:
            raise _unsupported("derived source replay/copy budget exceeded before work")
        size = 32 + sum(67 * len(source[index]) for index in (1, 3, 5))
        for index in (0, 2, 4):
            for row in source[index]:
                key = id(row)
                if key not in self._sizes:
                    self._sizes[key] = (row, len(encode_frame(row)))
                size += self._sizes[key][1]
        # Native historical projection retains a bounded number of detached
        # admission/state views in addition to the exported source occurrence.
        size *= 4
        if self.expanded_bytes + size > MAX_DERIVED_CANONICAL_BYTES:
            raise _unsupported("derived source expanded byte budget exceeded before work")
        self.visits += visits
        self.expanded_bytes += size

    def raw_source(self, raw):
        # Repeated hashing/decoding is work even when no original hold is found
        # and no exported source copy is subsequently constructed.
        if self.expanded_bytes + len(raw) > MAX_DERIVED_CANONICAL_BYTES:
            raise _unsupported("derived raw source byte work bound exceeded before decode/hash")
        self.step(raw.count(b"\n"))
        self.expanded_bytes += len(raw)

    def step(self, visits=1):
        if self.visits + visits > MAX_DERIVED_RECORD_VISITS:
            raise _unsupported("derived source decode/validation visit bound exceeded")
        self.visits += visits

    def contract(self):
        from . import jsonl
        return (type(self).read_member, type(self)._read_member,
                type(self).archive_payloads, type(self).archive_snapshot,
                type(self).assert_archive_snapshot, type(self).snapshot_bytes, type(self).account_owned,
                type(self).source, type(self).raw_source, type(self).step,
                jsonl._decode_path_records, jsonl._decode_bounded_frames,
                jsonl.validate_record, jsonl.MAX_RECORD_BYTES,
                jsonl.MAX_LEDGER_BYTES, jsonl.MAX_LEDGER_RECORDS, _project, _copy_source)

    @contextmanager
    def origin(self):
        if self.depth >= MAX_ORIGIN_DEPTH:
            raise _unsupported("native imported origin depth bound exceeded")
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1


def _project(root, resolver, home, node, worker, source):
    from .epoch_carry import verified_hold_proofs
    from .wake_hold import project_wake_items
    budget = resolver._planning
    key = (home, node, worker, tuple((len(source[index]), source[index + 1][-1])
                                    for index in (0, 2, 4)))
    if budget is not None:
        key += (budget.contract(), verified_hold_proofs, project_wake_items)
        if key in budget.projections:
            return budget.projections[key]
        budget.source(source)
    proofs = verified_hold_proofs(root, source, node, worker, home=home, resolver=resolver)
    states = project_wake_items(
        events=source[0], deliveries=source[2], acknowledgments=source[4],
        recipient=node, worker_session_id=worker, tenant_id=root.tenant_id,
        event_prefix_digests=source[1], delivery_prefix_digests=source[3],
        acknowledgment_prefix_digests=source[5], _hold_proofs=proofs,
    )
    result = states, proofs
    if budget is not None:
        budget.projections[key] = result
    return result


def _hold(root, resolver, home, node, worker, source, receipt, proofs):
    from .wake_hold import WakeHoldController, original_wake_selection
    resolver._planning.source(source)
    if proofs is not None and proofs.validates(receipt):
        before, original = proofs.original(receipt)
    else:
        try:
            event_count = source[1].index(receipt["event_prefix_digest"])
            delivery_count = source[3].index(receipt["delivery_prefix_digest"])
            ack_count = source[5].index(receipt["acknowledgment_prefix_digest"])
        except ValueError as exc:
            raise _failure("hold origin has no exact native prefix") from exc
        before = (source[0][:event_count], source[1][:event_count + 1],
                  source[2][:delivery_count], source[3][:delivery_count + 1],
                  source[4][:ack_count], source[5][:ack_count + 1])
        original, _proofs = _project(root, resolver, home, node, worker, before)
    fresh, held = original_wake_selection(original, receipt)
    if not fresh and [item.message["id"] for item in held[:receipt["limit"]]] == receipt["item_ids"]:
        raise _unsupported("held redelivery lacks historical holder-release decision testimony")
    if [item.message["id"] for item in fresh[:receipt["limit"]]] != receipt["item_ids"]:
        raise _failure("hold does not reproduce its exact original semantic selection")
    artifact = WakeHoldController._artifact(
        tenant_id=root.tenant_id, recipient=node, worker_session_id=worker,
        limit=receipt["limit"], fresh=fresh, held=held, receipt=receipt,
        event_digest=receipt["event_prefix_digest"],
        delivery_digest=receipt["delivery_prefix_digest"],
        acknowledgment_digest=receipt["acknowledgment_prefix_digest"],
    )
    resolver._planning.source(before)
    return {"receipt": receipt, "before": _copy_source(before), "artifact": artifact}


def _copy_source(source):
    # Collector-owned rows remain shared until one final detached result copy.
    # Preserve the existing mutable public six-list/axis shape.
    return [list(axis) for axis in source]


def require_wd3_retirement_admission(root, roll_receipt):
    """Admit only original hold decisions in one receipt-bound retiring epoch.

    This adds no survivor publication or general retirement authority. Native
    maintenance already owns that authority; this check preserves the WD-3
    refusal when a historical redelivery has no durable release testimony.
    """
    from .bus_epoch import (_archive_facts_from_payloads,
        _assert_epoch_archive_snapshot, _read_epoch_archive_payloads,
        _require_owned_epoch_guard, validate_epoch_receipt_archive)
    from .epoch_carry import ArchiveProofResolver, _decode, _original_hold_cuts
    from .events import EVENT_KINDS, validate_event_records
    from .wake_hold import WakeHoldController, original_wake_selection

    _require_owned_epoch_guard(root, exclusive=True)
    archive = validate_epoch_receipt_archive(root, roll_receipt)
    payloads, snapshot = _read_epoch_archive_payloads(archive)
    facts = _archive_facts_from_payloads(payloads)
    if any(roll_receipt.get(field) != value for field, value in facts.items()):
        raise _failure("retiring archive differs from its exact roll receipt")
    raw_events = next(raw for name, _plane, raw in payloads if name == "events.jsonl")
    events = _decode(root, "events.jsonl", raw_events,
        EVENT_KINDS, WakeHoldController._EVENT_DOMAIN, events=True)[0]
    validate_event_records(events)
    # Frozen target bytes act as the resolver's live source. Only an imported
    # predecessor needed by an original decision causes an archive lookup.
    resolver = ArchiveProofResolver(root, frozen_payloads=payloads,
        head=events[0] if events else None)
    pairs = {(row["recipient"], row.get("worker_session_id")) for row in events
             if row["kind"] == "message_envelope"}
    for node, worker in sorted(pairs, key=lambda pair: (pair[0], pair[1] or "")):
        source = resolver.source(root.tenant_home, node, worker)
        if not any(row["kind"] == "wake_hold_receipt" for row in source[2]):
            continue
        # A missing original cut is an import only after native projection has
        # verified its exact archived origin. Broken prefixes confer nothing.
        _project(root, resolver, root.tenant_home, node, worker, source)
        for ordinal, receipt in enumerate(source[2]):
            if receipt["kind"] != "wake_hold_receipt":
                continue
            cuts = _original_hold_cuts(source, receipt, ordinal)
            if cuts is None:
                continue  # A carried copy is not a new native release decision.
            before, after = cuts
            original, _proofs = _project(root, resolver, root.tenant_home, node, worker, before)
            fresh, held = original_wake_selection(original, receipt)
            if not fresh and [item.message["id"] for item in held[:receipt["limit"]]] == receipt["item_ids"]:
                raise _unsupported("held redelivery lacks historical holder-release decision testimony")
            if [item.message["id"] for item in fresh[:receipt["limit"]]] != receipt["item_ids"]:
                raise _failure("hold does not reproduce its exact original semantic selection")
            _project(root, resolver, root.tenant_home, node, worker, after)
    _assert_epoch_archive_snapshot(archive, snapshot)


def _detached(data):
    # Reuse the independently bounded expanded-JSON traversal before the only
    # public deep copy. Shared source rows still count at every occurrence.
    from .archive_survivor_sources import _domain_digest
    _domain_digest(data["domain"], data)
    return deepcopy(data)


def _directory_entries(root, relative, witness=None, budget=None):
    """Enumerate fixed namespaces through no-follow retained descriptors."""
    from .bus_epoch import _require_owned_epoch_guard, _selected_member_identity
    held = _require_owned_epoch_guard(root, exclusive=True)
    descriptors = []
    try:
        descriptor = os.dup(held.descriptor)
        descriptors.append(descriptor)
        coordinate = Path()
        for part in Path(relative).parts:
            coordinate /= part
            status = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if (not stat.S_ISDIR(status.st_mode) or (witness is not None
                    and witness.get(coordinate.as_posix()) != _selected_member_identity(status))):
                raise _unsupported("presentation census parent changed before opening")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            descriptors.append(child)
            if _selected_member_identity(os.fstat(child)) != _selected_member_identity(status):
                raise _unsupported("presentation census parent changed before listing")
            descriptor = child
        expected = _selected_member_identity(os.fstat(descriptor))
        rows = []
        if budget is not None:
            budget.step()
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if budget is not None:
                    budget.step()
                if len(rows) >= 100000:
                    raise _unsupported("presentation census exceeds namespace bound")
                rows.append((entry.name, entry.stat(follow_symlinks=False)))
        if _selected_member_identity(os.fstat(descriptor)) != expected:
            raise _unsupported("presentation census parent changed during listing")
        return tuple(sorted(rows))
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise _failure("checkpoint namespace is unavailable or not an ordinary directory") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def collect_projection_sources(root):
    """Replay complete live/lineage projections and preserve original hold cuts.

    This is a read-only planning boundary, not a survivor issuer. The returned
    caller-owned dictionaries cannot authorize imported holds, publication or
    retirement. Presentation, execution, PID and off-lineage roll retry domains
    still need their own native collectors before any complete checkpoint exists.
    EX prevents cooperative epoch changes; the full dependency witness also
    detects changed selected or unselected input during this collection.
    """
    from . import bus_epoch
    from .bus_epoch import _require_owned_epoch_guard
    from .cursor import SparseCursor
    from .epoch_carry import ArchiveProofResolver, _decode, carry_dependency_witness
    from .events import EVENT_KINDS, validate_event_records
    from .records import is_known_record_kind
    from .wake_hold import WakeHoldController
    _require_owned_epoch_guard(root, exclusive=True)
    witness = carry_dependency_witness(root)
    if witness is None:
        raise _failure("projection dependency namespace cannot be captured")
    selected = bus_epoch._preflight_selected_members(root)
    budget = _CollectionBudget(root, witness)
    live = budget.selected(selected)
    resolver = ArchiveProofResolver(root, frozen_payloads=live, _planning=budget)
    archives = resolver.archives()
    cuts, holds, identities, epochs = [], {}, {}, []
    def remember(rows):
        for row in rows:
            frame = encode_frame(row)
            if row["id"] in identities and identities[row["id"]] != frame:
                raise _failure("historical record ID has conflicting native frames")
            identities[row["id"]] = frame
    owner = None
    for home in (root.tenant_home, *archives):
        if home == root.tenant_home:
            payloads = live
        else:
            payloads, _snapshot = budget.archive_payloads(home)
        payload_by_name = {relative: raw for relative, _plane, raw in payloads}
        budget.raw_source(payload_by_name["events.jsonl"])
        events, event_prefixes, physical = _decode(root, "events.jsonl", payload_by_name["events.jsonl"],
                         EVENT_KINDS, WakeHoldController._EVENT_DOMAIN, events=True)
        if any(not is_known_record_kind(row["kind"]) for _frame, row in physical):
            raise _unsupported("future event record semantics cannot establish complete survivor closure")
        validate_event_records(events)
        # A carried ID is immutable across all native cuts. Receipt IDs and
        # message IDs are domain-prefixed, but neither may borrow changed bytes.
        remember(events)
        pairs = {(row["recipient"], row.get("worker_session_id")) for row in events
                 if row["kind"] == "message_envelope"}
        cursor = SparseCursor(root)
        allowed = {"events.jsonl"}
        for node, worker in pairs:
            allowed.add(cursor._relative_path_for(node, worker_session_id=worker).as_posix())
            allowed.add(cursor._delivery_relative_path_for(node, worker_session_id=worker).as_posix())
        if any(raw and relative not in allowed for relative, raw in payload_by_name.items()):
            raise _failure("nonempty projection receipt source has no matching envelope plane")
        anchor = events[0]["id"] if events and events[0]["kind"] == "bus_epoch_roll_receipt" else None
        budget.source((events, event_prefixes, (), (), (), ()))
        header = {"epoch_id": owner["epoch_id"] if owner else None,
                  "automatic": "automatic" in owner if owner else None,
                  "roll_receipt": owner, "anchor": anchor,
                  "event_source": [events, list(event_prefixes)]}
        # Lineage is independent of message planes: an empty manual epoch still
        # cuts future first-ACT eligibility through the automatic chain.
        epochs.append(header)
        for node, worker in sorted(pairs, key=lambda pair: (pair[0], pair[1] or "")):
            source = resolver.source(home, node, worker)
            budget.source(source)
            _states, proofs = _project(root, resolver, home, node, worker, source)
            remember(source[2])
            remember(source[4])
            budget.source(source)
            cuts.append({**{key: value for key, value in header.items() if key != "event_source"},
                         "recipient": node, "worker_session_id": worker,
                         "source": _copy_source(source)})
            for receipt in source[2]:
                if receipt["kind"] != "wake_hold_receipt":
                    continue
                result = _hold(root, resolver, home, node, worker, source, receipt, proofs)
                if receipt["id"] in holds and holds[receipt["id"]] != result:
                    raise _failure("historical hold has conflicting original results")
                holds[receipt["id"]] = result
        owner = events[0] if anchor is not None else None
    resolver.archives()
    if carry_dependency_witness(root) != witness:
        raise _failure("projection dependencies changed during native collection")
    return _detached({"schema_version": 1, "domain": "projection_sources",
            "tenant_id": root.tenant_id, "epochs": epochs, "cuts": cuts,
            "holds": [holds[key] for key in sorted(holds)]})


def collect_presentation_sources(root):
    """Replay all retained presentation groups, including completed attempts.

    Existing native write-ahead state contributes its effective reserved frame
    without appending it. Results preserve original prefix cuts and authority
    references, but grant no new consent, claim or presentation admission.
    """
    from . import bus_epoch
    from .bus_epoch import _require_owned_epoch_guard
    from .codex_wait_contract import CodexWaitSessionLedger, consent_ledger_relative
    from .cursor_presentations import CursorPresentationLedger
    from .epoch_carry import ArchiveProofResolver, carry_dependency_witness
    from .jsonl import MAX_LEDGER_BYTES, _decode_path_records
    _require_owned_epoch_guard(root, exclusive=True)
    witness = carry_dependency_witness(root)
    if witness is None:
        raise _failure("presentation dependency namespace cannot be captured")
    selected = bus_epoch._preflight_selected_members(root)
    budget = _CollectionBudget(root, witness)
    live = budget.selected(selected)
    resolver = ArchiveProofResolver(root, frozen_payloads=live, _planning=budget)
    resolver.archives()
    nodes = set()
    census_witness = dict(witness)
    for name, status in _directory_entries(root, "receipts/cursor-presentations", census_witness, budget):
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise _failure("presentation history namespace contains an aliased or special source")
        if Path(name).suffix == ".jsonl":
            nodes.add(Path(name).stem)
    for name, status in _directory_entries(root, "state/cursor-wait", census_witness, budget):
        if stat.S_ISLNK(status.st_mode):
            raise _failure("presentation witness parent is a symlink")
        if stat.S_ISDIR(status.st_mode) and any(
                leaf == "presentation-history.v1.json" for leaf, _status in
                _directory_entries(root, Path("state/cursor-wait") / name, census_witness, budget)):
            nodes.add(name)
    entries = []
    authority_cache = {}
    def authority_rows(relative, kinds):
        raw = budget.snapshot_bytes(relative, MAX_LEDGER_BYTES)
        if raw is None:
            return []
        budget.raw_source(raw)
        key = (relative, frozenset(kinds), budget.contract())
        if key not in authority_cache:
            authority_cache[key] = _decode_path_records(root.tenant_home / relative,
                root.tenant_id, frozenset(kinds), raw)
        return authority_cache[key]
    class CollectedPresentation(CursorPresentationLedger):
        def _bytes(self, relative, maximum=MAX_LEDGER_BYTES, *, _borrow=False):
            return budget.snapshot_bytes(relative, maximum, account_owned=True)
        def _sources(self, home, node, *, _borrow=False):
            # The native resolver revalidates every source/epoch identity. Its
            # collector budget covers archived sources as well as frozen live.
            return resolver.source(home, node)
    for node in sorted(nodes):
        plane = CollectedPresentation(root, _epoch_resolver=resolver, _authority_reader=authority_rows)
        # All fixed physical inputs are captured/charged before native _load.
        relatives = (plane.relative(node), plane.witness_relative(node),
                     consent_ledger_relative(node), CodexWaitSessionLedger._relative(node))
        for relative in relatives:
            path = root.tenant_home / relative
            try:
                status = path.lstat()
            except FileNotFoundError:
                continue
            if relative in (plane.relative(node), plane.witness_relative(node)):
                budget.account_owned(path, status)
            if relative == plane.witness_relative(node) and status.st_size > 131072:
                raise _unsupported("presentation witness exceeds its native capture bound")
            budget.raw_source(budget.read_member(path, status))
        physical = plane._bytes(plane.relative(node))
        state, _effective, rows, groups, _anchor, epochs = plane._load(node, materialize=False)
        consents = authority_rows(consent_ledger_relative(node), {"codex_wait_consent_receipt"})
        claims = authority_rows(CodexWaitSessionLedger._relative(node), {"codex_wait_session_receipt"})
        consent_ids, claim_ids, positions = {}, {}, {}
        for row in consents:
            consent_ids.setdefault(row["id"], row)
        for row in claims:
            claim_ids.setdefault(row["id"], row)
        for epoch, source in epochs.items():
            axes = []
            for axis in (1, 3, 5):
                first = {}
                for ordinal, digest in enumerate(source[axis]):
                    first.setdefault(digest, ordinal)
                axes.append(first)
            positions[epoch] = axes
        result = []
        for batch in groups.values():
            prepared = batch[0]
            source = epochs[prepared["epoch_anchor"]]
            budget.source(source)
            counts = [positions[prepared["epoch_anchor"]][axis][prepared[field]] for axis, field in enumerate((
                "event_prefix_digest", "delivery_prefix_digest", "acknowledgment_prefix_digest"))]
            result.append({"records": list(batch),
                           "source": _copy_source(source), "counts": counts,
                           "eligible_ids": list(prepared["item_ids"]),
                           "consent": consent_ids[prepared["consent_receipt_id"]],
                           "claim": claim_ids.get(prepared["claim_receipt_id"])})
        entries.append({"recipient": node, "witness": state,
                        "physical_byte_length": len(physical),
                        "physical_sha256": hashlib.sha256(physical).hexdigest(),
                        "records": rows, "groups": result})
    resolver.archives()
    if carry_dependency_witness(root) != witness:
        raise _failure("presentation dependencies changed during native collection")
    return _detached({"schema_version": 1, "domain": "presentation_sources",
            "tenant_id": root.tenant_id, "entries": entries})


def _compressed_retry_header(root, name, initial, budget, held):
    """Read record one from one verified compressed archive file."""
    from .bus_epoch import (
        _parse_physical_first_roll_receipt, _read_epoch_archive_payloads,
        _selected_member_identity,
    )

    parent = os.dup(held.descriptor)
    try:
        status = os.stat(name, dir_fd=parent, follow_symlinks=False)
        expected = initial.get(name)
        if (stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode)
                or _selected_member_identity(status) != expected
                or _selected_member_identity(os.fstat(parent)) != initial.get(".")):
            raise _failure("roll retry header differs from initial identity")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if _selected_member_identity(os.fstat(descriptor)) != expected:
                raise _failure("roll retry header changed before bounded read")
            magic = os.read(descriptor, 2)
        finally:
            os.close(descriptor)
        if magic != b"PK":
            return None
        archive = root.tenant_home / name
        if _selected_member_identity(archive.lstat()) != expected:
            raise _failure("roll retry header changed before bounded read")
        try:
            payloads, _snapshot = _read_epoch_archive_payloads(archive)
        except (OSError, ProtocolRefusal, IntegrityFailure) as exc:
            raise _failure("captured roll retry header is unavailable or aliased") from exc
        raw = next((payload for member, _plane, payload in payloads if member == "events.jsonl"), b"")
        newline = raw.find(b"\n")
        frame = raw if newline < 0 else raw[:newline + 1]
        if len(frame) > 65537:
            frame = frame[:65537]
        extra = max(0, frame.count(b"\n") - 1)
        if budget.captured_records + extra > MAX_CAPTURED_RECORDS:
            raise _unsupported("aggregate retry header physical record bound exceeded before retention")
        budget.step(extra)
        budget.captured_records += extra
        if (_selected_member_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected
                or _selected_member_identity(os.fstat(parent)) != initial.get(".")):
            raise _failure("roll retry header or parent changed during bounded read")
        receipt = _parse_physical_first_roll_receipt(frame, root.tenant_id)
        if receipt is not None:
            budget.raw_source(encode_frame(receipt))
        return receipt
    except OSError as exc:
        raise _failure("captured roll retry header is unavailable or aliased") from exc
    finally:
        os.close(parent)


def _completed_roll_retry_receipts(root, witness, budget):
    """Capture the native record-one census from the initial namespace only."""
    from .bus_epoch import (_parse_physical_first_roll_receipt,
                            _require_owned_epoch_guard, _selected_member_identity)
    held = _require_owned_epoch_guard(root, exclusive=True)
    initial = dict(witness)

    def root_census():
        if _selected_member_identity(os.fstat(held.descriptor)) != initial.get("."):
            raise _failure("roll retry root differs from initial header namespace")
        rows = _directory_entries(root, Path(), initial, budget)
        expected = {name: identity for name, identity in initial.items()
                    if name != "." and "/" not in name}
        if {name: _selected_member_identity(status) for name, status in rows} != expected:
            raise _failure("roll retry header namespace changed after capture")
        return rows

    rows = root_census()
    ordered = sorted(rows, key=lambda row: row[0].encode("utf-8"))
    candidates = [Path("events.jsonl")] + [Path(name) / "events.jsonl"
        for name, status in ordered
        if name.startswith("archive-") and stat.S_ISDIR(status.st_mode)]
    compressed = [name for name, status in ordered
        if name.startswith("archive-") and not stat.S_ISLNK(status.st_mode)
        and stat.S_ISREG(status.st_mode)]
    header_count = len(candidates) + len(compressed)
    reserved = header_count * 65537
    if (budget.captured_bytes + reserved > MAX_CAPTURED_BYTES
            or budget.captured_records + header_count > MAX_CAPTURED_RECORDS
            or budget.expanded_bytes + reserved > MAX_DERIVED_CANONICAL_BYTES):
        raise _unsupported("aggregate retry header capture/decode bound exceeded before read")
    budget.step(header_count)
    budget.captured_bytes += reserved
    budget.captured_records += header_count
    budget.expanded_bytes += reserved
    receipts = []
    for relative in candidates:
        descriptors = []
        try:
            parent = os.dup(held.descriptor)
            descriptors.append(parent)
            if relative.parent != Path():
                coordinate = relative.parent.as_posix()
                status = os.stat(coordinate, dir_fd=parent, follow_symlinks=False)
                if (not stat.S_ISDIR(status.st_mode)
                        or _selected_member_identity(status) != initial.get(coordinate)):
                    raise _failure("roll retry header parent differs from initial namespace")
                parent = os.open(coordinate, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=parent)
                descriptors.append(parent)
                if _selected_member_identity(os.fstat(parent)) != initial.get(coordinate):
                    raise _failure("roll retry header parent changed before open")
            else:
                coordinate = "."
            expected = initial.get(relative.as_posix())
            try:
                status = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if expected is not None:
                    raise _failure("captured roll retry header disappeared")
                continue
            if _selected_member_identity(status) != expected:
                raise _failure("roll retry header differs from initial identity")
            # Native record-one selection skips nonregular leaves and permits
            # regular nlink>1. Payload admission retains its separate contract.
            if not stat.S_ISREG(status.st_mode):
                continue
            descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
            descriptors.append(descriptor)
            if _selected_member_identity(os.fstat(descriptor)) != expected:
                raise _failure("roll retry header changed before bounded read")
            blocks, size, physical_records = [], 0, 0
            maximum = min(65537, expected[4] + 1)
            while size < maximum:
                block = os.read(descriptor, min(65536, maximum - size))
                if not block:
                    break
                size += len(block)
                records = physical_records + block.count(b"\n")
                extra = max(0, records - 1) - max(0, physical_records - 1)
                if budget.captured_records + extra > MAX_CAPTURED_RECORDS:
                    raise _unsupported("aggregate retry header physical record bound exceeded before retention")
                budget.step(extra)
                budget.captured_records += extra
                physical_records = records
                newline = block.find(b"\n")
                blocks.append(block if newline < 0 else block[:newline + 1])
                if newline >= 0:
                    break
            if (_selected_member_identity(os.fstat(descriptor)) != expected
                    or _selected_member_identity(os.stat(relative.name, dir_fd=parent,
                                                          follow_symlinks=False)) != expected
                    or _selected_member_identity(os.fstat(parent)) != initial.get(coordinate)):
                raise _failure("roll retry header or parent changed during bounded read")
            receipt = _parse_physical_first_roll_receipt(b"".join(blocks), root.tenant_id)
            if receipt is not None:
                # Native decoding returns one bounded schema row. Charge its
                # canonical metadata before retaining it in the census result.
                budget.raw_source(encode_frame(receipt))
                receipts.append(receipt)
        except OSError as exc:
            raise _failure("captured roll retry header is unavailable or aliased") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    for name in compressed:
        receipt = _compressed_retry_header(root, name, initial, budget, held)
        if receipt is not None:
            receipts.append(receipt)
    root_census()
    return tuple(receipts)


def collect_roll_retry_sources(root):
    """Verify every completed native retry receipt, including off-lineage.

    Off-lineage evidence remains confined to retry uniqueness. Its receipt-
    named archive is verified, but is never added to ordinary history traversal.
    These results remain unprivileged planning data, not completed retirement.
    """
    from . import bus_epoch
    from .bus_epoch import _require_owned_epoch_guard
    from .epoch_carry import carry_dependency_witness
    _require_owned_epoch_guard(root, exclusive=True)
    witness = carry_dependency_witness(root)
    if witness is None:
        raise _failure("roll retry dependency namespace cannot be captured")
    # Retry facts describe opaque native archive bytes, rather than decoded
    # live ledgers. Preserve that contract while bounding the whole collection.
    budget = _CollectionBudget(root, witness, _opaque_members=True)
    receipts = _completed_roll_retry_receipts(root, witness, budget)
    def archive_snapshot(home, expected=None):
        try:
            if expected is None:
                return budget.archive_snapshot(home)
            budget.assert_archive_snapshot(home, expected)
        except (OSError, IntegrityFailure) as exc:
            raise IntegrityFailure("epoch_receipt_archive_invalid",
                                   "completed roll receipt archive is unavailable or invalid") from exc
    keys = set()
    captured = []
    for receipt in receipts:
        key = receipt["idempotency_key"]
        if key in keys:
            raise IntegrityFailure("epoch_receipt_duplicate",
                                   "multiple completed roll receipts claim one idempotency key")
        keys.add(key)
        archive = bus_epoch._epoch_receipt_archive_path(root, receipt)
        snapshot = archive_snapshot(archive)
        budget.admit((archive / name, bus_epoch._selected_member_identity(status))
                     for name, plane, status in snapshot if plane is not None)
        captured.append((receipt, archive, snapshot))
    # All receipt-owned byte inventories have been admitted before ANY opaque
    # payload is read. The original receipt census still includes off-lineage.
    entries = []
    for receipt, archive, snapshot in captured:
        archive_snapshot(archive, snapshot)
        payloads = tuple((name, plane, budget.read_member(archive / name, status))
                         for name, plane, status in snapshot if plane is not None)
        archive_snapshot(archive, snapshot)
        # Empty opaque members still expand the canonical facts manifest and
        # exported inventory. Bound each small metadata row before native facts
        # builds its whole manifest or the final detached output is allocated.
        for name, plane, raw in payloads:
            metadata = encode_frame({"path": name, "plane": plane, "byte_length": len(raw), "sha256": "0" * 64})
            budget.raw_source(metadata)
            budget.raw_source(metadata)
            budget.raw_source(raw)
        facts = bus_epoch._archive_facts_from_payloads(payloads)
        if any(receipt.get(field) != value for field, value in facts.items()):
            raise IntegrityFailure("epoch_receipt_archive_invalid",
                                   "completed roll receipt facts do not match its archive")
        budget.raw_source(encode_frame(receipt))
        budget.raw_source(encode_frame(facts))
        inventory = []
        for relative, plane, raw in payloads:
            budget.raw_source(raw)
            inventory.append({"path": relative, "plane": plane,
                              "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        archive_snapshot(archive, snapshot)
        entries.append({"receipt": receipt, "facts": facts,
                        "inventory": inventory})
    _require_owned_epoch_guard(root, exclusive=True)
    if carry_dependency_witness(root) != witness:
        raise _failure("roll retry dependencies changed during native collection")
    return _detached({"schema_version": 1, "domain": "roll_retry_sources",
            "tenant_id": root.tenant_id, "entries": entries})
