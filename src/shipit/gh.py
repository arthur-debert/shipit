"""The ONE gh Tool adapter (and the git boundary) for shipit.

Every call that shells out to ``gh`` or ``git`` lives here, so the rest of the
package is pure and unit-testable by patching this one module. PROC02-WS01
(ADR-0028, glassbox PRD) merged the PR-state engine's former second boundary
(``shipit/prstate/ghapi.py``) into this adapter: the REST and GraphQL helpers,
the pagination-merging helper (defined exactly once, here), the PR-flow acts
(``pr_ready`` / ``pr_edit_reviewer`` / …), and the per-tool timeout defaults
all live in this module — building ``gh`` argv anywhere else is a review
defect (ADR-0028).

Execution routes through the one Exec runner (ADR-0028): every call here is an
Exec via :func:`shipit.execrun.run` with a stated timeout, one structured
record per Exec, and central redaction (:mod:`shipit.redact`) applied to
everything the runner logs or attaches to an error. A failed invocation raises
the single transport error :class:`shipit.execrun.ExecError` — this boundary
defines no error class of its own (the legacy ``GhError`` is deleted, no
alias). The one SEMANTIC error raised here is the engine's user-renderable
:class:`shipit.prstate.errors.PrStateError`, for the failure the Exec record
cannot carry — a ``gh`` call that exited 0 but returned an unusable answer
(a GraphQL response body carrying ``errors``).
"""

from __future__ import annotations

import json
import logging

from . import execrun
from .execrun import ExecError

# The engine's semantic error is safe to import here: `prstate.errors` is a
# leaf module (stdlib-only, imports nothing from shipit), so no cycle — while
# `identity` composes over THIS module and stays a deferred import below.
from .prstate.errors import PrStateError

#: The adapter's own logger (ADR-0029). The TRANSPORT record for every call is
#: the Exec runner's (one record per Exec on ``shipit.exec``); this boundary
#: records only what the runner cannot see — the GraphQL semantic failure, and
#: the draft-flip milestone it performs on the engine's behalf.
logger = logging.getLogger("shipit.gh")

#: Stated per-Exec timeouts (ADR-0028: every Exec carries one; nothing hangs by
#: default). Calls that talk to GitHub (``gh``, and ``git`` against origin) get
#: the runner's generous default; local git plumbing is near-instant and gets a
#: tight bound; the dissociated clone copies the full object store into the new
#: checkout (ADR-0014), so it alone gets a larger ceiling.
_NETWORK_TIMEOUT: float = execrun.DEFAULT_TIMEOUT
_LOCAL_GIT_TIMEOUT: float = 60.0
_CLONE_TIMEOUT: float = 600.0


