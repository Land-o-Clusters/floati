"""Read-only worktree cleanup eligibility and named refusal."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import ProtocolRefusal
from .git_observer import GitObserver, RepositoryIdentity


@dataclass(frozen=True)
class CleanupAssessment:
    repository_identity: RepositoryIdentity
    worktree: Path
    unreferenced_commits: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return not self.unreferenced_commits


class CleanupInspector:
    def __init__(self, repository: Path) -> None:
        self.observer = GitObserver(repository)

    def assess(self, worktree: Path) -> CleanupAssessment:
        commits = self.observer.unique_reachable_commits(worktree)
        return CleanupAssessment(
            repository_identity=self.observer.identity,
            worktree=worktree.resolve(),
            unreferenced_commits=commits,
        )

    def require_eligible(self, worktree: Path) -> CleanupAssessment:
        assessment = self.assess(worktree)
        if not assessment.eligible:
            raise ProtocolRefusal(
                "cleanup_unreferenced_commits",
                "cleanup refused; commits reachable only from the worktree: "
                + ",".join(assessment.unreferenced_commits),
            )
        return assessment
