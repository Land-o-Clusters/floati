"""Replay the retained issue-15 fixture on an explicitly copied scratch bank.

Relocate only fixture coordinates, never authentication or native transcripts.
The child is the shipped hook entry point and owns no external processes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", type=Path, required=True)
    parser.add_argument("--label", required=True, choices=("red", "green"))
    parser.add_argument("--sandbox", action="store_true")
    args = parser.parse_args()
    bank = args.bank
    source = Path("\x2fprivate/tmp/floati-issue15-resit-20261030-2355")
    if bank.resolve() != bank or bank.parent != Path("\x2fprivate/tmp") or bank == source:
        parser.error("use an explicit canonical copied bank in \x2fprivate/tmp")
    metadata = json.loads((bank / "bank.json").read_text())
    original = metadata["bank"]
    if original not in {str(source), str(bank)}:
        parser.error("bank must be a copy of the named issue-15 retained fixture")
    if original != str(bank):
        # Copy adaptation, not production ledger repair. Consent's map digest
        # must describe the relocated map bytes for the same fixture property.
        relative = Path("fleet/codex-wait/workspaces.v0.json")
        path = bank / relative
        before = path.read_bytes()
        after = before.replace(original.encode(), str(bank).encode())
        path.write_bytes(after)
        consent = bank / "fleet/receipts/codex-wait-consent/issue15-codex.jsonl"
        consent.write_bytes(consent.read_bytes().replace(original.encode(), str(bank).encode())
            .replace(hashlib.sha256(before).hexdigest().encode(), hashlib.sha256(after).hexdigest().encode()))
        for key in ("bank", "home", "root", "workspace", "hook_trace"):
            metadata[key] = metadata[key].replace(original, str(bank))
        (bank / "bank.json").write_text(json.dumps(metadata, indent=2) + "\n")
        # The original was already settled; replay one new invocation on copy.
        (bank / "observer-stopping").unlink(missing_ok=True)
    payload = {"session_id": "01a18f33-6774-79f3-8084-d55607ceadf8",
               "cwd": metadata["workspace"], "hook_event_name": "Stop",
               "transcript_path": None, "turn_id": "resit-copy",
               "model": "fixture", "permission_mode": "default",
               "stop_hook_active": False, "last_assistant_message": "Done"}
    environment = {"HOME": metadata["home"], "CODEX_HOME": str(Path(metadata["home"]) / ".codex"),
        "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(bank / "tmp"), "LANG": "en_US.UTF-8", "TERM": "xterm-256color"}
    command = ["/usr/bin/python3", "-B",
        str(Path(__file__).with_name("measure_codex_issue15.py")), "hook", "--bank", str(bank)]
    if args.sandbox:
        command = ["/opt/homebrew/bin/codex", "sandbox", "-P", ":workspace",
                   "-C", metadata["workspace"], "--", *command]
    child = subprocess.run(command,
        input=json.dumps(payload), text=True, capture_output=True, env=environment,
        cwd=metadata["workspace"])
    for name, value in (("stdout", child.stdout), ("stderr", child.stderr)):
        (bank / (args.label + "." + name)).write_text(value)
    print(json.dumps({"bank": str(bank), "exit_code": child.returncode,
        "stdout": child.stdout, "stderr": child.stderr}))
    return child.returncode


if __name__ == "__main__":
    sys.exit(main())
