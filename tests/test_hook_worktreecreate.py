"""Hook boundary: a `WorktreeCreate` payload on stdin → the Tree path on stdout.

The WorktreeCreate adapter (ADR-0017 + ADR-0027). Covers the four things the
boundary owns: the **coordinator-vs-helper fork** (`prompt_id` absent ⇒ the
coordinator's ephemeral session Tree; present ⇒ the helper holding branch), the
**branch-deferred** `tree create` call (the right spec shape reaches the
orchestrator), marker resolution + safe fallback, and the **fail-CLOSED** contract
(any error → exit 1, NOTHING on stdout, so the spawn/launch aborts rather than
escaping to a native worktree).
"""

from __future__ import annotations

import io
import json
import re

import pytest

from shipit.harness import worktree_adapter
from shipit.identity import repo_from_slug
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
        # The FLAT, self-describing leaf (ADR-0074): <repo>-<agent>-<timestamp>-<id>,
        # one segment under the root, no owner/kind segment.
        leaf = f"{spec.repo.name}-{spec.agent}-{spec.created}-{spec.tree_id}"
        return Tree(path=f"/trees/{leaf}", branch=spec.branch, base="origin/main")

    monkeypatch.setattr(worktreecreate.git, "repo_root", lambda: "/repo")
    monkeypatch.setattr(
        worktreecreate.identity,
        "resolve_repo",
        lambda cwd=".", **kw: repo_from_slug("acme/widget"),
    )
    # Default: the cwd-branch probe finds no branch, so a test that does not opt
    # into a branch never touches real git (its epic comes from the env override).
    monkeypatch.setattr(worktreecreate.git, "current_branch", lambda *, cwd: None)
    # Default: every candidate epic's umbrella "exists", so a test that does not
    # exercise the existence gate gets its epic namespaced; the gate-specific tests
    # below install their own fake keyed on which umbrellas are present.
    monkeypatch.setattr(
        worktreecreate.git, "epic_umbrella_exists", lambda epic, *, cwd: True
    )
    monkeypatch.setattr(worktreecreate, "create_from_source", fake_create)
    return captured


def _run(payload_text: str) -> tuple[int, str]:
    out = io.StringIO()
    code = worktreecreate.run(stdin=io.StringIO(payload_text), stdout=out)
    return code, out.getvalue()


def _helper(body: dict) -> str:
    """A helper-spawn payload: an in-CC spawn ALWAYS carries a `prompt_id`.

    The verified contract (SES02-WS01 spike): `prompt_id`'s absence is what flips
    the hook to the coordinator/ephemeral path (ADR-0027), so every helper-path
    payload in this file carries one — exactly like the live payloads do.
    """
    return json.dumps({"prompt_id": "c2f52d57-4f6e-4d0a-9b1c-8e3a5d7f2b91", **body})


def test_spawn_lands_in_a_tree_on_epic_branch(monkeypatch, fake_repo):
    # With the SHIPIT_EPIC override set, the spawn provisions a Tree on
    # `<epic>/agent-<id>` and prints its path (which CC adopts as the cwd). The id
    # is read from the VERIFIED payload field `name` (= `agent-<agentId>`).
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "TRE03")
    payload = _helper({"hook_event_name": "WorktreeCreate", "name": "agent-abc123"})
    code, out = _run(payload)
    assert code == 0
    spec = fake_repo["spec"]
    # The branch carries the id from `name` — `abc123`, NOT a synthesized random.
    assert spec.branch == "TRE03/agent-abc123"  # branch-deferred holding branch
    assert spec.repo == repo_from_slug("acme/widget")
    assert fake_repo["source_repo"] == "/repo"
    # The printed path is the flat leaf: <repo>-<agent>-<timestamp>-<id>, agent=claude.
    assert spec.agent == "claude"
    assert (
        out.strip() == f"/trees/{spec.repo.name}-claude-{spec.created}-{spec.tree_id}"
    )


