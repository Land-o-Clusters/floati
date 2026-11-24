"""Encode native source planning data; these bytes grant no admission."""

from . import archive_source_pool as source_pool
from .errors import IntegrityFailure
from .framing import encode_frame
from .wake_hold import WakeHoldController
from .archive_survivor_sources import _domain_digest
from .errors import ProtocolRefusal
from .jsonl import MAX_RECORD_BYTES
from .jsonl import VerifiedLedgerCursor

# Includes duplicate occurrences; unique-entry limits cannot bound input work.
MAX_INPUT_WORK = 1000000
MAX_INPUT_CANONICAL_BYTES = 1024 * 1024 * 1024


def _failure(detail):
    return IntegrityFailure("archive_survivor_pool_invalid", detail)


def encode_survivor_source_pool(plan):
    """Encode complete source axes and original cuts, without issuing proof.

    Native domain results and coverage remain separate planning data. This data
    transformation cannot authorize admission or retirement, even when a caller
    supplies dictionaries resembling a native source plan.
    """
    tenant = plan["tenant_id"]
    entries, identities, frame_keys = {}, {}, {}
    serialized_bytes, references = 0, 0
    input_work, input_bytes = 0, 0

    def charge(work=1, size=0):
        nonlocal input_work, input_bytes
        if input_work + work > MAX_INPUT_WORK or input_bytes + size > MAX_INPUT_CANONICAL_BYTES:
            raise source_pool._unsupported("survivor adapter exceeds its aggregate input work/byte bound")
        input_work += work
        input_bytes += size

    def preflight(value, limit):
        try:
            _domain_digest("adapter_input", value, _max_bytes=limit,
                _max_nodes=MAX_INPUT_WORK - input_work,
                _on_measure=lambda size, nodes: charge(nodes, size + 1))
        except ProtocolRefusal as exc:
            if exc.code == "archive_survivor_sources_unsupported":
                raise source_pool._unsupported("survivor adapter input exceeds bounded work/bytes") from exc
            raise

    def add(tag, **fields):
        nonlocal serialized_bytes, references
        charge()
        # Reserve room for fixed schema/key fields before content addressing.
        preflight(dict(tag=tag, tenant_id=tenant, **fields), source_pool.MAX_CHUNK_BYTES - 128)
        entry = source_pool.keyed_entry(tag, tenant, **fields)
        if entry["key"] in entries:
            return entry["key"]
        raw = encode_frame(entry)
        extra_refs = (sum(fields[name] is not None for name in ("owner", "anchor", "predecessor"))
                      if tag == "epoch" else 1 + len(fields["frames"]) if tag == "axis"
                      else 4 if tag == "cut" else 0)
        if (len(entries) >= source_pool.MAX_ENTRIES or len(raw) > source_pool.MAX_CHUNK_BYTES
                or serialized_bytes + len(raw) > source_pool.MAX_CHUNKS * source_pool.MAX_CHUNK_BYTES
                or references + extra_refs > source_pool.MAX_REFERENCES):
            raise source_pool._unsupported("survivor adapter exceeds the bounded source-pool collection")
        serialized_bytes += len(raw)
        references += extra_refs
        entries[entry["key"]] = entry
        return entry["key"]

    def frame(row):
        charge()
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(row.get("kind"), str):
            raise _failure("adapter frame is not an identified native record")
        preflight(row, MAX_RECORD_BYTES - 1)
        raw = encode_frame(row)
        identity = row["id"]
        if identity in identities and identities[identity] != raw:
            raise _failure("one native ID changed between collected frames and source axes")
        identities[identity] = raw
        key = add("frame", record=row)
        frame_keys[identity] = key
        return key

    for row in plan["frames"]:
        frame(row)
    projection = plan["sources"]["projection"]
    epoch_keys, anchors, previous = {}, {}, None
    for header in reversed(projection["epochs"]):
        charge()
        owner = frame(header["roll_receipt"]) if header["roll_receipt"] is not None else None
        anchor = frame_keys[header["anchor"]] if header["anchor"] is not None else None
        key = add("epoch", epoch_id=header["epoch_id"], owner=owner,
                  anchor=anchor, predecessor=previous)
        if header["epoch_id"] in epoch_keys or header["anchor"] in anchors:
            raise _failure("native source plan has duplicate epoch identities or anchors")
        epoch_keys[header["epoch_id"]] = key
        anchors[header["anchor"]] = key
        previous = key
    domains = {"events": WakeHoldController._EVENT_DOMAIN,
               "deliveries": WakeHoldController._DELIVERY_DOMAIN,
               "acks": WakeHoldController._ACK_DOMAIN}
    axes, axis_proofs = {}, {}

    def add_axis(epoch, plane, recipient, worker, rows, native_prefixes):
        charge()
        if type(rows) not in (list, tuple) or type(native_prefixes) not in (list, tuple):
            raise _failure("complete adapter source axes must be finite native arrays")
        if len(rows) > source_pool.MAX_AXIS_RECORDS:
            raise source_pool._unsupported("survivor adapter axis exceeds its native record support bound")
        if len(native_prefixes) != len(rows) + 1:
            raise _failure("complete adapter source axis has a non-exact prefix array")
        if len(rows) > MAX_INPUT_WORK - input_work:
            raise source_pool._unsupported("survivor adapter axis exceeds remaining input work")
        coordinate = (epoch, plane, recipient, worker)
        keys = [frame(row) for row in rows]
        key = add("axis", epoch=epoch, plane=plane, recipient=recipient,
                  worker_session_id=worker, frames=keys, record_count=len(keys),
                  digest_domain=domains[plane], digest=native_prefixes[-1])
        if coordinate in axes and axes[coordinate] != key:
            raise _failure("two native domains disagree on a complete source axis")
        axes[coordinate] = key
        if coordinate not in axis_proofs:
            charge(len(keys) + 1)
            digest = VerifiedLedgerCursor._initial_digest(domains[plane])
            prefixes = [digest.hexdigest()]
            for reference in keys:
                raw = identities[entries[reference]["record"]["id"]]
                charge(size=len(raw))
                digest.update(raw)
                prefixes.append(digest.hexdigest())
            axis_proofs[coordinate] = tuple(prefixes)
        charge(len(native_prefixes))
        if any(left != right for left, right in zip(native_prefixes, axis_proofs[coordinate])):
            raise _failure("complete adapter source prefix differs from its original axis frames")

    for header in projection["epochs"]:
        add_axis(epoch_keys[header["epoch_id"]], "events", None, None,
                 header["event_source"][0], header["event_source"][1])

    def context(source):
        charge()
        if type(source) not in (list, tuple) or len(source) != 6 or any(
                type(axis) not in (list, tuple) for axis in source):
            raise _failure("adapter source must have six finite native arrays")
        rows = source[0]
        if rows and (not isinstance(rows[0], dict)
                or not isinstance(rows[0].get("kind"), str)
                or not isinstance(rows[0].get("id"), str)):
            raise _failure("adapter source begins with an unidentified native record")
        anchor = rows[0]["id"] if rows and rows[0]["kind"] == "bus_epoch_roll_receipt" else None
        if anchor not in anchors:
            raise _failure("native source cut is outside the complete epoch graph")
        return anchors[anchor]

    def full_source(source, recipient, worker):
        epoch = context(source)
        for plane, axis in (("events", 0), ("deliveries", 2), ("acks", 4)):
            add_axis(epoch, plane, None if plane == "events" else recipient,
                     None if plane == "events" else worker, source[axis], source[axis + 1])

    def cut(source, recipient, worker, counts=None):
        charge()
        epoch = context(source)
        counts = [len(source[axis]) for axis in (0, 2, 4)] if counts is None else counts
        bound = {}
        for ordinal, plane in enumerate(("events", "deliveries", "acks")):
            coordinate = (epoch, plane, None if plane == "events" else recipient,
                          None if plane == "events" else worker)
            if coordinate not in axes:
                raise _failure("native cut lacks its complete contextual source axis")
            count = counts[ordinal]
            supplied = source[ordinal * 2 + 1]
            rows = source[ordinal * 2]
            expected_refs = entries[axes[coordinate]]["frames"]
            expected_prefixes = axis_proofs[coordinate]
            if len(rows) > len(expected_refs) or len(supplied) != len(rows) + 1:
                raise _failure("original adapter cut is not an exact complete-axis prefix")
            charge(len(rows) + len(supplied))
            for index, row in enumerate(rows):
                if frame(row) != expected_refs[index]:
                    raise _failure("original adapter cut row differs from its complete-axis position")
            if any(left != right for left, right in zip(supplied, expected_prefixes)):
                raise _failure("original adapter cut prefix differs from its complete-axis proof")
            if type(count) is not int or not 0 <= count < len(supplied):
                raise _failure("native cut count is outside its supplied prefix proof")
            bound[plane] = {"axis": axes[coordinate], "count": count, "digest": supplied[count]}
        return add("cut", epoch=epoch, recipient=recipient, worker_session_id=worker, axes=bound)

    for source_cut in projection["cuts"]:
        charge()
        source, recipient, worker = source_cut["source"], source_cut["recipient"], source_cut["worker_session_id"]
        full_source(source, recipient, worker)
        cut(source, recipient, worker)
    for hold in projection["holds"]:
        charge()
        receipt = hold["receipt"]
        cut(hold["before"], receipt["recipient"], receipt["worker_session_id"])
    for entry in plan["sources"]["presentation"]["entries"]:
        charge()
        for group in entry["groups"]:
            charge()
            prepared = group["records"][0]
            full_source(group["source"], prepared["node_id"], None)
            cut(group["source"], prepared["node_id"], None, group["counts"])
    for source in plan["sources"]["consumption"]["source_planes"]:
        charge()
        native = [source[field] for field in (
            "events", "event_prefix_digests", "deliveries", "delivery_prefix_digests",
            "acknowledgments", "acknowledgment_prefix_digests")]
        full_source(native, source["recipient"], source["worker_session_id"])
        cut(native, source["recipient"], source["worker_session_id"])
    return source_pool.encode_pool(entries.values())
