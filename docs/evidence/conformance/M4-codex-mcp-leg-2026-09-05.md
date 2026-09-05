# M4 — Codex MCP leg, 2026-09-05

**PARTIAL: five operational rows observed; doctor health unmeasured.** This real
Codex session drove an installed Floati build through newline-delimited JSON-RPC
over child-process stdin/stdout. This is the same shell-driver method as the
Claude head leg, not a claim that a native Codex MCP registration was installed.
No product fix is included. The architect must adjudicate the findings and the
missing doctor observation before closing M4.

Source and install: `1a17f706f5ece1cc3a3d0e5efa4a05b85bb5aade`, the fetched
main at boot. Kept RED: `10e53cf47d842b7b0b41100b6bb629ac4f453f18`;
`docs/evidence/m4-codex-red-2026-09-05.md` records the failing absence check.
The install used an exact detached scratch clone and `--committed-tree`:
its mode was `committed-tree-ci`, not a named-ref currency assertion. The
installed manifest independently records that source SHA.

All measurement writes were beneath `<temp>/floati-m4-codex-20260905`.
Two fixture identities, `operator` and `peer`, were registered as Codex. Manual
CLI setup installed and provisioned the scratch fleet; operational requests used
MCP only. The authority role/grant was a manual setup act through existing CLI
verbs. No new authority or consent surface was introduced.

## Six operational rows

| Row | Observation | Verbatim coordinate |
|---|---|---|
| status | `ok`; initially one open work item, finally one completed item and zero open/claimed | `surface.wire.jsonl#status`, `work.wire.jsonl#final-status` |
| send | `ok`, sender `operator`; repeated caller key returned the exact same envelope; readiness explicitly `recipient_not_listening` | `operator.wire.jsonl#send`, `#send-repeat` |
| inbox | One message, one delivery receipt and one acknowledgment; next drain `intentional_silence` with root and node present | `peer.wire.jsonl#inbox`, `#inbox-empty` |
| ack | Before delivery: `ack_item_not_delivered`; after inbox: exact existing acknowledgment returned, bound to `codex-m4-peer` | `peer.wire.jsonl#ack-before-delivery`, `#ack` |
| work | Explicit authority coordinate: claim and complete both `ok`, actor `peer`, item completed; omitted coordinate refused | `peer.wire.jsonl#claim-owner-no-authority`, `work.wire.jsonl#work-claim`, `#work-complete` |
| doctor | Only the invalid-profile boundary was executed: `doctor_profile_invalid`; valid health execution was held for the scope finding below | `operator.wire.jsonl#doctor-scope-refusal` |

The six rows above name operations; “six-rows Am.2” is the prior drill findings
ruling, not six assertions of success. No doctor PASS is implied by successfully
capturing its refusal.

## Surface, identity and refusal checks

The wire described **131 commands** and listed **21 tools: 12 read, 9 governed**.
The exposure-tagged executable paths and tool command paths agree exactly.
`confluence_grant`, `confluence_revoke` and `inbox` are now `governed` on the
wire (the Claude head leg observed the earlier classification). Grant/revoke
operations themselves were not exercised over MCP in this leg.

All 16 explicit denied-tool probes returned JSON-RPC `-32602`; the tested names
and exact replies are in `operator.wire.jsonl`. Malformed JSON returned `-32700`,
an unknown method returned `-32601`. This does not imply every denied verb was
called. Actor, root and session override attempts returned `arguments_invalid`.
The 1025-character note refused with `note_invalid`; the successful send did not
change sender after the attempted override.

**MCP-1:** the owner in the two-node fleet still receives
`solo_identity_ambiguous` when authority arguments are omitted. Supplying the
existing `authority_subject` and `authority_epoch` makes claim and completion
succeed with the pinned actor. This distinguishes omitted-authority defaulting
from the old finding that the actor could never claim even with authority.

**MCP-2 / REM-1:** missing send inputs now name MCP input names and provide a
remedy. However the note-cap remedy still says `shorten --note ...`, and the
omitted-authority remedy says `pass --as ...` although `actor` is not an MCP
input. These are observed CLI vocabulary leaks, not a new code change. Of 11
tool refusals, 7 carried string remedies and 4 carried typed `{kind: none, why}`
objects; none were null. `confluence_grant_required` still puts the CLI grant
instruction in `detail` and reports no action in `remedy`.

## WIRE-3 pin and transport

Every initialize response was retained with its `floatiIntegrationPin`.
`validate_mcp_observation` accepted the observed pin. All 21 pin tool schemas
and descriptions equal their served tools/list counterparts. Recorded posture:
`transport: stdio`, `network_posture: none`; executable/config digests are null.
The pin's command begins at the installed `floati/__main__.py`; the driver also
records the actual `/usr/bin/python3 -B -m floati ...` process argv separately.
These are different coordinates; the pin is not proof of interpreter identity.

Four MCP server processes were spawned: 88585, 92796, 93757, 94914. Each was
closed by stdin EOF and reaped with exit 0, no trailing stdout and empty stderr.
Each idle IPv4/IPv6 `lsof` sample returned exit 1, empty stdout/stderr. **This is
not proof of zero network activity:** no positive socket control, syscall trace,
Unix-domain enumeration, or continuous descendant sampler ran. No listener was
created by the measurement driver, and no network path was added.

## Doctor scope finding and limits

On the measured source, `floati/doctor.py` calls
`project_installed_bridge_currency` at line 1172 and
`project_installed_bus_watch_currency` at line 1322. Their helpers resolve
ambient per-user hook installation paths (lines 322 and 558), independently of
the explicit scratch root and explicit Codex hook arguments. The registered
profiles are `bus-only` and `orchestration`; neither supplies a complete
scratch-only host-path declaration. Running the valid health check would inspect
ambient hook installations outside this row's scratch scope. That is a static
finding, **not an observed foreign read**: execution was held before those reads.

The `scratch-isolated` profile probe intentionally demonstrated the typed
unsupported-profile refusal; it did not isolate doctor, and is not a substitute
for the missing health result. No profile, product code, harness setting or
environment home binding was changed to manufacture a health result.

Other limits: one host, sequential clients, no concurrent writers, no lease
expiry or retirement exercise, no native harness MCP registration, no daemon or
wake exercise. No host suite, managed battery, or load measurement ran. Production
files, bundle manifest, schemas, workflows, public remote and other workspaces
were untouched. The existing local boot edit was preserved and excluded.

## Verbatim bank and verification

All referenced files live in [m4-codex-20260905](m4-codex-20260905/).
`*.wire.jsonl` wraps each exact request/response string in a `raw` field; decoding
that field recovers the bytes without copy edits. `sha256.json` records sizes
and digests of 20 verbatim copied files, including the installed manifest,
manual setup stdout/stderr, request batches, driver and verification program.
No capture was retouched or redacted after execution. The initial install's
terminal stdout is not in this bank; source identity is backed by its retained
installed manifest, not a reconstructed install output.

`verification.json` records 45 JSON-RPC responses, 22 tool artifacts and the
observed counts. The check validates the pin, exposure correspondence, exact
structured/text artifact equivalence, send deduplication, delivery/ack binding,
denied tools, actor-switch refusals, work completion and clean process exits.
These assertions passed; they deliberately do not claim doctor health or
absence of transient sockets. The RED absence check must become green only
because this dated evidence entry now exists; that is evidence presence, not
conformance completion.
