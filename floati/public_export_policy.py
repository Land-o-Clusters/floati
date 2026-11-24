"""Shared public-export policy classification."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping


PUBLISHED_BASELINE_RELATIVE = ".github/public-export-baseline.v0.json"
POLICY_RELATIVE = ".github/public-export-policy.v0.json"
POLICY_KEYS = frozenset(
    (
        "additional_public_paths",
        "class1_evidence_prefixes",
        "class3_prefixes",
        "new_product_prefixes",
        "new_private_prefixes",
        "private_only_paths",
        "public_repository",
        "schema_version",
    )
)


class ExportRefusal(Exception):
    def __init__(self, code: str, detail: str, **evidence: object) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.evidence = evidence


@dataclass(frozen=True)
class Classification:
    disposition: str
    rule: str


@dataclass(frozen=True)
class ExportPolicy:
    public_repository: str
    private_only_paths: tuple[str, ...]
    class3_prefixes: tuple[str, ...]
    new_product_prefixes: tuple[str, ...]
    new_private_prefixes: tuple[str, ...]
    class1_evidence_prefixes: tuple[str, ...]
    additional_public_paths: tuple[str, ...]

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> "ExportPolicy":
        if not isinstance(mapping, Mapping) or set(mapping) != POLICY_KEYS:
            raise ExportRefusal(
                "public_export_policy_invalid",
                "public export policy must contain the exact version-zero fields",
            )
        if mapping.get("schema_version") != 0 or isinstance(
            mapping.get("schema_version"), bool
        ):
            raise ExportRefusal(
                "public_export_policy_invalid",
                "public export policy schema_version must be 0",
            )
        repository = mapping.get("public_repository")
        if repository != "Land-o-Clusters/floati":
            raise ExportRefusal(
                "public_export_policy_invalid",
                "public export policy must name Land-o-Clusters/floati",
            )

        values: dict[str, tuple[str, ...]] = {}
        for key in (
            "private_only_paths",
            "class3_prefixes",
            "new_product_prefixes",
            "new_private_prefixes",
            "class1_evidence_prefixes",
            "additional_public_paths",
        ):
            raw = mapping.get(key)
            if (
                not isinstance(raw, list)
                or any(not isinstance(value, str) or not value for value in raw)
                or raw != sorted(set(raw))
            ):
                raise ExportRefusal(
                    "public_export_policy_invalid",
                    f"public export policy {key} must be a sorted unique string list",
                )
            for value in raw:
                _validate_relative(value.rstrip("/") or value)
            values[key] = tuple(raw)
        return cls(public_repository=repository, **values)

    @classmethod
    def load(cls, path: Path) -> "ExportPolicy":
        try:
            mapping = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExportRefusal(
                "public_export_policy_invalid",
                "public export policy is unreadable",
            ) from exc
        return cls.from_mapping(mapping)


def _validate_relative(value: str) -> None:
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in ("", ".", "..") for part in pure.parts)
        or "\x00" in value
    ):
        raise ExportRefusal(
            "public_export_policy_invalid",
            "public export policy contains an invalid repository-relative path",
        )


def load_published_baseline(path: Path) -> frozenset[str]:
    """Return the published tree paths recorded beside the export policy."""

    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 0 or isinstance(
        document.get("schema_version"), bool
    ):
        raise ExportRefusal(
            "public_export_policy_invalid",
            "published baseline schema_version must be 0",
        )
    paths = document.get("public_paths")
    if (
        not isinstance(paths, list)
        or any(not isinstance(value, str) or not value for value in paths)
        or paths != sorted(set(paths))
    ):
        raise ExportRefusal(
            "public_export_policy_invalid",
            "published baseline must be a sorted unique string list",
        )
    return frozenset(paths)


def classify_path(
    relative: str, public_paths: set[str], policy: ExportPolicy
) -> Classification:
    if ".floati-dispatches" in relative.casefold().split("/"):
        return Classification("exclude", "local_dispatch_storage")
    if relative in policy.private_only_paths:
        return Classification("exclude", "private_only_exact")
    for prefix in policy.class3_prefixes:
        if relative.startswith(prefix):
            return Classification("exclude", f"class3_prefix:{prefix}")
    if relative in public_paths:
        return Classification("include", "public_baseline")
    for prefix in policy.new_product_prefixes:
        if relative.startswith(prefix):
            return Classification("include", f"new_product_prefix:{prefix}")
    for prefix in policy.class1_evidence_prefixes:
        if relative.startswith(prefix):
            return Classification("include", f"class1_evidence_prefix:{prefix}")
    if relative in policy.additional_public_paths:
        return Classification("include", "additional_public_exact")
    for prefix in policy.new_private_prefixes:
        if relative.startswith(prefix):
            return Classification("exclude", f"new_private_prefix:{prefix}")
    return Classification("unresolved", "unclassified_new_path")
