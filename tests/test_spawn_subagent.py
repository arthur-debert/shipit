from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from shipit import events, execrun, gh, logcontext
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.review import producer
from shipit.review.diff import review_view
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
_ATTACHED_PR = gh.PrAttachment(
    number=321,
    state="OPEN",
    is_draft=True,
    base_ref="TRE03/umbrella",
    head_ref="TRE03/WS01",
    is_cross_repository=False,
    maintainer_can_modify=False,
)


def spec(**overrides) -> SubagentSpec:
    fields = dict(repo="widget", role="implementer", epic="TRE03", ws=1, issue=156)
    fields.update(overrides)
    return SubagentSpec(**fields)


def bounds(
    tmp_path: Path,
    *,
    pr=_PR,
    attached_pr=_ATTACHED_PR,
    returncode: int = 0,
    umbrella: bool = True,
    org_repo: str = "acme/widget",
    status_lines: list[str] | None = None,
    stdout: str = "{}",
    stderr: str = "boom",
) -> tuple[Boundaries, dict]:
    calls: dict = {}
    parent = tmp_path / "repo"
    parent.mkdir(exist_ok=True)
    tree_dir = tmp_path / "tree"

    def create_tree(tree_spec, *, source_repo, github_url):
        calls["spec"] = tree_spec
        calls["source_repo"] = source_repo
        calls["github_url"] = github_url
        tree_dir.mkdir(parents=True, exist_ok=True)
        tp = layout.plan(tree_spec)
        return Tree(path=str(tree_dir), branch=tp.branch, base=tp.base)

    def runner(cmd, *, cwd, env, timeout=None):
        calls["cmd"] = cmd
        calls["cwd"] = cwd
        calls["env"] = env
        calls["timeout"] = timeout
        return launch.LaunchResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def pr_for_head(branch, *, cwd=None):
        calls["pr_branch"] = branch
        calls["pr_cwd"] = cwd
        return pr

    def pr_for_number(number, *, repo=None):
        calls["pr_number"] = number
        calls["pr_repo"] = repo
        return attached_pr

    def remote_branch_exists(branch, *, cwd=None, remote="origin"):
        calls["umbrella_branch"] = branch
        calls["umbrella_cwd"] = cwd
        return umbrella

    def status_porcelain(*, cwd):
        calls["status_cwd"] = cwd
        return list(status_lines or [])

    def run_review(backend, target, *, run_id, review_tree_naming=None):
        calls["review_backend"] = backend
        calls["review_target"] = target
        calls["review_run_id"] = run_id
        calls["review_tree_naming"] = review_tree_naming
        return {"review": {}, "post": {}}

    return (
        Boundaries(
            repo_root=lambda: str(parent),
            resolve_repo=lambda root: repo_from_slug(org_repo),
            remote_url=lambda *, cwd: "git@example:" + org_repo,
            remote_branch_exists=remote_branch_exists,
            create_tree=create_tree,
            pr_for_head=pr_for_head,
            pr_for_number=pr_for_number,
            status_porcelain=status_porcelain,
            runner=runner,
            run_review=run_review,
        ),
        calls,
    )


