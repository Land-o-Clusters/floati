# APPROVED — R3 capability and graphics sequencing brief

**Seat:** `build lane`  
**Date:** 2026-08-28  
**Program:** `docs/design/NIGHT_HARBOR.md`, Part II R3  
**Doctrine:** `docs/design/tui-research-triage-2026-08-28.md`, T-1 through T-5 and T-8  
**Study:** `docs/evidence/REGATTA-STUDY-2026-08-28.md` and the pinned Regatta research bank

Architect ruling `msg-01a0496179df79f4a5506c5e47e99a9a` approves approach C
with strict serialization: R3-CAP must pass, land to main, and become the base
of a fresh R3-GFX branch before the first GFX RED commit exists. CAP and GFX
have separate envelopes and separate gates; no stacked ungated work is allowed.

## Charter result

R3 must ship Floati's own buoy image on terminals that prove Kitty graphics
support, plus honest per-node activity sparklines. The same activity facts
remain visible as braille in every text tier and may be overpainted by
pixel-resolution charts only when the current terminal endpoint returns the
required protocol response. Absence is silent fallback, not a warning.

The existing Regatta spike is necessary but insufficient. It proves one exact
Kitty `a=q` response can gate one header image. It does not create the ruled
T-4 capability artifact, does not place probes behind a DA1 barrier, and does
not measure synchronized output, SGR mouse, SGR-Pixels, Kitty keyboard, and RGB
as separate facts. R3-GFX therefore cannot legally treat the spike boolean as
the capability layer.

## Fences quoted from the governing program

- R3: **"No third-party logos — our brand only."**
- R3: **"Detection by terminal response, never user-agent guessing; absence of
  support is silent fallback, not a warning."**
- Part II enabling law: terminal innovations remain **"plain bytes written to
  the existing stdout"**, stdlib-only, and unsupported terminals fall through
  the existing tiers.
- T-1: **"KEYBOARD-PRIMARY; POINTER IS ENHANCEMENT; SELECTION IS NOT FOCUS."**
- T-2: **"NO PERPETUAL FRAME LOOP."** Idle remains zero-output and
  event/deadline driven.
- T-3: synchronized output **"WRAPS THE WHOLE PRESENTATION"** — text, graphics,
  and cursor.
- T-4: **"A TERMINAL CAPABILITY IS A MEASUREMENT, AND OUR STAMPS APPLY."**
  DECRQM `?2026/?1006/?1016`, Kitty `a=q` and `?u`, and XTGETTCAP `RGB` are
  MEASURED; heuristics cannot enable them; DA2 identifies but never negotiates;
  tmux is the endpoint when present.
- T-5: **"NO_COLOR IS POLICY, NOT CAPABILITY."** It suppresses color and pixel
  graphics without changing the measured artifact.
- Post-release sequencing: **"Graphics protocols stay OUT until the capability
  layer exists."** The same ruling calls the capability layer a real row.
- T-8: mechanisms may transfer; expression, layout, copy, and brand remain
  Floati's.
- Standing Regatta fences remain: `--plain` and `--json` twins byte-identical,
  all degradation tiers complete, captures re-banked, visible copy
  `DRAFT -` stamped, and no wake/daemon/CLI-dispatch/bus-root edit.

## Approaches considered

### A. Reuse the spike boolean and build graphics immediately

Smallest diff, but rejected. It would enable graphics from one isolated query
without the T-4 artifact or DA1 ordering and would contradict the explicit
"graphics protocols stay OUT" law.

### B. One indivisible R3 commit containing capability probing and graphics

Preserves the public charter label but makes the capability layer impossible
to gate independently. A graphics failure could mask a probe-integrity defect,
and the doctrine explicitly sizes the capability layer as its own row.

### C. R3-CAP prerequisite, then R3-GFX consumer — recommended

Both stay on `lane/sol-r3`, but each begins with its own committed RED bank,
lands behind its own evidence gate, and has an independently reviewable SHA.
R3-CAP changes no rendering. R3-GFX consumes only the frozen artifact and
cannot infer capabilities itself. This is the smallest shape that satisfies
both the charter and T-4 without inventing a hidden prerequisite.

## R3-CAP design — measurement before graphics

### Model

`floati/tui_capabilities.py` owns immutable, in-memory values:

- `CapabilityFact(name, state, stamp, source, evidence_digest)` where `state`
  is `supported`, `unsupported`, or `unknown`. Every fact is individually
  receipted: exact active probe responses carry `MEASURED`; documented
  heuristics carry `DERIVED`; brand-environment observations carry `ESTIMATE`.
  Only `MEASURED` plus `supported` can enable a protocol.
- `TerminalCapabilityReceipt(schema_version, endpoint_id, endpoint_kind,
  facts, receipt_digest)` is the complete per-endpoint artifact passed to
  render loops. `NO_COLOR` is absent from this model. When tmux is the direct
  endpoint, the receipt identifies tmux; an outer terminal name is testimony,
  never an override.
- A deterministic version-zero JSON representation is the receipted artifact
  used by tests/evidence. No durable bus/root ledger and no new CLI verb are
  introduced in this lane.

### Probe choreography

`floati/tui_protocol.py` gains a bounded incremental probe decoder. Startup
sends DA1 alone and preserves all interleaved keyboard/mouse bytes. Only after
an exact DA1 response does it send the bounded query batch for DEC modes 2026,
1006, and 1016, Kitty graphics and keyboard, and XTGETTCAP `RGB`. DA2, when
returned, is stored as endpoint identity and never enables a feature.