def test_epic_inferred_from_cwd_branch(monkeypatch, fake_repo):
    # #173: with NO override, the epic is inferred from the coordinator's branch —
    # the hook probes the payload `cwd`'s branch and takes the prefix before `/`.
    monkeypatch.delenv(worktree_adapter.EPIC_MARKER_ENV, raising=False)
    seen: dict = {}

    def fake_branch(*, cwd):
        seen["cwd"] = cwd
        return "TRE04/WS01"

    monkeypatch.setattr(worktreecreate.git, "current_branch", fake_branch)
    payload = _helper({"name": "agent-abc123", "cwd": "/coordinator/checkout"})
    code, _ = _run(payload)
    assert code == 0
    assert seen["cwd"] == "/coordinator/checkout"  # probe ran against the payload cwd
    assert fake_repo["spec"].branch == "TRE04/agent-abc123"


def test_override_wins_over_inferred_cwd_branch(monkeypatch, fake_repo):
    # The SHIPIT_EPIC override takes precedence over the inferred branch prefix
    # (the rare cross-epic spawn).
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "HAR02")
    monkeypatch.setattr(
        worktreecreate.git, "current_branch", lambda *, cwd: "TRE04/WS01"
    )
    payload = _helper({"name": "agent-abc123", "cwd": "/coordinator/checkout"})
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
    monkeypatch.setattr(worktreecreate.git, "current_branch", lambda *, cwd: branch)
    monkeypatch.setattr(
        worktreecreate.git,
        "epic_umbrella_exists",
        lambda epic, *, cwd: epic in existing,
    )
    payload = _helper({"name": "agent-abc123", "cwd": "/coordinator/checkout"})
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
        worktreecreate.git, "current_branch", lambda *, cwd: "TRE04/WS01"
    )
    monkeypatch.setattr(worktreecreate.git, "epic_umbrella_exists", fake_exists)
    _run(_helper({"name": "agent-abc123", "cwd": "/coordinator/checkout"}))
    assert seen == {"epic": "TRE04", "cwd": "/coordinator/checkout"}


def test_override_naming_a_dead_epic_degrades_to_epicless(monkeypatch, fake_repo):
    # The existence gate applies to the SHIPIT_EPIC override too (no special-casing):
    # an override naming an epic whose umbrella does not exist degrades to the
    # epic-less fallback, consistent with an inferred non-epic prefix.
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "GHOST")
    monkeypatch.setattr(
        worktreecreate.git, "epic_umbrella_exists", lambda epic, *, cwd: False
    )
    payload = _helper({"name": "agent-abc123", "cwd": "/coordinator/checkout"})
    code, _ = _run(payload)
    assert code == 0
    assert fake_repo["spec"].branch == "agent-abc123"


def test_override_without_cwd_validates_in_ambient_checkout(monkeypatch, fake_repo):
    # An override-only spawn carries no `cwd`; the existence check falls back to the
    # ambient hook checkout (`git.repo_root()`) so the override is still validated.
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "TRE03")
    seen: dict = {}

    def fake_exists(epic, *, cwd):
        seen["cwd"] = cwd
        return True

    monkeypatch.setattr(worktreecreate.git, "epic_umbrella_exists", fake_exists)
    _run(_helper({"name": "agent-abc123"}))  # no cwd in payload
    assert seen["cwd"] == "/repo"  # git.repo_root() fallback
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
    monkeypatch.setattr(worktreecreate.git, "current_branch", lambda *, cwd: branch)
    body = {"name": "agent-abc123", "prompt_id": "c2f52d57-4f6e-4d0a-9b1c-8e3a5d7f2b91"}
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
    code, out = _run(_helper({"hook_event_name": "WorktreeCreate", **payload}))
    assert code == 0
    assert re.fullmatch(r"TRE03/agent-[0-9a-f]+", fake_repo["spec"].branch)
    assert out.strip().startswith("/trees/")


def test_legacy_worktree_name_field_is_ignored(monkeypatch, fake_repo):
    # Regression pin for the field-contract bug: the spawn id lives in `name`, not
    # the old `worktree_name` guess. A payload carrying ONLY `worktree_name` must
    # NOT adopt that value — it synthesizes a random id instead. If someone reverts
    # `_resolve_branch` to read `worktree_name`, this test fails loud.
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "TRE03")
    code, _ = _run(_helper({"worktree_name": "agent-shouldnotwin"}))
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
    monkeypatch.setattr(worktreecreate.git, "repo_root", lambda: None)
    code, out = _run(json.dumps({"name": "x"}))
    assert code == 1
    assert out == ""
    assert "not inside a git checkout" in capsys.readouterr().err


