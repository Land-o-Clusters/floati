"""Human-approved native launch through existing approval and grant owners."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
import shlex

from .adhoc_profiles import AdhocProfiles, current_time, timestamp
from .adhoc_records import canonical_json
from .approvals import ApprovalLedger
from .bus_epoch import epoch_guard
from .capabilities import CapabilityGrantLedger
from .errors import ProtocolRefusal
from .jsonl import read_records
from .planes import AuthorityGrantStore
from .policy import RepositoryPolicy, _build_policy, _parse_document
from .root import validate_identifier

CAPABILITY = "native_launch"
WORKER_PROFILE = "adhoc_native"
ROUTING_RANK = 0
BUDGET = "adhoc"
DEFAULT_POLICY = '''schema_version = 0
capability_registry = ["native_launch"]
[limits]
max_items = 1
max_depth = 1
max_fan_out = 1
max_active_attempts = 1
[budgets.adhoc]
unit = "attempts"
limit = 1
[worker_profiles.adhoc_native]
capabilities = ["native_launch"]
cancel_mode = "local_process_only"
callback_support = true
max_concurrency = 1
[capability_selectors.native_launch]
all_of = ["native_launch"]
[routing.adhoc_native_launch]
worker_profile = "adhoc_native"
capability_selector = "native_launch"
rank = 0
[retry_classes.transient]
automatic = true
[retry_classes.permanent]
automatic = false
[retry_classes.operator_required]
automatic = false
[retry_classes.policy_refusal]
automatic = false
[retry_classes.cancelled]
automatic = false
[retry_classes.unknown_effect]
automatic = false
[approval_requirements.low]
required = false
[approval_requirements.medium]
required = true
[approval_requirements.high]
required = true
[approval_requirements.critical]
required = true
[verification.unit]
argv = ["python3", "-m", "unittest", "tests.test_adhoc_runtime"]
[merge_gates.local]
verification_ids = ["unit"]
'''


def _time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@contextmanager
def _profile(root, profile, current):
    with AdhocProfiles(root).validated(profile["id"], now=current) as (record, binding):
        if record != profile:
            raise ProtocolRefusal("adhoc_binding_changed", "launch profile differs from its durable binding")
        yield record, binding


def _compatible(policy):
    worker = policy.worker_profiles.get(WORKER_PROFILE)
    selector = policy.capability_selectors.get(CAPABILITY)
    routes = [route for route in policy.routes if route.worker_profile == WORKER_PROFILE
        and route.capability_selector == CAPABILITY and route.rank == ROUTING_RANK]
    budget = policy.budgets.get(BUDGET)
    if (CAPABILITY not in policy.capability_registry or worker is None
        or CAPABILITY not in worker.capabilities or worker.cancel_mode == "unavailable"
        or not worker.callback_support or selector is None or tuple(selector.all_of) != (CAPABILITY,)
        or len(routes) != 1 or budget is None or budget.unit != "attempts" or budget.limit < 1
        or policy.approval_requirements["low"]):
        raise ProtocolRefusal("capability_selector_unsatisfied", "existing policy does not admit the bounded native launch")
    return policy


def _policy(root, *, preview=False):
    try:
        policy = RepositoryPolicy.load(root.path / "FLOATI.toml")
    except ProtocolRefusal as exc:
        if exc.code != "policy_missing" or not preview:
            raise
        policy = _build_policy(_parse_document(DEFAULT_POLICY))
    return _compatible(policy)


def _authority(root, subject, epoch, current):
    validate_identifier(subject, "authority_subject")
    if type(epoch) is not int or epoch < 1:
        raise ProtocolRefusal("authority_epoch_invalid", "launch approval requires a positive authority epoch")
    authority = AuthorityGrantStore(root).exact_tail(subject)
    if authority["epoch"] != epoch:
        raise ProtocolRefusal("authority_epoch_mismatch", "launch approval authority epoch changed")
    if (authority["state"] != "active" or current < _time(authority["renewed_at"])
        or current >= _time(authority["expires_at"])):
        raise ProtocolRefusal("authority_inactive", "launch approval authority is not active")
    return authority


def _preview(root, profile, policy, subject, epoch):
    if (subject is None) != (epoch is None):
        raise ProtocolRefusal("arguments_invalid", "launch authority subject and epoch must be supplied together")
    if subject is not None:
        validate_identifier(subject, "authority_subject")
        if type(epoch) is not int or epoch < 1:
            raise ProtocolRefusal("authority_epoch_invalid", "launch approval requires a positive authority epoch")
    coordinates = {"action": "native_launch", "root": str(root.path), "profile_id": profile["id"],
        "profile_digest": profile["digest"], "node_id": profile["node_id"], "harness": profile["adapter"],
        "workspace": profile["workspace"], "node_lease_id": profile["node_lease_id"],
        "lease_expires_at": profile["lease_expires_at"], "capability": CAPABILITY,
        "policy_digest": policy.digest, "authority_subject": subject, "authority_epoch": epoch}
    return {**coordinates, "exact_action_digest": hashlib.sha256(
        b"floati-adhoc-native-launch-v1\0" + canonical_json(coordinates)).hexdigest()}


def preview_adhoc_launch(root, profile, *, authority_subject=None, authority_epoch=None, now=None):
    """Describe the proposed capability without creating policy or approvals."""
    current = current_time(now)
    with epoch_guard(root), _profile(root, profile, current):
        return _preview(root, profile, _policy(root, preview=True), authority_subject, authority_epoch)


def _remedy(root, profile, dispatch):
    return shlex.join(["floati", "worker", "adhoc", "profile", "--root", str(root.path),
        "--as", profile["node_id"], "--adapter", profile["adapter"], "--lane-opening",
        profile["lane_opening_id"], "--repository", profile["repository"], "--to", profile["result_recipient"],
        "--idempotency-key", profile["idempotency_key"], "--authority-subject", dispatch["authority_subject"],
        "--authority-epoch", str(dispatch["authority_epoch"]), "--grant-launch"])


def _find_grant(root, profile, policy, preview, current):
    grants = CapabilityGrantLedger(root)
    effective = grants.effective(profile["node_id"], policy.digest, current)
    active = {grant_id for name, grant_id, _position in effective.grant_triples if name == CAPABILITY}
    approvals = ApprovalLedger(root)
    requests = {row["id"]: row for row in read_records(root, approvals.request_path, allowed_kinds={"approval_request"})}
    selected = []
    for grant in grants.records():
        if grant["kind"] != "capability_grant" or grant["id"] not in active:
            continue
        request = requests[grant["approval_request_id"]]
        if request.get("exact_action_digest") != preview["exact_action_digest"]:
            continue
        _request, decision = approvals.require_approved_action(grant["approval_request_id"],
            grant["approval_decision_id"], requester=profile["node_id"],
            exact_action_digest=preview["exact_action_digest"], now=current)
        authority = _authority(root, request["authority_subject"], request["authority_epoch"], current)
        if decision["decider"] != authority["holder"]:
            raise ProtocolRefusal("authority_holder_mismatch", "launch approval holder changed")
        selected.append(grant)
    if len(selected) > 1:
        raise ProtocolRefusal("effect_launch_binding_invalid", "launch requires one exact effective human grant")
    return selected[0] if selected else None


def _write_default_policy(root):
    """Only the explicit human grant route creates this exclusive fixed file."""
    with root.bind_adhoc_coordinates({"root": root.tenant_home}) as (_identities, descriptors):
        try:
            descriptor = os.open("FLOATI.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=descriptors["root"])
        except FileExistsError:
            return
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(DEFAULT_POLICY.encode("utf-8")); stream.flush(); os.fsync(stream.fileno())
        os.fsync(descriptors["root"])


def grant_adhoc_launch(root, profile, *, authority_subject, authority_epoch, now=None):
    """Record the CLI's explicit human act through existing approval owners."""
    # Approval owners persist millisecond timestamps. Use that exact observation
    # for the whole request/decision/grant chain so rounding cannot widen TTL.
    current = _time(timestamp(current_time(now)))
    with epoch_guard(root, exclusive=True), _profile(root, profile, current):
        authority = _authority(root, authority_subject, authority_epoch, current)
        policy = _policy(root, preview=True)
        preview = _preview(root, profile, policy, authority_subject, authority_epoch)
        existing = _find_grant(root, profile, policy, preview, current)
        if existing is not None:
            return {"launch_preview": preview, "launch_grant": existing}
        approvals = ApprovalLedger(root)
        requests = [row for row in read_records(root, approvals.request_path, allowed_kinds={"approval_request"})
            if row.get("exact_action_digest") == preview["exact_action_digest"]
            and row["requester"] == profile["node_id"] and row["capability"] == CAPABILITY]
        if len(requests) > 1:
            raise ProtocolRefusal("effect_launch_binding_invalid", "launch approval has multiple durable requests")
        request = requests[0] if requests else None
        if request is not None and any(row["kind"] == "capability_grant"
            and row["approval_request_id"] == request["id"] for row in CapabilityGrantLedger(root).records()):
            raise ProtocolRefusal("effect_approval_expired", "the prior launch grant is no longer effective; create a fresh profile")
        expiry = min(_time(profile["lease_expires_at"]), _time(authority["expires_at"]))
        if request is not None:
            expiry = min(expiry, _time(request["expires_at"]))
        ttl = min(86400, int((expiry - current).total_seconds()))
        if ttl < 1:
            raise ProtocolRefusal("effect_approval_expired", "launch approval has no remaining whole-second lifetime")
        try:
            RepositoryPolicy.load(root.path / "FLOATI.toml")
        except ProtocolRefusal as exc:
            if exc.code != "policy_missing":
                raise
            _write_default_policy(root)
        policy = _policy(root)
        if policy.digest != preview["policy_digest"]:
            raise ProtocolRefusal("capability_selector_unsatisfied", "launch policy changed after the human grant preview")
        if request is None:
            request = approvals.request_for_action(profile["node_id"], CAPABILITY, "worker:" + profile["node_id"],
                ttl, preview["exact_action_digest"], authority_subject, authority_epoch, now=current)
        decisions = [row for row in read_records(root, approvals.decision_path, allowed_kinds={"approval_decision"})
            if row["request_id"] == request["id"]]
        if len(decisions) > 1:
            raise ProtocolRefusal("effect_launch_binding_invalid", "launch request has multiple durable decisions")
        if decisions:
            decision = decisions[0]
            approvals.require_approved_action(request["id"], decision["id"], requester=profile["node_id"],
                exact_action_digest=preview["exact_action_digest"], now=current)
        else:
            decision = approvals.decide(request["id"], authority["holder"], "approved", None,
                granted_scope="worker:" + profile["node_id"], granted_ttl_seconds=ttl, now=current)
        grant = CapabilityGrantLedger(root).grant(profile["node_id"], CAPABILITY, policy,
            request["id"], decision["id"], now=current)
        return {"launch_preview": preview, "launch_grant": grant}


def require_adhoc_launch(root, profile, dispatch, *, now=None):
    """Read existing exact human authority; absence never creates any state."""
    dispatch = dispatch.get("dispatch", dispatch)
    remedy = _remedy(root, profile, dispatch)
    current = current_time(now)
    try:
        with epoch_guard(root), _profile(root, profile, current):
            policy = _policy(root)
            preview = _preview(root, profile, policy, dispatch["authority_subject"], dispatch["authority_epoch"])
            grant = _find_grant(root, profile, policy, preview, current)
            if grant is None:
                raise ProtocolRefusal("effect_approval_missing", "an exact human native launch grant is required")
            return {"policy": policy, "launch_preview": preview, "launch_grant": grant}
    except ProtocolRefusal as exc:
        raise ProtocolRefusal(exc.code, exc.detail, remedy) from exc
