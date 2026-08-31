# WAVE 2 R3 — bounded Herdr loopback observation client

**Status:** IMPLEMENTED — DRAFT evidence for architect restamp; live surface remains unclaimed.

**Dispatch:** `msg-01a045c7f6a07b8185ab9fcb407d2a9e` from `the architect`.

**Branch:** `lane/ws-b-wave2`, rooted at fetched `origin/main`
(`eb24ca58a2da69618f28d190f59fc7cab73131f5`).

**Implementation commits:** `e112c95b084f51d5d0f7c9c760748097c700a184`
(initial R3 client) and `62fffe882552f19e78cc1e4a4a1f70f4c13bc4d1`
(review hardening and deployable manifest refresh).

## Change

`floati/adapters/herdr.py` adds a dark, observation-only client. It is not a
WorkerAdapter, is not in the public CLI or roster, launches no process, opens
no listener, performs no discovery or scan, performs one bounded pull, and
returns only a closed, content-free observation result. Tests use injected
fixture sockets; no Herdr binary or live harness surface was exercised.

The ratified loopback conditions are enforced as follows:

1. Only literal `127.0.0.1` and `::1` are accepted. The client selects the
   socket family directly and never resolves a hostname.
2. `HerdrTargetRegistration` is a required explicit capability bound to the
   exact host, port, and pane. There is no default-true registration claim, no
   port scan, and the adapter configuration is immutable after construction.
3. `arm()` and `disarm()` each write a consent receipt. Arming snapshots the
   target, and observation refuses if that bound target changes.
4. A configured environment key or absolute regular file path is read only as
   a pass-through handle. The token is sent to the harness request but is not
   placed in results, receipts, or durable output; receipts record only that a
   source was configured.
5. Connection attempt, connected, and terminal outcome phases are receipted
   with metadata and fixed reason codes. Harness frames are never mirrored to
   the receipt sink.

The pull now carries one monotonic deadline through connect, send, and every
frame read. A peer that drips bytes cannot extend the observation by resetting
the per-read socket timeout.

## RED and GREEN

- Initial R3 RED: `python3 -m unittest -v tests.test_herdr_client_adapter` at
  the pre-client tree — import failure because the new module did not exist.
- Review-hardening RED: the new registration/deadline/immutability assertions
  initially failed to import `HerdrTargetRegistration` before its implementation.
- Review-hardening GREEN: `python3 -m unittest -v tests.test_herdr_client_adapter`
  — 12 tests, 0 failures, 0 errors.
- Combined row GREEN:
  `python3 -m unittest -v tests.test_herdr_client_adapter tests.test_headless_invocations tests.test_pi_deadline_classification tests.test_pi_adapter tests.test_roster_adapters tests.test_roster_parity_battery tests.test_manifest`
  — 73 tests, 0 failures, 0 errors. Existing roster parity tests emitted
  `ResourceWarning` messages but remained green.
- Source/history scrub:
  `python3 -m unittest -v tests.test_source_scrub` — 8 tests, 0 failures,
  0 errors.
- Manifest: mechanical refresh of `bundle-manifest.v0.json`; direct
  `verify_manifest(Path.cwd())` returned `[]`, and `tests.test_manifest` ran
  25 tests with 0 failures and 0 errors.
- Full discovery:
  `python3 -m unittest discover -s tests` — 1706 tests in 206.704 seconds,
  0 failures and 2 errors. Both errors are unrelated uninstall tests whose
  tombstone path is hard-coded under `~`, outside this
  managed writable workspace; the same run printed host sandbox
  initialization notices. No changed-row or manifest assertion failed.

## Independent review

The exact diff from `5fabdb9eac473747147e7a817babc82678186e00` through the
implementation was reviewed read-only. No Critical findings were reported.
Three Important findings were fixed in `62fffe8`: the whole-pull absolute
deadline, target binding through an immutable configuration and arm snapshot,
and the required explicit registration capability. The focused R3 and adapter
regressions were rerun after those fixes.

`floati/cli.py` has no delta. `surface_verified` remains false: this is
fixture-only protocol and boundary evidence, not live provider or harness
verification.
