"""Unit tests for ``tree.layout.plan`` — the pure Tree-planning truth table.

Asserts external behavior (the resolved branch/dir/base for a spec), never "it
called git": the planner is pure, so the plan IS the contract.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shipit.tree import layout
from shipit.tree.layout import TreeSpec, plan, sanitize_slug

ROOT = Path("/trees")


def _git_accepts_branch(branch: str) -> bool:
    """True iff git itself considers ``branch`` a valid ref name.

    ``git check-ref-format`` is a pure format check (no repo needed), so a test can
    prove the branch a shape produces is a REAL git ref — not one that only blows up
    later inside ``git checkout -b`` during ``tree create``/``spawn``.
    """
    return (
        subprocess.run(
            ["git", "check-ref-format", f"refs/heads/{branch}"],
            capture_output=True,
        ).returncode
        == 0
    )


def _issue_spec(**over) -> TreeSpec:
    base = dict(org="acme", repo="widget", agent_hash="deadbeef", issue=123, root=ROOT)
    base.update(over)
    return TreeSpec(**base)


# --------------------------------------------------------------------------
# branch — issues/<id>/<session>, session default 'work' (ADR-0026)
# --------------------------------------------------------------------------


def test_branch_is_issues_id_session_default_work():
    # No --session → the default 'work' leaf. The branch is slash-namespaced and
    # carries neither slug nor hash.
    assert plan(_issue_spec()).branch == "issues/123/work"


def test_branch_uses_a_non_default_session():
    assert plan(_issue_spec(session="onboard")).branch == "issues/123/onboard"


def test_branch_session_is_sanitized_like_a_slug():
    # A session becomes a ref component, so it is lowercased and separator-collapsed
    # the same way a slug is.
    assert plan(_issue_spec(session="Spike Two")).branch == "issues/123/spike-two"


def test_branch_slug_does_not_reach_the_issue_branch():
    # The slug rides the DIR leaf only; the canonical branch stays issues/<id>/<session>.
    p = plan(_issue_spec(slug="header-align"))
    assert p.branch == "issues/123/work"


def test_issue_branch_is_never_the_bare_issues_id_ref_collision_safety():
    # The whole point of the <session> suffix (ADR-0026): the branch is ALWAYS a
    # three-segment ref directory (issues/<id>/<session>), never a bare `issues/<id>`
    # that would occupy `refs/heads/issues/<id>` as a FILE and block a sibling session.
    for session in ("work", "onboard", "spike"):
        branch = plan(_issue_spec(session=session)).branch
        assert branch == f"issues/123/{session}"
        assert branch.count("/") == 2
        assert branch != "issues/123"


def test_branch_never_carries_the_agent_hash():
    p = plan(_issue_spec(agent_hash="cafe1234", slug="x"))
    assert "cafe1234" not in p.branch


@pytest.mark.parametrize("bad_issue", [0, -1, -42])
def test_issue_rejects_non_positive_number(bad_issue):
    # click accepts 0 and negatives; they format as out-of-grammar branches like
    # 'issues/0/work', so the planner rejects them at this invariant boundary.
    with pytest.raises(ValueError, match="positive integer"):
        plan(_issue_spec(issue=bad_issue))


@pytest.mark.parametrize("bad_session", ["", "   ", "///", " . / : "])
def test_issue_rejects_empty_session(bad_session):
    # A session that sanitizes to nothing would yield a bare `issues/<id>/` ref and
    # reintroduce the file-vs-directory collision the suffix dodges — rejected.
    with pytest.raises(ValueError, match="session"):
        plan(_issue_spec(session=bad_session))


# --------------------------------------------------------------------------
# dir — issues/<id>/<session>[-<slug>]-<hash>, hash on the leaf (ADR-0026)
# --------------------------------------------------------------------------


def test_dir_is_central_root_org_repo_issues_id_session_hash():
    p = plan(_issue_spec())
    assert p.dir == ROOT / "acme" / "widget" / "issues" / "123" / "work-deadbeef"


def test_dir_uses_a_non_default_session_leaf():
    p = plan(_issue_spec(session="onboard"))
    assert p.dir == ROOT / "acme" / "widget" / "issues" / "123" / "onboard-deadbeef"


def test_dir_carries_the_agent_hash():
    p = plan(_issue_spec(agent_hash="abc99999"))
    assert p.dir.name == "work-abc99999"


def test_dir_leaf_carries_the_slug_after_the_session():
    # Mirrors the epic shape: the slug rides the DIR leaf (session-slug-hash), never
    # the branch.
    p = plan(_issue_spec(slug="some words"))
    assert p.dir.name == "work-some-words-deadbeef"
    assert p.branch == "issues/123/work"


# --------------------------------------------------------------------------
# base
# --------------------------------------------------------------------------


def test_base_is_origin_main_for_an_issue():
    assert plan(_issue_spec()).base == "origin/main"


# --------------------------------------------------------------------------
# slug sanitization (lives in layout)
# --------------------------------------------------------------------------


def test_sanitize_lowercases_and_dashes_separators():
    assert sanitize_slug("Header/Align: Foo.Bar") == "header-align-foo-bar"


def test_sanitize_collapses_runs_and_trims():
    assert sanitize_slug("  Lots   of   Space  ") == "lots-of-space"


def test_sanitize_all_separators_is_empty():
    assert sanitize_slug("  ///  ") == ""


def test_plan_applies_slug_sanitization_to_the_dir_leaf():
    p = plan(_issue_spec(slug="Fix The Thing"))
    assert p.dir.name == "work-fix-the-thing-deadbeef"


# --------------------------------------------------------------------------
# epic / work-stream shape — branch E/WSnn, base origin/E/umbrella, hash on dir
# --------------------------------------------------------------------------


def _epic_spec(**over) -> TreeSpec:
    base = dict(
        org="acme",
        repo="widget",
        agent_hash="deadbeef",
        epic="HAR02",
        ws=2,
        root=ROOT,
    )
    base.update(over)
    return TreeSpec(**base)


@pytest.mark.parametrize(
    "ws, expected_branch",
    [
        (1, "HAR02/WS01"),
        (2, "HAR02/WS02"),
        (12, "HAR02/WS12"),
        (100, "HAR02/WS100"),
    ],
)
def test_epic_branch_is_slash_namespaced_zero_padded(ws, expected_branch):
    assert plan(_epic_spec(ws=ws)).branch == expected_branch


def test_epic_branch_keeps_epic_code_verbatim():
    # The epic code is human-assigned (uppercase THEME+NN) and is NOT sanitized.
    assert plan(_epic_spec(epic="GPU02")).branch == "GPU02/WS02"


def test_epic_base_is_origin_epic_umbrella():
    assert plan(_epic_spec()).base == "origin/HAR02/umbrella"


def test_epic_umbrella_base_helper_builds_origin_epic_umbrella():
    # The pure helper the planner AND the spawn verb share (#176): one place builds
    # the epic-grouped base, so the verb's fail-closed pre-clone check and the
    # planner agree by construction.
    assert layout.epic_umbrella_base("HAR02") == "origin/HAR02/umbrella"
    # And the planner's resolved base IS exactly what the helper produces.
    assert plan(_epic_spec()).base == layout.epic_umbrella_base("HAR02")


@pytest.mark.parametrize("bad_epic", ["", "  ", "HAR/02", "..", "a b"])
def test_epic_umbrella_base_rejects_unsafe_epic_code(bad_epic):
    # The helper validates the epic code the same way the planner does: a non
    # alphanumeric token would build a malformed origin//umbrella or path-traversing
    # ref, so it is refused rather than returned.
    with pytest.raises(ValueError):
        layout.epic_umbrella_base(bad_epic)


def test_epic_umbrella_base_none_raises_valueerror_not_typeerror():
    # The type is guarded BEFORE the regex, so a non-str (e.g. None) honors the
    # documented ValueError contract rather than leaking a TypeError from
    # _EPIC_CODE.fullmatch(None) — the fail-closed "never a traceback" promise of
    # #176 holds even if a future caller passes a non-str. (A bare `except
    # ValueError` in the spawn verb would NOT catch a TypeError.)
    with pytest.raises(ValueError, match="epic code"):
        layout.epic_umbrella_base(None)


def test_issue_branch_helper_builds_issues_id_session():
    # The pure helper the planner AND the spawn verb's reviewer path share (ADR-0026):
    # one place builds the standalone-issue branch, so a reviewer pins exactly the
    # branch a write Run's planner produced.
    assert layout.issue_branch(123, "work") == "issues/123/work"
    assert layout.issue_branch(123, "onboard") == "issues/123/onboard"
    # And the planner's resolved branch IS exactly what the helper produces.
    assert plan(_issue_spec()).branch == layout.issue_branch(123, "work")


@pytest.mark.parametrize("bad_issue", [0, -1, -42])
def test_issue_branch_helper_rejects_non_positive_issue(bad_issue):
    with pytest.raises(ValueError, match="positive integer"):
        layout.issue_branch(bad_issue, "work")


@pytest.mark.parametrize("not_an_int", [None, "5", 3.0])
def test_issue_branch_helper_non_int_issue_raises_valueerror_not_typeerror(not_an_int):
    # Parity with work_stream_branch: the type is guarded BEFORE the comparison, so a
    # non-int (e.g. None) honors the documented ValueError contract rather than leaking a
    # TypeError from `None < 1`.
    with pytest.raises(ValueError, match="positive integer"):
        layout.issue_branch(not_an_int, "work")


@pytest.mark.parametrize("bad_session", ["", "   ", "///", " . / : "])
def test_issue_branch_helper_rejects_empty_session(bad_session):
    # An empty session would build a bare `issues/<id>/` ref — refused so the
    # file-vs-directory collision the suffix dodges can never be reintroduced.
    with pytest.raises(ValueError, match="session"):
        layout.issue_branch(42, bad_session)


@pytest.mark.parametrize("not_a_str", [None, 3.0, 7, ["work"]])
def test_issue_branch_helper_non_str_session_raises_valueerror_not_attributeerror(
    not_a_str,
):
    # Parity with the issue guard: a non-str session must raise a clean ValueError, not
    # an AttributeError/TypeError from `sanitize_slug(None).strip()`.
    with pytest.raises(ValueError, match="session"):
        layout.issue_branch(1, not_a_str)


# --------------------------------------------------------------------------
# git-ref hardening (codex CHANGES_REQUESTED): session/slug becomes a git REF
# component, so sanitize_slug must strip EVERY char git forbids — not just the old
# separators — and issue_branch is always a VALID ref or a clean ValueError.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "session, expected",
    [
        ("foo~bar", "foo-bar"),  # tilde
        ("foo^bar", "foo-bar"),  # caret
        ("foo:bar", "foo-bar"),  # colon
        ("foo?bar", "foo-bar"),  # question mark
        ("foo*bar", "foo-bar"),  # asterisk
        ("foo[bar", "foo-bar"),  # open bracket
        ("back\\slash", "back-slash"),  # backslash
        ("foo.lock", "foo-lock"),  # trailing .lock (no dot survives)
        ("foo..bar", "foo-bar"),  # doubled dot
        ("@{tricky", "tricky"),  # the @{ reflog sequence
        ("a b c", "a-b-c"),  # spaces
        (".leading.", "leading"),  # leading/trailing dot
        ("ctrl\x01char", "ctrl-char"),  # control char
    ],
)
def test_issue_branch_sanitizes_git_ref_invalid_chars(session, expected):
    # Every git-ref-forbidden character collapses to '-', so the branch is a valid ref
    # rather than one that only fails later inside `git checkout -b`.
    assert layout.issue_branch(5, session) == f"issues/5/{expected}"


@pytest.mark.parametrize("bad", ["@{", "~", "^", "\\", "??", "***", "[", ":::"])
def test_issue_branch_rejects_session_that_is_all_invalid(bad):
    # A session made ENTIRELY of ref-invalid chars sanitizes to '' → the one rejection
    # policy: fail loud (never a bare `issues/<id>/` ref).
    with pytest.raises(ValueError, match="session"):
        layout.issue_branch(5, bad)


@pytest.mark.parametrize(
    "session",
    [
        "foo~bar",
        "foo.lock",
        "foo..bar",
        "@{tricky",
        "back\\slash",
        "foo^bar",
        "foo:bar",
        "foo?bar",
        "foo*bar",
        "foo[bar",
        "a b c",
        "trailing.",
        ".leading",
        "ctrl\x01char",
        "Mixed/Case.Session",
        "work",
        "onboard",
    ],
)
def test_issue_branch_is_always_a_valid_git_ref_or_rejected(session):
    # Prove it with git itself: whatever issue_branch RETURNS, `git check-ref-format`
    # accepts; anything it cannot make valid it REJECTS with ValueError. No middle ground
    # (an invalid ref reaching `tree create`/`spawn` is exactly the codex bug).
    try:
        branch = layout.issue_branch(5, session)
    except ValueError:
        return
    assert branch.startswith("issues/5/")
    assert _git_accepts_branch(branch), f"git rejected {branch!r}"


def test_sanitize_slug_is_an_allowlist_to_a_z0_9_dash():
    # Only [a-z0-9] survive; every other run collapses to '-'. Normal cases unchanged.
    assert sanitize_slug("Foo~Bar^Baz") == "foo-bar-baz"
    assert sanitize_slug("has space") == "has-space"
    assert sanitize_slug("a@{b") == "a-b"
    assert sanitize_slug("v1.2.lock") == "v1-2-lock"
    assert sanitize_slug("work") == "work"
    assert sanitize_slug("header-align") == "header-align"


def test_work_stream_branch_helper_builds_e_wsnn():
    # The pure helper the planner AND the spawn reviewer path share: one place builds
    # AND validates the E/WSnn branch, so both fail loud identically on a bad epic/ws.
    assert layout.work_stream_branch("HAR02", 2) == "HAR02/WS02"
    assert layout.work_stream_branch("GPU02", 12) == "GPU02/WS12"
    # And the planner's resolved branch IS exactly what the helper produces.
    assert plan(_epic_spec()).branch == layout.work_stream_branch("HAR02", 2)


@pytest.mark.parametrize("bad_epic", ["", "  ", "HAR/02", "..", "a b"])
def test_work_stream_branch_helper_rejects_unsafe_epic(bad_epic):
    with pytest.raises(ValueError, match="epic code"):
        layout.work_stream_branch(bad_epic, 2)


@pytest.mark.parametrize("bad_ws", [0, -1, -12])
def test_work_stream_branch_helper_rejects_non_positive_ws(bad_ws):
    with pytest.raises(ValueError, match="positive integer"):
        layout.work_stream_branch("HAR02", bad_ws)


def test_work_stream_branch_helper_none_epic_raises_valueerror_not_typeerror():
    # The type is guarded BEFORE the regex, so a non-str (e.g. None) honors the
    # documented ValueError contract rather than leaking a TypeError.
    with pytest.raises(ValueError, match="epic code"):
        layout.work_stream_branch(None, 2)


def test_non_epic_shapes_keep_origin_main_base():
    # The #176 change is scoped to the EPIC shape: the issue and freeform shapes (a
    # standalone, no-epic Tree) still cut from origin/main — never the umbrella base.
    assert plan(_issue_spec()).base == "origin/main"
    assert plan(_freeform_spec()).base == "origin/main"


def test_epic_dir_is_epics_kind_with_hash_on_leaf():
    p = plan(_epic_spec())
    assert p.dir == ROOT / "acme" / "widget" / "epics" / "HAR02" / "WS02-deadbeef"


def test_epic_dir_carries_slug_when_given_branch_does_not():
    p = plan(_epic_spec(slug="Tiling Pass"))
    assert (
        p.dir
        == ROOT / "acme" / "widget" / "epics" / "HAR02" / "WS02-tiling-pass-deadbeef"
    )
    # The slug rides the dir only; the canonical branch stays E/WSnn.
    assert p.branch == "HAR02/WS02"


def test_epic_branch_never_carries_the_agent_hash():
    p = plan(_epic_spec(agent_hash="cafe1234", slug="anything"))
    assert "cafe1234" not in p.branch


def test_epic_dir_leaf_carries_the_agent_hash():
    assert plan(_epic_spec(agent_hash="abc99999")).dir.name == "WS02-abc99999"


def test_epic_requires_both_epic_and_ws():
    with pytest.raises(ValueError, match="both --epic and --ws"):
        plan(_epic_spec(ws=None))
    with pytest.raises(ValueError, match="both --epic and --ws"):
        plan(TreeSpec(org="o", repo="r", agent_hash="h", ws=3, root=ROOT))


# The epic code is used verbatim as BOTH a branch ref component and a path
# segment, so the planner validates it at this invariant boundary.


@pytest.mark.parametrize(
    "bad_epic",
    [
        "",  # empty → '/WS02' and 'origin//umbrella', dir segment collapses
        "   ",  # whitespace-only
        "HAR 02",  # embedded space
        "HAR/02",  # path/ref separator
        "HAR.02",  # dot
        "..",  # path traversal
        "../evil",  # path traversal escaping the central root
    ],
)
def test_epic_rejects_unsafe_epic_code(bad_epic):
    with pytest.raises(ValueError, match="epic code"):
        plan(_epic_spec(epic=bad_epic))


@pytest.mark.parametrize("bad_ws", [0, -1, -12])
def test_epic_rejects_non_positive_ws(bad_ws):
    # Zero/negative work-stream numbers format as 'WS00'/'WS-1', outside the WSnn
    # grammar — reject before they become an unusable branch.
    with pytest.raises(ValueError, match="positive integer"):
        plan(_epic_spec(ws=bad_ws))


# --------------------------------------------------------------------------
# freeform shape — branch verbatim, base origin/main, sanitized dir leaf
# --------------------------------------------------------------------------


def _freeform_spec(**over) -> TreeSpec:
    base = dict(
        org="acme", repo="widget", agent_hash="deadbeef", branch="spike/foo", root=ROOT
    )
    base.update(over)
    return TreeSpec(**base)


def test_freeform_branch_is_verbatim():
    # The caller owns the freeform name; the planner reflects it unchanged.
    assert plan(_freeform_spec(branch="my/wild-Branch")).branch == "my/wild-Branch"


def test_freeform_base_is_origin_main():
    assert plan(_freeform_spec()).base == "origin/main"


def test_freeform_dir_is_branches_kind_with_sanitized_leaf():
    p = plan(_freeform_spec(branch="spike/foo"))
    assert p.dir == ROOT / "acme" / "widget" / "branches" / "spike-foo-deadbeef"


def test_freeform_dir_sanitizes_separators_and_casing():
    p = plan(_freeform_spec(branch="Spike/Foo.Bar Baz"))
    assert p.dir.name == "spike-foo-bar-baz-deadbeef"


def test_freeform_branch_never_carries_the_agent_hash():
    p = plan(_freeform_spec(agent_hash="cafe1234", branch="wip"))
    assert "cafe1234" not in p.branch


@pytest.mark.parametrize(
    "bad_branch",
    [
        "",  # empty → unusable branch and a bare '-<hash>' leaf
        "   ",  # whitespace-only
        "///",  # all separators → sanitizes to ''
        " . / : ",  # only separator characters
    ],
)
def test_freeform_rejects_branch_that_sanitizes_to_empty(bad_branch):
    with pytest.raises(ValueError, match="freeform --branch"):
        plan(_freeform_spec(branch=bad_branch))


# --------------------------------------------------------------------------
# ephemeral shape — the coordinator's session Tree (ADR-0027): branch
# ephemeral/<id> at birth, dir <root>/<org>/<repo>/ephemeral/<id> (leaf = the id,
# NO hash), base origin/main
# --------------------------------------------------------------------------


def _ephemeral_spec(**over) -> TreeSpec:
    base = dict(
        org="acme",
        repo="widget",
        agent_hash="deadbeef",
        ephemeral="sess-20260702-121314-4242",
        root=ROOT,
    )
    base.update(over)
    return TreeSpec(**base)


def test_ephemeral_branch_is_ephemeral_id():
    assert plan(_ephemeral_spec()).branch == "ephemeral/sess-20260702-121314-4242"


def test_ephemeral_base_is_origin_main():
    # At launch the work is unknown — there is nothing to bind the Tree to but main.
    assert plan(_ephemeral_spec()).base == "origin/main"


def test_ephemeral_dir_is_ephemeral_kind_with_id_leaf_and_no_hash():
    # Ephemeral-by-path: the dir leaf IS the session id — the Tree's identity is
    # the session, so the leaf carries NO agent hash (the launcher-minted id is
    # per-launch unique; the id disambiguates, a hash would be noise).
    p = plan(_ephemeral_spec())
    assert p.dir == ROOT / "acme" / "widget" / "ephemeral" / "sess-20260702-121314-4242"
    assert "deadbeef" not in p.dir.name


def test_ephemeral_dir_and_branch_mirror_at_birth():
    # The id rides BOTH the dir leaf and the branch at birth (the branch then moves
    # to the real work; the dir stays) — matched by construction.
    p = plan(_ephemeral_spec(ephemeral="My Session"))
    assert p.branch == f"ephemeral/{p.dir.name}"


def test_ephemeral_id_is_sanitized_like_every_other_leaf():
    # The id becomes a ref component AND the dir leaf, so it gets the same
    # [a-z0-9-] allow-list normalization as sessions/slugs.
    p = plan(_ephemeral_spec(ephemeral="Sess 42/Foo.Bar"))
    assert p.branch == "ephemeral/sess-42-foo-bar"
    assert p.dir.name == "sess-42-foo-bar"


@pytest.mark.parametrize("bad_id", ["", "   ", "///", " . / : ", "@{", "~"])
def test_ephemeral_rejects_id_that_sanitizes_to_empty(bad_id):
    # A degenerate id would yield a bare 'ephemeral/' ref and the kind dir as the
    # leaf — rejected loud (the hook synthesizes a random id before calling in).
    with pytest.raises(ValueError, match="session id"):
        plan(_ephemeral_spec(ephemeral=bad_id))


def test_ephemeral_branch_helper_builds_and_validates():
    # The pure helper the planner and any direct caller share: one place builds
    # AND validates the ephemeral/<id> branch.
    assert layout.ephemeral_branch("sess-1-2") == "ephemeral/sess-1-2"
    assert plan(
        _ephemeral_spec(ephemeral="sess-1-2")
    ).branch == layout.ephemeral_branch("sess-1-2")


def test_ephemeral_branch_helper_non_str_raises_valueerror_not_attributeerror():
    # Parity with issue_branch/work_stream_branch: a non-str honors the documented
    # ValueError contract rather than leaking an AttributeError from sanitize_slug.
    with pytest.raises(ValueError, match="session id"):
        layout.ephemeral_branch(None)


@pytest.mark.parametrize(
    "session_id",
    ["sess-20260702-121314-4242", "My Session", "spike/foo", "a@{b", "v1.2"],
)
def test_ephemeral_branch_is_always_a_valid_git_ref_or_rejected(session_id):
    # Same guarantee as the issue shape: whatever ephemeral_branch RETURNS, git
    # itself accepts; anything it cannot normalize it REJECTS. No middle ground.
    try:
        branch = layout.ephemeral_branch(session_id)
    except ValueError:
        return
    assert branch.startswith("ephemeral/")
    assert _git_accepts_branch(branch), f"git rejected {branch!r}"


def test_ephemeral_kind_constant_names_the_dir_segment():
    # The cleanup gc rule (SES02 Layer C) keys off this segment; pin that the
    # planner and the constant agree.
    assert plan(_ephemeral_spec()).dir.parent.name == layout.EPHEMERAL_KIND


# --------------------------------------------------------------------------
# the hash NEVER lands on the branch, for ANY shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        _issue_spec(agent_hash="cafef00d", slug="x"),
        _epic_spec(agent_hash="cafef00d", slug="x"),
        _freeform_spec(agent_hash="cafef00d", branch="spike/foo"),
        _ephemeral_spec(agent_hash="cafef00d"),
    ],
)
def test_hash_never_appears_in_any_branch(spec):
    assert spec.agent_hash not in plan(spec).branch


# --------------------------------------------------------------------------
# shape exclusivity — exactly one shape, else ValueError
# --------------------------------------------------------------------------


def test_plan_rejects_no_shape():
    spec = TreeSpec(org="o", repo="r", agent_hash="h", root=ROOT)
    with pytest.raises(ValueError, match="exactly one shape"):
        plan(spec)


@pytest.mark.parametrize(
    "over",
    [
        dict(issue=1, branch="x"),
        dict(issue=1, epic="HAR02", ws=2),
        dict(branch="x", epic="HAR02", ws=2),
        dict(ephemeral="sess-1", issue=1),
        dict(ephemeral="sess-1", branch="x"),
        dict(ephemeral="sess-1", epic="HAR02", ws=2),
    ],
)
def test_plan_rejects_more_than_one_shape(over):
    spec = TreeSpec(org="o", repo="r", agent_hash="h", root=ROOT, **over)
    with pytest.raises(ValueError, match="exactly one shape"):
        plan(spec)


# --------------------------------------------------------------------------
# central root override
# --------------------------------------------------------------------------


def test_central_root_env_override(monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "/custom/trees")
    assert layout.central_root() == Path("/custom/trees")


def test_central_root_default_when_unset(monkeypatch):
    monkeypatch.delenv(layout.CENTRAL_ROOT_ENV, raising=False)
    assert layout.central_root() == Path("~/workspace/trees").expanduser()


def test_central_root_rejects_relative_override(monkeypatch):
    # A relative override would place Trees under the cwd (possibly inside the
    # source checkout), breaking the isolation invariant — reject it loudly.
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "relative/trees")
    with pytest.raises(ValueError, match="absolute"):
        layout.central_root()


def test_plan_uses_central_root_when_spec_root_is_none(monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "/env/trees")
    p = plan(_issue_spec(root=None))
    assert (
        p.dir
        == Path("/env/trees") / "acme" / "widget" / "issues" / "123" / "work-deadbeef"
    )


# --- tree_kind: the path→reclaim-family mapping (ADR-0018/0027) -----------------


def test_tree_kind_maps_the_leaf_parent_segment():
    assert layout.tree_kind("/t/acme/widget/review/tre03-ws03") == layout.REVIEW_KIND
    assert layout.tree_kind("/t/acme/widget/ephemeral/sess-1") == layout.EPHEMERAL_KIND
    for write in (
        "/t/acme/widget/epics/HAR02/WS02-deadbeef",
        "/t/acme/widget/issues/123/work-deadbeef",
        "/t/acme/widget/branches/feat-x-deadbeef",
    ):
        assert layout.tree_kind(write) == layout.WRITE_KIND


def test_tree_kind_never_matches_mid_path_segments():
    # Kind is the leaf's PARENT only: an org/repo named after a kind must not
    # smuggle a write Tree onto another kind's reclaim ladder.
    assert layout.tree_kind("/t/review/widget/branches/x-aa") == layout.WRITE_KIND
    assert layout.tree_kind("/t/ephemeral/widget/branches/x-aa") == layout.WRITE_KIND


def test_tree_kind_epic_or_issue_named_after_a_kind_is_still_a_write_tree():
    # An epic write Tree is epics/<epic>/<leaf>, so the leaf's PARENT is the
    # free-form epic code — and `ephemeral`/`review` are valid epic codes (agy
    # review). The nested-namespace grandparent check must keep such Trees on the
    # write ladder: on the ephemeral ladder a clean, pushed one would be removable
    # after an hour idle, and `sessionstart` would hand it a pidfile.
    assert (
        layout.tree_kind("/t/acme/widget/epics/ephemeral/WS01-aa") == layout.WRITE_KIND
    )
    assert layout.tree_kind("/t/acme/widget/epics/review/WS01-aa") == layout.WRITE_KIND
    # The issues namespace has the same nested shape (issues/<id>/<leaf>); its ids
    # are numeric today, but the guard is structural, not name-based.
    assert layout.tree_kind("/t/acme/widget/issues/ephemeral/w-aa") == layout.WRITE_KIND
