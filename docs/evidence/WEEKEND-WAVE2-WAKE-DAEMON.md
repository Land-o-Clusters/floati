# Weekend Wave 2 wake-daemon buildout evidence

Date: 2026-08-28

Branch: `integrate/wake-daemon-20260828`

Merged baseline: `origin/main@0c8c0be96f6e68acb78083e7209b3fb8b82864b2`

Automated-candidate merge: `3fa0cd9454dff97ed5fd4ce842e2951fc3662f8c`

Final current-main refresh before candidate verification:
`77bcfce6a6e6ee64250f9009e5b0bbf8afeec80f`

## Scope and fences

This row implements the owner-ratified opt-in wake daemon for the exact
`root/node/harness/session` coordinate. It adds closed consent, lifecycle, and
adapter-binding records; fixed Codex and Cursor adapters; the bounded polling
engine; a deterministic macOS user LaunchAgent lifecycle; public DRAFT-stamped
operator commands; and a hidden plist-only `serve` entry point.

The row adds no listener, discovery, global pause, arbitrary command, alternate
supervisor root, hook mutation, fallback adapter, delivery acknowledgment,
README edit, flip, release, or publication action. No fenced foreign-project
artifact or foreign bus root was written.

## Integration ledger

- `386c02c` — closed wake-daemon contracts and three v0 schemas.
- `27f882a` — digest-bound Codex queue and Cursor resume adapters.
- `351bae4` — bounded owner-locked engine, durable attempt replay, budget,
  breaker, pause, and revocation behavior.
- `824f6f7` — deterministic LaunchAgent install/start/status/stop/remove/revoke.
- `4adeeb3` — DRAFT public CLI/help/copy surfaces and hidden `serve`.
- `3fa0cd9` — merged current `origin/main`, retained main's the architect restamp on
  existing surfaces, retained the ordered DRAFT fence on the new daemon copy,
  and updated `docs/TRUTH-GUARANTEES.md` for the standing no-listener fence.

## RED-first receipts

- Contract tests preceded the contract module, schemas, and record validators;
  the missing imports/contracts failed before implementation.
- Adapter tests preceded the adapter module. Exact Codex `queue --thread ...
  --message ...` and Cursor `--print --output-format json --single-turn
  --resume ...` vectors were then mutation-probed: changing Codex `--thread`
  to `resume` and changing the Cursor session operand to `-1` each failed the
  pinned test before restoration.
- Engine tests preceded the engine module and pinned no-consent, exact owner,
  malformed-pause, durable-evidence, restart, backoff, breaker, and wake-budget
  behavior before implementation.
- LaunchAgent tests preceded the supervisor module and pinned deterministic
  plist bytes, no launchctl on install, fixed user-domain vectors, and
  digest/inode-safe removal before implementation.
- The CLI RED bank ran 33 tests and failed exactly five expectations: four
  daemon operations returned `arguments_invalid`, and the nine daemon help
  entries were absent from generated copy. Existing manifest tests stayed
  green in that pre-implementation run.

## Automated GREEN receipts

- Task 5 CLI/copy/manifest/name/source bank: **54 tests, OK**, 0.756 seconds.
- Post-merge integration bank including daemon, no-listener, copy, manifest,
  and concurrent admin tests: **73 tests, OK**, 3.006 seconds.
- Plan-exact wake-daemon matrix: **37 tests, OK**, 0.422 seconds.
- Full discovery on the merged tree: **1,868 tests, OK**, 198.849 seconds,
  process exit 0. Refusal-path diagnostics and existing `ResourceWarning`
  lines appeared on stderr; the authoritative unittest verdict was `OK`.
- After the final current-main documentation merge, the first full discovery
  ran **1,868 tests in 193.542 seconds and FAILED with one failure**: the
  tracked evidence document itself contained the prohibited foreign-project
  token. That run is not counted as passed.
- The evidence wording was repaired; source-scrub/name-sweep ran **21 tests,
  OK** in 0.528 seconds, then final full discovery ran **1,868 tests, OK** in
  220.149 seconds with process exit 0.