def test_write_spawn_returns_the_typed_result(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale")
    b, calls = bounds(tmp_path)

    result = spawn_subagent(spec(), b)

    tree_spec = calls["spec"]
    assert tree_spec.repo == repo_from_slug("acme/widget")
    assert tree_spec.epic == "TRE03" and tree_spec.ws == 1
    assert tree_spec.issue is None and tree_spec.branch is None
    assert calls["source_repo"] == str(tmp_path / "repo")
    assert calls["cwd"] == str(tmp_path / "tree")
    assert calls["cmd"][calls["cmd"].index("--agent") + 1] == "implementer"
    assert "ANTHROPIC_API_KEY" not in calls["env"]
    assert calls["timeout"] is launch.LAUNCH_TIMEOUT
    assert launch.LAUNCH_TIMEOUT is None
    task = calls["cmd"][calls["cmd"].index("-p") + 1]
    assert "#156" in task and "TRE03/WS01" in task
    assert "for #156" in task and "closes #156" not in task
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


def test_write_spawn_emits_bounded_phase_events(tmp_path, caplog):
    b, _calls = bounds(tmp_path)

    with caplog.at_level(logging.INFO, logger="shipit.spawn"):
        spawn_subagent(spec(), b)

    phases = [
        rec.phase
        for rec in caplog.records
        if getattr(rec, events.EXTRA_KEY, None) == "agent.phase"
    ]
    assert phases == ["tree_provisioning", "agent_running", "pr_audit"]


def test_write_spawn_links_pr_from_the_tree_branch(tmp_path):
    b, calls = bounds(tmp_path)

    result = spawn_subagent(spec(ws=2, issue=99), b)

    assert calls["cwd"] == str(tmp_path / "tree")
    assert calls["pr_branch"] == "TRE03/WS02"
    assert calls["pr_cwd"] == str(tmp_path / "tree")
    assert result.pr == 321


def shepherd_spec(**overrides) -> SubagentSpec:
    fields = dict(repo="widget", role="shepherd", pr=321)
    fields.update(overrides)
    return SubagentSpec(**fields)


def test_shepherd_spawn_attaches_to_existing_pr_head_without_new_pr(tmp_path):
    attached = gh.PrAttachment(
        number=321,
        state="OPEN",
        is_draft=True,
        base_ref="TRE03/umbrella",
        head_ref="TRE03/WS04",
        is_cross_repository=False,
        maintainer_can_modify=False,
    )
    b, calls = bounds(
        tmp_path,
        attached_pr=attached,
        pr=gh.HeadPr(
            number=321,
            state="OPEN",
            is_draft=True,
            base_ref="TRE03/umbrella",
        ),
    )

    result = spawn_subagent(shepherd_spec(), b)

    assert calls["pr_number"] == 321
    assert calls["pr_repo"] == "acme/widget"
    tree_spec = calls["spec"]
    assert tree_spec.branch == "TRE03/WS04"
    assert tree_spec.base == "origin/TRE03/WS04"
    assert tree_spec.agent == "claude"
    assert tree_spec.tree_id and "-" in tree_spec.tree_id
    assert tree_spec.issue is None and tree_spec.epic is None and tree_spec.ws is None
    assert calls["pr_branch"] == "TRE03/WS04"
    assert calls["pr_cwd"] == str(tmp_path / "tree")
    assert calls["cmd"][calls["cmd"].index("--agent") + 1] == "shepherd"
    task = calls["cmd"][calls["cmd"].index("-p") + 1]
    assert "pull request #321" in task
    assert "git push origin HEAD:refs/heads/TRE03/WS04" in task
    assert "gh pr create" not in task
    assert "shipit pr next" in task and "do NOT run `shipit pr next`" in task
    assert result.to_dict() == {
        "tree": str(tmp_path / "tree"),
        "branch": "TRE03/WS04",
        "base": "origin/TRE03/WS04",
        "role": "shepherd",
        "backend": "claude",
        "pr": 321,
        "pr_state": "OPEN",
        "pr_is_draft": True,
    }


def test_shepherd_round_mints_a_fresh_per_run_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIPIT_TREES_ROOT", str(tmp_path / "trees"))
    attached = gh.PrAttachment(
        number=321,
        state="OPEN",
        is_draft=True,
        base_ref="TRE03/umbrella",
        head_ref="TRE03/WS04",
        is_cross_repository=False,
        maintainer_can_modify=False,
    )

    def run_round():
        b, calls = bounds(
            tmp_path,
            attached_pr=attached,
            pr=gh.HeadPr(
                number=321,
                state="OPEN",
                is_draft=True,
                base_ref="TRE03/umbrella",
            ),
        )
        result = spawn_subagent(shepherd_spec(), b)
        return calls, result

    first_calls, first = run_round()
    second_calls, _second = run_round()

    assert first_calls["spec"].branch == "TRE03/WS04"
    assert first_calls["spec"].base == "origin/TRE03/WS04"
    assert first.branch == "TRE03/WS04"
    first_id = first_calls["spec"].tree_id
    second_id = second_calls["spec"].tree_id
    assert first_id != second_id
    assert "-" in first_id and "-" in second_id


def test_shepherd_wrong_head_pr_is_refused_before_launch(tmp_path):
    attached = gh.PrAttachment(
        number=321,
        state="OPEN",
        is_draft=True,
        base_ref="TRE03/umbrella",
        head_ref="TRE03/WS04",
        is_cross_repository=False,
        maintainer_can_modify=False,
    )
    b, calls = bounds(
        tmp_path,
        attached_pr=attached,
        pr=gh.HeadPr(
            number=654,
            state="OPEN",
            is_draft=True,
            base_ref="TRE03/umbrella",
        ),
    )

    with pytest.raises(SpawnError, match="not the requested PR #321"):
        spawn_subagent(shepherd_spec(), b)

    assert "cmd" not in calls


def test_shepherd_existing_pr_does_not_require_draft_handshake(tmp_path):
    attached = gh.PrAttachment(
        number=321,
        state="OPEN",
        is_draft=False,
        base_ref="TRE03/umbrella",
        head_ref="TRE03/WS04",
        is_cross_repository=False,
        maintainer_can_modify=False,
    )
    b, calls = bounds(
        tmp_path,
        attached_pr=attached,
        pr=gh.HeadPr(
            number=321,
            state="OPEN",
            is_draft=False,
            base_ref="TRE03/umbrella",
        ),
    )

    result = spawn_subagent(shepherd_spec(), b)

    assert calls["cmd"][calls["cmd"].index("--agent") + 1] == "shepherd"
    assert result.pr == 321
    assert result.pr_is_draft is False


@pytest.mark.parametrize("maintainer_can_modify", [False, True])
def test_shepherd_fork_pr_is_refused_before_tree(tmp_path, maintainer_can_modify):
    attached = gh.PrAttachment(
        number=321,
        state="OPEN",
        is_draft=True,
        base_ref="TRE03/umbrella",
        head_ref="contributor/branch",
        is_cross_repository=True,
        maintainer_can_modify=maintainer_can_modify,
    )
    b, calls = bounds(tmp_path, attached_pr=attached)

    with pytest.raises(SpawnError, match="fork-head fetching and pushing"):
        spawn_subagent(shepherd_spec(), b)

    assert "spec" not in calls and "cmd" not in calls


def test_provisioned_write_spawn_launches_through_its_work_env(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("PIXI_PROJECT_MANIFEST", "/parent/pixi.toml")
    b, calls = bounds(tmp_path)
    tree_dir = tmp_path / "tree"
    meta = tree_dir / ".pixi" / "envs" / "default" / "conda-meta"
    meta.mkdir(parents=True)
    (meta / "pixi").write_text(
        json.dumps(
            {
                "manifest_path": str(tree_dir / "pixi.toml"),
                "environment_name": "default",
                "pixi_version": "0.63.2",
                "environment_lock_file_hash": "99f00798db0ea80c",
                "resolved_platform": {"subdir": "osx-arm64", "virtual_packages": []},
            }
        )
    )

    with caplog.at_level(logging.INFO, logger="shipit.spawn"):
        spawn_subagent(spec(), b)

    assert calls["cmd"][:4] == [
        "pixi",
        "run",
        "--manifest-path",
        str(tree_dir / "pixi.toml"),
    ]
    child_argv = calls["cmd"][calls["cmd"].index("--") + 1 :]
    assert child_argv[child_argv.index("--agent") + 1] == "implementer"
    assert "PIXI_PROJECT_MANIFEST" not in calls["env"]
    record = next(
        r
        for r in caplog.records
        if getattr(r, "work_env_boundary", None) == "spawn.write-run"
    )
    assert record.routing == "pixi-run"
    assert record.checkout_strategy == "new-write-tree"
    assert record.pixi_environment_name == "default"
    assert record.pixi_environment_lock_hash == "99f00798db0ea80c"
    assert record.working_dir_repo == "acme/widget"
    assert record.tree_branch == "TRE03/WS01"
    assert record.tree_base == "origin/TRE03/umbrella"
    assert not hasattr(record, "pixi_run_id")


@pytest.mark.parametrize("invalid_identity", ["not json", "[]", "{}", "null"])
def test_provisioned_write_spawn_tolerates_invalid_optional_env_identity(
    tmp_path, caplog, invalid_identity
):
    b, calls = bounds(tmp_path)
    tree_dir = tmp_path / "tree"
    meta = tree_dir / ".pixi" / "envs" / "default" / "conda-meta"
    meta.mkdir(parents=True)
    (meta / "pixi").write_text(invalid_identity)

    with caplog.at_level(logging.INFO, logger="shipit.spawn"):
        spawn_subagent(spec(), b)

    assert calls["cmd"][:4] == [
        "pixi",
        "run",
        "--manifest-path",
        str(tree_dir / "pixi.toml"),
    ]
    warning = next(
        r for r in caplog.records if "pixi env identity unreadable" in r.message
    )
    assert warning.levelno == logging.WARNING
    record = next(
        r
        for r in caplog.records
        if getattr(r, "work_env_boundary", None) == "spawn.write-run"
    )
    assert record.routing == "pixi-run"
    assert not hasattr(record, "pixi_environment_name")


def test_non_pixi_write_spawn_uses_ambient_routing_and_launches_bare(tmp_path, caplog):
    b, calls = bounds(tmp_path)

    with caplog.at_level(logging.INFO, logger="shipit.spawn"):
        spawn_subagent(spec(), b)

    assert calls["cmd"][0] != "pixi"
    assert calls["cmd"][calls["cmd"].index("--agent") + 1] == "implementer"
    record = next(
        r
        for r in caplog.records
        if getattr(r, "work_env_boundary", None) == "spawn.write-run"
    )
    assert record.routing == "ambient"
    assert record.checkout_strategy == "new-write-tree"
    assert not hasattr(record, "pixi_environment_name")


def test_write_spawn_checks_the_epic_umbrella_on_the_remote(tmp_path):
    b, calls = bounds(tmp_path, pr=replace(_PR, base_ref="TRE04/umbrella"))

    result = spawn_subagent(spec(epic="TRE04", ws=7, issue=200), b)

    assert calls["umbrella_branch"] == "TRE04/umbrella"
    assert calls["umbrella_cwd"] == str(tmp_path / "repo")
    assert calls["spec"].epic == "TRE04" and calls["spec"].ws == 7
    assert layout.plan(calls["spec"]).base == "origin/TRE04/umbrella"
    assert result.base == "origin/TRE04/umbrella"


def test_missing_epic_branch_fails_closed_no_main_fallback(tmp_path):
    b, calls = bounds(tmp_path, umbrella=False)

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(epic="TRE04"), b)

    assert "TRE04/umbrella" in str(exc.value)
    assert "does not exist" in str(exc.value)
    assert "origin/main" in str(exc.value)
    assert "spec" not in calls
    assert "cmd" not in calls


@pytest.mark.parametrize("bad_epic", ["", "   ", "TRE/04", "..", "TRE 04"])
def test_invalid_epic_is_a_clean_refusal(tmp_path, bad_epic):
    b, calls = bounds(tmp_path)

    with pytest.raises(SpawnError, match="epic code"):
        spawn_subagent(spec(epic=bad_epic), b)

    assert "spec" not in calls and "cmd" not in calls


@pytest.mark.parametrize("bad_epic", ["", "   ", "TRE/04", "..", "TRE 04"])
def test_reviewer_invalid_epic_is_a_clean_refusal(tmp_path, bad_epic):
    b, calls = bounds(tmp_path)

    with pytest.raises(SpawnError, match="epic code"):
        spawn_subagent(
            spec(
                role="reviewer",
                epic=bad_epic,
                ws=3,
                issue=None,
                backend="codex",
            ),
            b,
        )

    assert "review_target" not in calls and "spec" not in calls


@pytest.mark.parametrize(
    "exc",
    [
        execrun.ExecError(["pixi", "install"], rc=1, stderr="boom"),
        OSError("disk full"),
        ValueError("planner rejected the spec"),
        FileExistsError("tree dir already exists"),
    ],
)
def test_tree_creation_failure_fails_closed(tmp_path, exc):
    b, calls = bounds(tmp_path)

    def boom(tree_spec, *, source_repo, github_url):
        raise exc

    with pytest.raises(SpawnError, match="tree creation failed"):
        spawn_subagent(spec(), replace(b, create_tree=boom))

    assert "cmd" not in calls


def test_write_shape_refuses_a_pinless_base(tmp_path):
    b, calls = bounds(tmp_path)

    def pinless(tree_spec, *, source_repo, github_url):
        raise ValueError(
            "repo /trees/leaf has no [shipit].version pin — run the bootstrap "
            "`shipit install --pr` first (ADR-0033: a Tree rides its base's "
            "pinned shipit; a pinless base has nothing for bin/shipit to exec)"
        )

    with pytest.raises(
        SpawnError, match="no \\[shipit\\].version pin — run the bootstrap"
    ):
        spawn_subagent(spec(), replace(b, create_tree=pinless))

    assert "cmd" not in calls


def test_unsupported_backend_is_refused_before_any_io(tmp_path):
    def untouchable():
        raise AssertionError("the backend gate must fire before any I/O")

    b, calls = bounds(tmp_path)
    with pytest.raises(SpawnError, match="unsupported backend"):
        spawn_subagent(spec(backend="nonexistent"), replace(b, repo_root=untouchable))
    assert not calls


def test_unknown_role_is_refused_before_any_io(tmp_path, caplog):
    def untouchable():
        raise AssertionError("the role preflight must fire before any I/O")

    caplog.set_level(logging.ERROR, logger="shipit.spawn")
    b, calls = bounds(tmp_path)
    with pytest.raises(SpawnError, match="unknown role 'wizard'") as exc:
        spawn_subagent(spec(role="wizard"), replace(b, repo_root=untouchable))
    assert "detached" in str(exc.value)
    assert not calls
    refusal = next(
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and getattr(record, "refusal_reason", None) == "role-profile-validation"
    )
    assert refusal.requested_role == "wizard"
    assert refusal.launch_context == "detached"
    assert "SHIPIT_" not in refusal.getMessage()


@pytest.mark.parametrize("role", ["explorer", "coordinator"])
def test_detached_spawn_of_non_detachable_roles_is_refused(tmp_path, role):
    b, calls = bounds(tmp_path)
    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(role=role), b)
    message = str(exc.value)
    assert role in message and "detached" in message
    assert not calls


