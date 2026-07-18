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
    """The default epic-shape write spec; override any field per test."""
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
) -> tuple[Boundaries, dict]:
    """Fake every effectful edge as a recording callable; return (bounds, calls).

    The write creator 'creates' a real directory (the launcher needs a real cwd)
    and resolves branch/base through the REAL pure planner
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
        return launch.LaunchResult(returncode=returncode, stdout="{}", stderr="boom")

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
    # Epic WS shape ⇒ the non-closing `for #N` link (#649): the WS issue stays
    # open until the umbrella PR closes the epic's issues.
    assert "for #156" in task and "closes #156" not in task
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
    # Acceptance #156: the Run↔PR link is resolved from the *Tree's* branch, read
    # inside the Tree (cwd) — the PR on the branch IS the link, no side database.
    b, calls = bounds(tmp_path)

    result = spawn_subagent(spec(ws=2, issue=99), b)

    assert calls["cwd"] == str(tmp_path / "tree")  # the Run is rooted in the Tree
    assert calls["pr_branch"] == "TRE03/WS02"  # link resolved from the Tree branch
    assert calls["pr_cwd"] == str(tmp_path / "tree")  # ...read from inside the Tree
    assert result.pr == 321


# --- the happy path: shepherd existing-PR write attachment --------------------


def shepherd_spec(**overrides) -> SubagentSpec:
    """A detached shepherd attaches by PR number, never by issue/work-stream shape."""
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
    # ADR-0074: the spec's naming half is now three flat-leaf fields — the backend
    # binary <agent>, a <timestamp>, and a full-UUID <id> — not a per-PR `agent_hash`.
    # Shepherd Trees are per-Run (like review Trees): each round mints a fresh <id>, so
    # the cross-round identity rides the log context (`agent=pr321`), asserted by
    # test_shepherd_round_mints_a_fresh_per_run_tree.
    assert tree_spec.agent == "claude"
    assert tree_spec.tree_id and "-" in tree_spec.tree_id  # a full UUID, never a pid
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
    # ADR-0074: shepherd Trees are PER-RUN, like review Trees. Flat naming mints a fresh
    # UUID <id> per spawn, so there is no derivable per-PR path to reuse across rounds —
    # each round provisions its own clone via create_tree (never a FileExistsError-driven
    # reuse/refresh), and resume rebuilds from the durable per-repo logs, not a persisted
    # Tree. Two rounds on the SAME PR mint DISTINCT tree_ids.
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

    # Each round creates a Tree directly — no refresh/reuse seam exists any more.
    assert first_calls["spec"].branch == "TRE03/WS04"
    assert first_calls["spec"].base == "origin/TRE03/WS04"
    assert first.branch == "TRE03/WS04"
    # Per-Run: the two rounds mint DISTINCT full-UUID <id>s for the same PR.
    first_id = first_calls["spec"].tree_id
    second_id = second_calls["spec"].tree_id
    assert first_id != second_id
    assert "-" in first_id and "-" in second_id  # full UUIDs, never pids


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
    # Acceptance (RPE01-WS05): a PROVISIONED implementer write Run resolves a
    # Work Env and launches through the EXISTING pixi-run wrapping and
    # environment scrub — the spawn seam supplies the facts (the provisioned-env
    # sentinel, the on-disk env identity read through the pixi adapter), the
    # resolver decides purely, and the launch consumes the carried decision.
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

    # The backend argv was re-expressed through the Tree's OWN pixi env — the
    # same wrapped shape the pixi adapter builds (`pixi run --manifest-path
    # <tree>/pixi.toml -- <backend argv>`).
    assert calls["cmd"][:4] == [
        "pixi",
        "run",
        "--manifest-path",
        str(tree_dir / "pixi.toml"),
    ]
    child_argv = calls["cmd"][calls["cmd"].index("--") + 1 :]
    assert child_argv[child_argv.index("--agent") + 1] == "implementer"
    # The existing environment scrub still applies on top: the parent's leaked
    # project pointer never reaches the child (Work Env changed WHO decides the
    # routing, not what the launch does).
    assert "PIXI_PROJECT_MANIFEST" not in calls["env"]
    # The resolution record (spec §Observability): routing decision, checkout
    # strategy, and the BORROWED pixi env name — never a fabricated run id.
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
    # Routing is determined by the provisioned-env sentinel. The identity file
    # only enriches observability, so malformed optional metadata cannot make an
    # otherwise launchable write Run fail.
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
    # Acceptance (RPE01-WS05): a NON-pixi write Run represents absent pixi
    # activation honestly — the Work Env resolves AMBIENT (no activation, no
    # env identity extra on the record) — and the existing launch behavior is
    # preserved: the backend argv stays bare, exactly as before.
    b, calls = bounds(tmp_path)  # the fake tree dir carries no .pixi env

    with caplog.at_level(logging.INFO, logger="shipit.spawn"):
        spawn_subagent(spec(), b)

    assert calls["cmd"][0] != "pixi"  # bare backend argv, unrouted
    assert calls["cmd"][calls["cmd"].index("--agent") + 1] == "implementer"
    record = next(
        r
        for r in caplog.records
        if getattr(r, "work_env_boundary", None) == "spawn.write-run"
    )
    assert record.routing == "ambient"
    assert record.checkout_strategy == "new-write-tree"
    # Absent-not-null: no pixi env ⇒ no pixi identity extra on the record at all.
    assert not hasattr(record, "pixi_environment_name")


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


def test_unknown_role_is_refused_before_any_io(tmp_path, caplog):
    # RPE01-WS01: an arbitrary role string fails the Role Profile registry's
    # spawn preflight — before repo resolution, Tree provisioning, or launch —
    # naming the role and the requested (detached) context.
    def untouchable():
        raise AssertionError("the role preflight must fire before any I/O")

    caplog.set_level(logging.ERROR, logger="shipit.spawn")
    b, calls = bounds(tmp_path)
    with pytest.raises(SpawnError, match="unknown role 'wizard'") as exc:
        spawn_subagent(spec(role="wizard"), replace(b, repo_root=untouchable))
    assert "detached" in str(exc.value)  # the refusal names the context
    assert not calls  # no boundary touched: nothing created, nothing launched
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
    # RPE01-WS01: a KNOWN role whose profile does not support a detached launch
    # refuses before a Tree can be minted — the explorer is ambient (a detached
    # spawn would hand it a write Tree it must never have), the coordinator is
    # the host session itself.
    b, calls = bounds(tmp_path)
    with pytest.raises(SpawnError) as exc:
        spawn_subagent(spec(role=role), b)
    message = str(exc.value)
    assert role in message and "detached" in message  # role + context named
    assert not calls  # refused pre-provisioning, pre-launch


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
    # The registry parse (strip/lower) is what the pipeline DISPATCHES on, so a
    # cased '--role Reviewer' rides the read-only reviewer tail — it can never
    # slip past the dispatch into the write path — and a cased write role
    # reaches its launch argv normalized.
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
    # --issue feeds the task prompt and the PR's issue link (`closes #<issue>`
    # standalone / `for #<issue>` epic WS, #649); a
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
        spawn_subagent(
            spec(
                role="reviewer",
                epic=None,
                ws=None,
                issue=None,
            ),
            b,
        )


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
    # Standalone shape ⇒ the CLOSING `closes #N` link (#649), so the merged PR
    # auto-closes its issue (`for #N` is not a GitHub closing keyword).
    assert "closes #210" in task and "for #210" not in task
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


def test_reviewer_delegates_to_the_captured_review_service(tmp_path):
    # RPE01-WS03: the profile's shared-read-only strategy selects the product
    # capture-and-post service. The generic spawn runner/self-posting prompt is
    # never touched; the typed OPEN PR is the service target.
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
    # ADR-0074: the reviewer Tree is the single flat leaf one segment below the central
    # root — no `review/` kind segment. Its <repo>-<agent> head names the repo (widget)
    # and the codex backend binary; the branch identity lives on the branch, not the path.
    assert result_tree.parent == layout.central_root()
    assert result_tree.name.startswith("widget-codex-")
    assert "review" not in result_tree.parts


def test_reviewer_naming_threads_through_the_real_service_chain(tmp_path, monkeypatch):
    # #1039: the SPAWNED `tree` the coordinator reports must be the SAME per-Run
    # read-only Tree the producer clones the reviewer into. The WHOLE fix is
    # threading the boundary's pre-minted flat-leaf `review_tree_naming` down the
    # detached-review chain, so the test must EXERCISE that chain — not a double
    # that calls `provision_review_tree` directly (which would pass even if
    # `run_detached_review` / `generate_review` / `run_fanout_review` dropped the
    # naming). We run the REAL `run_detached_review` boundary and SPY the naming
    # `provision_review_tree` actually receives at the far end of the chain: if any
    # layer fails to pass `review_tree_naming=...`, provision sees `None` and the
    # assertion below fails (mutation-checked).
    from shipit.review import rounds, service
    from shipit.spawn import subagent as subagent_mod

    class _StopAfterProvision(Exception):
        """Sentinel: halt the chain the instant provision is reached, before the
        (mocked-away) model launch — the naming is already captured by then."""

    # A KNOWN flat-leaf naming minted at the boundary, so the assertion can pin the
    # EXACT `{agent, created, tree_id}` dict the coordinator reports as the payload's
    # tree id — and prove that same dict reaches `provision_review_tree` unchanged.
    minted = {
        "agent": "codex",
        "created": "20260717-000000",
        "tree_id": "3c8f9a1e-0000-4c0d-9b2a-000000001039",
    }
    monkeypatch.setattr(subagent_mod, "new_tree_naming", lambda binary: dict(minted))

    # The heavy PR resolve the detached child runs first — echo a fixed view so the
    # chain needs no fetch/diff. Its head_ref is the reviewer's PR head, the same
    # branch the SPAWNED payload names.
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
    # Round SCOPE is decided from git ancestry on the checkout — force the round-1
    # default plan so `generate_review` touches no git for this hand-fed view.
    monkeypatch.setattr(rounds, "planable", lambda ctx: False)
    # The per-round binary preflight is pure PATH I/O (the sandbox has no codex).
    monkeypatch.setattr(producer, "preflight_round", lambda backends: None)

    seen: dict = {}

    def spy_provision(ctx_arg, backend, *, naming=None):
        # The seam under test: capture what `run_fanout_review` threaded down —
        # THROUGH run_detached_review → generate_review → run_fanout_review — then
        # short-circuit before any pass launches.
        seen["naming"] = naming
        seen["head_ref"] = ctx_arg.head_ref
        raise _StopAfterProvision

    monkeypatch.setattr(producer, "provision_review_tree", spy_provision)

    b, _ = bounds(tmp_path)
    # The default boundary set runs the REAL detached-review child; our spy stops it
    # at provision, so the boundary normalizes the sentinel to a clean refusal.
    with pytest.raises(SpawnError):
        spawn_subagent(
            spec(role="reviewer", ws=3, issue=None, backend="codex"),
            replace(b, run_review=service.run_detached_review),
        )

    # The naming the boundary minted reached `provision_review_tree` UNCHANGED, so
    # the id the coordinator reports in the SPAWNED payload (minted["tree_id"]) IS
    # the id the producer clones under — the pre-#1039 bug was a second, unrelated
    # per-Run UUID minted inside the producer instead of this one.
    assert seen["naming"] == minted
    assert seen["head_ref"] == "TRE03/WS03"


def test_issue_only_reviewer_pins_the_issue_head(tmp_path):
    # A reviewer follows the same shapes: --issue with no epic pins the
    # standalone-issue head issues/<id>/<session> for the shared read-only Tree.
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
    # Claude has no captured review-service/App identity, so allowing it would
    # resurrect a second self-posting contract behind the same Role.
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


# --- the spawn-seam identity binding + export (LOG04-WS02 / ADR-0032) ---------
# The spawn seam binds the worker's dev-cycle identity from its OWN arguments
# (`epic`/`ws`/`role`, plus the minted `agent` spawn id) and `env_export`
# threads every bound key into the Run's environment as SHIPIT_LOG_CTX_* — so
# every shipit command the worker runs correlates to its Work Stream with zero
# worker cooperation.


def test_epic_spawn_exports_the_current_spawn_identity(tmp_path):
    b, calls = bounds(tmp_path)

    spawn_subagent(spec(), b)

    env = calls["env"]
    # The child env carries the whole identity (`ws` stringified — the child's
    # bind_from_env casts it back to int; the int-typed round-trip is pinned in
    # test_logcontext).
    assert env["SHIPIT_LOG_CTX_EPIC"] == "TRE03"
    assert env["SHIPIT_LOG_CTX_WS"] == "1"
    assert env["SHIPIT_LOG_CTX_ROLE"] == "implementer"
    assert env["SHIPIT_LOG_CTX_REPO"] == "acme/widget"
    # The agent spawn id IS the Tree dir's <id> (the full UUID, ADR-0074), so the log
    # key and the Tree leaf's trailing id agree.
    assert env["SHIPIT_LOG_CTX_AGENT"] == calls["spec"].tree_id
    # The Tree identity rides the seam too (LOG01-WS03).
    assert env["SHIPIT_LOG_CTX_TREE"] == str(tmp_path / "tree")
    # And the parent's own records carry the same identity from the seam on.
    bound = logcontext.bound()
    assert bound["epic"] == "TRE03" and bound["ws"] == 1
    assert bound["role"] == "implementer"
    assert bound["repo"] == "acme/widget"
    assert bound["tree"] == str(tmp_path / "tree")
    assert bound["agent"] == calls["spec"].tree_id


def test_issue_spawn_exports_no_epic_ws_keys(tmp_path):
    # A standalone-issue spawn has no epic/ws: the keys stay ABSENT from the
    # child env (present-when-bound crosses the seam) — role/agent still ride.
    b, calls = bounds(tmp_path, pr=replace(_PR, number=77, base_ref="main"))

    spawn_subagent(spec(epic=None, ws=None, issue=210), b)

    env = calls["env"]
    assert "SHIPIT_LOG_CTX_EPIC" not in env
    assert "SHIPIT_LOG_CTX_WS" not in env
    assert env["SHIPIT_LOG_CTX_ROLE"] == "implementer"
    assert env["SHIPIT_LOG_CTX_AGENT"] == calls["spec"].tree_id


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
    assert env["SHIPIT_LOG_CTX_AGENT"] == calls["spec"].tree_id  # THIS spawn's
    bound = logcontext.bound()
    assert "epic" not in bound and "ws" not in bound


def test_reviewer_spawn_exports_identity_with_a_minted_agent_id(tmp_path):
    # The reviewer's Tree is PER-RUN now (ADR-0074) and the review service provisions
    # its own flat clone internally, so this spawn boundary mints a FRESH agent id
    # (a full UUID) for the Run's identity rather than reading it off a shared leaf.
    b, calls = bounds(tmp_path)

    spawn_subagent(spec(role="reviewer", ws=3, issue=None, backend="codex"), b)

    # The service is in-process at this boundary; the spawn context is bound for
    # its capture/post records instead of exported to a generic child environment.
    bound = logcontext.bound()
    assert bound["epic"] == "TRE03"
    assert bound["ws"] == 3
    assert bound["role"] == "reviewer"
    assert bound["agent"]
    assert bound["pr"] == 321
    assert bound["repo"] == "acme/widget"
    # The bound Tree is the flat per-Run reviewer leaf, one segment below the central
    # root (no `review/` segment) — <repo>-<agent>-<timestamp>-<id>.
    tree = Path(bound["tree"])
    assert tree.parent == layout.central_root()
    assert tree.name.startswith("widget-codex-")
