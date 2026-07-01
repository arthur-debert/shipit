"""Hook boundary: a `WorktreeCreate` payload on stdin → the Tree path on stdout.

The demoted WorktreeCreate adapter (ADR-0017). Covers the three things the boundary
owns: the **branch-deferred** `tree create` call (the right freeform spec reaches
the orchestrator), marker resolution + safe fallback, and the **fail-CLOSED**
contract (any error → exit 1, NOTHING on stdout, so the spawn aborts rather than
escaping to a native worktree).
"""

from __future__ import annotations

import io
import json
import re

import pytest
from shipit.harness import worktree_adapter
from shipit.tree.create import Tree
from shipit.verbs.hook import worktreecreate


@pytest.fixture
def fake_repo(monkeypatch):
    """Stub the gh/git identity boundary so no real repo/clone is touched.

    Returns the dict the patched `create_from_source` captures its call into, so a
    test can assert exactly which `TreeSpec` (hence which branch) was provisioned.
    """
    captured: dict = {}

    def fake_create(spec, *, source_repo):
        captured["spec"] = spec
        captured["source_repo"] = source_repo
        return Tree(
            path=f"/trees/acme/widget/branches/{spec.agent_hash}",
            branch=spec.branch,
            base="origin/main",
        )

    monkeypatch.setattr(worktreecreate.gh, "repo_root", lambda: "/repo")
    monkeypatch.setattr(worktreecreate.gh, "current_repo", lambda: "acme/widget")
    # Default: the cwd-branch probe finds no branch, so a test that does not opt
    # into a branch never touches real git (its epic comes from the env override).
    monkeypatch.setattr(worktreecreate.gh, "git_current_branch", lambda *, cwd: None)
    # Default: every candidate epic's umbrella "exists", so a test that does not
    # exercise the existence gate gets its epic namespaced; the gate-specific tests
    # below install their own fake keyed on which umbrellas are present.
    monkeypatch.setattr(
        worktreecreate.gh, "epic_umbrella_exists", lambda epic, *, cwd: True
    )
    monkeypatch.setattr(worktreecreate, "create_from_source", fake_create)
    return captured


def _run(payload_text: str) -> tuple[int, str]:
    out = io.StringIO()
    code = worktreecreate.run(stdin=io.StringIO(payload_text), stdout=out)
    return code, out.getvalue()


def test_spawn_lands_in_a_tree_on_epic_branch(monkeypatch, fake_repo):
    # With the SHIPIT_EPIC override set, the spawn provisions a Tree on
    # `<epic>/agent-<id>` and prints its path (which CC adopts as the cwd). The id
    # is read from the VERIFIED payload field `name` (= `agent-<agentId>`).
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "TRE03")
    payload = json.dumps({"hook_event_name": "WorktreeCreate", "name": "agent-abc123"})
    code, out = _run(payload)
    assert code == 0
    spec = fake_repo["spec"]
    # The branch carries the id from `name` — `abc123`, NOT a synthesized random.
    assert spec.branch == "TRE03/agent-abc123"  # branch-deferred holding branch
    assert (spec.org, spec.repo) == ("acme", "widget")
    assert fake_repo["source_repo"] == "/repo"
    assert out.strip() == f"/trees/acme/widget/branches/{spec.agent_hash}"


def test_epic_inferred_from_cwd_branch(monkeypatch, fake_repo):
    # #173: with NO override, the epic is inferred from the coordinator's branch —
    # the hook probes the payload `cwd`'s branch and takes the prefix before `/`.
    monkeypatch.delenv(worktree_adapter.EPIC_MARKER_ENV, raising=False)
    seen: dict = {}

    def fake_branch(*, cwd):
        seen["cwd"] = cwd
        return "TRE04/WS01"

    monkeypatch.setattr(worktreecreate.gh, "git_current_branch", fake_branch)
    payload = json.dumps({"name": "agent-abc123", "cwd": "/coordinator/checkout"})
    code, _ = _run(payload)
    assert code == 0
    assert seen["cwd"] == "/coordinator/checkout"  # probe ran against the payload cwd
    assert fake_repo["spec"].branch == "TRE04/agent-abc123"


def test_override_wins_over_inferred_cwd_branch(monkeypatch, fake_repo):
    # The SHIPIT_EPIC override takes precedence over the inferred branch prefix
    # (the rare cross-epic spawn).
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "HAR02")
    monkeypatch.setattr(
        worktreecreate.gh, "git_current_branch", lambda *, cwd: "TRE04/WS01"
    )
    payload = json.dumps({"name": "agent-abc123", "cwd": "/coordinator/checkout"})
    code, _ = _run(payload)
    assert code == 0
    assert fake_repo["spec"].branch == "HAR02/agent-abc123"