def test_shepherd_requires_pr_attachment_before_any_tree(tmp_path):
    b, calls = bounds(tmp_path)

    with pytest.raises(SpawnError, match="existing-PR attachment role"):
        spawn_subagent(shepherd_spec(pr=None), replace(b, repo_root=lambda: None))

    assert not calls


def test_shepherd_refuses_issue_or_epic_shape(tmp_path):
    b, calls = bounds(tmp_path)

    with pytest.raises(SpawnError, match="--pr only"):
        spawn_subagent(shepherd_spec(issue=769), b)
    with pytest.raises(SpawnError, match="--pr only"):
        spawn_subagent(shepherd_spec(epic="TRE03", ws=4), b)

    assert "spec" not in calls and "cmd" not in calls


def test_pr_option_is_only_for_existing_pr_attachment_roles(tmp_path):
    b, calls = bounds(tmp_path)

    with pytest.raises(SpawnError, match="existing-PR attachment roles"):
        spawn_subagent(spec(pr=321), b)

    assert "spec" not in calls and "cmd" not in calls


def test_role_input_is_normalized_through_the_registry(tmp_path):
    b, calls = bounds(tmp_path)
    result = spawn_subagent(
        spec(role="  Reviewer ", ws=3, issue=None, backend="codex"), b
    )
    assert result.role == "reviewer"
    assert "review_target" in calls and "spec" not in calls

    b2, calls2 = bounds(tmp_path)
    result2 = spawn_subagent(spec(role="IMPLEMENTER"), b2)
    assert result2.role == "implementer"
    assert calls2["cmd"][calls2["cmd"].index("--agent") + 1] == "implementer"