def test_fail_closed_on_unresolvable_identity(monkeypatch, fake_repo, capsys):
    # A user-facing boundary: an origin remote the canonical resolver cannot parse
    # must abort the spawn LOUD — exit 1, nothing on stdout, and NO Tree
    # provisioned (no bogus identity reaching the orchestrator).
    def _raise(cwd=".", **kw):
        raise ValueError("cannot parse owner/name from remote url: 'garbage'")

    monkeypatch.setattr(worktreecreate.identity, "resolve_repo", _raise)
    code, out = _run(json.dumps({"name": "x"}))
    assert code == 1
    assert out == ""  # CC treats empty stdout + nonzero as a failed spawn
    assert "cannot parse owner/name" in capsys.readouterr().err
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


# --------------------------------------------------------------------------
# the ADR-0027 fork: a coordinator launch (no prompt_id) → the ephemeral shape
# --------------------------------------------------------------------------


def test_coordinator_launch_provisions_the_ephemeral_shape(fake_repo):
    # The spike-verified top-level `--worktree` payload (no `prompt_id`) takes the
    # ephemeral fork: the spec carries the session id in `ephemeral` and NO
    # freeform branch — the planner resolves ephemeral/<id> off origin/main.
    payload = json.dumps(
        {
            "session_id": "c6010bf9",
            "transcript_path": "/t/c6010bf9.jsonl",
            "cwd": "/coordinator/checkout",
            "hook_event_name": "WorktreeCreate",
            "name": "sess-20260702-121314-4242",
        }
    )
    code, out = _run(payload)
    assert code == 0
    spec = fake_repo["spec"]
    assert spec.ephemeral == "sess-20260702-121314-4242"
    assert spec.branch is None  # NOT the freeform helper shape
    assert spec.repo == repo_from_slug("acme/widget")
    assert out.strip()  # the printed path is what CC adopts as the root cwd


def test_helper_spawn_with_prompt_id_keeps_the_holding_branch(monkeypatch, fake_repo):
    # The same payload WITH a prompt_id is an in-CC helper spawn — the existing
    # <epic>/agent-<id> holding branch, unchanged by ADR-0027.
    monkeypatch.delenv(worktree_adapter.EPIC_MARKER_ENV, raising=False)
    monkeypatch.setattr(
        worktreecreate.git, "current_branch", lambda *, cwd: "TRE04/WS01"
    )
    payload = _helper({"name": "agent-abc123", "cwd": "/coordinator/checkout"})
    code, _ = _run(payload)
    assert code == 0
    spec = fake_repo["spec"]
    assert spec.branch == "TRE04/agent-abc123"
    assert spec.ephemeral is None


def test_coordinator_launch_never_runs_the_epic_probe(monkeypatch, fake_repo):
    # The ephemeral fork does not resolve a holding branch, so the coordinator
    # launch never probes the cwd branch or the umbrella ref — the session Tree is
    # always off origin/main regardless of what branch the launch dir sits on.
    def explode(*a, **k):
        raise AssertionError("the coordinator fork must not probe git state")

    monkeypatch.setattr(worktreecreate.git, "current_branch", explode)
    monkeypatch.setattr(worktreecreate.git, "epic_umbrella_exists", explode)
    payload = json.dumps({"name": "sess-x", "cwd": "/coordinator/checkout"})
    code, _ = _run(payload)
    assert code == 0
    assert fake_repo["spec"].ephemeral == "sess-x"


@pytest.mark.parametrize("payload", [{}, {"name": ""}, {"name": "@{"}])
def test_coordinator_launch_with_degenerate_name_synthesizes_an_id(payload, fake_repo):
    # A missing/empty/all-ref-forbidden `-w` value still yields a valid ephemeral
    # id (random hex) — a launch is never blocked on a degenerate name, mirroring
    # the helper path's missing-`name` fallback.
    code, out = _run(json.dumps({"hook_event_name": "WorktreeCreate", **payload}))
    assert code == 0
    assert re.fullmatch(r"[0-9a-f]+", fake_repo["spec"].ephemeral)
    assert out.strip()


def test_empty_prompt_id_takes_the_coordinator_fork(fake_repo):
    # A null prompt_id counts as absent (no prompt exists at launch) — coordinator.
    code, _ = _run(json.dumps({"name": "sess-y", "prompt_id": None}))
    assert code == 0
    assert fake_repo["spec"].ephemeral == "sess-y"


