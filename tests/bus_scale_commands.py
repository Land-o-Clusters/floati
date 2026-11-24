"""Reviewed parser closure for the BUS-SCALE-CENSUS-1 diagnostic.

BUS-SCALE-CENSUS-1: the merged fixture is captured from the pinned source parser
at 1f2f40f68d7bb958b2927b9a699058b4ffa54bb0. Importing this helper never
rebuilds the reviewed population. The merge adds explicit person membership and
leased native ad-hoc workers, and expands address, hook and lifecycle grammar.
Each new path has an exact disposition; historical fixtures remain byte-for-byte
measurement provenance. Parser coverage does not qualify new native workloads.

Only the fixture's two exact example-argument temporary-path tokens follow the
native platform temporary parent. Argument defaults and all other row fields
remain exact. This is grammar portability, not a workload or scale verdict.
The API's ``routine`` partition names non-administration contract candidates;
the architect's separately scoped workload map decides which are measured.
"""

from __future__ import annotations

import argparse
import copy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from floati.command_contract import describe_parser
# The contract examples name the native parent, never a test override; the
# name stays worker_workspace_root so a platform patch still reaches it.
from floati.host_paths import native_worker_workspace_root as worker_workspace_root
from tests.projected_fixture_pins import SOURCE_FIXTURE_DIGESTS, expected_fixture_sha256


BASELINE_SOURCE_SHA = "1f2f40f68d7bb958b2927b9a699058b4ffa54bb0"
_FIXTURE_RELATIVE = "tests/fixtures/bus-scale-census/native-command-contract-1f2f40f6.json"
_FIXTURE_PATH = Path(__file__).resolve().parents[1] / _FIXTURE_RELATIVE
BASELINE_FIXTURE_SHA256 = SOURCE_FIXTURE_DIGESTS[_FIXTURE_RELATIVE]
_FIXTURE = _FIXTURE_PATH
_NODE_SPINUP_FIXTURE_RELATIVE = "tests/fixtures/node-spinup-command-contract.v0.json"
_NODE_SPINUP_FIXTURE = Path(__file__).resolve().parents[1] / _NODE_SPINUP_FIXTURE_RELATIVE
_NODE_SPINUP_FIXTURE_SHA256 = "57c5fd86638fff7456513814f152d5abfe2102253e55fc60aeaa6093087d1e80"
_PREPARE_FIXTURE_RELATIVE = "tests/fixtures/update-prepare-command-contract.v0.json"
_PREPARE_FIXTURE = Path(__file__).resolve().parents[1] / _PREPARE_FIXTURE_RELATIVE
_PREPARE_SHA256 = "0b7be914b7be262b9337d65e27e09483d701095f4a1e4b6e33b53a62f91d76e1"
_TRAIN_AMENDMENT = _FIXTURE_PATH.with_name("native-command-amendment-7d0c404e.json")
_TRAIN_SOURCE_SHA = "7d0c404e7ac5e588505ff2392f93dcd4c677c53b"
_QUIET_AMENDMENT = _FIXTURE_PATH.with_name("native-command-amendment-d6263c86.json")
_QUIET_SOURCE_SHA = "d6263c86db08b6c88428eceb91ca6230217d30d6"
_ROW_FIELDS = frozenset({
    "path", "program", "executable", "public", "mcp_exposure", "mcp_required",
    "mcp_omit", "artifact_schema_version", "arguments",
    "mutually_exclusive_groups", "example_argv",
})