def test_non_positive_ws_is_refused(tmp_path):
    b, _ = bounds(tmp_path)
    with pytest.raises(SpawnError, match="--ws must be a positive integer"):
        spawn_subagent(spec(ws=0), b)


@pytest.mark.parametrize("bad_issue", [0, -1, None])
def test_write_run_requires_a_positive_issue(tmp_path, bad_issue):
    b, calls = bounds(tmp_path)
    with pytest.raises(SpawnError, match="--issue must be a positive integer"):
        spawn_subagent(spec(issue=bad_issue), b)
    assert "spec" not in calls and "cmd" not in calls


def test_epic_without_ws_is_refused(tmp_path):
    b, _ = bounds(tmp_path)
    with pytest.raises(SpawnError, match="both --epic and --ws"):
        spawn_subagent(spec(ws=None), b)


def test_ws_without_epic_is_refused(tmp_path):
    b, _ = bounds(tmp_path)
    with pytest.raises(SpawnError, match="both --epic and --ws"):
        spawn_subagent(spec(epic=None), b)


def test_reviewer_without_any_shape_is_refused(tmp_path):
    b, _ = bounds(tmp_path)
    with pytest.raises(SpawnError, match="needs a branch to review"):
        spawn_subagent(
            spec(
                role="reviewer",
                epic=None,
                ws=None,
                issue=None,
            ),
            b,
        )