class UnknownPr:
    """Sentinel: a head's PR state could NOT be read — distinct from ``None`` (no PR).

    :func:`pr_for_head` returns this singleton when ``gh`` failed for a reason OTHER
    than "the branch has no PR" (auth/network/rate-limit), or returned empty/malformed
    output — i.e. whenever the PR state is genuinely *undetermined*, as opposed to
    provably absent (``None``). Keeping the two apart is the whole point of TRE02-WS03:
    a Tree whose PR state is UNKNOWN must never be reclaimed by ``gc`` AND its presence
    must be surfaced (the incomplete-sweep warning) rather than silently collapsed to
    "no PR". A singleton so callers test it with ``pr is gh.UNKNOWN``.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNKNOWN"


#: The singleton unreadable-PR-state sentinel (see :class:`UnknownPr`).
UNKNOWN = UnknownPr()


def _token_env(token: str | None) -> dict[str, str] | None:
    """The env override that makes ``gh`` authenticate as ``token``.

    ``None`` leaves the user's normal ``gh`` auth in place. Otherwise sets
    ``GH_TOKEN=<token>``; :func:`_run` also *removes* any ``GITHUB_TOKEN`` from
    the child env (rather than blanking it — an empty-but-set var still reads as
    "set" to many tools, and its precedence vs ``GH_TOKEN`` is gh-version
    dependent) so the call authenticates as EXACTLY the token we pass — the seam
    for posting a review AS a GitHub App installation. An installation token
    (``ghs_…``) is a normal bearer token to ``gh``.
    """
    if token is None:
        return None
    return {"GH_TOKEN": token}


def _run(
    args: list[str],
    *,
    input_text: str | None = None,
    cwd: str | None = None,
    token: str | None = None,
    timeout: float | None = _NETWORK_TIMEOUT,
) -> str:
    """Run a command through the Exec runner, returning stdout.

    Raises :class:`ExecError` on failure — the runner normalizes a nonzero
    exit, a timeout expiry, and a missing binary into that one transport error,
    records each Exec exactly once, and redacts everything it logs or raises
    (:mod:`shipit.redact`), so nothing secret rides a record or an exception
    to a sink. The token / child env / stdin body are never logged at all.

    ``token``, when given, runs the subprocess with ``GH_TOKEN=<token>`` (and
    ``GITHUB_TOKEN`` removed) so a ``gh`` call authenticates as that token
    rather than the user's login (see :func:`_token_env`). ``replace_env`` is
    what makes the *removal* possible — an env merge can only add or override.
    """
    env = None
    replace_env = False
    if token is not None:
        import os

        # Drop GITHUB_TOKEN entirely (not blank it) so only GH_TOKEN remains.
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        env.update(_token_env(token))
        replace_env = True
    result = execrun.run(
        args,
        input=input_text,
        cwd=cwd,
        env=env,
        replace_env=replace_env,
        timeout=timeout,
    )
    return result.stdout


def _run_probe(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float | None = _NETWORK_TIMEOUT,
) -> execrun.ExecResult:
    """Run a command whose nonzero exit is a NORMAL answer, not a failure.

    ``check=False`` through the runner (ADR-0028): the Exec still gets its one
    record, but at DEBUG — a no-PR lookup, an absent-ref check, or a
    not-a-checkout read happens on every routine scan/hook and must not spray
    ERROR records over normal flows. The caller branches on the result's
    ``rc``/``stderr`` instead of catching :class:`ExecError` (which the runner
    still raises for launch-level failures: missing binary, timeout).
    """
    return execrun.run(args, cwd=cwd, check=False, timeout=timeout)


def _git_probe(
    args: list[str], *, cwd: str, timeout: float | None = _LOCAL_GIT_TIMEOUT
) -> execrun.ExecResult:
    """``git -C <cwd> <args>`` as a probe (see :func:`_run_probe`)."""
    return _run_probe(["git", "-C", cwd, *args], timeout=timeout)


# --------------------------------------------------------------------------
# gh api
# --------------------------------------------------------------------------


def rest(
    path: str,
    *,
    method: str | None = None,
    body: object | None = None,
    fields: dict[str, str] | None = None,
    paginate: bool = False,
    token: str | None = None,
) -> object:
    """Call ``gh api <path>`` and return the parsed JSON.

    ``method`` sets ``--method`` (GET when omitted). ``body``, when given, is
    JSON-encoded and piped to ``gh api --input -`` (the way to send a structured
    request body); ``fields`` sends string parameters as ``-f key=value`` (the
    lighter form for a flat body — the two are alternatives, not companions).
    ``paginate`` adds ``--paginate``; the per-page JSON arrays are
    concatenated into one list. ``token``, when given, authenticates the call as
    that token (a GitHub App installation token) instead of the user's ``gh``
    login — the seam for posting a review AS ``<app-slug>[bot]``.
    """
    args = ["gh", "api", path]
    if method:
        args += ["--method", method]
    if paginate:
        args.append("--paginate")
    for key, value in (fields or {}).items():
        args += ["-f", f"{key}={value}"]
    input_text = None
    if body is not None:
        args += ["--input", "-"]
        input_text = json.dumps(body)
    out = _run(args, input_text=input_text, token=token)
    if not out.strip():
        return None
    if paginate:
        return _merge_paginated(out)
    return json.loads(out)


def _merge_paginated(output: str) -> list:
    """Concatenate the JSON arrays ``gh api --paginate`` emits back-to-back."""
    merged: list = []
    decoder = json.JSONDecoder()
    idx = 0
    text = output.strip()
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        value, end = decoder.raw_decode(text, idx)
        if isinstance(value, list):
            merged.extend(value)
        else:
            merged.append(value)
        idx = end
    return merged


def graphql(query: str, **variables: object) -> dict:
    """Run one of the PR-state ENGINE's own GraphQL queries/mutations; return `data`.

    PURPOSE-BUILT for the engine's cursor/pagination + review-thread queries —
    NOT a general-purpose GraphQL boundary. Two deliberate behaviours make it
    correct for every call the engine makes but surprising to a general caller:

      * a variable whose value is ``None`` is OMITTED entirely (not sent as
        null) — so a first-page ``after: $cursor`` defaults to GraphQL null;
        you cannot pass an *explicit* null through this helper; and
      * a str value is forced as a string via ``-f`` (only int/bool type-infer
        via ``-F``) — required for ``ID!`` variables, which must not be coerced
        to a number.

    A future caller needing explicit-null variables, or float/enum/list
    variables, must not reach for this — build that call against ``gh api
    graphql`` directly. Raises :class:`PrStateError` if the response carries
    errors (a semantic failure: the Exec succeeded, the answer is unusable).
    """
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        # Omit None entirely: an unprovided nullable GraphQL variable defaults
        # to null, which is what a first-page `after: $cursor` wants. Passing
        # it through would send the literal string "None".
        if value is None:
            continue
        # -F type-infers ints/bools; -f forces a string (needed for ID! vars).
        flag = "-F" if isinstance(value, (int, bool)) else "-f"
        args += [flag, f"{key}={value}"]
    payload = json.loads(_run(args))
    if payload.get("errors"):
        # A propagating semantic failure the Exec record cannot carry (the gh
        # call exited 0): record it at ERROR with the exception attached
        # (glassbox spray) before it leaves this boundary. Raise-then-log so the
        # record carries a real traceback — `exc_info=<unraised instance>` would
        # attach only the type+value (its `__traceback__` is still None).
        try:
            raise PrStateError(f"graphql errors: {payload['errors']}")
        except PrStateError:
            logger.error(
                "gh graphql call returned errors (Exec succeeded, answer unusable)",
                exc_info=True,
            )
            raise
    return payload["data"]


# --------------------------------------------------------------------------
# repo identity
# --------------------------------------------------------------------------


def current_repo(*, cwd: str | None = None) -> str:
    """``owner/name`` of the repo in ``cwd`` — the current directory if omitted (via ``gh``)."""
    out = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=cwd,
    )
    return out.strip()


def repo_canonical(slug: str) -> str:
    """Resolve a (possibly aliased/renamed) ``OWNER/NAME`` slug to its canonical
    ``owner/name``.

    GitHub keeps a 307 redirect from an old/aliased slug to the repo's current
    canonical slug. ``gh api`` follows it for GET but NOT for POST, so a write to
    an aliased slug hard-fails with ``HTTP 307``. Normalizing the slug up front,
    where it enters, keeps every write path on the canonical owner/name.
    """
    out = _run(
        ["gh", "repo", "view", slug, "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    )
    return out.strip()


def repo_slug() -> tuple[str, str]:
    """Return (owner, name) for the current repo."""
    data = json.loads(_run(["gh", "repo", "view", "--json", "owner,name"]))
    return data["owner"]["login"], data["name"]


def pr_view(pr: str, *, repo: str | None = None, json_fields: list[str]) -> str:
    """``gh pr view <pr> [--repo …] --json <fields>`` → stripped stdout (the JSON).

    Raises :class:`ExecError` if gh fails (e.g. the PR can't be resolved); the
    caller parses the returned JSON object.
    """
    args = ["gh", "pr", "view", pr]
    if repo is not None:
        args += ["--repo", repo]
    args += ["--json", ",".join(json_fields)]
    return _run(args).strip()


def pr_meta(pr: int) -> dict:
    """PR-level metadata the PR-state engine needs in one call.

    Deliberately does NOT fetch ``reviewRequests``: ``gh pr view --json``
    silently omits Bot-typed requested reviewers (a requested Copilot reads as
    ``[]``), so the engine sources requested reviewers from GraphQL instead
    (`prstate.fetch._threads_and_review_requests`).
    """
    return json.loads(
        pr_view(
            str(pr),
            json_fields=[
                "number",
                "headRefOid",
                "baseRefName",
                "isDraft",
                "mergeable",
                "mergeStateStatus",
                "statusCheckRollup",
            ],
        )
    )


def repo_root(*, cwd: str | None = None) -> str | None:
    """The git working-tree root for ``cwd`` (the current directory if omitted).

    ``None`` when ``cwd`` is not inside a checkout. This is THE single
    ``git rev-parse --show-toplevel`` boundary — the ``cwd`` parameter (ADR-0024)
    is what lets every caller route through it instead of re-implementing the
    command (``identity.resolve_working_dir``, the eval hook / report, review
    diff), so the toplevel is derived one way, in one place.
    """
    args = ["git"]
    if cwd is not None:
        args += ["-C", cwd]
    args += ["rev-parse", "--show-toplevel"]
    try:
        result = _run_probe(args, timeout=_LOCAL_GIT_TIMEOUT)
    except ExecError:
        return None
    if not result.ok:
        return None
    return result.stdout.strip() or None


def git_head_commit(*, cwd: str) -> str | None:
    """The current ``HEAD`` commit SHA for the checkout at ``cwd``, or ``None``.

    ``None`` on any git failure (detached/unborn HEAD, not a checkout) — the
    revision half of a :class:`shipit.identity.WorkingDir`, and the eval record's
    ``git.commit`` stamp, are both best-effort: an unresolvable commit degrades to
    ``None`` rather than raising.
    """
    try:
        result = _git_probe(["rev-parse", "HEAD"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    return result.stdout.strip() or None


def owner_kind(login: str) -> str:
    """The account type of ``login`` — ``"User"`` or ``"Organization"`` (via ``gh api``).

    The ONE identity call that needs the network: :func:`shipit.identity.Repo`
    identity derives locally from the origin remote, but an **Owner**'s *kind* is a
    lazily-resolved enrichment (:func:`shipit.identity.resolve_owner_kind`) read
    from ``gh api users/<login>``, whose ``type`` field is ``User`` for a user
    account and ``Organization`` for an org (the endpoint serves both).

    Raises :class:`ValueError` when the call succeeded but the response is not
    a usable answer (missing/mistyped ``type``) — a data-shape problem, distinct
    from the transport :class:`ExecError` a failed ``gh`` call raises.
    """
    info = rest(f"users/{login}")
    if not isinstance(info, dict) or "type" not in info:
        raise ValueError(f"could not resolve owner kind for {login!r}")
    return str(info["type"])


def default_branch(repo: str) -> str:
    """The repo's default branch name.

    Raises :class:`ValueError` on a response with no usable ``default_branch``
    (a data-shape problem — the transport failure is :class:`ExecError`).
    """
    info = rest(f"repos/{repo}")
    if not isinstance(info, dict) or "default_branch" not in info:
        raise ValueError(f"could not resolve default branch for {repo}")
    return str(info["default_branch"])


# --------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------


def label_create(repo: str, name: str, *, description: str, color: str) -> None:
    """Create-or-update a label (``gh label create --force`` is idempotent)."""
    _run(
        [
            "gh",
            "label",
            "create",
            name,
            "--repo",
            repo,
            "--description",
            description,
            "--color",
            color,
            "--force",
        ]
    )


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------


def secret_set(name: str, value: str, *, repo: str) -> None:
    """Set an Actions secret, passing the value on stdin (never in argv)."""
    _run(["gh", "secret", "set", name, "--repo", repo], input_text=value)


def secret_list(repo: str) -> list[str]:
    """The names of the repo's Actions secrets."""
    out = _run(
        ["gh", "secret", "list", "--repo", repo, "--json", "name", "-q", ".[].name"]
    )
    return [line for line in out.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# git + PR — the boundary ``install`` needs (pull, never push)
# --------------------------------------------------------------------------


def _git(
    args: list[str], *, cwd: str, timeout: float | None = _LOCAL_GIT_TIMEOUT
) -> str:
    """``git -C <cwd> <args>`` via :func:`_run`.

    Local plumbing by default (:data:`_LOCAL_GIT_TIMEOUT`); the git calls that
    talk to origin (fetch/push) state :data:`_NETWORK_TIMEOUT` instead.
    """
    return _run(["git", "-C", cwd, *args], timeout=timeout)


def git_status_porcelain(*, cwd: str) -> str:
    """Machine-readable working-tree status (``git status --porcelain``).

    Empty output means a clean tree; each non-empty line is one changed/untracked
    path. The eval exit-hygiene check reads this to flag a coordinator run that
    left a dirty worktree (uncommitted edits, conflict markers, stray files).
    """
    return _git(["status", "--porcelain"], cwd=cwd)


def git_current_branch(*, cwd: str) -> str | None:
    """The current branch name, or ``None`` on a detached/unborn HEAD."""
    try:
        result = _git_probe(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    name = result.stdout.strip()
    return None if (not name or name == "HEAD") else name


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
    fallback. Never raises: any git failure (the ref is absent) reads as "not an epic".
    """
    for ref in (
        f"refs/remotes/origin/{epic}/umbrella",
        f"refs/heads/{epic}/umbrella",
    ):
        try:
            if _git_probe(["show-ref", "--verify", "--quiet", ref], cwd=cwd).ok:
                return True
        except ExecError:
            continue
    return False


def git_ls_files(*, cwd: str) -> list[str]:
    """Tracked files (``git ls-files``), repo-root-relative, in git's order.

    Tracked-only is deliberate: it keeps generated/ignored paths out of the lint
    scope without an exclude list (docs/prd/lint-checks.md — "whole tree via git ls-files").
    """
    out = _git(["ls-files"], cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


def git_switch_create(branch: str, *, cwd: str) -> None:
    """Create-or-reset ``branch`` from the current HEAD and switch to it.

    ``-C`` (force) so a re-run that reuses the install branch name starts clean
    rather than failing on an existing branch.
    """
    _git(["switch", "-C", branch], cwd=cwd)


def git_add(paths: list[str], *, cwd: str) -> None:
    """``git add -f -- <paths>`` — stage ONLY these pathspecs, never ``-A``.

    ``-f`` because the managed paths are shipit-owned and must be tracked even if
    a consumer ``.gitignore`` happens to cover one (plain ``git add`` errors on an
    ignored path).
    """
    if not paths:
        return
    _git(["add", "-f", "--", *paths], cwd=cwd)


def git_commit(message: str, paths: list[str], *, cwd: str) -> None:
    """``git commit -m <message> -- <paths>`` — commit only the given pathspecs."""
    _git(["commit", "-m", message, "--", *paths], cwd=cwd)


def git_push(
    branch: str, *, cwd: str, remote: str = "origin", force: bool = False
) -> None:
    """``git push <remote> <branch>``.

    ``force`` plain-force-pushes the shipit-owned install branch, which install
    regenerates from HEAD every run — so re-running with a prior install PR still
    open updates that PR rather than failing non-fast-forward. (Plain ``--force``,
    not ``--force-with-lease``: a freshly recreated branch has no remote-tracking
    ref to lease against, and the branch is shipit-exclusive, so there is nothing
    to protect.) The break-glass push to a real branch (main) never forces.
    """
    args = ["push"]
    if force:
        args.append("--force")
    args += [remote, branch]
    _git(args, cwd=cwd, timeout=_NETWORK_TIMEOUT)


# --------------------------------------------------------------------------
# git — the Tree-creation boundary (clone / fetch / checkout)
# --------------------------------------------------------------------------


def git_clone_dissociated(url: str, dest: str, *, reference: str) -> None:
    """Clone ``url`` into ``dest`` as an INDEPENDENT, dissociated checkout.

    ``--reference <reference>`` borrows the local checkout's object store so the
    clone is near-instant and tiny over the wire; ``--dissociate`` then copies
    every borrowed object into the new clone and drops the alternates link, so the
    result shares NOTHING with the reference (no ``.git/objects/info/alternates``)
    and is safe to ``rm -rf`` (ADR-0014). ``origin`` is set to ``url`` — the GitHub
    URL — so ``gh``/``git`` work inside the Tree unchanged.
    """
    _run(
        ["git", "clone", "--reference", reference, "--dissociate", url, dest],
        timeout=_CLONE_TIMEOUT,
    )


def git_fetch(*, cwd: str, remote: str = "origin") -> None:
    """``git fetch <remote>`` inside the Tree, so its base ref is up to date."""
    _git(["fetch", remote], cwd=cwd, timeout=_NETWORK_TIMEOUT)


def git_checkout_new_branch(branch: str, base: str, *, cwd: str) -> None:
    """``git checkout -b <branch> <base>`` — cut ``branch`` from ``base`` and switch."""
    _git(["checkout", "-b", branch, base], cwd=cwd)


def git_checkout(branch: str, *, cwd: str) -> None:
    """``git checkout <branch>`` — switch to an EXISTING branch (no ``-b``).

    The read-only-Tree counterpart of :func:`git_checkout_new_branch`: a reviewer
    Tree checks out a branch that already exists on ``origin`` (the PR head) rather
    than cutting a new one. After a ``git fetch`` the plain checkout DWIMs a local
    tracking branch from ``origin/<branch>``, so the read-only clone lands on the
    exact head under review.
    """
    _git(["checkout", branch], cwd=cwd)


def git_reset_hard(ref: str, *, cwd: str) -> None:
    """``git reset --hard <ref>`` — force HEAD, index, and working tree to ``ref``.

    The read-only-Tree reuse counterpart of :func:`git_checkout`: when a shared review
    clone is reused after the PR head advanced, a ``git fetch`` followed by a hard reset
    to ``origin/<branch>`` re-pins the working tree to the CURRENT head, so a second
    reviewer never reads the stale commit the first clone happened to land on.
    """
    _git(["reset", "--hard", ref], cwd=cwd)


def git_remote_url(*, cwd: str, remote: str = "origin") -> str:
    """The configured URL of ``remote`` for the checkout at ``cwd``."""
    return _git(["remote", "get-url", remote], cwd=cwd).strip()


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
    base = ["git", "-C", cwd] if cwd is not None else ["git"]
    args = [*base, "ls-remote", "--heads", remote, ref]
    for line in _run(args).splitlines():
        # Each line is "<sha>\t<refname>"; require exact refname equality.
        parts = line.split("\t")
        if len(parts) == 2 and parts[1] == ref:
            return True
    return False


# --------------------------------------------------------------------------
# git + gh — the Tree-registry boundary (scan reads; never mutates)
# --------------------------------------------------------------------------


def git_upstream_ref(*, cwd: str) -> str | None:
    """The branch's configured upstream tracking ref (e.g. ``origin/main``), or ``None``.

    This is the only durable, on-disk record of what a Tree's branch is measured
    against — there is NO manifest (PRD: the clones on disk are the whole store), so
    ``scan`` reports the upstream git itself tracks as the Tree's *base*. ``None`` when
    the branch has no upstream (never pushed / set), which ``scan`` surfaces as such.
    """
    try:
        result = _git_probe(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            cwd=cwd,
        )
    except ExecError:
        return None
    if not result.ok:
        return None
    return result.stdout.strip() or None


def git_ahead_behind(*, cwd: str) -> tuple[int, int]:
    """``(ahead, behind)`` commit counts of ``HEAD`` vs its upstream.

    ``ahead`` is commits on ``HEAD`` not yet on the upstream (unpushed); ``behind`` is
    commits on the upstream not yet on ``HEAD``. ``(0, 0)`` when there is no upstream
    (or the rev-list fails), so a freshly-cut Tree reads as level rather than erroring.
    """
    try:
        result = _git_probe(
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


def git_unpushed_shas(*, cwd: str) -> tuple[str, ...] | None:
    """The SHAs of commits on ``HEAD`` that exist on NO remote — ``None`` if unreadable.

    The upstream-independent "unpushed" signal ADR-0027's ephemeral gc ladder is
    defined over: :func:`git_ahead_behind`'s ``ahead`` reads ``(0, 0)`` for a branch
    with **no upstream**, so a fresh ``ephemeral/<id>`` branch carrying local-only
    commits would look level — exactly the misread that loses work. ``rev-list HEAD
    --not --remotes`` lists commits reachable from ``HEAD`` but from no remote ref,
    so a missing upstream never by itself blocks reclaim (empty = everything on some
    remote) while a genuinely local commit is always listed. The SHAs — not just a
    count — are what lets the ephemeral floor exclude exactly the recorded
    provisioning commit (#232) while any OTHER local-only commit still protects.

    ``None`` — not empty — when the list cannot be read (detached/unborn HEAD, a git
    failure, malformed output): the CALLER must treat "unknown" conservatively (keep),
    and collapsing it to "nothing unpushed" would point the failure mode at data loss.
    """
    try:
        result = _git_probe(["rev-list", "HEAD", "--not", "--remotes"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    shas = _validated_shas(result.stdout)
    return tuple(shas) if shas is not None else None


def git_commits_between(base: str, head: str, *, cwd: str) -> list[str] | None:
    """The SHAs reachable from ``head`` but not ``base`` (``rev-list base..head``).

    Used at Tree provisioning to identify exactly what the managed-set install
    committed (#232): the SHAs between the pre- and post-install ``HEAD``. ``None``
    on any git failure or malformed output so the caller records nothing rather
    than something wrong — an unrecorded provisioning commit only KEEPS the Tree.
    """
    try:
        result = _git_probe(["rev-list", f"{base}..{head}"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    return _validated_shas(result.stdout)


def _validated_shas(out: str) -> list[str] | None:
    """Parse ``git rev-list`` output into validated, normalized sha strings.

    Validity lives in the :class:`shipit.identity.Sha` constructor (COR02) — the
    old ad-hoc "looks like a sha" check retired into the type. Malformed output
    yields ``None`` (record nothing rather than something wrong), matching the
    callers' conservative contract; the values returned are the type's
    lowercase-normalized string forms.
    """
    # Imported here, not at module top: `identity` composes over this boundary
    # module (its resolvers default to it), so a top-level import would cycle.
    from .identity import Sha

    try:
        return [str(Sha(line.strip())) for line in out.splitlines() if line.strip()]
    except ValueError:
        return None


def pr_for_head(branch: str, *, cwd: str | None = None) -> dict | None | UnknownPr:
    """The PR whose head is ``branch`` as ``{number, state, isDraft, baseRefName}`` — or
    ``None`` / :data:`UNKNOWN`.

    Reads ``gh pr view <branch> --json number,state,isDraft,baseRefName`` from inside
    the Tree (``cwd``) and returns a THREE-way result, never crashing the fleet scan:

    - the PR snapshot ``dict`` when a PR is read cleanly;
    - ``None`` when the branch *provably* has no PR — ``gh`` exits non-zero with its
      documented "no pull requests found" message;
    - :data:`UNKNOWN` when the state is *undetermined* — any OTHER ``gh`` failure
      (auth/network/rate-limit) or empty/malformed/non-JSON output.

    The ``None`` vs :data:`UNKNOWN` split is load-bearing: an unreadable state must
    NOT masquerade as "no PR" (which would let a conservative caller treat it like an
    abandoned Tree). Callers surface UNKNOWN (``gc``'s incomplete-sweep warning) and
    keep treating it conservatively, but they can now tell the two apart.
    """
    try:
        result = _run_probe(
            ["gh", "pr", "view", branch, "--json", "number,state,isDraft,baseRefName"],
            cwd=cwd,
        )
    except ExecError:
        return UNKNOWN
    if result.rc != 0:
        # gh's documented no-PR message is the one provable absence; every other
        # failure (auth/network/rate-limit) leaves the state undetermined.
        return None if NO_PR_MARKER in result.stderr.lower() else UNKNOWN
    out = result.stdout.strip()
    if not out:
        return UNKNOWN
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return UNKNOWN
    if not isinstance(data, dict):
        return UNKNOWN
    number = data.get("number")
    state = data.get("state")
    # A dict that decoded cleanly but is missing/mistyped its load-bearing fields
    # (e.g. ``{}`` or ``{"number": null, "state": null}``) is NOT a usable PR
    # snapshot — returning it would render as ``#None None`` in ``tree list``. Treat
    # it as an undetermined state, the same as malformed/non-JSON output above.
    if not isinstance(number, int) or not isinstance(state, str):
        return UNKNOWN
    return {
        "number": number,
        "state": state,
        "isDraft": data.get("isDraft"),
        "baseRefName": data.get("baseRefName"),
    }


#: ``gh`` exits non-zero with this message when a head simply has no associated
#: PR — the one failure that is a provable *absence*, not an undetermined state
#: (the exit code is a bare ``1`` for both cases, so the stderr message is the
#: only signal). Matched narrowly on purpose so an unrelated failure is never
#: mistaken for a provable absence. Public: ``verbs/pr/_resolve.py`` keys on
#: the SAME marker when branching on :func:`pr_number_probe`'s result, so the
#: per-tool knowledge is written down exactly once.
NO_PR_MARKER = "no pull requests found for branch"


def pr_number_probe() -> execrun.ExecResult:
    """``gh pr view --json number`` for the CURRENT branch, as a probe.

    The mechanics half of "which PR am I on?" — the argv lives here (ADR-0028:
    gh argv built outside the adapter is a defect) while the three-way
    branching (a number / provably no PR / a real gh failure) stays with the
    verb-layer resolver (``verbs/pr/_resolve``), which keys the no-PR case on
    :data:`NO_PR_MARKER` in the result's stderr. A probe because a PR-less
    branch is this call's NORMAL answer on every bare ``pr`` verb (records at
    DEBUG, never a spurious ERROR).
    """
    return _run_probe(["gh", "pr", "view", "--json", "number"])


def pr_url_for_head(branch: str, *, cwd: str | None = None) -> str | None:
    """The URL of the open PR whose head is ``branch``, or ``None``."""
    out = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url",
            "-q",
            ".[0].url",
        ],
        cwd=cwd,
    )
    return out.strip() or None


def pr_create(
    *,
    repo: str | None = None,
    base: str | None = None,
    head: str | None = None,
    title: str,
    body: str,
    draft: bool = True,
    cwd: str | None = None,
) -> str:
    """``gh pr create`` (draft by default); returns the new PR's URL.

    The body is passed on stdin (``--body-file -``) so a long, multi-line PR body
    never hits an argv limit.
    """
    args = ["gh", "pr", "create"]
    if repo is not None:
        args += ["--repo", repo]
    if base is not None:
        args += ["--base", base]
    if head is not None:
        args += ["--head", head]
    if draft:
        args.append("--draft")
    args += ["--title", title, "--body-file", "-"]
    return _run(args, input_text=body, cwd=cwd).strip()


# --------------------------------------------------------------------------
# gh — the PR-flow acts (merged from the engine's ghapi boundary, PROC02-WS01)
# --------------------------------------------------------------------------


def pr_edit_reviewer(pr: int, reviewer: str, *, remove: bool = False) -> None:
    """Add (or remove) a reviewer on a PR via ``gh pr edit``.

    ``gh pr edit --add-reviewer`` resolves the reviewer handle to its real
    node id and mutates through GraphQL. That path is load-bearing for bot
    reviewers: the REST ``requested_reviewers`` POST silently no-ops for them
    (returns 200 but leaves ``requested_reviewers`` empty) — never swap this
    for the REST call.
    """
    owner, name = repo_slug()
    flag = "--remove-reviewer" if remove else "--add-reviewer"
    _run(["gh", "pr", "edit", str(pr), "--repo", f"{owner}/{name}", flag, reviewer])


def pr_ready(pr: int, *, undo: bool = False) -> None:
    """Flip a PR's draft flag via ``gh pr ready`` (``--undo`` for ready→draft).

    ``gh pr ready`` is idempotent: flipping a PR that is already in the target
    state prints a notice and exits 0, so callers don't need to pre-check the
    flag to stay safe — they pre-check only to *say* something more useful.
    """
    owner, name = repo_slug()
    args = ["gh", "pr", "ready", str(pr), "--repo", f"{owner}/{name}"]
    if undo:
        args.append("--undo")
    _run(args)
    # The draft-flag flip is the ONE human hand-off signal in the whole cycle
    # (LOG02 convergence): give it a durable INFO milestone at the boundary that
    # performed it — before this, its only record was the Exec runner's DEBUG
    # line, invisible to an INFO-level read of the story.
    logger.info(
        "pr#%s draft flag flipped %s on %s/%s",
        pr,
        "ready→draft" if undo else "draft→ready",
        owner,
        name,
        extra={"pr": pr, "repo": f"{owner}/{name}"},
    )


def pr_review_reply(pr: int, comment_id: int, body: str) -> None:
    """Post a threaded reply to an existing PR review comment.

    Wraps ``POST /repos/{owner}/{name}/pulls/{pr}/comments/{comment_id}/replies``
    — the dedicated reply endpoint that threads the new comment under the
    target rather than starting a fresh top-level review comment. ``comment_id``
    is the numeric REST id (the same handle ``pr resolve-thread`` takes); ``body``
    is the reply text. This is the push-back path: reply with rationale, then
    resolve the thread.
    """
    owner, name = repo_slug()
    rest(
        f"repos/{owner}/{name}/pulls/{pr}/comments/{comment_id}/replies",
        method="POST",
        fields={"body": body},
    )
