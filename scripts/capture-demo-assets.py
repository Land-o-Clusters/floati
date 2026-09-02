#!/usr/bin/env python3
"""Build governed Demo/UAT capture candidates without appending corpus rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import NamedTuple, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFont


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from floati.brand import render_buoy_mark  # noqa: E402
from floati.demo import build_demo_model, seed_demo  # noqa: E402
from floati import fixture_ids  # noqa: E402
from floati.graph import HarborGraph, HarborTraffic  # noqa: E402
from floati.graph_render import render_harbor_chart  # noqa: E402
from floati.host_paths import capture_temporary_parent  # noqa: E402
from floati.identity_fence import (  # noqa: E402
    RETIRED_PRODUCT_NAME,
    GOVERNED_TEMP_PREFIXES,
    redact_governed_temp_prefixes,
)
from floati.replay_render import render_replay_frame  # noqa: E402
from floati.tui_render import render_frame  # noqa: E402
SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")


def _operator_account_name() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or Path.home().name


UNSAFE_TEXT = (
    "\x2fUsers/",
    *GOVERNED_TEMP_PREFIXES,
    _operator_account_name(),
    # The refusal token KEEPS ITS VALUE -- it is what stops the retired name
    # reaching a rendered GIF -- and is hex-built so the guard is not itself
    # a finding of the scrub that now carries the same name.
    RETIRED_PRODUCT_NAME + "-spawn-groups",
)
INSTALL_CAPTURE_TEMPORARY_PREFIX = "floati-capture-install-"


class CaptureSpec(NamedTuple):
    name: str
    captured_from: str
    maximum_bytes: int


class CaptureArtifact(NamedTuple):
    name: str
    path: Path
    sha256: str
    width: int
    height: int
    bytes: int
    captured_from: str
    source_sha: str
    source_scale: int


def capture_specs() -> tuple[CaptureSpec, ...]:
    return (
        CaptureSpec(
            "hero-three-fault-replay.gif",
            "deterministic three-fault replay fixture",
            10_000_000,
        ),
        CaptureSpec(
            "board-glow.gif",
            "deterministic Floati fleet fixture",
            6_000_000,
        ),
        CaptureSpec(
            "harbor-chart-map.gif",
            "deterministic Floati fleet fixture",
            6_000_000,
        ),
        CaptureSpec(
            "install-moment.gif",
            "committed-tree Floati install receipt",
            6_000_000,
        ),
    )


def candidate_sizes() -> dict[str, tuple[int, int]]:
    return {
        "hero-three-fault-replay.gif": (1400, 640),
        "board-glow.gif": (1400, 760),
        "harbor-chart-map.gif": (1400, 520),
        "install-moment.gif": (1400, 600),
    }


def validate_source_sha(value: object) -> str:
    if not isinstance(value, str) or SOURCE_SHA.fullmatch(value) is None:
        raise ValueError("source SHA must be lowercase 40-hex")
    return value


def ensure_capture_text_safe(text: str) -> str:
    for token in UNSAFE_TEXT:
        if token.casefold() in text.casefold():
            raise ValueError(f"capture text contains private token: {token}")
    return text


def _collapse_install_staging_coordinate(text: str) -> str:
    temporary_prefix = INSTALL_CAPTURE_TEMPORARY_PREFIX
    exposed_coordinate = temporary_prefix.removesuffix("-")
    return re.sub(
        re.escape(f"<temp>/{temporary_prefix}") + r"[a-z0-9_]{8}(?=/)",
        f"<temp>/{exposed_coordinate}",
        text,
    )


def _expose_install_receipt(text: str) -> str:
    exposed = redact_governed_temp_prefixes(text)
    exposed = _collapse_install_staging_coordinate(exposed)
    return ensure_capture_text_safe(exposed)


def validate_output_paths(output: Path, master_output: Path) -> None:
    if not master_output.is_absolute():
        raise ValueError("master output must be absolute and outside the repository")
    output = output.resolve()
    master_output = master_output.resolve()
    demo_root = (REPOSITORY_ROOT / "docs" / "demo").resolve()
    if output == demo_root or demo_root not in output.parents:
        raise ValueError("candidate output must be a child of docs/demo")
    if not master_output.is_absolute() or (
        master_output == REPOSITORY_ROOT or REPOSITORY_ROOT in master_output.parents
    ):
        raise ValueError("master output must be absolute and outside the repository")


def load_replay_artifact(path: Path) -> dict[str, object]:
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.startswith("{"):
            continue
        candidate = json.loads(line)
        if (
            isinstance(candidate, dict)
            and candidate.get("command") == "log"
            and isinstance(candidate.get("evidence"), dict)
        ):
            return dict(candidate["evidence"])
    raise ValueError("banked replay artifact is absent")


def _validated_capture_temporary_parent(candidate: Path | None = None) -> Path:
    safe_parent = candidate if candidate is not None else capture_temporary_parent()
    repository_root = REPOSITORY_ROOT.resolve()
    resolved = safe_parent.resolve()
    if (
        not safe_parent.is_absolute()
        or not safe_parent.is_dir()
        or safe_parent.is_symlink()
        or not os.access(safe_parent, os.W_OK)
        or resolved == repository_root
        or repository_root in resolved.parents
    ):
        raise RuntimeError(
            f"public-safe capture temporary parent is unavailable: {safe_parent}"
        )
    return resolved


def _install_frames() -> list[str]:
    safe_parent = _validated_capture_temporary_parent()
    with tempfile.TemporaryDirectory(
        prefix=INSTALL_CAPTURE_TEMPORARY_PREFIX, dir=safe_parent
    ) as temporary:
        base = Path(temporary)
        destination = base / "installed"
        source = base / "source"
        head = subprocess.run(
            ["/usr/bin/git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            [
                "/usr/bin/git",
                "clone",
                "--shared",
                "--no-checkout",
                str(REPOSITORY_ROOT),
                str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["/usr/bin/git", "checkout", "--detach", head],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            [
                str(source / "scripts" / "floati"),
                "install",
                "--source",
                str(source),
                "--destination",
                str(destination),
                "--committed-tree",
            ],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"committed-tree install failed: {result.stderr.strip()}")
        receipt = _expose_install_receipt(result.stdout.strip())
    logical_lines = receipt.replace(',"', ',\n"').splitlines()
    wrapped = [
        piece
        for line in logical_lines
        for piece in textwrap.wrap(
            line,
            width=108,
            break_long_words=True,
            break_on_hyphens=False,
        )
    ]
    receipt_view = "\n".join([*wrapped[:4], *wrapped[-16:]]) + "\n"
    mark = render_buoy_mark(color=True)
    mark_lines = mark.splitlines()
    frames = [receipt_view]
    for visible in range(1, len(mark_lines) + 1):
        frames.append(
            "\n".join(mark_lines[-visible:]) + "\n" + receipt_view
        )
    return frames


NEUTRAL_CAPTURE_IDENTITIES = dict(
    zip(
        (
            fixture_ids.compose("fa", "ble"),
            fixture_ids.compose("lane", "-app"),
            fixture_ids.compose("lane", "-floati"),
        ),
        ("floati-a", "floati-b", "floati-c"),
    )
)


def _neutralize_fixture_identities(text: str) -> str:
    for retired, neutral in NEUTRAL_CAPTURE_IDENTITIES.items():
        text = text.replace(retired, neutral)
    return text


def build_text_frames() -> dict[str, list[str]]:
    replay_path = (
        REPOSITORY_ROOT
        / "docs"
        / "evidence"
        / "captures"
        / "floati-replay-drill.txt"
    )
    artifact = load_replay_artifact(replay_path)
    events = list(artifact["events"])
    hero = [
        render_replay_frame(artifact, count, width=112, height=38)
        for count in range(1, len(events) + 1)
    ]

    with tempfile.TemporaryDirectory(prefix="floati-capture-fixture-") as temporary:
        root = seed_demo(Path(temporary) / "demo-fleet")
        model = build_demo_model(root)
        board = [
            _neutralize_fixture_identities(
                render_frame(model, 112, 38, selected=index, color=True)
            )
            for index in range(4)
        ]
        topology = HarborGraph(root).artifact()
        traffic = HarborTraffic(root).artifact()
        chart_frame = _neutralize_fixture_identities(
            render_harbor_chart(topology, traffic, color=True)
        )
        chart = [chart_frame, chart_frame]

    frames = {
        "hero-three-fault-replay.gif": hero,
        "board-glow.gif": board,
        "harbor-chart-map.gif": chart,
        "install-moment.gif": _install_frames(),
    }
    for sequence in frames.values():
        for frame in sequence:
            ensure_capture_text_safe(frame)
    return frames


ANSI_COLOR = re.compile(r"\x1b\[([0-9;]*)m")
FONT = Path("/System/Library/Fonts/Menlo.ttc")
LAMP_FONT = FONT
BACKGROUND = "#12161c"
FOREGROUND = "#d8dee9"
DIM = "#9aa6b2"
ACCENTS = ("#ff9f43", "#f08b35", "#ffb26b")
LIT_LAMP = "#F5C518"
ANSI_PALETTE = {
    "38;5;240": "#585858",
    "38;5;252": "#d0d0d0",
    "38;5;245": DIM,
    "38;5;37": "#00afaf",
    "38;5;214": "#ffaf00",
    "38;5;196": "#ff0000",
    "38;5;45": "#00d7ff",
    "38;5;42": "#00d787",
    "90": "#585858",
    "97": "#d0d0d0",
    "37": DIM,
    "36": "#00afaf",
    "33": "#ffaf00",
    "31": "#ff0000",
    "96": "#00d7ff",
    "32": "#00d787",
}
SEMANTIC_ACCENT = (
    "DRIVING",
    "DEGRADED",
    "! DENIAL",
    "TRAFFIC",
    "REPLAY COMPLETE",
    "⊙",
)


def _line_runs(line: str, accent: str) -> list[tuple[str, str]]:
    runs: list[tuple[str, str]] = []
    color = FOREGROUND
    cursor = 0
    for match in ANSI_COLOR.finditer(line):
        if match.start() > cursor:
            runs.append((line[cursor : match.start()], color))
        code = match.group(1)
        if code in {"38;5;208", "93"}:
            color = accent
        elif code in ANSI_PALETTE:
            color = ANSI_PALETTE[code]
        elif code in {"", "0"}:
            color = FOREGROUND
        cursor = match.end()
    if cursor < len(line):
        runs.append((line[cursor:], color))
    if not runs:
        runs.append(("", color))
    if ANSI_COLOR.sub("", line).strip() == "⊙":
        return [(text, LIT_LAMP) for text, _ in runs]
    if all(color == FOREGROUND for _, color in runs) and any(
        token in line for token in SEMANTIC_ACCENT
    ):
        return [(text, accent) for text, _ in runs]
    return runs


def _overlay_lit_lamps(
    image: Image.Image,
    text: str,
    *,
    source_font: ImageFont.FreeTypeFont,
    source_margin: int,
    source_line_height: int,
) -> None:
    draw = ImageDraw.Draw(image)
    lamp_font = ImageFont.truetype(str(LAMP_FONT), max(8, round(source_font.size / 2)))
    source_measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for index, line in enumerate(text.splitlines()):
        visible = ANSI_COLOR.sub("", line)
        if visible.strip() != "⊙":
            continue
        prefix = visible[: visible.index("⊙")]
        x = (source_margin + source_measure.textlength(prefix, font=source_font)) / 2
        y = (source_margin + index * source_line_height) / 2
        draw.text((round(x), round(y)), "⊙", font=lamp_font, fill=LIT_LAMP)


def render_capture_frame(
    text: str,
    *,
    candidate_size: tuple[int, int],
    phase: int,
) -> Image.Image:
    ensure_capture_text_safe(text)
    width, height = candidate_size
    if width < 80 or height < 48:
        raise ValueError("candidate dimensions are too small")
    source_size = (width * 2, height * 2)
    font_size = max(7, round(source_size[0] / 82))
    font = ImageFont.truetype(str(FONT), font_size)
    margin = max(8, round(source_size[0] * 0.018))
    line_height = max(9, round(font_size * 1.24))
    image = Image.new("RGB", source_size, BACKGROUND)
    draw = ImageDraw.Draw(image)
    accent = ACCENTS[phase % len(ACCENTS)]
    y = margin
    for line in text.splitlines():
        if y + line_height > source_size[1] - margin:
            break
        x = margin
        for piece, color in _line_runs(line, accent):
            draw.text((x, y), piece, font=font, fill=color)
            x += round(draw.textlength(piece, font=font))
            if x >= source_size[0] - margin:
                break
        y += line_height
    rendered = image.resize(candidate_size, Image.Resampling.LANCZOS)
    _overlay_lit_lamps(
        rendered,
        text,
        source_font=font,
        source_margin=margin,
        source_line_height=line_height,
    )
    return rendered


def render_capture_frames(
    text_frames: Sequence[str],
    *,
    candidate_size: tuple[int, int],
) -> list[Image.Image]:
    return [
        render_capture_frame(text, candidate_size=candidate_size, phase=index)
        for index, text in enumerate(text_frames)
    ]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _master_frame(frame: Image.Image) -> Image.Image:
    canvas = Image.new("RGB", (3840, 2160), BACKGROUND)
    fitted = frame.copy()
    fitted.thumbnail(canvas.size, Image.Resampling.LANCZOS)
    offset = ((canvas.width - fitted.width) // 2, (canvas.height - fitted.height) // 2)
    canvas.paste(fitted, offset)
    return canvas


def _write_master(
    destination: Path,
    text_frames: Sequence[str],
    ffmpeg: Path,
) -> None:
    if not ffmpeg.is_absolute() or not ffmpeg.is_file() or not os.access(ffmpeg, os.X_OK):
        raise ValueError("ffmpeg must be one absolute executable")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="floati-master-frames-") as temporary:
        frame_root = Path(temporary)
        rendered = render_capture_frames(text_frames, candidate_size=(3840, 2160))
        for index, frame in enumerate(rendered):
            _master_frame(frame).save(frame_root / f"frame-{index:03d}.png")
        result = subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-framerate",
                "1.5",
                "-i",
                str(frame_root / "frame-%03d.png"),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "slow",
                "-crf",
                "18",
                "-movflags",
                "+faststart",
                str(destination),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"4K master encoding failed: {result.stderr.strip()}")


def build_candidates(
    output: Path,
    master_output: Path,
    source_sha: str,
    *,
    ffmpeg: Path | None,
    candidate_size: tuple[int, int] | None = None,
) -> list[CaptureArtifact]:
    validate_output_paths(output, master_output)
    source_sha = validate_source_sha(source_sha)
    text_frames = build_text_frames()
    output.mkdir(parents=True, exist_ok=True)
    durations = {
        "hero-three-fault-replay.gif": 650,
        "board-glow.gif": 550,
        "harbor-chart-map.gif": 650,
        "install-moment.gif": 260,
    }
    artifacts: list[CaptureArtifact] = []
    for spec in capture_specs():
        path = output / spec.name
        rendered_size = candidate_size or candidate_sizes()[spec.name]
        frames = render_capture_frames(
            text_frames[spec.name],
            candidate_size=rendered_size,
        )
        write_gif(path, frames, duration_ms=durations[spec.name])
        size = path.stat().st_size
        if size > spec.maximum_bytes:
            raise ValueError(f"{spec.name} exceeds its ruled byte ceiling")
        artifacts.append(
            CaptureArtifact(
                spec.name,
                path,
                _digest(path),
                rendered_size[0],
                rendered_size[1],
                size,
                spec.captured_from,
                source_sha,
                2,
            )
        )
    if ffmpeg is not None:
        master_path = master_output / "hero-three-fault-replay.mp4"
        _write_master(
            master_path,
            text_frames["hero-three-fault-replay.gif"],
            ffmpeg,
        )
        artifacts.append(
            CaptureArtifact(
                master_path.name,
                master_path,
                _digest(master_path),
                3840,
                2160,
                master_path.stat().st_size,
                "deterministic three-fault replay fixture",
                source_sha,
                2,
            )
        )
    return artifacts


def _palettize_frame(frame: Image.Image) -> Image.Image:
    delta = ImageChops.difference(frame, Image.new("RGB", frame.size, LIT_LAMP))
    red, green, blue = delta.split()
    nonmatching = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    exact_lamp = nonmatching.point(lambda value: 255 if value == 0 else 0)
    if exact_lamp.getbbox() is None:
        return frame.convert("P", palette=Image.Palette.ADAPTIVE)
    converted = frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=255)
    palette = converted.getpalette()
    palette[255 * 3 : 256 * 3] = [245, 197, 24]
    converted.putpalette(palette)
    converted.paste(255, mask=exact_lamp)
    return converted


def write_gif(
    path: Path,
    frames: Sequence[Image.Image],
    *,
    duration_ms: int,
) -> None:
    if len(frames) < 2:
        raise ValueError("animated GIF requires at least two frames")
    path.parent.mkdir(parents=True, exist_ok=True)
    converted = [_palettize_frame(frame) for frame in frames]
    converted[0].save(
        path,
        format="GIF",
        save_all=True,
        append_images=converted[1:],
        duration=duration_ms,
        loop=0,
        disposal=2,
        optimize=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--master-output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--ffmpeg", type=Path)
    args = parser.parse_args(argv)
    artifacts = build_candidates(
        args.output,
        args.master_output,
        args.source_sha,
        ffmpeg=args.ffmpeg,
    )
    report = [
        {
            **artifact._asdict(),
            "path": str(artifact.path),
        }
        for artifact in artifacts
    ]
    print(json.dumps({"artifacts": report}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