def test_repo_mismatch_is_refused(tmp_path):
    b, _ = bounds(tmp_path, org_repo="acme/widget")
    with pytest.raises(SpawnError, match="--repo 'gadget'"):
        spawn_subagent(spec(repo="gadget"), b)


def test_repo_accepts_the_org_qualified_slug(tmp_path):
    b, _ = bounds(tmp_path)
    result = spawn_subagent(spec(repo="acme/widget"), b)
    assert result.pr == 321


def test_unparseable_origin_is_refused(tmp_path):
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


def test_child_nonzero_exit_is_refused_with_its_stderr(tmp_path):
    b, _ = bounds(tmp_path, returncode=2, stdout="")
    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), b)
    assert "claude child exited 2" in str(exc.value)
    assert "boom" in str(exc.value)


def test_child_nonzero_exit_surfaces_stdout_when_stderr_is_empty(tmp_path):
    # #1153, the failure that cost five DOC01 Runs their reason: a headless
    # `claude -p` reports its own errors on STDOUT, and the refusal used to read
    # stderr only — so five real failures rendered as a bare `child exited 1`
    # with nothing else. The child's account must reach the operator whichever
    # stream carried it.
    b, _ = bounds(
        tmp_path,
        returncode=1,
        stdout="Credit balance is too low to run this request",
        stderr="",
    )

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), b)

    assert "Credit balance is too low" in str(exc.value)


def test_child_nonzero_exit_surfaces_both_streams_labelled(tmp_path):
    # Neither stream is preferred away: a child that wrote to both gets both
    # reported, each labelled, so the operator can tell which said what.
    b, _ = bounds(
        tmp_path,
        returncode=1,
        stdout="stdout-said-this",
        stderr="stderr-said-that",
    )

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), b)

    message = str(exc.value)
    assert "stdout-said-this" in message
    assert "stderr-said-that" in message
    assert "child stdout" in message and "child stderr" in message


def test_silent_nonzero_child_refusal_says_so_and_names_the_tree(tmp_path):
    # The four DOC01 Runs that died with a CLEAN tree and both streams empty: with
    # nothing to quote, the refusal must say the child produced no account at all
    # and hand over the two coordinates that remain actionable — which tree to open
    # and how long the child ran (204s of silence reads very differently from 2s).
    b, _ = bounds(tmp_path, returncode=1, stdout="", stderr="   \n  ")

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), b)

    message = str(exc.value)
    assert "claude child exited 1" in message
    assert "wrote NOTHING to either stdout or stderr" in message
    assert str(tmp_path / "tree") in message  # which tree to open
    assert "ms" in message  # how long it ran before dying
    # Whitespace-only is EMPTY: an empty labelled section is worse than none.
    assert "child stderr" not in message


