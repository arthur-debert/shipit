"""dogfood — the OPT-IN, LIVE end-to-end verification harness for ``shipit spawn``.

TRE03 builds shipit-owned subagent spawning (ADR-0017/0018/0019): ``shipit spawn
subagent`` creates an isolated **Tree** (a dissociated clone), launches a headless
``claude`` Run rooted in it, and — for a write Run — has that Run open a draft PR
from the Tree's branch; for a **reviewer** Run it provisions a shared READ-ONLY Tree
and posts a review through the PR. Every WS is unit-tested with the ``claude`` spawn
and the ``gh`` boundary FAKED (``tests/test_spawn_verb.py``,
``tests/test_tree_*.py``). Those faked tests prove the *shape* shipit produces; they
cannot prove the load-bearing facts that exist only against live tooling:

  - a real ``claude`` Run actually lands its work in the Tree (on the planned
    branch, NOT ``shipit/install``) and opens a **real draft PR** from it;
  - a real reviewer Run gets a **genuinely shared, genuinely read-only** Tree and
    actually **posts a review**;
  - a forced Tree-create failure **fails closed** (loud, no native-worktree
    fallback) against the real git/clone path;
  - the maintainer's three non-negotiable **isolation invariants** hold on every
    Tree a real spawn materializes, with **no origin side effects** from
    provisioning (WS08 made provisioning local-only — no ``shipit/install`` push,
    no stray PR).

This module is that missing live counterpart, mirroring
:mod:`shipit.review.funnel_verify`: :func:`verify` drives the whole spawn lifecycle
end-to-end against a **separate scratch checkout** and asserts every standing fact.

**Siting — why this is NOT in the test checks.** It spawns real ``claude`` Runs
(token spend), opens real PRs, and needs a scratch checkout + a live login, so it
must never run inside ``pixi run test`` / CI. So it is a standalone ``python -m``
entrypoint (``pixi run -e dogfood verify-spawn``), it REFUSES to run without an
explicit ``--scratch`` checkout and target coordinates (or their env equivalents),
and pytest never collects it (it lives in ``src/``, not ``tests/``). Its
assertion/wiring logic is regression-covered in the normal test checks by
``tests/test_spawn_dogfood.py``, which drives the pure assertions against fixtures
and the orchestration with every live seam FAKED — so the harness itself can't
silently rot even though its live mode is opt-in.

How the maintainer runs it live is documented in
``docs/dev/spawn-dogfood-verification.md`` (and ``--help``): it spawns real Runs and
opens real PRs, so running it live is a deliberate, informed act.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .. import gh
from ..tree import create as tree_create
from ..tree import layout

logger = logging.getLogger("shipit.spawn")

#: The env var whose RELATIVE value forces a deterministic, real fail-closed:
#: :func:`shipit.tree.layout.central_root` rejects a non-absolute Trees root with
#: ``ValueError``, which ``run_subagent`` catches on its fail-closed Tree-create
#: branch → a clean loud exit-1 with no native-worktree fallback. The forced-failure
#: scenario sets this so the harness exercises the REAL fail-closed code path.
TREES_ROOT_ENV = layout.CENTRAL_ROOT_ENV


@dataclass
class Check:
    """One asserted fact: a human-readable ``name``, whether it ``passed``, and a
    ``detail`` string carrying the observed value."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class Report:
    """The accumulated result of a :func:`verify` run — an ordered list of
    :class:`Check`s."""

    checks: list[Check] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        """Append a :class:`Check` and return its ``passed`` flag (so a caller can
        branch on it)."""
        self.checks.append(Check(name, passed, detail))
        return passed

    @property
    def passed(self) -> bool:
        """True only if at least one check ran and every check passed."""
        return bool(self.checks) and all(c.passed for c in self.checks)


