"""No provider calls: persistent fake Claude process with external witnesses."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid

# The sign-in fixture models macOS's unavailable login Keychain. The documented
# setup-token flow instead returns a token for the owner to store privately.
if sys.argv[1:] == ["auth", "login"]:
    Path(os.environ["CLAUDE_CONFIG_DIR"], ".claude.json").write_text("{}\n")
    print("Keychain error: private HOME has no login keychain", file=sys.stderr)
    sys.exit(1)
if sys.argv[1:] == ["setup-token"]:
    print("fixture-private-slot-oauth-token")
    sys.exit(0)

parser = argparse.ArgumentParser()
parser.add_argument("--fixture-mode", required=True)
parser.add_argument("-p", action="store_true")
parser.add_argument("--input-format", required=True)
parser.add_argument("--output-format", required=True)
parser.add_argument("--permission-mode", required=True)
parser.add_argument("--tools", required=True)
parser.add_argument("--restricted", action="store_true")
parser.add_argument("--settings")
parser.add_argument("--resume")
parser.add_argument("prompt")
args = parser.parse_args()
home = Path(os.environ["HOME"])
config = Path(os.environ["CLAUDE_CONFIG_DIR"])
if home != config or args.permission_mode != "dontAsk" or args.tools != "Read,Write,Edit":
    sys.exit(71)
# Claude's own confinement must be explicit on every launch: restricted mode
# (file tools confined to the working directories, no project or user settings)
# and the file-tool write block outside them.
if not args.restricted or args.settings is None or json.loads(args.settings) != {
        "permissions": {"blockWritesOutsideWorkingDirectories": True}}:
    print("Claude write confinement was not set for this launch", file=sys.stderr)
    sys.exit(76)
if args.fixture_mode == "oauth" and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") != "fixture-private-slot-oauth-token":
    print("Private-slot OAuth token unavailable", file=sys.stderr)
    sys.exit(74)
if args.fixture_mode == "oauth" and any(name in os.environ for name in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FD",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
    print("Ambient provider authentication can override the private slot", file=sys.stderr)
    sys.exit(75)
launch = {"resume": args.resume, "pid": os.getpid(), "home": str(home), "argv": sys.argv[1:]}
with (home / "launches.jsonl").open("a") as stream:
    stream.write(json.dumps(launch) + "\n")
if args.fixture_mode == "crash":
    sys.exit(7)
saved = home / "saved-session.json"
if args.resume:
    prior = json.loads(saved.read_text())
    if prior["session_id"] != args.resume:
        sys.exit(72)
else:
    if saved.exists():
        sys.exit(73)  # A second create is observable and cannot pass as resume.
    prior = {"session_id": str(uuid.uuid4()), "creates": 1, "turns": 0}
prior["turns"] += 1
saved.write_text(json.dumps(prior))
saved.chmod(0o600)
Path("PROOF.txt").write_text("fixture turn " + str(prior["turns"]) + "\n")
result = {"type": "result", "subtype": "success", "is_error": False,
          "result": "fixture proof", "session_id": prior["session_id"]}
if args.fixture_mode.startswith("usage"):
    result["usage"] = {"input_tokens": 140, "output_tokens": 20,
        "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10}
    if args.fixture_mode == "usage-invalid":
        result["usage"]["input_tokens"] = -1
if args.fixture_mode == "substitute":
    result["session_id"] = str(uuid.uuid4())
elif args.fixture_mode == "missing-id":
    del result["session_id"]
elif args.fixture_mode == "error":
    result["is_error"] = True
elif args.fixture_mode == "stderr-limit":
    sys.stderr.write("x" * (1048576 + 1))
print(json.dumps(result), flush=True)
if args.fixture_mode == "post-terminal":
    print(json.dumps(result), flush=True)
