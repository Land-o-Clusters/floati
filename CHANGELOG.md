# Changelog

All notable changes to Floati are recorded here, by hand. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow the rules in
[RELEASING.md](RELEASING.md). Every release below names its receipt.

## [0.1.0] — 2026-08-31

The first public release. Everything here exists because a fleet of coding
agents has been running on it daily — the receipts in `docs/evidence/` are
that fleet's own records.

### Added

- **The bus.** A local, append-only message ledger with separate envelope,
  delivery, and acknowledgment receipts — an append is not a delivery, and a
  delivery is not an acknowledgment. Whole-fleet readers survive records they
  do not recognize: a newer writer cannot blind an older reader.
- **Nodes, roles, and authority.** Registration, six shipped role templates,
  temporary nodes with leases, and architect-gated work authority: exact
  (holder, subject, epoch) grants with revocation that costs no more than the
  grant. Refusals name the coordinate they looked for.
- **Wake.** A consented Stop-hook waiter for Codex sessions with one armed
  acting session per workspace, receipted wakes, and honest trust reporting:
  when your harness un-trusts a modified hook — as it should — the installer
  says `untrusted_pending_user` and tells you the one step to fix it. It
  never trusts itself.
- **Context and Tide.** Read-only context status and turnover receipts, and
  tide tables: thresholds over measurable facts only. A policy that names a
  metric without a shipped evaluator is refused with the citation in the
  refusal.
- **Purge.** Explicit preserved-root sanitation through the macOS Trash,
  with digest-bound receipts for every file moved. There is no delete
  primitive in the module — proving otherwise is a security report.
- **Lifecycle round-trip.** Manifest-exact install, update, and uninstall:
  only owned files, foreign files preserved, and a permanent guard that every
  owned path stays uninstallable. Releases are verifiable on disk with
  `install --ref` and `doctor`.
- **The TUI.** Harbor board, live harbor map, and a flight-recorder replay —
  plain-first, keyboard-primary, honest in every color tier, `NO_COLOR`
  respected as policy. Every screenshot and recording in the README is a real
  capture of a real fleet. Each capture directory ships a `manifest.json`
  with a per-file SHA-256. The three diagrams are drawings, and they are the
  only images here that are not captures.
- **Truth guarantees.** `docs/TRUTH-GUARANTEES.md` wires every promise to the
  test or receipt that enforces it, including the promises about what Floati
  refuses to do. `tests/test_no_listener_fence.py` structurally enforces that
  nothing listens and nothing phones home.

### Release receipt

Recorded in the tagged release notes at publication: bundle manifest SHA-256,
full-suite result and platform, and the conformance receipts behind the
README's harness table.
