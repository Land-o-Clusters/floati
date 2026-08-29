"""Controller-owned staged provisioning that yields a seat only after testimony."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from ..errors import DurabilityFailure, ProtocolRefusal
from ..ids import uuid7_hex
from ..root import validate_identifier
from .ledger import LockLedger


@dataclass(frozen=True)
class ProvisionedSeat:
    seat_id: str
    hook_names: tuple[str, ...]
    manifest_digest: str
    resource_root: Path


class ProvisioningController:
    def __init__(self, ledger: LockLedger) -> None:
        if type(ledger) is not LockLedger:
            raise ProtocolRefusal("locks_provisioning_invalid", "provisioning requires the exact Locks ledger owner")
        self.ledger = ledger

    @staticmethod
    def _hooks(source: Iterable[object]) -> tuple[object, ...]:
        try:
            hooks = tuple(source)
        except Exception as exc:
            raise ProtocolRefusal("provisioning_hooks_invalid", "hook collection could not be snapshotted") from exc
        if not hooks or len(hooks) > 64:
            raise ProtocolRefusal("provisioning_hooks_invalid", "provisioning needs one bounded hook set")
        names: list[str] = []
        for hook in hooks:
            try:
                name = validate_identifier(getattr(hook, "name", None), "hook_name")
            except ProtocolRefusal:
                raise
            for method in ("prepare", "abort", "verify_absent"):
                if not callable(getattr(hook, method, None)):
                    raise ProtocolRefusal(
                        "provisioning_hooks_invalid",
                        f"hook {name} lacks required {method} behavior",
                    )
            names.append(name)
        if len(set(names)) != len(names):
            raise ProtocolRefusal("provisioning_hooks_invalid", "provisioning hook names must be unique")
        return hooks

    @staticmethod
    def _manifest_fragment(
        value: object,
        hook_name: str,
        staging_root: Path,
    ) -> dict[str, object]:
        if type(value) is not dict:
            raise ProtocolRefusal(
                "provisioning_manifest_invalid",
                f"hook {hook_name} returned a non-mapping manifest fragment",
            )
        if set(value) != {"resource"} or type(value.get("resource")) is not str:
            raise ProtocolRefusal(
                "provisioning_manifest_invalid",
                f"hook {hook_name} must name one controller-relative resource",
            )
        resource = Path(str(value["resource"]))
        if (
            resource.is_absolute()
            or str(resource).startswith("-")
            or any(part in {"", ".", ".."} for part in resource.parts)
        ):
            raise ProtocolRefusal(
                "provisioning_manifest_invalid",
                f"hook {hook_name} named a resource outside controller staging",
            )
        try:
            if staging_root.is_symlink() or staging_root.resolve(strict=True) != staging_root:
                raise OSError("staging root identity changed")
            candidate = staging_root / resource
            current = staging_root
            for part in resource.parts:
                current = current / part
                if current.is_symlink():
                    raise OSError("resource path contains a symlink")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(staging_root)
        except (OSError, ValueError) as exc:
            raise ProtocolRefusal(
                "provisioning_manifest_invalid",
                f"hook {hook_name} resource is absent or escapes controller staging",
            ) from exc
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ProtocolRefusal(
                "provisioning_manifest_invalid",
                f"hook {hook_name} returned non-I-JSON evidence",
            ) from exc
        if len(encoded) > 65_536:
            raise ProtocolRefusal("provisioning_manifest_invalid", "hook evidence exceeds its byte bound")
        return {"resource": str(resource)}

    @staticmethod
    def _abort(prepared: list[object], root: Path) -> bool:
        rollback_ok = True
        for hook in reversed(prepared):
            try:
                hook.abort(root)
            except Exception:
                rollback_ok = False
        for hook in prepared:
            try:
                if hook.verify_absent(root) is not True:
                    rollback_ok = False
            except Exception:
                rollback_ok = False
        return rollback_ok

    @staticmethod
    def _remove_tree(path: Path) -> None:
        try:
            if path.exists():
                shutil.rmtree(path)
        except OSError as exc:
            raise DurabilityFailure("storage_unavailable", "provisioning staging cleanup failed") from exc

    def provision(
        self,
        *,
        seat_id: object,
        hooks: Iterable[object],
        now: object,
    ) -> ProvisionedSeat:
        seat = validate_identifier(seat_id if type(seat_id) is str else None, "seat_id")
        selected = self._hooks(hooks)
        if seat in self.ledger.snapshot().seats:
            raise ProtocolRefusal("seat_already_provisioned", "seat already has provisioning testimony")
        staging_parent = self.ledger.root.resolve_relative(Path("locks/provisioning-staging"))
        final_parent = self.ledger.root.resolve_relative(Path("locks/provisioned"))
        staging = staging_parent / (seat + "-" + uuid7_hex())
        final = final_parent / seat
        if final.exists():
            raise ProtocolRefusal("provisioning_resource_exists", "seat resource root already exists")
        try:
            staging.mkdir(parents=True, mode=0o700)
        except OSError as exc:
            raise DurabilityFailure("storage_unavailable", "provisioning staging root could not be created") from exc

        prepared: list[object] = []
        manifest: list[dict[str, object]] = []
        try:
            for hook in selected:
                name = str(hook.name)
                prepared.append(hook)
                fragment = self._manifest_fragment(hook.prepare(staging), name, staging)
                manifest.append({"hook": name, "evidence": fragment})
        except Exception as exc:
            rollback_ok = self._abort(prepared, staging)
            self._remove_tree(staging)
            if not rollback_ok:
                raise ProtocolRefusal(
                    "provisioning_rollback_failed",
                    "failed provisioning left an unverified rollback",
                ) from exc
            if isinstance(exc, ProtocolRefusal):
                raise exc
            raise ProtocolRefusal(
                "provisioning_prepare_failed",
                "a provisioning hook failed before seat issue",
            ) from exc

        encoded_manifest = json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(encoded_manifest).hexdigest()
        try:
            final_parent.mkdir(parents=True, exist_ok=True)
            staging.rename(final)
        except OSError as exc:
            rollback_ok = self._abort(prepared, staging)
            self._remove_tree(staging)
            if not rollback_ok:
                raise ProtocolRefusal("provisioning_rollback_failed", "publish failure left unverified rollback") from exc
            raise DurabilityFailure("storage_unavailable", "provisioning resources could not be published") from exc
        try:
            self.ledger.record_seat_provisioned(
                seat_id=seat,
                hook_names=[str(hook.name) for hook in selected],
                manifest_digest=digest,
                now=now,
            )
        except Exception:
            rollback_ok = self._abort(prepared, final)
            self._remove_tree(final)
            if not rollback_ok:
                raise ProtocolRefusal("provisioning_rollback_failed", "ledger failure left unverified rollback")
            raise
        return ProvisionedSeat(
            seat_id=seat,
            hook_names=tuple(str(hook.name) for hook in selected),
            manifest_digest=digest,
            resource_root=final,
        )
