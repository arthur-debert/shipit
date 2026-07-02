"""diff — resolve a PR to the diff (and changed files) the review runs over.

This replaces the Phase-1.2 single-base stepping-stone (a bare ``git diff
<base>...HEAD`` in cwd) with real PR resolution: given a PR number, ask GitHub
for the PR's base/head refs, make the base + head available locally (FETCH only —
never a branch switch, so the user's working tree is untouched), and compute the
three-dot diff and changed-file list the agent reviews.

The CHECKOUT model: the agent backend reads files from ``ReviewView.workdir`` so
it can open the surrounding source for context. When the review runs in the
consumer's own checkout of the PR (``workdir`` defaults to cwd) the head is
typically already at ``HEAD``; otherwise we fetch the PR head as an object and
diff against it. We never switch branches — if the head isn't the current
working tree, the agent can still read the changed content via the diff and via
``git show <sha>:<path>``, but a full file-tree read of the head requires that
the head actually be checked out (documented limitation, not a branch switch).

``ReviewView`` is the **review path's** richer view (ADR-0024): it *composes* a
canonical :class:`shipit.pr.PR` (identity + cheap core) and adds the diff /
changed_files / workdir the review runs over. It replaces the old ``PRContext``
snapshot — the core (``number`` / ``head_sha`` / ``base_ref``) now lives on the
composed ``PR``, read via delegating properties, and the PR identity's repo is set
authoritatively ONLY from an explicit ``--repo`` slug (canonicalized here); an
omitted ``--repo`` leaves it the honest-None placeholder so downstream canonicalizes
via ``gh repo view`` rather than trusting the checkout's (possibly aliased) origin.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .. import execrun, gh, git
from ..identity import Sha, repo_from_slug
from ..pr import PR, core_from_node


class ReviewError(RuntimeError):
    """A review precondition failed (not a git checkout, PR unresolvable, …).

    Carries an actionable message — the facade prints it and exits nonzero.
    """


@dataclass
class ReviewView:
    """The review path's view of one PR: a canonical :class:`shipit.pr.PR`
    (identity + cheap core) enriched with the diff + changed files + workdir the
    review runs over.

    Composes a ``PR`` rather than re-declaring the core (ADR-0024): ``number`` /
    ``head_sha`` / ``base_ref`` are read straight off ``self.pr`` via delegating
    properties, so the review path exposes exactly the core its ``PR`` fetched.
    ``base_sha`` / ``diff`` / ``changed_files`` / ``workdir`` / ``head_ref`` are
    review-ONLY enrichments the readiness path never fetches, so they live on the
    view, not the shared core.
    """

    pr: PR
    base_sha: str
    diff: str
    changed_files: list[str] = field(default_factory=list)
    workdir: str = "."
    # The PR head BRANCH name (``headRefName`` from `gh pr view`). The funnel
    # producer (`shipit.review.producer`) needs it to provision the shared
    # read-only Tree (ADR-0018) on the PR head — `resolve_pr` already reads it for
    # the head fetch, so it is surfaced here rather than re-resolved. Empty only
    # for a hand-built context (tests); a resolved PR always carries it.
    head_ref: str = ""

    # --- core + identity, delegated to the composed PR (ADR-0024) -----------
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
        """The ``owner/name`` slug the review posts to — the PR identity's repo,
        resolved once at :func:`resolve_pr`. Downstream posters/producers read this
        as a slug string exactly as they did the old ``PRContext.repo``.

        Returns ``None`` — NOT the ``local/local`` placeholder slug — when this view
        was hand-built without a known repo (the :data:`_HANDBUILT_REPO` identity).
        That preserves the old ``PRContext.repo`` FALSEY contract (ADR-0024): the
        review path's ``repo`` is authoritative for a resolved PR, but a hand-built
        context honestly reports "repo not independently known" so downstream
        posters/producers keep their ``gh repo view`` fallback instead of silently
        posting/provisioning against ``local/local``."""
        if self.pr.repo == _HANDBUILT_REPO:
            return None
        return self.pr.repo.slug


def review_view(
    *,
    number: int,
    head_sha: str | Sha,
    base_ref: str | None,
    base_sha: str,
    diff: str,
    is_draft: bool,
    repo: str | None = None,
    merge_state: str | None = None,
    changed_files: list[str] | None = None,
    workdir: str = ".",
    head_ref: str = "",
) -> ReviewView:
    """Compose a :class:`ReviewView` from flattened fields — the ergonomic builder
    for callers (and tests) that hold the values directly rather than a raw node.

    ``repo`` is an ``owner/name`` slug (parsed into the PR identity's
    :class:`shipit.identity.Repo`); ``None`` yields the :data:`_HANDBUILT_REPO`
    placeholder identity (which :attr:`ReviewView.repo` surfaces as ``None``) for a
    hand-built context. ``is_draft`` is REQUIRED (no default) — mirroring
    :func:`shipit.prstate.model.readiness_view`: the shared ``PR`` core carries
    ``is_draft`` and the real review path genuinely fetches it (:func:`resolve_pr`
    → :func:`core_from_node`), so this convenience builder must not silently
    fabricate a ``False`` and reintroduce the defaulted-core-field trap this WS
    exists to remove (ADR-0024). The review path never READS ``is_draft`` /
    ``merge_state`` off the view, but the core still carries what it fetched.
    """
    pr = PR(
        repo=repo_from_slug(repo) if repo else _HANDBUILT_REPO,
        number=number,
        head_sha=head_sha if isinstance(head_sha, Sha) else Sha(head_sha),
        base_ref=base_ref,
        is_draft=is_draft,
        merge_state=merge_state,
    )
    return ReviewView(
        pr=pr,
        base_sha=base_sha,
        diff=diff,
        changed_files=changed_files if changed_files is not None else [],
        workdir=workdir,
        head_ref=head_ref,
    )


#: Placeholder repo identity for a hand-built :class:`ReviewView` with no slug —
#: a resolved PR always carries its real, canonical repo (see :func:`resolve_pr`).
_HANDBUILT_REPO = repo_from_slug("local/local")


def _git_toplevel(workdir: str) -> str | None:
    """The git working-tree root for ``workdir``, or ``None`` when not a checkout.

    The backend reads files with ``cwd=ctx.workdir`` and the review prompt names
    paths relative to the REPO ROOT, so running from a nested subdir would leave
    repo-relative paths unopenable. Normalizing ``workdir`` to the toplevel makes
    the agent's cwd the repo root regardless of where the command was invoked.

    Routes through the single :func:`shipit.git.repo_root` boundary (ADR-0024)
    rather than re-implementing ``git rev-parse --show-toplevel``.
    """
    return git.repo_root(cwd=workdir)


def _pr_meta(pr: int, repo: str | None) -> dict:
    """``gh pr view <pr> [--repo …] --json …`` → parsed metadata dict.

    Raises :class:`ReviewError` if gh can't resolve the PR.
    """
    try:
        raw = gh.pr_view(
            str(pr),
            repo=repo,
            json_fields=[
                # The PR CORE (`pr.CORE_JSON_FIELDS`: number/headRefOid/baseRefName/
                # isDraft/mergeStateStatus) so the view's core is built through the
                # one `core_from_node` boundary — the SAME extraction the readiness
                # path uses — plus the review-only endpoints (headRefName for the
                # head fetch + Tree, baseRefOid for the authoritative base diff).
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
) -> ReviewView:
    """Resolve PR ``pr`` to a :class:`ReviewView` (diff + changed files + workdir).

    * ``repo`` (``OWNER/NAME``) targets a specific repo (canonicalized here); ``None``
      leaves the PR identity's repo the honest-None placeholder so downstream
      posters/producers canonicalize via ``gh repo view`` — NOT the checkout's
      (possibly aliased) origin, which would 307 on write (ADR-0024).
    * ``workdir`` is the checkout the agent reads files from; defaults to the
      current directory (the consumer reviewing their own PR).

    Resolves BOTH endpoints authoritatively from ``gh pr view`` — the head from
    ``headRefOid`` and the base from ``baseRefOid`` — then fetches each as a known
    commit object (never a branch switch) and diffs from their MERGE BASE (the PR
    branch point) to the head: GitHub's three-dot "Files changed" diff, computed
    from an authoritative base rather than a possibly-stale local ``origin/<base>``.
    Both SHAs are HARD preconditions: a base or head that can't be made present
    fails loud rather than silently degrading to a local ref or the base tip (the
    review must never run against a stale/wrong base). Raises :class:`ReviewError`
    if ``workdir`` is not a git checkout, the PR can't be resolved, either commit
    can't be fetched, or the two share no common ancestor.
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
        except execrun.ExecError as exc:
            raise ReviewError(
                f"Could not resolve repo {repo!r} to its canonical owner/name via "
                f"`gh repo view`: {exc}"
            ) from exc

    # The PR identity's repo is authoritative ONLY when it came from an explicit
    # `--repo` slug — which we canonicalized just above via `gh repo view`, following
    # GitHub's 307 for a transferred/renamed repo. When `--repo` is OMITTED we do NOT
    # synthesize an authoritative repo from the checkout's origin remote: that slug is
    # LOCAL and un-canonicalized (`identity.resolve_repo` is deliberately offline/
    # Tree-safe, ADR-0022/0024), so a checkout whose `origin` still points at an
    # old/alias slug would make downstream POST reviews / mint app-installation auth
    # against the alias (which 307s on write). Instead we leave the identity's repo the
    # honest-None placeholder (:data:`_HANDBUILT_REPO`, surfaced as `ReviewView.repo is
    # None`) so `post._resolve_repo` / the producer keep their `gh repo view` fallback,
    # which canonicalizes (follows the 307) exactly as before this epic.
    repo_obj = repo_from_slug(repo) if repo else _HANDBUILT_REPO

    meta = _pr_meta(pr, repo)
    # The CORE (number/head_sha/base_ref/is_draft/merge_state) is read off `meta`
    # through the one `core_from_node` boundary — the SAME extraction the readiness
    # path uses, so `head_sha` is fetched exactly one way. The review-only endpoints
    # (base sha + head branch) are read alongside it for the git diff + Tree.
    # The head endpoint of the diff is ALWAYS the resolved head sha
    # (``headRefOid`` from ``gh pr view``). We never fall back to FETCH_HEAD or
    # HEAD: FETCH_HEAD may point at the base ref we just fetched (silently
    # diffing the wrong thing), and HEAD is the user's unrelated working tree.
    # `core_from_node` mints the head into a `Sha` (COR02), so a missing, empty,
    # or malformed `headRefOid` — and any other unusable core field — fails HERE,
    # normalized to the review path's actionable `ReviewError`.
    try:
        pr_core = core_from_node(meta, repo_obj)
    except (KeyError, ValueError) as exc:
        raise ReviewError(
            f"PR #{pr} returned an unusable core from `gh pr view` ({exc}) — "
            f"cannot resolve the PR head to review."
        ) from exc
    base_ref = pr_core.base_ref or "main"
    base_sha = meta.get("baseRefOid") or ""
    # The git plumbing below (fetch/rev-parse/diff argv) works on the string
    # form; the composed `PR` core keeps the typed `Sha`.
    head_sha = str(pr_core.head_sha)
    head_ref = meta.get("headRefName") or ""

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
    if not git.commit_present(head_sha, cwd=workdir):
        git.fetch_ref(f"pull/{pr}/head", cwd=workdir)
        if not git.commit_present(head_sha, cwd=workdir) and head_ref:
            git.fetch_ref(head_ref, cwd=workdir)
        if not git.commit_present(head_sha, cwd=workdir):
            git.fetch_ref(head_sha, cwd=workdir)

    if not git.commit_present(head_sha, cwd=workdir):
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
    if not git.commit_present(base_sha, cwd=workdir):
        git.fetch_ref(base_ref, cwd=workdir)
        if not git.commit_present(base_sha, cwd=workdir):
            git.fetch_ref(base_sha, cwd=workdir)

    if not git.commit_present(base_sha, cwd=workdir):
        raise ReviewError(
            f"Can't resolve PR #{pr} base {base_sha} (baseRefOid) — the commit "
            f"isn't available after fetching the base branch '{base_ref}' and the "
            f"sha directly. Fetch it into this checkout and re-run rather than "
            f"reviewing against a stale or wrong base."
        )

    # The diff endpoint is the MERGE BASE of the authoritative base + head — the
    # point at which the PR branch diverged from its base — so the review sees
    # exactly the PR's own commits (GitHub's "Files changed" three-dot diff), never
    # commits that merely landed on the base after the branch point. We compute
    # merge-base explicitly (rather than relying on git's `A...B` shorthand) so the
    # endpoint is unambiguous, and FAIL LOUD if there is no common ancestor instead
    # of silently degrading to the base tip.
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
