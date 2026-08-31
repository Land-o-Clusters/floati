# WAVE 2 R2 — deterministic Pi protocol classification

**Status:** IMPLEMENTED — local verification complete.

**Dispatch:** `msg-01a045c7f6a07b8185ab9fcb407d2a9e` from `the architect`.

**Branch:** `lane/ws-b-wave2`, rooted at fetched `origin/main`.

**Implementation commit:** `de5cd39e9bced31c31c2d2bcecdf1bb8f6c28702`.

## Change

Pi now performs a zero-wait read of bytes already ready on its stdout pipe at
deadline expiry. A complete queued frame reaches the existing parser, so a
queued malformed frame remains `protocol_error`; an empty pipe remains
`process_timeout`. The drain does not add a grace wait or reinterpret bytes
that have not arrived.

## RED and GREEN

- RED: `python3 -m unittest -v tests.test_pi_deadline_classification` at the pre-change tree — 1 failure: the fixture-confirmed queued malformed frame was reported as `process_timeout`.
- GREEN: the same command after the change — 1 test, 0 failures, 0 errors.
- GREEN regression: `python3 -m unittest -v tests.test_pi_adapter` — 10 tests, 0 failures, 0 errors.
- GREEN perturbation repetition: `python3 -m unittest tests.test_pi_deadline_classification` — 8 independent runs, each 1/1 OK.
- `git diff --check` — clean before the row receipt.

The fixture uses a subprocess and an explicit marker to establish that the
malformed complete frame was queued before the test expires the read deadline;
it does not depend on scheduler luck. No provider payload is copied into the
receipt or any durable Floati output.

