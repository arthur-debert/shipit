"""The single GitHub / git boundary for shipit.

Every call that shells out to ``gh`` or ``git`` lives here, so the rest of the
package is pure and unit-testable by patching this one module. This is the slim
descendant of release-core's ``gh.py`` — only the surface ``gh-setup`` needs.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess

#: The boundary's logger — a child of the package ``shipit`` logger, so it
#: inherits the sinks :func:`shipit.logsetup.configure_logging` attaches. Every
#: ``gh`` / ``git`` call and its outcome is recorded here at DEBUG (the verbose
#: file/CI record), so the console surface (WARNING+) stays unchanged.
logger = logging.getLogger("shipit.gh")

#: Token shapes GitHub mints (PAT / OAuth / user / installation / refresh, plus
#: the fine-grained ``github_pat_`` prefix). Used to MASK any token-shaped
#: argument before a call's argv is logged — so a secret accidentally placed in
#: argv never reaches a sink. Tokens normally travel in the env (never argv);
#: this is the load-bearing no-secrets guard, applied belt-and-suspenders.
_TOKEN_RE = re.compile(r"gh[posru]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+")

#: The placeholder a masked secret is replaced with in a log record.
_REDACTED = "***"


def _argv_for_log(args: list[str]) -> str:
    """A single redacted command string for a log record.

    Only the argv is ever logged — never the child env, the ``token``, or any
    stdin body (``input_text``): those are the secret-bearing channels and are
    deliberately kept out of every record. Any token-shaped argument is masked
    too, as defence in depth.
    """
    return _TOKEN_RE.sub(_REDACTED, " ".join(args))


class GhError(RuntimeError):
    """A ``gh`` / ``git`` invocation failed (non-zero exit)."""


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
) -> str:
    """Run a command, returning stdout. Raise :class:`GhError` on failure.

    ``token``, when given, runs the subprocess with ``GH_TOKEN=<token>`` (and
    ``GITHUB_TOKEN`` cleared) so a ``gh`` call authenticates as that token rather
    than the user's login (see :func:`_token_env`).
    """
    env = None
    if token is not None:
        import os

        # Drop GITHUB_TOKEN entirely (not blank it) so only GH_TOKEN remains.
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        env.update(_token_env(token))
    cmd = _argv_for_log(args)
    # Record the call before it runs: the redacted argv, the cwd, and the auth
    # mode (a bare boolean — NEVER the token value). The token / env / stdin body
    # are the secret-bearing channels and are intentionally absent from the log.
    logger.debug(
        "run %s (cwd=%s, auth=%s)", cmd, cwd or ".", "token" if token else "default"
    )
    try:
        proc = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError as exc:
        logger.debug("run %s -> %r not found on PATH", cmd, args[0])
        raise GhError(f"{args[0]!r} not found on PATH") from exc
    if proc.returncode != 0:
        # Redact the argv AND the stderr in BOTH the log record and the raised
        # error: GhError messages are surfaced and re-logged by callers (e.g.
        # review.post logs the exc), so a token echoed in argv/stderr must never
        # ride the exception text to a sink either.
        stderr = _TOKEN_RE.sub(_REDACTED, proc.stderr.strip())
        logger.debug("run %s -> exit %s: %s", cmd, proc.returncode, stderr)
        raise GhError(f"{cmd} exited {proc.returncode}: {stderr}")
    logger.debug("run %s -> ok (%d bytes stdout)", cmd, len(proc.stdout))
    return proc.stdout


# --------------------------------------------------------------------------
# gh api
# --------------------------------------------------------------------------


def rest(
    path: str,
    *,
    method: str | None = None,
    body: object | None = None,
    paginate: bool = False,
    token: str | None = None,
) -> object:
    """Call ``gh api <path>`` and return the parsed JSON.

    ``method`` sets ``--method`` (GET when omitted). ``body``, when given, is
    JSON-encoded and piped to ``gh api --input -`` (the way to send a structured
    request body). ``paginate`` adds ``--paginate``; the per-page JSON arrays are
    concatenated into one list. ``token``, when given, authenticates the call as
    that token (a GitHub App installation token) instead of the user's ``gh``
    login — the seam for posting a review AS ``<app-slug>[bot]``.
    """
    args = ["gh", "api", path]
    if method:
        args += ["--method", method]
    if paginate:
        args.append("--paginate")
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


def pr_view(pr: str, *, repo: str | None = None, json_fields: list[str]) -> str:
    """``gh pr view <pr> [--repo …] --json <fields>`` → stripped stdout (the JSON).

    Raises :class:`GhError` if gh fails (e.g. the PR can't be resolved); the
    caller parses the returned JSON object.
    """
    args = ["gh", "pr", "view", pr]
    if repo is not None:
        args += ["--repo", repo]
    args += ["--json", ",".join(json_fields)]
    return _run(args).strip()


def repo_root() -> str | None:
    """The local git working-tree root, or ``None`` when not inside one."""
    try:
        out = _run(["git", "rev-parse", "--show-toplevel"])
    except GhError:
        return None
    return out.strip() or None


def default_branch(repo: str) -> str:
    """The repo's default branch name."""
    info = rest(f"repos/{repo}")
    if not isinstance(info, dict) or "default_branch" not in info:
        raise GhError(f"could not resolve default branch for {repo}")
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


def _git(args: list[str], *, cwd: str) -> str:
    """``git -C <cwd> <args>`` via :func:`_run`."""
    return _run(["git", "-C", cwd, *args])


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
        name = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd).strip()
    except GhError:
        return None
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
            _git(["show-ref", "--verify", "--quiet", ref], cwd=cwd)
            return True
        except GhError:
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
    _git(args, cwd=cwd)


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
    _run(["git", "clone", "--reference", reference, "--dissociate", url, dest])


