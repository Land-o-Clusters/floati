"""Closed opt-in startup measurements and real copied-producer controls.

The product and launcher are never edited here. Stage byte transforms below
are admission expectations, not an implementation or a product writer.
"""

import argparse
import ast
import base64
from contextlib import ExitStack
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    # -I deliberately omits the script directory. Admit only this exact source
    # root before importing the diagnostic's repository-local utilities.
    sys.path.insert(0, str(ROOT))
from scripts.harbor_projection_observer import digest, file_row, selected_ids


PRODUCER = "ff24e08d85e0bd0c211111eb26063db7c69704c0"
HELP_COMMIT = "2b39e4b78c45f2eb329319a1ce2f2357fcbef8bf"
HELP_BANK = Path("\x2fprivate/tmp/night-harbor-help-union-after-20261021-01/help-union-calls.json")
HELP_SHA = "a43078fa23f19653cdf0f7f63f7caa92e4cc28037d610b5c22921585f3fbc0df"
STAGES = ("c0", "c1", "c2")
COLD_CASES = (("root-help", ["--help"]),
              ("effect-refusal", ["effects", "--record", "hostile"]),
              ("describe", ["describe", "--json"]))
PROBE_CASES = (*COLD_CASES, ("hidden-help", ["wake-evaluate", "--help"]))
SAFE_ENV = ("HOME", "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "XDG_CACHE_HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "FLOATI_PYTHON",
            "FLOATI_TEST_TEMP_ROOT", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH")
HELP_METHODS = {
    "tests.test_cli.SlipCliTests.test_every_command_has_static_man_page_quality_help",
    "tests.test_effect_cli.EffectCliTests.test_static_help_covers_every_effect_command_without_import_side_effects",
}
_ACTIVE = None


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def error_record(exc):
    return {"type": type(exc).__module__ + "." + type(exc).__qualname__, "message": str(exc)}


def triple(row):
    return {key: row[key] for key in ("returncode", "stdout", "stderr")}


def parse_importtime(raw):
    """Classify the pinned CPython 3.9.6 byte grammar, retaining every line.

    This is syntax recognition, not authentication of a stderr writer. A
    product line with the exact format is indistinguishable; source review
    and unchanged native baseline evidence narrow but cannot remove that limit.
    """
    header = b"import time: self [us] | cumulative | imported package\n"
    pattern = re.compile(rb"import time: ( *[0-9]+) \| ( *[0-9]+) \| ( *)([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\n")
    rows, lines, errors, residual = [], [], [], bytearray()
    header_count, offset = 0, 0
    pieces = raw.split(b"\n")
    if raw.endswith(b"\n"):
        pieces.pop()
    for index, piece in enumerate(pieces):
        complete = index < len(pieces) - 1 or raw.endswith(b"\n")
        line = piece + (b"\n" if complete else b"")
        record = {"offset": offset, "end_offset": offset + len(line),
                  "bytes_base64": base64.b64encode(line).decode(), "classification": "residual"}
        lines.append(record)
        issue = None
        if not complete:
            issue = "truncated line"
        elif line == header:
            header_count += 1
            if header_count != 1 or rows:
                issue = "duplicate or late header"
            else:
                record["classification"] = "header"
        elif line.startswith(b"import time:"):
            matched = pattern.fullmatch(line)
            if matched is None:
                issue = "unknown import timing grammar"
            else:
                own, cumulative = int(matched[1]), int(matched[2])
                spaces, module = len(matched[3]), matched[4].decode("ascii")
                rendered = (f"import time: {own:9d} | {cumulative:10d} | ".encode("ascii") +
                            b" " * spaces + module.encode("ascii") + b"\n")
                if rendered != line or spaces % 2 or cumulative < own:
                    issue = "padding, nesting or cumulative relation differs"
                elif header_count != 1:
                    issue = "row without its unique preceding header"
                else:
                    record["classification"] = "row"
                    record["row_index"] = len(rows)
                    rows.append({"index": len(rows), "offset": offset, "end_offset": offset + len(line),
                                 "self_us": own, "cumulative_us": cumulative, "depth": spaces // 2,
                                 "module": module})
        if issue is not None:
            record["error"] = issue
            errors.append({"offset": offset, "error": issue})
        if record["classification"] == "residual":
            residual.extend(line)
        offset += len(line)
    if header_count != 1:
        errors.append({"error": "exactly one header required", "count": header_count})
    if not rows:
        errors.append({"error": "at least one recognized timing row required"})
    for index, row in enumerate(rows):
        following = next((later for later in rows[index + 1:] if later["depth"] < row["depth"]), None)
        if row["depth"] == 0:
            row.update(parent_index=None, parent_basis="top-level attempted import")
        elif following is not None and following["depth"] == row["depth"] - 1:
            row.update(parent_index=following["index"], parent_basis="first subsequent enclosing postorder depth")
        else:
            row.update(parent_index=None, parent_basis="ambiguous or enclosing attempt not observed")
    return {"format": "CPython-v3.9.6-import.c", "valid": not errors, "header_count": header_count,
            "lines": lines, "rows": rows, "errors": errors,
            "residual_base64": base64.b64encode(residual).decode(),
            "residual_sha256": sha(residual), "raw_sha256": sha(raw),
            "limits": ["Syntactically matching product stderr cannot be distinguished from profiler output.",
                       "Rows represent attempted loads, including failed imports; cached imports are incomplete.",
                       "Postorder/depth parent links do not establish a complete dependency graph."]}


def ordinary(path, directory=False):
    path = Path(path)
    require(path.is_absolute() and path == Path(os.path.normpath(str(path))), "noncanonical path")
    for parent in reversed(path.parents):
        require(stat.S_ISDIR(parent.lstat().st_mode), "symlinked or non-directory ancestor")
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), "nonordinary path")
    require(info.st_uid == os.getuid(), "foreign owned path")
    return path


def git(*args, root=ROOT, input_data=None):
    completed = subprocess.run(["/usr/bin/git", *args], cwd=root, input=input_data,
                               capture_output=True, check=True)
    return completed.stdout


def committed_files(commit, paths):
    """Read exact old Git blobs in one batch, without importing the producer."""
    listing = git("ls-tree", "-r", "-z", commit, "--", *paths)
    entries = []
    for item in listing.split(b"\0"):
        if not item:
            continue
        metadata, path = item.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        require(kind == "blob" and mode in ("100644", "100755"), "producer has nonordinary source")
        entries.append((path.decode(), mode, blob))
    data = git("cat-file", "--batch", input_data="".join(row[2] + "\n" for row in entries).encode())
    offset, result = 0, {}
    for name, mode, blob in entries:
        end = data.index(b"\n", offset)
        found, kind, size = data[offset:end].decode().split()
        require(found == blob and kind == "blob", "producer batch identity mismatch")
        offset = end + 1
        content = data[offset:offset + int(size)]
        offset += int(size)
        require(data[offset:offset + 1] == b"\n", "producer batch framing mismatch")
        offset += 1
        result[name] = {"mode": mode, "blob": blob, "bytes": content}
    require(offset == len(data), "producer batch has trailing output")
    return result


