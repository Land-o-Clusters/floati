#!/usr/bin/env python3
"""Build the repo-shipped monospace font the capture fixtures declare (CAP-4-F1/F2).

The TUI capture tests must never depend on host fonts. This script
deterministically generates ``tests/fixtures/floati-capture-mono.ttf`` from
the 5x7 pixel alphabet drawn below, so the committed bytes are reproducible
and their provenance is this file.

Provenance (CAP-4-F2): the alphabet is ORIGINAL WORK drawn for this
repository and ships under this repository's MIT licence. No third-party
bitmap was copied; an earlier draft described it as a "classic public-domain
LCD table" without a citation, which is exactly the gap this file closes by
redrawing every glyph as authored art.

    python3 scripts/capture-fixture-font.py           # (re)write the fixture
    python3 scripts/capture-fixture-font.py --check   # exit 1 if bytes drift

Pillow consumes the font through its bytes (ImageFont.truetype over the
bytes, the 09-02 Pillow law); no fontconfig or host font is ever consulted.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_FONT = REPOSITORY_ROOT / "tests" / "fixtures" / "floati-capture-mono.ttf"

PIXEL = 100  # font units per bitmap pixel
ROWS = 7
ADVANCE = 6 * PIXEL
BASELINE_TOP = (ROWS - 1) * PIXEL  # y of the bitmap's top row in font units

# The authored alphabet: one 7-tuple of 5-character rows per printable ASCII
# codepoint 0x20..0x7E ('X' = inked pixel, '.' = empty). Top row first.
GLYPHS: dict[int, tuple[str, str, str, str, str, str, str]] = {
    0x20: (".....", ".....", ".....", ".....", ".....", ".....", "....."),
    0x21: ("..X..", "..X..", "..X..", "..X..", "..X..", ".....", "..X.."),
    0x22: (".X.X.", ".X.X.", ".....", ".....", ".....", ".....", "....."),
    0x23: (".X.X.", ".X.X.", "XXXXX", ".X.X.", "XXXXX", ".X.X.", ".X.X."),
    0x24: ("..X..", ".XXXX", "X....", ".XXX.", "....X", "XXXX.", "..X.."),
    0x25: ("XX..X", "XX.X.", "..X..", "..X..", ".X.XX", "X..XX", "....."),
    0x26: (".XX..", "X..X.", ".XX..", "X.X.X", "X..X.", ".XX.X", "X...X"),
    0x27: ("..X..", "..X..", ".....", ".....", ".....", ".....", "....."),
    0x28: ("..X..", ".X...", "X....", "X....", "X....", ".X...", "..X.."),
    0x29: ("..X..", "...X.", "....X", "....X", "....X", "...X.", "..X.."),
    0x2A: (".....", "..X..", "X.X.X", ".XXX.", "X.X.X", ".....", "....."),
    0x2B: (".....", "..X..", "..X..", "XXXXX", "..X..", "..X..", "....."),
    0x2C: (".....", ".....", ".....", ".....", "..X..", "..X..", ".X..."),
    0x2D: (".....", ".....", ".....", "XXXXX", ".....", ".....", "....."),
    0x2E: (".....", ".....", ".....", ".....", ".....", "..XX.", "..XX."),
    0x2F: ("....X", "....X", "...X.", "..X..", ".X...", "X....", "X...."),
    0x30: (".XXX.", "X...X", "X..XX", "X.X.X", "XX..X", "X...X", ".XXX."),
    0x31: ("..X..", ".XX..", "..X..", "..X..", "..X..", "..X..", ".XXX."),
    0x32: (".XXX.", "X...X", "....X", "...X.", "..X..", ".X...", "XXXXX"),
    0x33: ("XXXX.", "....X", "....X", "..XX.", "....X", "....X", "XXXX."),
    0x34: ("...X.", "..XX.", ".X.X.", "X..X.", "XXXXX", "...X.", "...X."),
    0x35: ("XXXXX", "X....", "XXXX.", "....X", "....X", "X...X", ".XXX."),
    0x36: ("..XX.", ".X...", "X....", "XXXX.", "X...X", "X...X", ".XXX."),
    0x37: ("XXXXX", "....X", "...X.", "..X..", ".X...", ".X...", ".X..."),
    0x38: (".XXX.", "X...X", "X...X", ".XXX.", "X...X", "X...X", ".XXX."),
    0x39: (".XXX.", "X...X", "X...X", ".XXXX", "....X", "...X.", ".XX.."),
    0x3A: (".....", "..XX.", "..XX.", ".....", "..XX.", "..XX.", "....."),
    0x3B: (".....", "..XX.", "..XX.", ".....", "..X..", "..X..", ".X..."),
    0x3C: ("...X.", "..X..", ".X...", "X....", ".X...", "..X..", "...X."),
    0x3D: (".....", ".....", "XXXXX", ".....", "XXXXX", ".....", "....."),
    0x3E: (".X...", "..X..", "...X.", "....X", "...X.", "..X..", ".X..."),
    0x3F: (".XXX.", "X...X", "....X", "...X.", "..X..", ".....", "..X.."),
    0x40: (".XXX.", "X...X", "X.XXX", "X.X.X", "X.XXX", "X....", ".XXX."),
    0x41: (".XXX.", "X...X", "X...X", "XXXXX", "X...X", "X...X", "X...X"),
    0x42: ("XXXX.", "X...X", "X...X", "XXXX.", "X...X", "X...X", "XXXX."),
    0x43: (".XXX.", "X...X", "X....", "X....", "X....", "X...X", ".XXX."),
    0x44: ("XXXX.", "X...X", "X...X", "X...X", "X...X", "X...X", "XXXX."),
    0x45: ("XXXXX", "X....", "X....", "XXXX.", "X....", "X....", "XXXXX"),
    0x46: ("XXXXX", "X....", "X....", "XXXX.", "X....", "X....", "X...."),
    0x47: (".XXX.", "X...X", "X....", "X.XXX", "X...X", "X...X", ".XXXX"),
    0x48: ("X...X", "X...X", "X...X", "XXXXX", "X...X", "X...X", "X...X"),
    0x49: (".XXX.", "..X..", "..X..", "..X..", "..X..", "..X..", ".XXX."),
    0x4A: ("..XXX", "...X.", "...X.", "...X.", "...X.", "X..X.", ".XX.."),
    0x4B: ("X...X", "X..X.", "X.X..", "XX...", "X.X..", "X..X.", "X...X"),
    0x4C: ("X....", "X....", "X....", "X....", "X....", "X....", "XXXXX"),
    0x4D: ("X...X", "XX.XX", "X.X.X", "X.X.X", "X...X", "X...X", "X...X"),
    0x4E: ("X...X", "XX..X", "X.X.X", "X..XX", "X...X", "X...X", "X...X"),
    0x4F: (".XXX.", "X...X", "X...X", "X...X", "X...X", "X...X", ".XXX."),
    0x50: ("XXXX.", "X...X", "X...X", "XXXX.", "X....", "X....", "X...."),
    0x51: (".XXX.", "X...X", "X...X", "X...X", "X.X.X", "X..X.", ".XX.X"),
    0x52: ("XXXX.", "X...X", "X...X", "XXXX.", "X.X..", "X..X.", "X...X"),
    0x53: (".XXXX", "X....", "X....", ".XXX.", "....X", "....X", "XXXX."),
    0x54: ("XXXXX", "..X..", "..X..", "..X..", "..X..", "..X..", "..X.."),
    0x55: ("X...X", "X...X", "X...X", "X...X", "X...X", "X...X", ".XXX."),
    0x56: ("X...X", "X...X", "X...X", "X...X", "X...X", ".X.X.", "..X.."),
    0x57: ("X...X", "X...X", "X...X", "X.X.X", "X.X.X", "XX.XX", "X...X"),
    0x58: ("X...X", "X...X", ".X.X.", "..X..", ".X.X.", "X...X", "X...X"),
    0x59: ("X...X", "X...X", ".X.X.", "..X..", "..X..", "..X..", "..X.."),
    0x5A: ("XXXXX", "....X", "...X.", "..X..", ".X...", "X....", "XXXXX"),
    0x5B: (".XXX.", ".X...", ".X...", ".X...", ".X...", ".X...", ".XXX."),
    0x5C: ("X....", "X....", ".X...", "..X..", "...X.", "....X", "....X"),
    0x5D: (".XXX.", "...X.", "...X.", "...X.", "...X.", "...X.", ".XXX."),
    0x5E: ("..X..", ".X.X.", "X...X", ".....", ".....", ".....", "....."),
    0x5F: (".....", ".....", ".....", ".....", ".....", ".....", "XXXXX"),
    0x60: (".X...", "..X..", ".....", ".....", ".....", ".....", "....."),
    0x61: (".....", ".....", ".XXX.", "....X", ".XXXX", "X...X", ".XXXX"),
    0x62: ("X....", "X....", "XXXX.", "X...X", "X...X", "X...X", "XXXX."),
    0x63: (".....", ".....", ".XXXX", "X....", "X....", "X....", ".XXXX"),
    0x64: ("....X", "....X", ".XXXX", "X...X", "X...X", "X...X", ".XXXX"),
    0x65: (".....", ".....", ".XXX.", "X...X", "XXXXX", "X....", ".XXXX"),
    0x66: ("..XX.", ".X..X", ".X...", "XXXX.", ".X...", ".X...", ".X..."),
    0x67: (".....", ".....", ".XXXX", "X...X", "X...X", ".XXXX", "....X"),
    0x68: ("X....", "X....", "XXXX.", "X...X", "X...X", "X...X", "X...X"),
    0x69: ("..X..", ".....", "..X..", "..X..", "..X..", "..X..", "..X.."),
    0x6A: ("...X.", ".....", "...X.", "...X.", "...X.", "X..X.", ".XX.."),
    0x6B: ("X....", "X....", "X..X.", "X.X..", "XX...", "X.X..", "X..X."),
    0x6C: (".XX..", "..X..", "..X..", "..X..", "..X..", "..X..", ".XXX."),
    0x6D: (".....", ".....", "XX.XX", "X.X.X", "X.X.X", "X.X.X", "X.X.X"),
    0x6E: (".....", ".....", "XXXX.", "X...X", "X...X", "X...X", "X...X"),
    0x6F: (".....", ".....", ".XXX.", "X...X", "X...X", "X...X", ".XXX."),
    0x70: (".....", ".....", "XXXX.", "X...X", "X...X", "XXXX.", "X...."),
    0x71: (".....", ".....", ".XXXX", "X...X", "X...X", ".XXXX", "....X"),
    0x72: (".....", ".....", "X.XX.", "XX...", "X....", "X....", "X...."),
    0x73: (".....", ".....", ".XXXX", "X....", ".XXX.", "....X", "XXXX."),
    0x74: (".X...", ".X...", "XXXX.", ".X...", ".X...", ".X...", "..XX."),
    0x75: (".....", ".....", "X...X", "X...X", "X...X", "X...X", ".XXXX"),
    0x76: (".....", ".....", "X...X", "X...X", "X...X", ".X.X.", "..X.."),
    0x77: (".....", ".....", "X...X", "X...X", "X.X.X", "X.X.X", ".X.X."),
    0x78: (".....", ".....", "X...X", ".X.X.", "..X..", ".X.X.", "X...X"),
    0x79: (".....", ".....", "X...X", "X...X", "X...X", ".XXXX", "....X"),
    0x7A: (".....", ".....", "XXXXX", "...X.", "..X..", ".X...", "XXXXX"),
    0x7B: ("...XX", "..X..", ".X...", ".X...", ".X...", "..X..", "...XX"),
    0x7C: ("..X..", "..X..", "..X..", "..X..", "..X..", "..X..", "..X.."),
    0x7D: ("XX...", "..X..", "...X.", "...X.", "...X.", "..X..", "XX..."),
    0x7E: (".....", ".....", ".X.X.", "X.X.X", ".....", ".....", "....."),
}


def _glyph_name(codepoint: int) -> str:
    return f"uni{codepoint:04X}"


def _draw_glyph(codepoint: int):
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    pen = TTGlyphPen(None)
    for y, row in enumerate(GLYPHS[codepoint]):
        x = 0
        while x < len(row):
            if row[x] != "X":
                x += 1
                continue
            run_start = x
            while x < len(row) and row[x] == "X":
                x += 1
            # Merge horizontal pixel runs into one rectangle per stroke.
            x0 = run_start * PIXEL
            x1 = x * PIXEL
            top = BASELINE_TOP - y * PIXEL
            pen.moveTo((x0, top))
            pen.lineTo((x1, top))
            pen.lineTo((x1, top + PIXEL))
            pen.lineTo((x0, top + PIXEL))
            pen.closePath()
    return pen.glyph()


def build_font_bytes() -> bytes:
    from fontTools.fontBuilder import FontBuilder

    codepoints = sorted(GLYPHS)
    glyph_order = [".notdef"] + [_glyph_name(cp) for cp in codepoints]

    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap({cp: _glyph_name(cp) for cp in codepoints})
    glyphs = {name: _draw_glyph(cp) for cp, name in
              ((cp, _glyph_name(cp)) for cp in codepoints)}
    glyphs[".notdef"] = _draw_glyph(0x20)
    builder.setupGlyf(glyphs)
    metrics = {_glyph_name(cp): (ADVANCE, 0) for cp in codepoints}
    metrics[".notdef"] = (ADVANCE, 0)
    builder.setupHorizontalMetrics(metrics)
    builder.setupNameTable({
        "familyName": "Floati Capture Mono",
        "styleName": "Regular",
        "psName": "FloatiCaptureMono",
    })
    builder.setupOS2(
        sTypoAscender=BASELINE_TOP + PIXEL,
        sTypoDescender=0,
        sTypoLineGap=0,
        usWinAscent=BASELINE_TOP + PIXEL,
        usWinDescent=0,
    )
    builder.setupHorizontalHeader(ascent=BASELINE_TOP + PIXEL, descent=0)
    builder.setupPost()
    # Deterministic bytes: no wall-clock identity in the committed fixture.
    builder.font["head"].created = 0
    builder.font["head"].modified = 0

    target = io.BytesIO()
    builder.save(target)
    return target.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    data = build_font_bytes()
    if args.write:
        FIXTURE_FONT.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_FONT.write_bytes(data)
        print(f"wrote {FIXTURE_FONT.relative_to(REPOSITORY_ROOT)} ({len(data)} bytes)")
        return 0
    if not FIXTURE_FONT.is_file():
        print(f"fixture font missing: {FIXTURE_FONT}", file=sys.stderr)
        return 1
    if FIXTURE_FONT.read_bytes() != data:
        print("fixture font bytes drifted from the generator", file=sys.stderr)
        return 1
    print("fixture font matches the generator")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
