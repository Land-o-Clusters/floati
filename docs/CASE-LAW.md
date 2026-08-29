# Why this repo has case law

You will find several hundred documents in `docs/evidence/` and
`docs/design/` written in an unusual register: verdicts, rulings, gates,
corrections, some of them loud. Here is what they are.

Floati's claim is that a fleet's history should be reconstructable from
receipts rather than trusted from memory. We build the product the same way.
Every landed change was gated by an independent re-run in a clean checkout;
every finding — including the embarrassing ones, including the ones where the
project's own README overclaimed and got caught — is filed with the exact
commands, exit codes, and hashes that witnessed it. Rulings that shaped the
product (why the loopback client refuses certain sockets, why approvals can
never be granted by an environment variable, why uninstall shows you the
manifest it will honor) are written down with their reasoning, so a
contributor can learn the law of the codebase instead of guessing it.

Two things follow. First, if a claim anywhere in this repository seems too
confident, pull its thread — it cites the instrument that measured it, and
the instrument is in the tree. Second, these documents are working records,
not marketing. They were written by and for the people and agents building
the product, and they are published because hiding your receipts while
selling receipts would be a strange look.