@pytest.mark.parametrize(
    "branch,existing,expected",
    [
        # codex regression (#178): an ordinary slash-branch a coordinator happens to
        # sit on is NOT an epic — only a prefix whose `<prefix>/umbrella` exists is.
        ("feature/foo", set(), "agent-abc123"),  # (a) no `feature/umbrella` → epic-less
        (
            "issues/123/work",
            set(),
            "agent-abc123",
        ),  # (b) no `issues/umbrella` → epic-less
        # (c) real epic: `TRE04/umbrella` exists → the prefix DOES namespace the branch.
        ("TRE04/WS01", {"TRE04"}, "TRE04/agent-abc123"),
        # (d) nested spawn (coordinator already on an `agent-` holding branch) still
        # resolves to the epic when the umbrella is present.
        ("TRE04/agent-x", {"TRE04"}, "TRE04/agent-abc123"),
    ],
)
def test_epic_namespacing_gated_on_umbrella_existence(
    branch, existing, expected, monkeypatch, fake_repo
):
    # The fix for the codex finding: the inferred prefix only namespaces the holding
    # branch when it names a REAL epic (its umbrella branch exists). A non-epic prefix
    # degrades to the same safe epic-less fallback the module already documented.
    monkeypatch.delenv(worktree_adapter.EPIC_MARKER_ENV, raising=False)
    monkeypatch.setattr(worktreecreate.gh, "git_current_branch", lambda *, cwd: branch)
    monkeypatch.setattr(
        worktreecreate.gh,
        "epic_umbrella_exists",
        lambda epic, *, cwd: epic in existing,
    )
    payload = json.dumps({"name": "agent-abc123", "cwd": "/coordinator/checkout"})
    code, _ = _run(payload)
    assert code == 0
    assert fake_repo["spec"].branch == expected


def test_existence_check_runs_against_the_payload_cwd(monkeypatch, fake_repo):
    # The umbrella-existence ref lookup runs in the coordinator's checkout (the payload
    # `cwd` — the same place the spawning branch was read), with the inferred prefix.
    monkeypatch.delenv(worktree_adapter.EPIC_MARKER_ENV, raising=False)
    seen: dict = {}

    def fake_exists(epic, *, cwd):
        seen["epic"] = epic
        seen["cwd"] = cwd
        return True

    monkeypatch.setattr(
        worktreecreate.gh, "git_current_branch", lambda *, cwd: "TRE04/WS01"
    )
    monkeypatch.setattr(worktreecreate.gh, "epic_umbrella_exists", fake_exists)
    _run(json.dumps({"name": "agent-abc123", "cwd": "/coordinator/checkout"}))
    assert seen == {"epic": "TRE04", "cwd": "/coordinator/checkout"}


def test_override_naming_a_dead_epic_degrades_to_epicless(monkeypatch, fake_repo):
    # The existence gate applies to the SHIPIT_EPIC override too (no special-casing):
    # an override naming an epic whose umbrella does not exist degrades to the
    # epic-less fallback, consistent with an inferred non-epic prefix.
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "GHOST")
    monkeypatch.setattr(
        worktreecreate.gh, "epic_umbrella_exists", lambda epic, *, cwd: False
    )
    payload = json.dumps({"name": "agent-abc123", "cwd": "/coordinator/checkout"})
    code, _ = _run(payload)
    assert code == 0
    assert fake_repo["spec"].branch == "agent-abc123"


def test_override_without_cwd_validates_in_ambient_checkout(monkeypatch, fake_repo):
    # An override-only spawn carries no `cwd`; the existence check falls back to the
    # ambient hook checkout (`gh.repo_root()`) so the override is still validated.
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "TRE03")
    seen: dict = {}

    def fake_exists(epic, *, cwd):
        seen["cwd"] = cwd
        return True

    monkeypatch.setattr(worktreecreate.gh, "epic_umbrella_exists", fake_exists)
    _run(json.dumps({"name": "agent-abc123"}))  # no cwd in payload
    assert seen["cwd"] == "/repo"  # gh.repo_root() fallback
    assert fake_repo["spec"].branch == "TRE03/agent-abc123"


