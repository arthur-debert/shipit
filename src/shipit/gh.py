"""The single GitHub / git boundary for shipit.

Every call that shells out to ``gh`` or ``git`` lives here, so the rest of the
package is pure and unit-testable by patching this one module. This is the slim
descendant of release-core's ``gh.py`` — only the surface ``gh-setup`` needs.
"""

from __future__ import annotations

import json
import subprocess


class GhError(RuntimeError):
    """A ``gh`` / ``git`` invocation failed (non-zero exit)."""


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
        raise GhError(f"{args[0]!r} not found on PATH") from exc
    if proc.returncode != 0:
        raise GhError(
            f"{' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()}"
        )
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


def current_repo() -> str:
    """``owner/name`` of the repo in the current directory (via ``gh``)."""
    out = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]
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


def git_current_branch(*, cwd: str) -> str | None:
    """The current branch name, or ``None`` on a detached/unborn HEAD."""
    try:
        name = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd).strip()
    except GhError:
        return None
    return None if (not name or name == "HEAD") else name


def git_ls_files(*, cwd: str) -> list[str]:
    """Tracked files (``git ls-files``), repo-root-relative, in git's order.

    Tracked-only is deliberate: it keeps generated/ignored paths out of the lint
    scope without an exclude list (ROADMAP.lex §3 — "whole tree via git ls-files").
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
