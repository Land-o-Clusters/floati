"""Golden pins for every load-bearing occurrence of the retired repository name.

Recorded BEFORE any source hex-build (row NAME-2a of the 2026-09-02 name-2 ruling),
at a tree where all twenty-seven sites are still spelled literally. Every value below
is either the CURRENT runtime value or an id derived through the REAL function with a
fixed input. Under R-N5 these pins are never regenerated after a source change: the
retired name is an opaque salt inside SHA-256 preimages that derive stored record ids,
so a byte that moves here is a ledger-compatibility break, not a test to update.

Two layers, deliberately:

* DERIVATION pins call the real function and pin its output byte-for-byte. They catch
  a changed algorithm or a changed domain, but they supply the domain themselves, so
  they cannot see which domain a given call site passes.
* CALL-SITE pins resolve the domain expression at each named site and compare it to
  the expected value. They catch the mis-wiring a derivation pin cannot see -- a
  hex-build that lands the wrong suffix at the wrong site still derives a valid id.

A call-site pin resolves its expression against the imported module's namespace, so a
hex-built replacement must be readable at module or class scope (the convention
floati/identity_fence.py already follows). A domain hidden in a function local would
RED here, and that RED is the instrument working.

The retired name is not spelled in this file. It is built from hex exactly the way
identity_fence.py builds every governed token, and the hex itself is anchored to a
measured digest by the first test -- so a wrong hex here cannot quietly agree with a
wrong hex in the source.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import tempfile
import types
import unittest
from pathlib import Path

from floati import effects, jsonl, records, runtruth, spawn_groups, storage_identity, wake, wake_hold, workers
from floati.adapters import codex_live
from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot
from floati.sequencer import sequencer_socket_path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

RETIRED = bytes.fromhex("736c6970776179").decode("ascii")
RETIRED_DIGEST = "05d5d9c721bc99eabb1a23a75d41968a7b10ff1659a1f1e81f1a76157032484b"

# The nine spawn-lifecycle domains, in the order their call sites appear in
# floati/spawn_groups.py, and the id each derives from one fixed input.
SPAWN_GROUP_DOMAIN_SUFFIXES = (
    "-spawn-group-v1",
    "-spawn-activation-v1",
    "-spawn-abort-v1",
    "-child-admitted-v1",
    "-child-rejected-v1",
    "-spawn-close-v1",
    "-descendant-v1",
    "-observation-close-v1",
    "-late-result-v1",
)
SEMANTIC_UUID_PIN_INPUT = {"pin": "name-2a", "ordinal": 1}
SEMANTIC_UUID_PINS = (
    "66d22613ddd6784793bf0425446f892d",
    "512ae00adce273e6af679f563ae67c2f",
    "7a1db8675f0f7ff19171be02814119a8",
    "a436e949925277138428fd9dd8ff870e",
    "7fb3ca06ca8f70c483ddd6c1857e68e5",
    "1767cb8de7a2781589bb77d19f5ddfc3",
    "c56e7c4dfefc7d7eab8f97f71e0816e5",
    "3c9a6e16296a7708a06ac83506dad48b",
    "cc6e68f6cca671dd9481f0bb89b3d042",
)

WAKE_HOLD_EVENT_DOMAIN = RETIRED + "-wake-hold-events-v1"
WAKE_HOLD_DELIVERY_DOMAIN = RETIRED + "-wake-hold-deliveries-v1"
WAKE_HOLD_ACK_DOMAIN = RETIRED + "-wake-hold-acknowledgments-v1"
WAKE_HOLD_DECISION_DOMAIN = RETIRED + "-wake-hold-decision-v1"

EFFECT_OPERATION_DOMAIN = RETIRED + "-effect-operation-v1"
WORKER_PROCESS_LOSS_DOMAIN = RETIRED + "-worker-effect-process-loss-v1"
LEGACY_WORKSPACE_PREFIX = "." + RETIRED
SEQUENCER_SOCKET_PREFIX = RETIRED + "-sequencer-"
CAPTURE_REFUSAL_TOKEN = RETIRED + "-spawn-groups"

WAKE_HOLD_DECISION_PIN_RECORD = {
    "recipient": "builder-core",
    "worker_session_id": "session-01",
    "idempotency_key": "key-01",
    "limit": 3,
    "item_ids": ["msg-01", "msg-02"],
    "event_prefix_digest": "a" * 64,
    "delivery_prefix_digest": "b" * 64,
    "acknowledgment_prefix_digest": "c" * 64,
}
WAKE_HOLD_DECISION_DIGEST_PIN = (
    "2c9ef5f5adb2a56978a37c064e02bdcbe4b875ed39c7b0928be34bb6de8a85ca"
)

ATTEMPT_FENCE_PIN = (
    "66a100ca3535adff303ee08c75d9f2a9ab8665f13059117f50ff3e1bb713cc02"
)
RETRY_DELAY_PIN = 224
EFFECT_OPERATION_ID_PIN = "effect-op-cd2102d137457806a249d4f65e8811b8"
ONE_SHOT_WAKE_LABEL_PIN = (
    "com.landoclusters.floati.oneshot."
    "15deb86e2aea22752eb9c15671272b025146d1940d7d1c9ab07ce28166ffd6b7"
)

CAPTURE_SCRIPT = REPOSITORY_ROOT / "scripts" / "capture-demo-assets.py"


def _digest(value: bytes | str) -> str:
    return hashlib.sha256(
        value if isinstance(value, bytes) else value.encode("utf-8")
    ).hexdigest()


def _function(module: types.ModuleType, name: str) -> ast.FunctionDef:
    """Return one named function's AST from the module's own source file."""

    source = Path(module.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{module.__name__} no longer defines {name}")


def _resolve(node: ast.expr, module: types.ModuleType) -> object:
    """Return the runtime value of one expression from a shipped call site.

    A literal resolves to itself; a hex-built module constant resolves through the
    imported module's namespace. Anything unreachable from module scope raises, which
    is the pin refusing to guess rather than passing on an unread value.
    """

    # ast.literal_eval cannot be used here and that is the point: after the
    # hex-build the domain is no longer a literal, it is `bytes.fromhex(...)`
    # or a name bound to one. The compiled node comes from this repository's
    # own source file on disk, read by a test, and is evaluated against that
    # same already-imported module's namespace -- no external or user input
    # reaches it. There is no untrusted expression to sandbox.
    try:
        return eval(  # noqa: S307 - repository source, read from disk, in a test
            compile(ast.Expression(body=node), "<retired-name-pin>", "eval"),
            dict(vars(module)),
        )
    except Exception as error:  # pragma: no cover - exercised only on a bad build
        raise AssertionError(
            f"{module.__name__}: a load-bearing domain is not resolvable at module "
            f"scope ({error!r}); hex-build it as a module or class constant"
        ) from error


def _leftmost_addend(node: ast.expr) -> ast.expr:
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        node = node.left
    return node


def _sha256_preimage_prefixes(module: types.ModuleType, function: str) -> list[object]:
    """Return each sha256 preimage prefix inside one function, in source order."""

    calls = [
        node
        for node in ast.walk(_function(module, function))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "sha256"
    ]
    calls.sort(key=lambda node: (node.lineno, node.col_offset))
    return [_resolve(_leftmost_addend(call.args[0]), module) for call in calls]


def _dict_domain_values(module: types.ModuleType, function: str) -> list[object]:
    """Return every `"domain"` value written by one function, in source order."""

    values: list[tuple[tuple[int, int], object]] = []
    for node in ast.walk(_function(module, function)):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "domain":
                values.append(((node.lineno, node.col_offset), _resolve(value, module)))
    values.sort(key=lambda entry: entry[0])
    return [value for _position, value in values]


def _startswith_prefixes(module: types.ModuleType, function: str) -> list[object]:
    """Return every `str.startswith` prefix inside one function, in source order."""

    calls = [
        node
        for node in ast.walk(_function(module, function))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "startswith"
    ]
    calls.sort(key=lambda node: (node.lineno, node.col_offset))
    return [_resolve(call.args[0], module) for call in calls]


def _capture_module() -> types.ModuleType:
    specification = importlib.util.spec_from_file_location(
        "retired_name_pins_capture", CAPTURE_SCRIPT
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class RetiredNameHexAnchorTests(unittest.TestCase):
    """The pins' own token is anchored, so a wrong hex cannot agree with a wrong build."""

    def test_the_pinned_token_matches_its_measured_digest(self) -> None:
        self.assertEqual(7, len(RETIRED))
        self.assertEqual(RETIRED_DIGEST, _digest(RETIRED))


