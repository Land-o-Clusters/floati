# Security

Floati's pitch is trust. Report anything that breaks it.

## Reporting

Use GitHub's private vulnerability reporting on this repository ("Report a
vulnerability" under the Security tab). That channel reaches the maintainer
privately and keeps the report out of public issues until a fix exists. If
you cannot use GitHub, mail floati@landoclusters.com.

Floati is maintained by one person. You will get an acknowledgment within a
week, usually much sooner. There is no bounty program.

## What counts

Anything that falsifies a receipt, and anything that moves data it should not:

- A way to make a ledger record claim something that did not happen, or to
  alter one after it was appended.
- A way to make Floati touch the network. The no-listener fence is enforced by
  `tests/test_no_listener_fence.py`; a hole in that fence is a critical report.
- A way to make `purge` delete instead of move, or reach a path the caller did
  not name. The purge writer has no delete primitive; proving otherwise is a
  critical report.
- A way to grant an approval, arm a wake, or acquire authority without the
  human act the receipts say happened.
- Install or uninstall writing or removing a file outside the manifest it
  shows you.

## What we will do

Confirmed reports get a fix, a regression test that reproduces the report
before the fix, and a changelog entry that credits you unless you ask
otherwise. That is the same standard every internal defect here gets — you can
read the evidence directory to see what that looks like in practice.