def test_silent_nonzero_child_still_reports_uncommitted_work(tmp_path):
    # The salvage note is what made WS11/WS15 recoverable (#587) — the richer
    # reason must ride ALONGSIDE it, never displace it.
    b, _ = bounds(
        tmp_path,
        returncode=1,
        stdout="",
        stderr="",
        status_lines=[" M a.py", " M b.py"],
    )

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), b)

    message = str(exc.value)
    assert "wrote NOTHING to either stdout or stderr" in message
    assert "2 uncommitted change(s)" in message
    assert "salvageable" in message


def test_nonzero_child_refusal_logs_the_stream_sizes(tmp_path, caplog):
    # The durable record carries the stream sizes as greppable extras, so "the
    # child said nothing" is answerable from the JSONL log without re-running it.
    b, _ = bounds(tmp_path, returncode=1, stdout="", stderr="")

    with caplog.at_level(logging.ERROR, logger="shipit.spawn"):
        with pytest.raises(SpawnError):
            spawn_subagent(spec(), b)

    record = next(r for r in caplog.records if hasattr(r, "stdout_bytes"))
    assert record.stdout_bytes == 0
    assert record.stderr_bytes == 0
    assert record.rc == 1


def test_launch_transport_failure_is_a_clean_refusal(tmp_path):
    b, _ = bounds(tmp_path)

    def no_binary(cmd, *, cwd, env, timeout=None):
        raise execrun.ExecError(["claude"], rc=None, cause=execrun.CAUSE_MISSING_BINARY)

    with pytest.raises(SpawnError, match="claude"):
        spawn_subagent(spec(), replace(b, runner=no_binary))


def test_no_pr_on_the_branch_is_refused(tmp_path):
    b, _ = bounds(tmp_path, pr=None)
    with pytest.raises(SpawnError, match="opened no PR"):
        spawn_subagent(spec(), b)


def test_unknown_pr_state_is_refused(tmp_path):
    b, _ = bounds(tmp_path, pr=gh.UNKNOWN)
    with pytest.raises(SpawnError, match="could not be read"):
        spawn_subagent(spec(), b)


def test_audit_handshake_is_the_pure_stage():
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


def test_no_pr_refusal_reports_uncommitted_work(tmp_path):
    b, calls = bounds(
        tmp_path, pr=None, status_lines=[" M src/fix.py", "?? tests/t.py"]
    )

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), b)

    assert "opened no PR" in str(exc.value)
    assert "2 uncommitted change(s)" in str(exc.value)
    assert "salvageable" in str(exc.value)
    assert str(tmp_path / "tree") in str(exc.value)
    assert calls["status_cwd"] == str(tmp_path / "tree")


def test_nonzero_child_refusal_reports_uncommitted_work(tmp_path):
    b, _ = bounds(tmp_path, returncode=2, status_lines=[" M src/fix.py"])

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), b)

    assert "claude child exited 2" in str(exc.value)
    assert "1 uncommitted change(s)" in str(exc.value)


def test_clean_tree_refusal_carries_no_salvage_line(tmp_path):
    b, calls = bounds(tmp_path, pr=None)

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), b)

    assert "opened no PR" in str(exc.value)
    assert "salvageable" not in str(exc.value)
    assert "uncommitted" not in str(exc.value)
    assert calls["status_cwd"] == str(tmp_path / "tree")


def test_salvage_probe_failure_never_masks_the_refusal(tmp_path):
    b, _ = bounds(tmp_path, pr=None)

    def unreadable(*, cwd):
        raise ExecError(["git", "status"], rc=128, stderr="not a git repository")

    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(), replace(b, status_porcelain=unreadable))

    assert "opened no PR" in str(exc.value)
    assert "not a git repository" not in str(exc.value)


def test_tree_creation_failure_does_not_probe_salvage(tmp_path):
    b, calls = bounds(tmp_path)

    def no_probe(*, cwd):
        raise AssertionError("a pre-launch refusal must not run the salvage probe")

    def boom(tree_spec, *, source_repo, github_url):
        raise OSError("disk full")

    with pytest.raises(SpawnError, match="tree creation failed"):
        spawn_subagent(spec(), replace(b, create_tree=boom, status_porcelain=no_probe))
    assert "cmd" not in calls


def test_reviewer_failure_does_not_probe_salvage(tmp_path):
    b, _ = bounds(tmp_path)

    def no_probe(*, cwd):
        raise AssertionError("the reviewer tail must not run the salvage probe")

    def fail_review(*args, **kwargs):
        raise RuntimeError("review backend exited 3")

    with pytest.raises(SpawnError, match="review backend exited 3") as exc:
        spawn_subagent(
            spec(role="reviewer", ws=3, issue=None, backend="codex"),
            replace(b, status_porcelain=no_probe, run_review=fail_review),
        )
    assert "salvageable" not in str(exc.value)


