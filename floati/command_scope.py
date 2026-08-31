"""Truthful command-root scope testimony."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Union

from .root import (
    CommandRootSource,
    FloatiRoot,
    resolve_command_root_with_source,
)


@dataclass(frozen=True)
class CommandScope:
    root: str
    tenant: str
    root_source: CommandRootSource

    def evidence(self) -> Dict[str, str]:
        return {
            "root": self.root,
            "tenant": self.tenant,
            "root_source": self.root_source,
        }


def resolve_command_scope(
    explicit_root: Optional[Union[Path, str]],
    *,
    environ: Optional[Mapping[str, str]] = None,
    create: bool = False,
) -> tuple[FloatiRoot, CommandScope]:
    root, root_source = resolve_command_root_with_source(
        explicit_root,
        environ=environ,
        create=create,
    )
    return root, CommandScope(
        root=str(root.path),
        tenant=root.tenant_id,
        root_source=root_source,
    )
