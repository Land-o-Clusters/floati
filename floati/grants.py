"""Architect-gated public authority grant and revocation surface."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from .admin_registry import RegistryAdminBackend
from .credential_leases import CredentialLeaseLedger
from .errors import ProtocolRefusal
from .planes import AuthorityGrantStore
from .registry import Registry
from .root import FloatiRoot, resolve_command_root, validate_identifier


HandlerResult = Tuple[str, Dict[str, Any], int]
OK = 0


def _now(value: Optional[datetime] = None) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ProtocolRefusal(
            "time_invalid", "authority act requires an aware datetime"
        )
    return current.astimezone(timezone.utc)


class AuthorityGrantService:
    """Authorize one exact grant act from current registry-role evidence."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root
        self.registry = Registry(root)
        self.backend = RegistryAdminBackend(root)
        self.store = AuthorityGrantStore(root)

    def _require_architect(self, grantor: str) -> str:
        actor = validate_identifier(grantor, "grantor")
        self.registry.require_active(actor)
        try:
            role = self.backend.role_record(actor)
        except ProtocolRefusal as exc:
            if exc.code != "role_assignment_missing":
                raise
            raise ProtocolRefusal(
                "grant_requires_architect",
                "grant requires an active architect role; remedy: "
                "floati node role --template architect",
            ) from exc
        if role.get("state") != "active" or role.get("template_role") != "architect":
            raise ProtocolRefusal(
                "grant_requires_architect",
                "grant requires an active architect role; remedy: "
                "floati node role --template architect",
            )
        return actor

    @staticmethod
    def _coordinate(holder: str, subject: str, epoch: int) -> tuple[str, str, int]:
        owner = validate_identifier(holder, "holder")
        authority_subject = validate_identifier(subject, "subject")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ProtocolRefusal(
                "authority_epoch_invalid",
                "authority epoch must be a positive integer",
            )
        return owner, authority_subject, epoch

    def grant(
        self,
        grantor: str,
        holder: str,
        subject: str,
        epoch: int,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        owner, authority_subject, exact_epoch = self._coordinate(
            holder, subject, epoch
        )
        actor = self._require_architect(grantor)
        self.registry.require_active(owner)
        record = self.store.grant_exact(
            authority_subject, owner, exact_epoch, _now(now)
        )
        return {"operation": "grant", "grantor": actor, "record": record}

    def revoke(
        self,
        grantor: str,
        holder: str,
        subject: str,
        epoch: int,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        owner, authority_subject, exact_epoch = self._coordinate(
            holder, subject, epoch
        )
        actor = self._require_architect(grantor)
        record = self.store.revoke_exact(
            authority_subject, owner, exact_epoch, _now(now)
        )
        lease_revocations = CredentialLeaseLedger(self.root).revoke_alias(
            authority_subject, owner, exact_epoch, now=_now(now)
        )
        return {
            "operation": "revoke",
            "grantor": actor,
            "record": record,
            "credential_lease_revocations": lease_revocations,
        }


def _arguments(args: argparse.Namespace) -> tuple[FloatiRoot, str, str, str, int]:
    root = resolve_command_root(args.root, create=False)
    values = (args.grantor, args.holder, args.subject, args.epoch)
    if any(value is None for value in values):
        raise ProtocolRefusal(
            "arguments_invalid",
            "grant requires --root, --as, --holder, --subject, and --epoch",
        )
    return (
        root,
        args.grantor,
        args.holder,
        args.subject,
        args.epoch,
    )


def _grant(args: argparse.Namespace) -> HandlerResult:
    root, grantor, holder, subject, epoch = _arguments(args)
    return "ok", AuthorityGrantService(root).grant(
        grantor, holder, subject, epoch
    ), OK


def _revoke(args: argparse.Namespace) -> HandlerResult:
    root, grantor, holder, subject, epoch = _arguments(args)
    return "ok", AuthorityGrantService(root).revoke(
        grantor, holder, subject, epoch
    ), OK


def _add_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument("--root")
    parser.add_argument("--as", dest="grantor", required=required)
    parser.add_argument("--holder", required=required)
    parser.add_argument("--subject", required=required)
    parser.add_argument("--epoch", type=int, required=required)


def register_cli(commands: argparse._SubParsersAction) -> None:
    grant = commands.add_parser("grant")
    _add_arguments(grant, required=False)
    grant.set_defaults(handler=_grant, grant_command=None)
    operations = grant.add_subparsers(dest="grant_command")
    revoke = operations.add_parser("revoke")
    _add_arguments(revoke, required=True)
    revoke.set_defaults(handler=_revoke)
