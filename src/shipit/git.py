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
import shutil
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
#: the new checkout (ADR-0014), so it alone gets a larger ceiling.
_NETWORK_TIMEOUT: float = execrun.DEFAULT_TIMEOUT
_LOCAL_TIMEOUT: float = 60.0
_CLONE_TIMEOUT: float = 600.0


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
    scope without an exclude list (docs/prd/lint-checks.md — "whole tree via git ls-files").
    """
    out = _git(["ls-files"], cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


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


# --------------------------------------------------------------------------
# mutations — thin typed functions (install / Tree creation / review reuse)
# --------------------------------------------------------------------------


def switch_create(branch: str, *, cwd: str) -> None:
    """Create-or-reset ``branch`` from the current HEAD and switch to it.

    ``-C`` (force) so a re-run that reuses the install branch name starts clean
    rather than failing on an existing branch.
    """
    _git(["switch", "-C", branch], cwd=cwd)


def add(paths: list[str], *, cwd: str) -> None:
    """``git add -f -- <paths>`` — stage ONLY these pathspecs, never ``-A``.

    ``-f`` because the managed paths are shipit-owned and must be tracked even if
    a consumer ``.gitignore`` happens to cover one (plain ``git add`` errors on an
    ignored path).
    """
    if not paths:
        return
    _git(["add", "-f", "--", *paths], cwd=cwd)


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


def clone_dissociated(url: str, dest: str, *, reference: str) -> None:
    """Clone ``url`` into ``dest`` as an INDEPENDENT, dissociated checkout.

    ``--reference <reference>`` borrows the local checkout's object store so the
    clone is near-instant and tiny over the wire; ``--dissociate`` then copies
    every borrowed object into the new clone and drops the alternates link, so the
    result shares NOTHING with the reference (no ``.git/objects/info/alternates``)
    and is safe to ``rm -rf`` (ADR-0014). ``origin`` is set to ``url`` — the GitHub
    URL — so ``gh``/``git`` work inside the Tree unchanged.

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
    narrated at WARNING with the reference path; any other failure — and a
    failure of the retry itself — propagates untouched. This one seam keeps
    BOTH consumers (write-Tree ``tree.create`` and read-only ``tree.readonly``)
    working without having to suppress every commit-graph writer in every
    possible donor.
    """
    try:
        _git(
            [
                "-c",
                "core.commitGraph=false",
                "clone",
                "--reference",
                reference,
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
            "reference clone of %s failed at clone-time checkout (reference %s "
            "is a poisoned donor — commit-graph chain, #353); retrying once as "
            "a full clone without --reference",
            url,
            reference,
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
    """
    _git(["reset", "--hard", ref], cwd=cwd)
