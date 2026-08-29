# H2 — send / receive / ack round-trip

**Family:** round-trip. Capture sha256 `8b01eaeff030e4ec3de568c09b25fb28303fccc0e5d0448f1ae9c1d25aada149`.
**Trunk:** `c4dd4a164328f91407e4103562a0e6308d573f73`
**Scratch:** `~/Projects/floati-grok/.gauntlet-scratch/h20260828004028`
**Node:** `grok-h`

This trunk's `send` requires `--doc` (not optional).

## send

exit 0. Envelope `msg-01a045cf99bd705eb30547678f7d1541`. `--sha c4dd4a164328f91407e4103562a0e6308d573f73` `--doc docs/status/WEEKEND_PROGRAM_2026-08-28.md`.

## inbox

exit 0. Delivery `delivery-01a045cf9a0e7cf5af6d32b1a8c27cb4`, `presentation_count=1`.

## ack

exit 0. `ack-01a045cf9a587930b8388ea7dc15066f` for that message id.

## inbox after ack

exit 31, `intentional_silence`.

**Verdict: PASS**
