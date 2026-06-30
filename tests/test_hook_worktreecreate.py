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
    monkeypatch.setattr(worktreecreate, "create_from_source", fake_create)
    return captured


def _run(payload_text: str) -> tuple[int, str]:
    out = io.StringIO()
    code = worktreecreate.run(stdin=io.StringIO(payload_text), stdout=out)
    return code, out.getvalue()


def test_spawn_lands_in_a_tree_on_epic_branch(monkeypatch, fake_repo):
    # With the session-stable epic marker set, the spawn provisions a Tree on
    # `<epic>/agent-<id>` and prints its path (which CC adopts as the cwd).
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "TRE03")
    payload = json.dumps(
        {"hook_event_name": "WorktreeCreate", "worktree_name": "agent-a5d633b0"}
    )
    code, out = _run(payload)
    assert code == 0
    spec = fake_repo["spec"]
    assert spec.branch == "TRE03/agent-a5d633b0"  # branch-deferred holding branch
    assert (spec.org, spec.repo) == ("acme", "widget")
    assert fake_repo["source_repo"] == "/repo"
    assert out.strip() == f"/trees/acme/widget/branches/{spec.agent_hash}"


def test_missing_marker_falls_back_to_epicless_branch(monkeypatch, fake_repo):
    # No marker → a safe epic-less branch; the spawn STILL lands in a real Tree.
    monkeypatch.delenv(worktree_adapter.EPIC_MARKER_ENV, raising=False)
    payload = json.dumps({"worktree_name": "agent-a5d633b0"})
    code, out = _run(payload)
    assert code == 0
    assert fake_repo["spec"].branch == "agent-a5d633b0"
    assert out.strip().startswith("/trees/")


def test_missing_worktree_name_synthesizes_an_id(monkeypatch, fake_repo):
    # A payload with no usable id still resolves a branch (random id), never blocks.
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "TRE03")
    code, out = _run(json.dumps({"hook_event_name": "WorktreeCreate"}))
    assert code == 0
    assert re.fullmatch(r"TRE03/agent-[0-9a-f]+", fake_repo["spec"].branch)


def test_fail_closed_when_tree_create_errors(monkeypatch, fake_repo, capsys):
    # A create failure aborts the spawn LOUD — exit 1, nothing on stdout, no
    # native-worktree fallback.
    def boom(spec, *, source_repo):
        raise RuntimeError("clone exploded")

    monkeypatch.setattr(worktreecreate, "create_from_source", boom)
    code, out = _run(json.dumps({"worktree_name": "x"}))
    assert code == 1
    assert out == ""  # CC treats empty stdout + nonzero as a failed spawn
    assert "clone exploded" in capsys.readouterr().err


def test_fail_closed_when_not_in_a_checkout(monkeypatch, fake_repo, capsys):
    monkeypatch.setattr(worktreecreate.gh, "repo_root", lambda: None)
    code, out = _run(json.dumps({"worktree_name": "x"}))
    assert code == 1
    assert out == ""
    assert "not inside a git checkout" in capsys.readouterr().err


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
