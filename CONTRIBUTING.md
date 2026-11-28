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
  Python 3.9 or newer and unittest, without pytest. The runtime has zero
  third-party dependencies; the full verification suite requires Minisign on
  `PATH`, plus Pillow and jsonschema in the Python environment running tests.
- `python3 -m floati.selftest` must pass, including manifest verification.
- Every invited commit needs a DCO sign-off (the Developer Certificate of
  Origin): `git commit -s` adds the `Signed-off-by:` line, and the certificate
  you are signing is <https://developercertificate.org/>.
- New user-visible strings ship with tests that pin their properties, never
  their exact wording. If changing a string changes what a reader understands,
  it is copy; if it changes what the code means, it is an identifier.

Install verification tools before running the suite. On macOS, install Minisign
with `brew install minisign`; on Debian/Ubuntu, use `sudo apt-get install minisign`.
Install the Python verification packages with `python3 -m pip install pillow jsonschema`
in your chosen development environment. These tools support signature, image and
schema checks; they are not required for the basic install and solo work-log flow.

### T3 worktrees on the Harbor Mac

Run `scripts/agent-bootstrap.sh` from the checkout. It creates a worktree-local
`.venv` from `scripts/agent-test-requirements.txt`. Repository setup takes no
packages or build output from the primary checkout. The host needs Python
matching `pyproject.toml`, Node 26 for the OpenCode bridge tests, Git, and
Minisign. The bridge uses Node's built-ins and requires no npm install.

Run `scripts/agent-bootstrap.sh doctor` (the `doctor` Make target) to check the
test tools, free disk space, and installed Codex project-job profile. The
gateway uses its registered interpreter and tools. Doctor reports the exact
registry field and canonical Homebrew replacement when an upgrade moves a
pinned executable.

For focused local checks use the virtual environment's Python. For the two
managed batteries use the gateway from the worktree root.

```sh
.venv/bin/python -B -m unittest tests.test_managed_test_tools tests.test_opencode_host_bridge
~/.codex/bin/codex-project-job floati-harbor selftest
~/.codex/bin/codex-project-job floati-harbor unittest
```

Register this shell command as T3's `runOnWorktreeCreate` in the project.
It skips branches predating bootstrap and leaves a worktree open for diagnosis
if setup fails.

```sh
if [ -f scripts/agent-bootstrap.sh ]; then
    scripts/agent-bootstrap.sh || printf '%s\n' 'Floati bootstrap failed. Run scripts/agent-bootstrap.sh doctor.' >&2
fi
exit 0
```

## Why the docs folder looks like that

See [docs/CASE-LAW.md](docs/CASE-LAW.md). Short version: the receipts are the
product, so the project keeps its own.
