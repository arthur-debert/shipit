"""shipit's PR value objects and the one boundary that reads a PR core off the wire.
See docs/adr/0024-core-identities-repo-workingdir-pr.md
"""

from __future__ import annotations

from dataclasses import dataclass

from .identity import Repo, Sha

CORE_JSON_FIELDS = (
    "number",
    "headRefOid",
    "baseRefName",
    "isDraft",
    "mergeStateStatus",
)


@dataclass(frozen=True)
class PrId:
    """The identity half of a :class:`PR` — ``(repo, number)``, nothing fetched; construction validates ``number`` as an exact, positive ``int``."""

    repo: Repo
    number: int

    def __post_init__(self) -> None:
        if type(self.number) is not int:
            raise ValueError(
                f"PR number must be int, got {self.number!r} "
                f"({type(self.number).__name__})"
            )
        if self.number < 1:
            raise ValueError(f"PR number must be positive, got {self.number!r}")

    @property
    def slug(self) -> str:
        return self.repo.slug


@dataclass(frozen=True)
class PR:
    """A pull request: a composed :class:`PrId` plus its core state, every field REQUIRED so a path that did not fetch one cannot build a PR."""

    id: PrId
    head_sha: Sha
    base_ref: str | None
    is_draft: bool
    merge_state: str | None

    @property
    def repo(self) -> Repo:
        return self.id.repo

    @property
    def number(self) -> int:
        return self.id.number

    @property
    def slug(self) -> str:
        return self.id.slug


def core_from_node(node: dict, repo: Repo) -> PR:
    """Build the :class:`PR` core from a ``pullRequest`` node — the ONE wire read; a missing or non-bool required field raises."""
    try:
        pr_id = PrId(repo=repo, number=node["number"])
    except ValueError as exc:
        raise ValueError(f"malformed PR node: {exc}") from exc
    is_draft = node["isDraft"]
    if not isinstance(is_draft, bool):
        raise ValueError(
            f"malformed PR node: isDraft must be a bool, got {is_draft!r} "
            f"({type(is_draft).__name__})"
        )
    return PR(
        id=pr_id,
        head_sha=Sha(node["headRefOid"]),
        base_ref=node.get("baseRefName"),
        is_draft=is_draft,
        merge_state=node.get("mergeStateStatus"),
    )