# Each exception names one exact executable parser path. New descendants,
# options, choices or other contract changes cannot inherit these reasons.
_ADMINISTRATION = {
    "init": "Fleet creation and initial topology, outside routine seat operation.",
    "setup": "Operator provisioning and file restoration for one local seat.",
    "register": "Registry membership administration.",
    "retire": "Registry membership administration.",
    "node add": "Node provisioning and initial assignment.",
    "node spawn": "Role-instance provisioning.",
    "node retire": "Node membership retirement.",
    "node switch": "Provider assignment administration.",
    "node role": "Role assignment administration.",
    "node person set": "Explicit person membership assignment is registry administration, outside routine seat operation.",
    "node person clear": "Explicit person membership removal is registry administration, outside routine seat operation.",
    "seat add": "Owned seat provisioning and configuration.",
    "seat remove": "Owned seat removal.",
    "seat migrate": "One-time pre-governance seat migration.",
    "seat retire": "Owned seat retirement and membership transition, outside steady-state seat operation.",
    "hook install": "Harness hook installation.",
    "hook uninstall": "Removal of an owned harness hook registration.",
    "backup": "Explicit archive backup publication and retention administration.",
    "wake daemon consent": "Daemon activation consent administration.",
    "wake daemon bind": "Daemon runtime binding administration.",
    "wake daemon install": "LaunchAgent installation.",
    "wake daemon start": "Daemon process lifecycle administration.",
    "wake daemon stop": "Daemon process lifecycle administration.",
    "wake daemon remove": "LaunchAgent removal.",
    "wake daemon revoke": "Daemon activation consent revocation.",
    "sequencer serve": "Managed writer process lifecycle administration.",
    "sequencer direct": "Managed writer mode administration.",
    "grant": "Work-authority grant administration.",
    "grant revoke": "Work-authority revocation administration.",
    "epoch roll": "Whole bus epoch archival and replacement.",
    "journal checkpoint": "Explicit journal checkpoint administration.",
    "journal verify": "Explicit checkpoint verification administration.",
    "repair quarantine": "Governed ledger repair administration.",
    "signature sign": "Explicit artifact-signature and key operation.",
    "signature verify": "Explicit artifact-signature verification.",
    "install": "Governed product installation.",
    "update": "Governed product update.",
    "update prepare": "Explicit root preparation after a product update.",
    "update consent": "Product update consent administration.",
    "update revoke": "Product update consent revocation.",
    "update status": "Product update administration status.",
    "update check": "Product update availability administration.",
    "update apply": "Governed product update application.",
    "update rollback": "Governed product update rollback.",
    "update fleet preview": "Fleet-wide product update planning.",
    "update fleet apply": "Fleet-wide product update application.",
    "uninstall": "Owned product removal.",
    "purge": "Explicit root archival into account Trash.",
    "chart add-root": "Declared-root configuration administration.",
    "chart remove-root": "Declared-root configuration administration.",
    "survey": "Explicit foreign-bus estate administration survey.",
    "snapshot": "Consented maintainer support-bundle administration.",
    "context policy set": "Tide policy administration.",
    "context policy clear": "Tide policy administration.",
    "presence set": "Explicit operator presence declaration for the fleet.",
    "role transfer-architect": "Architect role transfer administration.",
    "role new": "Root-local role template authoring.",
    "role import": "Root-local role template import.",
    "role edit": "Root-local role template replacement.",
}


def _signature(row: dict) -> str:
    """Keep JSON types exact: Python equality alone equates True with 1."""
    def require_json(value: object) -> None:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("command contract JSON object keys must be strings")
                require_json(item)
        elif type(value) is list:
            for item in value:
                require_json(item)
        elif type(value) not in (str, int, float, bool, type(None)):
            raise ValueError("command contract must contain exact JSON value types")

    require_json(row)
    try:
        return json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("command contract must contain finite JSON data") from exc


def _row_signature(row: dict) -> tuple[str, str]:
    if type(row) is not dict or set(row) != _ROW_FIELDS:
        raise ValueError("command contract row has missing or extra fields")
    parts = row["path"]
    if (
        type(parts) is not list or not parts
        or any(
            type(part) is not str or not part or any(char.isspace() for char in part)
            for part in parts
        )
    ):
        raise ValueError("command contract path must be a nonempty token list")
    if type(row["executable"]) is not bool:
        raise ValueError("command contract executable flag must be boolean")
    return " ".join(parts), _signature(row)


def _path(row: dict) -> str:
    return _row_signature(row)[0]


def _from_commands(commands: list) -> dict[str, dict]:
    if type(commands) is not list:
        raise ValueError("native command contract commands must be a list")
    rows: dict[str, dict] = {}
    seen: set[str] = set()
    for row in commands:
        path = _path(row)
        if path in seen:
            raise ValueError("native command contract contains a duplicate path")
        seen.add(path)
        if row["executable"]:
            rows[path] = row
    return rows


def _validate_rows(rows: dict[str, dict]) -> dict[str, str]:
    if type(rows) is not dict:
        raise ValueError("command coverage must be a dictionary of full rows")
    signatures = {}
    for path, row in rows.items():
        actual, signature = _row_signature(row)
        if type(path) is not str or path != actual or row["executable"] is not True:
            raise ValueError("command coverage key must name its executable full row")
        signatures[path] = signature
    return signatures


