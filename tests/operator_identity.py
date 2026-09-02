"""The operator identity shipped source must not name — derived, never spelled.

Two suites assert that no operator account name reaches shipped product source.
Both used to spell the account name as a string literal, and that made them the
one class of fence the publication pipeline cannot carry: the public exporter
maps the operator's account name to the project's public handle, so the
PUBLISHED copies of those tests asserted the *handle* was absent — a different
string, one the exporter itself writes into other public files, and one that
would still be absent if the account name leaked. The fence passed either way,
because after projection it was no longer looking for the thing it guards.

⇒ A FENCE THAT IS ITSELF REDACTED BY THE PIPELINE IT GUARDS IS REWRITTEN INTO A
FENCE FOR SOMETHING ELSE.

So the forbidden values are read at run time from two coordinates the exporter
does not touch, and every module that uses this helper projects byte-identically:

* `floati.identity_fence` — the module that already owns this vocabulary and
  already builds every governed token from hex for exactly this reason, so the
  exporter's literal scanner never sees one. This names the account this
  repository was written on, and it names the same account in the public
  checkout, where the literal would have been rewritten.
* `Path.home()` — the account running the suite, so a contributor who hardcodes
  *their own* home path is caught too. Its full path is used and never its bare
  basename: a bare account name is a substring test, and names like `runner` or
  `root` occur inside ordinary identifiers here, so that form would report the
  English language rather than a defect.
"""

from __future__ import annotations

from pathlib import Path

from floati.identity_fence import HOME_PREFIX, OWNER_USERNAME


def assert_source_names_no_operator(case, relative_path: str) -> None:
    """Fail `case` if the shipped source at `relative_path` names an operator."""

    source = Path(relative_path).read_text(encoding="utf-8")
    case.assertNotIn(HOME_PREFIX, source)
    case.assertNotIn(str(Path.home()), source)
    case.assertNotIn(OWNER_USERNAME.casefold(), source.casefold())