def package_files(root):
    paths = []
    for parent, directories, files in os.walk(root / "floati", followlinks=False):
        for name in directories:
            path = Path(parent) / name
            require(not path.is_symlink(), "package directory is symlinked")
            require(name != "__pycache__", "package bytecode cache makes producer admission ambiguous")
        for name in files:
            require(not name.endswith((".pyc", ".pyo")), "package contains bytecode")
            paths.append(Path(parent) / name)
    paths.append(root / "scripts/floati")
    rows = []
    for path in sorted(paths):
        require(stat.S_ISREG(path.lstat().st_mode), "package/launcher file is not ordinary")
        rows.append(file_row(path, path.relative_to(root).as_posix()))
    return rows


def stage_cli_bytes(original, stage):
    old = b'''    from .helptext import help_for

    static_help = help_for(arguments)
    if static_help is not None:
        print(static_help, end="")
        return OK
'''
    gated = b'''    if "--help" in arguments:
        from .helptext import help_for

        static_help = help_for(arguments)
        if static_help is not None:
            print(static_help, end="")
            return OK
'''
    require(original.count(old) == 1, "C0 help seam drifted")
    if stage == "c0":
        return original
    first = original.replace(old, gated, 1)
    if stage == "c1":
        return first
    parser = b'''    parser = _parser()
    from .command_contract import schema_version_for_arguments

    artifact_schema_version = schema_version_for_arguments(parser, arguments)
'''
    require(first.count(parser + gated) == 1, "C1 parser/help seam drifted")
    return first.replace(parser + gated, gated + parser, 1)


def source_identity(root):
    paths = sorted(set(git("ls-files", "--cached", "-z", root=root).decode().split("\0")) - {""})
    rows = [file_row(root / name, name) for name in paths]
    others = git("ls-files", "--others", "--exclude-standard", "-z", root=root).decode().split("\0")
    return {"head": git("rev-parse", "HEAD", root=root).decode().strip(),
            "status_base64": base64.b64encode(git("status", "--porcelain=v1", "-z", "--untracked-files=all", root=root)).decode(),
            "index_entries_sha256": sha(git("ls-files", "--stage", "-z", root=root)),
            "tracked": rows, "tracked_digest": digest(rows),
            "untracked_python": [file_row(root / name, name) for name in sorted(others) if name.endswith(".py")]}


def namespace(root):
    if not os.path.lexists(root):
        return {"exists": False, "entries": []}
    rows = [file_row(root, ".")]
    for parent, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        for name in sorted(directories + files):
            path = Path(parent) / name
            rows.append(file_row(path, path.relative_to(root).as_posix()))
    return {"exists": True, "entries": sorted(rows, key=lambda row: row["path"])}


def selected_interpreter(environment):
    explicit = environment.get("FLOATI_PYTHON", "")
    candidates = [explicit] if explicit else ["/usr/bin/python3", "/bin/python3"]
    for name in candidates:
        path = Path(name)
        if path.is_file() and os.access(path, os.X_OK):
            if explicit:
                require(path.is_absolute() and not path.is_symlink(), "invalid explicit launcher interpreter")
            return {"selected_path": str(path), "resolved_selection": str(path.resolve()),
                    "basis": "unchanged launcher fixed branch and recorded environment",
                    "running_executable": None,
                    "running_scope": "unmodified launcher child executable not independently observed"}
    raise AssertionError("no launcher interpreter selection")


def admit_producer(root, stage):
    bank_bytes = ordinary(HELP_BANK).read_bytes()
    require(sha(bank_bytes) == HELP_SHA, "accepted help bank hash changed")
    bank = json.loads(bank_bytes)
    require(bank["source_before"] == bank["source_after"], "accepted help source changed")
    require(len(bank["calls"]) == len(bank["children"]) == 42, "accepted help count changed")
    require(all(row["returncode"] == 0 for row in bank["children"]), "accepted help collection failed")
    old = committed_files(PRODUCER, ["floati", "scripts/floati"])
    help_product = committed_files(HELP_COMMIT, ["floati", "scripts/floati"])
    require(old == help_product, "help banking commit differs from actual accepted parent producer")
    current = package_files(root)
    require({row["path"] for row in current} == set(old), "whole package namespace differs from producer")
    wanted_cli = stage_cli_bytes(old["floati/cli.py"]["bytes"], stage)
    expected = []
    for row in current:
        name = row["path"]
        previous = old[name]
        data = wanted_cli if name == "floati/cli.py" else previous["bytes"]
        mode = 0o755 if previous["mode"] == "100755" else 0o644
        require(row["type"] == "file" and row["sha256"] == sha(data) and row["mode"] == mode,
                "stage package or launcher bytes/mode mismatch: " + name)
        expected.append({"path": name, "producer_blob": previous["blob"],
                         "producer_sha256": sha(previous["bytes"]), "current_sha256": row["sha256"],
                         "mode": mode, "bytes": row["bytes"]})
    for name, recorded in bank["source_before"].items():
        if name != "floati/cli.py":
            require(sha((root / name).read_bytes()) == recorded, "accepted help dependency/subject differs: " + name)
        else:
            require(sha(old[name]["bytes"]) == recorded, "accepted help CLI producer identity differs")
    return {"producer_commit": PRODUCER, "help_bank_commit": HELP_COMMIT,
            "help_bank": str(HELP_BANK), "help_bank_sha256": HELP_SHA,
            "whole_package_and_launcher": expected, "package_digest": digest(current),
            "source_stage": stage, "help_output_reuse_only": True,
            "old_interpreter_testimony": bank["interpreter"],
            "old_FLOATI_PYTHON": bank["FLOATI_PYTHON"],
            "old_running_executable": "not independently observed by this bank",
            "baseline_cli": old["floati/cli.py"]["bytes"]}, bank


