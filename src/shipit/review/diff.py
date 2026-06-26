"""diff — resolve a PR to the diff (and changed files) the review runs over.

This replaces the Phase-1.2 single-base stepping-stone (a bare ``git diff
<base>...HEAD`` in cwd) with real PR resolution: given a PR number, ask GitHub
for the PR's base/head refs, make the base + head available locally (FETCH only —
never a branch switch, so the user's working tree is untouched), and compute the
three-dot diff and changed-file list the agent reviews.

The CHECKOUT model: the agent backend reads files from ``PRContext.workdir`` so
it can open the surrounding source for context. When the review runs in the
consumer's own checkout of the PR (``workdir`` defaults to cwd) the head is
typically already at ``HEAD``; otherwise we fetch the PR head as an object and
diff against it. We never switch branches — if the head isn't the current
working tree, the agent can still read the changed content via the diff and via
``git show <sha>:<path>``, but a full file-tree read of the head requires that
the head actually be checked out (documented limitation, not a branch switch).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field

from .. import gh, proc


class ReviewError(RuntimeError):
    """A review precondition failed (not a git checkout, PR unresolvable, …).

    Carries an actionable message — the facade prints it and exits nonzero.
    """


@dataclass
class PRContext:
    """Everything the review needs about one PR, resolved and ready to diff."""

    number: int
    repo: str | None
    head_sha: str
    base_ref: str
    base_sha: str
    diff: str
    changed_files: list[str] = field(default_factory=list)
    workdir: str = "."


def _git_toplevel(workdir: str) -> str | None:
    """The git working-tree root for ``workdir``, or ``None`` when not a checkout.

    The backend reads files with ``cwd=ctx.workdir`` and the review prompt names
    paths relative to the REPO ROOT, so running from a nested subdir would leave
    repo-relative paths unopenable. Normalizing ``workdir`` to the toplevel makes
    the agent's cwd the repo root regardless of where the command was invoked.
    """
    result = proc.run(
        ["git", "-C", workdir, "rev-parse", "--show-toplevel"],
        check=False,
    )
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return top or None


def _git(
    workdir: str, args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return proc.run(["git", "-C", workdir, *args], check=check)


def _sha_present(workdir: str, sha: str) -> bool:
    """True if ``sha`` is a commit object reachable in ``workdir`` (no fetch)."""
    if not sha:
        return False
    result = proc.run(
        ["git", "-C", workdir, "cat-file", "-e", f"{sha}^{{commit}}"],
        check=False,
    )
    return result.returncode == 0


def _pr_meta(pr: int, repo: str | None) -> dict:
    """``gh pr view <pr> [--repo …] --json …`` → parsed metadata dict.

    Raises :class:`ReviewError` if gh can't resolve the PR.
    """
    try:
        raw = gh.pr_view(
            str(pr),
            repo=repo,
            json_fields=[
                "number",
                "headRefName",
                "headRefOid",
                "baseRefName",
                "baseRefOid",
            ],
        )
    except gh.GhError as exc:
        raise ReviewError(
            f"Could not resolve PR #{pr}"
            + (f" in {repo}" if repo else "")
            + f" via `gh pr view`: {exc}"
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewError(
            f"Unparseable `gh pr view` output for PR #{pr}: {exc}"
        ) from exc


def resolve_pr(
    pr: int,
    *,
    repo: str | None = None,
    workdir: str | None = None,
) -> PRContext:
    """Resolve PR ``pr`` to a :class:`PRContext` (diff + changed files + workdir).

    * ``repo`` (``OWNER/NAME``) targets a specific repo; ``None`` lets ``gh``
      infer the repo from ``workdir``'s remote.
    * ``workdir`` is the checkout the agent reads files from; defaults to the
      current directory (the consumer reviewing their own PR).

    Resolves BOTH endpoints authoritatively from ``gh pr view`` — the head from
    ``headRefOid`` and the base from ``baseRefOid`` — then fetches each as a known
    commit object (never a branch switch) and computes the three-dot diff
    ``base_sha...head_sha``. Both SHAs are HARD preconditions: a base or head that
    can't be made present fails loud rather than silently degrading to a local
    ref or the base tip (the review must never run against a stale/wrong base).
    Raises :class:`ReviewError` if ``workdir`` is not a git checkout, the PR can't
    be resolved, or either commit can't be fetched into ``workdir``.
    """
    workdir = workdir or os.getcwd()
    toplevel = _git_toplevel(workdir)
    if toplevel is None:
        raise ReviewError(
            f"{workdir!r} is not a git checkout — `shipit pr review` resolves a "
            f"PR by diffing inside a clone of the repository. cd into the repo (or "
            f"pass a checkout) and re-run."
        )
    # Normalize to the repo root: the backend runs with cwd=workdir and the
    # review prompt names repo-root-relative paths, so a nested-subdir cwd would
    # leave those paths unopenable for the agent.
    workdir = toplevel

    # Normalize any explicit repo slug to its canonical owner/name. An aliased
    # slug (e.g. a transferred/renamed repo) 307-redirects on GET but NOT on
    # POST, so posting a review to it hard-fails with HTTP 307. Normalizing here
    # — at the boundary where the external slug enters — keeps ALL downstream
    # consumers (generation AND posting) on the canonical slug. When repo is
    # None, gh infers it from the checkout, which is already canonical.
    if repo is not None:
        try:
            repo = gh.repo_canonical(repo)
        except gh.GhError as exc:
            raise ReviewError(
                f"Could not resolve repo {repo!r} to its canonical owner/name via "
                f"`gh repo view`: {exc}"
            ) from exc

    meta = _pr_meta(pr, repo)
    base_ref = meta.get("baseRefName") or "main"
    base_sha = meta.get("baseRefOid") or ""
    head_sha = meta.get("headRefOid") or ""
    head_ref = meta.get("headRefName") or ""

    # The head endpoint of the diff is ALWAYS the resolved head sha
    # (``headRefOid`` from ``gh pr view``). We never fall back to FETCH_HEAD or
    # HEAD: FETCH_HEAD may point at the base ref we just fetched (silently
    # diffing the wrong thing), and HEAD is the user's unrelated working tree.
    if not head_sha:
        raise ReviewError(
            f"PR #{pr} returned no head sha (headRefOid) from `gh pr view` — "
            f"cannot resolve the PR head to review."
        )

    # The base endpoint is resolved the SAME authoritative way as the head:
    # ``baseRefOid`` from `gh pr view` is a known commit object, so the review
    # diffs against the PR's REAL base — never against whatever a local
    # `origin/<base>` happens to point at (which may be stale or missing). A
    # missing baseRefOid fails loud rather than degrading to a guessed base.
    if not base_sha:
        raise ReviewError(
            f"PR #{pr} returned no base sha (baseRefOid) from `gh pr view` — "
            f"cannot resolve the PR base to review against."
        )

    # Make the head commit object available locally (fetch only — never a
    # checkout-switch). Try the PR head ref namespace first (works without the
    # branch being local), then the named head branch, then the sha directly.
    if not _sha_present(workdir, head_sha):
        _git(workdir, ["fetch", "--quiet", "origin", f"pull/{pr}/head"], check=False)
        if not _sha_present(workdir, head_sha) and head_ref:
            _git(workdir, ["fetch", "--quiet", "origin", head_ref], check=False)
        if not _sha_present(workdir, head_sha):
            _git(workdir, ["fetch", "--quiet", "origin", head_sha], check=False)

    if not _sha_present(workdir, head_sha):
        raise ReviewError(
            f"Can't resolve PR #{pr} head {head_sha} — the commit isn't available "
            f"after fetching pull/{pr}/head, the head branch, and the sha directly. "
            f"The PR may be from a fork (its head isn't on origin) or the head is "
            f"otherwise unavailable; fetch it into this checkout and re-run."
        )

    head_point = head_sha

    # Make the base commit object available the SAME way — fetch the base branch
    # (its tip is baseRefOid), then the sha directly — and FAIL LOUD if it still
    # isn't present. No silent degrade to a local `origin/<base>` ref or to the
    # base tip: an unfetchable base SHA stops the review rather than diffing
    # against the wrong base.
    if not _sha_present(workdir, base_sha):
        _git(workdir, ["fetch", "--quiet", "origin", base_ref], check=False)
        if not _sha_present(workdir, base_sha):
            _git(workdir, ["fetch", "--quiet", "origin", base_sha], check=False)

    if not _sha_present(workdir, base_sha):
        raise ReviewError(
            f"Can't resolve PR #{pr} base {base_sha} (baseRefOid) — the commit "
            f"isn't available after fetching the base branch '{base_ref}' and the "
            f"sha directly. Fetch it into this checkout and re-run rather than "
            f"reviewing against a stale or wrong base."
        )

    try:
        diff = _git(workdir, ["diff", f"{base_sha}...{head_point}"]).stdout
        names = _git(
            workdir, ["diff", "--name-only", f"{base_sha}...{head_point}"]
        ).stdout
    except proc.ProcError as exc:
        raise ReviewError(
            f"failed to compute diff for PR #{pr} ({base_sha}...{head_point}): {exc}"
        ) from exc
    changed_files = [line for line in names.splitlines() if line.strip()]

    return PRContext(
        number=pr,
        repo=repo,
        head_sha=head_sha,
        base_ref=base_ref,
        base_sha=base_sha,
        diff=diff,
        changed_files=changed_files,
        workdir=workdir,
    )