def _baseline_state() -> tuple[dict[str, dict], dict[str, str]]:
    """Recheck live bytes before reusing a content- and platform-bound template."""
    try:
        data = _FIXTURE.read_bytes()
    except OSError as exc:
        raise ValueError("reviewed native command fixture is unavailable") from exc
    if hashlib.sha256(data).hexdigest() != expected_fixture_sha256(_FIXTURE_PATH):
        raise ValueError("reviewed native command fixture digest changed")
    amendment = _NODE_SPINUP_FIXTURE.read_bytes()
    if hashlib.sha256(amendment).hexdigest() != expected_fixture_sha256(_NODE_SPINUP_FIXTURE):
        raise ValueError("node spinup command amendment digest changed")
    prepare = _PREPARE_FIXTURE.read_bytes()
    if hashlib.sha256(prepare).hexdigest() != expected_fixture_sha256(_PREPARE_FIXTURE):
        raise ValueError("update preparation command amendment digest changed")
    train = _TRAIN_AMENDMENT.read_bytes()
    if hashlib.sha256(train).hexdigest() != expected_fixture_sha256(_TRAIN_AMENDMENT):
        raise ValueError("train command amendment digest changed")
    quiet = _QUIET_AMENDMENT.read_bytes()
    if hashlib.sha256(quiet).hexdigest() != expected_fixture_sha256(_QUIET_AMENDMENT):
        raise ValueError("quiet release command amendment digest changed")
    rows, signatures = _baseline_template(
        data, amendment, prepare, train, quiet, str(worker_workspace_root().parent)
    )
    if len(_ADMINISTRATION) != 59 or not set(_ADMINISTRATION) <= set(rows):
        raise ValueError("reviewed administration exceptions do not bind the fixture")
    return rows, signatures


@lru_cache(maxsize=4)
def _baseline_template(
    data: bytes, amendment: bytes, prepare: bytes, train: bytes, quiet: bytes, temporary_parent: str
) -> tuple[dict, dict]:
    """Private templates are never returned through the mutable public API."""
    fixture = json.loads(data)
    if (
        fixture.get("kind") != "command_contract"
        or type(fixture.get("schema_version")) is not int
        or fixture["schema_version"] != 0
        or fixture.get("program") != "floati"
        or fixture.get("command_count") != len(fixture.get("commands", ()))
    ):
        raise ValueError("reviewed native command fixture header is invalid")
    rows = _from_commands(fixture["commands"])
    # Keep the historical captured fixture intact. This row's generated
    # amendment binds only its two reviewed administration grammars.
    updated = json.loads(amendment)
    changes = _from_commands(updated["commands"])
    if updated.get("schema_version") != 0 or set(changes) != {"hook install", "node role"}:
        raise ValueError("node spinup command amendment scope changed")
    if updated.get("predecessors") != {path: _signature(rows[path]) for path in changes}:
        raise ValueError("node spinup command amendment predecessor changed")
    rows.update(changes)
    rows.update(_from_commands([json.loads(prepare)]))
    train_document = json.loads(train)
    train_rows = _from_commands(train_document["commands"])
    if (train_document.get("schema_version") != 0
            or train_document.get("source_commit") != _TRAIN_SOURCE_SHA
            or set(train_rows) & set(rows) != {"work add"}):
        raise ValueError("train command amendment scope changed")
    rows.update(train_rows)
    # The train tip changed one grammar the train amendment froze: an operator
    # can resolve a stopped intent with its pid and start token. The amendment
    # names the exact row it replaces and the reviewed row it must replace.
    quiet_document = json.loads(quiet)
    quiet_rows = _from_commands(quiet_document["commands"])
    if (quiet_document.get("schema_version") != 0
            or quiet_document.get("source_commit") != _QUIET_SOURCE_SHA
            or set(quiet_rows) != {"quiet release"}
            or quiet_document.get("predecessors") != {path: _signature(rows[path]) for path in quiet_rows}):
        raise ValueError("quiet release command amendment scope changed")
    rows.update(quiet_rows)

    # The captured Darwin fixture contains only these two exact native example
    # tokens. Substitute in the expected examples, never in supplied rows,
    # defaults, user values, arbitrary descendants or any other contract field.
    temporary_parent = Path(temporary_parent)
    tokens = {
        "\x2fprivate/tmp/floati-contract": str(temporary_parent / "floati-contract"),
        "\x2fprivate/tmp/floati-contract.json": str(temporary_parent / "floati-contract.json"),
    }
    for row in rows.values():
        row["example_argv"] = [tokens.get(value, value) for value in row["example_argv"]]
    rows["setup"] = _setup_contract(temporary_parent)
    # NET-REPLAY adds one exact audit command to the historical parser capture.
    # Bind its selector options using the captured ask root grammar.
    root = next(arg for arg in rows["ask"]["arguments"] if arg["name"] == "root")
    rows["replay"] = {
        "path": ["replay"],
        "program": "floati replay",
        "executable": True,
        "public": True,
        "mcp_exposure": "never",
        "mcp_required": [],
        "mcp_omit": [],
        "artifact_schema_version": None,
        "arguments": [
            dict(root),
            dict(root, name="question", metavar="ID", option_strings=["--question"],
                 required=False, mutually_exclusive_group=0),
            dict(root, name="work_item", metavar="ID", option_strings=["--work-item"],
                 required=False, mutually_exclusive_group=0),
        ],
        "mutually_exclusive_groups": [{"required": True, "arguments": ["question", "work_item"]}],
        "example_argv": ["replay", "--root", str(temporary_parent / "floati-contract"),
                         "--question", "value"],
    }
    return rows, {path: _signature(row) for path, row in rows.items()}