class Session:
    def __init__(self, root, output, alias, suite):
        self.root, self.output, self.alias = root, output, alias
        _prefix, self.stage, self.operation = alias.split("-", 2)
        require(self.stage in STAGES, "unknown startup stage")
        self.selection = selected_ids(suite)
        self.environment = dict(os.environ)
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"
        self.interpreter = selected_interpreter(self.environment)
        self.native_popen = subprocess.Popen
        self.calls, self.children, self.owned, self.cleanups = [], [], [], []
        self.details, self.checks = {}, {}
        self.before = source_identity(root)
        self.admission, self.help_bank = admit_producer(root, self.stage)
        self.baseline_cli = self.admission.pop("baseline_cli")
        self.fixture_root = ordinary(Path(os.environ["FLOATI_TEST_TEMP_ROOT"]), directory=True)
        require(self.fixture_root == output / "worker/fixtures", "startup worker binding differs")

    def allocate(self, prefix):
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=self.fixture_root))
        info = path.lstat()
        self.owned.append((path, (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))))
        return path

    def collect_popen(self, *args, **kwargs):
        child = self.native_popen(*args, **kwargs)
        self.children.append(child)
        return child

    def launch(self, argv, *, label, cwd=None, environment=None):
        row = {"label": label, "argv": list(map(str, argv)), "cwd": str(cwd or self.root),
               "status": "started"}
        self.calls.append(row)
        first, began = len(self.children), time.monotonic_ns()
        try:
            with mock.patch.object(subprocess, "Popen", self.collect_popen):
                value = subprocess.run(argv, cwd=cwd or self.root,
                                       env=environment or self.environment,
                                       capture_output=True, text=True, timeout=30)
        except BaseException as exc:
            row.update(status="raised", exception=error_record(exc))
            if isinstance(exc, subprocess.TimeoutExpired):
                row["partial_stdout_base64"] = base64.b64encode(exc.stdout or b"").decode()
                row["partial_stderr_base64"] = base64.b64encode(exc.stderr or b"").decode()
            raise
        else:
            row.update(status="returned", returncode=value.returncode, stdout=value.stdout, stderr=value.stderr)
            return row
        finally:
            row["elapsed_ns"] = time.monotonic_ns() - began
            row["children"] = [{"pid": child.pid, "args": child.args, "returncode": child.returncode}
                               for child in self.children[first:]]

    def launcher(self, arguments, label, root=None):
        return self.launch([str((root or self.root) / "scripts/floati"), *arguments], label=label)

    def launch_import_profile(self, argv, environment):
        """The sole binary-capture import child; existing launch is unchanged."""
        row = {"label": "import-attribution", "argv": list(argv), "cwd": str(self.root),
               "status": "started", "stdout_available": False, "stderr_available": False}
        self.calls.append(row)
        first, began = len(self.children), time.monotonic_ns()
        stdout, stderr = b"", b""
        try:
            with mock.patch.object(subprocess, "Popen", self.collect_popen):
                value = subprocess.run(argv, cwd=self.root, env=environment,
                                       capture_output=True, timeout=30)
        except BaseException as exc:
            row.update(status="raised", exception=error_record(exc))
            for field in ("stdout", "stderr"):
                partial = getattr(exc, field, None)
                if partial is not None:
                    require(type(partial) is bytes, "partial import output is not raw bytes")
                    if field == "stdout":
                        stdout = partial
                    else:
                        stderr = partial
                    row[field + "_available"] = True
            raise
        else:
            stdout, stderr = value.stdout, value.stderr
            require(type(stdout) is bytes and type(stderr) is bytes, "import output is not raw bytes")
            row.update(status="returned", returncode=value.returncode,
                       stdout_available=True, stderr_available=True)
            return row, stdout, stderr
        finally:
            row["elapsed_ns"] = time.monotonic_ns() - began
            row["children"] = [{"pid": child.pid, "args": child.args, "returncode": child.returncode}
                               for child in self.children[first:]]
            for field, raw in (("stdout", stdout), ("stderr", stderr)):
                path = self.output / ("import-child." + field + ".raw")
                with path.open("xb") as stream:
                    stream.write(raw)
                row[field] = {"path": str(path), "bytes": len(raw), "sha256": sha(raw),
                              "complete": row["status"] == "returned",
                              "available": row[field + "_available"]}

    def baseline_native(self):
        path = Path("\x2fprivate/tmp/night-harbor-cli-startup-c0-native-20261021-01/startup-observation.json")
        data = ordinary(path).read_bytes()
        document = json.loads(data)
        require(document["valid"] and document["stage"] == "c0" and document["operation"] == "native",
                "C0 native baseline is not valid")
        require(document["admission"]["producer_commit"] == PRODUCER and
                document["admission"]["help_bank_sha256"] == HELP_SHA, "C0 baseline producer differs")
        require(len(document["calls"]) == 32, "C0 native baseline count differs")
        self.details["native_baseline"] = {"path": str(path), "sha256": sha(data)}
        return {row["label"]: row for row in document["calls"]}

    def native(self):
        for round_number in range(5):
            for label, arguments in COLD_CASES:
                self.launcher(arguments, f"cold:{round_number}:{label}")
        private = self.allocate("startup-native-")
        from floati.root import FloatiRoot
        from floati.registry import Registry
        root = FloatiRoot.open_direct_home(private / "fleet", create=True)
        Registry(root).register("recipient", "Codex")
        absent = private / "must-stay-absent"
        operation = "effect-op-018f7e9b3c117abc8def0123456789ab"
        cases = [
            ["role", "show", "builder", "--help"], ["register", "worker", "--help"],
            ["receipts", "worker", "--help"], ["update", "--json", "fleet", "--help"],
            ["update", "--source", "fleet", "--help"], ["wake-evaluate", "--help"],
            ["wake", "daemon", "serve", "--help"], ["role", "unknown-child", "--help"],
            ["role", "show", "builder", "--", "--help"], ["update", "--source", "--help"],
            ["--help=x"], ["effect", "show"],
            ["effect", "compensate", "--operation", operation, "--preview", "--confirm", "a" * 64],
            ["wake-evaluat"], ["effect", "show", "--operation", "--help"],
            ["init", "--root", str(absent), "--solo", "fixture", "--harness", "Codex\x1b[31m"],
            ["inbox", "--root", str(root.path), "--as", "recipient", "--peek", "--help"],
        ]
        before = namespace(root.path)
        for index, arguments in enumerate(cases, 1):
            self.launcher(arguments, "matrix:" + str(index))
        after = namespace(root.path)
        self.details["fixture"] = {"root": str(root.path), "before": before, "after": after,
                                   "unsafe_init_root": str(absent), "unsafe_init_absent": not os.path.lexists(absent)}
        require(before == after and not os.path.lexists(absent), "healthy native pre-bind/help path wrote a fixture")
        require(len(self.calls) == 32 and all(row["status"] == "returned" for row in self.calls), "healthy count/return differs")
        for label, _arguments in COLD_CASES:
            repeated = [triple(row) for row in self.calls[:15] if row["label"].endswith(":" + label)]
            require(len(repeated) == 5 and all(row == repeated[0] for row in repeated),
                    "repeated native complete output differs: " + label)
        expected_help = {tuple(row["argv"]): triple(row) for row in self.help_bank["calls"]}
        for row in self.calls[:15]:
            case = row["label"].split(":")[-1]
            if case == "root-help":
                require(triple(row) == expected_help[("--help",)], "root help differs from accepted native output")
            elif case == "effect-refusal":
                artifact = json.loads(row["stdout"])
                require(row["returncode"] == 20 and row["stderr"] == "" and
                        artifact["schema_version"] == 1 and artifact["evidence"]["code"] == "arguments_invalid",
                        "native effect refusal contract differs")
            else:
                require(row["returncode"] == 0 and row["stderr"] == "", "native describe failed")
        for number, topic in enumerate(("role show", "register", "receipts", "update fleet", "update"), 1):
            row = self.calls[14 + number]
            require(row["returncode"] == 0 and row["stderr"] == "" and
                    row["stdout"].splitlines()[1].startswith("    floati " + topic + " - "), "token-role help topic differs")
        for number in (6, 7, 8, 14):
            row = self.calls[14 + number]
            require(row["returncode"] == 20 and json.loads(row["stdout"])["evidence"]["code"] == "arguments_invalid",
                    "hidden or unknown native help did not refuse")
        if self.stage != "c0":
            baseline = self.baseline_native()
            for row in self.calls:
                require(triple(row) == triple(baseline[row["label"]]), "healthy complete native output differs: " + row["label"])
        self.details["cold_seconds"] = {label: {
            "samples": [row["elapsed_ns"] / 1e9 for row in self.calls if row["label"].endswith(":" + label)],
            "minimum": min(row["elapsed_ns"] / 1e9 for row in self.calls if row["label"].endswith(":" + label)),
            "maximum": max(row["elapsed_ns"] / 1e9 for row in self.calls if row["label"].endswith(":" + label)),
            "median": statistics.median(row["elapsed_ns"] / 1e9 for row in self.calls if row["label"].endswith(":" + label))}
            for label, _args in COLD_CASES}

    def attribution(self):
        baseline = self.baseline_native()
        expected_counts = {"root-help": 1 if self.stage == "c2" else 2,
                           "effect-refusal": 1, "describe": 2, "hidden-help": 2}
        observations = []
        self.details["attribution"] = observations
        for name, arguments in PROBE_CASES:
            out = self.output / ("attribution-" + name + ".json")
            row = self.launch([self.interpreter["selected_path"], "-I", "-B", str(Path(__file__).resolve()),
                               "_probe", "--case", name, "--output", str(out)], label="attribution:" + name)
            data = json.loads(ordinary(out).read_text())
            observations.append(data)
            require(not any(data["before_import_modules"].values()) and
                    not any(data["before_main_modules"].values()), "attribution began with help modules loaded")
            require(row["argv"][0] == self.interpreter["selected_path"] and
                    len(row["children"]) == 1 and row["children"][0]["args"] == row["argv"],
                    "attribution child differs from exact selected invocation")
            require(data["cli_sha256"] == sha((self.root / "floati/cli.py").read_bytes()),
                    "attribution CLI bytes differ")
            reference = baseline["matrix:6" if name == "hidden-help" else "cold:0:" + name]
            require(triple(row) == triple(reference), "attribution changed actual CLI output")
            require(data["parser_count"] == expected_counts[name] and
                    len(set(data["parser_ids"])) == expected_counts[name], "actual parser construction count/identity differs")
            expect_help = name in ("root-help", "hidden-help") or self.stage == "c0"
            require(data["after_main_modules"] == {"floati.helptext": expect_help, "floati.generated_help": expect_help},
                    "actual help module presence differs")
            require(data["cli_file"] == str(self.root / "floati/cli.py") and data["parser_binding_restored"],
                    "attribution package/binding differs")
        require(all(row["observed_running_interpreter"] == observations[0]["observed_running_interpreter"]
                    for row in observations), "attribution reported running interpreter changed across probes")

    def copied_source(self):
        private = self.allocate("startup-producer-")
        shutil.copytree(self.root / "floati", private / "floati")
        (private / "scripts").mkdir()
        shutil.copy2(self.root / "scripts/floati", private / "scripts/floati")
        require(package_files(private) == package_files(self.root), "copied producer bytes differ")
        return private

    def interpreter_route(self):
        require(self.stage == "c2", "interpreter-route observation requires accepted C2")
        native_path = Path("\x2fprivate/tmp/night-harbor-cli-startup-c2-native-20261021-01/startup-observation.json")
        attribution_path = Path("\x2fprivate/tmp/night-harbor-cli-startup-c2-attribution-20261021-01/startup-observation.json")
        accepted = []
        for path, expected in (
            (native_path, "1ae8755f7164952f3a770fd525a073fdcc0e764f36b00af5d6062dfde92eea2d"),
            (attribution_path, "9ec9ac8255ba3ab1afe89729e6eefce6e036e07b1826ab4d8dc2cb5b52dbcb99"),
        ):
            raw = ordinary(path).read_bytes()
            require(sha(raw) == expected, "accepted C2 route dependency changed")
            document = json.loads(raw)
            require(document["valid"] and document["stage"] == "c2" and
                    document["source_before"] == document["source_after"], "C2 route dependency invalid")
            require(document["admission"]["package_digest"] == self.admission["package_digest"],
                    "route package differs from accepted C2")
            require(document["retained_source"]["source-manifest.json"] ==
                    sha((self.root / "bundle-manifest.v0.json").read_bytes()), "route manifest differs from C2")
            accepted.append(document)
        native, attribution = accepted
        require(self.environment.get("FLOATI_PYTHON", "") == "" and
                native["environment"].get("FLOATI_PYTHON") in (None, "") and
                attribution["environment"].get("FLOATI_PYTHON") in (None, "") and
                self.interpreter["selected_path"] == "/usr/bin/python3",
                "route diagnostic requires admitted unchanged default selection")
        expected_runtime = attribution["details"]["attribution"][0]["observed_running_interpreter"]
        require(all(row["observed_running_interpreter"] == expected_runtime
                    for row in attribution["details"]["attribution"]), "C2 runtime testimony differs")
        canonical = "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9"
        require(expected_runtime["resolved"] == canonical and
                expected_runtime["version"].startswith("3.9.6 "), "unadmitted canonical interpreter/version")
        executables = {"S": "/usr/bin/python3", "R": canonical}

        def executable_identity(name):
            path = Path(name)
            require(path.is_absolute() and str(path) == os.path.normpath(name), "noncanonical executable coordinate")
            require(all(stat.S_ISDIR(parent.lstat().st_mode) for parent in path.parents),
                    "executable has symlinked/nonordinary ancestor")
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and os.access(path, os.X_OK), "selected executable is not ordinary/executable")
            return {"path": name, "resolved": str(path.resolve(strict=True)), "device": info.st_dev,
                    "inode": info.st_ino, "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode),
                    "bytes": info.st_size, "mtime_ns": info.st_mtime_ns, "sha256": sha(path.read_bytes())}

        identities = {name: executable_identity(path) for name, path in executables.items()}
        require(identities["R"]["resolved"] == canonical, "canonical route is not its admitted ordinary file")
        reference = next(row for row in native["calls"] if row["label"] == "cold:0:effect-refusal")
        private = self.allocate("startup-route-")
        sidecar = private / "runtime.literal"
        # Same bootstrap and argv after executable for both routes. Only the
        # finally block adds observation, after real run_module execution.
        bootstrap = '''import runpy
import sys
sys.path.insert(0, sys.argv.pop(1))
_sidecar = sys.argv.pop(1)
try:
    runpy.run_module("floati", run_name="__main__", alter_sys=True)
finally:
    with open(_sidecar, "x", encoding="utf-8") as _stream:
        _stream.write(repr({
            "executable": sys.executable,
            "version": sys.version,
            "version_info": tuple(sys.version_info),
            "implementation": (sys.implementation.name, tuple(sys.implementation.version), sys.implementation.cache_tag),
            "flags": (sys.flags.isolated, sys.flags.ignore_environment, sys.flags.no_user_site, sys.flags.dont_write_bytecode),
            "package_file": getattr(sys.modules.get("floati"), "__file__", None),
            "cli_file": getattr(sys.modules.get("floati.cli"), "__file__", None),
        }))
'''
        suffix = ["-I", "-B", "-c", bootstrap, str(self.root), str(sidecar), "effects", "--record", "hostile"]
        environment = dict(self.environment)
        environment_digest = digest(environment)
        observations = []
        detail = {"scope": "six diagnostic route durations including common finally sidecar; not literal launcher timing",
                  "accepted_native": {"path": str(native_path), "sha256": sha(native_path.read_bytes())},
                  "accepted_attribution": {"path": str(attribution_path), "sha256": sha(attribution_path.read_bytes())},
                  "executable_identities_before": identities, "bootstrap": bootstrap,
                  "argv_suffix": suffix, "argv_suffix_sha256": digest(suffix),
                  "complete_environment_sha256": environment_digest,
                  "schedule": ["S", "R", "R", "S", "S", "R"], "observations": observations,
                  "limits": ["No import timing or Apple shim-operation attribution follows.",
                             "Do not subtract these values from literal launcher medians or extrapolate suite speedup."]}
        self.details["interpreter_route"] = detail
        for index, route in enumerate(detail["schedule"]):
            require(not os.path.lexists(sidecar) and digest(environment) == environment_digest,
                    "sidecar or common environment drift before route child")
            row = self.launch([executables[route], *suffix], label=f"route:{index}:{route}", environment=environment)
            began = time.monotonic_ns()
            observation = {"index": index, "route": route, "call_label": row["label"]}
            observations.append(observation)
            raw = ordinary(sidecar).read_bytes()
            require(len(raw) <= 65536, "route sidecar is unbounded")
            (self.output / ("route-" + str(index) + ".literal")).write_bytes(raw)
            actual = ast.literal_eval(raw.decode("utf-8"))
            require(type(actual) is dict and set(actual) == {
                "executable", "version", "version_info", "implementation", "flags", "package_file", "cli_file"},
                "route runtime sidecar shape differs")
            require(type(actual["executable"]) is str and Path(actual["executable"]).is_absolute(),
                    "route did not report an absolute running executable")
            resolved = str(Path(actual["executable"]).resolve(strict=True))
            observation.update(sidecar_sha256=sha(raw), reported=actual, resolved_running_executable=resolved)
            require(resolved == canonical and actual["version"] == expected_runtime["version"],
                    "route reported runtime differs from admitted C2")
            require(actual["package_file"] == str(self.root / "floati/__init__.py") and
                    actual["cli_file"] == str(self.root / "floati/cli.py") and actual["flags"] == (1, 1, 1, 1),
                    "route package or isolated flags differ")
            require(row["returncode"] == 20 and triple(row) == triple(reference) and
                    row["argv"][1:] == suffix and len(row["children"]) == 1 and
                    row["children"][0]["args"] == row["argv"] and row["children"][0]["returncode"] == 20,
                    "route actual native invocation/outcome differs")
            sidecar.unlink()
            observation["sidecar_vacated"] = not os.path.lexists(sidecar)
            observation["controller_identity_ns"] = time.monotonic_ns() - began
        require(len(self.calls) == len(self.children) == len(observations) == 6,
                "route observation did not collect exactly six native children")
        detail["executable_identities_after"] = {name: executable_identity(path) for name, path in executables.items()}
        require(detail["executable_identities_after"] == identities and digest(environment) == environment_digest,
                "route executable/common environment changed")
        detail["paired_seconds"] = []
        for pair in range(3):
            calls = self.calls[pair * 2:pair * 2 + 2]
            by_route = {row["label"].split(":")[-1]: row["elapsed_ns"] / 1e9 for row in calls}
            detail["paired_seconds"].append({"order": [row["label"].split(":")[-1] for row in calls],
                                               **by_route, "S_minus_R": by_route["S"] - by_route["R"]})

    def import_attribution(self):
        require(self.stage == "c2", "import observation requires accepted C2")
        accepted = []
        for path, expected in (
            (Path("\x2fprivate/tmp/night-harbor-cli-startup-c2-native-20261021-01/startup-observation.json"),
             "1ae8755f7164952f3a770fd525a073fdcc0e764f36b00af5d6062dfde92eea2d"),
            (Path("\x2fprivate/tmp/night-harbor-cli-startup-c2-interpreter-route-20261021-01/startup-observation.json"),
             "bad9c47dfd7b5d82852c659f055290af4fa596d7d7b02155ca218e9c3fb35e9a"),
        ):
            raw = ordinary(path).read_bytes()
            require(sha(raw) == expected, "accepted import dependency changed")
            document = json.loads(raw)
            require(document["valid"] and document["stage"] == "c2" and
                    document["source_before"] == document["source_after"] and
                    document["admission"]["package_digest"] == self.admission["package_digest"] and
                    document["retained_source"]["source-manifest.json"] ==
                    sha((self.root / "bundle-manifest.v0.json").read_bytes()), "import dependency invalid/drifted")
            accepted.append((path, expected, document))
        native, route = [entry[2] for entry in accepted]
        require(native["operation"] == "native" and len(native["calls"]) == 32 and
                route["operation"] == "interpreter-route" and len(route["calls"]) == len(route["children"]) == 6,
                "accepted dependency shape differs")
        reference = next(row for row in native["calls"] if row["label"] == "cold:0:effect-refusal")
        prior = route["details"]["interpreter_route"]
        require(all(triple(row) == triple(reference) for row in route["calls"]) and
                all(row["returncode"] == 20 for row in route["children"]), "route native parity differs")
        require(self.interpreter["selected_path"] == "/usr/bin/python3" and
                self.environment.get("FLOATI_PYTHON", "") == "", "import default selection differs")
        constructor = "scripts/parallel_isolation.py"
        old_constructor = next(row for row in route["source_before"]["tracked"] if row["path"] == constructor)
        require(file_row(self.root / constructor, constructor) == old_constructor, "environment constructor drift")

        def run_coordinates(bank):
            worker = bank / "worker"
            home = worker / "fixtures/prepin-home"
            temporary = str(Path("\x2ftmp") / (worker / "tmp").relative_to("\x2fprivate/tmp"))
            return {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache"),
                    "TMPDIR": temporary, "TMP": temporary, "TEMP": temporary,
                    "FLOATI_TEST_TEMP_ROOT": str(worker / "fixtures")}

        current_paths, previous_paths = run_coordinates(self.output), run_coordinates(accepted[1][0].parent)
        for key in SAFE_ENV:
            if key in current_paths:
                require(self.environment.get(key) == current_paths[key] and route["environment"][key] == previous_paths[key],
                        "unexplained owned environment coordinate: " + key)
            else:
                require(self.environment.get(key) == route["environment"][key], "comparable environment drift: " + key)
        environment = dict(self.environment)
        environment_digest, controller_environment_digest = digest(environment), digest(dict(os.environ))
        selected = Path("/usr/bin/python3")
        manual = Path("/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/share/man/man1/python3.9.1")
        system_inputs = {
            selected: (0o755, "34129c71a01a74f7f3b2443521519b2e5447553fa187f5fcafaaf8c42cc192e2"),
            manual: (0o644, "53895876607067b415d8b2362efeaa8dec5a837400782537e9079799a114cdd0"),
        }

        def system_identity(path):
            require(path in system_inputs and path.is_absolute() and str(path) == os.path.normpath(str(path)) and
                    path.resolve(strict=True) == path, "unexpected fixed system path")
            for parent in path.parents:
                ancestor = parent.lstat()
                require(stat.S_ISDIR(ancestor.st_mode) and ancestor.st_uid == ancestor.st_gid == 0 and
                        stat.S_IMODE(ancestor.st_mode) == 0o755, "fixed system ancestor identity differs")
            mode, expected_hash = system_inputs[path]
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == info.st_gid == 0 and
                    stat.S_IMODE(info.st_mode) == mode, "fixed system file identity differs")
            require(path != selected or os.access(path, os.X_OK), "selected executable unavailable")
            row = file_row(path, str(path))
            after_read = path.lstat()
            fields = ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
            require(row["sha256"] == expected_hash and
                    all(getattr(after_read, field) == getattr(info, field) for field in fields),
                    "fixed system bytes/identity changed")
            return {**row, "device": info.st_dev, "inode": info.st_ino,
                    "uid": info.st_uid, "gid": info.st_gid, "mtime_ns": info.st_mtime_ns,
                    "ctime_ns": info.st_ctime_ns, "resolved": str(path)}

        executable_before, manual_before = system_identity(selected), system_identity(manual)
        bootstrap = prior["bootstrap"]
        require(prior["argv_suffix"][3] == bootstrap, "route bootstrap coordinates differ")
        emitters, collisions = [], []
        for name, raw in [(row["path"], (self.root / row["path"]).read_bytes())
                          for row in self.admission["whole_package_and_launcher"]] + [("<route-bootstrap>", bootstrap.encode())]:
            for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
                if any(token in line for token in ("import time:", "self [us]", "cumulative", "stderr", "dup2", "os.write")):
                    emitters.append({"path": name, "line": number, "text": line})
                if "import time:" in line or "self [us]" in line:
                    collisions.append({"path": name, "line": number, "text": line})
        require(not collisions, "plausible import timing prefix collision requires a new decision")
        for name, data in (("source-index.entries", git("ls-files", "--stage", "-z", root=self.root)),
                           ("source-staged.patch", git("diff", "--cached", "--binary", root=self.root)),
                           ("source-working.patch", git("diff", "--binary", root=self.root))):
            (self.output / name).write_bytes(data)
        private = self.allocate("startup-import-")
        sidecar = private / "runtime.literal"
        argv = [str(selected), "-I", "-B", "-X", "importtime", "-c", bootstrap,
                str(self.root), str(sidecar), "effects", "--record", "hostile"]
        detail = {"scope": "one instrumented import attempt trace, not native wall time or expected savings",
                  "accepted": [{"path": str(path), "sha256": expected} for path, expected, _ in accepted],
                  "bootstrap": bootstrap, "bootstrap_sha256": sha(bootstrap.encode()), "argv": argv,
                  "executable_before": executable_before, "manual_before": manual_before,
                  "complete_current_environment_sha256": environment_digest,
                  "controller_environment_before_sha256": controller_environment_digest,
                  "historical_environment_scope": "only retained SAFE_ENV fields; no historical full-map comparison",
                  "owned_coordinates_before": previous_paths, "owned_coordinates_current": current_paths,
                  "comparable_environment_fields": [key for key in SAFE_ENV if key not in current_paths],
                  "stderr_source_census": emitters, "trace_prefix_collisions": collisions,
                  "format_source": "https://github.com/python/cpython/blob/v3.9.6/Python/import.c#L1623-L1682",
                  "manual_sha256": sha(manual.read_bytes()), "relevance_threshold_instrumented_us": 10000,
                  "source_retained": {name: sha((self.output / name).read_bytes()) for name in
                                      ("source-index.entries", "source-staged.patch", "source-working.patch")},
                  "limits": ["Exact-format product stderr is syntactically indistinguishable from profiler output.",
                             "One trace includes bootstrap and possible post-product sidecar imports.",
                             "Do not sum overlapping cumulative rows or extrapolate native/suite savings."]}
        self.details["import_attribution"] = detail
        try:
            require(not os.path.lexists(sidecar), "import sidecar already exists")
            row, stdout, stderr = self.launch_import_profile(argv, environment)
            parsed = parse_importtime(stderr)
            (self.output / "import-trace.json").write_text(json.dumps(parsed, indent=2) + "\n")
            residual = base64.b64decode(parsed["residual_base64"])
            (self.output / "import-residual.stderr.raw").write_bytes(residual)
            detail.update(trace_sha256=sha((self.output / "import-trace.json").read_bytes()),
                          trace_valid=parsed["valid"], trace_rows=len(parsed["rows"]),
                          residual_sha256=sha(residual), trace_errors=parsed["errors"],
                          largest_self_rows=sorted(parsed["rows"], key=lambda item: (-item["self_us"], item["index"]))[:30],
                          relevant_subtrees=[item for item in parsed["rows"] if
                                             item["module"].startswith("floati") or item["module"].startswith("multiprocessing")])
            # Preserve parsing products before strict decoding or any parity assertion.
            detail["stdout_matches_native_text"] = stdout.decode("utf-8") == reference["stdout"]
            detail["residual_matches_native_text"] = residual.decode("utf-8") == reference["stderr"]
            require(parsed["valid"] and detail["stdout_matches_native_text"] and detail["residual_matches_native_text"] and
                    row["returncode"] == reference["returncode"] == 20, "import trace or native parity invalid")
            raw = ordinary(sidecar).read_bytes()
            require(len(raw) <= 65536, "import runtime sidecar unbounded")
            actual = ast.literal_eval(raw.decode("utf-8"))
            reported = json.loads(json.dumps(actual))
            selected_reports = [item["reported"] for item in prior["observations"] if item["route"] == "S"]
            expected_runtime = selected_reports[0]
            require(len(selected_reports) == 3 and all(item == expected_runtime for item in selected_reports),
                    "accepted selected-route runtime differs")
            detail["reported_runtime"] = reported
            require(reported == expected_runtime and reported["flags"] == [1, 1, 1, 1] and
                    reported["version"].startswith("3.9.6 ") and
                    str(Path(reported["executable"]).resolve(strict=True)) ==
                    "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9" and
                    reported["package_file"] == str(self.root / "floati/__init__.py") and
                    reported["cli_file"] == str(self.root / "floati/cli.py"), "import runtime metadata differs")
            require(len(self.calls) == len(self.children) == len(row["children"]) == 1 and
                    row["children"][0]["args"] == argv and row["children"][0]["returncode"] == 20,
                    "import child count, invocation or collection differs")
        finally:
            if os.path.lexists(sidecar):
                raw = ordinary(sidecar).read_bytes()
                (self.output / "import-runtime.literal").write_bytes(raw)
                detail["sidecar_sha256"] = sha(raw)
            detail["executable_after"] = system_identity(selected)
            detail["manual_after"] = system_identity(manual)
            detail["complete_current_environment_after_sha256"] = digest(environment)
            detail["controller_environment_after_sha256"] = digest(dict(os.environ))
            unchanged = (detail["executable_after"] == executable_before and detail["manual_after"] == manual_before and
                         digest(environment) == environment_digest and digest(self.environment) == environment_digest and
                         digest(dict(os.environ)) == controller_environment_digest)
            self.checks["import_environment_executable_unchanged"] = unchanged
            require(unchanged, "import environment/executable changed")

    def help_conflict(self):
        private = self.copied_source()
        target = private / "floati/helptext.py"
        original = target.read_bytes()
        tree = ast.parse(original)
        assignments = [node for node in tree.body if isinstance(node, ast.AnnAssign)
                       and isinstance(node.target, ast.Name) and node.target.id == "HELP"]
        require(len(assignments) == 1, "HELP producer assignment changed")
        point = sum(len(line) for line in original.splitlines(keepends=True)[:assignments[0].lineno - 1])
        changed = original[:point] + b"register('help.root', 'startup conflict control', 'CLI help')\n" + original[point:]
        states = []
        try:
            for label in ("pristine", "conflict", "restored"):
                target.write_bytes(changed if label == "conflict" else original)
                state = {"label": label, "helptext_sha256": sha(target.read_bytes()),
                         "producer": package_files(private), "calls": []}
                states.append(state)
                state["calls"].append(self.launcher(["effects", "--record", "hostile"], label + ":refusal", private))
                state["calls"].append(self.launcher(["--help"], label + ":help", private))
                environment = dict(self.environment)
                environment.pop("PYTHONPATH", None)
                state["calls"].append(self.launch([self.interpreter["selected_path"], "-B", "-m", "floati.copy"],
                                                  label=label + ":catalog", cwd=private, environment=environment))
        finally:
            target.write_bytes(original)
            self.details["help_conflict"] = {"private_source": str(private), "states": states,
                                              "source_restored": package_files(private) == package_files(self.root),
                                              "mutation": "one real conflicting help.root registration before HELP"}
        require(self.details["help_conflict"]["source_restored"], "copied help source not restored")
        pristine, conflict, restored = states
        require([triple(row) for row in pristine["calls"]] == [triple(row) for row in restored["calls"]],
                "restored copied help output differs")
        require([row["returncode"] for row in pristine["calls"]] == [20, 0, 0], "pristine help control failed")
        baseline = self.baseline_native()
        require(triple(pristine["calls"][0]) == triple(baseline["cold:0:effect-refusal"]), "copied refusal differs")
        for index, row in enumerate(conflict["calls"]):
            if index == 0 and self.stage == "c1":
                require(triple(row) == triple(pristine["calls"][0]), "deferred broken-help boundary failed")
            else:
                require(row["returncode"] != 0 and
                        "ValueError: copy key 'help.root' was registered with different content" in row["stderr"],
                        "real help conflict did not propagate honestly")

    def parser_control(self):
        require(self.stage == "c2", "parser producer control belongs only to C2")
        private = self.copied_source()
        target = private / "floati/cli.py"
        original = target.read_bytes()
        matches = [node for node in ast.walk(ast.parse(original)) if isinstance(node, ast.Assign)
                   and any(isinstance(item, ast.Name) and item.id == "effect_show" for item in node.targets)
                   and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                   and node.value.func.attr == "add_parser" and len(node.value.args) == 1
                   and isinstance(node.value.args[0], ast.Constant) and node.value.args[0].value == "show"]
        require(len(matches) == 1, "effect-show parser producer seam changed")
        lines = original.splitlines(keepends=True)
        value = matches[0].value.args[0]
        begin = sum(map(len, lines[:value.lineno - 1])) + value.col_offset
        end = sum(map(len, lines[:value.end_lineno - 1])) + value.end_col_offset
        changed = original[:begin] + b'"show-probe"' + original[end:]
        states = []
        try:
            for label in ("pristine", "changed", "restored"):
                target.write_bytes(changed if label == "changed" else original)
                state = {"label": label, "cli_sha256": sha(target.read_bytes()), "producer": package_files(private),
                         "calls": [self.launcher(["effect", "show", "--help"], label + ":help", private),
                                   self.launcher(["describe", "--json"], label + ":describe", private)]}
                states.append(state)
        finally:
            target.write_bytes(original)
            self.details["parser_control"] = {"private_source": str(private), "states": states,
                                               "source_restored": package_files(private) == package_files(self.root),
                                               "mutation": "one AST-selected actual show registration becomes show-probe"}
        require(self.details["parser_control"]["source_restored"], "copied parser source not restored")
        pristine, changed, restored = states
        require([triple(row) for row in pristine["calls"]] == [triple(row) for row in restored["calls"]], "parser restoration output differs")
        require([row["returncode"] for row in pristine["calls"]] == [0, 0], "pristine parser producer failed")
        refused, described = changed["calls"]
        artifact = json.loads(refused["stdout"])
        require(refused["returncode"] == 20 and refused["stderr"] == "" and artifact["schema_version"] == 1 and
                artifact["evidence"]["code"] == "arguments_invalid", "lost real registration did not reach typed fallback")
        require(described["returncode"] == 0 and described["stderr"] == "" and
                'show-probe' in described["stdout"] and triple(described) != triple(pristine["calls"][1]),
                "describe did not witness actual changed registry")

    def cover(self, suite, runner):
        from tests import test_cli, test_effect_cli
        owners = (test_cli.SlipCliTests, test_effect_cli.EffectCliTests)
        calls, fixtures = [], []
        def wrap(original):
            def observed(case, *args, **kwargs):
                if case.id() not in HELP_METHODS:
                    return original(case, *args, **kwargs)
                first = len(self.children)
                began = time.monotonic_ns()
                value = original(case, *args, **kwargs)
                calls.append({"test": case.id(), "argv": list(args), **triple(vars(value)),
                              "elapsed_ns": time.monotonic_ns() - began,
                              "children": [{"pid": child.pid, "returncode": child.returncode} for child in self.children[first:]]})
                fixtures.append(case.temp.name)
                return value
            return observed
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(subprocess, "Popen", self.collect_popen))
            for owner in owners:
                stack.enter_context(mock.patch.object(owner, "run_cli", wrap(owner.run_cli)))
            result = runner.run(suite)
        expected = {tuple(row["argv"]): triple(row) for row in self.help_bank["calls"]}
        self.details["help_cover"] = {"calls": calls, "fixtures_removed": {path: not os.path.lexists(path) for path in fixtures},
                                      "expected_topics": [list(key) for key in expected]}
        require(result.testsRun == len(self.selection) == 82, "cover population differs")
        require(len(calls) == 42 and len({tuple(row["argv"]) for row in calls}) == 42, "cover help population differs")
        for row in calls:
            require(tuple(row["argv"]) in expected and triple(row) == expected[tuple(row["argv"])], "cover complete help output differs")
            require(len(row["children"]) == 1 and row["children"][0]["returncode"] == 0, "cover help child collection differs")
        require(all(self.details["help_cover"]["fixtures_removed"].values()), "cover help fixture remains")
        return result

    def finish(self, result):
        all_collected = all(child.returncode is not None for child in self.children)
        for path, identity in reversed(self.owned):
            row = {"path": str(path), "collected_before_cleanup": all_collected, "removed": False}
            self.cleanups.append(row)
            try:
                info = ordinary(path, directory=True).lstat()
                require((info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)) == identity,
                        "owned fixture identity changed")
                if all_collected:
                    shutil.rmtree(path)
                row["removed"] = not os.path.lexists(path)
            except Exception as exc:
                row["error"] = error_record(exc)
        after = source_identity(self.root)
        self.checks.update(source_unchanged=self.before == after,
                           native_children_collected=all_collected,
                           owned_fixtures_removed=all(row["removed"] for row in self.cleanups),
                           unittest_returned=result is not None,
                           unittest_successful=result is not None and result.wasSuccessful(),
                           no_outer_skips=result is not None and not result.skipped)
        valid = all(self.checks.values())
        document = {"schema_version": 1, "alias": self.alias, "stage": self.stage, "operation": self.operation,
                    "valid": valid, "qualified": False, "checks": self.checks, "selection": self.selection,
                    "source_before": self.before, "source_after": after, "admission": self.admission,
                    "environment": {key: self.environment.get(key) for key in SAFE_ENV},
                    "interpreter_selection": self.interpreter,
                    "controller_interpreter": {"executable": sys.executable, "resolved": str(Path(sys.executable).resolve()), "version": sys.version},
                    "calls": self.calls, "details": self.details, "cleanup": self.cleanups,
                    "children": [{"pid": child.pid, "args": child.args, "returncode": child.returncode} for child in self.children],
                    "limits": ["Full decoded output parity is not raw-byte or exhaustive encoding parity.",
                               "Fresh-process observations do not establish cold filesystem caches or quiet host load.",
                               "Attribution bootstrap is separate from the literal unchanged shell launcher.",
                               "Only retained Popen children are counted; this is not an exhaustive descendant census.",
                               "Private evidence bank homes and tool caches may remain; only named owned fixtures are removed."]}
        cli = (self.root / "floati/cli.py").read_bytes()
        (self.output / "source-cli.py").write_bytes(cli)
        (self.output / "source-manifest.json").write_bytes((self.root / "bundle-manifest.v0.json").read_bytes())
        patch = "".join(difflib.unified_diff(self.baseline_cli.decode().splitlines(keepends=True),
                                           cli.decode().splitlines(keepends=True), fromfile="C0/floati/cli.py", tofile=self.stage + "/floati/cli.py"))
        (self.output / "stage-diff.patch").write_text(patch)
        document["retained_source"] = {name: sha((self.output / name).read_bytes())
                                       for name in ("source-cli.py", "source-manifest.json", "stage-diff.patch")}
        (self.output / "startup-observation.json").write_text(json.dumps(document, indent=2) + "\n")
        return valid


