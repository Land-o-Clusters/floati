from __future__ import annotations

import hashlib
import importlib
import inspect
import io
import json
import os
import re
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


DA1 = b"\x1b[?1;2c"
DA2 = b"\x1b[>1;4000;0c"
SYNC = b"\x1b[?2026;1$y"
MOUSE = b"\x1b[?1006;2$y"
PIXELS = b"\x1b[?1016;3$y"
KITTY_GRAPHICS = b"\x1b_Gi=7108;OK\x1b\\"
KITTY_KEYBOARD = b"\x1b[?1u"
RGB = b"\x1bP1+r524742=31\x1b\\"
FULL_RESPONSE = DA1 + SYNC + MOUSE + PIXELS + KITTY_GRAPHICS + KITTY_KEYBOARD + RGB + DA2
FACT_NAMES = (
    "synchronized_output",
    "sgr_mouse",
    "sgr_pixels",
    "kitty_graphics",
    "kitty_keyboard",
    "rgb",
)


def _cap_api():
    try:
        module = importlib.import_module("floati.tui_capabilities")
    except ModuleNotFoundError:
        module = None
    if module is None:
        raise AssertionError("R3-CAP module must exist")
    return module


def _responses() -> dict[str, bytes]:
    return {
        "da1": DA1,
        "da2": DA2,
        "synchronized_output": SYNC,
        "sgr_mouse": MOUSE,
        "sgr_pixels": PIXELS,
        "kitty_graphics": KITTY_GRAPHICS,
        "kitty_keyboard": KITTY_KEYBOARD,
        "rgb": RGB,
    }


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 91


