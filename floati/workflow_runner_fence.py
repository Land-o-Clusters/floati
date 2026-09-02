"""Refuse a public projection whose workflows name anything but hosted Linux.

The public repository accepts fork pull requests. A runner named in one of its
workflows is therefore a runner a stranger can aim code at, and a self-hosted
label points that code at a machine somebody owns. Hosted macOS is the other
half: its minutes bill at ten times Linux, and an every-branch matrix over it
burned an enterprise's whole monthly pool in two days.

So the allowlist is three literals and nothing else, and the rule that matters
more than the allowlist is this: **A RUNNER WHOSE VALUE IS NOT IN THE FILE IS
NOT A RUNNER THIS FENCE CAN CLEAR.** ``runs-on: ${{ fromJSON(vars.X) }}`` reads
as harmless and resolves in repository settings, where a fence that reads files
cannot follow it. Every shape the resolver cannot evaluate is a refusal, never
a pass — including an empty workflows directory, because an empty population
satisfies every must-be-zero.

Resolver shapes after the runner-allowlist gate of a sibling estate, with
credit. Their allowlist admits self-hosted because their gate routes work to a
box they own; ours refuses it, so the same shapes reach opposite verdicts.

Standard library only, and a deliberately small YAML reader: the product ships
zero dependencies, and a workflow file this reader cannot parse is a refusal
rather than a silent skip.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


#: The only runner labels a public workflow may name.
ALLOWED_RUNNER_LABELS = ("ubuntu-22.04", "ubuntu-24.04", "ubuntu-latest")

#: The typed code the export projection refuses with.
REFUSAL_CODE = "public_workflow_runner_unhosted"

WORKFLOW_DIRECTORY = ".github/workflows"
WORKFLOW_SUFFIXES = (".yml", ".yaml")

#: Quoted tokens an expression compares against that are events, not labels.
#: The list is exact on purpose: any other quoted token is either a label this
#: fence must check or something it cannot read, and both outrank a guess.
EVENT_NAME_TOKENS = frozenset(
    {
        "merge_group",
        "pull_request",
        "push",
        "schedule",
        "workflow_dispatch",
    }
)

#: Every reason a finding can carry. Pinned so a new one cannot appear unnamed.
REASONS = (
    "job_not_a_mapping",
    "runner_absent",
    "runner_expression_double_quoted_literal",
    "runner_external_reusable_workflow",
    "runner_hosted_macos",
    "runner_hosted_windows",
    "runner_label_unknown",
    "runner_self_hosted",
    "runner_unresolved",
    "workflow_jobs_invalid",
    "workflow_not_a_mapping",
    "workflow_path_type_invalid",
    "workflow_unparsed",
    "workflow_unreadable",
    "workflows_directory_empty",
)

_DETAILS = {
    "job_not_a_mapping": "job is not a mapping, so it has no readable runs-on",
    "runner_absent": "job names no runs-on and calls no reusable workflow",
    "runner_expression_double_quoted_literal": (
        "expression uses a double-quoted literal, which GitHub's expression "
        "grammar rejects and this resolver cannot read"
    ),
    "runner_external_reusable_workflow": (
        "job delegates its runner to a reusable workflow outside this tree, "
        "whose runs-on is not in the projection"
    ),
    "runner_hosted_macos": "hosted macOS is banned org-wide after the billing incident",
    "runner_hosted_windows": "hosted Windows is not an allowed runner image",
    "runner_label_unknown": (
        "label is not one of the known hosted Linux images "
        f"({', '.join(ALLOWED_RUNNER_LABELS)})"
    ),
    "runner_self_hosted": (
        "self-hosted runner in a repository that accepts fork pull requests"
    ),
    "runner_unresolved": (
        "runs-on does not resolve to labels inside this file, so the value "
        "lives somewhere this fence cannot read"
    ),
    "workflow_jobs_invalid": "workflow declares no readable jobs mapping",
    "workflow_not_a_mapping": "workflow document is not a mapping",
    "workflow_path_type_invalid": "workflow path is not a regular file",
    "workflow_unparsed": "workflow could not be parsed, so its runners are unknown",
    "workflow_unreadable": "workflow bytes are not readable UTF-8 text",
    "workflows_directory_empty": (
        f"projection carries no {WORKFLOW_DIRECTORY}/*.yml, and an empty "
        "population satisfies every must-be-zero"
    ),
}

_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_SINGLE_QUOTED = re.compile(r"'((?:[^']|'')*)'")
_MATRIX_REFERENCE = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}$")
_LABEL_SHAPED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BLOCK_SCALAR = re.compile(r"^[|>][+-]?$")


class WorkflowParseError(ValueError):
    """The reader could not turn a workflow file into a document."""


# --------------------------------------------------------------------------
# A small block-YAML reader. GitHub accepts JSON as YAML, and several of this
# repository's workflows are written as JSON, so JSON is tried first.
# --------------------------------------------------------------------------


def load_workflow(text: str) -> Any:
    """Return the workflow document, or raise WorkflowParseError."""

    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkflowParseError(f"json: {exc}") from exc
    return _BlockYamlReader(text).read()


def _strip_comment(content: str) -> str:
    """Drop a trailing YAML comment without touching one inside a scalar."""

    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(content):
        character = content[index]
        if quote is not None:
            out.append(character)
            if character == quote:
                if quote == "'" and content[index + 1 : index + 2] == "'":
                    out.append("'")
                    index += 2
                    continue
                quote = None
            elif quote == '"' and character == "\\":
                index += 1
                if index < len(content):
                    out.append(content[index])
                    index += 1
                continue
            index += 1
            continue
        if character in "'\"":
            quote = character
            out.append(character)
            index += 1
            continue
        if character == "#" and (not out or out[-1] in " \t"):
            break
        out.append(character)
        index += 1
    if quote is not None:
        raise WorkflowParseError("unterminated quoted scalar")
    return "".join(out).rstrip()


class _BlockYamlReader:
    """Enough block YAML to read a workflow, and a refusal for the rest."""

    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        for number, raw in enumerate(self.lines, start=1):
            if raw[: len(raw) - len(raw.lstrip())].count("\t"):
                raise WorkflowParseError(f"tab indentation on line {number}")

    # -- line helpers ------------------------------------------------------

    def indent_of(self, index: int) -> int:
        raw = self.lines[index]
        return len(raw) - len(raw.lstrip(" "))

    def skippable(self, index: int) -> bool:
        stripped = self.lines[index].strip()
        return not stripped or stripped.startswith("#")

    def next_content(self, index: int) -> int:
        while index < len(self.lines) and self.skippable(index):
            index += 1
        return index

    def content_of(self, index: int) -> str:
        return _strip_comment(self.lines[index][self.indent_of(index) :])

    # -- entry point -------------------------------------------------------

    def read(self) -> Any:
        index = self.next_content(0)
        if index >= len(self.lines):
            return None
        if self.content_of(index) == "---":
            index = self.next_content(index + 1)
            if index >= len(self.lines):
                return None
        value, index = self.collection(index, self.indent_of(index))
        index = self.next_content(index)
        if index < len(self.lines) and self.content_of(index) != "...":
            raise WorkflowParseError(f"trailing content on line {index + 1}")
        return value

    # -- collections -------------------------------------------------------

    def collection(self, index: int, indent: int) -> tuple[Any, int]:
        index = self.next_content(index)
        if index >= len(self.lines) or self.indent_of(index) < indent:
            raise WorkflowParseError("expected a block collection")
        content = self.content_of(index)
        if content == "-" or content.startswith("- "):
            return self.sequence(index, self.indent_of(index))
        return self.mapping(index, self.indent_of(index))

    def mapping(self, index: int, indent: int) -> tuple[dict, int]:
        result: dict[str, Any] = {}
        while True:
            index = self.next_content(index)
            if index >= len(self.lines):
                return result, index
            current = self.indent_of(index)
            if current < indent:
                return result, index
            if current > indent:
                raise WorkflowParseError(f"unexpected indent on line {index + 1}")
            content = self.content_of(index)
            if content == "-" or content.startswith("- "):
                raise WorkflowParseError(
                    f"sequence entry inside a mapping on line {index + 1}"
                )
            key, rest = self.split_key(content, index)
            value, index = self.value(rest, index, current)
            result[key] = value

    def sequence(self, index: int, indent: int) -> tuple[list, int]:
        result: list[Any] = []
        while True:
            index = self.next_content(index)
            if index >= len(self.lines):
                return result, index
            current = self.indent_of(index)
            if current < indent:
                return result, index
            if current > indent:
                raise WorkflowParseError(f"unexpected indent on line {index + 1}")
            content = self.content_of(index)
            if content != "-" and not content.startswith("- "):
                return result, index
            body = content[1:]
            offset = current + 1 + (len(body) - len(body.lstrip(" ")))
            body = body.lstrip(" ")
            if not body:
                nxt = self.next_content(index + 1)
                if nxt < len(self.lines) and self.indent_of(nxt) > current:
                    value, index = self.collection(nxt, self.indent_of(nxt))
                else:
                    value, index = None, index + 1
                result.append(value)
                continue
            if self.starts_a_mapping(body):
                self.lines[index] = " " * offset + body
                value, index = self.mapping(index, offset)
            else:
                key_free, index_after = self.value(body, index, current)
                value, index = key_free, index_after
            result.append(value)

    def starts_a_mapping(self, body: str) -> bool:
        if body.startswith(("[", "{")):
            return False
        try:
            self.split_key(body, 0)
        except WorkflowParseError:
            return False
        return True

    def split_key(self, content: str, index: int) -> tuple[str, str]:
        quote: str | None = None
        for position, character in enumerate(content):
            if quote is not None:
                if character == quote:
                    quote = None
                continue
            if character in "'\"":
                quote = character
                continue
            if character == ":" and (
                position + 1 == len(content) or content[position + 1] == " "
            ):
                key = content[:position].strip()
                if not key:
                    raise WorkflowParseError(f"empty mapping key on line {index + 1}")
                return _unquote(key), content[position + 1 :].strip()
        raise WorkflowParseError(f"expected 'key: value' on line {index + 1}")

    # -- values ------------------------------------------------------------

    def value(self, rest: str, index: int, indent: int) -> tuple[Any, int]:
        if not rest:
            nxt = self.next_content(index + 1)
            if nxt >= len(self.lines):
                return None, nxt
            child = self.indent_of(nxt)
            if child > indent:
                return self.collection(nxt, child)
            if child == indent:
                content = self.content_of(nxt)
                if content == "-" or content.startswith("- "):
                    return self.sequence(nxt, child)
            return None, index + 1
        if _BLOCK_SCALAR.fullmatch(rest):
            return self.block_scalar(rest, index, indent)
        return _scalar(rest), index + 1

    def block_scalar(self, header: str, index: int, indent: int) -> tuple[str, int]:
        chomp = header[1:]
        collected: list[str] = []
        block_indent: int | None = None
        cursor = index + 1
        while cursor < len(self.lines):
            raw = self.lines[cursor]
            if not raw.strip():
                collected.append("")
                cursor += 1
                continue
            current = len(raw) - len(raw.lstrip(" "))
            if current <= indent:
                break
            if block_indent is None:
                block_indent = current
            elif current < block_indent:
                break
            collected.append(raw[block_indent:])
            cursor += 1
        while collected and not collected[-1]:
            collected.pop()
        body = "\n".join(collected)
        if chomp != "-" and body:
            body += "\n"
        return body, cursor


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        inner = value[1:-1]
        return inner.replace("''", "'") if value[0] == "'" else inner
    return value


def _scalar(text: str) -> Any:
    text = text.strip()
    if text.startswith(("[", "{")):
        value, remainder = _flow(text)
        if remainder.strip():
            raise WorkflowParseError(f"trailing flow content: {remainder!r}")
        return value
    if text.startswith(("'", '"')):
        return _unquote(text)
    if text in ("null", "~", ""):
        return None
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    try:
        return int(text)
    except ValueError:
        return text


def _flow(text: str) -> tuple[Any, str]:
    text = text.lstrip()
    if text.startswith("["):
        items: list[Any] = []
        rest = text[1:].lstrip()
        if rest.startswith("]"):
            return items, rest[1:]
        while True:
            item, rest = _flow_item(rest, "],")
            items.append(item)
            rest = rest.lstrip()
            if rest.startswith(","):
                rest = rest[1:].lstrip()
                continue
            if rest.startswith("]"):
                return items, rest[1:]
            raise WorkflowParseError(f"unterminated flow sequence: {text!r}")
    if text.startswith("{"):
        mapping: dict[str, Any] = {}
        rest = text[1:].lstrip()
        if rest.startswith("}"):
            return mapping, rest[1:]
        while True:
            key, rest = _flow_item(rest, ":")
            rest = rest.lstrip()
            if not rest.startswith(":"):
                raise WorkflowParseError(f"expected ':' in flow mapping: {text!r}")
            value, rest = _flow_item(rest[1:].lstrip(), "},")
            mapping[str(key)] = value
            rest = rest.lstrip()
            if rest.startswith(","):
                rest = rest[1:].lstrip()
                continue
            if rest.startswith("}"):
                return mapping, rest[1:]
            raise WorkflowParseError(f"unterminated flow mapping: {text!r}")
    raise WorkflowParseError(f"expected a flow collection: {text!r}")


def _flow_item(text: str, stops: str) -> tuple[Any, str]:
    text = text.lstrip()
    if text.startswith(("[", "{")):
        return _flow(text)
    if text.startswith(("'", '"')):
        quote = text[0]
        cursor = 1
        while cursor < len(text):
            if text[cursor] == quote:
                if quote == "'" and text[cursor + 1 : cursor + 2] == "'":
                    cursor += 2
                    continue
                return _unquote(text[: cursor + 1]), text[cursor + 1 :]
            cursor += 1
        raise WorkflowParseError(f"unterminated quoted flow scalar: {text!r}")
    cursor = 0
    while cursor < len(text) and text[cursor] not in stops:
        cursor += 1
    return _scalar(text[:cursor]), text[cursor:]


# --------------------------------------------------------------------------
# The fence itself.
# --------------------------------------------------------------------------


def _finding(path: str, job: str, runs_on: str, reason: str, **extra: Any) -> dict:
    finding = {
        "detail": _DETAILS[reason],
        "job": job,
        "path": path,
        "reason": reason,
        "runs_on": runs_on,
    }
    finding.update(extra)
    return finding


def classify_label(label: str) -> str | None:
    """Return the refusal reason for one runner label, or None if it is allowed."""

    trimmed = label.strip()
    lowered = trimmed.casefold()
    if not trimmed or "${" in trimmed:
        return "runner_unresolved"
    if "self-hosted" in lowered:
        return "runner_self_hosted"
    if lowered.startswith("macos"):
        return "runner_hosted_macos"
    if lowered.startswith("windows"):
        return "runner_hosted_windows"
    if trimmed in ALLOWED_RUNNER_LABELS:
        return None
    return "runner_label_unknown"


def _expression_labels(text: str) -> tuple[list[str], str | None]:
    """Resolve an expression to the labels written inside it.

    Returns (labels, reason). A reason means REFUSE without reading labels: the
    expression carries a value this fence cannot see.
    """

    regions = _EXPRESSION.findall(text)
    if not regions:
        return [], None
    # A `"` that survives the removal of every single-quoted literal is a
    # double-quoted literal in the expression itself. One INSIDE a literal is
    # ordinary JSON — `fromJSON('["ubuntu-latest"]')` — and refusing that would
    # refuse the very shape this fence is meant to read.
    if any('"' in _SINGLE_QUOTED.sub("", region) for region in regions):
        return [], "runner_expression_double_quoted_literal"
    outside = _EXPRESSION.sub("", text)
    if outside.strip():
        # `ubuntu-${{ ... }}`: the label is a concatenation, so no literal in
        # the file is the label.
        return [], "runner_unresolved"

    labels: list[str] = []
    for region in regions:
        for raw in _SINGLE_QUOTED.findall(region):
            literal = raw.replace("''", "'")
            if literal.lstrip().startswith("["):
                try:
                    parsed = json.loads(literal)
                except json.JSONDecodeError:
                    return [], "runner_unresolved"
                if not isinstance(parsed, list) or not parsed:
                    return [], "runner_unresolved"
                if not all(isinstance(item, str) for item in parsed):
                    return [], "runner_unresolved"
                labels.extend(parsed)
                continue
            if literal in EVENT_NAME_TOKENS:
                continue
            if _LABEL_SHAPED.fullmatch(literal):
                labels.append(literal)
                continue
            return [], "runner_unresolved"
    if not labels:
        return [], "runner_unresolved"
    return labels, None


def _matrix_labels(job: dict, key: str) -> list[str] | None:
    """Resolve `matrix.<key>` through strategy.matrix, or None if unreadable."""

    strategy = job.get("strategy")
    if not isinstance(strategy, dict):
        return None
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return None
    labels: list[str] = []
    base = matrix.get(key)
    if base is not None:
        if not isinstance(base, list) or not base:
            return None
        if not all(isinstance(item, str) for item in base):
            return None
        labels.extend(base)
    include = matrix.get("include")
    if include is not None:
        if not isinstance(include, list):
            return None
        for entry in include:
            if not isinstance(entry, dict):
                return None
            item = entry.get(key)
            if item is None:
                continue
            if not isinstance(item, str):
                return None
            labels.append(item)
    if not labels:
        return None
    seen: list[str] = []
    for label in labels:
        if label not in seen:
            seen.append(label)
    return seen


def _runs_on_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _labels_from_runs_on(job: dict, runs_on: Any) -> tuple[list[str], str | None]:
    if isinstance(runs_on, list):
        if not runs_on or not all(isinstance(item, str) for item in runs_on):
            return [], "runner_unresolved"
        return list(runs_on), None
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels")
        if isinstance(labels, str):
            labels = [labels]
        if not isinstance(labels, list) or not labels:
            return [], "runner_unresolved"
        if not all(isinstance(item, str) for item in labels):
            return [], "runner_unresolved"
        return list(labels), None
    if not isinstance(runs_on, str):
        return [], "runner_unresolved"
    matrix = _MATRIX_REFERENCE.fullmatch(runs_on.strip())
    if matrix is not None:
        resolved = _matrix_labels(job, matrix.group(1))
        if resolved is None:
            return [], "runner_unresolved"
        return resolved, None
    if "${{" in runs_on:
        return _expression_labels(runs_on)
    return [runs_on], None


def _scan_job(
    path: str, name: str, job: Any, local_workflows: frozenset[str]
) -> list[dict]:
    if not isinstance(job, dict):
        return [_finding(path, name, "", "job_not_a_mapping")]
    runs_on = job.get("runs-on")
    if runs_on is None:
        uses = job.get("uses")
        if isinstance(uses, str) and uses.startswith("./"):
            # A local callee is scanned in its own right; a callee this tree
            # does not carry is a runner nobody here can read.
            if uses[2:] in local_workflows:
                return []
            return [_finding(path, name, uses, "runner_unresolved")]
        if isinstance(uses, str):
            return [_finding(path, name, uses, "runner_external_reusable_workflow")]
        return [_finding(path, name, "", "runner_absent")]

    text = _runs_on_text(runs_on)
    labels, reason = _labels_from_runs_on(job, runs_on)
    if reason is not None:
        return [_finding(path, name, text, reason)]
    findings = []
    for label in labels:
        label_reason = classify_label(label)
        if label_reason is not None:
            findings.append(_finding(path, name, text, label_reason, label=label))
    return findings


def scan_workflow_source(
    path: str, text: str, *, local_workflows: frozenset[str] = frozenset()
) -> list[dict]:
    """Return every runner finding for one workflow file's text."""

    try:
        document = load_workflow(text)
    except WorkflowParseError as exc:
        return [_finding(path, "", "", "workflow_unparsed", parse_error=str(exc))]
    if not isinstance(document, dict):
        return [_finding(path, "", "", "workflow_not_a_mapping")]
    jobs = document.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return [_finding(path, "", "", "workflow_jobs_invalid")]
    findings: list[dict] = []
    for name in sorted(jobs):
        findings.extend(_scan_job(path, str(name), jobs[name], local_workflows))
    return _sorted(findings)


