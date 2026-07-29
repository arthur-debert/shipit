"""The OPT-IN, LIVE verification harness for ``shipit spawn``: it opens real PRs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .. import execrun, gh, git, pixienv
from ..tree import layout
from . import launch

logger = logging.getLogger("shipit.spawn")

TREES_ROOT_ENV = layout.CENTRAL_ROOT_ENV


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> bool:
        """Append a :class:`Check` and return its ``passed`` flag."""
        self.checks.append(Check(name, passed, detail))
        return passed

    @property
    def passed(self) -> bool:
        """True only if at least one check ran and every check passed."""
        return bool(self.checks) and all(c.passed for c in self.checks)


@dataclass(frozen=True)
class SpawnInvocation:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DogfoodConfig:
    """One live dogfood run's coordinates; an empty ``central_root`` resolves."""

    scratch: str
    repo: str
    epic: str
    ws: int
    issue: int
    write_role: str = "implementer"
    central_root: str = ""


def assert_dissociated_clone(
    report: Report, tree_path: str | None, *, label: str
) -> None:
    """The Tree is a dissociated clone, not a native worktree."""
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
    """Nothing in the working tree is writable — files AND dirs — but ``.git`` is."""
    if not _has_path(report, tree_path, label):
        return
    root = Path(tree_path)  # type: ignore[arg-type]
    writable: list[str] = []
    checked = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
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
    assert_dissociated_clone(report, tree_path, label=label)
    assert_under_central_root(report, tree_path, central_root, label=label)
    assert_distinct_from_scratch(report, tree_path, scratch, label=label)


def _has_path(report: Report, tree_path: str | None, label: str) -> bool:
    """False (after recording one failed check) when there is no Tree path to inspect."""
    if tree_path and Path(tree_path).exists():
        return True
    report.record(
        f"{label}: Tree path is present on disk",
        False,
        f"tree_path={tree_path!r}",
    )
    return False


def parse_spawned(stdout: str) -> dict | None:
    """The ``SPAWNED`` JSON block from stdout, or ``None`` if unparseable."""
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


def _run_spawn(
    argv: list[str], *, cwd: str, env: Mapping[str, str] | None = None
) -> SpawnInvocation:
    """Run ``shipit <argv>`` in ``cwd``; ``env`` overlays the parent's."""
    result = execrun.run(
        ["shipit", *argv],
        cwd=cwd,
        env=dict(env) if env else None,
        check=False,
        timeout=launch.LAUNCH_TIMEOUT,
    )
    return SpawnInvocation(result.rc, result.stdout, result.stderr)


def _current_branch(tree_path: str) -> str | None:
    return git.current_branch(cwd=tree_path)


def _pixi_runs(tree_path: str) -> tuple[bool, str]:
    """``(ok, detail)`` for whether pixi resolves and runs the env in ``tree_path``."""
    try:
        result = pixienv.run_in_env(
            ["python", "-c", "print('pixi-ok')"],
            tree_path,
            env=pixienv.scrub_env(os.environ),
            check=False,
        )
    except execrun.ExecError as exc:
        return False, f"pixi not launchable: {exc}"
    ok = result.ok and "pixi-ok" in result.stdout
    return ok, f"rc={result.rc}"


def _scratch_dirty(scratch: str) -> str:
    return "\n".join(git.status_porcelain(cwd=scratch))


def _open_pr_heads(repo: str) -> list[str]:
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
    obj = gh.rest(f"/repos/{repo}/pulls/{pr}/reviews", paginate=True)
    return [r for r in obj if isinstance(r, dict)] if isinstance(obj, list) else []


def _resolve_repo_slug(repo: str, *, scratch: str) -> str:
    """Resolve a spawn ``--repo`` value (a slug or a bare code) to ``owner/name``."""
    if "/" in repo:
        return gh.repo_canonical(repo).slug
    return gh.current_repo(cwd=scratch).slug


def verify_write_run(report: Report, cfg: DogfoodConfig) -> dict | None:
    """Spawn a real WRITE Run and return its SPAWNED payload."""
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
    """Spawn a real REVIEWER Run and assert the read-only / per-Run / review facts."""
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
        "--backend",
        "codex",
    ]
    # Snapshot BEFORE spawning: the check must assert a NEW review, not "≥1 exists".
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

    # A second reviewer on the same head gets its OWN distinct Tree.
    second = _run_spawn(argv, cwd=cfg.scratch)
    second_payload = parse_spawned(second.stdout) if second.returncode == 0 else None
    report.record(
        "read-only Tree is per-Run (2nd reviewer gets its own distinct Tree)",
        second_payload is not None
        and tree_path is not None
        and second_payload.get("tree") not in (None, tree_path),
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
    """Force a Tree-create failure (a RELATIVE Trees root) and assert fail-closed."""
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
    # ``is_dir`` guards ``iterdir`` against a file or broken symlink at that path.
    report.record(
        "fail-closed left NO native worktree fallback",
        not native.is_dir() or not any(native.iterdir()),
        f"{native} exists={native.exists()} is_dir={native.is_dir()}",
    )


def verify(cfg: DogfoodConfig) -> Report:
    """Drive write Run → reviewer Run → fail-closed into one Report. Never raises."""
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
    """Console entrypoint; refuses without an explicit ``--scratch`` target."""
    parser = argparse.ArgumentParser(
        prog="shipit-spawn-dogfood",
        description=(
            "OPT-IN live end-to-end verification of `shipit spawn subagent` "
            "(write Run -> draft PR, reviewer Run -> per-Run read-only Tree + review, "
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