class StartupDiagnostic(unittest.TestCase):
    def test_native(self):
        _ACTIVE.native()

    def test_attribution(self):
        _ACTIVE.attribution()

    def test_help_conflict(self):
        _ACTIVE.help_conflict()

    def test_parser_control(self):
        _ACTIVE.parser_control()

    def test_interpreter_route(self):
        _ACTIVE.interpreter_route()

    def test_import_attribution(self):
        _ACTIVE.import_attribution()


def observe_startup(root, output, alias, suite, runner):
    global _ACTIVE
    require(_ACTIVE is None, "nested startup observation")
    session = Session(root, output, alias, suite)
    result = None
    _ACTIVE = session
    try:
        result = session.cover(suite, runner) if session.operation == "cover" else runner.run(suite)
    finally:
        _ACTIVE = None
        valid = session.finish(result)
    return result, valid


def probe(name, output):
    arguments = dict(PROBE_CASES)[name]
    ordinary(output.parent, directory=True)
    require(Path("\x2fprivate/tmp") in output.parents and not os.path.lexists(output), "probe output must be absent and private")
    names = ("floati.helptext", "floati.generated_help")
    before_import = {key: key in sys.modules for key in names}
    from floati import cli
    original = cli._parser
    parsers, intervals = [], []
    def delegated(*args, **kwargs):
        began = time.monotonic_ns()
        value = original(*args, **kwargs)
        intervals.append(time.monotonic_ns() - began)
        parsers.append(value)
        return value
    before_main = {key: key in sys.modules for key in names}
    cli._parser = delegated
    code, failure = None, None
    try:
        code = cli.main(arguments)
        return code
    except BaseException as exc:
        failure = error_record(exc)
        raise
    finally:
        cli._parser = original
        document = {"case": name, "argv": arguments, "returncode": code, "exception": failure,
                    "observed_running_interpreter": {"executable": sys.executable,
                        "resolved": str(Path(sys.executable).resolve()), "version": sys.version},
                    "cli_file": cli.__file__, "cli_sha256": sha(Path(cli.__file__).read_bytes()),
                    "before_import_modules": before_import, "before_main_modules": before_main,
                    "after_main_modules": {key: key in sys.modules for key in names},
                    "parser_count": len(parsers), "parser_ids": [id(value) for value in parsers],
                    "parser_intervals_ns": intervals, "parser_binding_restored": cli._parser is original}
        with output.open("x") as stream:
            stream.write(json.dumps(document, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("operation", choices=("_probe",))
    parser.add_argument("--case", choices=tuple(dict(PROBE_CASES)), required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    sys.exit(probe(options.case, options.output))