def _setup_contract(temporary_parent: Path) -> dict:
    """Pin this one administrative addition without changing the captured fixture."""
    def flag(name, *, metavar=None, required=False, choices=(), group=None, switch=False):
        return {"name": name, "metavar": metavar, "option_strings": ["--" + name],
                "positional": False, "required": required,
                "type": "boolean" if switch else "string", "item_type": "string",
                "repeat": False, "nargs": 0 if switch else None, "choices": list(choices),
                "default": False if switch else None, "mutually_exclusive_group": group}

    harnesses = ("claude", "codex", "cursor", "zcode")
    return {"path": ["setup"], "program": "floati setup", "executable": True, "public": True,
            "mcp_exposure": "never", "mcp_required": [], "mcp_omit": [],
            "artifact_schema_version": None,
            "arguments": [flag("root", metavar="PATH", required=True),
                          flag("seat", choices=harnesses), flag("session", metavar="SESSION"),
                          flag("executable", metavar="PATH"), flag("hook", choices=harnesses),
                          flag("confirm", metavar="TOKEN", group=0),
                          flag("teardown", group=0, switch=True)],
            "mutually_exclusive_groups": [{"required": False, "arguments": ["confirm", "teardown"]}],
            "example_argv": ["setup", "--root", str(temporary_parent / "floati-contract")]}


def _baseline_contract() -> dict[str, dict]:
    return copy.deepcopy(_baseline_state()[0])


def executable_contract(parser: argparse.ArgumentParser) -> dict[str, dict]:
    """Project every native executable node without executing any handler."""
    return _from_commands(describe_parser(parser)["commands"])


def classify_contract(rows: dict[str, dict]) -> dict:
    """Classify only exact reviewed rows; changed and new rows stay unknown.

    This projection does not by itself certify the population is complete.
    ``require_coverage`` also rejects missing reviewed paths and measurements.
    """
    signatures = _validate_rows(rows)
    _baseline, baseline_signatures = _baseline_state()
    return _classify_signatures(signatures, baseline_signatures)


def _classify_signatures(signatures: dict, baseline_signatures: dict) -> dict:
    routine: list[str] = []
    administration: dict[str, str] = {}
    unknown: list[str] = []
    for path in sorted(signatures):
        if signatures[path] != baseline_signatures.get(path):
            unknown.append(path)
        elif path in _ADMINISTRATION:
            administration[path] = _ADMINISTRATION[path]
        else:
            routine.append(path)
    return {"routine": routine, "administration": administration, "unknown": unknown}


def _require_closed_contract(rows: dict[str, dict]) -> dict:
    """Validate the full frozen population once, without deriving fake measurements."""
    signatures = _validate_rows(rows)
    _baseline, expected = _baseline_state()
    if set(signatures) != set(expected):
        raise ValueError("native executable population has missing or extra paths")
    classified = _classify_signatures(signatures, expected)
    if classified["unknown"]:
        raise ValueError("native executable contract contains new or changed rows: "
                         + ", ".join(classified["unknown"]))
    routine, administration = set(classified["routine"]), set(classified["administration"])
    if routine & administration or routine | administration != set(rows):
        raise ValueError("routine and administration coverage must be a disjoint partition")
    return classified


def require_coverage(rows: dict[str, dict], measured_contracts: dict[str, dict]) -> None:
    """Refuse incomplete, stale, overlapping or unreviewed command coverage.

    Rows bind parser grammar only. A supplied row is not evidence that its
    native workload ran, succeeded, or met any measurement threshold.
    """
    signatures = _validate_rows(rows)
    measured_signatures = _validate_rows(measured_contracts)
    _baseline, expected = _baseline_state()
    if set(rows) != set(expected):
        raise ValueError("native executable population has missing or extra paths")
    classified = _classify_signatures(signatures, expected)
    routine = set(classified["routine"])
    administration = set(classified["administration"])
    if classified["unknown"]:
        raise ValueError("native executable contract contains new or changed rows")
    if routine & administration or routine | administration != set(rows):
        raise ValueError("routine and administration coverage must be a disjoint partition")
    if set(measured_contracts) != routine:
        raise ValueError("routine measurement coverage has missing, extra or administration paths")
    for path in sorted(routine):
        if measured_signatures[path] != signatures[path]:
            raise ValueError("routine measurement does not bind its full native contract row")
