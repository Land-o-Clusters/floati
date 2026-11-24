"""Cold presentation replay DATA; no runtime publication or hold admission."""
from copy import deepcopy
from pathlib import PurePosixPath

from .archive_projection_replay import PoolEpochView, _projection_replayer
from .archive_survivor_sources import _domain_digest
from .cursor_presentations import (KINDS, _HistoricalCuts, _failure,
    _presentation_witness_data, _presentation_groups, _presentation_cuts,
    _validate_presentation_groups)
from .framing import encode_frame
from .jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES
from .records import validate_record
from .root import validate_identifier
from .errors import IntegrityFailure
from .jsonl import _decode_bounded_frames


def replay_presentation_history(source_chunks, *, tenant_id, node_id, witness,
                                physical_bytes, consents, claims):
    """Replay physical history and virtual prepares against original pool cuts.

    No filesystem adapter, current claim lookup, output, or live hold admission
    occurs here. Native issuance must establish the input testimony separately.
    """
    validate_identifier(node_id, "node")
    if type(physical_bytes) is not bytes or len(physical_bytes) > MAX_LEDGER_BYTES:
        raise _failure("presentation physical history exceeds native framing bounds")
    if type(consents) is not list or type(claims) is not list:
        raise _failure("presentation consent and claim testimony must be finite native arrays")
    _domain_digest("presentation", [witness, consents, claims])
    view = PoolEpochView(source_chunks, tenant_id)
    frames = {row["record"]["id"]: row["record"] for key in view._pool.by_tag("frame")
              for row in (view._pool.entry(key),)}
    for supplied, kind in ((consents, "codex_wait_consent_receipt"),
                           (claims, "codex_wait_session_receipt")):
        seen = set()
        for row in supplied:
            validate_record(row, tenant_id, {kind}, integrity=True)
            if row["id"] in seen or row["id"] not in frames or encode_frame(row) != encode_frame(frames[row["id"]]):
                raise _failure("presentation consent or claim differs from its original source frame")
            seen.add(row["id"])
    witness, effective = _presentation_witness_data(witness, physical_bytes, tenant_id, node_id)
    if len(effective) > MAX_LEDGER_BYTES:
        raise IntegrityFailure("ledger_too_large", "effective presentation history exceeds native byte bounds")
    rows = _decode_bounded_frames(PurePosixPath("cursor-presentations.jsonl"), effective)
    if len(rows) > MAX_LEDGER_RECORDS:
        raise _failure("presentation history exceeds native record bounds")
    seen = set()
    for row in rows:
        validate_record(row, tenant_id, KINDS, integrity=True)
        if len(encode_frame(row)) > MAX_RECORD_BYTES or row["id"] in seen:
            raise _failure("presentation history violates native framing or unique identity")
        seen.add(row["id"])
    # Even self-consistent caller edits cannot replace retained native IDs.
    for row in rows + ([witness["reserved_record"]] if witness["reserved_record"] is not None else []):
        if row["id"] not in frames or encode_frame(row) != encode_frame(frames[row["id"]]):
            raise _failure("presentation frame differs from its original source testimony")
    groups = _presentation_groups(rows, node_id)
    epochs, keys = {}, {}
    for header in view._epochs:
        anchor = view._frames[header["anchor"]]["id"] if header["anchor"] is not None else witness["genesis_id"]
        if anchor in epochs:
            raise _failure("presentation epoch has conflicting original anchors")
        epochs[anchor] = view._source(header["key"], node_id, None)
        keys[anchor] = header["key"]
    cuts, limits = _presentation_cuts(groups, epochs)
    project, _original, _origin, _holds = _projection_replayer(view, tenant_id)
    history = {anchor: _HistoricalCuts(epochs[anchor], node_id, tenant_id, counts,
        _replay=lambda source, anchor=anchor: project(source, keys[anchor], node_id, None))
        for anchor, counts in limits.items()}
    results = _validate_presentation_groups(groups, epochs, cuts, history, consents, claims, node_id)
    for result in results:
        prepared = result["records"][0]
        source = epochs[prepared["epoch_anchor"]]
        result["source_cut"] = view._cut_key_counts(keys[prepared["epoch_anchor"]], node_id, None,
            result["counts"], tuple(source[ordinal * 2 + 1][count]
                                    for ordinal, count in enumerate(result["counts"])))
    _domain_digest("presentation_results", results)
    return {"schema_version": 1, "domain": "presentation_replay",
            "tenant_id": tenant_id, "node_id": node_id,
            "records": deepcopy(rows), "groups": deepcopy(results),
            "uncertain": any(len(batch) == 1 for batch in groups.values())}
