"""Parent-only, descriptor-bound banking of exact files in an existing lane row."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import selectors
import time

from .adhoc_profiles import AdhocProfiles
from .errors import ProtocolRefusal
from .git_process import fixed_git_command, fixed_git_environment


_BORROWED_GIT_PRELUDE = (
    "import os,stat,sys\n"
    "try:\n"
    " if len(sys.argv)<5: raise ValueError('invalid Git launch')\n"
    " fd,device,inode=(int(value) for value in sys.argv[1:4])\n"
    " metadata=os.fstat(fd)\n"
    " if fd<3 or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=os.geteuid() or (metadata.st_dev,metadata.st_ino)!=(device,inode): raise ValueError('Git directory identity changed')\n"
    " if sys.argv[4]!='/usr/bin/git': raise ValueError('invalid Git executable')\n"
    " os.fchdir(fd)\n"
    " os.close(fd)\n"
    " os.execve('/usr/bin/git',sys.argv[4:],os.environ)\n"
    "except BaseException: raise SystemExit(126)\n"
)


def _borrowed_git_launch(common_descriptor, common_identity, arguments, *, index=None):
    """Produce a closed fresh exec; only its child changes cwd to the held inode."""
    environment = fixed_git_environment("/usr/bin/git")
    environment["GIT_AUTHOR_NAME"] = environment["GIT_COMMITTER_NAME"] = "Floati"
    environment["GIT_AUTHOR_EMAIL"] = environment["GIT_COMMITTER_EMAIL"] = "floati@localhost"
    if index is not None:
        environment["GIT_INDEX_FILE"] = str(index)
    git_command = fixed_git_command("/usr/bin/git", Path("."), (
        "--literal-pathspecs", "--git-dir=.", "--bare", *arguments))
    launch_command = [os.path.realpath(sys.executable), "-I", "-S", "-B", "-c",
        _BORROWED_GIT_PRELUDE, str(common_descriptor), str(common_identity["dev"]),
        str(common_identity["ino"]), *git_command]
    return launch_command, environment


def _refuse(code, detail):
    raise ProtocolRefusal(code, detail,
        remedy="preserve the borrowed row and exact receipts; restore its binding before retrying")


def validate_borrowed_binding(value):
    if type(value) is not dict or set(value) != {
        "profile_id", "lane_opening_id", "node_lease_id", "workspace"}:
        _refuse("adhoc_binding_changed", "borrowed binding must repeat its closed native coordinates")
    from .adhoc_records import UUID, _path
    for field, prefix in (("profile_id", "adhoc-profile-"),
                          ("lane_opening_id", "lane-workspace-"), ("node_lease_id", "lease-")):
        if type(value[field]) is not str or re.fullmatch(re.escape(prefix) + UUID, value[field]) is None:
            _refuse("adhoc_binding_changed", "borrowed binding has an invalid native reference")
    _path(value["workspace"], _refuse)
    return deepcopy(value)


class BorrowedWorkspace:
    """Pins never own the row's deletion; only launch scratch is disposable."""

    def __init__(self, root, binding):
        self.root = root
        self.binding = validate_borrowed_binding(binding)
        self.profiles = AdhocProfiles(root)
        self._fds = {}
        self._pin = None
        self.__settlement_attestation = None
        self.__settlement_pending = None

    def close(self):
        for descriptor in self._fds.values():
            os.close(descriptor)
        self._fds.clear()
        self._pin = None
        self.__settlement_attestation = None

    def _open(self, name, path, *, directory=False):
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if directory:
            flags |= os.O_DIRECTORY
        descriptor = os.open(path, flags)
        self._fds[name] = descriptor
        return os.fstat(descriptor)

    def _check(self, *, clean=False):
        try:
            with self.profiles.validated(self.binding["profile_id"], require_clean=clean) as (profile, _lane):
                for field in ("lane_opening_id", "node_lease_id", "workspace"):
                    if profile[field] != self.binding[field]:
                        _refuse("adhoc_binding_changed", "borrowed binding differs from its native profile")
                if self._pin is not None:
                    if profile != self._pin["profile"]:
                        _refuse("adhoc_binding_changed", "profile changed after pin")
                    self._check_pinned_files()
                return deepcopy(profile)
        except (OSError, ValueError) as exc:
            raise ProtocolRefusal("adhoc_binding_changed", "borrowed administrative binding is unavailable") from exc

    def _check_pinned_files(self):
        # Descriptor checks take no fleet locks, including while a prepared Git
        # transaction owns the ref lock. Avoid inverting lane/ref lock order.
        for name, path in self._pin["paths"].items():
            actual = os.lstat(path)
            held = os.fstat(self._fds[name])
            if (actual.st_dev, actual.st_ino) != (held.st_dev, held.st_ino):
                _refuse("adhoc_binding_changed", "borrowed administrative identity changed")
        if self._read_fd("marker") != self._pin["marker"]:
            _refuse("adhoc_binding_changed", "borrowed ownership marker changed")
        if self._read_fd("gitfile") != self._pin["gitfile"]:
            _refuse("adhoc_binding_changed", "borrowed Git pointer changed")
        if "index" in self._pin and self._read_fd("index") != self._pin["index"]:
            _refuse("adhoc_binding_changed", "initial row index changed during the turn")
        if self._read_fd("common_config") != self._pin["common_config"]:
            _refuse("adhoc_binding_changed", "borrowed repository configuration changed")

    def _read_fd(self, name):
        descriptor = self._fds[name]
        size = os.fstat(descriptor).st_size
        if size > 16 * 1024 * 1024:
            _refuse("adhoc_binding_changed", "borrowed administrative file exceeds its bound")
        os.lseek(descriptor, 0, os.SEEK_SET)
        data = os.read(descriptor, size + 1)
        if len(data) != size:
            _refuse("adhoc_binding_changed", "borrowed administrative file changed during read")
        return data

    def pin(self):
        if self._pin is not None:
            self._check()
            return {"expected_head": self._pin["head"], "binding": deepcopy(self.binding)}
        return self._pin_workspace(clean=True)

    def _pin_workspace(self, *, clean):
        profile = self._check(clean=clean)
        workspace = Path(profile["workspace"])
        paths = {"workspace": workspace, "admin": Path(profile["git_admin_dir"]),
                 "common": Path(profile["git_common_dir"]), "gitfile": workspace / ".git",
                 "marker": Path(profile["git_admin_dir"]) / "floati-lane-owner.json",
                 "index": Path(profile["git_admin_dir"]) / "index",
                 "common_config": Path(profile["git_common_dir"]) / "config",
                 "head_file": Path(profile["git_admin_dir"]) / "HEAD"}
        try:
            for name, path in paths.items():
                info = self._open(name, path, directory=name in {"workspace", "admin", "common"})
                if info.st_uid != os.geteuid():
                    _refuse("adhoc_binding_changed", "borrowed inode is foreign")
            self._pin = {"profile": profile, "paths": paths,
                "marker": self._read_fd("marker"), "gitfile": self._read_fd("gitfile"),
                "common_config": self._read_fd("common_config")}
            self._check(clean=clean)
            head = self._git("rev-parse", "refs/heads/" + profile["branch"]).decode().strip()
            self._pin["head"] = head
            self._pin["index"] = self._read_fd("index")
            return {"expected_head": head, "binding": deepcopy(self.binding)}
        except BaseException:
            self.close()
            self._pin = None
            raise

    def recover_pin(self, dispatch_id):
        """Recover only a journal-proved banking operation, never a new turn."""
        from .adhoc_journal import AdhocJournal

        journal = AdhocJournal(self.root)
        admission = journal.show(dispatch_id)
        history = journal.lifecycle(dispatch_id)
        phases = {(row["phase"], row["evidence"].get("stage")): row["evidence"] for row in history}
        reservation = phases.get(("spawn_reserved", None))
        cleanup = phases.get(("process_closed", "cleanup"), {}).get("cleanup")
        terminal = phases.get(("provider_terminal", None), {}).get("event")
        intent = phases.get(("process_closed", "banking_intent"))
        if (admission["profile_id"] != self.binding["profile_id"]
            or reservation is None or cleanup is None
            or terminal is None or terminal["outcome"] != "success"
            or cleanup["alive_after_cleanup"] or cleanup["adapter_pid"] is None
            or cleanup["isolation_backend"] is None
            or any((phase, None) in phases for phase in ("failed", "unknown", "teardown_completed"))
            or reservation["native_context"]["workspace_binding"] != self.binding
            or intent is not None and reservation["workspace_pin"] != intent["workspace_pin"]):
            _refuse("adhoc_binding_changed", "banking recovery requires its exact reserved pin, terminal, and cleanup intent")
        saved = reservation["workspace_pin"]
        files = None if intent is None else self._expected_files(intent["files"])
        settlement = phases.get(("process_closed", "index_settlement_intent"))
        settled = phases.get(("process_closed", "index_settled"))
        result = phases.get(("process_closed", "result"), {}).get("result")
        if settled is not None and settlement is None:
            _refuse("adhoc_binding_changed", "settled index lacks its durable settlement intent")
        if settlement is not None:
            self._validate_index_settlement(settlement)
            if (result is None or settlement["head_sha"] != result["sha"]
                or settlement["head_sha"] == saved["expected_head"]
                or settlement["index_sha256_before"] != saved["index_sha256"]
                or settlement["remote_ref"] != "refs/heads/" + self._profile_branch(admission)
                or settled is not None and settled != dict(settlement, stage="index_settled")):
                _refuse("adhoc_binding_changed", "index settlement differs from its original dispatch and result")
        try:
            if self._pin is None:
                self._pin_workspace(clean=False)
            self._check()
            current = self._git("rev-parse", "refs/heads/" + self._pin["profile"]["branch"]).decode().strip()
            accepted_indexes = {saved["index_sha256"]}
            if settlement is not None:
                if (current != settlement["head_sha"] or self._settlement_origin() != settlement["remote_coordinate"]):
                    _refuse("adhoc_binding_changed", "settlement recovery differs from its exact HEAD and remote")
                accepted_indexes.add(settlement["index_sha256_after"])
                if settled is not None:
                    accepted_indexes = {settlement["index_sha256_after"]}
            if (hashlib.sha256(self._read_fd("index")).hexdigest() not in accepted_indexes
                or self._pin["head"] not in {saved["expected_head"],
                    self._git("rev-parse", "refs/heads/" + self._pin["profile"]["branch"]).decode().strip()}):
                _refuse("adhoc_binding_changed", "banking recovery differs from its reserved branch or original index")
            current = self._git("rev-parse", "refs/heads/" + self._pin["profile"]["branch"]).decode().strip()
            if current == saved["expected_head"]:
                for path, expected in (() if files is None else files.items()):
                    mode, data = self._result(path)
                    self._require_expected_file(mode, data, expected)
                completed = None
            else:
                if files is None:
                    _refuse("adhoc_binding_changed", "advanced banking recovery requires its prior immutable file intent")
                self._require_intended_child(current, saved["expected_head"], files)
                completed = current
            self._pin["head"] = saved["expected_head"]
            self._check()
            return {"expected_head": saved["expected_head"], "binding": deepcopy(self.binding),
                    "completed_sha": completed}
        except BaseException:
            self.close()
            raise

    def _profile_branch(self, admission):
        matches = [row for row in self.profiles.records() if row["id"] == admission["profile_id"]
            and row["digest"] == admission["profile_digest"]]
        if len(matches) != 1:
            _refuse("adhoc_binding_changed", "settlement requires its exact admitted profile")
        return matches[0]["branch"]

    @staticmethod
    def _validate_index_settlement(value):
        fields = {"stage", "head_sha", "remote_coordinate", "remote_ref", "remote_sha",
            "index_sha256_before", "index_sha256_after", "remote_evidence_digest"}
        if (type(value) is not dict or set(value) != fields
            or value.get("stage") not in {"index_settlement_intent", "index_settled"}
            or any(type(value.get(field)) is not str for field in fields)
            or re.fullmatch(r"[0-9a-f]{40}", value["head_sha"]) is None
            or value["remote_sha"] != value["head_sha"]
            or any(re.fullmatch(r"[0-9a-f]{64}", value[field]) is None for field in
                ("index_sha256_before", "index_sha256_after", "remote_evidence_digest"))):
            _refuse("adhoc_binding_changed", "settlement requires its exact bounded native evidence")

    def _settlement_origin(self):
        from .effect_reconciliation_protocol import classify_remote_coordinate
        try:
            raw = self._git("config", "--no-includes", "--null", "--file", "config",
                "--get-all", "remote.origin.url", maximum=4096)
            coordinate = raw[:-1].decode("utf-8", "strict")
        except (ProtocolRefusal, UnicodeError) as exc:
            raise ProtocolRefusal("adhoc_result_unbanked", "settlement requires one exact readable origin coordinate",
                remedy="bank the publication commit on the exact origin row ref before retrying") from exc
        if (not raw.endswith(b"\0") or raw.count(b"\0") != 1
            or classify_remote_coordinate(coordinate) is None):
            _refuse("adhoc_result_unbanked", "settlement requires one supported explicit origin coordinate")
        return coordinate

    def _settlement_remote(self, dispatch_id, head_sha):
        from .effect_reconciliation_observer import _remote_result
        from .effect_reconciliation_protocol import build_request, validate_result
        from .adhoc_records import UUID
        match = re.fullmatch(r"adhoc-dispatch-(" + UUID + r")", dispatch_id)
        if match is None:
            _refuse("adhoc_binding_changed", "settlement requires its exact native dispatch")
        coordinate = self._settlement_origin()
        reference = "refs/heads/" + self._pin["profile"]["branch"]
        # Correlation-only identifiers append no effect record or authority.
        suffix = match.group(1)
        request = build_request(operation_id="effect-op-" + suffix,
            current_evidence_id="effect-unknown-" + suffix, adapter="git_remote_explicit",
            target={"kind": "git_remote_ref", "coordinate": coordinate,
                "identity_digest": hashlib.sha256(coordinate.encode()).hexdigest()},
            expected_confirmation={"kind": "git_remote_ref_equals", "locator": reference,
                "expected_digest": head_sha}, budget_claim={"git": 1},
            local_repository_identity=None, request_id=suffix)
        observed = validate_result(_remote_result(request, None), request)
        if (observed.outcome != "unknown" or observed.reason_code != "reconciliation_inconclusive"
            or observed.observation is None or observed.observation.get("observed_ref_digest") != head_sha
            or self._settlement_origin() != coordinate):
            _refuse("adhoc_result_unbanked", "exact origin branch readback does not match the saved result")
        return {"remote_coordinate": coordinate, "remote_ref": reference,
            "remote_sha": head_sha, "remote_evidence_digest": observed.evidence_digest}

    def _settlement_rows(self, dispatch_id):
        from .adhoc_journal import AdhocJournal
        journal = AdhocJournal(self.root)
        admission = journal.show(dispatch_id)
        if admission["profile_id"] != self.binding["profile_id"]:
            _refuse("adhoc_binding_changed", "settlement belongs to a different borrowed profile")
        phases = {(row["phase"], row["evidence"].get("stage")): row["evidence"]
            for row in journal.lifecycle(dispatch_id)}
        return admission, phases

    def _settlement_head(self, dispatch_id):
        recovered = self.recover_pin(dispatch_id)
        _admission, phases = self._settlement_rows(dispatch_id)
        result = phases.get(("process_closed", "result"), {}).get("result")
        if (result is None or recovered["completed_sha"] != result["sha"]
            or self._git("rev-parse", "refs/heads/" + self._pin["profile"]["branch"]).decode().strip() != result["sha"]
            or self._git("symbolic-ref", "--quiet", "refs/heads/" + self._pin["profile"]["branch"])):
            _refuse("adhoc_binding_changed", "settlement requires the exact finalized direct branch")
        return result["sha"], phases

    @contextmanager
    def _settlement_index(self, head_sha):
        with tempfile.TemporaryDirectory(prefix="floati-adhoc-settlement-") as scratch:
            index = Path(scratch) / "index"
            self._git("-c", "core.splitIndex=false", "-c", "index.version=2",
                "read-tree", "--no-sparse-checkout", head_sha, index=index)
            metadata = index.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid() or metadata.st_size > 16 * 1024 * 1024):
                _refuse("adhoc_binding_changed", "generated settlement index is not an owned bounded file")
            data = index.read_bytes()
            if len(data) != metadata.st_size:
                _refuse("adhoc_binding_changed", "generated settlement index changed during reading")
            self._require_head_worktree(head_sha, index)
            yield index, data

    def _require_head_worktree(self, head_sha, index):
        # Compare raw blob identities without letting .gitattributes select a
        # configured clean filter or another external process in the parent.
        tree = self._git("ls-tree", "-r", "-z", head_sha)
        entries = [item for item in tree.split(b"\0") if item]
        if len(entries) > 10000:
            _refuse("adhoc_binding_changed", "settlement tree exceeds its bounded file census")
        expected_paths = set()
        expected_directories = {"."}
        for entry in entries:
            try:
                metadata, path = entry.split(b"\t", 1)
                mode, kind, oid = metadata.decode("ascii").split()
                relative = path.decode("utf-8", "strict")
                expected_paths.add(relative)
                expected_directories.update(str(parent) for parent in Path(relative).parents)
                if mode not in {"100644", "100755"} or kind != "blob":
                    _refuse("adhoc_binding_changed", "settlement requires ordinary unchanged HEAD files")
                actual_mode, data = self._result(relative)
                actual_oid = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
            except (ValueError, UnicodeError) as exc:
                raise ProtocolRefusal("adhoc_binding_changed", "settlement tree has an invalid file coordinate") from exc
            if actual_mode != mode or actual_oid != oid:
                _refuse("adhoc_binding_changed", "settlement cannot discard unbanked worktree changes")
        # Walk the inherited directory descriptor: macOS does not expose directory
        # children through /dev/fd, and no Git attribute/ignore filter may execute.
        population = 0
        try:
            for directory, folders, filenames, descriptor in os.fwalk(".", follow_symlinks=False,
                    dir_fd=self._fds["workspace"]):
                relative_directory = Path(directory).as_posix().removeprefix("./")
                for name in [*folders, *filenames]:
                    if directory == "." and name == ".git":
                        continue
                    population += 1
                    relative = (Path(relative_directory) / name).as_posix()
                    metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if (population > 20000 or stat.S_ISLNK(metadata.st_mode)
                        or stat.S_ISDIR(metadata.st_mode) and relative not in expected_directories
                        or not stat.S_ISDIR(metadata.st_mode) and relative not in expected_paths):
                        _refuse("adhoc_binding_changed", "settlement cannot discard untracked or ignored worktree files")
        except OSError as exc:
            raise ProtocolRefusal("adhoc_binding_changed", "settlement worktree census changed or is unavailable") from exc

    def prepare_index_settlement(self, dispatch_id):
        head_sha, phases = self._settlement_head(dispatch_id)
        prior = phases.get(("process_closed", "index_settlement_intent"))
        with self._settlement_index(head_sha) as (_index, data):
            reservation = phases[("spawn_reserved", None)]["workspace_pin"]
            evidence = {"stage": "index_settlement_intent", "head_sha": head_sha,
                "index_sha256_before": reservation["index_sha256"],
                "index_sha256_after": hashlib.sha256(data).hexdigest(),
                **self._settlement_remote(dispatch_id, head_sha)}
            self._validate_index_settlement(evidence)
            if prior is not None and prior != evidence:
                _refuse("adhoc_binding_changed", "settlement preparation differs from its durable intent")
            self._check()
            self.__settlement_attestation = (dispatch_id, deepcopy(evidence))
            return deepcopy(evidence)

    def apply_index_settlement(self, dispatch_id, intent):
        self._validate_index_settlement(intent)
        if intent["stage"] != "index_settlement_intent":
            _refuse("adhoc_binding_changed", "index application requires a settlement intent")
        head_sha, phases = self._settlement_head(dispatch_id)
        if phases.get(("process_closed", "index_settlement_intent")) != intent or head_sha != intent["head_sha"]:
            _refuse("adhoc_binding_changed", "index application requires its exact durable native intent")
        with self._settlement_index(head_sha) as (temporary_index, data):
            if hashlib.sha256(data).hexdigest() != intent["index_sha256_after"]:
                _refuse("adhoc_binding_changed", "generated index differs from the durable settlement intent")
            if self._settlement_remote(dispatch_id, head_sha) != {field: intent[field] for field in
                ("remote_coordinate", "remote_ref", "remote_sha", "remote_evidence_digest")}:
                _refuse("adhoc_result_unbanked", "settlement remote readback changed after preparation")
            current_digest = hashlib.sha256(self._read_fd("index")).hexdigest()
            if current_digest not in {intent["index_sha256_before"], intent["index_sha256_after"]}:
                _refuse("adhoc_binding_changed", "row index differs from both durable settlement states")
            if current_digest != intent["index_sha256_after"]:
                self.__settlement_pending = (head_sha, temporary_index, data, intent)
                try:
                    self._update_ref("refs/heads/" + self._pin["profile"]["branch"],
                        head_sha, head_sha, before_commit=self._replace_settlement_index,
                        verify_only=True)
                finally:
                    self.__settlement_pending = None
            self._check()
            completed = dict(intent, stage="index_settled")
            self.__settlement_attestation = (dispatch_id, deepcopy(completed))
            return completed

    def _replace_settlement_index(self):
        if self.__settlement_pending is None:
            _refuse("adhoc_binding_changed", "index replacement requires a live prepared native ref")
        head_sha, temporary_index, data, intent = self.__settlement_pending
        admin, lock, replaced = self._fds["admin"], None, False
        try:
            lock = os.open("index.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=admin)
            view = memoryview(data)
            while view:
                written = os.write(lock, view)
                if written <= 0:
                    _refuse("adhoc_binding_changed", "settlement index write was incomplete")
                view = view[written:]
            os.fsync(lock)
            self._check()
            if (hashlib.sha256(self._read_fd("index")).hexdigest() != intent["index_sha256_before"]
                or self._git("rev-parse", "refs/heads/" + self._pin["profile"]["branch"]).decode().strip() != head_sha
                or self._settlement_origin() != intent["remote_coordinate"]):
                _refuse("adhoc_binding_changed", "settlement binding changed before index replacement")
            self._require_head_worktree(head_sha, temporary_index)
            self._check_pinned_files()
            os.replace("index.lock", "index", src_dir_fd=admin, dst_dir_fd=admin)
            replaced = True
            old = self._fds["index"]
            self._fds["index"] = os.dup(lock)
            self._pin["index"] = data
            os.close(old)
            os.fsync(admin)
            self._check()
        except OSError as exc:
            raise ProtocolRefusal("adhoc_binding_changed", "native row index settlement is unavailable") from exc
        finally:
            if lock is not None:
                try:
                    if not replaced:
                        try:
                            named, held = os.stat("index.lock", dir_fd=admin, follow_symlinks=False), os.fstat(lock)
                            if (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino):
                                os.unlink("index.lock", dir_fd=admin)
                        except FileNotFoundError:
                            pass
                finally:
                    os.close(lock)

    def _index_settlement_attestation(self, dispatch_id, stage):
        """Attest only evidence produced by this exact pinned native owner."""
        # Called under the journal lock: descriptor checks do not reacquire
        # lower-ranked lane/profile locks. Native preparation/application owns
        # the branch and remote observations before this attestation is exposed.
        self._check_pinned_files()
        attestation = self.__settlement_attestation
        if attestation is None or attestation[0] != dispatch_id or attestation[1]["stage"] != stage:
            _refuse("adhoc_binding_changed", "settlement evidence lacks its exact native workspace owner")
        evidence = attestation[1]
        expected = evidence["index_sha256_after"] if stage == "index_settled" else evidence["index_sha256_before"]
        if hashlib.sha256(self._read_fd("index")).hexdigest() != expected:
            _refuse("adhoc_binding_changed", "settlement attestation differs from the live owned index and HEAD")
        return deepcopy(evidence)

    @staticmethod
    def _expected_files(values):
        if type(values) is not list or not 1 <= len(values) <= 32:
            _refuse("adhoc_binding_changed", "banking requires its exact bounded file intent")
        files = {}
        for value in values:
            try:
                path_size = len(value.get("path", "").encode("utf-8")) if type(value) is dict and type(value.get("path")) is str else 0
            except UnicodeError:
                _refuse("adhoc_binding_changed", "banking requires its exact bounded file intent")
            if (type(value) is not dict or set(value) != {"path", "mode", "sha256"}
                or type(value["path"]) is not str or not value["path"]
                or value["path"].startswith("/") or path_size > 4096
                or any(part.casefold() in {"", ".", "..", ".git"} for part in value["path"].split("/"))
                or any(unicodedata.category(c).startswith("C") for c in value["path"])
                or value["path"] in files or type(value["mode"]) is not str or value["mode"] not in {"100644", "100755"}
                or type(value["sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None):
                _refuse("adhoc_binding_changed", "banking requires its exact bounded file intent")
            files[value["path"]] = deepcopy(value)
        return files

    @staticmethod
    def _require_expected_file(mode, data, expected):
        if mode != expected["mode"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
            _refuse("adhoc_binding_changed", "result bytes or mode differ from the durable banking intent")

    def _require_intended_child(self, current, expected_head, files):
        parents = self._git("rev-list", "--parents", "-n", "1", current).decode().split()
        if parents != [current, expected_head]:
            _refuse("adhoc_binding_changed", "advanced banking branch is not the exact reserved direct child")
        changed = self._git("diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z",
                            expected_head, current).split(b"\0")
        if changed[-1:] == [b""]:
            changed.pop()
        if not set(changed) <= {path.encode("utf-8") for path in files}:
            _refuse("adhoc_binding_changed", "advanced banking commit contains changes outside its exact file intent")
        for path, expected in files.items():
            tree = self._git("ls-tree", "-z", current, "--", path)
            try:
                header, named = tree[:-1].split(b"\t", 1)
                mode, kind, blob = header.decode("ascii").split()
                if (not tree.endswith(b"\0") or named != path.encode("utf-8") or kind != "blob"
                    or mode != expected["mode"] or re.fullmatch(r"[0-9a-f]{40}", blob) is None):
                    raise ValueError()
                size = int(self._git("cat-file", "-s", blob).strip())
                if not 0 <= size <= 16 * 1024 * 1024:
                    raise ValueError()
                data = self._git("cat-file", "blob", blob, maximum=16 * 1024 * 1024)
                if len(data) != size:
                    raise ValueError()
            except (ValueError, UnicodeError) as exc:
                raise ProtocolRefusal("adhoc_binding_changed", "advanced banking commit lacks its exact ordinary intended blob") from exc
            self._require_expected_file(mode, data, expected)

    def _git(self, *arguments, input=None, index=None, timeout=30, maximum=8 * 1024 * 1024):
        # Only the common directory descriptor crosses into the fresh child.
        # Git uses its child-only inode-bound cwd, never a mutable row path.
        # This exact read-only probe is used while update-ref holds its native
        # prepared lock. Inspect identity before lane revalidation can refuse a
        # symbolic replacement; every mutating command keeps both checks.
        symbolic_probe = arguments == ("symbolic-ref", "--quiet",
            "refs/heads/" + self._pin["profile"]["branch"])
        if not symbolic_probe:
            self._check()
        try:
            command, environment = _borrowed_git_launch(self._fds["common"],
                self._pin["profile"]["filesystem_bindings"]["git_common_dir"], arguments, index=index)
            result = subprocess.run(command, env=environment, input=input, capture_output=True,
                check=False, timeout=timeout, close_fds=True, pass_fds=(self._fds["common"],))
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProtocolRefusal("adhoc_binding_changed", "parent Git banking is unavailable") from exc
        if (result.returncode not in ({0, 1} if symbolic_probe else {0})
            or len(result.stdout) > maximum or len(result.stderr) > 1024 * 1024):
            _refuse("adhoc_binding_changed", "parent Git binding or expected-old ref changed")
        if not symbolic_probe:
            self._check()
        return result.stdout

    def _update_ref(self, reference, new_head, expected_head, *, before_commit=None, verify_only=False):
        """Keep Git's prepared no-deref lock across direct-ref identity check.

        no-deref alone would overwrite a replacement symref. Preparing first
        locks the named ref and verifies its old OID; while that lock is held,
        reject symbolic identity, then commit. Native Git writers cannot replace
        the ref between this check and commit. EOF/abort releases native locks.
        """
        self._check()
        if (reference != "refs/heads/" + self._pin["profile"]["branch"]
            or (not verify_only and expected_head != self._pin["head"])
            or (verify_only and (self.__settlement_pending is None
                or expected_head != new_head or new_head != self.__settlement_pending[0]
                or before_commit != self._replace_settlement_index))
            or type(verify_only) is not bool
            or type(new_head) is not str or re.fullmatch(r"[0-9a-f]{40}", new_head) is None):
            _refuse("adhoc_binding_changed", "native ref transaction differs from the pinned owned branch")
        process = None
        selector = selectors.DefaultSelector()
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + 30

        def write_command(data):
            if process.stdin.write(data) != len(data):
                _refuse("adhoc_binding_changed", "native Git transaction input was incomplete")

        def expect_line(expected):
            while True:
                if b"\n" in buffers["stdout"]:
                    line, _, tail = buffers["stdout"].partition(b"\n")
                    buffers["stdout"] = bytearray(tail)
                    if line != expected:
                        _refuse("adhoc_binding_changed", "native Git ref transaction response differs")
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _refuse("adhoc_binding_changed", "native Git ref transaction exceeded its bound")
                ready = selector.select(remaining)
                if not ready:
                    _refuse("adhoc_binding_changed", "native Git ref transaction did not respond")
                for key, _events in ready:
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        if key.data == "stdout":
                            _refuse("adhoc_binding_changed", "native Git refused ref preparation or commit")
                        continue
                    buffers[key.data].extend(data)
                    if len(buffers[key.data]) > (4096 if key.data == "stdout" else 1024 * 1024):
                        _refuse("adhoc_binding_changed", "native Git ref transaction output exceeds its bound")

        try:
            command, environment = _borrowed_git_launch(self._fds["common"],
                self._pin["profile"]["filesystem_bindings"]["git_common_dir"],
                ("update-ref", "--no-deref", "--stdin"))
            process = subprocess.Popen(command, env=environment, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                close_fds=True, pass_fds=(self._fds["common"],))
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            operation = ("verify " + reference + " " + expected_head if verify_only
                else "update " + reference + " " + new_head + " " + expected_head)
            write_command(("start\n" + operation + "\nprepare\n").encode())
            expect_line(b"start: ok")
            expect_line(b"prepare: ok")
            # Git has the named ref's native lock here, not its referent's lock.
            # A symbolic replacement at the same OID must abort, never overwrite.
            if self._git("symbolic-ref", "--quiet", reference,
                         timeout=max(0.001, deadline - time.monotonic())):
                _refuse("adhoc_binding_changed", "owned direct ref was replaced by a symbolic ref")
            self._check_pinned_files()
            if before_commit is not None:
                # Object staging and ref preparation can outlive dispatch
                # authority. Its parent veto belongs under this native ref
                # lock, immediately before the only branch mutation.
                self._check()
                before_commit()
                self._check()
            if time.monotonic() >= deadline:
                _refuse("adhoc_binding_changed", "native ref identity check exceeded its bound")
            write_command(b"commit\n")
            expect_line(b"commit: ok")
            process.stdin.close()
            if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                _refuse("adhoc_binding_changed", "native Git ref transaction did not exit cleanly")
            self._check()
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProtocolRefusal("adhoc_binding_changed", "native parent ref transaction is unavailable") from exc
        finally:
            selector.close()
            if process is not None:
                try:
                    if process.stdin is not None and not process.stdin.closed:
                        try:
                            if process.poll() is None:
                                process.stdin.write(b"abort\n")
                        except (BrokenPipeError, OSError):
                            pass
                        process.stdin.close()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2)
                finally:
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()

    def _result(self, relative):
        if (type(relative) is not str or not relative or any(unicodedata.category(c).startswith("C") for c in relative)
            or len(relative.encode()) > 4096 or relative.startswith("/")
            or any(part.casefold() in {"", ".", "..", ".git"} for part in relative.split("/"))):
            _refuse("adhoc_result_disposition_invalid", "result must be one exact project file")
        descriptor = os.dup(self._fds["workspace"])
        try:
            parts = relative.split("/")
            for part in parts[:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
            file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            try:
                before = os.fstat(file_descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 16 * 1024 * 1024:
                    _refuse("adhoc_result_disposition_invalid", "result must be a bounded unaliased ordinary file")
                chunks, remaining = [], before.st_size + 1
                while remaining:
                    chunk = os.read(file_descriptor, min(remaining, 65536))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                after = os.fstat(file_descriptor)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    _refuse("adhoc_result_disposition_invalid", "result changed during banking")
                data = b"".join(chunks)
                if len(data) != before.st_size:
                    _refuse("adhoc_result_disposition_invalid", "result size changed during banking")
                return ("100755" if before.st_mode & 0o111 else "100644"), data
            finally:
                os.close(file_descriptor)
        except OSError as exc:
            raise ProtocolRefusal("adhoc_result_disposition_invalid", "result or parent is not an ordinary project path") from exc
        finally:
            os.close(descriptor)

    def finalize(self, result_paths, expected_head, *, before_commit=None, expected_files=None):
        if before_commit is not None and not callable(before_commit):
            _refuse("adhoc_binding_changed", "finalization authority guard must be callable")
        if self._pin is None or type(expected_head) is not str or expected_head != self._pin["head"]:
            _refuse("adhoc_binding_changed", "finalization requires the exact pinned old HEAD")
        if (type(result_paths) is not list or not 1 <= len(result_paths) <= 32
            or any(type(path) is not str for path in result_paths)
            or len(set(result_paths)) != len(result_paths)):
            _refuse("adhoc_result_disposition_invalid", "finalization requires exact unique result paths")
        files = None if expected_files is None else self._expected_files(expected_files)
        if files is not None and set(files) != set(result_paths):
            _refuse("adhoc_binding_changed", "banking paths differ from the exact durable file intent")
        self._check()
        if self._git("rev-parse", "refs/heads/" + self._pin["profile"]["branch"]).decode().strip() != expected_head:
            _refuse("adhoc_binding_changed", "owned branch advanced after pin")
        profile = self._pin["profile"]
        results = [(path, *self._result(path)) for path in result_paths]
        with tempfile.TemporaryDirectory(prefix="floati-adhoc-index-") as scratch:
            index = Path(scratch) / "index"
            self._git("read-tree", expected_head, index=index)
            for path, mode, data in results:
                self._check()
                if files is not None:
                    self._require_expected_file(mode, data, files[path])
                blob = self._git("hash-object", "-w", "--stdin", input=data).decode().strip()
                self._git("update-index", "-z", "--index-info", index=index,
                    input=(mode + " " + blob + "\t" + path + "\0").encode())
            tree = self._git("write-tree", index=index).decode().strip()
            sha = self._git("commit-tree", tree, "-p", expected_head,
                input=b"Bank exact ad-hoc result paths\n").decode().strip()
            self._check()
            self._update_ref("refs/heads/" + profile["branch"], sha, expected_head,
                             before_commit=before_commit)
            self._check()
        return [{"repo": profile["repository"], "sha": sha, "doc": path} for path in result_paths]