class RegattaR3CapabilityTests(unittest.TestCase):
    def test_da1_barrier_precedes_every_capability_query_and_preserves_input(self) -> None:
        """Catches capability probes preceding DA1 or eating startup keystrokes."""
        cap = _cap_api()
        output = TTY()

        receipt, remainder = cap.probe_terminal_capabilities(
            TTY(),
            output,
            environment={},
            preloaded_response=b"j" + FULL_RESPONSE + b"q",
        )

        self.assertEqual(
            cap.DA1_QUERY + cap.CAPABILITY_QUERY_BATCH,
            output.getvalue().encode("ascii"),
        )
        self.assertEqual(b"jq", remainder)
        self.assertTrue(receipt.enabled("kitty_graphics"))

    def test_exact_responses_control_only_their_named_capability(self) -> None:
        """Catches one successful protocol response cross-enabling a neighbor."""
        cap = _cap_api()
        responses = _responses()
        responses["kitty_graphics"] = b"\x1b_Gi=7108;EINVAL\x1b\\"
        responses["rgb"] = b"\x1bP0+r524742\x1b\\"

        receipt = cap.receipt_from_responses(
            responses,
            timed_out=(),
            environment={},
        )
        states = {fact.name: fact.state for fact in receipt.facts}

        self.assertEqual("unsupported", states["kitty_graphics"])
        self.assertEqual("unsupported", states["rgb"])
        self.assertEqual(
            {"supported"},
            {states[name] for name in FACT_NAMES if name not in {"kitty_graphics", "rgb"}},
        )
        self.assertFalse(receipt.enabled("kitty_graphics"))
        self.assertTrue(receipt.enabled("synchronized_output"))

    def test_every_fact_is_receipted_with_state_stamp_source_and_digest(self) -> None:
        """Catches an unstamped fact or a receipt digest not binding its contents."""
        cap = _cap_api()
        receipt = cap.receipt_from_responses(
            _responses(),
            timed_out=(),
            environment={},
        )
        artifact = receipt.to_dict()
        receipt_digest = artifact.pop("receipt_digest")
        canonical = json.dumps(
            artifact,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        self.assertEqual(0, artifact["schema_version"])
        self.assertEqual("terminal_capability_receipt", artifact["kind"])
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), receipt_digest)
        self.assertEqual(FACT_NAMES, tuple(fact.name for fact in receipt.facts))
        for fact in receipt.facts:
            with self.subTest(name=fact.name):
                self.assertEqual("supported", fact.state)
                self.assertEqual("MEASURED", fact.stamp)
                self.assertTrue(fact.source)
                self.assertRegex(fact.evidence_digest, r"^[0-9a-f]{64}$")

        from floati.errors import ProtocolRefusal

        with self.assertRaises(ProtocolRefusal) as caught:
            replace(receipt, receipt_digest="0" * 64)
        self.assertEqual("terminal_capability_receipt_invalid", caught.exception.code)

    def test_timeout_is_typed_unknown_never_unsupported(self) -> None:
        """Catches silence being converted into a false negative capability claim."""
        cap = _cap_api()
        receipt = cap.receipt_from_responses(
            {"da1": DA1},
            timed_out=FACT_NAMES,
            environment={},
        )

        self.assertEqual(
            [(name, "unknown", "MEASURED", "probe_timeout") for name in FACT_NAMES],
            [(fact.name, fact.state, fact.stamp, fact.source) for fact in receipt.facts],
        )
        self.assertFalse(any(receipt.enabled(name) for name in FACT_NAMES))

    def test_tmux_is_the_endpoint_and_outer_brand_cannot_override_measurement(self) -> None:
        """Catches an outer terminal brand overriding the directly queried tmux pane."""
        cap = _cap_api()
        environment = {
            "TMUX": "\x2fprivate/tmp/tmux-501/default,123,0",
            "TERM": "tmux-256color",
            "TERM_PROGRAM": "WezTerm",
            "KITTY_WINDOW_ID": "77",
        }
        receipt = cap.receipt_from_responses(
            {"da1": DA1},
            timed_out=FACT_NAMES,
            environment=environment,
        )

        self.assertEqual("tmux", receipt.endpoint_kind)
        self.assertEqual("ESTIMATE", receipt.endpoint_stamp)
        self.assertNotIn(environment["TMUX"], receipt.endpoint_id)
        self.assertNotIn(environment["TERM_PROGRAM"], receipt.to_json())
        self.assertFalse(receipt.enabled("kitty_graphics"))
        self.assertEqual({"unknown"}, {fact.state for fact in receipt.facts})
        self.assertEqual({"ESTIMATE"}, {fact.stamp for fact in receipt.facts})
        self.assertEqual(
            {"brand_environment_after_timeout"},
            {fact.source for fact in receipt.facts},
        )

        derived = cap.receipt_from_responses(
            {"da1": DA1},
            timed_out=FACT_NAMES,
            environment={"TERM": "xterm-256color"},
        )
        self.assertEqual({"unknown"}, {fact.state for fact in derived.facts})
        self.assertEqual({"DERIVED"}, {fact.stamp for fact in derived.facts})
        self.assertEqual(
            {"terminal_environment_after_timeout"},
            {fact.source for fact in derived.facts},
        )

    def test_no_color_cannot_change_capability_receipt_bytes(self) -> None:
        """Catches rendering policy poisoning measured terminal truth."""
        cap = _cap_api()
        without_policy = cap.receipt_from_responses(
            _responses(),
            timed_out=(),
            environment={"TERM": "xterm-256color"},
        )
        with_policy = cap.receipt_from_responses(
            _responses(),
            timed_out=(),
            environment={"TERM": "xterm-256color", "NO_COLOR": "1"},
        )

        self.assertEqual(without_policy.to_json(), with_policy.to_json())
        self.assertNotIn("NO_COLOR", with_policy.to_json())

    def test_non_tty_stdout_emits_zero_probe_bytes(self) -> None:
        """Catches terminal queries leaking into redirected or machine output."""
        cap = _cap_api()
        output = io.StringIO()

        receipt, remainder = cap.probe_terminal_capabilities(
            TTY(),
            output,
            environment={"TERM": "xterm-kitty"},
            preloaded_response=FULL_RESPONSE,
        )

        self.assertEqual("", output.getvalue())
        self.assertEqual(b"", remainder)
        self.assertEqual(
            [(name, "unknown", "stdout_not_tty") for name in FACT_NAMES],
            [(fact.name, fact.state, fact.source) for fact in receipt.facts],
        )
        self.assertFalse(any(receipt.enabled(name) for name in FACT_NAMES))

    def test_partial_coalesced_and_oversized_replies_preserve_user_input_boundedly(self) -> None:
        """Catches arbitrary reads losing input or retaining hostile unbounded bytes."""
        cap = _cap_api()
        partial = KITTY_GRAPHICS[:-1]
        partial_responses, partial_remainder = cap.parse_capability_responses(partial)
        self.assertEqual({}, partial_responses)
        self.assertEqual(partial, partial_remainder)

        responses, remainder = cap.parse_capability_responses(
            b"j" + FULL_RESPONSE + b"q"
        )
        self.assertEqual(
            {"da1", "da2", *FACT_NAMES},
            set(responses),
        )
        self.assertEqual(b"jq", remainder)

        oversized_responses, oversized_remainder = cap.parse_capability_responses(
            b"x" * 4097 + FULL_RESPONSE
        )
        self.assertEqual({}, oversized_responses)
        self.assertLessEqual(len(oversized_remainder), 64)

    def test_board_and_live_map_emit_the_same_per_endpoint_receipt(self) -> None:
        """Catches the pure capability artifact never reaching the real TUI loops."""
        from floati.tui import run_board
        from floati.tui_chart import run_live_harbor_map
        from floati.tui_render import HarborBoardModel
        from tests.test_regatta_r1 import _artifact
        from tests.test_tui_render import RECEIPTS, SNAPSHOT, WORK

        self.assertIn("capability_receipt_sink", inspect.signature(run_board).parameters)
        self.assertIn(
            "capability_receipt_sink",
            inspect.signature(run_live_harbor_map).parameters,
        )

        board_receipts: list[dict[str, object]] = []
        board_output = TTY()
        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui.termios.tcgetattr", return_value=[]),
            patch("floati.tui.termios.tcsetattr"),
            patch("floati.tui.tty.setcbreak"),
            patch("floati.tui._read_terminal_input", return_value="q"),
            patch(
                "floati.tui.shutil.get_terminal_size",
                return_value=os.terminal_size((100, 30)),
            ),
        ):
            run_board(
                model_loader=lambda: model,
                input_stream=TTY(),
                output_stream=board_output,
                terminal_response=FULL_RESPONSE,
                capability_receipt_sink=board_receipts.append,
            )

        map_receipts: list[dict[str, object]] = []
        map_output = TTY()
        events = ["q"]
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_chart.termios.tcgetattr", return_value=[]),
            patch("floati.tui_chart.termios.tcsetattr"),
            patch("floati.tui_chart.tty.setcbreak"),
            patch(
                "floati.tui_chart.shutil.get_terminal_size",
                return_value=os.terminal_size((100, 30)),
            ),
        ):
            run_live_harbor_map(
                snapshot_loader=lambda: _artifact(include_envelope=False),
                input_stream=TTY(),
                output_stream=map_output,
                read_event=lambda timeout: events.pop(0),
                color_tier="mono",
                terminal_response=FULL_RESPONSE,
                capability_receipt_sink=map_receipts.append,
            )

        self.assertEqual(1, len(board_receipts))
        self.assertEqual(board_receipts, map_receipts)

    def test_supported_and_unknown_receipt_evidence_is_byte_identical(self) -> None:
        """Catches committed capability evidence drifting from the real receipt format."""
        cap = _cap_api()
        cases = (
            (
                Path("docs/evidence/capabilities/r3-cap-supported.json"),
                cap.receipt_from_responses(
                    _responses(), timed_out=(), environment={}
                ),
            ),
            (
                Path("docs/evidence/capabilities/r3-cap-unknown.json"),
                cap.receipt_from_responses(
                    {"da1": DA1}, timed_out=FACT_NAMES, environment={}
                ),
            ),
        )
        for path, receipt in cases:
            with self.subTest(path=path):
                self.assertTrue(path.is_file())
                self.assertEqual(receipt.to_json(), path.read_text(encoding="utf-8"))
                self.assertNotIn("NO_COLOR", receipt.to_json())


if __name__ == "__main__":
    unittest.main()