@pytest.mark.parametrize(
    "branch,cwd",
    [
        (None, "/coordinator/checkout"),  # detached / unborn / git error → None
        ("main", "/coordinator/checkout"),  # no `/` prefix
        (None, None),  # payload carries no cwd → probe never runs
    ],
)
def test_no_inferable_epic_falls_back_to_epicless_branch(
    branch, cwd, monkeypatch, fake_repo
):
    # No override AND no inferable epic (detached/no-slash/unreadable branch or a
    # missing cwd) → a safe epic-less branch; the spawn STILL lands in a real Tree.
    monkeypatch.delenv(worktree_adapter.EPIC_MARKER_ENV, raising=False)
    monkeypatch.setattr(worktreecreate.gh, "git_current_branch", lambda *, cwd: branch)
    body = {"name": "agent-abc123"}
    if cwd is not None:
        body["cwd"] = cwd
    code, out = _run(json.dumps(body))
    assert code == 0
    assert fake_repo["spec"].branch == "agent-abc123"
    assert out.strip().startswith("/trees/")


@pytest.mark.parametrize("payload", [{}, {"name": ""}, {"name": "   "}])
def test_missing_or_empty_name_synthesizes_an_id(payload, monkeypatch, fake_repo):
    # A payload with no usable `name` still resolves a VALID branch (random id) and
    # never crashes — the spawn is never blocked on a missing/empty name.
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "TRE03")
    code, out = _run(json.dumps({"hook_event_name": "WorktreeCreate", **payload}))
    assert code == 0
    assert re.fullmatch(r"TRE03/agent-[0-9a-f]+", fake_repo["spec"].branch)
    assert out.strip().startswith("/trees/")


def test_legacy_worktree_name_field_is_ignored(monkeypatch, fake_repo):
    # Regression pin for the field-contract bug: the spawn id lives in `name`, not
    # the old `worktree_name` guess. A payload carrying ONLY `worktree_name` must
    # NOT adopt that value — it synthesizes a random id instead. If someone reverts
    # `_resolve_branch` to read `worktree_name`, this test fails loud.
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "TRE03")
    code, _ = _run(json.dumps({"worktree_name": "agent-shouldnotwin"}))
    assert code == 0
    assert "shouldnotwin" not in fake_repo["spec"].branch
    assert re.fullmatch(r"TRE03/agent-[0-9a-f]+", fake_repo["spec"].branch)


def test_fail_closed_when_tree_create_errors(monkeypatch, fake_repo, capsys):
    # A create failure aborts the spawn LOUD — exit 1, nothing on stdout, no
    # native-worktree fallback.
    def boom(spec, *, source_repo):
        raise RuntimeError("clone exploded")

    monkeypatch.setattr(worktreecreate, "create_from_source", boom)
    code, out = _run(json.dumps({"name": "x"}))
    assert code == 1
    assert out == ""  # CC treats empty stdout + nonzero as a failed spawn
    assert "clone exploded" in capsys.readouterr().err


def test_fail_closed_when_not_in_a_checkout(monkeypatch, fake_repo, capsys):
    monkeypatch.setattr(worktreecreate.gh, "repo_root", lambda: None)
    code, out = _run(json.dumps({"name": "x"}))
    assert code == 1
    assert out == ""
    assert "not inside a git checkout" in capsys.readouterr().err


@pytest.mark.parametrize(
    "slug",
    [
        "",  # remote missing/unresolved → empty string
        "widget",  # bare name, no org and no separator
        "/widget",  # empty org
        "acme/",  # empty repo
    ],
)
def test_fail_closed_on_malformed_repo_slug(slug, monkeypatch, fake_repo, capsys):
    # A user-facing boundary: a missing/malformed `org/repo` slug must abort the
    # spawn LOUD — exit 1, nothing on stdout, and NO Tree provisioned (no partial
    # `TreeSpec(repo="")` reaching the orchestrator).
    monkeypatch.setattr(worktreecreate.gh, "current_repo", lambda: slug)
    code, out = _run(json.dumps({"name": "x"}))
    assert code == 1
    assert out == ""  # CC treats empty stdout + nonzero as a failed spawn
    assert "malformed repo slug" in capsys.readouterr().err
    assert "spec" not in fake_repo  # create_from_source was never reached


@pytest.mark.parametrize(
    "garbage",
    [
        "",  # empty stdin
        "not json",
        "{",  # truncated
        "[]",  # valid json, wrong shape
        json.dumps("a string payload"),
    ],
)
def test_fail_closed_on_malformed_input(garbage, capsys):
    # Unlike the fail-OPEN pretooluse guard, a bad payload here aborts the spawn.
    code, out = _run(garbage)
    assert code == 1
    assert out == ""
    assert capsys.readouterr().err  # a diagnostic is surfaced
