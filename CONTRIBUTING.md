# Contributing

Floati is young and maintained by one person, so intake is deliberately
narrow. Here is what that means honestly.

## Now

- **Bug reports and questions:** open an issue. A report that includes the
  exact command, the full JSON artifact it printed, and your platform is a joy
  to work on. Security reports go through [SECURITY.md](SECURITY.md), not
  issues.
- **Small fixes** (a wrong help string, a broken link, a real bug with a
  failing test): pull requests welcome.
- **Features:** open an issue first. This project moves on written contracts —
  a feature lands with its tests, its receipts, and its documented refusals,
  which usually means design conversation before code.

## The ground rules

- Tests run with `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover` —
  system Python 3, no pytest, no virtualenv, no dependencies. That is a design
  decision, not an accident.
- `python3 -m floati.selftest` must pass, including manifest verification.
- Every commit needs a DCO sign-off (the Developer Certificate of Origin):
  `git commit -s` adds the `Signed-off-by:` line, and the certificate you are
  signing is <https://developercertificate.org/>.
- New user-visible strings ship with tests that pin their properties, never
  their exact wording. If changing a string changes what a reader understands,
  it is copy; if it changes what the code means, it is an identifier.

## Why the docs folder looks like that

See [docs/CASE-LAW.md](docs/CASE-LAW.md). Short version: the receipts are the
product, so the project keeps its own.
