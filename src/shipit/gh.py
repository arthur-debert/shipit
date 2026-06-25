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


def _run(
    args: list[str], *, input_text: str | None = None, cwd: str | None = None
) -> str:
    """Run a command, returning stdout. Raise :class:`GhError` on failure."""
    try:
        proc = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
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
) -> object:
    """Call ``gh api <path>`` and return the parsed JSON.

    ``method`` sets ``--method`` (GET when omitted). ``body``, when given, is
    JSON-encoded and piped to ``gh api --input -`` (the way to send a structured
    request body). ``paginate`` adds ``--paginate``; the per-page JSON arrays are
    concatenated into one list.
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
    out = _run(args, input_text=input_text)
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


def label_create(
    repo: str, name: str, *, description: str, color: str
) -> None:
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


def git_push(branch: str, *, cwd: str, remote: str = "origin") -> None:
    """``git push <remote> <branch>`` — a plain push (never ``--force``)."""
    _git(["push", remote, branch], cwd=cwd)


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
