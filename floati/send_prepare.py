"""Root-bound, resumable preparation of a native event prefix after an update."""
import hashlib
import hmac
import json
import os
import uuid
from contextlib import contextmanager

from . import jsonl
from .errors import IntegrityFailure, ProtocolRefusal
from .framing import decode_frames, encode_frame
from .send_pages import Pages, open_bank, read_range
from .send_prefix import CacheMiss, Prefix, _CACHE_ERRORS, _EMPTY_HEAD, _atomic, _bytes, _identity, _read

STEP_FRAMES = 4096
STEP_BYTES = 2 * 1024 * 1024
COMPANIONS = ("delivery_claim", "send_readiness_observation", "seat_execution")


def _progress(prefix, *, previous=None):
    identity = _identity(prefix.path) if previous is None else previous
    if identity and identity[4] > jsonl.MAX_LEDGER_BYTES:
        raise IntegrityFailure("ledger_too_large", f"events.jsonl exceeds {jsonl.MAX_LEDGER_BYTES} bytes")
    try:
        doc = json.loads(_read(prefix.home / "preparation.json", 16384))
        cut = doc["payload"]
        seal = hmac.digest(prefix.secret, _bytes(cut), "sha256").hex()
        if (hmac.compare_digest(doc["seal"], seal) and cut["binding"] == prefix.binding
                and cut["identity"] == identity):
            return cut
    except _CACHE_ERRORS:
        pass
    return {"binding": prefix.binding, "identity": identity, "generation": uuid.uuid4().hex,
            "offset": 0, "count": 0, "head": _EMPTY_HEAD, "end": 0, "directory": None,
            "pending_only": 0, "anchor": None, "carry": None, "seeds": None,
            "envelopes": 0}


def _state(cut, status):
    state = {"state": status, "frames": cut["count"], "validated_bytes": cut["offset"],
             "source_bytes": cut["identity"][4] if cut["identity"] else 0}
    if "restart_reason" in cut:
        state["restart_reason"] = cut["restart_reason"]
    return state


def _save(prefix, cut):
    seal = hmac.digest(prefix.secret, _bytes(cut), "sha256").hex()
    _atomic(prefix.home / "preparation.json", _bytes({"payload": cut, "seal": seal}))


def advance_source(prefix, identity):
    """A certified append preserves all previously validated source offsets."""
    cut = _progress(prefix, previous=prefix.payload["identity"])
    if cut["end"]:
        cut["identity"] = list(identity)
        _save(prefix, cut)


def _frame(source, pointer):
    offset, size, _ordinal, digest = pointer
    return read_range(source, (offset, size, digest), jsonl.MAX_RECORD_BYTES + 1)


def _row(source, pointer):
    if pointer is None:
        return None
    frame = _frame(source, pointer)
    return decode_frames(frame + b"\n" if frame.endswith(b"\r") else frame)[0]


def _slice(source, remaining):
    # splitlines is the native decoder's physical grammar (LF, CRLF, or
    # internal CR). Look ahead by one bounded frame to finish the last row.
    raw = source.read(min(remaining, STEP_BYTES + jsonl.MAX_RECORD_BYTES + 2))
    stop = 0
    for ordinal, frame in enumerate(raw.splitlines(keepends=True), 1):
        stop += len(frame)
        if stop >= STEP_BYTES or ordinal == STEP_FRAMES:
            break
    return raw[:stop]


def _order(pages, source, rows, start):
    from .events import _validate_event_order
    messages, retracted, observed, questions, resolved = {}, set(), set(), {}, set()
    launchers = set()
    references = {row.get(field) for row in rows for field in
                  ("reply_to", "retracted_message_id", "envelope_id", "note_ref", "message_id")
                  if row.get(field)}
    for reference in references:
        prior = _row(source, pages.get("id", reference))
        if prior is None:
            continue
        if prior["kind"] == "message_envelope":
            messages[reference] = prior
            if pages.get("retraction", reference) is not None:
                retracted.add(reference)
            if pages.get("send_readiness_observation", reference) is not None:
                observed.add(reference)
            if pages.get("launcher", reference) is not None:
                launchers.add(reference)
        elif prior["kind"] == "ask_question":
            questions[reference] = prior
            if pages.get("resolved", reference) is not None:
                resolved.add(reference)
    _validate_event_order(rows, start, messages, retracted, observed, questions, resolved, launchers)


