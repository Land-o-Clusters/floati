# RULING — the daemon buildout is assigned NOW (the architect, 2026-08-28)

Amends `gate-wave2-reconcile-2026-08-28.md` and the design's own closing line.
The gate accepted `wake-daemon-design-2026-08-28.md` "for a later
implementation plan" — **that sequencing is WRONG and is corrected here, on
the owner's word: there is no work left "for later" before launch.** The
gauntlet wake family drills **hook AND daemon** (WS-H), the gauntlet is a
flip gate, and the Cursor ~28-minute wake death has the daemon as its
structural fix. A design accepted but not assigned is the R-row gap wearing
a different coat.

**Assignment: build lane builds the daemon, starting after (or interleaved
with) the two small repair rows from the reconcile gate.** WS-A is their
territory by the program's own table. The verification matrix in the design
doc (6 REDs, 5 GREENs) is the acceptance contract, RED-first.

## The three open inputs, answered so nothing is guessed

1. **Host supervisor (v1, macOS): a launchd user LaunchAgent.** One plist per
   root/node/harness coordinate, label derived from the coordinate digest,
   installed by the installer with a SHA-256 receipt under the existing
   installer discipline, and **removed by the same path — the EXIT DOOR
   ships with it** (uninstall/revoke must delete the plist and prove the
   process gone or say `unknown`). No login-item, no daemon-domain (user
   domain only), no other supervisor in v1. A missing supervisor is a typed
   refusal at `start`, never a silent fallback to an unsupervised process.
2. **Consent/lifecycle schemas: the lane drafts them** as `schemas/v0/`
   documents carrying exactly the fields the design names (node, harness,
   adapter version/digest, min/max polling interval, max backoff, activation
   epoch; lifecycle states from the failure table). the architect gates the schemas
   with the implementation — drafting is not guessing when the field list is
   already ruled in the design.
3. **Per-harness binding/wake contracts: Codex and Cursor only in v1.**
   Codex binds through the already-landed workspace map + registry + Stop
   waiter trust boundary (the adapter is mostly extraction). **Cursor is the
   acceptance harness**: its adapter record is the explicit binding the
   design demands (never the stale registry role label), grok's live seat is
   the first subject, and the >35-minute three-cycle run is the acceptance
   evidence. Every other harness stays typed-absent — unsupported means
   refused, never generic fallback.

## Sequencing into the flip ladder

Daemon lands → gauntlet wake family can close (hook drills run now on the
controller; daemon drills queue behind the landing; **the family may not be
marked green on hook-only evidence**) → captures → README swap → owner flip
ritual. The long pole is the Cursor 35-minute run — it starts the moment the
Cursor adapter exists, not after everything else is polished.

Ships OFF by default with its own consent, exactly as designed — "no work
left before launch" means the capability is built and gauntlet-proven, not
that it is switched on for anyone.
