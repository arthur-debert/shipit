"""The one git Tool adapter: every ``git`` argv in shipit is assembled here.
See docs/adr/0028-one-exec-seam-tool-adapters.md
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
    from .identity import Sha

logger = logging.getLogger("shipit.git")

_NETWORK_TIMEOUT: float = execrun.DEFAULT_TIMEOUT
_LOCAL_TIMEOUT: float = 60.0
_CLONE_TIMEOUT: float = 600.0
_STRIP_TIMEOUT: float = 600.0


def _argv(args: list[str], cwd: str | None) -> list[str]:
    return ["git", "-C", cwd, *args] if cwd is not None else ["git", *args]


def _git(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float | None = _LOCAL_TIMEOUT,
    env: dict[str, str] | None = None,
) -> str:
    """Run ``git`` through the Exec runner, returning stdout; raises :class:`ExecError`."""
    return execrun.run(_argv(args, cwd), timeout=timeout, env=env).stdout


def _index_env(index_file: str | None) -> dict[str, str] | None:
    return {"GIT_INDEX_FILE": index_file} if index_file is not None else None


def _probe(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float | None = _LOCAL_TIMEOUT,
) -> execrun.ExecResult:
    """Run ``git`` as a probe: a nonzero exit is a NORMAL answer, not a failure."""
    return execrun.run(_argv(args, cwd), check=False, timeout=timeout)


def repo_root(*, cwd: str | None = None) -> str | None:
    """The git working-tree root, or ``None`` when ``cwd`` is not inside a checkout."""
    try:
        result = _probe(["rev-parse", "--show-toplevel"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    return result.stdout.strip() or None


def hooks_dir(*, cwd: str) -> Path | None:
    """The checkout's hooks dir, worktree-correct, or ``None`` when unresolvable."""
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
    """The ``HEAD`` commit, or ``None`` on any git failure (detached or unborn HEAD included)."""
    from .identity import Sha

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