def git_fetch(*, cwd: str, remote: str = "origin") -> None:
    """``git fetch <remote>`` inside the Tree, so its base ref is up to date."""
    _git(["fetch", remote], cwd=cwd)


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
        out = _git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            cwd=cwd,
        ).strip()
    except GhError:
        return None
    return out or None


def git_ahead_behind(*, cwd: str) -> tuple[int, int]:
    """``(ahead, behind)`` commit counts of ``HEAD`` vs its upstream.

    ``ahead`` is commits on ``HEAD`` not yet on the upstream (unpushed); ``behind`` is
    commits on the upstream not yet on ``HEAD``. ``(0, 0)`` when there is no upstream
    (or the rev-list fails), so a freshly-cut Tree reads as level rather than erroring.
    """
    try:
        out = _git(
            ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"], cwd=cwd
        ).strip()
    except GhError:
        return (0, 0)
    parts = out.split()
    if len(parts) != 2:
        return (0, 0)
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return (0, 0)
    return (ahead, behind)


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
        out = _run(
            ["gh", "pr", "view", branch, "--json", "number,state,isDraft,baseRefName"],
            cwd=cwd,
        ).strip()
    except GhError as exc:
        return None if _is_no_pr_error(exc) else UNKNOWN
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


# gh's exact wording when a head has no associated PR — the SAME marker
# verbs/pr/_resolve.py keys on (_NO_PR_MARKER). Kept narrow on purpose: a
# broader substring risks classifying an unrelated gh failure as a provable
# no-PR and collapsing it to None instead of UNKNOWN.
_NO_PR_MARKER = "no pull requests found for branch"


def _is_no_pr_error(exc: GhError) -> bool:
    """``True`` when a ``gh pr view`` failure means "this branch has no PR".

    ``gh`` exits non-zero with a "no pull requests found for branch ..." message when
    a head simply has no associated PR — the one failure that is a provable *absence*,
    not an undetermined state. Every other ``GhError`` (auth, network, rate-limit) is
    left as :data:`UNKNOWN`. Matched on ``gh``'s precise no-PR marker (the exit code is
    a bare ``1`` for both cases, so the message is the only signal) — narrow on
    purpose so an unrelated failure is never mistaken for a provable absence.
    """
    return _NO_PR_MARKER in str(exc).lower()


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