class DerivedIdentityPinTests(unittest.TestCase):
    """L1/L2: every derived id, digest and delay, through the real function."""

    def test_spawn_lifecycle_ids_derive_to_their_recorded_bytes(self) -> None:
        for suffix, pinned in zip(SPAWN_GROUP_DOMAIN_SUFFIXES, SEMANTIC_UUID_PINS):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    pinned,
                    spawn_groups._semantic_uuid(
                        RETIRED + suffix, SEMANTIC_UUID_PIN_INPUT
                    ),
                )

    def test_wake_hold_decision_digest_derives_to_its_recorded_bytes(self) -> None:
        self.assertEqual(
            WAKE_HOLD_DECISION_DIGEST_PIN,
            records.wake_hold_decision_digest(WAKE_HOLD_DECISION_PIN_RECORD),
        )

    def test_attempt_fence_token_derives_to_its_recorded_bytes(self) -> None:
        self.assertEqual(
            ATTEMPT_FENCE_PIN,
            runtruth.attempt_fence_token("run-01", "item-01", 2, 7),
        )

    def test_retry_jitter_derives_to_its_recorded_delay(self) -> None:
        """The retry domain moves TIMING, not only ids; this pin is the only witness."""

        self.assertEqual(
            RETRY_DELAY_PIN,
            runtruth.retry_delay_from_backoff(
                "run-01",
                "item-01",
                3,
                {"base_delay_ms": 100, "cap_delay_ms": 1000, "strategy": "exponential"},
            ),
        )

    def test_one_shot_wake_label_derives_to_its_recorded_bytes(self) -> None:
        request = types.SimpleNamespace(
            root=types.SimpleNamespace(path="/pin/root", tenant_id="tenant-01"),
            run_id="run-01",
            item_id="item-01",
            attempt_id="attempt-01",
            wake_at="2099-01-02T03:04:05.000Z",
            scheduler_epoch=7,
            fence_token="d" * 64,
        )
        self.assertEqual(ONE_SHOT_WAKE_LABEL_PIN, wake._label(request))

    def test_effect_operation_id_derives_to_its_recorded_bytes(self) -> None:
        self.assertEqual(
            EFFECT_OPERATION_ID_PIN,
            effects._semantic_operation_id("tenant-01", "key-01"),
        )


