"""Resolve a PR (or a commit range) to the diff and changed files a review runs over.

Fetches only — never a branch switch, so the caller's working tree is untouched.
See docs/adr/0024-canonical-pr-value-object.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .. import execrun, gh, git
from ..identity import Repo, Sha, repo_from_slug
from ..pr import PR, PrId, core_from_node


class ReviewError(RuntimeError):
    """A review precondition failed."""


@dataclass
class ReviewView:
    pr: PR
    base_sha: Sha
    diff: str
    changed_files: list[str] = field(default_factory=list)
    workdir: str = "."
    #: The head BRANCH name; empty only for a hand-built context.
    head_ref: str = ""

    @property
    def number(self) -> int:
        return self.pr.number

    @property
    def head_sha(self) -> Sha:
        return self.pr.head_sha

    @property
    def base_ref(self) -> str | None:
        return self.pr.base_ref

    @property
    def repo(self) -> str | None:
        if self.pr.repo == _HANDBUILT_REPO:
            return None
        return self.pr.repo.slug


def review_view(
    *,
    number: int,
    head_sha: str | Sha,
    base_ref: str | None,
    base_sha: str | Sha,
    diff: str,
    is_draft: bool,
    repo: str | None = None,
    merge_state: str | None = None,
    changed_files: list[str] | None = None,
    workdir: str = ".",
    head_ref: str = "",
) -> ReviewView:
    """``is_draft`` has no default, so no caller fabricates a fetched core field."""
    pr = PR(
        id=PrId(repo=repo_from_slug(repo) if repo else _HANDBUILT_REPO, number=number),
        head_sha=head_sha if isinstance(head_sha, Sha) else Sha(head_sha),
        base_ref=base_ref,
        is_draft=is_draft,
        merge_state=merge_state,
    )
    return ReviewView(
        pr=pr,
        base_sha=base_sha if isinstance(base_sha, Sha) else Sha(base_sha),
        diff=diff,
        changed_files=changed_files if changed_files is not None else [],
        workdir=workdir,
        head_ref=head_ref,
    )


@dataclass(frozen=True)
class RangeView:
    repo: Repo
    base_sha: Sha
    head_sha: Sha
    diff: str
    changed_files: list[str]
    workdir: str


def rescoped_view(view: ReviewView, base_sha: str | Sha) -> ReviewView:
    base = base_sha if isinstance(base_sha, Sha) else Sha(str(base_sha))
    try:
        diff = git.diff_range(base, view.head_sha, cwd=view.workdir)
        changed_files = git.diff_name_only(base, view.head_sha, cwd=view.workdir)
    except execrun.ExecError as exc:
        raise ReviewError(
            f"failed to compute the incremental fix-range diff for PR "
            f"#{view.number} ({base}..{view.head_sha}): {exc}"
        ) from exc
    return ReviewView(
        pr=view.pr,
        base_sha=base,
        diff=diff,
        changed_files=changed_files,
        workdir=view.workdir,
        head_ref=view.head_ref,
    )


_HANDBUILT_REPO = repo_from_slug("local/local")


def _git_toplevel(workdir: str) -> str | None:
    return git.repo_root(cwd=workdir)


def _pr_meta(pr: int, repo: str | None) -> dict:
    try:
        return gh.pr_view(
            str(pr),
            repo=repo,
            json_fields=[
                "number",
                "headRefName",
                "headRefOid",
                "baseRefName",
                "baseRefOid",
                "isDraft",
                "mergeStateStatus",
            ],
        )
    except execrun.ExecError as exc:
        raise ReviewError(
            f"Could not resolve PR #{pr}"
            + (f" in {repo}" if repo else "")
            + f" via `gh pr view`: {exc}"
        ) from exc
    except ValueError as exc:
        raise ReviewError(f"Unusable `gh pr view` output for PR #{pr}: {exc}") from exc


def resolve_pr(
    pr: int,
    *,
    repo: str | None = None,
    workdir: str | None = None,
) -> ReviewView:
    """Diffs merge-base to head; an endpoint that cannot be made present fails
    loud rather than degrading to a local ref."""
    workdir = workdir or os.getcwd()
    toplevel = _git_toplevel(workdir)
    if toplevel is None:
        raise ReviewError(
            f"{workdir!r} is not a git checkout — `shipit pr review` resolves a "
            f"PR by diffing inside a clone of the repository. cd into the repo (or "
            f"pass a checkout) and re-run."
        )
    # The prompt names repo-root-relative paths, unopenable from a nested cwd.
    workdir = toplevel

    canonical: Repo | None = None
    if repo is not None:
        try:
            canonical = gh.repo_canonical(repo)
        except (execrun.ExecError, ValueError) as exc:
            raise ReviewError(
                f"Could not resolve repo {repo!r} to its canonical owner/name via "
                f"`gh repo view`: {exc}"
            ) from exc

    # Never synthesized from the checkout's origin: that slug is un-canonicalized.
    repo_obj = canonical if canonical is not None else _HANDBUILT_REPO

    meta = _pr_meta(pr, canonical.slug if canonical is not None else None)
    try:
        pr_core = core_from_node(meta, repo_obj)
    except (KeyError, ValueError) as exc:
        raise ReviewError(
            f"PR #{pr} returned an unusable core from `gh pr view` ({exc}) — "
            f"cannot resolve the PR head to review."
        ) from exc
    base_ref = pr_core.base_ref or "main"
    head_sha = pr_core.head_sha
    head_ref = meta.get("headRefName") or ""

    raw_base = meta.get("baseRefOid") or ""
    if not raw_base:
        raise ReviewError(
            f"PR #{pr} returned no base sha (baseRefOid) from `gh pr view` — "
            f"cannot resolve the PR base to review against."
        )
    try:
        base_sha = Sha(raw_base)
    except ValueError as exc:
        raise ReviewError(
            f"PR #{pr} returned an unusable base sha (baseRefOid) from "
            f"`gh pr view` ({exc}) — cannot resolve the PR base to review against."
        ) from exc

    if not git.commit_present(head_sha, cwd=workdir):
        git.fetch_ref(f"pull/{pr}/head", cwd=workdir)
        if not git.commit_present(head_sha, cwd=workdir) and head_ref:
            git.fetch_ref(head_ref, cwd=workdir)
        if not git.commit_present(head_sha, cwd=workdir):
            git.fetch_ref(str(head_sha), cwd=workdir)

    if not git.commit_present(head_sha, cwd=workdir):
        raise ReviewError(
            f"Can't resolve PR #{pr} head {head_sha} — the commit isn't available "
            f"after fetching pull/{pr}/head, the head branch, and the sha directly. "
            f"The PR may be from a fork (its head isn't on origin) or the head is "
            f"otherwise unavailable; fetch it into this checkout and re-run."
        )

    head_point = head_sha

    if not git.commit_present(base_sha, cwd=workdir):
        git.fetch_ref(base_ref, cwd=workdir)
        if not git.commit_present(base_sha, cwd=workdir):
            git.fetch_ref(str(base_sha), cwd=workdir)

    if not git.commit_present(base_sha, cwd=workdir):
        raise ReviewError(
            f"Can't resolve PR #{pr} base {base_sha} (baseRefOid) — the commit "
            f"isn't available after fetching the base branch '{base_ref}' and the "
            f"sha directly. Fetch it into this checkout and re-run rather than "
            f"reviewing against a stale or wrong base."
        )

    base_point = git.merge_base(base_sha, head_point, cwd=workdir)
    if base_point is None:
        raise ReviewError(
            f"PR #{pr} base {base_sha} and head {head_point} have no common "
            f"ancestor — cannot compute a meaningful review diff. The PR base/head "
            f"may be unrelated histories; resolve the base and re-run."
        )

    try:
        diff = git.diff_range(base_point, head_point, cwd=workdir)
        changed_files = git.diff_name_only(base_point, head_point, cwd=workdir)
    except execrun.ExecError as exc:
        raise ReviewError(
            f"failed to compute diff for PR #{pr} ({base_point}..{head_point}): {exc}"
        ) from exc

    return ReviewView(
        pr=pr_core,
        base_sha=base_sha,
        diff=diff,
        changed_files=changed_files,
        workdir=workdir,
        head_ref=head_ref,
    )