def head_committed_at(*, cwd: str) -> float | None:
    """``HEAD``'s committer timestamp in epoch seconds; ``None`` — never ``0`` — when unreadable."""
    try:
        result = _probe(["log", "-1", "--format=%ct", "HEAD"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    raw = result.stdout.strip()
    try:
        return float(raw)
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
    """The remote's default branch, probing main/master/develop/trunk when ``<remote>/HEAD`` is unset."""
    result = _probe(["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"], cwd=cwd)
    if result.ok:
        name = result.stdout.strip().removeprefix(f"{remote}/")
        if name:
            return name
    for candidate in ("main", "master", "develop", "trunk"):
        probe = _probe(
            ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{candidate}"],
            cwd=cwd,
        )
        if probe.ok:
            return candidate
    return "main"


def remote_url(*, cwd: str, remote: str = "origin") -> str:
    return _git(["remote", "get-url", remote], cwd=cwd).strip()


def status_porcelain(*, cwd: str) -> list[str]:
    out = _git(["status", "--porcelain"], cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


def ls_files(*, cwd: str) -> list[str]:
    out = _git(["ls-files"], cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


def ls_files_matching(pathspecs: list[str], *, cwd: str) -> list[str] | None:
    """Tracked files matching ``pathspecs``, or ``None`` when ``cwd`` is not a git repo."""
    res = _probe(["ls-files", "-z", "--", *pathspecs], cwd=cwd)
    if not res.ok:
        return None
    return [p for p in res.stdout.split("\0") if p.strip()]


def tree_paths(ref: str, pathspecs: list[str], *, cwd: str) -> list[str] | None:
    """Which of ``pathspecs`` the commit ``ref`` carries, or ``None`` when ``ref`` is unreadable."""
    res = _probe(["ls-tree", "-r", "--name-only", "-z", ref, "--", *pathspecs], cwd=cwd)
    if not res.ok:
        return None
    return [p for p in res.stdout.split("\0") if p.strip()]


def epic_umbrella_exists(epic: str, *, cwd: str) -> bool:
    """Whether ``<epic>/umbrella`` exists as a LOCAL ref (remote-tracking or head); no network."""
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
    """``True`` iff ``refs/heads/<branch>`` exists on ``remote``, matched exactly and never as a pattern."""
    if any(ch in branch for ch in "*?["):
        return False
    ref = f"refs/heads/{branch}"
    out = _git(["ls-remote", "--heads", remote, ref], cwd=cwd, timeout=_NETWORK_TIMEOUT)
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1] == ref:
            return True
    return False


def upstream_ref(*, cwd: str) -> str | None:
    """The branch's configured upstream tracking ref, or ``None`` when it has none."""
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
    """``(ahead, behind)`` commit counts of ``HEAD`` vs its upstream; ``(0, 0)`` when there is none."""
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
    """Commits on ``HEAD`` reachable from no remote ref; ``None`` — not empty — when unreadable."""
    try:
        result = _probe(["rev-list", "HEAD", "--not", "--remotes"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    shas = _validated_shas(result.stdout)
    return tuple(shas) if shas is not None else None


def commits_between(base: Sha, head: Sha, *, cwd: str) -> list[Sha] | None:
    """``rev-list base..head``; ``None`` on git failure or malformed output."""
    try:
        result = _probe(["rev-list", f"{base}..{head}"], cwd=cwd)
    except ExecError:
        return None
    if not result.ok:
        return None
    return _validated_shas(result.stdout)


def _validated_shas(out: str) -> list[Sha] | None:
    """Parse ``rev-list`` output into ``Sha`` values; ``None`` if any line is malformed."""
    from .identity import Sha

    try:
        return [Sha(line.strip()) for line in out.splitlines() if line.strip()]
    except ValueError:
        return None


def resolve_commit(rev: str, *, cwd: str) -> Sha | None:
    """The commit ``rev`` names in ``cwd``, or ``None`` when it names no commit there."""
    from .identity import Sha

    result = _probe(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd=cwd)
    if not result.ok:
        return None
    raw = result.stdout.strip()
    try:
        return Sha(raw)
    except ValueError:
        return None


def commit_present(sha: Sha, *, cwd: str) -> bool:
    """Whether ``sha`` is present as a commit object in ``cwd``; never fetches."""
    return _probe(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=cwd).ok


def fetch_ref(refspec: str, *, cwd: str, remote: str = "origin") -> bool:
    """Best-effort fetch of ``refspec``: ``True`` if it ran clean, ``False`` if the ref is absent."""
    return _probe(
        ["fetch", "--quiet", remote, refspec], cwd=cwd, timeout=_NETWORK_TIMEOUT
    ).ok


def merge_base(a: Sha, b: Sha, *, cwd: str) -> Sha | None:
    """The merge base of ``a`` and ``b``, or ``None`` when they share no ancestor — never a guessed endpoint."""
    from .identity import Sha

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
    """``True`` only on a clean exit 0; a genuine non-ancestor AND any error both answer ``False``."""
    return _probe(
        ["merge-base", "--is-ancestor", str(ancestor), str(descendant)], cwd=cwd
    ).ok


def diff_range(base: Sha, head: Sha, *, cwd: str) -> str:
    """The two-dot patch text ``git diff <base>..<head>``."""
    return _git(["diff", f"{base}..{head}"], cwd=cwd)


def diff_name_only(base: Sha, head: Sha, *, cwd: str) -> list[str]:
    out = _git(["diff", "--name-only", f"{base}..{head}"], cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


def changed_paths_since(base_ref: str, *, cwd: str) -> list[str] | None:
    """Three-dot ``git diff --name-only <base_ref>...HEAD``; ``None`` when git cannot answer."""
    res = _probe(["diff", "--name-only", f"{base_ref}...HEAD"], cwd=cwd)
    if res.rc != 0:
        return None
    return [line for line in res.stdout.splitlines() if line.strip()]


def list_tags(*, cwd: str) -> list[str]:
    out = _git(["tag", "--list"], cwd=cwd)
    return [line.strip() for line in out.splitlines() if line.strip()]


def tag_annotated(name: str, message: str, *, cwd: str) -> None:
    _git(["tag", "-a", name, "-m", message], cwd=cwd)


def push_tag(name: str, *, cwd: str, remote: str = "origin") -> None:
    _git(["push", remote, f"refs/tags/{name}"], cwd=cwd, timeout=_NETWORK_TIMEOUT)


def push_atomic(branch: str, tag: str, *, cwd: str, remote: str = "origin") -> None:
    """Publish ``branch`` and ``refs/tags/<tag>`` as ONE server-side transaction: both refs or neither."""
    _git(
        ["push", "--atomic", remote, branch, f"refs/tags/{tag}"],
        cwd=cwd,
        timeout=_NETWORK_TIMEOUT,
    )


def delete_tag(name: str, *, cwd: str) -> None:
    _git(["tag", "-d", name], cwd=cwd)


def switch_create(branch: str, *, cwd: str) -> None:
    """Create-or-reset ``branch`` from the current HEAD and switch to it."""
    _git(["switch", "-C", branch], cwd=cwd)


def switch(branch: str, *, cwd: str) -> None:
    """Switch to an EXISTING branch; git refuses when an untracked file HEAD carries is in the way."""
    _git(["switch", branch], cwd=cwd)


def read_tree(ref: str, *, cwd: str, index_file: str) -> None:
    """Seed the scratch index at ``index_file`` from ``ref``; the real index and working tree are untouched."""
    _git(["read-tree", ref], cwd=cwd, env=_index_env(index_file))


def add(paths: list[str], *, cwd: str, index_file: str | None = None) -> None:
    """Stage only these pathspecs, forcing through ``.gitignore``; ``index_file`` targets a scratch index."""
    if not paths:
        return
    _git(["add", "-f", "--", *paths], cwd=cwd, env=_index_env(index_file))


def rm_cached(paths: list[str], *, cwd: str, index_file: str | None = None) -> None:
    """Stage removal of ``paths`` from the INDEX only; an unmatched or absent pathspec is a no-op."""
    if not paths:
        return
    _git(
        ["rm", "--cached", "--ignore-unmatch", "--", *paths],
        cwd=cwd,
        env=_index_env(index_file),
    )


def add_all(*, cwd: str) -> None:
    _git(["add", "-A"], cwd=cwd)


def commit_all(
    message: str, *, cwd: str, no_verify: bool = False, index_file: str | None = None
) -> None:
    """Commit everything already staged; ``index_file`` commits that scratch index instead of ``.git/index``."""
    args = ["commit"]
    if no_verify:
        args.append("--no-verify")
    _git([*args, "-m", message], cwd=cwd, env=_index_env(index_file))


def clean_non_committed(*, cwd: str) -> None:
    """Remove every untracked AND ignored path, leaving exactly the committed content."""
    _git(["clean", "-ffdx"], cwd=cwd, timeout=_STRIP_TIMEOUT)


def init_main(*, cwd: str) -> None:
    _git(["init", "-b", "main"], cwd=cwd)


def _ident_name(var: str, *, cwd: str) -> str | None:
    """The display name from ``git var <var>``'s IDENT string, or ``None`` when git cannot resolve one."""
    result = _probe(["var", var], cwd=cwd)
    if not result.ok:
        return None
    ident = result.stdout.strip()
    marker = ident.rfind(" <")
    if marker <= 0:
        return None
    return ident[:marker].strip() or None


def author_name(*, cwd: str) -> str | None:
    return _ident_name("GIT_AUTHOR_IDENT", cwd=cwd)


def committer_name(*, cwd: str) -> str | None:
    return _ident_name("GIT_COMMITTER_IDENT", cwd=cwd)


def commit(
    message: str, paths: list[str], *, cwd: str, no_verify: bool = False
) -> None:
    """Commit only the given pathspecs — git's PARTIAL-commit mode, which disregards the index."""
    args = ["commit"]
    if no_verify:
        args.append("--no-verify")
    _git([*args, "-m", message, "--", *paths], cwd=cwd)


def staged_paths(
    paths: list[str], *, cwd: str, index_file: str | None = None
) -> list[str]:
    """The subset of ``paths`` carrying a staged diff against HEAD; an empty ``paths`` never probes."""
    if not paths:
        return []
    out = _git(
        ["diff", "--cached", "--name-only", "--", *paths],
        cwd=cwd,
        env=_index_env(index_file),
    )
    return [line for line in out.splitlines() if line.strip()]


def reset_index(*, cwd: str) -> None:
    _git(["reset"], cwd=cwd)


def push(
    branch: str,
    *,
    cwd: str,
    remote: str = "origin",
    force: bool = False,
    no_verify: bool = False,
) -> None:
    """``force`` plain-force-pushes (no lease); ``no_verify`` bypasses the repo's pre-push hook."""
    args = ["push"]
    if force:
        args.append("--force")
    if no_verify:
        args.append("--no-verify")
    args += [remote, branch]
    _git(args, cwd=cwd, timeout=_NETWORK_TIMEOUT)


def pull_rebase(branch: str, *, cwd: str, remote: str = "origin") -> None:
    _git(["pull", "--rebase", remote, branch], cwd=cwd, timeout=_NETWORK_TIMEOUT)


_POISONED_REFERENCE_MARKERS: tuple[str, ...] = (
    "clone succeeded, but checkout failed",
    "unable to parse commit",
)


def _is_poisoned_reference_failure(err: ExecError) -> bool:
    """Whether ``err`` is the clone-succeeded-checkout-failed signature; only a real child exit qualifies."""
    if err.cause != execrun.CAUSE_EXIT:
        return False
    text = f"{err.stderr}\n{err.stdout}".lower()
    return all(marker in text for marker in _POISONED_REFERENCE_MARKERS)


def _resolve_reference_donor(reference: str) -> str:
    """The donor to borrow from: a linked worktree resolves to its shared common gitdir, anything else passes through."""
    absolute = _probe(["rev-parse", "--absolute-git-dir"], cwd=reference)
    common = _probe(["rev-parse", "--git-common-dir"], cwd=reference)
    if not absolute.ok or not common.ok:
        return reference
    absolute_gitdir = absolute.stdout.strip()
    common_out = common.stdout.strip()
    if not absolute_gitdir or not common_out:
        return reference
    common_gitdir = os.path.realpath(os.path.join(reference, common_out))
    if common_gitdir == os.path.realpath(absolute_gitdir):
        return reference
    logger.info(
        "reference %s is a linked worktree; dereferencing to its shared common "
        "gitdir %s for the --reference borrow (#509)",
        reference,
        common_gitdir,
    )
    return common_gitdir


def clone_dissociated(url: str, dest: str, *, reference: str) -> None:
    """Clone ``url`` into ``dest`` sharing nothing with ``reference``; a poisoned donor retries once as a full clone."""
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
        shutil.rmtree(dest, ignore_errors=True)
        _git(["clone", url, dest], timeout=_CLONE_TIMEOUT)


SAFE_DONOR_CONFIG: tuple[tuple[str, str], ...] = (
    ("fetch.writeCommitGraph", "false"),
    ("gc.writeCommitGraph", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
)


def configure_safe_reference_donor(*, cwd: str) -> None:
    """Disable the commit-graph writers and auto-gc so ``cwd`` stays a safe ``--reference`` donor."""
    for key, value in SAFE_DONOR_CONFIG:
        _git(["config", "--local", key, value], cwd=cwd)


def fetch(*, cwd: str, remote: str = "origin") -> None:
    _git(["fetch", remote], cwd=cwd, timeout=_NETWORK_TIMEOUT)


def clone(url: str, dest: str, *, depth: int | None = 1) -> None:
    args = ["clone"]
    if depth is not None:
        args += ["--depth", str(depth)]
    _git([*args, url, dest], timeout=_CLONE_TIMEOUT)


def configure_identity(name: str, email: str, *, cwd: str) -> None:
    """Set ``user.name``/``user.email`` in ``cwd``'s LOCAL git config."""
    _git(["config", "--local", "user.name", name], cwd=cwd)
    _git(["config", "--local", "user.email", email], cwd=cwd)


def checkout_create_or_reset(branch: str, base: str, *, cwd: str) -> None:
    """Cut ``branch`` from ``base`` and switch, resetting ``branch`` if it already exists locally."""
    _git(["checkout", "-B", branch, base], cwd=cwd)


def checkout(branch: str, *, cwd: str) -> None:
    """Switch to an existing branch; after a fetch this DWIMs a local branch tracking ``origin/<branch>``."""
    _git(["checkout", branch], cwd=cwd)


def reset_hard(ref: str, *, cwd: str) -> None:
    _git(["reset", "--hard", ref], cwd=cwd)


def reset_soft(ref: str, *, cwd: str) -> None:
    """Move HEAD to ``ref``, leaving the index and the working tree as they are."""
    _git(["reset", "--soft", ref], cwd=cwd)


def submodule_update_init(*, cwd: str) -> None:
    """Sync then recursively init submodules; a repo with none is a no-op, and a fetch failure raises."""
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