class LoadBearingCallSitePinTests(unittest.TestCase):
    """The wiring a derivation pin cannot see: which domain each site actually passes."""

    def test_spawn_group_call_sites_pass_their_recorded_domains_in_order(self) -> None:
        source = Path(spawn_groups.__file__).read_text(encoding="utf-8")
        calls = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_semantic_uuid"
        ]
        calls.sort(key=lambda node: (node.lineno, node.col_offset))
        self.assertEqual(9, len(calls))
        self.assertEqual(
            [RETIRED + suffix for suffix in SPAWN_GROUP_DOMAIN_SUFFIXES],
            [_resolve(call.args[0], spawn_groups) for call in calls],
        )

    def test_wake_hold_controller_domains_are_their_recorded_values(self) -> None:
        controller = wake_hold.WakeHoldController
        self.assertEqual(WAKE_HOLD_EVENT_DOMAIN, controller._EVENT_DOMAIN)
        self.assertEqual(WAKE_HOLD_DELIVERY_DOMAIN, controller._DELIVERY_DOMAIN)
        self.assertEqual(WAKE_HOLD_ACK_DOMAIN, controller._ACK_DOMAIN)

    def test_delivery_digest_derives_the_same_bytes_in_both_files(self) -> None:
        """R-N1: two files carry this domain today; they must never disagree."""

        prefixes = _sha256_preimage_prefixes(jsonl, "_transact_wake_hold_records")
        self.assertEqual(1, len(prefixes))
        self.assertEqual(
            wake_hold.WakeHoldController._DELIVERY_DOMAIN.encode("ascii") + b"\0",
            prefixes[0],
        )
        self.assertEqual(WAKE_HOLD_DELIVERY_DOMAIN.encode("ascii") + b"\0", prefixes[0])

    def test_wake_hold_decision_preimage_is_its_recorded_bytes(self) -> None:
        prefixes = _sha256_preimage_prefixes(records, "wake_hold_decision_digest")
        self.assertEqual(1, len(prefixes))
        self.assertEqual(
            WAKE_HOLD_DECISION_DOMAIN.encode("ascii") + b"\0", prefixes[0]
        )

    def test_persisted_effect_domain_is_its_recorded_value(self) -> None:
        self.assertEqual(
            [EFFECT_OPERATION_DOMAIN],
            _dict_domain_values(effects, "_semantic_operation_id"),
        )

    def test_persisted_worker_loss_domain_is_its_recorded_value(self) -> None:
        self.assertEqual(
            [WORKER_PROCESS_LOSS_DOMAIN],
            _dict_domain_values(workers, "_record_worker_effect_uncertainty"),
        )