def test_issue_only_builds_the_issue_shape_spec(tmp_path):
    b, calls = bounds(tmp_path, pr=replace(_PR, number=77, base_ref="main"))

    result = spawn_subagent(spec(epic=None, ws=None, issue=210), b)

    tree_spec = calls["spec"]
    assert tree_spec.issue == 210 and tree_spec.session == "work"
    assert tree_spec.epic is None and tree_spec.ws is None and tree_spec.branch is None
    task = calls["cmd"][calls["cmd"].index("-p") + 1]
    assert "#210" in task and "issues/210/work" in task
    assert "closes #210" in task and "for #210" not in task
    assert result.branch == "issues/210/work"
    assert result.base == "origin/main"
    assert result.pr == 77


def test_issue_only_uses_a_non_default_session(tmp_path):
    b, calls = bounds(tmp_path, pr=replace(_PR, number=5, base_ref="main"))

    spawn_subagent(spec(epic=None, ws=None, issue=210, session="onboard"), b)

    assert calls["spec"].session == "onboard"
    assert calls["pr_branch"] == "issues/210/onboard"


def test_issue_only_does_not_probe_an_epic_umbrella(tmp_path):
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
    b, calls = bounds(tmp_path)
    with pytest.raises(SpawnError, match="session"):
        spawn_subagent(spec(epic=None, ws=None, issue=210, session=bad_session), b)
    assert "spec" not in calls


def test_reviewer_delegates_to_the_captured_review_service(tmp_path):
    b, calls = bounds(tmp_path)

    result = spawn_subagent(spec(role="reviewer", ws=3, issue=None, backend="codex"), b)

    assert calls["pr_branch"] == "TRE03/WS03"
    assert calls["pr_cwd"] == str(tmp_path / "repo")
    assert calls["review_backend"].name == "codex"
    assert calls["review_target"].slug == "acme/widget"
    assert calls["review_target"].number == 321
    assert calls["review_run_id"] is None
    assert "spec" not in calls and "cmd" not in calls
    assert result.branch == "TRE03/WS03"
    assert result.base == "origin/TRE03/WS03"
    assert result.role == "reviewer" and result.backend == "codex"
    assert result.pr is None
    result_tree = Path(result.tree)
    assert result_tree.parent == layout.central_root()
    assert result_tree.name.startswith("widget-codex-")
    assert "review" not in result_tree.parts


def test_reviewer_naming_threads_through_the_real_service_chain(tmp_path, monkeypatch):
    from shipit.review import rounds, service
    from shipit.spawn import subagent as subagent_mod

    class _StopAfterProvision(Exception):
        """Sentinel: halt the chain the instant provision is reached, before the
        (mocked-away) model launch — the naming is already captured by then."""

    minted = {
        "agent": "codex",
        "created": "20260717-000000",
        "tree_id": "3c8f9a1e-0000-4c0d-9b2a-000000001039",
    }
    monkeypatch.setattr(subagent_mod, "new_tree_naming", lambda binary: dict(minted))

    ctx = review_view(
        number=321,
        repo="acme/widget",
        head_sha="deadbeef" * 5,
        base_ref="TRE03/umbrella",
        base_sha="cafe" * 10,
        diff="diff --git a/x b/x\n",
        is_draft=False,
        changed_files=["x"],
        workdir="/checkout",
        head_ref="TRE03/WS03",
    )
    monkeypatch.setattr(service, "resolve_pr", lambda number, *, repo: ctx)
    monkeypatch.setattr(rounds, "planable", lambda ctx: False)
    monkeypatch.setattr(producer, "preflight_round", lambda backends: None)

    seen: dict = {}

    def spy_provision(ctx_arg, backend, *, naming=None):
        seen["naming"] = naming
        seen["head_ref"] = ctx_arg.head_ref
        raise _StopAfterProvision

    monkeypatch.setattr(producer, "provision_review_tree", spy_provision)

    b, _ = bounds(tmp_path)
    with pytest.raises(SpawnError):
        spawn_subagent(
            spec(role="reviewer", ws=3, issue=None, backend="codex"),
            replace(b, run_review=service.run_detached_review),
        )

    assert seen["naming"] == minted
    assert seen["head_ref"] == "TRE03/WS03"


def test_issue_only_reviewer_pins_the_issue_head(tmp_path):
    b, calls = bounds(tmp_path)

    result = spawn_subagent(
        spec(
            role="reviewer",
            epic=None,
            ws=None,
            issue=210,
            backend="codex",
        ),
        b,
    )

    assert calls["pr_branch"] == "issues/210/work"
    assert result.role == "reviewer"
    assert result.branch == "issues/210/work"


@pytest.mark.parametrize(
    ("backend", "funnel_agent"), [("codex", "codex"), ("antigravity", "agy")]
)
def test_reviewer_service_receives_the_registry_backend(
    tmp_path, backend, funnel_agent
):
    b, calls = bounds(tmp_path)

    result = spawn_subagent(spec(role="reviewer", ws=3, issue=None, backend=backend), b)

    assert calls["review_backend"].funnel_agent == funnel_agent
    assert result.backend == backend


