"""git — the one git Tool adapter (ADR-0028).

Every ``git`` argv in shipit is encoded HERE — building one anywhere else is a
review defect (CONTEXT.md: **Tool adapter**). This is the git half of the old
``gh.py`` "GitHub / git boundary", consolidated into its own adapter so each
tool has exactly one home: :mod:`shipit.gh` keeps the ``gh`` surface; the
review-diff path's former direct git calls route through here.

Execution routes through the one Exec runner (ADR-0028): every call is an Exec
via :func:`shipit.execrun.run` with a stated timeout, one structured record per
Exec, and central redaction. A failed invocation raises the single transport
error :class:`shipit.execrun.ExecError` — this adapter defines no error class
of its own.

Output parsing is centralized here too — the adapter harvests git's most
structured output (porcelain / plumbing formats) and returns parsed values:
``ls-remote`` refname-column equality (:func:`remote_branch_exists`),
``rev-list`` sha validation (:func:`_validated_shas`), and porcelain status
lines (:func:`status_porcelain`). Mutation-heavy operations (clone / fetch /
checkout / push) keep their thin typed-function shape.

Two call styles, matching the two kinds of git asks:

- :func:`_git` — ``check=True``; a nonzero exit is a FAILURE and raises
  :class:`ExecError` (the mutations, and reads whose failure is exceptional).
- :func:`_probe` — ``check=False``; a nonzero exit is a NORMAL answer (absent
  ref, no upstream, not a checkout) recorded at DEBUG, and the caller branches
  on the result instead of catching.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from . import execrun
from .execrun import ExecError

if TYPE_CHECKING:
    # Type-only: `identity` composes over this boundary module (its resolvers
    # default to it), so a runtime top-level import would cycle. Construction
    # sites import `Sha` lazily inside the function instead.
    from .identity import Sha

#: The adapter's own logger (ADR-0029 spray): the Exec runner already records
#: every git subprocess, so this logger speaks only when the adapter makes a
#: DECISION of its own — today, the #353 degraded-clone retry WARNING.
logger = logging.getLogger("shipit.git")

#: Stated per-Exec timeouts (ADR-0028: every Exec carries one; nothing hangs by
#: default). Local git plumbing is near-instant and gets a tight bound; the
#: calls that talk to a remote (clone/fetch/push/ls-remote) get the runner's
#: generous default; the dissociated clone copies the full object store into
#: the new checkout (ADR-0014), and ``git clean -ffdx`` unlinks a fully
#: materialized environment and build cache — both are bulk-filesystem work
#: whose runtime scales with on-disk artifacts, not plumbing, so they get a
#: larger ceiling.
_NETWORK_TIMEOUT: float = execrun.DEFAULT_TIMEOUT
_LOCAL_TIMEOUT: float = 60.0
_CLONE_TIMEOUT: float = 600.0
_STRIP_TIMEOUT: float = 600.0


def _argv(args: list[str], cwd: str | None) -> list[str]:
    """The one place a ``git`` argv is assembled: ``git [-C <cwd>] <args>``.

    ``-C`` rather than the runner's ``cwd=`` so the executed argv — the thing
    the Exec record logs — states the checkout it ran against on its face.
    """
    return ["git", "-C", cwd, *args] if cwd is not None else ["git", *args]


def _git(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float | None = _LOCAL_TIMEOUT,
) -> str:
    """Run ``git`` through the Exec runner, returning stdout; raises :class:`ExecError`."""
    return execrun.run(_argv(args, cwd), timeout=timeout).stdout


def _probe(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float | None = _LOCAL_TIMEOUT,
) -> execrun.ExecResult:
    """Run ``git`` as a probe: a nonzero exit is a NORMAL answer, not a failure.

    ``check=False`` through the runner (ADR-0028): the Exec still gets its one
    record, but at DEBUG — an absent-ref check, a no-upstream read, or a
    not-a-checkout read happens on every routine scan/hook and must not spray
    ERROR records over normal flows. The caller branches on the result's
    ``rc``/``stdout`` instead of catching :class:`ExecError` (which the runner
    still raises for launch-level failures: missing binary, timeout).
    """
    return execrun.run(_argv(args, cwd), check=False, timeout=timeout)


# --------------------------------------------------------------------------
# checkout reads (identity / registry / hooks)
# --------------------------------------------------------------------------


def repo_root(*, cwd: str | None = None) -> str | None:
    """The git working-tree root for ``cwd`` (the current directory if omitted).

    ``None`` when ``cwd`` is not inside a checkout. This is THE single
    ``git rev-parse --show-toplevel`` boundary — the ``cwd`` parameter (ADR-0024)
    is what lets every caller route through it instead of re-implementing the
    command (``identity.resolve_working_dir``, the eval hook / report, review
    diff), so the toplevel is derived one way, in one place.
    """
    try:
        result = _probe(["rev-parse", "--show-toplevel"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    return result.stdout.strip() or None


def hooks_dir(*, cwd: str) -> Path | None:
    """The checkout's git hooks directory, resolved WORKTREE-correctly, or ``None``.

    ``git rev-parse --git-path hooks`` (ADR-0028: every git argv lives HERE) — the
    ONE place install resolves ``.git/hooks``. In a normal checkout that is
    ``<root>/.git/hooks``; in a LINKED worktree ``.git`` is a *file* (a ``gitdir:``
    pointer) and the hooks live under the SHARED common dir, so the old hardcoded
    ``root / ".git" / "hooks"`` guarded by ``.is_dir()`` reads the absent dir as
    "no hooks" and every install path that touches ``.git/hooks`` (the two
    activation preclean passes, the self-cert ``hooks`` postcondition) silently
    no-ops for a worktree consumer (#914). Routing them all through this one
    resolver fixes the whole module in one place rather than re-deriving
    lefthook's own resolution piecemeal.

    git returns the path RELATIVE to the queried checkout for a normal repo and
    ABSOLUTE for a worktree's shared common dir, so the answer is resolved against
    ``cwd`` (``os.path.join`` keeps an already-absolute answer verbatim). A probe:
    ``None`` when ``cwd`` is not a checkout (a normal nonzero answer) or a
    launch-level failure (missing git, timeout) — a best-effort read whose callers
    degrade to "no hooks dir" (the same no-op the old absent-dir guard produced)
    rather than crashing the install.
    """
    try:
        result = _probe(["rev-parse", "--git-path", "hooks"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    return Path(os.path.join(cwd, out))


def head_commit(*, cwd: str) -> Sha | None:
    """The current ``HEAD`` commit as a :class:`~shipit.identity.Sha`, or ``None``.

    A commit-IDENTITY read (PROC03): the return is the validated
    :class:`~shipit.identity.Sha` value object, never a raw string — callers
    compare identities through the type and stringify only at a serialization
    seam. ``None`` on any git failure (detached/unborn HEAD, not a checkout, or
    output that does not validate as a full sha) — the revision half of a
    :class:`shipit.identity.WorkingDir`, and the eval record's ``git.commit``
    stamp, are both best-effort: an unresolvable commit degrades to ``None``
    rather than raising.
    """
    from .identity import Sha  # lazy: see module-top TYPE_CHECKING note.

    try:
        result = _probe(["rev-parse", "HEAD"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return Sha(raw)
    except ValueError:
        return None


def current_branch(*, cwd: str) -> str | None:
    """The current branch name, or ``None`` on a detached/unborn HEAD."""
    try:
        result = _probe(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    name = result.stdout.strip()
    return None if (not name or name == "HEAD") else name


def default_branch(*, cwd: str, remote: str = "origin") -> str:
    """The remote's default branch name (e.g. ``main``), from ``<remote>/HEAD``.

    Reads the local ``refs/remotes/<remote>/HEAD`` symbolic ref a clone points at
    the remote's default branch and strips the ``<remote>/`` prefix. This is the
    base the MODE_PR install flow resets its ``shipit/install`` staging branch
    onto (#852): the staging branch must be based on the CURRENT default branch,
    never on whatever HEAD a Tree was cut from, or a Tree cut from a stale
    leftover remote ``shipit/install`` head would stack a conflicting commit.

    A PROBE, not a mutation: a missing symref (some reference-borrow clones never
    set ``<remote>/HEAD``) is a normal answer. Rather than blindly returning
    ``main`` — which would mis-resolve a ``master``/``trunk`` remote and then
    crash the MODE_PR reset onto a non-existent ``origin/main`` — the fallback
    PROBES the common default-branch names against the remote-tracking refs a
    fetch populated, ``main`` first (the portfolio default), and only when none
    exist returns ``main`` as the last resort. A launch-level failure (missing
    git, timeout) still propagates :class:`ExecError`.
    """
    result = _probe(["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"], cwd=cwd)
    if result.ok:
        name = result.stdout.strip().removeprefix(f"{remote}/")
        if name:
            return name
    for candidate in ("main", "master", "trunk"):
        probe = _probe(
            ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{candidate}"],
            cwd=cwd,
        )
        if probe.ok:
            return candidate
    return "main"


def remote_url(*, cwd: str, remote: str = "origin") -> str:
    """The configured URL of ``remote`` for the checkout at ``cwd``."""
    return _git(["remote", "get-url", remote], cwd=cwd).strip()


def status_porcelain(*, cwd: str) -> list[str]:
    """Machine-readable working-tree status, parsed to its non-empty lines.

    ``git status --porcelain``: an empty list means a clean tree; each line is
    one changed/untracked entry (``XY <path>``). The PARSED list — not the raw
    text — is the return so the line-splitting lives here (the centralized
    porcelain read), and callers ask their own question of it: dirty-at-all
    (truthiness), how-dirty (``len``), or the lines themselves (diagnostics).
    """
    out = _git(["status", "--porcelain"], cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


def ls_files(*, cwd: str) -> list[str]:
    """Tracked files (``git ls-files``), repo-root-relative, in git's order.

    Tracked-only is deliberate: it keeps generated/ignored paths out of the lint
    scope without an exclude list (docs/legacy-prd/lint-checks.md — "whole tree via git ls-files").
    """
    out = _git(["ls-files"], cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


def ls_files_matching(pathspecs: list[str], *, cwd: str) -> list[str] | None:
    """Tracked files matching ``pathspecs``, or ``None`` when ``cwd`` is no git repo.

    The pathspec-scoped sibling of :func:`ls_files` (#547: install's toolchain
    signal detection reads the tracked manifest names through it). A probe read:
    not-a-repo is a NORMAL answer (``None`` — the caller falls back to its
    non-git heuristic), never an exception; NUL-delimited (``-z``) so paths with
    spaces/newlines survive, tracked-only for the same reason as :func:`ls_files`.
    """
    res = _probe(["ls-files", "-z", "--", *pathspecs], cwd=cwd)
    if not res.ok:
        return None
    return [p for p in res.stdout.split("\0") if p.strip()]


def epic_umbrella_exists(epic: str, *, cwd: str) -> bool:
    """Whether ``<epic>/umbrella`` exists as a branch in the checkout at ``cwd``.

    The semantic test for "is ``<epic>`` a real epic?": ADR-0016 gives every epic an
    ``<epic>/umbrella`` branch, so the umbrella's existence IS the epic's existence —
    a sturdier signal than any branch-name *grammar* proxy (robust to naming drift).
    The WorktreeCreate hook uses it to tell a true epic prefix (``TRE04`` →
    ``TRE04/umbrella`` exists) from an ordinary slash-branch a coordinator happens to
    sit on (``feature/foo`` → no ``feature/umbrella``), so only a real epic namespaces
    the holding branch.

    A **LOCAL** ref lookup, deliberately NOT a network ``git ls-remote``: the hook
    fires synchronously inside a spawn, and the coordinator's clone already carries the
    umbrella's tracking ref — so no network round-trip gates the spawn. Checks the
    remote-tracking ref first (``refs/remotes/origin/<epic>/umbrella``, the usual shape
    in a clone), then a local head (``refs/heads/<epic>/umbrella``). Uses ``git
    show-ref --verify`` with the EXACT full ref (never a pattern — avoids a glob
    matching an unrelated ref), so a garbage ``epic`` (separators, ``..``) simply
    yields a ref that does not resolve → ``False`` → the caller's safe epic-less
    fallback.

    An absent ref is a NORMAL answer (``_probe`` reports the nonzero exit as
    ``ok=False`` → "not an epic"); a launch-level failure (missing git, timeout)
    raises :class:`ExecError` instead of masquerading as that same ``False`` —
    the disposition shared with the other probe reads (:func:`commit_present`,
    :func:`fetch_ref`, :func:`merge_base`). The one caller is the fail-CLOSED
    WorktreeCreate hook, whose catch-all turns the raise into a loudly aborted
    spawn — strictly better than silently degrading a real epic's spawn to a
    mis-based epic-less holding branch.
    """
    for ref in (
        f"refs/remotes/origin/{epic}/umbrella",
        f"refs/heads/{epic}/umbrella",
    ):
        if _probe(["show-ref", "--verify", "--quiet", ref], cwd=cwd).ok:
            return True
    return False


def remote_branch_exists(
    branch: str, *, cwd: str | None = None, remote: str = "origin"
) -> bool:
    """Whether ``branch`` exists on ``remote`` (``git ls-remote --heads``).

    A live query of the remote — not the local tracking refs — so a caller can
    fail-closed on a missing base branch BEFORE cloning, without relying on a prior
    fetch having populated a tracking ref. Raises :class:`ExecError` if the
    ``git ls-remote`` call itself fails (no network / bad remote), so an
    undetermined remote state is never silently read as "branch absent".

    Exact, not pattern. ``git ls-remote`` treats its final argument as a ref
    *pattern*, so a bare branch name carrying a glob metacharacter (``*``,
    ``?``, ``[``) or one that happens to match a *different* head could be
    reported as present even when ``refs/heads/<branch>`` is absent — a false
    positive that would defeat the fail-closed precondition. Two guards make
    this exact:

    * a branch name carrying a glob metacharacter can never name a real git
      ref (git forbids those characters in refnames), so it short-circuits to
      ``False`` and is never sent to ``git`` as a pattern; and
    * the query asks for the fully-qualified ``refs/heads/<branch>`` and the
      output is parsed line-by-line (``<sha>\\t<refname>``), returning ``True``
      only when some line's refname column equals exactly ``refs/heads/<branch>``
      — never merely "the output was non-empty".

    The net guarantee: returns ``True`` iff ``refs/heads/<branch>`` genuinely
    exists on ``remote``.
    """
    # A real git refname can never contain these; refuse to send one as a pattern.
    if any(ch in branch for ch in "*?["):
        return False
    ref = f"refs/heads/{branch}"
    out = _git(["ls-remote", "--heads", remote, ref], cwd=cwd, timeout=_NETWORK_TIMEOUT)
    for line in out.splitlines():
        # Each line is "<sha>\t<refname>"; require exact refname equality.
        parts = line.split("\t")
        if len(parts) == 2 and parts[1] == ref:
            return True
    return False


# --------------------------------------------------------------------------
# Tree-registry reads (scan reads; never mutates)
# --------------------------------------------------------------------------


def upstream_ref(*, cwd: str) -> str | None:
    """The branch's configured upstream tracking ref (e.g. ``origin/main``), or ``None``.

    This is the only durable, on-disk record of what a Tree's branch is measured
    against — there is NO manifest (PRD: the clones on disk are the whole store), so
    ``scan`` reports the upstream git itself tracks as the Tree's *base*. ``None`` when
    the branch has no upstream (never pushed / set), which ``scan`` surfaces as such.
    """
    try:
        result = _probe(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            cwd=cwd,
        )
    except ExecError:
        return None
    if not result.ok:
        return None
    return result.stdout.strip() or None


def ahead_behind(*, cwd: str) -> tuple[int, int]:
    """``(ahead, behind)`` commit counts of ``HEAD`` vs its upstream.

    ``ahead`` is commits on ``HEAD`` not yet on the upstream (unpushed); ``behind`` is
    commits on the upstream not yet on ``HEAD``. ``(0, 0)`` when there is no upstream
    (or the rev-list fails), so a freshly-cut Tree reads as level rather than erroring.
    """
    try:
        result = _probe(
            ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"], cwd=cwd
        )
    except ExecError:
        return (0, 0)
    if not result.ok:
        return (0, 0)
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return (0, 0)
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return (0, 0)
    return (ahead, behind)


def unpushed_shas(*, cwd: str) -> tuple[Sha, ...] | None:
    """The :class:`~shipit.identity.Sha`\\s of commits on ``HEAD`` that exist on NO
    remote — ``None`` if unreadable.

    The upstream-independent "unpushed" signal ADR-0027's ephemeral gc ladder is
    defined over: :func:`ahead_behind`'s ``ahead`` reads ``(0, 0)`` for a branch
    with **no upstream**, so a fresh ``ephemeral/<id>`` branch carrying local-only
    commits would look level — exactly the misread that loses work. ``rev-list HEAD
    --not --remotes`` lists commits reachable from ``HEAD`` but from no remote ref,
    so a missing upstream never by itself blocks reclaim (empty = everything on some
    remote) while a genuinely local commit is always listed. The SHAs — not just a
    count — are what lets the ephemeral floor exclude exactly the recorded
    provisioning commit (#232) while any OTHER local-only commit still protects;
    each is a validated :class:`~shipit.identity.Sha` value object (PROC03), so
    the exclusion compares identities through the type, never raw strings.

    ``None`` — not empty — when the list cannot be read (detached/unborn HEAD, a git
    failure, malformed output): the CALLER must treat "unknown" conservatively (keep),
    and collapsing it to "nothing unpushed" would point the failure mode at data loss.
    """
    try:
        result = _probe(["rev-list", "HEAD", "--not", "--remotes"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    shas = _validated_shas(result.stdout)
    return tuple(shas) if shas is not None else None


def commits_between(base: Sha, head: Sha, *, cwd: str) -> list[Sha] | None:
    """The :class:`~shipit.identity.Sha`\\s reachable from ``head`` but not ``base``
    (``rev-list base..head``).

    Typed at both ends (PROC03): the endpoints are commit identities — the
    :class:`~shipit.identity.Sha`\\s :func:`head_commit` returned — and the range
    comes back as ``Sha`` value objects. Used at Tree provisioning to identify
    exactly what the managed-set install committed (#232): the commits between
    the pre- and post-install ``HEAD``. ``None`` on any git failure or malformed
    output so the caller records nothing rather than something wrong — an
    unrecorded provisioning commit only KEEPS the Tree.
    """
    try:
        result = _probe(["rev-list", f"{base}..{head}"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    return _validated_shas(result.stdout)


def _validated_shas(out: str) -> list[Sha] | None:
    """Parse ``git rev-list`` output into :class:`~shipit.identity.Sha` values.

    Validity lives in the :class:`shipit.identity.Sha` constructor (COR02) — the
    old ad-hoc "looks like a sha" check retired into the type — and the VALUES
    are the type itself (PROC03), not its string form: rev-list output is commit
    identity, so it leaves the adapter as ``Sha``. Malformed output yields
    ``None`` (record nothing rather than something wrong), matching the callers'
    conservative contract.
    """
    from .identity import Sha  # lazy: see module-top TYPE_CHECKING note.

    try:
        return [Sha(line.strip()) for line in out.splitlines() if line.strip()]
    except ValueError:
        return None


# --------------------------------------------------------------------------
# review-diff reads (resolve a PR's endpoints; fetch-only, never a switch)
# --------------------------------------------------------------------------


def resolve_commit(rev: str, *, cwd: str) -> Sha | None:
    """Resolve a revision NAME (branch, tag, sha prefix, ``HEAD~2``, …) to the
    commit :class:`~shipit.identity.Sha` it names in ``cwd``, or ``None``.

    ``git rev-parse --verify --quiet <rev>^{commit}`` as a probe: the
    commit-range review path (RVW02-WS03 replay) takes ARBITRARY user-supplied
    endpoints, so — unlike the PR path, whose endpoints arrive as validated
    ``gh``-supplied oids — the raw name is resolved to a typed commit identity
    HERE, at the one git boundary. ``None`` means "not a commit in this
    checkout" (unknown ref, ambiguous name, a non-commit object) so the caller
    can fail loud with its own actionable message; a launch-level failure
    (missing git, timeout) raises :class:`ExecError` rather than collapsing
    into that same ``None``. Output that does not validate as a full sha
    returns ``None`` too — the adapter's conservative parse contract: nothing
    rather than something wrong.
    """
    from .identity import Sha  # lazy: see module-top TYPE_CHECKING note.

    result = _probe(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd=cwd)
    if not result.ok:
        return None
    raw = result.stdout.strip()
    try:
        return Sha(raw)
    except ValueError:
        return None


def commit_present(sha: Sha, *, cwd: str) -> bool:
    """True if ``sha`` is a commit object reachable in ``cwd`` (no fetch).

    ``git cat-file -e <sha>^{commit}`` as a probe: the review-diff path asks it
    before and after each fetch attempt to decide whether a PR endpoint is
    locally available. The endpoint is a commit identity, so it arrives as a
    :class:`~shipit.identity.Sha` (PROC03) — the old "empty sha is trivially
    absent" guard is gone because an empty ``Sha`` is unconstructible.

    A launch-level failure (missing git, timeout) is NOT "absent": ``_probe``
    already answers a normal nonzero exit as ``ok=False`` (the sha is genuinely
    not present), so its :class:`ExecError` — raised only for a failed
    invocation — propagates rather than being misread as a clean absence.
    """
    return _probe(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=cwd).ok


def fetch_ref(refspec: str, *, cwd: str, remote: str = "origin") -> bool:
    """Best-effort ``git fetch --quiet <remote> <refspec>`` — True if the fetch ran clean.

    A PROBE, not a mutation contract: the review-diff path tries several
    candidate refspecs for a PR endpoint (``pull/<n>/head``, the head branch,
    the bare sha) and re-checks :func:`commit_present` after each, so an
    individual fetch failing (ref absent on the remote) is a normal answer —
    ``_probe`` reports it as ``ok=False``. A launch-level failure (missing git,
    timeout) is not a normal answer: its :class:`ExecError` propagates rather
    than masquerading as a cleanly-absent ref.

    ``refspec`` stays ``str`` — deliberately, while the surrounding diff
    plumbing is typed (PROC03): this is the one seam that takes MIXED refspecs
    (branch names, ``pull/<n>/head``, bare shas), so a caller holding a
    :class:`~shipit.identity.Sha` stringifies HERE rather than the adapter
    pretending every refspec is a commit identity.
    """
    return _probe(
        ["fetch", "--quiet", remote, refspec], cwd=cwd, timeout=_NETWORK_TIMEOUT
    ).ok


def merge_base(a: Sha, b: Sha, *, cwd: str) -> Sha | None:
    """The merge base of commits ``a`` and ``b``, or ``None`` when they share no ancestor.

    Typed at both ends (PROC03): the endpoints are commit identities and the
    merge base IS one, so it leaves the adapter as a validated
    :class:`~shipit.identity.Sha` — the review-diff path hands it straight to
    :func:`diff_range` / :func:`diff_name_only` without a raw-string hop.

    ``None`` — never a guessed endpoint — so the review-diff path can FAIL LOUD
    on unrelated histories instead of silently diffing against the base tip.
    ``None`` means "no usable merge base": the nonzero exit for no common
    ancestor (``_probe`` reports it as ``ok=False``) and, per the adapter's
    conservative parse contract, output that does not validate as a full sha —
    return nothing rather than something wrong. A launch-level failure raises
    :class:`ExecError` rather than collapsing into that same ``None``.
    """
    from .identity import Sha  # lazy: see module-top TYPE_CHECKING note.

    result = _probe(["merge-base", str(a), str(b)], cwd=cwd)
    if not result.ok:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return Sha(raw)
    except ValueError:
        return None


def is_ancestor(ancestor: Sha, descendant: Sha, *, cwd: str) -> bool:
    """True iff ``ancestor`` is a first-parent-or-any ancestor of ``descendant``
    (``git merge-base --is-ancestor``).

    The convergence gate for incremental review rounds (RVW02-WS06, ADR-0045):
    a round after the first reviews only ``last-reviewed-head..new-head``, which
    is a meaningful *fix range* ONLY when the last-reviewed head is still in the
    new head's history. A rebase or force-push rewrites that history, so the old
    head is no longer an ancestor — the incremental premise is void and the
    caller must fall back to a full-PR round (fail toward over-reviewing).

    A PROBE, not a hard command: ``git merge-base --is-ancestor`` exits 0 when
    the ancestry holds, 1 when it does not, and something else on error (a
    commit not present in this checkout, a broken repo). Only exit 0 returns
    ``True``; EVERY other outcome — a genuine non-ancestor AND any error —
    returns ``False``, so an unresolvable ancestry check degrades to the SAFE
    side (a full round), never a wrongly-narrowed incremental one. A
    launch-level failure (missing binary) still raises :class:`ExecError`
    through :func:`_probe`'s runner, exactly like the other probes.
    """
    return _probe(
        ["merge-base", "--is-ancestor", str(ancestor), str(descendant)], cwd=cwd
    ).ok


def diff_range(base: Sha, head: Sha, *, cwd: str) -> str:
    """The two-dot diff ``git diff <base>..<head>`` — the patch text between two commits.

    The endpoints are commit identities, taken as :class:`~shipit.identity.Sha`
    (PROC03) and stringified only into the argv here. The review path passes an
    explicitly computed :func:`merge_base` as ``base``, which makes this
    GitHub's three-dot "Files changed" diff with an unambiguous, pre-resolved
    endpoint. Raises :class:`ExecError` on failure — by the time the diff runs
    both endpoints are proven present, so a failure is exceptional.
    """
    return _git(["diff", f"{base}..{head}"], cwd=cwd)


def diff_name_only(base: Sha, head: Sha, *, cwd: str) -> list[str]:
    """The paths changed between two commits (``git diff --name-only <base>..<head>``).

    Parsed to a list here (the adapter owns output parsing); same endpoint
    contract as :func:`diff_range`. Raises :class:`ExecError` on failure.
    """
    out = _git(["diff", "--name-only", f"{base}..{head}"], cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


def changed_paths_since(base_ref: str, *, cwd: str) -> list[str] | None:
    """The paths changed since diverging from ``base_ref`` — the three-dot
    ``git diff --name-only <base_ref>...HEAD`` (the merge-base diff, the same
    file set GitHub's "Files changed" shows for a PR).

    Unlike :func:`diff_name_only` the endpoint is a REF NAME (``origin/main``),
    not a proven :class:`~shipit.identity.Sha` — the lane planner's shell
    (:mod:`shipit.verbs.ci`) passes the PR base ref straight from the CI event.
    A probe: ``None`` when git cannot answer (unknown ref, a shallow clone
    missing the merge-base, not a checkout) — the caller treats an unknown
    diff as FULL scope, so a diff failure only ever runs more checks, never
    fewer.
    """
    res = _probe(["diff", "--name-only", f"{base_ref}...HEAD"], cwd=cwd)
    if res.rc != 0:
        return None
    return [line for line in res.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# mutations — thin typed functions (install / Tree creation / review reuse)
# --------------------------------------------------------------------------


def list_tags(*, cwd: str) -> list[str]:
    """Every tag name in ``cwd``'s checkout (``git tag --list``), unordered.

    The release version resolver's input (ADR-0041): it filters and orders
    the ``v<semver>`` tags itself (:func:`shipit.release.version.version_tags`)
    — the adapter hands over the raw name list and imposes no policy.
    """
    out = _git(["tag", "--list"], cwd=cwd)
    return [line.strip() for line in out.splitlines() if line.strip()]


def tag_annotated(name: str, message: str, *, cwd: str) -> None:
    """Create annotated tag ``name`` at ``HEAD`` with ``message`` as the
    annotation (``git tag -a -m``).

    The release prepare stage's tag write (ADR-0041: the tag is the version
    authority; its annotation carries THE one release-notes text, story 26).
    ``message`` rides argv — never a shell string — so arbitrary notes text is
    safe by construction (ADR-0028).
    """
    _git(["tag", "-a", name, "-m", message], cwd=cwd)


def push_tag(name: str, *, cwd: str, remote: str = "origin") -> None:
    """``git push <remote> refs/tags/<name>`` — publish one tag.

    Spelled with the full ref so a same-named branch can never be pushed by
    mistake (the release tag push must move exactly one ref).
    """
    _git(["push", remote, f"refs/tags/{name}"], cwd=cwd, timeout=_NETWORK_TIMEOUT)


def push_atomic(branch: str, tag: str, *, cwd: str, remote: str = "origin") -> None:
    """``git push --atomic <remote> <branch> refs/tags/<tag>`` — publish a
    branch and a tag as ONE server-side transaction.

    The release prepare stage's final (non-tag-only) publish: ``--atomic`` means
    the remote updates both refs or neither, so a tag-ref rejection can never
    leave the branch advanced while the tag is missing — a partial-published
    state the next run could neither resume (no remote tag) nor cleanly redo
    (the tree already carries the version). Like :func:`push_tag`, the tag rides
    its full ``refs/tags/`` ref so a same-named branch is never pushed by
    mistake; the push runs the repo's pre-push checks (story 24: no bypass).
    """
    _git(
        ["push", "--atomic", remote, branch, f"refs/tags/{tag}"],
        cwd=cwd,
        timeout=_NETWORK_TIMEOUT,
    )


def delete_tag(name: str, *, cwd: str) -> None:
    """``git tag -d <name>`` — remove a LOCAL tag.

    The release prepare stage's rollback for a failed publish: an annotated tag
    is written locally before the push, so a push failure must delete it again —
    otherwise the leftover local tag makes the next run falsely RESUME
    (ADR-0009 keys resume off tag existence) and report success on a cut that
    never reached the remote.
    """
    _git(["tag", "-d", name], cwd=cwd)


def switch_create(branch: str, *, cwd: str) -> None:
    """Create-or-reset ``branch`` from the current HEAD and switch to it.

    ``-C`` (force) so a re-run that reuses the install branch name starts clean
    rather than failing on an existing branch.
    """
    _git(["switch", "-C", branch], cwd=cwd)


def switch(branch: str, *, cwd: str) -> None:
    """``git switch <branch>`` — switch to an EXISTING branch (no ``-C``).

    The restore counterpart of :func:`switch_create`: the ``pr`` install flow
    switches onto its ``shipit/install`` scratch branch to stage the commit and
    must return the caller's checkout to the branch it started on afterwards
    (#777 mode 1 — leaving the operator on the staging branch with no notice is
    the surprise the issue reports). A plain ``switch`` (never ``-C``) so this
    only ever moves HEAD to a ref that already exists and never creates one.
    """
    _git(["switch", branch], cwd=cwd)


def add(paths: list[str], *, cwd: str) -> None:
    """``git add -f -- <paths>`` — stage ONLY these pathspecs, never ``-A``.

    ``-f`` because the managed paths are shipit-owned and must be tracked even if
    a consumer ``.gitignore`` happens to cover one (plain ``git add`` errors on an
    ignored path).
    """
    if not paths:
        return
    _git(["add", "-f", "--", *paths], cwd=cwd)


def add_all(*, cwd: str) -> None:
    """``git add -A`` — stage every change in the working tree.

    The whole-tree counterpart of :func:`add` (which stages named pathspecs):
    ``shipit repo new`` stages the entire freshly-generated Repo — consumer
    scaffold, managed baseline, and the resolved ``pixi.lock`` — for its single
    ``Initial commit``, so it needs the sweep rather than an enumerated list.
    """
    _git(["add", "-A"], cwd=cwd)


def commit_all(message: str, *, cwd: str, no_verify: bool = False) -> None:
    """``git commit -m <message>`` — commit everything already staged.

    The whole-tree counterpart of :func:`commit` (which scopes to pathspecs):
    ``shipit repo new`` stages the whole Repo with :func:`add_all` and commits
    it as one root ``Initial commit``. ``no_verify`` is left at its default
    ``False`` by creation so the installed hooks run on that commit exactly as
    they would for any consumer (ADR-0062).
    """
    args = ["commit"]
    if no_verify:
        args.append("--no-verify")
    _git([*args, "-m", message], cwd=cwd)


def clean_non_committed(*, cwd: str) -> None:
    """``git clean -ffdx`` — remove everything the tree does not track.

    Leaves exactly the committed content: every untracked AND ignored path
    (``-x``) is removed, recursing into untracked directories (``-d``) and
    forcing through nested working trees (``-ff``), so no build cache, resolved
    environment, or other regenerable artifact survives.

    ``shipit repo new`` uses this to make publication RELOCATABLE (ADR-0059).
    Staged certification (ADR-0062) builds the Rust workspace and materializes
    the pixi environment in the temporary sibling; those ignored artifacts embed
    the staging path as an ABSOLUTE location — Cargo bakes
    ``CARGO_BIN_EXE_<bin>`` into the compiled black-box test, and the conda-based
    ``.pixi`` environment hard-codes its own prefix — so an atomic rename that
    carried them would leave the published Repo running canonical commands
    against the vanished staging sibling. Stripping them after the ``Initial
    commit`` (they are gitignored, so the commit already excludes them) and
    before the rename publishes only the committed, location-independent tree;
    the destination regenerates its build and environment state fresh on first
    use from the committed lockfiles.

    Unlinking a fully materialized ``.pixi`` environment and Cargo build cache
    is bulk-filesystem work — tens of thousands of small files — that can run
    well past the tight local-plumbing bound on slower disks, so this carries
    the generous ``_STRIP_TIMEOUT`` rather than ``_LOCAL_TIMEOUT``; a spurious
    timeout here would fail repo creation mid-strip while it was still
    progressing normally.
    """
    _git(["clean", "-ffdx"], cwd=cwd, timeout=_STRIP_TIMEOUT)


def init_main(*, cwd: str) -> None:
    """``git init -b main`` — initialize a repository on the ``main`` branch.

    ``shipit repo new`` creates the local Repo on ``main`` (the portfolio's
    primary branch, ``docs/spec/repo-new.md``) before staging and committing it.
    ``-b main`` names the initial branch directly so no post-init rename is
    needed on an unborn HEAD.
    """
    _git(["init", "-b", "main"], cwd=cwd)


def _ident_name(var: str, *, cwd: str) -> str | None:
    """The display name from ``git var <var>`` (an IDENT string), or ``None``.

    ``git var GIT_AUTHOR_IDENT``/``GIT_COMMITTER_IDENT`` resolves the SAME
    identity a commit will use — honoring the matching ``GIT_*_NAME``/
    ``GIT_*_EMAIL`` env vars, the ``author.*``/``committer.*``/``user.*`` config
    chain, and git's own precedence — and fails exactly where ``git commit``
    would (e.g. ``user.useConfigOnly`` with nothing configured, or a missing
    email). A probe read: git's failure is a NORMAL answer (``None``), not an
    exception.
    """
    result = _probe(["var", var], cwd=cwd)
    if not result.ok:
        return None
    # An IDENT is `Name <email> <timestamp> <tz>`; the display name is
    # everything before the ` <email>` bracket (a name never contains ``<``).
    ident = result.stdout.strip()
    marker = ident.rfind(" <")
    if marker <= 0:
        return None
    return ident[:marker].strip() or None


def author_name(*, cwd: str) -> str | None:
    """Git's fully resolved author display name for ``cwd``, or ``None``.

    Resolves ``GIT_AUTHOR_IDENT`` (see :func:`_ident_name`) — the SAME author
    identity the commit will use. ``shipit repo new`` attributes the generated
    MIT ``LICENSE`` to the returned name; an unresolvable author is a creation
    preflight failure the caller raises, never a template placeholder.
    """
    return _ident_name("GIT_AUTHOR_IDENT", cwd=cwd)


def committer_name(*, cwd: str) -> str | None:
    """Git's fully resolved committer display name for ``cwd``, or ``None``.

    Resolves ``GIT_COMMITTER_IDENT`` (see :func:`_ident_name`) — the identity
    ``git commit`` records as the committer, which git resolves INDEPENDENTLY of
    the author (``GIT_COMMITTER_*`` env / ``committer.*`` config, else the
    ``user.*`` fallback). ``shipit repo new`` probes this alongside
    :func:`author_name` so a setup that resolves an author but no committer
    (e.g. only ``GIT_AUTHOR_NAME``/``GIT_AUTHOR_EMAIL`` set) is caught as a
    creation preflight failure rather than a raw ``Initial commit`` error.
    """
    return _ident_name("GIT_COMMITTER_IDENT", cwd=cwd)


def commit(
    message: str, paths: list[str], *, cwd: str, no_verify: bool = False
) -> None:
    """``git commit -m <message> -- <paths>`` — commit only the given pathspecs.

    ``no_verify`` bypasses the repo's commit hooks (``--no-verify``): install's
    reconcile commit uses it deliberately (ADR-0033) — the whole-tree gate is
    the REPO'S bar, not install's, and a consumer's pre-existing lint debt must
    never deadlock the very install that delivers the env to clear it.
    """
    args = ["commit"]
    if no_verify:
        args.append("--no-verify")
    _git([*args, "-m", message, "--", *paths], cwd=cwd)


def has_staged_changes(paths: list[str], *, cwd: str) -> bool:
    """Whether the index holds a staged diff for ``paths`` against HEAD.

    ``git diff --cached --quiet -- <paths>`` is a PROBE: exit 0 means the named
    pathspecs match HEAD (nothing staged), a nonzero exit means at least one
    differs. The MODE_PR staging flow asks this AFTER ``reset --soft`` +
    ``git add`` (#852 review): once the staging branch is reset onto
    ``origin/<default>`` the managed set can already match the base (a Tree
    duplicating an already-merged reconcile), and a pathspec :func:`commit` over
    an empty diff fails ``git commit`` with "nothing to commit" — so apply checks
    here and skips the commit rather than crashing. An empty ``paths`` is a
    vacuous "no staged changes" (never a bare unscoped diff)."""
    if not paths:
        return False
    return not _probe(["diff", "--cached", "--quiet", "--", *paths], cwd=cwd).ok


def reset_index(*, cwd: str) -> None:
    """``git reset`` — unstage everything, rewinding the index to HEAD.

    HEAD and the working tree are untouched; only the index moves back. The
    MODE_PR caller-restore uses it when the operator STARTED on the
    ``shipit/install`` scratch branch (#852 review): the flow's
    ``reset --soft origin/<default>`` staged the pre-reset..base diff into the
    index, and with no other branch to switch back to, unstaging is how the
    operator is returned to a clean index rather than a heavily-polluted one."""
    _git(["reset"], cwd=cwd)


def push(
    branch: str,
    *,
    cwd: str,
    remote: str = "origin",
    force: bool = False,
    no_verify: bool = False,
) -> None:
    """``git push <remote> <branch>``.

    ``force`` plain-force-pushes the shipit-owned install branch, which install
    regenerates from HEAD every run — so re-running with a prior install PR still
    open updates that PR rather than failing non-fast-forward. (Plain ``--force``,
    not ``--force-with-lease``: a freshly recreated branch has no remote-tracking
    ref to lease against, and the branch is shipit-exclusive, so there is nothing
    to protect.) The break-glass push to a real branch (main) never forces.

    ``no_verify`` bypasses the repo's pre-push hook (``--no-verify``): install's
    own pushes use it deliberately (#477, ADR-0033) — the pre-push hook runs the
    WHOLE-TREE lint gate, which install itself just armed during staging, so on
    a virgin consumer carrying pre-existing lint debt the un-bypassed push dies
    on debt the install PR exists to make clearable (the tripwire armed by the
    very run that trips it). Like the commit-side bypass, this is install's
    opt-in only, never the adapter's default.
    """
    args = ["push"]
    if force:
        args.append("--force")
    if no_verify:
        args.append("--no-verify")
    args += [remote, branch]
    _git(args, cwd=cwd, timeout=_NETWORK_TIMEOUT)


def pull_rebase(branch: str, *, cwd: str, remote: str = "origin") -> None:
    """``git pull --rebase <remote> <branch>`` in a data-store checkout."""
    _git(["pull", "--rebase", remote, branch], cwd=cwd, timeout=_NETWORK_TIMEOUT)


#: The stderr signatures of a REFERENCE-POISONED clone (#353, diagnosis
#: narrowed in #372). On git 2.54 a reference repo carrying ANY commit-graph —
#: a plain ``objects/info/commit-graph`` file or a split chain under
#: ``objects/info/commit-graphs/`` (a MIDX alone is incidental) — makes
#: ``clone --reference --dissociate`` fail DETERMINISTICALLY at the clone-time
#: checkout: git prints ``fatal: unable to parse commit <sha>`` and ``Clone
#: succeeded, but checkout failed.`` and exits 128. No object is lost — the
#: failure is STALE IN-PROCESS STATE inside the single clone invocation: the
#: clone process reads the reference's commit-graph through the alternates
#: link, ``--dissociate`` repacks and severs the alternate, and the clone-time
#: checkout then dies on the stale graph. After the fact the clone is
#: self-consistent (the object is present; fsck is clean; a fresh-process
#: checkout succeeds). Matching requires BOTH message fragments (lowercased,
#: across both streams) rather than the bare rc: 128 is git's generic fatal
#: exit, and either fragment alone has innocent causes (real object
#: corruption; a checkout killed by disk space or an unrepresentable filename)
#: that must propagate, not trigger a full re-clone.
_POISONED_REFERENCE_MARKERS: tuple[str, ...] = (
    "clone succeeded, but checkout failed",
    "unable to parse commit",
)


def _is_poisoned_reference_failure(err: ExecError) -> bool:
    """Whether ``err`` is the #353 clone-succeeded-checkout-failed signature.

    Only a real child EXIT qualifies — a timeout or launch failure is never the
    poisoned-reference shape, and retrying a full clone after a 10-minute
    timeout would double the hang instead of degrading gracefully.
    """
    if err.cause != execrun.CAUSE_EXIT:
        return False
    text = f"{err.stderr}\n{err.stdout}".lower()
    return all(marker in text for marker in _POISONED_REFERENCE_MARKERS)


def _resolve_reference_donor(reference: str) -> str:
    """Resolve a ``--reference`` donor path, dereferencing a linked worktree (#509).

    ``git clone --reference`` refuses a git LINKED worktree as its source (git
    2.54: ``fatal: reference repository '<path>' as a linked checkout is not
    supported yet``). The review funnel hands :func:`clone_dissociated` the PR's
    source workdir as the donor, and when an implementer ran under
    ``Agent(isolation: worktree)`` that workdir IS a linked worktree — so the
    read-only review clone died at launch and the local (codex/agy) review was
    silently lost for every worktree-sourced PR.

    A linked worktree SHARES its object store with the repo's COMMON gitdir, and
    a normal gitdir is a valid ``--reference`` source — so dereferencing the
    worktree to that common gitdir preserves the near-instant borrow (ADR-0014)
    without ever falling back to a slower full clone. The normal (non-worktree)
    donor path is deliberately left untouched.

    Probe the reference for its two git dirs (``_probe``, so a not-a-repo path is
    a normal nonzero answer, not a raise):

    - ``rev-parse --absolute-git-dir`` — the per-worktree gitdir (for a linked
      worktree, ``.../.git/worktrees/<name>``);
    - ``rev-parse --git-common-dir`` — the SHARED common dir (for a linked
      worktree, ``.../.git``). git returns it RELATIVE to the queried checkout,
      NOT the process cwd, so it is resolved to an absolute path against
      ``reference`` (an already-absolute answer is kept as-is by ``os.path.join``).

    Returns ``reference`` UNCHANGED when the probe fails (not a git repo) or the
    two resolve equal — a NORMAL checkout, whose per-worktree gitdir IS the
    common dir — so the common path is never perturbed. When they DIFFER (a
    linked worktree) the resolved absolute common dir is returned and the deref
    is narrated at INFO with both the original and resolved paths.
    """
    absolute = _probe(["rev-parse", "--absolute-git-dir"], cwd=reference)
    common = _probe(["rev-parse", "--git-common-dir"], cwd=reference)
    if not absolute.ok or not common.ok:
        return reference
    absolute_gitdir = absolute.stdout.strip()
    common_out = common.stdout.strip()
    if not absolute_gitdir or not common_out:
        return reference
    # --git-common-dir is relative to the queried checkout (an absolute answer is
    # kept verbatim by os.path.join); realpath both ends so the equality is
    # symlink-robust (e.g. macOS /tmp -> /private/tmp).
    common_gitdir = os.path.realpath(os.path.join(reference, common_out))
    if common_gitdir == os.path.realpath(absolute_gitdir):
        return reference  # normal checkout: the per-worktree gitdir IS the common dir.
    logger.info(
        "reference %s is a linked worktree; dereferencing to its shared common "
        "gitdir %s for the --reference borrow (#509)",
        reference,
        common_gitdir,
    )
    return common_gitdir


def clone_dissociated(url: str, dest: str, *, reference: str) -> None:
    """Clone ``url`` into ``dest`` as an INDEPENDENT, dissociated checkout.

    ``--reference <reference>`` borrows the local checkout's object store so the
    clone is near-instant and tiny over the wire; ``--dissociate`` then copies
    every borrowed object into the new clone and drops the alternates link, so the
    result shares NOTHING with the reference (no ``.git/objects/info/alternates``)
    and is safe to ``rm -rf`` (ADR-0014). ``origin`` is set to ``url`` — the GitHub
    URL — so ``gh``/``git`` work inside the Tree unchanged.

    A LINKED-worktree ``reference`` is first dereferenced to its shared common
    gitdir via :func:`_resolve_reference_donor` (#509): git refuses a linked
    worktree as a ``--reference`` source, so the donor actually borrowed from is
    the resolved common gitdir — a valid source that shares the same object
    store. A normal checkout reference passes through untouched.

    ``-c core.commitGraph=false`` disables commit-graph READING for the clone
    process only (#372): on git 2.54 a reference carrying ANY commit-graph
    kills the stock command at clone-time checkout — the clone process reads
    the donor's graph through the alternates link, ``--dissociate`` severs the
    alternate, and the checkout dies on the stale in-process graph state
    (``fatal: unable to parse commit <sha>``). With graph reading off the
    borrow works against any donor. The ``-c`` sits BEFORE the subcommand, so
    it scopes to this one process and persists nothing in the new clone's
    config.

    FAIL-OPEN on a poisoned reference (#353): when the referenced clone still
    dies with the clone-succeeded-checkout-failed signature (see
    :func:`_is_poisoned_reference_failure` — with the #372 fix this should be
    unreachable for the commit-graph trigger, but it guards donor pathologies
    not yet met), the half-checked-out ``dest`` is removed and the clone is
    retried ONCE without ``--reference`` (and therefore without
    ``--dissociate`` — a full clone is already independent). The retry trades
    the near-instant borrow for a full transfer, so the degradation is
    narrated at WARNING with the donor path; any other failure — and a
    failure of the retry itself — propagates untouched. This one seam keeps
    BOTH consumers (write-Tree ``tree.create`` and read-only ``tree.readonly``)
    working without having to suppress every commit-graph writer in every
    possible donor.
    """
    donor = _resolve_reference_donor(reference)
    try:
        _git(
            [
                "-c",
                "core.commitGraph=false",
                "clone",
                "--reference",
                donor,
                "--dissociate",
                url,
                dest,
            ],
            timeout=_CLONE_TIMEOUT,
        )
    except ExecError as err:
        if not _is_poisoned_reference_failure(err):
            raise
        logger.warning(
            "reference clone of %s failed at clone-time checkout (donor %s "
            "is poisoned — commit-graph chain, #353); retrying once as "
            "a full clone without --reference",
            url,
            donor,
            exc_info=True,
        )
        # git leaves the cloned-but-not-checked-out dest behind on this failure;
        # a retry into a non-empty dir would fail on the leftovers, not the bug.
        shutil.rmtree(dest, ignore_errors=True)
        _git(["clone", url, dest], timeout=_CLONE_TIMEOUT)


#: The four local-config keys that make a checkout a SAFE ``--reference`` donor
#: (#353): the two commit-graph writers off, plus auto-gc/auto-maintenance off —
#: proven necessary in the live diagnosis, where a routine ``git gc --auto``
#: (fired after fetches) regenerated ``objects/info/commit-graphs/`` even with
#: both write flags false. Disabling auto-gc in a Tree is acceptable: Trees are
#: short-lived leaves, so unbounded loose objects never accumulate enough to
#: matter before the Tree is removed.
SAFE_DONOR_CONFIG: tuple[tuple[str, str], ...] = (
    ("fetch.writeCommitGraph", "false"),
    ("gc.writeCommitGraph", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
)


def configure_safe_reference_donor(*, cwd: str) -> None:
    """Write the :data:`SAFE_DONOR_CONFIG` keys into ``cwd``'s local git config.

    Tree provisioning calls this on every Tree it mints — BEFORE the Tree's
    first ``git fetch`` — so a session Tree never grows the split commit-graph
    chain that poisons it as a ``--reference`` donor for its children's clones
    (#353). Belt and suspenders with the :func:`clone_dissociated` retry: the
    retry keeps clones working against ANY poisoned donor (e.g. a user checkout
    that predates this config), while this keeps shipit-minted Trees fast donors
    that never need the degraded full-clone path in the first place.
    """
    for key, value in SAFE_DONOR_CONFIG:
        _git(["config", "--local", key, value], cwd=cwd)


def fetch(*, cwd: str, remote: str = "origin") -> None:
    """``git fetch <remote>`` inside the Tree, so its base ref is up to date."""
    _git(["fetch", remote], cwd=cwd, timeout=_NETWORK_TIMEOUT)


def clone(url: str, dest: str, *, depth: int | None = 1) -> None:
    """``git clone [--depth N] <url> <dest>`` — a plain (non-dissociated) clone.

    The publish stage's tap-push clone (TOL02-WS05): a small side repo cloned
    fresh, mutated, pushed, and discarded — none of the reference-donor
    machinery of :func:`clone_dissociated` applies. ``depth=1`` by default
    (the clone exists to carry one commit forward); pass ``None`` for full
    history. ``url`` may carry a token userinfo — the caller registers that
    token with the central redactor first, so the recorded argv is masked.
    """
    args = ["clone"]
    if depth is not None:
        args += ["--depth", str(depth)]
    _git([*args, url, dest], timeout=_CLONE_TIMEOUT)


def configure_identity(name: str, email: str, *, cwd: str) -> None:
    """Set ``user.name``/``user.email`` in ``cwd``'s LOCAL git config.

    A fresh throwaway clone (the publish stage's tap push) commits on a
    runner that may carry no global identity; stating one locally keeps the
    commit from dying on ``Author identity unknown`` without touching any
    global state.
    """
    _git(["config", "--local", "user.name", name], cwd=cwd)
    _git(["config", "--local", "user.email", email], cwd=cwd)


def checkout_new_branch(branch: str, base: str, *, cwd: str) -> None:
    """``git checkout -b <branch> <base>`` — cut ``branch`` from ``base`` and switch."""
    _git(["checkout", "-b", branch, base], cwd=cwd)


def checkout(branch: str, *, cwd: str) -> None:
    """``git checkout <branch>`` — switch to an EXISTING branch (no ``-b``).

    The read-only-Tree counterpart of :func:`checkout_new_branch`: a reviewer
    Tree checks out a branch that already exists on ``origin`` (the PR head) rather
    than cutting a new one. After a ``git fetch`` the plain checkout DWIMs a local
    tracking branch from ``origin/<branch>``, so the read-only clone lands on the
    exact head under review.
    """
    _git(["checkout", branch], cwd=cwd)


def reset_hard(ref: str, *, cwd: str) -> None:
    """``git reset --hard <ref>`` — force HEAD, index, and working tree to ``ref``.

    The read-only-Tree reuse counterpart of :func:`checkout`: when a shared review
    clone is reused after the PR head advanced, a ``git fetch`` followed by a hard reset
    to ``origin/<branch>`` re-pins the working tree to the CURRENT head, so a second
    reviewer never reads the stale commit the first clone happened to land on.

    Also the ``-release-rc`` live-fire cut's branch restore (legacy release#663
    contract, :mod:`shipit.verbs.release`): the bump commit travels on the TAG
    ONLY, so after tagging, prepare resets the branch back to the pre-bump
    commit — the commit stays reachable from the tag, the branch's version
    line stays clean.
    """
    _git(["reset", "--hard", ref], cwd=cwd)


def reset_soft(ref: str, *, cwd: str) -> None:
    """``git reset --soft <ref>`` — move HEAD to ``ref``, keep index and working tree.

    The MODE_PR staging-branch rebase (#852, :mod:`shipit.install.apply`):
    install (re)creates the ``shipit/install`` branch and resets it onto
    ``origin/<default>`` so the managed commit lands as ONE clean refresh on top
    of the current default branch, no matter what HEAD the Tree was cut from.
    ``--soft`` moves only the branch pointer — the rendered managed files stay in
    the working tree — so the following pathspec commit (which takes the listed
    paths from the working tree and everything else from HEAD) produces
    ``origin/<default>`` + the managed delta, never a stack on stale commits.
    """
    _git(["reset", "--soft", ref], cwd=cwd)


def submodule_update_init(*, cwd: str) -> None:
    """``git submodule sync --recursive`` + ``update --init --recursive`` — populate submodules.

    A dissociated clone (:func:`clone_dissociated`) leaves every registered submodule
    as an EMPTY gitlink directory: ``git clone`` does not recurse submodules and neither
    the fetch nor the checkout populates them (#485). A consumer whose suite reads
    submodule-backed fixtures (e.g. lex's ``comms/specs`` spec root) then fails in a Tree
    even though it is green in a normal checkout. This recursive init makes a Tree match
    what CI does (``actions/checkout`` with ``submodules: recursive``).

    The ``sync --recursive`` FIRST is load-bearing on the reuse-refresh path (#486): when
    a shared reviewer clone is re-pinned to an advanced PR head, that head may have moved
    a submodule's URL in ``.gitmodules``. ``update --init`` only reads ``.git/config`` for
    an already-initialized submodule and would keep fetching the STALE URL (and fail);
    ``sync`` first copies the checked-out ``.gitmodules`` URLs into ``.git/config`` so the
    update fetches the right remote. On a fresh clone (or a URL that did not move) it is a
    harmless no-op.

    A repo with NO submodules is a clean no-op (exit 0) for both commands, so this is
    unconditional across Tree provisioning — no manifest gate is needed. It FAILS LOUD
    (``check=True`` → :class:`ExecError`): a submodule that cannot be fetched (auth/network)
    aborts Tree materialization and rolls the half-built leaf back, rather than leaving a
    silently empty submodule dir the suite would fail on much later. Submodule work hits
    the network, so both carry the remote-facing timeout, not the local-plumbing bound.
    """
    _git(
        ["submodule", "sync", "--recursive"],
        cwd=cwd,
        timeout=_NETWORK_TIMEOUT,
    )
    _git(
        ["submodule", "update", "--init", "--recursive"],
        cwd=cwd,
        timeout=_NETWORK_TIMEOUT,
    )
