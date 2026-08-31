# Post-v1 Capabilities and Fencing Evidence

Date: 2026-08-08

Branch: `codex/capabilities-fencing`

Exact base: `c400b5a5b36e4cf1c8916dea637618aec83152dc`

Authority: the architect approved Model A and the exact machine shape in
`msg-019fe3191d1e77658824874a3e02a32a`, including the three amendments recorded
in the design checkpoint: evaluation time is testimony, `policy_replaced`
names the replacing digest, and bounded lock acquisition refuses on timeout.

## RED witnesses

- Policy registry RED: `tests.test_policy.RepositoryPolicyTests.test_registry_is_sorted_finite_and_covers_profiles_and_selectors`
  failed with `policy_fields_invalid` because production did not yet accept
  `capability_registry`.
- Capability grant RED: all four initial grant-lifecycle tests failed because
  `slip.capabilities` and `CapabilityGrantLedger` did not exist.
- Capability binding RED: all four initial snapshot/dispatch tests failed
  because `slip.capability_binding` and `CapabilityBinder` did not exist.

These were observed before their respective production implementations.

## Implemented authority chain

1. `FLOATI.toml` owns one sorted, unique `capability_registry`; profile and
   selector names outside it refuse during policy loading.
2. `capabilities/grants.jsonl` stores strict v1 `capability_grant` and
   `capability_revoked` frames. A grant resolves an exact approved request and
   decision for the same worker, capability, worker scope, authority epoch,
   and bounded expiry. Replay re-joins that evidence, rederives its TTL
   arithmetic and covering authority frames, and permits one physical grant
   per approval decision.
3. `CapabilityBinder` takes the grant lock, projects physical grant state,
   proves complete selector coverage, and while retaining that lock appends
   one exact `capability_set_bound` run frame under the run lock.
   Node identity remains distinct from policy worker-profile identity; the
   selected profile is validated against the unique policy route and rank.
4. A v1 dispatch names that snapshot and exactly repeats its worker,
   attempt/fence-derived identity, policy, route rank, and capability digest.
   The snapshot is consumed once. `attempt_started` uses the accepted dispatch
   and performs no clock or grant re-evaluation.
5. Legacy v0 dispatch history projects as `legacy_unenforced`; it is never
   presented as v1 enforcement.

The capability-set digest is the canonical JSON encoding of the sorted list of
triples `(capability_name, grant_id, physical_position)`. Timestamp testimony,
revocation reason, eligible worker order, and caller-provided required sets do
not enter the digest.

## Measured verification

- `python3 -m unittest -q`: 625 tests, 0 failures, 40.823 seconds.
- `python3 -m slip.selftest`: 625 tests, 0 failures, 41.704 seconds; emitted
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
- Focused capability, policy, schema, and manifest gate: 76 tests, 0 failures,
  2.373 seconds.
- New focused capability suite includes forced short-write rollback for all
  four new durable kinds, a bounded grant-lock timeout, malformed/truncated/
  duplicate/oversized/non-UTF8 grant frames, physical forward revocation
  refusal, hostile and reordered run frames, timestamp-testimony invariance,
  and a twelve-process snapshot-versus-revoke race.
- Durable replay tests reject missing, denied, capability-mismatched, TTL-
  forged, duplicated, or reused approval evidence and rederive covering
  authority frames before returning any effective grant.
- Grant creation retains the authority CAS lock through its append, so a
  release cannot race between authority observation and durable grant truth.
  The audited lock graph is authority-to-grant-to-run with no inverse pair.
  An authority release after a grant requires a physical
  `authority_revoked` capability lifecycle frame for early closure.
- `git diff --check`: exit 0.
- `git diff --name-only -- bundle/c7.1 bundle/c7.2 schemas/v0`: empty. The
  pinned c7.1/c7.2 packages and all v0 schemas were not changed.
- Repository manifest verification: zero errors and `bundle_verified`.

This evidence is local/static and SHA-bound only after the final commit. It
does not claim deployment, activation, release, or any external gate.
