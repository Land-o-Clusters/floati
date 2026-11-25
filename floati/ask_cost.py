"""Read the opted-in local Puddle export for an exact ask/answer pair."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path


_EXPORT_VARIABLE = "FLOATI_PUDDLE_ASK_COST_EXPORT"
_MAX_EXPORT_BYTES = 1024 * 1024
_PAIR_FIELDS = ("tenant_id", "question_id", "answer_id")


def project_ask_cost(tenant_id: str, question_id: str, asker: str, answer_id: str | None) -> dict:
    if answer_id is None:
        return {"status": "unmeasured", "reason": "ask_answer_absent", "attributed_to": asker}
    measurement, reason = _read_measurement((tenant_id, question_id, answer_id))
    if measurement is None:
        return {"status": "unmeasured", "reason": reason, "attributed_to": asker,
                "outcome": "UNKNOWN"}
    return {
        "status": "measured",
        "attributed_to": asker,
        **{key: measurement[key] for key in ("total_tokens", "measurement_id", "observed_at")},
    }


def _read_measurement(pair: tuple[str, str, str]) -> tuple[dict | None, str | None]:
    configured = os.environ.get(_EXPORT_VARIABLE)
    if not configured:
        return None, "puddle_measured_cost_seam_unavailable"
    path = Path(configured)
    try:
        if not path.is_absolute() or path.name != "ask-cost-v1.json" or path.resolve(strict=True) != path:
            return None, "puddle_ask_cost_invalid"
        with open(path, "rb", opener=_open_export) as stream:
            raw = stream.read(_MAX_EXPORT_BYTES + 1)
    except FileNotFoundError:
        return None, "puddle_measured_cost_seam_unavailable"
    except (OSError, ValueError, RuntimeError):
        return None, "puddle_ask_cost_unreadable"
    if len(raw) > _MAX_EXPORT_BYTES:
        return None, "puddle_ask_cost_invalid"
    try:
        export = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        return None, "puddle_ask_cost_invalid"
    if not isinstance(export, dict) or type(export.get("schema_version")) is not int:
        return None, "puddle_ask_cost_invalid"
    if export["schema_version"] != 1:
        return None, "puddle_ask_cost_version_unsupported"
    rows = export.get("records")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or any(not isinstance(row.get(key), str) for key in _PAIR_FIELDS)
        for row in rows
    ):
        return None, "puddle_ask_cost_invalid"
    matches = [row for row in rows if tuple(row[key] for key in _PAIR_FIELDS) == pair]
    if not matches:
        return None, "puddle_ask_cost_not_found"
    if len(matches) != 1:
        return None, "puddle_ask_cost_ambiguous"
    measurement = matches[0]
    if measurement.get("basis") != "MEASURED":
        return None, "puddle_ask_cost_not_measured"
    if not _valid_measurement(measurement):
        return None, "puddle_ask_cost_invalid"
    return measurement, None


def _open_export(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise OSError("cost export must be a private regular file owned by the reader")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _unique_object(entries):
    result = {}
    for key, value in entries:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _valid_measurement(row):
    tokens = row.get("total_tokens")
    identity = row.get("measurement_id")
    stamp = row.get("observed_at")
    if type(tokens) is not int or not 0 <= tokens <= 2**63 - 1:
        return False
    if not isinstance(identity, str) or not identity.isprintable() or not 1 <= len(identity) <= 128:
        return False
    if not isinstance(stamp, str) or not stamp.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(stamp[:-1] + "+00:00")
    except ValueError:
        return False
    return True