class OnDiskCoordinatePinTests(unittest.TestCase):
    """L3/L4: names the product READS off a disk, and the endpoint it BINDS."""

    def test_legacy_workspace_refusal_still_fires_on_its_recorded_prefix(self) -> None:
        """The migration safety net, exercised through the real refusal."""

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            (workspace / (LEGACY_WORKSPACE_PREFIX + "-bus")).mkdir()
            with self.assertRaises(ProtocolRefusal) as raised:
                storage_identity.refuse_legacy_workspace_artifacts(workspace)
            self.assertEqual("legacy_workspace_artifacts", raised.exception.code)

    def test_legacy_workspace_refusal_ignores_a_current_workspace(self) -> None:
        """Perturbation control: the refusal is the PREFIX, not any dotted entry."""

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            (workspace / storage_identity.EVIDENCE_DIRECTORY).mkdir()
            storage_identity.refuse_legacy_workspace_artifacts(workspace)

    def test_legacy_workspace_prefix_is_its_recorded_value(self) -> None:
        self.assertEqual(
            [LEGACY_WORKSPACE_PREFIX],
            _startswith_prefixes(
                storage_identity, "refuse_legacy_workspace_artifacts"
            ),
        )

    def test_worker_workspace_marker_prefix_is_its_recorded_value(self) -> None:
        self.assertEqual(
            [LEGACY_WORKSPACE_PREFIX],
            _startswith_prefixes(codex_live, "_accept_prepared_workspace"),
        )

    def test_sequencer_endpoint_keeps_its_recorded_directory_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = FloatiRoot.open(Path(temporary) / "root", "alpha")
            socket = sequencer_socket_path(root)
            directory = socket.parent.name
            self.assertEqual("sequencer.sock", socket.name)
            self.assertEqual(SEQUENCER_SOCKET_PREFIX, directory[: len(SEQUENCER_SOCKET_PREFIX)])
            self.assertRegex(directory[len(SEQUENCER_SOCKET_PREFIX):], r"^[0-9a-f]{32}$")


class CaptureRefusalTokenPinTests(unittest.TestCase):
    """L5: the guard that must spell the name to work; its VALUE is the pin."""

    def test_capture_refusal_token_keeps_its_recorded_value(self) -> None:
        module = _capture_module()
        matches = [
            token
            for token in module.UNSAFE_TEXT
            if token == CAPTURE_REFUSAL_TOKEN
        ]
        self.assertEqual([CAPTURE_REFUSAL_TOKEN], matches)

    def test_capture_pipeline_still_refuses_text_carrying_the_token(self) -> None:
        module = _capture_module()
        with self.assertRaises(ValueError):
            module.ensure_capture_text_safe(f"frame {CAPTURE_REFUSAL_TOKEN} frame")
        self.assertEqual(
            "frame builder-core frame",
            module.ensure_capture_text_safe("frame builder-core frame"),
        )


if __name__ == "__main__":
    unittest.main()
