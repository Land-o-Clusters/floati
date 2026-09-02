# Codex app-server recorded-shape fixtures

Recorded on 2026-07-31 from the closed-world contract in the sibling Puddle
repository:

- `PHASE5_CODEX_APPSERVER_RULING_REQUEST.md` §2 records the original three
  outbound messages and the `initialize` request shape.
- `PHASE5_CODEX_APPSERVER_SEQUENCE_RULING_REQUEST.md` later amends that
  provider-specific outbound sequence to four messages.
- `Sources/PuddleCore/CodexAppServerAdapter.swift` records the current
  request/response/notification envelope encoding used by Puddle.

These fixtures lock only the three envelope *categories* required by the HM-1
brief: request, response, and notification. They do not authorize an outbound
method sequence, process launch, network request, credential read, approval,
or worker action. The sample `clientInfo.name` is provenance, not a Floati
runtime identity.