# --------------------------------------------------------------------------
# ADR-0074 <id> provenance — the flat dir leaf's <id> per creation path, no pid
# --------------------------------------------------------------------------

#: A full UUID (8-4-4-4-12 hex) — the ONLY shape ADR-0074 allows for a leaf's <id>.
_UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def test_coordinator_tree_id_is_the_harness_session_uuid_never_a_pid(fake_repo):
    # ADR-0074: the coordinator session Tree's dir <id> IS the harness session UUID
    # from the payload, so the dir name is the resume handle a human types into
    # `claude --resume`. The `--worktree` launch NAME may be a pid-shaped token, but
    # that never reaches the dir — the trailing <id> is the full session UUID, no pid.
    session_uuid = "c6010bf9-1234-4abc-8def-0123456789ab"
    payload = json.dumps(
        {
            "session_id": session_uuid,
            "cwd": "/coordinator/checkout",
            "hook_event_name": "WorktreeCreate",
            "name": "sess-20260702-121314-4242",  # a pid-shaped launch name...
        }
    )
    code, out = _run(payload)
    assert code == 0
    spec = fake_repo["spec"]
    assert spec.agent == "claude"
    assert spec.tree_id == session_uuid  # ...but the dir <id> is the session UUID
    assert out.strip().endswith(session_uuid)  # the leaf ends in the full UUID
    assert "4242" not in out  # the launch name's pid never reaches the dir


@pytest.mark.parametrize(
    "bad_sid", ["d1", "c6010bf9", "", "not-a-uuid", "../escape", "c6010bf9-1234"]
)
def test_coordinator_non_uuid_session_id_is_refused_and_a_uuid_minted(
    bad_sid, fake_repo
):
    # Finding B (ADR-0074): a coordinator `session_id` that is NOT a full UUID — a pid, a
    # truncated prefix, empty, or a path-bearing value — must never become the flat dir
    # <id>: it would mint an unresolvable session Tree (or one whose leaf escapes the
    # central root). The hook mints a fresh full UUID instead of trusting the raw value,
    # so the leaf is always a real, resolvable, full-UUID <id>.
    payload = json.dumps(
        {
            "session_id": bad_sid,
            "cwd": "/coordinator/checkout",
            "hook_event_name": "WorktreeCreate",
            "name": "sess-1",
        }
    )
    code, out = _run(payload)
    assert code == 0
    spec = fake_repo["spec"]
    assert spec.tree_id != bad_sid  # the raw value is refused
    assert re.fullmatch(_UUID_RE, spec.tree_id)  # a freshly minted full UUID
    assert out.strip().endswith(spec.tree_id)


def test_helper_tree_id_is_minted_not_the_parents_session_id(monkeypatch, fake_repo):
    # ADR-0074: on the helper arm the payload's session_id is the PARENT's, so the
    # hook MINTS a fresh UUID for the dir <id> rather than carrying it — naming the
    # wrong session is worse than naming none.
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "TRE03")
    parent_sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    payload = _helper({"name": "agent-abc123", "session_id": parent_sid})
    code, _ = _run(payload)
    assert code == 0
    spec = fake_repo["spec"]
    assert spec.agent == "claude"
    assert spec.tree_id != parent_sid  # minted, NOT the parent's session id
    assert re.fullmatch(_UUID_RE, spec.tree_id)  # ...and a real full UUID, never a pid


def test_every_creation_arm_stamps_a_timestamp_and_claude_agent(monkeypatch, fake_repo):
    # Both hook arms mint the flat leaf's coordinates: agent = the backend binary
    # (claude — this hook fires only for Claude Code) and a %Y%m%d-%H%M%S created stamp.
    monkeypatch.setenv(worktree_adapter.EPIC_MARKER_ENV, "TRE03")
    _run(_helper({"name": "agent-abc123"}))  # helper arm
    helper = fake_repo["spec"]
    _run(json.dumps({"session_id": "d1", "name": "sess-x"}))  # coordinator arm
    coordinator = fake_repo["spec"]
    for spec in (helper, coordinator):
        assert spec.agent == "claude"
        assert re.fullmatch(r"\d{8}-\d{6}", spec.created)