def test_claude_reviewer_is_refused_before_any_io(tmp_path):
    b, calls = bounds(tmp_path)

    with pytest.raises(SpawnError, match="no captured review-service identity"):
        spawn_subagent(spec(role="reviewer", ws=3, issue=None), b)
    assert calls == {}


@pytest.mark.parametrize(
    ("pr", "message"),
    [
        (None, "has no pull request"),
        (gh.UNKNOWN, "could not determine"),
        (replace(_PR, state="CLOSED"), "not OPEN"),
    ],
)
def test_reviewer_requires_a_known_open_pr_before_service(tmp_path, pr, message):
    b, calls = bounds(tmp_path, pr=pr)

    with pytest.raises(SpawnError, match=message):
        spawn_subagent(spec(role="reviewer", ws=3, issue=None, backend="codex"), b)
    assert "review_target" not in calls


def test_reviewer_service_failure_is_a_clean_refusal(tmp_path):
    b, _ = bounds(tmp_path)

    def boom(*args, **kwargs):
        raise ExecError(["codex"], rc=1, stderr="clone or launch failed")

    with pytest.raises(SpawnError, match="captured review service failed"):
        spawn_subagent(
            spec(role="reviewer", ws=3, issue=None, backend="codex"),
            replace(b, run_review=boom),
        )


def test_reviewer_service_failure_records_elapsed_time(tmp_path, caplog):
    b, _ = bounds(tmp_path)

    def boom(*args, **kwargs):
        raise ExecError(["codex"], rc=1, stderr="clone or launch failed")

    with caplog.at_level(logging.ERROR, logger="shipit.spawn"):
        with pytest.raises(SpawnError):
            spawn_subagent(
                spec(role="reviewer", ws=3, issue=None, backend="codex"),
                replace(b, run_review=boom),
            )

    [refusal] = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert isinstance(refusal.duration_ms, int)


def test_epic_spawn_exports_the_current_spawn_identity(tmp_path):
    b, calls = bounds(tmp_path)

    spawn_subagent(spec(), b)

    env = calls["env"]
    assert env["SHIPIT_LOG_CTX_EPIC"] == "TRE03"
    assert env["SHIPIT_LOG_CTX_WS"] == "1"
    assert env["SHIPIT_LOG_CTX_ROLE"] == "implementer"
    assert env["SHIPIT_LOG_CTX_REPO"] == "acme/widget"
    assert env["SHIPIT_LOG_CTX_AGENT"] == calls["spec"].tree_id
    assert env["SHIPIT_LOG_CTX_TREE"] == str(tmp_path / "tree")
    bound = logcontext.bound()
    assert bound["epic"] == "TRE03" and bound["ws"] == 1
    assert bound["role"] == "implementer"
    assert bound["repo"] == "acme/widget"
    assert bound["tree"] == str(tmp_path / "tree")
    assert bound["agent"] == calls["spec"].tree_id


def test_issue_spawn_exports_no_epic_ws_keys(tmp_path):
    b, calls = bounds(tmp_path, pr=replace(_PR, number=77, base_ref="main"))

    spawn_subagent(spec(epic=None, ws=None, issue=210), b)

    env = calls["env"]
    assert "SHIPIT_LOG_CTX_EPIC" not in env
    assert "SHIPIT_LOG_CTX_WS" not in env
    assert env["SHIPIT_LOG_CTX_ROLE"] == "implementer"
    assert env["SHIPIT_LOG_CTX_AGENT"] == calls["spec"].tree_id


def test_issue_spawn_does_not_inherit_a_prior_spawns_epic_identity(tmp_path):
    b_epic, _ = bounds(tmp_path)
    spawn_subagent(spec(), b_epic)

    b_issue, calls = bounds(tmp_path, pr=replace(_PR, number=77, base_ref="main"))
    spawn_subagent(spec(epic=None, ws=None, issue=210), b_issue)

    env = calls["env"]
    assert "SHIPIT_LOG_CTX_EPIC" not in env
    assert "SHIPIT_LOG_CTX_WS" not in env
    assert env["SHIPIT_LOG_CTX_AGENT"] == calls["spec"].tree_id
    bound = logcontext.bound()
    assert "epic" not in bound and "ws" not in bound


def test_reviewer_spawn_exports_identity_with_a_minted_agent_id(tmp_path):
    b, calls = bounds(tmp_path)

    spawn_subagent(spec(role="reviewer", ws=3, issue=None, backend="codex"), b)

    bound = logcontext.bound()
    assert bound["epic"] == "TRE03"
    assert bound["ws"] == 3
    assert bound["role"] == "reviewer"
    assert bound["agent"]
    assert bound["pr"] == 321
    assert bound["repo"] == "acme/widget"
    tree = Path(bound["tree"])
    assert tree.parent == layout.central_root()
    assert tree.name.startswith("widget-codex-")
