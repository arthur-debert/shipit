"""Typed tests for the spawn domain pipeline (`shipit.spawn.subagent`, CLI02-WS02).

The ADR-0030 collapse of the old deep-monkeypatch verb tests: every stage of
the pipeline — shape validation → identity → umbrella check → Tree → launch →
post-condition audit — is driven typed-in/typed-out through the injectable
:class:`Boundaries` value (fake git/gh/create/runner seams as plain callables,
zero module monkeypatching). A refusal is the :class:`SpawnError` domain
exception asserted with ``pytest.raises``; success is a frozen
:class:`SpawnResult` asserted field by field. The CLI wiring (click binding,
the error shell, the byte-stable SPAWNED render) is the thin smoke layer in
``test_spawn_verb.py``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from shipit import execrun, gh, logcontext
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.spawn import launch
from shipit.spawn.subagent import (
    Boundaries,
    SpawnError,
    SubagentSpec,
    audit_handshake,
    spawn_subagent,
)
from shipit.tree import layout
from shipit.tree.create import Tree

_PR = gh.HeadPr(number=321, state="OPEN", is_draft=True, base_ref="TRE03/umbrella")


def spec(**overrides) -> SubagentSpec:
    """The default epic-shape write spec; override any field per test."""
    fields = dict(repo="widget", role="implementer", epic="TRE03", ws=1, issue=156)
    fields.update(overrides)
    return SubagentSpec(**fields)


def bounds(
    tmp_path: Path,
    *,
    pr=_PR,
    returncode: int = 0,
    umbrella: bool = True,
    org_repo: str = "acme/widget",
    status_lines: list[str] | None = None,
) -> tuple[Boundaries, dict]:
    """Fake every effectful edge as a recording callable; return (bounds, calls).

    The write/readonly creators 'create' a real directory (the launcher needs a
    real cwd) and resolve branch/base through the REAL pure planner
    (:func:`shipit.tree.layout.plan`), so the epic-grouped base the pipeline
    audits against is the true one, never a hardcoded string. The runner
    records the launch contract (cmd/cwd/env) and never spawns anything.
    ``status_lines`` is what the salvage probe's porcelain read reports (#587)
    — the default ``None`` means a clean tree.
    """
    calls: dict = {}
    parent = tmp_path / "repo"
    parent.mkdir(exist_ok=True)
    tree_dir = tmp_path / "tree"
    review_dir = tmp_path / "review"

    def create_tree(tree_spec, *, source_repo, github_url):
        calls["spec"] = tree_spec
        calls["source_repo"] = source_repo
        calls["github_url"] = github_url
        tree_dir.mkdir(parents=True, exist_ok=True)
        tp = layout.plan(tree_spec)
        return Tree(path=str(tree_dir), branch=tp.branch, base=tp.base)

    def create_readonly_tree(plan, *, source_repo, github_url):
        calls["plan"] = plan
        calls["source_repo"] = source_repo
        calls["github_url"] = github_url
        review_dir.mkdir(parents=True, exist_ok=True)
        return Tree(
            path=str(review_dir), branch=plan.branch, base=f"origin/{plan.branch}"
        )

    def runner(cmd, *, cwd, env, timeout=None):
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        calls["env"] = env
        calls["timeout"] = timeout
        return launch.LaunchResult(returncode=returncode, stdout="{}", stderr="boom")

    def pr_for_head(branch, *, cwd=None):
        calls["pr_branch"] = branch
        calls["pr_cwd"] = cwd
        return pr

    def remote_branch_exists(branch, *, cwd=None, remote="origin"):
        calls["umbrella_branch"] = branch
        calls["umbrella_cwd"] = cwd
        return umbrella

    def status_porcelain(*, cwd):
        calls["status_cwd"] = cwd
        return list(status_lines or [])

    return (
        Boundaries(
            repo_root=lambda: str(parent),
            resolve_repo=lambda root: repo_from_slug(org_repo),
            remote_url=lambda *, cwd: "git@example:" + org_repo,
            remote_branch_exists=remote_branch_exists,
            create_tree=create_tree,
            create_readonly_tree=create_readonly_tree,
            pr_for_head=pr_for_head,
            status_porcelain=status_porcelain,
            runner=runner,
        ),
        calls,
    )


# --- the happy path: epic write shape ----------------------------------------


def test_write_spawn_returns_the_typed_result(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale")
    b, calls = bounds(tmp_path)

    result = spawn_subagent(spec(), b)

    # The Tree was created via the reused path with the EPIC shape (#176): the
    # slash-namespaced E/WSnn branch cut from the epic-grouped umbrella base
    # (origin/E/umbrella), NOT origin/main — so the draft PR targets the epic
    # branch.
    tree_spec = calls["spec"]
    assert tree_spec.repo == repo_from_slug("acme/widget")
    assert tree_spec.epic == "TRE03" and tree_spec.ws == 1
    assert tree_spec.issue is None and tree_spec.branch is None
    assert calls["source_repo"] == str(tmp_path / "repo")
    # The launch contract: cwd IS the Tree, the role rides --agent, the key is gone.
    assert calls["cwd"] == str(tmp_path / "tree")
    assert calls["cmd"][calls["cmd"].index("--agent") + 1] == "implementer"
    assert "ANTHROPIC_API_KEY" not in calls["env"]
    # #404: an implementer WRITE Run is legitimately unbounded (ADR-0019 §6) — the
    # review-path deadline must NOT leak onto the spawn seam. The launcher gets the
    # UNBOUNDED default (LAUNCH_TIMEOUT is None), so no bound can kill a long Run.
    assert calls["timeout"] is launch.LAUNCH_TIMEOUT
    assert launch.LAUNCH_TIMEOUT is None
    # The task tells the Run which issue to implement and the branch to PR from.
    task = calls["cmd"][calls["cmd"].index("-p") + 1]
    assert "#156" in task and "TRE03/WS01" in task
    # The typed result IS the SPAWNED payload — coordinates + Run↔PR linkage.
    assert result.to_dict() == {
        "tree": str(tmp_path / "tree"),
        "branch": "TRE03/WS01",
        "base": "origin/TRE03/umbrella",
        "role": "implementer",
        "backend": "claude",
        "pr": 321,
        "pr_state": "OPEN",
        "pr_is_draft": True,
    }


def test_write_spawn_links_pr_from_the_tree_branch(tmp_path):
    # Acceptance #156: the Run↔PR link is resolved from the *Tree's* branch, read
    # inside the Tree (cwd) — the PR on the branch IS the link, no side database.
    b, calls = bounds(tmp_path)

    result = spawn_subagent(spec(ws=2, issue=99), b)

    assert calls["cwd"] == str(tmp_path / "tree")  # the Run is rooted in the Tree
    assert calls["pr_branch"] == "TRE03/WS02"  # link resolved from the Tree branch
    assert calls["pr_cwd"] == str(tmp_path / "tree")  # ...read from inside the Tree
    assert result.pr == 321


def test_write_spawn_checks_the_epic_umbrella_on_the_remote(tmp_path):
    # #176: --epic E --ws N resolves the epic-grouped base; the umbrella branch's
    # existence is checked against the remote (E/umbrella), read from the source repo.
    b, calls = bounds(tmp_path, pr=replace(_PR, base_ref="TRE04/umbrella"))

    result = spawn_subagent(spec(epic="TRE04", ws=7, issue=200), b)

    assert calls["umbrella_branch"] == "TRE04/umbrella"
    assert calls["umbrella_cwd"] == str(tmp_path / "repo")
    assert calls["spec"].epic == "TRE04" and calls["spec"].ws == 7
    assert layout.plan(calls["spec"]).base == "origin/TRE04/umbrella"
    assert result.base == "origin/TRE04/umbrella"


# --- fail-closed: the umbrella / Tree gates -----------------------------------


def test_missing_epic_branch_fails_closed_no_main_fallback(tmp_path):
    # #176 fail-closed: --epic E with NO origin/E/umbrella on the remote refuses
    # LOUD and NEVER silently falls back to origin/main. The Tree is never created
    # and nothing is launched — the precondition gates before any side effect.
    b, calls = bounds(tmp_path, umbrella=False)

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(epic="TRE04"), b)

    assert "TRE04/umbrella" in str(exc.value)
    assert "does not exist" in str(exc.value)
    assert "origin/main" in str(exc.value)  # the diagnostic names the refused fallback
    assert "spec" not in calls  # no Tree created
    assert "cmd" not in calls  # nothing launched


@pytest.mark.parametrize("bad_epic", ["", "   ", "TRE/04", "..", "TRE 04"])
def test_invalid_epic_is_a_clean_refusal(tmp_path, bad_epic):
    # An invalid/empty epic code is not a single alphanumeric token, so the pure
    # `epic_umbrella_base` helper raises ValueError; the pipeline surfaces it as
    # the clean domain refusal — never an escaping ValueError, and no side effect.
    b, calls = bounds(tmp_path)

    with pytest.raises(SpawnError, match="epic code"):
        spawn_subagent(spec(epic=bad_epic), b)

    assert "spec" not in calls and "cmd" not in calls


@pytest.mark.parametrize("bad_epic", ["", "   ", "TRE/04", "..", "TRE 04"])
def test_reviewer_invalid_epic_is_a_clean_refusal(tmp_path, bad_epic):
    # Fail-closed CONSISTENCY: the reviewer (read) epic/ws shape validates the epic
    # code the SAME way the write path does — via `work_stream_branch` — so an
    # empty/invalid epic refuses loud instead of silently building a "/WS03" head.
    b, calls = bounds(tmp_path)

    with pytest.raises(SpawnError, match="epic code"):
        spawn_subagent(spec(role="reviewer", epic=bad_epic, ws=3, issue=None), b)

    assert "plan" not in calls and "cmd" not in calls


@pytest.mark.parametrize(
    "exc",
    [
        execrun.ExecError(["pixi", "install"], rc=1, stderr="boom"),  # provisioning
        OSError("disk full"),  # a filesystem step failed
        ValueError("planner rejected the spec"),  # the planner refused
        FileExistsError("tree dir already exists"),
    ],
)
def test_tree_creation_failure_fails_closed(tmp_path, exc):
    # Fail-closed (ADR-0017/0019): a Tree-creation error fails the spawn loud, and
    # NEVER falls back to launching anything — the runner must not be called.
    b, calls = bounds(tmp_path)

    def boom(tree_spec, *, source_repo, github_url):
        raise exc

    with pytest.raises(SpawnError, match="tree creation failed"):
        spawn_subagent(spec(), replace(b, create_tree=boom))

    assert "cmd" not in calls  # no fallback launch


def test_write_shape_refuses_a_pinless_base(tmp_path):
    # ADR-0033's surviving guard, through the spawn write shape: a base with no
    # .shipit.toml [shipit].version pin fails Tree provisioning closed (the pin
    # gate's ValueError), and the spawn refuses LOUD — the refusal carries the
    # bootstrap diagnostic, and no Run is ever launched against the parent
    # checkout or a half-provisioned Tree.
    b, calls = bounds(tmp_path)

    def pinless(tree_spec, *, source_repo, github_url):
        # Exactly what shipit.tree.create._provision raises on a pinless base.
        raise ValueError(
            "repo /trees/leaf has no [shipit].version pin — run the bootstrap "
            "`shipit install --pr` first (ADR-0033: a Tree rides its base's "
            "pinned shipit; a pinless base has nothing for bin/shipit to exec)"
        )

    with pytest.raises(
        SpawnError, match="no \\[shipit\\].version pin — run the bootstrap"
    ):
        spawn_subagent(spec(), replace(b, create_tree=pinless))

    assert "cmd" not in calls  # fail-closed: nothing launched


# --- the shape gate (stage 1) --------------------------------------------------


def test_unsupported_backend_is_refused_before_any_io(tmp_path):
    # The backend gate fires before any repo resolution or Tree creation — and
    # guards the programmatic entry (the CLI's click.Choice is only the parse gate).
    def untouchable():
        raise AssertionError("the backend gate must fire before any I/O")

    b, calls = bounds(tmp_path)
    with pytest.raises(SpawnError, match="unsupported backend"):
        spawn_subagent(spec(backend="nonexistent"), replace(b, repo_root=untouchable))
    assert not calls


def test_non_positive_ws_is_refused(tmp_path):
    b, _ = bounds(tmp_path)
    with pytest.raises(SpawnError, match="--ws must be a positive integer"):
        spawn_subagent(spec(ws=0), b)


@pytest.mark.parametrize("bad_issue", [0, -1, None])
def test_write_run_requires_a_positive_issue(tmp_path, bad_issue):
    # --issue feeds the task prompt and the PR's `for #<issue>` link; a
    # zero/negative value — OR a MISSING one for a write role — refuses before any
    # Tree/child work. The CLI keeps --issue optional (a reviewer spawn carries
    # none), so this write-run requirement lives here, not at the click boundary.
    b, calls = bounds(tmp_path)
    with pytest.raises(SpawnError, match="--issue must be a positive integer"):
        spawn_subagent(spec(issue=bad_issue), b)
    assert (
        "spec" not in calls and "cmd" not in calls
    )  # nothing created, nothing launched


def test_epic_without_ws_is_refused(tmp_path):
    b, _ = bounds(tmp_path)
    with pytest.raises(SpawnError, match="both --epic and --ws"):
        spawn_subagent(spec(ws=None), b)


def test_ws_without_epic_is_refused(tmp_path):
    b, _ = bounds(tmp_path)
    with pytest.raises(SpawnError, match="both --epic and --ws"):
        spawn_subagent(spec(epic=None), b)


def test_reviewer_without_any_shape_is_refused(tmp_path):
    # A reviewer with neither an epic shape nor an issue has no branch to review —
    # a clean refusal naming the ACTUAL problem, not a `None/WS…` branch.
    b, _ = bounds(tmp_path)
    with pytest.raises(SpawnError, match="needs a branch to review"):
        spawn_subagent(spec(role="reviewer", epic=None, ws=None, issue=None), b)


# --- identity (stage 2) ----------------------------------------------------------


def test_repo_mismatch_is_refused(tmp_path):
    b, _ = bounds(tmp_path, org_repo="acme/widget")
    with pytest.raises(SpawnError, match="--repo 'gadget'"):
        spawn_subagent(spec(repo="gadget"), b)


def test_repo_accepts_the_org_qualified_slug(tmp_path):
    # --repo may be given as either the bare name or the full org/repo slug.
    b, _ = bounds(tmp_path)
    result = spawn_subagent(spec(repo="acme/widget"), b)
    assert result.pr == 321


def test_unparseable_origin_is_refused(tmp_path):
    # An origin remote with no owner/name tail cannot yield a Repo identity; the
    # canonical resolver refuses it loud (ValueError) and the pipeline surfaces a
    # clean refusal — a bogus identity never reaches the TreeSpec.
    b, _ = bounds(tmp_path)

    def unparseable(root):
        raise ValueError("cannot parse owner/name from origin URL 'widget'")

    with pytest.raises(SpawnError, match="cannot parse owner/name"):
        spawn_subagent(spec(), replace(b, resolve_repo=unparseable))


def test_outside_a_checkout_is_refused(tmp_path):
    b, _ = bounds(tmp_path)
    with pytest.raises(SpawnError, match="not inside a git checkout"):
        spawn_subagent(spec(), replace(b, repo_root=lambda: None))


def test_a_git_error_is_a_clean_refusal(tmp_path):
    b, _ = bounds(tmp_path)

    def boom(*, cwd):
        raise ExecError(["git"], rc=1, stderr="could not read origin remote")

    with pytest.raises(SpawnError):
        spawn_subagent(spec(), replace(b, remote_url=boom))


# --- launch (stage 5) -------------------------------------------------------------


def test_child_nonzero_exit_is_refused_with_its_stderr(tmp_path):
    b, _ = bounds(tmp_path, returncode=2)
    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), b)
    # The child's stderr is surfaced in the refusal, not swallowed.
    assert "claude child exited 2" in str(exc.value)
    assert "boom" in str(exc.value)


def test_launch_transport_failure_is_a_clean_refusal(tmp_path):
    # The child never starts — the backend binary is missing, so the runner raises
    # ExecError (the Exec runner normalizes the raw FileNotFoundError, ADR-0028).
    b, _ = bounds(tmp_path)

    def no_binary(cmd, *, cwd, env, timeout=None):
        raise execrun.ExecError(["claude"], rc=None, cause=execrun.CAUSE_MISSING_BINARY)

    with pytest.raises(SpawnError, match="claude"):
        spawn_subagent(spec(), replace(b, runner=no_binary))


# --- the post-condition audit (stage 6) --------------------------------------------


def test_no_pr_on_the_branch_is_refused(tmp_path):
    # A child that exits 0 but opened NO PR on the Tree's branch did not report
    # back (acceptance #156): the Run↔PR link is absent, so the spawn refuses.
    b, _ = bounds(tmp_path, pr=None)
    with pytest.raises(SpawnError, match="opened no PR"):
        spawn_subagent(spec(), b)


def test_unknown_pr_state_is_refused(tmp_path):
    # An UNDETERMINED PR state (gh unreadable) must NOT masquerade as success.
    b, _ = bounds(tmp_path, pr=gh.UNKNOWN)
    with pytest.raises(SpawnError, match="could not be read"):
        spawn_subagent(spec(), b)


def test_audit_handshake_is_the_pure_stage():
    # The audit is drivable as a plain function over the resolved PR snapshot —
    # each invalid lifecycle state is its own precise refusal.
    ok = audit_handshake(_PR, branch="TRE03/WS01", base_branch="TRE03/umbrella")
    assert ok is _PR

    with pytest.raises(SpawnError, match="is CLOSED, not OPEN"):
        audit_handshake(
            replace(_PR, state="CLOSED"),
            branch="TRE03/WS01",
            base_branch="TRE03/umbrella",
        )
    with pytest.raises(SpawnError, match="is not a draft"):
        audit_handshake(
            replace(_PR, is_draft=False),
            branch="TRE03/WS01",
            base_branch="TRE03/umbrella",
        )
    with pytest.raises(SpawnError) as exc:
        audit_handshake(
            replace(_PR, base_ref="main"),
            branch="TRE03/WS01",
            base_branch="TRE03/umbrella",
        )
    assert "targets base 'main'" in str(exc.value)
    assert "not the intended 'TRE03/umbrella'" in str(exc.value)


@pytest.mark.parametrize(
    "bad_pr, detail",
    [
        (replace(_PR, state="MERGED"), "is MERGED, not OPEN"),
        (replace(_PR, is_draft=False), "is not a draft"),
        (replace(_PR, base_ref="main"), "targets base 'main'"),
    ],
)
def test_invalid_handshake_states_refuse_through_the_pipeline(tmp_path, bad_pr, detail):
    b, _ = bounds(tmp_path, pr=bad_pr)
    with pytest.raises(SpawnError, match=detail.replace("'", "'")):
        spawn_subagent(spec(), b)


# --- the salvage signal (#587) ------------------------------------------------------
# A write Run killed mid-work (wall-clock hit while verifying) can strand its whole
# diagnosis UNCOMMITTED in the dead Tree; the write tail's post-launch refusals must
# carry the uncommitted-work count so the coordinator inspects the Tree instead of
# discarding a resumable handoff as a total loss.


def test_no_pr_refusal_reports_uncommitted_work(tmp_path):
    # The observed #587 shape: the child exits 0 without committing or opening a PR.
    # The refusal keeps its original diagnosis AND appends the salvage line — the
    # porcelain count, read from inside the dead Tree.
    b, calls = bounds(
        tmp_path, pr=None, status_lines=[" M src/fix.py", "?? tests/t.py"]
    )

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), b)

    assert "opened no PR" in str(exc.value)  # the original refusal survives intact
    assert "2 uncommitted change(s)" in str(exc.value)
    assert "salvageable" in str(exc.value)
    assert str(tmp_path / "tree") in str(exc.value)  # the note names the Tree to read
    assert calls["status_cwd"] == str(tmp_path / "tree")  # probed IN the dead Tree


def test_nonzero_child_refusal_reports_uncommitted_work(tmp_path):
    # The other post-launch failure class: a child killed nonzero mid-work also
    # leaves a Tree worth inspecting, so the same salvage line rides that refusal.
    b, _ = bounds(tmp_path, returncode=2, status_lines=[" M src/fix.py"])

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), b)

    assert "claude child exited 2" in str(exc.value)
    assert "1 uncommitted change(s)" in str(exc.value)


def test_clean_tree_refusal_carries_no_salvage_line(tmp_path):
    # Nothing to salvage → nothing appended: the refusal is byte-identical to the
    # bare audit refusal, so a clean failure never nags the coordinator to dig.
    b, calls = bounds(tmp_path, pr=None)

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), b)

    assert "opened no PR" in str(exc.value)
    assert "salvageable" not in str(exc.value)
    assert "uncommitted" not in str(exc.value)
    assert calls["status_cwd"] == str(tmp_path / "tree")  # probed, found clean


def test_salvage_probe_failure_never_masks_the_refusal(tmp_path):
    # The probe runs UNDER an already-failing spawn: an unreadable Tree (ExecError)
    # must surface the ORIGINAL refusal untouched — best-effort, never fatal.
    b, _ = bounds(tmp_path, pr=None)

    def unreadable(*, cwd):
        raise ExecError(["git", "status"], rc=128, stderr="not a git repository")

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), replace(b, status_porcelain=unreadable))

    assert "opened no PR" in str(exc.value)
    assert "not a git repository" not in str(exc.value)


def test_tree_creation_failure_does_not_probe_salvage(tmp_path):
    # Fail-closed BEFORE the child ran: there is no Run work to salvage (the Tree
    # may not even exist), so the pre-launch refusals never touch the probe.
    b, calls = bounds(tmp_path)

    def no_probe(*, cwd):
        raise AssertionError("a pre-launch refusal must not run the salvage probe")

    def boom(tree_spec, *, source_repo, github_url):
        raise OSError("disk full")

    with pytest.raises(SpawnError, match="tree creation failed"):
        spawn_subagent(spec(), replace(b, create_tree=boom, status_porcelain=no_probe))
    assert "cmd" not in calls


def test_reviewer_failure_does_not_probe_salvage(tmp_path):
    # A reviewer Run writes nothing (chmod'd read-only Tree) — its failures carry
    # no salvage note and never probe the shared Tree's status.
    b, _ = bounds(tmp_path, returncode=3)

    def no_probe(*, cwd):
        raise AssertionError("the reviewer tail must not run the salvage probe")

    with pytest.raises(SpawnError, match="claude child exited 3") as exc:
        spawn_subagent(
            spec(role="reviewer", ws=3, issue=None),
            replace(b, status_porcelain=no_probe),
        )
    assert "salvageable" not in str(exc.value)


# --- the standalone-issue shape (ADR-0026) -----------------------------------------


def test_issue_only_builds_the_issue_shape_spec(tmp_path):
    # --issue with NO --epic/--ws builds the standalone issue shape: branch
    # issues/<id>/<session> (default work), base origin/main, so the draft PR
    # targets main. The write tail launches + links its PR exactly like the epic
    # shape.
    b, calls = bounds(tmp_path, pr=replace(_PR, number=77, base_ref="main"))

    result = spawn_subagent(spec(epic=None, ws=None, issue=210), b)

    tree_spec = calls["spec"]
    assert tree_spec.issue == 210 and tree_spec.session == "work"
    assert tree_spec.epic is None and tree_spec.ws is None and tree_spec.branch is None
    # The task names the issue and the standalone-issue branch to PR from.
    task = calls["cmd"][calls["cmd"].index("-p") + 1]
    assert "#210" in task and "issues/210/work" in task
    assert result.branch == "issues/210/work"
    assert result.base == "origin/main"
    assert result.pr == 77


def test_issue_only_uses_a_non_default_session(tmp_path):
    # --session rides the standalone-issue branch: issues/<id>/<session>.
    b, calls = bounds(tmp_path, pr=replace(_PR, number=5, base_ref="main"))

    spawn_subagent(spec(epic=None, ws=None, issue=210, session="onboard"), b)

    assert calls["spec"].session == "onboard"
    assert calls["pr_branch"] == "issues/210/onboard"  # PR linked from the branch


def test_issue_only_does_not_probe_an_epic_umbrella(tmp_path):
    # The standalone-issue path cuts from origin/main, so it must NOT run the epic
    # umbrella remote pre-check (that guard belongs to the epic shape only).
    b, calls = bounds(tmp_path, pr=replace(_PR, base_ref="main"))

    def no_probe(branch, *, cwd=None, remote="origin"):
        raise AssertionError("issue shape must not probe an epic umbrella")

    spawn_subagent(
        spec(epic=None, ws=None, issue=210),
        replace(b, remote_branch_exists=no_probe),
    )
    assert "umbrella_branch" not in calls


@pytest.mark.parametrize("bad_session", ["", "   ", "///"])
def test_issue_only_empty_session_is_refused(tmp_path, bad_session):
    # A --session that sanitizes to nothing would build a bare `issues/<id>/` ref;
    # it is refused BEFORE any Tree side effect.
    b, calls = bounds(tmp_path)
    with pytest.raises(SpawnError, match="session"):
        spawn_subagent(spec(epic=None, ws=None, issue=210, session=bad_session), b)
    assert "spec" not in calls  # no Tree created


# --- the reviewer path (ADR-0018) ---------------------------------------------------


def test_reviewer_gets_the_shared_readonly_tree_and_posture(tmp_path, monkeypatch):
    # Acceptance #157: role reviewer takes the read-only path (create_readonly, NOT
    # the write create), launches with --agent reviewer + the read-only --tools
    # allow-list, and returns the SPAWNED coordinates with NO Run↔PR linkage — the
    # review lands in the EXISTING PR.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale")
    b, calls = bounds(tmp_path)

    result = spawn_subagent(spec(role="reviewer", ws=3, issue=None), b)

    # The read-only plan is shared per (repo, branch): the WS PR head, no agent hash.
    plan = calls["plan"]
    assert plan.branch == "TRE03/WS03"
    assert plan.dir.name.startswith("tre03-ws03-")
    assert plan.dir.parent.name == "review"
    assert calls["source_repo"] == str(tmp_path / "repo")
    assert "spec" not in calls  # the write creator was never touched
    # Launch contract for a reviewer: cwd = the read-only Tree, --agent reviewer,
    # the read-only --tools allow-list (no Write), key scrubbed.
    assert calls["cwd"] == str(tmp_path / "review")
    assert calls["cmd"][calls["cmd"].index("--agent") + 1] == "reviewer"
    allowlist = calls["cmd"][calls["cmd"].index("--tools") + 1]
    assert "Write" not in allowlist and "Edit" not in allowlist
    assert "ANTHROPIC_API_KEY" not in calls["env"]
    # The typed result: role reviewer, no PR block at all.
    assert result.to_dict() == {
        "tree": str(tmp_path / "review"),
        "branch": "TRE03/WS03",
        "base": "origin/TRE03/WS03",
        "role": "reviewer",
        "backend": "claude",
    }


def test_issue_only_reviewer_pins_the_issue_head(tmp_path):
    # A reviewer follows the same shapes: --issue with no epic pins the
    # standalone-issue head issues/<id>/<session> for the shared read-only Tree.
    b, calls = bounds(tmp_path)

    result = spawn_subagent(spec(role="reviewer", epic=None, ws=None, issue=210), b)

    assert calls["plan"].branch == "issues/210/work"
    assert result.role == "reviewer"
    assert result.branch == "issues/210/work"


def test_codex_reviewer_launches_with_the_read_only_posture(tmp_path, monkeypatch):
    # #185: a non-Claude reviewer takes the SAME shared read-only Tree path and
    # launches with the codex reviewer posture — the network-capable
    # workspace-write sandbox, NOT the write bypass. The chmod'd Tree is the FS
    # guard.
    monkeypatch.setenv("OPENAI_API_KEY", "stale")
    b, calls = bounds(tmp_path)

    result = spawn_subagent(spec(role="reviewer", ws=3, issue=None, backend="codex"), b)

    cmd = calls["cmd"]
    assert cmd[:2] == ["codex", "exec"]
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "--tools" not in cmd
    assert calls["cwd"] == str(tmp_path / "review")
    assert "OPENAI_API_KEY" not in calls["env"]
    assert result.role == "reviewer" and result.backend == "codex"


def test_antigravity_reviewer_drops_skip_permissions(tmp_path, monkeypatch):
    # The agy reviewer path: read_only=True drops --dangerously-skip-permissions
    # and is rooted in the read-only Tree via --add-dir <Tree> (agy ignores the
    # process cwd).
    monkeypatch.setenv("GEMINI_API_KEY", "stale")
    b, calls = bounds(tmp_path)

    spawn_subagent(spec(role="reviewer", ws=3, issue=None, backend="antigravity"), b)

    cmd = calls["cmd"]
    assert cmd[0] == "agy"
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path / "review")
    assert "GEMINI_API_KEY" not in calls["env"]


def test_reviewer_readonly_tree_failure_fails_closed(tmp_path):
    # Fail-closed for the reviewer path too: a read-only-Tree error refuses loud,
    # and the launcher is never reached.
    b, calls = bounds(tmp_path)

    def boom(plan, *, source_repo, github_url):
        raise ExecError(["gh"], rc=1, stderr="clone failed")

    with pytest.raises(SpawnError, match="read-only tree creation failed"):
        spawn_subagent(
            spec(role="reviewer", ws=3, issue=None),
            replace(b, create_readonly_tree=boom),
        )
    assert "cmd" not in calls


def test_reviewer_child_nonzero_exit_is_refused(tmp_path):
    b, _ = bounds(tmp_path, returncode=3)
    with pytest.raises(SpawnError, match="claude child exited 3"):
        spawn_subagent(spec(role="reviewer", ws=3, issue=None), b)


# --- the spawn-seam identity binding + export (LOG04-WS02 / ADR-0032) ---------
# The spawn seam binds the worker's dev-cycle identity from its OWN arguments
# (`epic`/`ws`/`role`, plus the minted `agent` spawn id) and `env_export`
# threads every bound key into the Run's environment as SHIPIT_LOG_CTX_* — so
# every shipit command the worker runs correlates to its Work Stream with zero
# worker cooperation.


def test_epic_spawn_exports_all_four_dev_cycle_keys(tmp_path):
    b, calls = bounds(tmp_path)

    spawn_subagent(spec(), b)

    env = calls["env"]
    # The child env carries the whole identity (`ws` stringified — the child's
    # bind_from_env casts it back to int; the int-typed round-trip is pinned in
    # test_logcontext).
    assert env["SHIPIT_LOG_CTX_EPIC"] == "TRE03"
    assert env["SHIPIT_LOG_CTX_WS"] == "1"
    assert env["SHIPIT_LOG_CTX_ROLE"] == "implementer"
    # The agent spawn id IS the Tree dir's disambiguating hash, so the log key
    # and the Tree leaf name agree.
    assert env["SHIPIT_LOG_CTX_AGENT"] == calls["spec"].agent_hash
    # The Tree identity rides the seam too (LOG01-WS03).
    assert env["SHIPIT_LOG_CTX_TREE"] == str(tmp_path / "tree")
    # And the parent's own records carry the same identity from the seam on.
    bound = logcontext.bound()
    assert bound["epic"] == "TRE03" and bound["ws"] == 1
    assert bound["role"] == "implementer"
    assert bound["tree"] == str(tmp_path / "tree")
    assert bound["agent"] == calls["spec"].agent_hash


def test_issue_spawn_exports_no_epic_ws_keys(tmp_path):
    # A standalone-issue spawn has no epic/ws: the keys stay ABSENT from the
    # child env (present-when-bound crosses the seam) — role/agent still ride.
    b, calls = bounds(tmp_path, pr=replace(_PR, number=77, base_ref="main"))

    spawn_subagent(spec(epic=None, ws=None, issue=210), b)

    env = calls["env"]
    assert "SHIPIT_LOG_CTX_EPIC" not in env
    assert "SHIPIT_LOG_CTX_WS" not in env
    assert env["SHIPIT_LOG_CTX_ROLE"] == "implementer"
    assert env["SHIPIT_LOG_CTX_AGENT"] == calls["spec"].agent_hash


def test_issue_spawn_does_not_inherit_a_prior_spawns_epic_identity(tmp_path):
    # The pipeline OWNS the spawn-identity keys at entry: `bind` drops `None`
    # halves, so without the entry unbind a standalone-issue spawn in a process
    # that already carries epic/ws (a prior spawn here, or a nested spawn's
    # inherited SHIPIT_LOG_CTX_* rebound at logging setup) would export the OLD
    # workstream's identity into its child. The stale keys must not cross.
    b_epic, _ = bounds(tmp_path)
    spawn_subagent(spec(), b_epic)  # leaves epic/ws/role/agent/tree bound

    b_issue, calls = bounds(tmp_path, pr=replace(_PR, number=77, base_ref="main"))
    spawn_subagent(spec(epic=None, ws=None, issue=210), b_issue)

    env = calls["env"]
    assert "SHIPIT_LOG_CTX_EPIC" not in env
    assert "SHIPIT_LOG_CTX_WS" not in env
    assert env["SHIPIT_LOG_CTX_AGENT"] == calls["spec"].agent_hash  # THIS spawn's
    bound = logcontext.bound()
    assert "epic" not in bound and "ws" not in bound


def test_reviewer_spawn_exports_identity_with_a_minted_agent_id(tmp_path):
    # The reviewer's Tree is SHARED per (repo, branch) — no per-Run hash of its
    # own — so the seam mints a fresh agent id for the Run's identity.
    b, calls = bounds(tmp_path)

    spawn_subagent(spec(role="reviewer", ws=3, issue=None), b)

    env = calls["env"]
    assert env["SHIPIT_LOG_CTX_EPIC"] == "TRE03"
    assert env["SHIPIT_LOG_CTX_WS"] == "3"
    assert env["SHIPIT_LOG_CTX_ROLE"] == "reviewer"
    assert env["SHIPIT_LOG_CTX_AGENT"]  # minted per Run, non-empty hex
