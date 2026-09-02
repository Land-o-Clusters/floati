"""Pure terminal rendering for ``floati doctor`` artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .copy import register


TUI_DOCTOR_COPY = {
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
    if severity == "error":
        remediation = _text(finding.get("remediation"))
        if not remediation:
            remediation = (
                TUI_DOCTOR_COPY["tui.doctor.remedy_absent_prefix"]
                + _text(finding.get("code"))
            )
        parts.extend((_finding_coordinate(artifact, finding), remediation))
    return " // ".join(parts)


def render_doctor(artifact: Mapping[str, object]) -> str:
    """Render one doctor artifact with RED findings first and stable ties."""

    raw_findings = artifact.get("findings")
    findings = (
        [item for item in raw_findings if isinstance(item, Mapping)]
        if isinstance(raw_findings, Sequence) and not isinstance(raw_findings, (str, bytes))
        else []
    )
    if findings and all(_text(item.get("severity")) == "ok" for item in findings):
        return TUI_DOCTOR_COPY["tui.doctor.calm"] + "\n"
    ordered = sorted(
        enumerate(findings),
        key=lambda pair: (_SEVERITY_ORDER.get(_text(pair[1].get("severity")), 2), pair[0]),
    )
    return "".join(_finding_line(artifact, finding) + "\n" for _, finding in ordered)
