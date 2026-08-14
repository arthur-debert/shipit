"""The fleet signing-posture check: each declared portfolio repo's posture against the Actions secret NAMES it actually carries."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import execrun, gh
from .fleetsweep import PortfolioEntry
from .release import posture

logger = logging.getLogger("shipit.fleet")

STATUS_CONFORMS = "conforms"
STATUS_DIVERGENT = "divergent"
STATUS_UNKNOWN = "unknown"

#: Names-only listing seam: a repo slug -> its Actions secret names.
NamesFn = Callable[[str], list[str]]


@dataclass(frozen=True)
class RepoPosture:
    """One repo's row: its declared posture and every finding against it, or why it could not be read."""

    entry: PortfolioEntry
    status: str
    findings: tuple[posture.Finding, ...] = ()
    secrets: tuple[str, ...] = ()
    reason: str | None = None

    def summary(self) -> str:
        if self.status == STATUS_UNKNOWN:
            return f"{self.entry.repo}: posture UNKNOWN — {self.reason}"
        if self.status == STATUS_CONFORMS:
            note = (
                f" ({self.entry.signing_reason})" if self.entry.signing_reason else ""
            )
            return f"{self.entry.repo}: {self.entry.signing}, conforms{note}"
        detail = "; ".join(f"{f.secret}: {f.detail}" for f in self.findings)
        return f"{self.entry.repo}: {self.entry.signing}, DIVERGENT — {detail}"

    def to_dict(self) -> dict:
        data: dict = {
            "stack": self.entry.stack,
            "repo": self.entry.repo,
            "signing": self.entry.signing,
            "status": self.status,
            "signing_secrets": list(self.secrets),
            "findings": [f.to_dict() for f in self.findings],
            "summary": self.summary(),
        }
        if self.entry.signing_reason is not None:
            data["signing_reason"] = self.entry.signing_reason
        if self.reason is not None:
            data["reason"] = self.reason
        return data


@dataclass(frozen=True)
class PostureReport:
    """The registry as checked: one row per declared portfolio repo."""

    repos: tuple[RepoPosture, ...]

    @property
    def divergent(self) -> tuple[RepoPosture, ...]:
        return tuple(row for row in self.repos if row.status == STATUS_DIVERGENT)

    @property
    def unknown(self) -> tuple[RepoPosture, ...]:
        return tuple(row for row in self.repos if row.status == STATUS_UNKNOWN)

    def verdict(self) -> int:
        """The exit code: 0 only when every row was read and conforms — an unreadable repo is not a pass."""
        return 0 if not self.divergent and not self.unknown else 1

    def to_dict(self) -> dict:
        return {
            "kind": "fleet-posture-report",
            "postures": list(posture.POSTURES),
            "canonical_signing_secrets": list(posture.CANONICAL_SIGNING_SECRETS),
            "divergent": [row.entry.repo for row in self.divergent],
            "unknown": [row.entry.repo for row in self.unknown],
            "repos": [row.to_dict() for row in self.repos],
        }


def check_repo(entry: PortfolioEntry, *, names_fn: NamesFn) -> RepoPosture:
    """One row: read the repo's secret names, judge them against the declared posture; an unreadable repo yields ``unknown``, never a silent pass."""
    try:
        names = names_fn(entry.repo)
    except (execrun.ExecError, ValueError) as exc:
        logger.warning(
            "could not read repo secret names",
            exc_info=True,
            extra={"repo": entry.repo},
        )
        return RepoPosture(
            entry=entry,
            status=STATUS_UNKNOWN,
            reason=f"could not list Actions secrets: {exc}",
        )
    findings = posture.conformance(entry.signing, names)
    return RepoPosture(
        entry=entry,
        status=STATUS_DIVERGENT if findings else STATUS_CONFORMS,
        findings=findings,
        secrets=posture.signing_names(names),
    )


def check(
    entries: Sequence[PortfolioEntry], *, names_fn: NamesFn | None = None
) -> PostureReport:
    """The whole registry checked, in declaration order."""
    read = names_fn or gh.secret_list
    return PostureReport(repos=tuple(check_repo(e, names_fn=read) for e in entries))