@dataclass(frozen=True)
class SpawnInvocation:
    """The result of one ``shipit spawn subagent`` invocation — the seam the harness
    drives instead of a function call, so the LIVE run exercises the shipped CLI
    exactly as the coordinator does."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DogfoodConfig:
    """The coordinates of one live dogfood run.

    ``scratch`` is the separate checkout the harness spawns FROM (never the checkout
    building the feature); ``repo`` / ``epic`` / ``ws`` / ``issue`` are the spawn
    target; ``write_role`` is the role for the write Run (the reviewer Run always
    uses the ``reviewer`` role). ``central_root`` is the Trees root the spawned Trees
    must live under; empty resolves :func:`shipit.tree.layout.central_root`.
    """

    scratch: str
    repo: str
    epic: str
    ws: int
    issue: int
    write_role: str = "implementer"
    central_root: str = ""


# --------------------------------------------------------------------------
# Pure assertions — operate on a real Tree directory, so they are table-tested
# against planted fixtures (a real `.git` dir, a `.git` file, a planted
# `alternates`, a `.claude`-containing path) with NO live spawn.
# --------------------------------------------------------------------------


def assert_dissociated_clone(
    report: Report, tree_path: str | None, *, label: str
) -> None:
    """Isolation invariant 2: the Tree is a **dissociated clone**, not a worktree.

    A real ``git clone`` makes ``.git`` a **directory**; a native ``git worktree``
    (the ADR-0014 footgun) makes ``.git`` a **file** pointing into the parent repo's
    ``.git/worktrees/...``. And ``--dissociate`` (ADR-0014) means the clone keeps NO
    ``objects/info/alternates`` link back to the reference repo's object store — so
    the Tree survives the reference being moved/pruned. Both are asserted here.
    """
    if not _has_path(report, tree_path, label):
        return
    git = Path(tree_path) / ".git"  # type: ignore[arg-type]
    report.record(
        f"{label}: .git is a directory (a clone, NOT a native worktree)",
        git.is_dir(),
        f".git={'dir' if git.is_dir() else 'file' if git.is_file() else 'absent'}",
    )
    alternates = git / "objects" / "info" / "alternates"
    report.record(
        f"{label}: clone is dissociated (no objects/info/alternates)",
        not alternates.exists(),
        f"alternates={'present' if alternates.exists() else 'absent'}",
    )


def assert_under_central_root(
    report: Report, tree_path: str | None, central_root: str, *, label: str
) -> None:
    """Isolation invariant 3: the Tree lives **under the central root**, and **NOT
    inside any ``.claude`` directory**.

    The central root is one predictable place OUTSIDE every repo (ADR-0014); a Tree
    that escaped it — or that landed inside a ``.claude`` dir (the native-worktree
    home) — would break the uniform-cleanup / isolation guarantee the whole feature
    rests on.
    """
    if not _has_path(report, tree_path, label):
        return
    path = Path(tree_path).resolve()  # type: ignore[arg-type]
    root = Path(central_root).resolve()
    report.record(
        f"{label}: Tree is under the central root ({root})",
        path.is_relative_to(root),
        f"path={path}",
    )
    in_dotclaude = any(part == ".claude" for part in path.parts)
    report.record(
        f"{label}: Tree is NOT inside any .claude dir",
        not in_dotclaude,
        f"path={path}",
    )


def assert_distinct_from_scratch(
    report: Report, tree_path: str | None, scratch: str, *, label: str
) -> None:
    """Isolation invariant 1 (the per-Tree half of no-cwd-footgun): the Tree is a
    **distinct directory** from the scratch checkout — never the scratch repo itself,
    never nested inside it. The Run is rooted in this dir (ADR-0019 §1), so a Tree
    that coincided with (or sat inside) the scratch checkout would leak the Run's
    writes back into the parent. The complementary half — the scratch checkout staying
    clean after a write Run — is asserted in :func:`verify_write_run`.
    """
    if not _has_path(report, tree_path, label):
        return
    path = Path(tree_path).resolve()  # type: ignore[arg-type]
    scratch_path = Path(scratch).resolve()
    distinct = path != scratch_path and not path.is_relative_to(scratch_path)
    report.record(
        f"{label}: Tree is a distinct dir from the scratch checkout (cwd isolation)",
        distinct,
        f"tree={path} scratch={scratch_path}",
    )


def assert_readonly_worktree(
    report: Report, tree_path: str | None, *, label: str
) -> None:
    """Read-only-Tree invariant (ADR-0018): **nothing in the working tree is
    writable** — not the files, not the directories — while ``.git`` **stays
    writable** (git's own reads need it).

    Walks the working tree (skipping ``.git`` and symlinks) and asserts every file
    AND every directory (the Tree root included) has its write bits cleared. A
    writable directory is a real hole, not a nit: on Unix the right to create or
    delete an entry is governed by the *containing* directory's mode, so a reviewer
    with ``Bash`` could add files even with every file read-only — exactly the bits
    :func:`shipit.tree.readonly.chmod_readonly` clears. It then actively attempts to
    create a file in the working tree and requires that write to FAIL (the guardrail
    proven by behaviour, not just bits), and finally asserts ``.git`` itself is still
    a writable directory.
    """
    if not _has_path(report, tree_path, label):
        return
    root = Path(tree_path)  # type: ignore[arg-type]
    writable: list[str] = []
    checked = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        # ``os.walk`` visits the Tree root and then every surviving subdir as a
        # ``dirpath`` exactly once, so checking ``dirpath`` each pass covers every
        # directory; ``filenames`` covers the files in it.
        for entry in (Path(dirpath), *(Path(dirpath) / name for name in filenames)):
            if entry.is_symlink():
                continue
            checked += 1
            if entry.stat().st_mode & 0o222:
                writable.append(str(entry))
    report.record(
        f"{label}: read-only Tree has no writable working file or directory",
        checked > 0 and not writable,
        f"checked={checked} writable={writable[:3]}",
    )
    probe = root / ".shipit-dogfood-write-probe"
    write_failed = False
    try:
        probe.write_text("probe")
    except OSError:
        write_failed = True
    else:
        probe.unlink()  # it must NOT have succeeded; clean up if the guardrail leaked
    report.record(
        f"{label}: read-only Tree refuses a new file (an actual write fails)",
        write_failed,
        f"probe={probe}",
    )
    git = root / ".git"
    report.record(
        f"{label}: read-only Tree keeps .git writable (git reads still work)",
        git.is_dir() and bool(git.stat().st_mode & 0o222),
        f".git writable={git.is_dir() and bool(git.stat().st_mode & 0o222)}",
    )


def assert_isolation_invariants(
    report: Report,
    tree_path: str | None,
    *,
    central_root: str,
    scratch: str,
    label: str,
) -> None:
    """The three per-Tree isolation invariants the maintainer marks non-negotiable,
    asserted on one materialized Tree: dissociated-clone (not a worktree),
    under-central-root (not in ``.claude``), and distinct-from-scratch (cwd
    isolation)."""
    assert_dissociated_clone(report, tree_path, label=label)
    assert_under_central_root(report, tree_path, central_root, label=label)
    assert_distinct_from_scratch(report, tree_path, scratch, label=label)


def _has_path(report: Report, tree_path: str | None, label: str) -> bool:
    """Guard the pure assertions against a missing Tree path: record one failed check
    and return False (so the caller skips the rest) when there is no path to inspect."""
    if tree_path and Path(tree_path).exists():
        return True
    report.record(
        f"{label}: Tree path is present on disk",
        False,
        f"tree_path={tree_path!r}",
    )
    return False


def parse_spawned(stdout: str) -> dict | None:
    """Extract the ``SPAWNED`` JSON summary from a ``shipit spawn subagent`` stdout.

    The verb prints a ``SPAWNED`` line followed by a ``json.dumps(..., indent=2)``
    block (:func:`shipit.verbs.spawn._emit_spawned`) — the Run's coordinates
    (``tree``/``branch``/``base``/``role``/``backend`` and, for a write Run,
    ``pr``/``pr_state``/``pr_is_draft``). Returns the parsed dict, or ``None`` when
    no parseable SPAWNED block is present (a failed/silent spawn). Tolerates trailing
    output after the JSON via :func:`json.JSONDecoder.raw_decode`.
    """
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "SPAWNED":
            blob = "\n".join(lines[i + 1 :]).strip()
            try:
                obj, _ = json.JSONDecoder().raw_decode(blob)
            except json.JSONDecodeError:
                return None
            return obj if isinstance(obj, dict) else None
    return None


# --------------------------------------------------------------------------
# Live seams — module-level so tests monkeypatch them (exactly as
# test_review_funnel_verify fakes `funnel_verify.gh`). The defaults drive the real
# CLI / git / pixi / gh; the structural tests replace them with fakes.
# --------------------------------------------------------------------------


def _run_spawn(
    argv: list[str], *, cwd: str, env: Mapping[str, str] | None = None
) -> SpawnInvocation:
    """Run ``shipit <argv>`` in ``cwd`` and capture it (the live spawn seam).

    ``env`` overlays the inherited environment (used by the fail-closed scenario to
    inject a relative ``SHIPIT_TREES_ROOT``). ``stdin`` is ``/dev/null`` so a
    TTY-less child never blocks. ``check=False``: a nonzero spawn is a normal outcome
    the harness asserts on, not an exception.
    """
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    completed = subprocess.run(  # noqa: S603 — argv is a constructed list, never shell
        ["shipit", *argv],
        cwd=cwd,
        env=full_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    return SpawnInvocation(completed.returncode, completed.stdout, completed.stderr)


def _current_branch(tree_path: str) -> str | None:
    """The branch ``tree_path`` has checked out (``git rev-parse --abbrev-ref HEAD``)."""
    return gh.git_current_branch(cwd=tree_path)


def _pixi_runs(tree_path: str) -> tuple[bool, str]:
    """Whether ``pixi`` resolves and runs the provisioned env inside ``tree_path``.

    Runs ``pixi run python -c "print('pixi-ok')"`` with the parent's leaked
    ``PIXI_*`` project pointers scrubbed (the same leak class
    :func:`shipit.tree.create.provision_env` guards against, reused here), so the
    child ``pixi`` re-resolves the Tree's own manifest. Returns ``(ok, detail)``.
    """
    env = {k: v for k, v in os.environ.items() if not tree_create.is_leaked_pixi_var(k)}
    try:
        completed = subprocess.run(  # noqa: S603,S607 — fixed argv, scrubbed env
            ["pixi", "run", "python", "-c", "print('pixi-ok')"],
            cwd=tree_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return False, f"pixi not launchable: {exc}"
    ok = completed.returncode == 0 and "pixi-ok" in completed.stdout
    return ok, f"rc={completed.returncode}"


def _scratch_dirty(scratch: str) -> str:
    """The scratch checkout's porcelain status (empty = clean). The no-cwd-leak read:
    a write Run rooted in its Tree must leave the scratch checkout untouched."""
    return gh.git_status_porcelain(cwd=scratch).strip()


def _open_pr_heads(repo: str) -> list[str]:
    """The head ref names of every OPEN PR on ``repo`` (the origin-side-effect read).

    WS08 made provisioning local-only: a Tree create must push no ``shipit/install``
    and open no stray PR. Listing open PR heads lets the harness assert
    ``shipit/install`` is not among them.
    """
    obj = gh.rest(f"/repos/{repo}/pulls?state=open", paginate=True)
    if not isinstance(obj, list):
        return []
    heads = []
    for pr in obj:
        head = pr.get("head") if isinstance(pr, dict) else None
        ref = head.get("ref") if isinstance(head, dict) else None
        if ref:
            heads.append(ref)
    return heads


def _pr_reviews(repo: str, pr: int) -> list[dict]:
    """The reviews posted on ``repo``#``pr`` (``GET /repos/{repo}/pulls/{pr}/reviews``)."""
    obj = gh.rest(f"/repos/{repo}/pulls/{pr}/reviews", paginate=True)
    return [r for r in obj if isinstance(r, dict)] if isinstance(obj, list) else []


def _resolve_repo_slug(repo: str, *, scratch: str) -> str:
    """Resolve the spawn ``--repo`` value to a canonical GitHub ``owner/name`` for the
    REST seams.

    ``--repo`` accepts a bare repo *code* (e.g. ``shipit``) because ``shipit spawn
    subagent`` resolves identity from the ambient (scratch) checkout and uses
    ``--repo`` only as a wrong-checkout guard — but the ``/repos/{owner}/{name}/...``
    endpoints :func:`_open_pr_heads` and :func:`_pr_reviews` hit require the full
    slug, so passing the documented ``shipit`` straight through would request
    ``/repos/shipit/pulls`` and misreport the origin-side-effect / review checks.
    The spawn target IS the scratch checkout's repo (the verb refuses any other), so a
    slashless code resolves to the scratch checkout's ``owner/name``
    (:func:`shipit.gh.current_repo`); a value already in ``owner/name`` form is
    normalised to its canonical slug (:func:`shipit.gh.repo_canonical`).
    """
    if "/" in repo:
        return gh.repo_canonical(repo)
    return gh.current_repo(cwd=scratch)


# --------------------------------------------------------------------------
# Orchestration — drives the live scenarios through the seams above and the pure
# assertions, accumulating one Report. Each scenario degrades to recorded FAILs
# (never raises) so the harness always prints a structured PASS/FAIL.
# --------------------------------------------------------------------------


def verify_write_run(report: Report, cfg: DogfoodConfig) -> dict | None:
    """Spawn a real WRITE Run and assert it landed correctly.

    Drives ``shipit spawn subagent --role <write_role>`` against the scratch
    checkout, parses the SPAWNED summary, then asserts: the spawn exited 0; the Tree
    is on the planned ``EPIC/WSnn`` branch (NOT ``shipit/install``), both in the
    summary and on disk; the Run opened an OPEN, DRAFT PR; ``pixi`` runs in the Tree;
    the three isolation invariants hold; the scratch checkout stayed clean (no
    cwd-leak); and provisioning left no ``shipit/install`` PR on origin (no side
    effects). Returns the SPAWNED payload (so the reviewer scenario can read the PR
    number), or ``None`` if the write Run did not produce one.
    """
    branch = f"{cfg.epic}/WS{cfg.ws:02d}"
    argv = [
        "spawn",
        "subagent",
        "--repo",
        cfg.repo,
        "--epic",
        cfg.epic,
        "--ws",
        str(cfg.ws),
        "--issue",
        str(cfg.issue),
        "--role",
        cfg.write_role,
    ]
    result = _run_spawn(argv, cwd=cfg.scratch)
    if not report.record(
        "write spawn exited 0",
        result.returncode == 0,
        f"rc={result.returncode} stderr={result.stderr.strip()[:200]}",
    ):
        return None
    payload = parse_spawned(result.stdout)
    if not report.record("write spawn emitted a SPAWNED summary", payload is not None):
        return None
    assert payload is not None  # guarded by the record above
    tree_path = payload.get("tree")

    report.record(
        f"write Tree summary branch is {branch!r} (not shipit/install)",
        payload.get("branch") == branch != "shipit/install",
        f"branch={payload.get('branch')!r}",
    )
    report.record(
        "write Run opened an OPEN, DRAFT PR",
        payload.get("pr") is not None
        and payload.get("pr_state") == "OPEN"
        and payload.get("pr_is_draft") is True,
        f"pr={payload.get('pr')} state={payload.get('pr_state')} "
        f"draft={payload.get('pr_is_draft')}",
    )

    assert_isolation_invariants(
        report,
        tree_path,
        central_root=cfg.central_root,
        scratch=cfg.scratch,
        label="write Tree",
    )

    actual = _current_branch(tree_path) if tree_path else None
    report.record(
        f"write Tree HEAD is on the planned branch {branch!r}",
        actual == branch,
        f"HEAD={actual!r}",
    )
    report.record(
        "write Tree HEAD is NOT shipit/install",
        actual != "shipit/install",
        f"HEAD={actual!r}",
    )

    if tree_path:
        ok, detail = _pixi_runs(tree_path)
        report.record("pixi runs inside the write Tree", ok, detail)

    dirty = _scratch_dirty(cfg.scratch)
    report.record(
        "no cwd leak: scratch checkout stayed clean",
        dirty == "",
        "dirty" if dirty else "clean",
    )

    heads = _open_pr_heads(_resolve_repo_slug(cfg.repo, scratch=cfg.scratch))
    report.record(
        "no origin side effect: provisioning opened no shipit/install PR",
        "shipit/install" not in heads,
        f"open_pr_heads={heads}",
    )
    return payload


def verify_reviewer_run(
    report: Report, cfg: DogfoodConfig, write_payload: dict | None
) -> None:
    """Spawn a real REVIEWER Run and assert the read-only / shared / review facts.

    Drives ``shipit spawn subagent --role reviewer`` (no ``--issue``), then asserts:
    the spawn exited 0 and emitted a SPAWNED summary with NO PR linkage (a reviewer
    reports through the PR); the isolation invariants hold; the Tree is genuinely
    read-only; a SECOND reviewer spawn REUSES the same Tree (shared per
    ``(repo, branch)``, ADR-0018); and a NEW review actually landed on the write Run's
    PR — counted as a delta against the reviews present BEFORE this spawn, so a
    pre-existing review can never make the check pass on its own.
    """
    branch = f"{cfg.epic}/WS{cfg.ws:02d}"
    argv = [
        "spawn",
        "subagent",
        "--repo",
        cfg.repo,
        "--epic",
        cfg.epic,
        "--ws",
        str(cfg.ws),
        "--role",
        "reviewer",
    ]
    # Snapshot the PR's reviews BEFORE spawning the reviewer, so the post-spawn check
    # asserts a NEW review (a count delta), not merely "≥1 review exists".
    have_pr = bool(write_payload) and write_payload.get("pr") is not None
    repo_slug = _resolve_repo_slug(cfg.repo, scratch=cfg.scratch) if have_pr else ""
    reviews_before = (
        _pr_reviews(repo_slug, int(write_payload["pr"]))  # type: ignore[index]
        if have_pr
        else []
    )
    result = _run_spawn(argv, cwd=cfg.scratch)
    if not report.record(
        "reviewer spawn exited 0",
        result.returncode == 0,
        f"rc={result.returncode} stderr={result.stderr.strip()[:200]}",
    ):
        return
    payload = parse_spawned(result.stdout)
    if not report.record(
        "reviewer spawn emitted a SPAWNED summary", payload is not None
    ):
        return
    assert payload is not None  # guarded by the record above
    tree_path = payload.get("tree")

    report.record(
        "reviewer Tree carries no PR linkage (it reviews THROUGH the PR)",
        "pr" not in payload,
        f"pr={payload.get('pr')!r}",
    )
    report.record(
        f"reviewer Tree summary branch is {branch!r}",
        payload.get("branch") == branch,
        f"branch={payload.get('branch')!r}",
    )

    assert_isolation_invariants(
        report,
        tree_path,
        central_root=cfg.central_root,
        scratch=cfg.scratch,
        label="reviewer Tree",
    )
    assert_readonly_worktree(report, tree_path, label="reviewer Tree")

    # Shared per (repo, branch): a second reviewer on the same head REUSES the clone.
    second = _run_spawn(argv, cwd=cfg.scratch)
    second_payload = parse_spawned(second.stdout) if second.returncode == 0 else None
    report.record(
        "read-only Tree is shared per (repo,branch) (2nd reviewer reuses the clone)",
        second_payload is not None
        and second_payload.get("tree") == tree_path
        and tree_path is not None,
        f"first={tree_path!r} second={second_payload.get('tree') if second_payload else None!r}",
    )

    if have_pr:
        reviews_after = _pr_reviews(repo_slug, int(write_payload["pr"]))  # type: ignore[index]
        report.record(
            "reviewer Run posted a NEW review on the PR",
            len(reviews_after) > len(reviews_before),
            f"pr=#{write_payload['pr']} before={len(reviews_before)} "  # type: ignore[index]
            f"after={len(reviews_after)}",
        )
    else:
        report.record(
            "reviewer Run posted a NEW review on the PR",
            False,
            "no write-Run PR to read reviews from (write scenario did not open one)",
        )


def verify_fail_closed(report: Report, cfg: DogfoodConfig) -> None:
    """Force a Tree-create failure and assert the spawn **fails closed** — loud, with
    NO native-worktree fallback.

    Forces a deterministic, REAL create failure by setting ``SHIPIT_TREES_ROOT`` to a
    RELATIVE path: :func:`shipit.tree.layout.central_root` rejects it with
    ``ValueError``, which ``run_subagent`` catches on its fail-closed branch. Asserts
    the spawn exits nonzero, emits a loud ``tree creation failed`` diagnostic on
    stderr, and left NO native ``.claude/worktrees`` checkout under the scratch repo.
    """
    argv = [
        "spawn",
        "subagent",
        "--repo",
        cfg.repo,
        "--epic",
        cfg.epic,
        "--ws",
        str(cfg.ws),
        "--issue",
        str(cfg.issue),
        "--role",
        cfg.write_role,
    ]
    result = _run_spawn(argv, cwd=cfg.scratch, env={TREES_ROOT_ENV: "relative-not-abs"})
    report.record(
        "forced Tree-create failure exits nonzero (fail-closed)",
        result.returncode != 0,
        f"rc={result.returncode}",
    )
    report.record(
        "fail-closed is loud (diagnostic on stderr)",
        "tree creation failed" in result.stderr.lower(),
        f"stderr={result.stderr.strip()[:200]}",
    )
    native = Path(cfg.scratch) / ".claude" / "worktrees"
    # Guard ``iterdir`` with ``is_dir``: if ``.claude/worktrees`` exists as a FILE or a
    # broken symlink it is not a worktree home, and ``iterdir`` would raise
    # ``NotADirectoryError`` — the harness must report a clean PASS/FAIL, never crash.
    report.record(
        "fail-closed left NO native worktree fallback",
        not native.is_dir() or not any(native.iterdir()),
        f"{native} exists={native.exists()} is_dir={native.is_dir()}",
    )


def verify(cfg: DogfoodConfig) -> Report:
    """Drive the whole live dogfood — write Run, reviewer Run, fail-closed — and
    assert every standing fact, returning the accumulated :class:`Report`.

    Resolves the central root (when ``cfg.central_root`` is empty), then runs the
    three scenarios in order: the write Run (whose PR the reviewer reviews), the
    reviewer Run, and the forced fail-closed. It NEVER raises — each scenario calls
    live seams (``gh.rest``, git, pixi, the filesystem), and any one of them could
    throw, so each scenario is run under :func:`_guard`, which turns an unexpected
    exception into a recorded FAIL. The structured PASS/FAIL report is therefore
    always produced, and the harness always exits 0/1.
    """
    central_root = cfg.central_root or str(layout.central_root())
    cfg = DogfoodConfig(
        scratch=cfg.scratch,
        repo=cfg.repo,
        epic=cfg.epic,
        ws=cfg.ws,
        issue=cfg.issue,
        write_role=cfg.write_role,
        central_root=central_root,
    )
    report = Report()
    write_payload = _guard(
        report, "write Run scenario", lambda: verify_write_run(report, cfg)
    )
    _guard(
        report,
        "reviewer Run scenario",
        lambda: verify_reviewer_run(report, cfg, write_payload),
    )
    _guard(report, "fail-closed scenario", lambda: verify_fail_closed(report, cfg))
    return report


def _guard(report: Report, scenario: str, fn):
    """Run one scenario, converting an unexpected exception into a recorded FAIL.

    The orchestration promises :func:`verify` never raises and always prints the
    report, but the scenarios drive live seams that can throw. Wrapping each one here
    means a thrown ``gh``/git/pixi/fs error becomes one failed :class:`Check` (carrying
    the exception detail) on top of whatever checks the scenario already recorded
    before it threw — so the structured report still prints. Returns the scenario's
    value on success, or ``None`` when it threw (so a caller can keep chaining)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — the whole point is to never let one escape
        report.record(
            f"{scenario} ran without an unexpected error",
            False,
            f"{type(exc).__name__}: {exc}",
        )
        return None


def format_report(report: Report, *, cfg: DogfoodConfig) -> str:
    """A clear, line-per-check PASS/FAIL block for the console."""
    verdict = "PASS" if report.passed else "FAIL"
    lines = [
        f"shipit spawn dogfood verification — {verdict}",
        f"  scratch={cfg.scratch}  repo={cfg.repo}  "
        f"target={cfg.epic}/WS{cfg.ws:02d} issue=#{cfg.issue}",
        "",
    ]
    for check in report.checks:
        mark = "PASS" if check.passed else "FAIL"
        line = f"  [{mark}] {check.name}"
        if check.detail:
            line += f"  ({check.detail})"
        lines.append(line)
    lines.append("")
    lines.append(f"shipit spawn dogfood verification — {verdict}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Console entrypoint: parse the scratch target, run :func:`verify`, print the
    PASS/FAIL report, and exit ``0``/``1``.

    REFUSES to run without an explicit ``--scratch`` checkout AND the spawn
    coordinates (or their env equivalents), so it can never fire by accident inside
    the test checks — and because a live run spawns real ``claude`` Runs and opens
    real PRs (token spend), which must be a deliberate act.
    """
    parser = argparse.ArgumentParser(
        prog="shipit-spawn-dogfood",
        description=(
            "OPT-IN live end-to-end verification of `shipit spawn subagent` "
            "(write Run -> draft PR, reviewer Run -> shared read-only Tree + review, "
            "fail-closed, + the isolation invariants) against a SCRATCH checkout. "
            "Spawns real claude Runs and opens real PRs — run it deliberately."
        ),
    )
    parser.add_argument(
        "--scratch",
        default=os.environ.get("SHIPIT_DOGFOOD_SCRATCH"),
        help="path to the scratch checkout to spawn FROM (or SHIPIT_DOGFOOD_SCRATCH). "
        "NEVER the checkout building the feature.",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("SHIPIT_DOGFOOD_REPO"),
        help="repo the spawn targets, as owner/name (e.g. arthur-debert/shipit) or a "
        "bare repo code resolved against the scratch checkout (or SHIPIT_DOGFOOD_REPO).",
    )
    parser.add_argument(
        "--epic",
        default=os.environ.get("SHIPIT_DOGFOOD_EPIC"),
        help="epic code the spawned Run rides, e.g. TRE03 (or SHIPIT_DOGFOOD_EPIC).",
    )
    parser.add_argument(
        "--ws",
        type=int,
        default=_env_int("SHIPIT_DOGFOOD_WS"),
        help="work stream number (or SHIPIT_DOGFOOD_WS).",
    )
    parser.add_argument(
        "--issue",
        type=int,
        default=_env_int("SHIPIT_DOGFOOD_ISSUE"),
        help="issue the write Run implements (or SHIPIT_DOGFOOD_ISSUE).",
    )
    parser.add_argument(
        "--write-role",
        default=os.environ.get("SHIPIT_DOGFOOD_WRITE_ROLE", "implementer"),
        help="role for the write Run (default: implementer).",
    )
    args = parser.parse_args(argv)

    missing = [
        name
        for name, value in (
            ("--scratch", args.scratch),
            ("--repo", args.repo),
            ("--epic", args.epic),
            ("--ws", args.ws),
            ("--issue", args.issue),
        )
        if value is None
    ]
    if missing:
        parser.error(
            f"missing required {', '.join(missing)} (or the SHIPIT_DOGFOOD_* env "
            "equivalents). This harness spawns LIVE claude Runs and opens real PRs; "
            "it never runs by accident."
        )

    cfg = DogfoodConfig(
        scratch=args.scratch,
        repo=args.repo,
        epic=args.epic,
        ws=args.ws,
        issue=args.issue,
        write_role=args.write_role,
    )
    report = verify(cfg)
    print(format_report(report, cfg=cfg))
    return 0 if report.passed else 1


def _env_int(name: str) -> int | None:
    """Parse an int env var, or ``None`` when unset/blank/non-numeric."""
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