def _verification(cut, row, pointer, pages, source):
    from . import epoch_teardown as et
    from .sha256_midstate import SHA256
    kind = row["kind"]
    if kind == "bus_epoch_roll_receipt":
        if cut["count"]:
            raise IntegrityFailure("bus_epoch_roll_receipt_position_invalid",
                                   "one bus epoch roll receipt is permitted only as physical record one")
        cut["anchor"] = row["id"]
        if "teardown_carry" in row.get("automatic", {}):
            digest = SHA256().update(et._SEED_DOMAIN + b"[")
            cut["seeds"] = {"count": 0, "last": "", "state": digest.state,
                            "length": digest.length, "tail": digest.tail.hex()}
    elif kind == "message_envelope" and cut["anchor"]:
        cut["envelopes"] += 1
        head = et._carry_head([{"sha256": hashlib.sha256(encode_frame(row)).hexdigest()}],
                              cut.get("carry_head", et.EMPTY_CARRY_HEAD))
        cut["carry_head"] = head
        pages.put("carry-head", str(cut["envelopes"]), (*pointer[:3], bytes.fromhex(head)))
    elif kind in (et.CARRY_KIND, et.KIND):
        anchor = _row(source, pages.get("id", cut["anchor"])) if cut["anchor"] else None
        if kind == et.CARRY_KIND:
            if cut["carry"] is not None or anchor is None:
                raise IntegrityFailure("epoch_carry_verification_invalid", "carry verification needs one roll anchor")
            if row["roll_receipt_id"] != anchor["id"] or row["roll_receipt_sha256"] != hashlib.sha256(encode_frame(anchor)).hexdigest():
                raise IntegrityFailure("epoch_carry_verification_invalid", "carry verification differs from the active roll")
            cut["carry"] = row["id"]
            return
        seed = et._seed(row)
        saved = cut["seeds"]
        if (saved is None or row["roll_receipt_id"] != anchor["id"]
                or row["roll_receipt_sha256"] != hashlib.sha256(encode_frame(anchor)).hexdigest()
                or row["outgoing_epoch_id"] != anchor["epoch_id"] or row["teardown_receipt_id"] <= saved["last"]):
            raise et._failure("teardown carry differs from the active roll or seed order")
        et._validate_seed(seed, bound=True)
        digest = SHA256(state=saved["state"], length=saved["length"], tail=bytes.fromhex(saved["tail"]))
        digest.update((b"," if saved["count"] else b"") + et._canonical(seed))
        saved.update(count=saved["count"] + 1, last=row["teardown_receipt_id"],
                     state=digest.state, length=digest.length, tail=digest.tail.hex())


def _finish_verification(cut, pages, source):
    from . import epoch_teardown as et
    from .sha256_midstate import SHA256
    if cut["carry"]:
        row = _row(source, pages.get("id", cut["carry"]))
        if row["schema_version"] == 1:
            count = row["envelope_count"]
            ref = pages.get("carry-head", str(count)) if count else None
            digest = ref[3].hex() if ref else et.EMPTY_CARRY_HEAD
            if count > cut["envelopes"] or digest != row["digest"]:
                raise IntegrityFailure("epoch_carry_verification_invalid", "carried envelope frames differ from the roll inventory")
    saved = cut["seeds"]
    if saved is not None:
        anchor = _row(source, pages.get("id", cut["anchor"]))
        digest = SHA256(state=saved["state"], length=saved["length"], tail=bytes.fromhex(saved["tail"]))
        if {"count": saved["count"], "digest": digest.update(b"]").hexdigest()} != anchor["automatic"]["teardown_carry"]:
            raise et._failure("teardown carry complete seed set differs from the roll commitment")


