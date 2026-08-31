# Contributing

Floati is maintained by one person, so intake is narrow on purpose. Here is
what that means honestly.

## Now

- **Bug reports and questions:** open an issue. A report that includes the
  exact command, the full JSON artifact it printed, and your platform is the
  most useful thing you can send. Security reports go through
  [SECURITY.md](SECURITY.md), never a public issue.
- **Pull requests: by invitation.** An unsolicited one will sit unreviewed, and
  that is worse for you than a plain no. Raise an issue first; you will get a
  scope and a review path, or a straight refusal.
- **Features:** open an issue. This project moves on written contracts — a
  feature lands with its tests, its receipts, and its documented refusals,
  which usually means a design conversation before code.

This is where the project is today, not a permanent policy. Review capacity is
the constraint; when there is more of it, this page changes and says so.

## The ground rules

- Tests run with `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover` —
  system Python 3, no pytest, no virtualenv, no dependencies. That is a design
  decision, not an accident.
- `python3 -m floati.selftest` must pass, including manifest verification.
- Every invited commit needs a DCO sign-off (the Developer Certificate of
  Origin): `git commit -s` adds the `Signed-off-by:` line, and the certificate
  you are signing is <https://developercertificate.org/>.
- New user-visible strings ship with tests that pin their properties, never
  their exact wording. If changing a string changes what a reader understands,
  it is copy; if it changes what the code means, it is an identifier.

## Why the docs folder looks like that

See [docs/CASE-LAW.md](docs/CASE-LAW.md). Short version: the receipts are the
product, so the project keeps its own.