- After the organic run exposed and pinned the launch argv, lane-level
  namespace, and Cursor result-shape repairs, the merged-current-main focused
  wake/scrub/name bank ran **61 tests, OK** in 1.152 seconds.
- After the `77bcfce` main refresh and first final manifest regeneration,
  canonical full discovery ran **1,899 tests, OK** in 205.778 seconds with
  process exit 0. Expected refusal-path diagnostics and existing
  `ResourceWarning` lines appeared on stderr; the authoritative verdict was
  `OK`.

The whole-product fence confines network-capable imports, allows socket imports
only in ruled local transports, and permits `bind`/`listen` only in the AF_UNIX
sequencer. Dedicated herdr tests retain the literal-loopback and no-DNS pins.

## Live Cursor longevity gate: passed organically

The earlier workspace-trust refusal was resolved through the harness's own
trust surface; no trust-bypass argument was added to the adapter. The accepted
binding used Cursor session `c124e3f8-cc2d-4331-9bcd-bc45221d0577`, session
digest `7f754dda8d84b2a5f9a0af0122bdf40764b8e5b807e61f00a4d5cbd4d1d82a62`,
adapter digest
`254a9e3c654b2f72bf323af95a77ec610888e41ac491eeffa9ab86d782f778a6`,
and executable version `2026.07.09-a3815c0` with SHA-256
`eed61c5224668c9236334c4c68936a16aecc37374b592f59e31eb50433817831`.

Live acceptance exposed three defects that the earlier fake-runner coverage
did not reveal. Each received a real regression test before its repair:

- The LaunchAgent plist passed the shell launcher to `/usr/bin/python3`, so
  launchd produced a shell-syntax `SyntaxError`. The plist now executes the
  installed launcher directly, and the test executes the generated argv.
- The daemon queried only the exact-session message namespace, while live
  lane envelopes correctly carry an unbound `worker_session_id`. The daemon
  now checks exact-session work first, then lane-level work, and records the
  actual message namespace (`null`) independently from the bound acting
  session.
- Cursor success had been defined as any JSON object after exit zero. The
  adapter now requires `type=result`, `subtype=success`, `is_error=false`, and
  an exact match to the bound session ID. Wrong-session, error-shaped, and
  arbitrary JSON objects are perturbation-pinned refusals.

Preliminary activation attempts are retained but excluded from the final
claim. A2 produced hold
`wake-hold-01a0467ee5587ef5a4ba057e164c420f`, then timed out after 300 seconds;
attempt `wake-attempt-01a046837bd17c9889fe76ea23fc16e9` is durably `refused`
with `wake_prompt_failed`, and no wake is claimed. Activation `1787890021`
proved the corrected launch path, but was superseded when the success parser
was hardened. Only activation `1787890328` counts below.

### Final activation identity

- Consent: `wake-daemon-consent-01a0469180077382aba909c89c76ad0a`.
- Installed plist SHA-256:
  `f0d622fbbb085c0ec3e5ae80c39a5544c729f29739296981415902db3e2dc58e`.
- Install/start lifecycle receipts:
  `wake-daemon-lifecycle-01a04691949c7958a7cfe1f637bd4fbe` and
  `wake-daemon-lifecycle-01a0469255bc7856b073354c396e1392`.
- Daemon instance: `daemon-01a0469257a87b0c851773afa5ac2551`.
- Coordinate digest:
  `890944d42bc6e499307301a14f1e02f61162e3867626fdc3745e51f7226a7b46`.
- Schedule: 720-second minimum/maximum poll, 1,440-second maximum backoff,
  closed circuit, zero consecutive refusals after the final wake.

### Organic cycle ledger

1. The final epoch started at 04:13:10.972Z. A4
   `msg-01a0469217b278a8956f1bc72fd51fa3` was held by
   `wake-hold-01a0469262a671d9b58f82a1171f47b8` and woke the exact bound
   session through `wake-attempt-01a046935ec970e194b61e17fdf67c14` at
   04:14:18.313Z.
