"""Prepare or diagnose test tooling in the calling checkout."""

import argparse
import json
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK = Path("scripts/agent-test-requirements.txt")
NODE_MAJOR = 26  # Worktree test toolchain, matching the managed Harbor profile.
GIB = 1024 ** 3
BOOTSTRAP = "scripts/agent-bootstrap.sh"
LOCK_CHECK = """
import importlib.metadata as metadata
import sys
from pathlib import Path

if Path(sys.prefix) != Path(sys.argv[1]):
    raise SystemExit("Python is not running in the worktree's own venv")
for line in Path(sys.argv[2]).read_text().splitlines():
    if line and not line.startswith("#"):
        name, expected = line.split("==")
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            raise SystemExit(f"{name}=={expected} is missing")
        if actual != expected:
            raise SystemExit(f"{name} is {actual}; expected {expected}")
import PIL
import jsonschema
"""


def probe(*argv):
    result = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        check=False, timeout=10,
    )
    output = result.stdout.strip()
    if result.returncode:
        raise ValueError(output or f"{argv[0]} exited {result.returncode}")
    return output


def python_version(executable, floor):
    version = probe(executable, "-I", "-B", "--version")
    match = re.fullmatch(r"Python (\d+)\.(\d+)\.\d+.*", version)
    if match is None or tuple(map(int, match.groups())) < floor:
        raise ValueError(f"{version}. Install Python {'.'.join(map(str, floor))}+ and put it on PATH")
    return version


def node_version(executable):
    version = probe(executable, "--version")
    if not re.fullmatch(rf"v{NODE_MAJOR}\.\d+\.\d+", version):
        raise ValueError(f"found Node {version}, expected {NODE_MAJOR}")
    return version


def disk_space(root):
    free = shutil.disk_usage(root).free
    if free < GIB:
        raise ValueError(f"{free / GIB:.2f} GiB free. Run df -h . and free at least 1 GiB before testing")
    return f"{free / GIB:.1f} GiB free"


def owned_venv(root):
    directory = root / ".venv"
    if not directory.exists() and not directory.is_symlink():
        return None
    marker = directory / ".agent-bootstrap"
    if (directory.is_symlink() or not marker.is_file() or marker.is_symlink()
            or marker.read_text() != str(root) + "\n"):
        raise ValueError(f"Move {directory} aside before bootstrapping this worktree")
    return directory


def check_venv(directory):
    probe(directory / "bin/python", "-I", "-B", "-c", LOCK_CHECK, directory, LOCK)


def bootstrap():
    disk_space(ROOT)
    directory = owned_venv(ROOT)
    if directory is not None:
        try:
            check_venv(directory)
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(
                f"{error}\nMove .venv aside (mv .venv .venv.old) and rerun {BOOTSTRAP}"
            ) from error
        print("bootstrap: worktree .venv already matches the test pins")
        return
    directory = ROOT / ".venv"
    directory.mkdir(mode=0o700)
    ready = False
    try:
        venv.EnvBuilder(with_pip=True).create(directory)
        subprocess.run(
            [directory / "bin/python", "-I", "-B", "-m", "pip", "--isolated",
             "install", "--disable-pip-version-check", "--no-cache-dir", "--progress-bar", "off",
             "--no-deps", "--only-binary=:all:", "-r", LOCK],
            check=True, timeout=120,
        )
        check_venv(directory)
        (directory / ".agent-bootstrap").write_text(str(ROOT) + "\n")
        ready = True
    finally:
        if not ready:
            shutil.rmtree(directory)
    print("bootstrap: created worktree .venv and installed the pinned test tools")


def brew_replacement(old, validate):
    # Resolve the current formula alias, then validate the exact physical file.
    if len(old.parents) < 5 or old.parents[3].name != "Cellar":
        return None
    prefix = old.parents[4]
    formula = old.parents[2].name
    formulas = [formula]
    if old.name == "node":
        formulas.append(f"node@{NODE_MAJOR}")
    for name in formulas:
        try:
            candidate = (prefix / "opt" / name / "bin" / old.name).resolve(strict=True)
            if prefix / "Cellar" not in candidate.parents:
                continue
            validate(str(candidate), "replacement executable")
            if old.name == "node":
                node_version(candidate)
            return candidate
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            continue
    return None


