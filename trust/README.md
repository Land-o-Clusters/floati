# Floati release trust

The Floati project release key is live.

- **Key id:** `A9FBAB3833B4D4EF`
- **Public key:** [`floati-release.pub`](floati-release.pub) · metadata and validity:
  [`keys.json`](keys.json)
- **Provisioned:** 2026-08-29, by the project owner, per
  `docs/runbooks/v2-key-ceremony-2026-08-29.md`. The secret key is passphrase-encrypted
  and owner-held. No agent, lane, CI job, or repository has ever contained it.
- **Ceremony status:** COMPLETE (2026-08-29). Self-test verified by the architect from
  the owner's transcript; encrypted off-machine backup made and byte-verified against the
  self-tested original. Backup locations are deliberately not published here.

**Pin the key, do not fetch it.** Record the key id and the key bytes above the first
time you verify; refuse a changed key until a signed `key-transition-*.json` in this
directory explains it. Verification proves authenticity and append-only continuity up to
the newest trusted checkpoint — it cannot prove freshness.

Signatures are Minisign detached `.minisig` files; the trusted comment binds filename,
version, and (for checkpoints) journal id and sequence. OpenSSH is documented only for
hash-checking a Minisign-authenticated release manifest — there is no second signature
format or custody path.

The public fixture under `tests/fixtures/minisign/` authenticates test bytes only. Its
deliberately public secret key must never be copied here or used for a release.