def _sorted(findings: Iterable[dict]) -> list[dict]:
    return sorted(
        findings,
        key=lambda finding: (
            str(finding["path"]),
            str(finding["job"]),
            str(finding["reason"]),
            str(finding.get("label", "")),
        ),
    )


def scan_tree(root: Path) -> list[dict]:
    """Return every runner finding for a projected tree's workflow files."""

    base = Path(root)
    directory = base / ".github" / "workflows"
    relative_directory = f"{WORKFLOW_DIRECTORY}/"
    if directory.is_symlink() or not directory.is_dir():
        return [_finding(WORKFLOW_DIRECTORY, "", "", "workflows_directory_empty")]
    paths = sorted(
        entry
        for entry in directory.iterdir()
        if entry.suffix in WORKFLOW_SUFFIXES and not entry.name.startswith(".")
    )
    if not paths:
        return [_finding(WORKFLOW_DIRECTORY, "", "", "workflows_directory_empty")]
    local_workflows = frozenset(
        relative_directory + entry.name for entry in paths if not entry.is_symlink()
    )
    findings: list[dict] = []
    for entry in paths:
        relative = relative_directory + entry.name
        if entry.is_symlink() or not entry.is_file():
            findings.append(_finding(relative, "", "", "workflow_path_type_invalid"))
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append(_finding(relative, "", "", "workflow_unreadable"))
            continue
        findings.extend(
            scan_workflow_source(relative, text, local_workflows=local_workflows)
        )
    return _sorted(findings)
