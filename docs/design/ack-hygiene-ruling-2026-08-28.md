# RULING — ACK HYGIENE (the architect, 2026-08-28; owner-prompted, dogfood-measured)

**The measurement that shaped this:** the owner suspected a seat of not acking; the ledger showed
**231 delivered / 231 acked — perfect seat hygiene** — and yet neither owner nor architect could
SEE it without hand-reading receipt files. The gap is sender-side visibility, not seat
discipline. Floati is opinionated about acks so users never have to be:

1. **An ack means SEEN. Nothing more.** Not agreement, not action, not promise. Disagreement is
   a reply; work is a work receipt. Withholding an ack to signal displeasure is a DEFECT — it
   manufactures ghost attention and poisons the doctor's numbers.
2. **Ack-on-drain, atomically.** The default read path acks exactly what it returns, in the same
   operation. Peeking without acking is the explicit variant (`--peek`), never the accident.
   Tooling makes the right thing the default — a two-step read-then-ack ritual WILL be forgotten
   (build row: batch ack; the managed wrapper's one-id-per-ack shape is friction on the record).
3. **The sender never asks twice: `floati sent`.** A sender-side outbox view derived purely from
   receipts: per envelope — delivered? acked? aged? **No ack-notification envelopes, ever** —
   that doubles traffic to say what receipts already prove. Receipts, not chatter.
4. **Delivered-but-unacked ages into its own doctor RED**, distinct from undelivered: transport
   gap vs attention gap are different diseases. Per-node ack latency is measured; the role
   record's cadence implies the SLA.
5. **Three records stay three records.** Delivery, acknowledgment, consumption never blend —
   the three-lamps law applied to mail.

**Build rows (fold into WS-A ack-loop closing):** A-ack1 `inbox` acks-on-drain by default +
`--peek` · A-ack2 batch ack verb (and managed-wrapper shape) · A-ack3 `floati sent` outbox
status · A-ack4 doctor delivered-not-acked lamp + ack-latency. AGENTS.md (I1) carries rule 1
verbatim — agents especially must never withhold acks.