def _index(prefix, cut, pages, source, data, *, final):
    unknown = {}
    # The full source must end in LF. At an interior CR boundary only, let
    # the native decoder see the equivalent complete CRLF slice. All offsets
    # and commitments below still cover the captured source bytes verbatim.
    decoding = data + b"\n" if not final and data.endswith(b"\r") else data
    checked = jsonl._decode_path_records(prefix.path, prefix.root.tenant_id, prefix.kinds, decoding, unrecognized=unknown)
    _order(pages, source, checked, cut["count"])
    physical = decode_frames(decoding) if unknown else checked
    for frame, row in zip(data.splitlines(keepends=True), physical):
        if cut["count"] >= jsonl.MAX_LEDGER_RECORDS:
            raise IntegrityFailure("ledger_record_limit", f"events.jsonl exceeds {jsonl.MAX_LEDGER_RECORDS} records")
        pointer = cut["offset"], len(frame), cut["count"], hashlib.sha256(frame).digest()
        if not pages.put("id", row["id"], pointer):
            raise IntegrityFailure("duplicate_record_id", "events.jsonl repeats a physical record id")
        kind = row["kind"]
        _verification(cut, row, pointer, pages, source)
        if cut["count"] == 0:
            pages.put("first", "physical", pointer)
        if kind == "message_envelope":
            pages.put("send", row["idempotency_key"], pointer)
        elif kind in COMPANIONS:
            parent = row["note_ref"] if kind == "delivery_claim" else row["envelope_id"]
            if parent is not None:
                pages.put(kind, parent, pointer)
        elif kind == "message_retracted":
            pages.put("retraction", row["retracted_message_id"], pointer)
        elif kind == "native_dispatch_launcher_record":
            pages.put("launcher", row["message_id"], pointer)
        elif kind in {"ask_answer", "ask_decline"}:
            pages.put("resolved", row["reply_to"], pointer)
        if cut["pending_only"] is not None:
            if kind not in {"message_envelope", "delivery_claim", "send_readiness_observation"} or frame != encode_frame(row):
                cut["pending_only"] = None
            elif kind == "message_envelope":
                cut["pending_only"] += 1
        cut["head"] = hashlib.sha256(bytes.fromhex(cut["head"]) + frame).hexdigest()
        cut["offset"] += len(frame)
        cut["count"] += 1


def _publish(prefix, cut):
    if _identity(prefix.path) != cut["identity"]:
        raise IntegrityFailure("ledger_identity_changed_during_read", "events.jsonl changed during prefix validation")
    payload = {key: cut[key] for key in ("binding", "identity", "generation", "offset", "count", "head", "pending_only")}
    payload.update(trie=None, mail_base_count=cut["count"], pages=cut["directory"])
    prefix.payload = payload
    prefix.publish()
    (prefix.home / "preparation.json").unlink(missing_ok=True)


def finish_interrupted_publish(prefix, cut):
    # The completed bank is renamed before publishing its checkpoint. A send
    # arriving at this cut can complete publication before reusing the bank.
    size = cut["identity"][4] if cut["identity"] else 0
    if not cut["end"] or cut["offset"] != size or (prefix.home / "preparing.bin").exists():
        return False
    with open_bank(prefix.bank) as stream:
        Pages(stream, cut["directory"])
    _publish(prefix, cut)
    return True


def step(prefix, cut):
    draft = prefix.home / "preparing.bin"
    if cut["end"]:
        try:
            if draft.exists():
                return _step(prefix, cut, draft)
            try:
                if finish_interrupted_publish(prefix, cut):
                    return "done"
            except FileNotFoundError:
                pass
        except CacheMiss:
            pass
        # The cursor alone cannot testify to missing or damaged pages.
        # Discard this derived generation and validate the native source again.
        (prefix.home / "preparation.json").unlink(missing_ok=True)
        cut.clear()
        cut.update(_progress(prefix))
        cut["restart_reason"] = "prepared_pages_unavailable"
    draft.unlink(missing_ok=True)
    return _step(prefix, cut, draft)