Malformed, oversized, duplicate, partial, or timed-out responses cannot
produce `supported`. A timeout is a typed `unknown`, never `unsupported` and
never false. Unknown bytes return to the ordinary input decoder in their
original order. Environment and terminal-name strings may be recorded with
their ruled `DERIVED` or `ESTIMATE` stamp but cannot override the immediate
endpoint's MEASURED response. No probe byte is written when stdout is not a
TTY. No probe enables a terminal mode, so timeout cleanup has no new mode to
reverse.

### R3-CAP RED bank

1. DA1 barrier: no capability query bytes appear before exact DA1; input typed
   during the barrier survives byte-for-byte.
2. Exact response matrix: each DECRQM, Kitty, and XTGETTCAP response controls
   only its own observation; malformed neighbors cannot cross-enable.
3. Artifact honesty: every fact is receipted with state, stamp, source, and
   evidence digest; timeout is typed `unknown`, and no number or support claim
   is filled in.
4. Endpoint law: tmux/outer-terminal environment values cannot override the
   response from the endpoint being queried; the receipt is per endpoint and
   DA2 identifies only.
5. Policy separation: `NO_COLOR` changes no capability artifact byte.
6. Non-TTY silence: no DA1 or capability probe byte is written when stdout is
   not a TTY.
7. Input bounds: partial/coalesced replies and hostile oversized sequences are
   bounded while unrelated terminal input remains ordered.
8. Machine pins: existing plain and JSON twins remain byte-identical.

Gate: this bank green, a deterministic capability-artifact evidence pair
(fully supported fixture and fully unknown fixture), manifest regenerated
last, full discovery clean, push, and architect envelope.

## R3-GFX design — one fact, two expressions

### Activity testimony

`floati/tui_activity.py` converts only records already loaded by a TUI into a
fixed five-bucket event-sequence series per node. A bucket is a count of exact
records naming that node; no wall-clock sample or missing history is invented.
Board, live-map, and replay renderers receive the series as explicit input.
When a surface has no qualifying records, five zero buckets are truthful for
that surface's loaded event window.

`activity_braille(samples)` maps the same five counts to `⣀⣠⣤⣶⣿` with a
visible glyph in 256-color, 16-color, monochrome, `NO_COLOR`, dumb, and plain
surfaces. Color never carries a bucket value.

### Pixel upgrade

`floati/tui_graphics.py` generates deterministic Floati-orange RGBA PNGs using
only `struct`, `zlib`, and `base64`: the existing buoy plus one bounded activity
strip per visible node. Stable, non-colliding image/placement ids derive from
the rendered target identity. The text frame retains the braille series under
the overlay, so loss or rejection of an image never removes testimony.

An overlay is eligible only when:

1. Kitty graphics is `supported` and `MEASURED` in the current
   `TerminalCapabilities` artifact;
2. the renderer reports an exact visible row for that node;
3. rendering policy is not monochrome/`NO_COLOR`.

All eligible overlays are emitted inside the same synchronized-output frame as
their text and cursor placement. Resize or row removal deletes stale placements;
normal exit deletes every image id actually sent. No terminal brand, TERM value,
or explicit "force graphics" flag exists.

The Board and Live Harbor Map own readable input and can run R3-CAP directly.
Replay remains braille unless it is explicitly handed a measured artifact by a
future in-territory caller; its output-only API must not guess support. Static
README/capture renderers remain text and do not acquire terminal controls.

### R3-GFX RED bank

1. Honest series: only exact loaded records contribute; bucket order and node
   identity are deterministic; absent activity is five measured zeros.
2. Tier parity: removing SGR from 256/16 output yields monochrome exactly, and
   `NO_COLOR` selects the same bytes; focus and fault glyphs remain intact.
3. Pixel gate: only a MEASURED supported Kitty observation emits PNG bytes;
   unsupported, unknown, DERIVED, malformed, and terminal-name-only inputs
   emit none and no warning.
4. Pixel fidelity: PNG bytes are deterministic, use Floati brand colors only,
   encode the same five samples as the braille twin, and stay within fixed
   byte/image/count limits.
5. Geometry: only visible node rows get placements; resize/removal deletes
   stale placements; clipped rows never retain invisible hit or image targets.
6. Frame/lifecycle: text, overlays, and cursor are inside one mode-2026 frame;
   every sent image id is deleted even when termios restoration fails.
7. Surface coverage: Board, Live Harbor Map, and replay all render the same
   braille activity law; Board and Live Map use pixel overlays when their
   measured artifact allows it.
8. Gate assets: color and monochrome capture pair re-banked; existing R1/R2/R4
   captures and plain/JSON twins remain byte-identical.

Gate: both RED banks green, protocol hostile-input tests green, capture pair
byte-matched, idle pseudo-terminal comparison shows no new polling/output,
manifest regenerated last, canonical full discovery clean, push, and architect
envelope quoting the fences above.

## Serialization law

Only R3-CAP is authorized from this brief now. After its gate passes, its SHA is
pushed and enveloped. R3-GFX remains nonexistent until the architect lands CAP
to main; the GFX branch then starts from that landed main and begins with a new
RED-only commit. Reusing the branch name is permitted, but stacking GFX on the
ungated CAP candidate is forbidden.