2. The exact session was paused by
   `wake-control-01a04693a48f75958b0a267dea8f9aa1`. The next deadline recorded
   `paused` in `wake-daemon-lifecycle-01a0469d55d97e00a18f4f13ffc8a696`
   with no new wake attempt. While paused, a stop/status/start drill proved a
   stopped supervisor and restarted the same coordinate through
   `wake-daemon-lifecycle-01a0469e391c73b983068311503c7450`; exact resume is
   `wake-control-01a0469e55d5720c9fcd214b8f743f62`.
3. The paused payload B was delivered and acknowledged by another consumer
   19 seconds before the resumed daemon boundary. The daemon therefore
   recorded honest `idle` lifecycle
   `wake-daemon-lifecycle-01a046a85ed9787da6f2b76cde50b343`; this is not
   counted as a wake.
4. C `msg-01a046b348a7700b8701b5b1f6dcfe15` arrived 1.327 seconds before its
   deadline, was held by `wake-hold-01a046b359687b4b9a9b91b271230372`, and
   woke the exact bound session through
   `wake-attempt-01a046b673dd74e799b3722e0afaf908` at 04:52:37.469Z.
5. D `msg-01a046be452675638f67efe89b9d0bb7` arrived 1.393 seconds before its
   deadline, was held by `wake-hold-01a046be56bd7888a50c68c02b359558`, and
   woke the exact bound session through
   `wake-attempt-01a046bf10247edbbeced5fba0d1a352` at 05:02:01.764Z. Its
   lifecycle receipt is `wake-daemon-lifecycle-01a046bf102d7ed5956ca65d1fab6a18`.

The accepted epoch therefore spans **48 minutes 50.792 seconds**, contains
three real successful wake receipts (A4, C, and D), one exact paused cycle,
one honest externally-consumed idle cycle, and one supervisor restart with
preserved activation/session/coordinate identity. No synthetic wake row,
manual waiter invocation, or fake-runner receipt is included.

### Cursor stop-hook disarm

During acceptance only, the Grok-specific empty Floati waiter marker was moved
to the inert quarantine path
`grok.cursor-wake.enable.acceptance-disarmed-20260828T0402Z`, preserving its
SHA-256
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
After D passed, the exact Floati stop-hook entry was permanently removed from
the Cursor hook configuration. The configuration SHA-256 changed from
`9c1ab173b027b41153cfc95a205e89184fb8c045c111f5e6a9e2f7e7fd350882` to
`69b2efe6f36a7ca321af5e4b4e01c2687336fc39964465401f50bf817f47e859`,
and the stop-hook count changed from four to three. The incumbent bus waiter
`~/.cursor/hooks/grok_bus_stop.py` and every unrelated hook
remain present. The separately ruled iOS reproduction is post-release and was
not run or claimed here.

## Candidate closure

The manifest is regenerated after this evidence document and verified against
the exact deployable tree. The pushed commit and manifest digest are bound in
the architect envelope; that envelope does not claim the gauntlet wake family,
flip, release, or publication complete.

## the architect GATE: ACCEPTANCE + REPAIRS + DISARM — PASS AND MERGED (2026-08-28 morning)

The strongest verification position of the program: I couriered every drill
envelope and watched every receipt land live, and the delivered evidence
names exactly those ids — A4/C/D wakes, the pause lifecycle, the restart
lifecycle, the hooks digests 9c1ab173→69b2efe6. Independently re-derived at
the combined landing (this branch + restamp wave 2): **1,899 tests, OK,
exit 0 (pipestatus-captured), frozen tree**; manifest exact; the disarm
verified on the live machine this morning (3 stop hooks, incumbent
preserved, quarantined marker inert). The run's two live repairs (lane-level
namespace query; prompt-confirmation-is-the-result parsing) shipped with
their RED-first banks. **THE WAKE FAMILY'S DAEMON HALF IS GREEN.** Remaining
for the family: grok's per-surface drills against the production daemon.
