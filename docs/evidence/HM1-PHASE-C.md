# HM-1 Phase C evidence

Status date: 2026-07-31.

This ledger records the local Harbor Board and synthetic-fleet checkpoint. It
does not claim Fable live polish, hosted CI, deployment, activation, or push.

## Identity

- Branch: `lane/hm0`.
- Phase B evidence tip: `321124ad1db581239c014d7050be5a3f648f36fe`.
- Board implementation: `8bea0e1`.
- Synthetic-fleet implementation: `057f0a1`.
- Evidence-binding commit: not predicted by this document.

## RED-first ledger

The first Phase C focused run collected eight errors because `slip.tui` and
`slip.tui_render` did not exist. The implemented board then passed eight
focused rendering and control tests.

The demo-first run subsequently failed because `slip.demo`, the `make demo`
target, the board CLI/help surface, and state-signature seam were absent. The
first deterministic capture test also exposed unstable receipt ordering when
timestamps tied; an explicit kind tie-break fixed it. A later keyboard audit
found that `a` had no durable action seam; a failing regression was added
before wiring it to the normal acknowledgment core.

Final focused command:

```sh
python3 -m unittest -v tests.test_tui_render tests.test_tui_controls tests.test_demo tests.test_copy_ledger tests.test_cli tests.test_projection
```

Observed: exit 0; 33 tests ran; `OK`.

## Instrument evidence

- Redraw/input ceiling: `REDRAW_INTERVAL_SECONDS = 0.25`, tested as no greater
  than 250 ms.
- A 1,000-frame local rendering sample measured median 0.011 ms, p95 0.013 ms,
  and maximum 0.043 ms. This is local render timing, not deployment evidence.
- `make demo-capture` regenerated deterministic 100-by-30 color and
  monochrome fixtures at `docs/evidence/captures/hm1-tui-color.txt` and
  `docs/evidence/captures/hm1-tui-monochrome.txt`.
- The color fixture contains ANSI buoy orange (index 208) and harbor slate
  (index 236). The monochrome fixture carries the same plane words, alerts,
  work states, receipt kinds, selection, and key legend without ANSI.

## Complete local gate

Fresh gate commands after the manifest, design, README, evidence, ruling
request, and captures were present:

```sh
python3 -m slip.selftest
python3 -m slip.conformance --live-root-smoke
python3 -c 'from pathlib import Path; from slip.scrub import scan_generated_tree; hits=scan_generated_tree(Path.cwd()); print("scrub_hits="+str(len(hits))); raise SystemExit(bool(hits))'
python3 -m unittest -v tests.test_copy_ledger
make demo-capture
python3 -m slip.demo --capture monochrome > /tmp/floati-hm1-mono.txt
cmp docs/evidence/captures/hm1-tui-monochrome.txt /tmp/floati-hm1-mono.txt
git diff --check
```

The first complete run stopped RED after 146 tests because the manifest held a
stale committed digest for `slip/copy.py`. Only the measured digest was
corrected. The complete command was then restarted from the beginning.

Observed on the restart: selftest exit 0, 146 tests, `OK`, then
`bundle_verified` naming `refs/heads/lane/hm0`; smoke exit 0 with five
conformant cases; scrub exit 0 with zero hits; copy-ledger focused check exit 0
with two tests; regenerated monochrome capture compared byte-for-byte equal;
diff check exit 0 with no output.

## Boundaries

- Board model, rendering, keyboard path, durable visible ack, and synthetic
  fleet: locally executed.
- Fable voice approval and live visual-polish session: pending and unobserved.
- Bare `slip` remains intentionally unbound: choosing a hidden default root or
  showing synthetic evidence would violate the explicit-root and honest-state
  laws. The operator surfaces are `slip board --root`, `slip board --demo`,
  and `make demo` pending a governed root-selection ruling.
- Hosted CI, deployment, activation, push, and local/origin parity: unobserved.