def _step(prefix, cut, draft):
    size = cut["identity"][4] if cut["identity"] else 0
    with open_bank(draft, create=not cut["end"], write=True) as bank:
        pages = Pages(bank, cut["directory"])
        if size:
            with prefix.path.open("rb") as source:
                source.seek(cut["offset"])
                data = _slice(source, size - cut["offset"])
                if not data and cut["offset"] < size:
                    raise IntegrityFailure("ledger_identity_changed_during_read", "events.jsonl changed during prefix validation")
                _index(prefix, cut, pages, source, data, final=cut["offset"] + len(data) == size)
                if cut["offset"] == size:
                    _finish_verification(cut, pages, source)
        if _identity(prefix.path) != cut["identity"]:
            raise IntegrityFailure("ledger_identity_changed_during_read", "events.jsonl changed during prefix validation")
        cut["directory"], cut["end"] = pages.commit(cut["end"])
    _save(prefix, cut)
    if cut["offset"] == size:
        os.replace(draft, prefix.bank)
        _publish(prefix, cut)
        return "done"
    return "in_progress"


@contextmanager
def _scope(root, path, status):
    if status:
        from .bus_epoch import _epoch_transition_pending
        from .epoch_rotation import _observation_scope
        with _observation_scope(root):
            if _epoch_transition_pending(root):
                raise ProtocolRefusal("epoch_roll_recovery_required", "an incomplete epoch roll must be reconciled before preparation")
            yield
        return
    lock, coordinate = jsonl._lock_beside(path, "events.jsonl")
    with jsonl._epoch_writer_guard(root, "events.jsonl", path=path), jsonl._locked_path(
            lock, exclusive=True, relative=coordinate):
        yield


def prepare(root, *, status=False):
    from .events import EVENT_KINDS
    path = root.resolve_relative("events.jsonl")
    with _scope(root, path, status):
        try:
            prefix = Prefix(root, EVENT_KINDS, read_only=status)
            try:
                prefix.load()
                if "filter" not in prefix.payload:
                    if "pages" in prefix.payload:
                        with open_bank(prefix.bank) as stream:
                            Pages(stream, prefix.payload["pages"])
                    return _state(prefix.payload, "done")
            except _CACHE_ERRORS:
                pass
            cut = _progress(prefix)
            if status:
                return _state(cut, "in_progress" if cut["end"] else "pending")
            return _state(cut, step(prefix, cut))
        except (CacheMiss, OSError, ValueError, KeyError, TypeError) as failure:
            if status and not (root.tenant_home / ".send-prefix").exists():
                return {"state": "pending", "frames": 0, "validated_bytes": 0,
                        "source_bytes": path.stat().st_size if path.exists() else 0}
            raise ProtocolRefusal("send_preparation_unavailable", "The send index could not be prepared.",
                                  "Check the root and its private index storage, then run floati update prepare --root ROOT again.") from failure


def lookup(prefix, domain, value):
    # The prepared bank certifies send references. Aggregate mail projections
    # require their own full tree and otherwise use their native reader.
    if domain == "mail-valid-v1":
        return False
    if domain not in {"id", "send", "retraction", "mail-ordinal-v1", "mail-first-v1"}:
        return None
    with open_bank(prefix.bank) as bank:
        pages = Pages(bank, prefix.payload["pages"])
        key = {"mail-ordinal-v1": "id", "mail-first-v1": "first"}.get(domain, domain)
        pointer = pages.get(key, value)
        if pointer is None:
            return None
        if domain == "mail-ordinal-v1":
            return pointer[2]
        with prefix.path.open("rb") as source:
            row = _row(source, pointer)
            if domain != "send":
                return row
            from .records import validate_record
            group = [row]
            for kind in COMPANIONS:
                companion = _row(source, pages.get(kind, row["id"]))
                if companion is not None:
                    group.append(companion)
            return [validate_record(member, prefix.root.tenant_id, prefix.kinds, integrity=True) for member in group]
