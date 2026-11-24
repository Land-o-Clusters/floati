"""Pure terminal rendering for ``floati doctor`` artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

from .copy import register


TUI_DOCTOR_COPY = {
    "tui.doctor.recent_sessions": register(
        "tui.doctor.recent_sessions", "recent sessions, 1 hour", "Doctor recent session evidence window",
    ),
    "tui.doctor.native_woke": register(
        "tui.doctor.native_woke", "Native woke receipts", "Doctor native wake evidence label",
    ),
    "tui.doctor.cursor_publications": register(
        "tui.doctor.cursor_publications", "Cursor local publications", "Doctor local publication evidence label",
    ),
    "tui.doctor.none_recorded": register(
        "tui.doctor.none_recorded", "none recorded in this window", "Doctor empty recorded session window",
    ),
    "tui.doctor.cursor_coverage": register(
        "tui.doctor.cursor_coverage", "Cursor coverage", "Doctor journal coverage label",
    ),
    "tui.doctor.legacy_rows": register(
        "tui.doctor.legacy_rows", "legacy rows are unproven", "Doctor legacy journal coverage limit",
    ),
    "tui.doctor.invalid_rows": register(
        "tui.doctor.invalid_rows", "invalid rows", "Doctor incomplete journal coverage count",
    ),
    "tui.doctor.calm": register(
        "tui.doctor.calm",
        "Nothing to fix. Every check passed and the receipts agree.",
        "Doctor TTY fully-green result",
    ),
    "tui.doctor.receipt_absent": register(
        "tui.doctor.receipt_absent",
        "no receipt id: the doctor finding is the record",
        "Doctor TTY RED evidence coordinate",
    ),
    "tui.doctor.remedy_absent_prefix": register(
        "tui.doctor.remedy_absent_prefix",
        "no remedy recorded: ",
        "Doctor TTY RED remedy absence",
    ),
}

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2, "ok": 3}
_SEVERITY_GLYPH = {"error": "x", "warning": "!", "info": "·", "ok": "✓"}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _receipt_id(value: object) -> str | None:
    """Return one deterministic explicit receipt id from nested finding evidence."""

    if isinstance(value, Mapping):
        for key in sorted(value):
            candidate = value[key]
            if str(key).endswith("_receipt_id") and isinstance(candidate, str) and candidate:
                return candidate
        for key in sorted(value):
            candidate = _receipt_id(value[key])
            if candidate is not None:
                return candidate
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            candidate = _receipt_id(item)
            if candidate is not None:
                return candidate
    return None


def _finding_coordinate(
    artifact: Mapping[str, object], finding: Mapping[str, object]
) -> str:
    explicit = _receipt_id(finding)
    if explicit is not None:
        return explicit
    artifact_coordinate = next(
        (
            _text(artifact.get(key))
            for key in ("artifact_id", "id", "path", "root", "source")
            if _text(artifact.get(key))
        ),
        "",
    )
    code = _text(finding.get("code"))
    if artifact_coordinate and code:
        return f"doctor {artifact_coordinate}#{code}"
    return TUI_DOCTOR_COPY["tui.doctor.receipt_absent"]


def _finding_line(
    artifact: Mapping[str, object], finding: Mapping[str, object]
) -> str:
    severity = _text(finding.get("severity"))
    parts = [
        f"{_SEVERITY_GLYPH.get(severity, '·')} {_text(finding.get('code'))}",
        _text(finding.get("subject")),
        _text(finding.get("detail")),
    ]
    if severity == "error" or finding.get("code") == "seat_attention_unknown":
        remediation = _text(finding.get("remediation"))
        if not remediation:
            remediation = (
                TUI_DOCTOR_COPY["tui.doctor.remedy_absent_prefix"]
                + _text(finding.get("code"))
            )
        parts.extend((_finding_coordinate(artifact, finding), remediation))
    return " // ".join(parts)


def _recent_session_lines(findings: Sequence[Mapping[str, object]]) -> str:
    """Render only typed evidence fields; quote all untrusted string values."""
    blocks = []
    for finding in findings:
        if finding.get("code") != "wake_health":
            continue
        health = finding.get("wake_health")
        if not isinstance(health, Mapping):
            continue
        recent = health.get("recent_sessions")
        node = health.get("node_id")
        if not isinstance(recent, Mapping) or not isinstance(node, str):
            continue
        lines = [json.dumps(node, ensure_ascii=True) + " — " + TUI_DOCTOR_COPY["tui.doctor.recent_sessions"]]
        for key, label in (("native_woke", "native_woke"), ("cursor_local_publication", "cursor_publications")):
            plane = recent.get(key)
            if not isinstance(plane, Mapping):
                continue
            prefix = "  " + TUI_DOCTOR_COPY["tui.doctor." + label] + ": "
            raw_sessions = plane.get("sessions")
            sessions = raw_sessions if isinstance(raw_sessions, list) else []
            source = plane.get("source_path")
            rendered = []
            for row in sessions:
                if not isinstance(row, Mapping) or not isinstance(source, str):
                    continue
                session, stamp, reference = row.get("session_id"), row.get("last_at"), row.get("last_record")
                if not isinstance(session, str) or not isinstance(stamp, str) or not isinstance(reference, Mapping):
                    continue
                line = reference.get("line")
                if not isinstance(line, int) or isinstance(line, bool) or line < 1:
                    continue
                text = " — ".join(json.dumps(value, ensure_ascii=True)
                    for value in (session, stamp, source + ":" + str(line)))
                rendered.append((session, prefix + text))
            lines.extend(text for _session, text in sorted(rendered))
            if not sessions:
                lines.append(prefix + TUI_DOCTOR_COPY["tui.doctor.none_recorded"])
            if key == "cursor_local_publication":
                coverage = plane.get("coverage")
                if isinstance(coverage, str):
                    detail = "  " + TUI_DOCTOR_COPY["tui.doctor.cursor_coverage"] + ": " + json.dumps(coverage, ensure_ascii=True)
                    for count_key, copy_key in (("legacy_row_count", "legacy_rows"), ("invalid_row_count", "invalid_rows")):
                        count = plane.get(count_key)
                        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                            detail += "; " + str(count) + " " + TUI_DOCTOR_COPY["tui.doctor." + copy_key]
                    lines.append(detail)
        blocks.append((node, "\n".join(lines) + "\n"))
    return "".join(text for _node, text in sorted(blocks))


def render_doctor(artifact: Mapping[str, object]) -> str:
    """Render one doctor artifact with RED findings first and stable ties."""

    from .fleet_presence import presence_status_line
    mode = artifact.get("fleet_presence")
    presence = presence_status_line(mode) + "\n" if isinstance(mode, Mapping) else ""
    raw_findings = artifact.get("findings")
    findings = (
        [item for item in raw_findings if isinstance(item, Mapping)]
        if isinstance(raw_findings, Sequence) and not isinstance(raw_findings, (str, bytes))
        else []
    )
    if findings and all(_text(item.get("severity")) == "ok" for item in findings):
        return presence + TUI_DOCTOR_COPY["tui.doctor.calm"] + "\n" + _recent_session_lines(findings)
    ordered = sorted(
        enumerate(findings),
        key=lambda pair: (_SEVERITY_ORDER.get(_text(pair[1].get("severity")), 2), pair[0]),
    )
    return presence + "".join(_finding_line(artifact, finding) + "\n" for _, finding in ordered) + _recent_session_lines(findings)