def gateway_pins(gateway):
    """Use the installed gateway's validation without dispatching a job."""
    registry = gateway["PROFILE_REGISTRY"]
    document = json.loads(gateway["_load_registry_bytes"](), object_pairs_hook=gateway["_unique_object"])
    errors = []
    for name, profile in document["profiles"].items():
        executables = {
            f"tools.{key}": path for key, path in profile["tools"].items()
            if key.endswith("_executable")
        }
        if "interpreter" in profile:
            executables["interpreter"] = profile["interpreter"]
        for field, path in executables.items():
            try:
                selected = gateway["_fixed_executable"](path, field)
                if field == "tools.node_executable":
                    node_version(selected)
            except (gateway["ApprovalRequired"], ValueError, OSError, subprocess.TimeoutExpired) as error:
                replacement = brew_replacement(Path(path), gateway["_fixed_executable"])
                if replacement is None:
                    tool = Path(path).name
                    formula = f"node@{NODE_MAJOR}" if tool == "node" else tool
                    fix = f"restore the pinned executable or resolve the installed tool with realpath \"$(brew --prefix {formula})/bin/{tool}\""
                else:
                    fix = f'set this field to "{replacement}"'
                errors.append(f"{registry} profiles.{name}.{field} ({path}): {error}. Owner fix: {fix}")
        directories = {
            f"git_common_dirs[{index}]": path
            for index, path in enumerate(profile["git_common_dirs"])
        }
        if "python_import_root" in profile["tools"]:
            directories["tools.python_import_root"] = profile["tools"]["python_import_root"]
        for field, path in directories.items():
            try:
                gateway["_fixed_directory"](path, field)
            except gateway["ApprovalRequired"] as error:
                command = (
                    f"{shlex.quote(profile['interpreter'])} -m site --user-site"
                    if field == "tools.python_import_root"
                    else "/usr/bin/git rev-parse --path-format=absolute --git-common-dir"
                )
                errors.append(
                    f"{registry} profiles.{name}.{field} ({path}): {error}. "
                    f"Owner fix: restore the directory or verify its replacement with {command}"
                )
    if errors:
        raise ValueError("\n".join(errors))


def gateway_check(floor):
    wrapper = Path.home() / ".codex/bin/codex-project-job"
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        raise ValueError(f"missing executable {wrapper}. Restore the owner-managed project-job gateway")
    gateway = runpy.run_path(str(wrapper))
    try:
        gateway["_validate_wrapper_invocation"](str(wrapper))
        if gateway["_has_git_redirection"](os.environ):
            raise ValueError("unset inherited Git repository redirection and rerun doctor")
        gateway_pins(gateway)
        profiles = gateway["load_registry"]()
        profile = profiles["floati-harbor"]
        repo, git_dir, common = gateway["repository_identity"](ROOT)
        if not gateway["is_primary_or_linked"](repo, git_dir, common, profile["git_common_dirs"]):
            raise ValueError("use a worktree linked to the registered Harbor repository")
        for job in ("selftest", "unittest"):
            gateway["_parse_arguments"]((str(wrapper), "floati-harbor", job), profiles, repo)
            gateway["_validate_required_file"](repo, gateway["JOB_SPECS"][job]["required"], job)
        python_version(profile["interpreter"], floor)
    except gateway["ApprovalRequired"] as error:
        raise ValueError(f"{error}. Owner fix: review {gateway['PROFILE_REGISTRY']}") from error
    try:
        probe(profile["interpreter"], "-I", "-B", "-c",
              "import sys; sys.path.insert(0, sys.argv[1]); import PIL, jsonschema",
              profile["tools"]["python_import_root"])
    except ValueError as error:
        command = shlex.quote(str(profile["interpreter"]))
        raise ValueError(
            f"{error}\nOwner fix for the registered import root: {command} -m pip install --user -r {LOCK}"
        ) from error
    return "registry pins, worktree binding and test imports verified"


def doctor(floor):
    def venv_check():
        directory = owned_venv(ROOT)
        if directory is None:
            raise ValueError(f"worktree .venv is missing. Run: {BOOTSTRAP}")
        try:
            check_venv(directory)
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"{error}\nRun: mv .venv .venv.old && {BOOTSTRAP}") from error
        return "pinned test tools present"

    checks = (
        ("Python", lambda: python_version(sys.executable, floor)),
        ("Node", lambda: node_version("node")),
        ("Minisign", lambda: probe("minisign", "-v")),
        ("Git", lambda: probe("/usr/bin/git", "--version")),
        ("Disk", lambda: disk_space(ROOT)),
        (".venv", venv_check),
        ("Gateway", lambda: gateway_check(floor)),
    )
    fixes = {
        "Node": f"Run: brew install node@{NODE_MAJOR} and add its bin directory to PATH",
        "Minisign": "Run: brew install minisign",
        "Git": "Run: xcode-select --install",
    }
    blocked = False
    for label, check in checks:
        try:
            print(f"ok {label}: {check()}")
        except (ValueError, OSError, subprocess.TimeoutExpired, KeyError, TypeError) as error:
            blocked = True
            print(f"BLOCKED {label}: {error}")
            if label in fixes:
                print(fixes[label])
    return int(blocked)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("bootstrap", "doctor"), nargs="?", default="bootstrap")
    args = parser.parse_args()
    floor = tuple(map(int, re.search(
        r'^requires-python = ">=([0-9]+)\.([0-9]+)"$', Path("pyproject.toml").read_text(), re.M
    ).groups()))
    if args.mode == "doctor":
        return doctor(floor)
    try:
        python_version(sys.executable, floor)
        bootstrap()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"bootstrap: {error}. Check Python's venv support and access to the pinned wheels.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
