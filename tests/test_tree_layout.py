"""Unit tests for ``tree.layout.plan`` — the pure Tree-planning truth table.

Asserts external behavior (the resolved branch/dir/base for a spec), never "it
called git": the planner is pure, so the plan IS the contract.

Since ADR-0074 the DIR is one flat, self-describing leaf for EVERY shape —
``<root>/<repo>-<agent>-<timestamp>-<id>`` — so the dir truth table collapses to
"every shape resolves the SAME flat leaf"; only the BRANCH and BASE still differ
per shape (that identity moved out of the path and into the branch). The old
nested truth table (``epics``/``issues``/``branches``/``ephemeral``/``review``
segments, a per-Tree ``agent_hash`` on the leaf, ``tree_kind`` dispatch, and
``repo_dir``) is gone with the nested shape it described.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shipit.identity import repo_from_slug
from shipit.tree import layout
from shipit.tree.layout import TreeSpec, plan, sanitize_slug

#: The canonical Repo identity every spec in this file namespaces under —
#: built through the ONE slug parser, exactly as the feeders build it. Its NAME
#: (``widget``) leads the flat dir leaf; the owner (``acme``) is NOT a path segment.
REPO = repo_from_slug("acme/widget")

ROOT = Path("/trees")

#: The three minted flat-leaf coordinates (ADR-0074 / naming.lex §4): the backend
#: BINARY name, the ``%Y%m%d-%H%M%S`` stamp, and a full UUID. The caller mints these
#: (they are impure — clock + randomness / the harness session id), so ``plan`` stays
#: a pure function of the spec.
AGENT = "claude"
CREATED = "20260717-081333"
TREE_ID = "619cf51a-f501-44dc-992f-74df773204aa"

#: The single flat leaf every shape resolves to: ``<repo>-<agent>-<timestamp>-<id>``.
LEAF = f"widget-{AGENT}-{CREATED}-{TREE_ID}"


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


def _spec(**over) -> TreeSpec:
    """A :class:`TreeSpec` carrying the flat-leaf coordinates plus whatever shape
    fields ``over`` sets — the base every per-shape helper builds on."""
    base = dict(repo=REPO, agent=AGENT, created=CREATED, tree_id=TREE_ID, root=ROOT)
    base.update(over)
    return TreeSpec(**base)


def _issue_spec(**over) -> TreeSpec:
    base = dict(issue=123)
    base.update(over)
    return _spec(**base)


# --------------------------------------------------------------------------
# branch — issues/<id>/<session>, session default 'work' (ADR-0026)
# --------------------------------------------------------------------------


def test_branch_is_issues_id_session_default_work():
    # No --session → the default 'work' leaf. The branch is slash-namespaced.
    assert plan(_issue_spec()).branch == "issues/123/work"


def test_branch_uses_a_non_default_session():
    assert plan(_issue_spec(session="onboard")).branch == "issues/123/onboard"


def test_branch_session_is_sanitized_like_a_slug():
    # A session becomes a ref component, so it is lowercased and separator-collapsed
    # the same way a slug is.
    assert plan(_issue_spec(session="Spike Two")).branch == "issues/123/spike-two"


def test_branch_slug_does_not_reach_the_issue_branch():
    # The slug is accepted for call-site compatibility but no longer rides the dir
    # (the flat leaf carries who/when); the canonical branch stays issues/<id>/<session>.
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
# dir — the single flat leaf <repo>-<agent>-<timestamp>-<id> (ADR-0074)
# --------------------------------------------------------------------------


def test_dir_is_the_flat_leaf_for_the_issue_shape():
    assert plan(_issue_spec()).dir == ROOT / LEAF


def test_dir_is_shape_independent_every_shape_resolves_the_same_leaf():
    # ADR-0074: the dir carries who/when, not what — so the issue, epic, freeform, and
    # ephemeral shapes all resolve the IDENTICAL flat leaf from the same coordinates.
    # Only the branch/base differ (asserted per shape below).
    issue = plan(_issue_spec()).dir
    epic = plan(_epic_spec()).dir
    freeform = plan(_freeform_spec()).dir
    ephemeral = plan(_ephemeral_spec()).dir
    assert issue == epic == freeform == ephemeral == ROOT / LEAF


def test_dir_leaf_is_repo_first_then_agent_then_timestamp_then_id():
    # The grammar the leaf pins: repo NAME first (the axis a human narrows on, so
    # `ls | grep widget` groups), agent second (the backend binary), then the
    # timestamp, then the full UUID — owner and kind segments gone.
    name = plan(_issue_spec()).dir.name
    assert name == f"widget-{AGENT}-{CREATED}-{TREE_ID}"
    assert name.startswith("widget-claude-")
    assert name.endswith(TREE_ID)


def test_dir_session_does_not_change_the_flat_leaf():
    # The <session> plays its structural role in the BRANCH; the dir is the same flat
    # leaf regardless (unlike the retired nested leaf, which encoded the session).
    assert plan(_issue_spec(session="onboard")).dir == ROOT / LEAF


def test_dir_slug_does_not_ride_the_flat_leaf():
    # The slug is accepted for call-site compatibility but no longer appears in the
    # dir (git records what; the dir records who/when).
    p = plan(_issue_spec(slug="some words"))
    assert p.dir == ROOT / LEAF
    assert "some-words" not in p.dir.name


# --------------------------------------------------------------------------
# base
# --------------------------------------------------------------------------


def test_base_is_origin_main_for_an_issue():
    assert plan(_issue_spec()).base == "origin/main"


def test_freeform_branch_can_override_base_for_pr_attachment():
    p = plan(
        _spec(
            branch="RPE01/WS04",
            base="origin/RPE01/WS04",
        )
    )

    assert p.branch == "RPE01/WS04"
    assert p.base == "origin/RPE01/WS04"
    assert p.dir == ROOT / LEAF


def test_freeform_branch_normalizes_base_override_for_pr_attachment():
    p = plan(_freeform_spec(base="  origin/RPE01/WS04  "))

    assert p.base == "origin/RPE01/WS04"


# --------------------------------------------------------------------------
# slug sanitization (lives in layout)
# --------------------------------------------------------------------------


def test_sanitize_lowercases_and_dashes_separators():
    assert sanitize_slug("Header/Align: Foo.Bar") == "header-align-foo-bar"


def test_sanitize_collapses_runs_and_trims():
    assert sanitize_slug("  Lots   of   Space  ") == "lots-of-space"


def test_sanitize_all_separators_is_empty():
    assert sanitize_slug("  ///  ") == ""


# --------------------------------------------------------------------------
# epic / work-stream shape — branch E/WSnn, base origin/E/umbrella, flat dir
# --------------------------------------------------------------------------


def _epic_spec(**over) -> TreeSpec:
    base = dict(epic="HAR02", ws=2)
    base.update(over)
    return _spec(**base)


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


def test_epic_dir_is_the_flat_leaf():
    # The epic/work-stream identity lives in the BRANCH (E/WSnn); the dir is the same
    # flat leaf every shape resolves.
    assert plan(_epic_spec()).dir == ROOT / LEAF


def test_epic_slug_does_not_ride_the_flat_leaf():
    p = plan(_epic_spec(slug="Tiling Pass"))
    assert p.dir == ROOT / LEAF
    assert "tiling-pass" not in p.dir.name
    # The slug rides nothing now; the canonical branch stays E/WSnn.
    assert p.branch == "HAR02/WS02"


def test_epic_requires_both_epic_and_ws():
    with pytest.raises(ValueError, match="both --epic and --ws"):
        plan(_epic_spec(ws=None))
    with pytest.raises(ValueError, match="both --epic and --ws"):
        plan(_spec(ws=3))


# The epic code is used verbatim as BOTH a branch ref component and a path
# segment, so the planner validates it at this invariant boundary.


@pytest.mark.parametrize(
    "bad_epic",
    [
        "",  # empty → '/WS02' and 'origin//umbrella'
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
# freeform shape — branch verbatim, base origin/main, flat dir
# --------------------------------------------------------------------------


def _freeform_spec(**over) -> TreeSpec:
    base = dict(branch="spike/foo")
    base.update(over)
    return _spec(**base)


def test_freeform_branch_is_verbatim():
    # The caller owns the freeform name; the planner reflects it unchanged.
    assert plan(_freeform_spec(branch="my/wild-Branch")).branch == "my/wild-Branch"


def test_freeform_base_is_origin_main():
    assert plan(_freeform_spec()).base == "origin/main"


@pytest.mark.parametrize("base", ["", "   "])
def test_freeform_explicit_blank_base_is_refused(base):
    with pytest.raises(ValueError, match="base override must not be empty"):
        plan(_freeform_spec(base=base))


def test_freeform_dir_is_the_flat_leaf():
    # The freeform name lives in the BRANCH, not the path, so an arbitrary `spike/foo`
    # never needs sanitizing into a dir leaf — the dir is the flat shape.
    p = plan(_freeform_spec(branch="spike/foo"))
    assert p.dir == ROOT / LEAF


def test_freeform_dir_is_flat_regardless_of_branch_casing():
    p = plan(_freeform_spec(branch="Spike/Foo.Bar Baz"))
    assert p.dir == ROOT / LEAF


@pytest.mark.parametrize(
    "bad_branch",
    [
        "",  # empty → unusable branch
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
# ephemeral/<id> at birth, base origin/main, flat dir whose <id> is the harness
# session UUID (supplied via tree_id, NOT the ephemeral branch id)
# --------------------------------------------------------------------------


def _ephemeral_spec(**over) -> TreeSpec:
    base = dict(ephemeral="sess-20260702-121314-4242")
    base.update(over)
    return _spec(**base)


def test_ephemeral_branch_is_ephemeral_id():
    assert plan(_ephemeral_spec()).branch == "ephemeral/sess-20260702-121314-4242"


def test_ephemeral_base_is_origin_main():
    # At launch the work is unknown — there is nothing to bind the Tree to but main.
    assert plan(_ephemeral_spec()).base == "origin/main"


def test_ephemeral_dir_is_the_flat_leaf_not_the_branch_id():
    # ADR-0074: the dir and branch NO LONGER share a leaf. The `ephemeral/<id>`
    # identity is the BRANCH's; the dir's <id> is the harness session UUID (supplied
    # via tree_id so the dir name IS the resume handle). The branch id never appears
    # in the dir.
    p = plan(_ephemeral_spec())
    assert p.dir == ROOT / LEAF
    assert "sess-20260702-121314-4242" not in p.dir.name
    assert p.dir.name.endswith(TREE_ID)


def test_ephemeral_dir_and_branch_no_longer_mirror():
    # The birth-branch id names ONLY the branch now; the dir keeps the harness UUID.
    p = plan(_ephemeral_spec(ephemeral="My Session"))
    assert p.branch == "ephemeral/my-session"
    assert p.dir == ROOT / LEAF
    assert p.dir.name != p.branch.split("/", 1)[1]


def test_ephemeral_branch_id_is_sanitized_like_every_other_ref():
    # The branch id becomes a ref component, so it gets the same [a-z0-9-] allow-list
    # normalization as sessions/slugs. (It no longer doubles as the dir leaf.)
    p = plan(_ephemeral_spec(ephemeral="Sess 42/Foo.Bar"))
    assert p.branch == "ephemeral/sess-42-foo-bar"
    assert p.dir == ROOT / LEAF


@pytest.mark.parametrize("bad_id", ["", "   ", "///", " . / : ", "@{", "~"])
def test_ephemeral_rejects_id_that_sanitizes_to_empty(bad_id):
    # A degenerate id would yield a bare 'ephemeral/' ref — rejected loud (the hook
    # synthesizes a random id before calling in).
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


def test_ephemeral_branch_prefix_constant_names_the_branch_segment():
    # The birth-branch prefix is now a BRANCH concern only (the flat dir has no kind
    # segment); pin that the planner and the constant agree.
    branch = plan(_ephemeral_spec()).branch
    assert branch.split("/", 1)[0] == layout.EPHEMERAL_BRANCH_PREFIX


# --------------------------------------------------------------------------
# tree_leaf / tree_dir / created_from_leaf — the flat grammar helpers (ADR-0074)
# --------------------------------------------------------------------------


def test_tree_leaf_builds_repo_agent_timestamp_id():
    assert layout.tree_leaf(REPO, AGENT, CREATED, TREE_ID) == LEAF


@pytest.mark.parametrize("agent", ["claude", "codex", "agy"])
def test_tree_leaf_accepts_all_three_backend_binary_names(agent):
    # ADR-0074: <agent> is the CLI BINARY name — claude / codex / agy, all three.
    leaf = layout.tree_leaf(REPO, agent, CREATED, TREE_ID)
    assert leaf == f"widget-{agent}-{CREATED}-{TREE_ID}"


@pytest.mark.parametrize("bad_agent", ["", "Claude", "cla ude", "agy!", "co/dex", None])
def test_tree_leaf_rejects_a_non_alphanumeric_agent(bad_agent):
    with pytest.raises(ValueError, match="agent"):
        layout.tree_leaf(REPO, bad_agent, CREATED, TREE_ID)


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_tree_leaf_rejects_empty_created_or_id(bad):
    with pytest.raises(ValueError):
        layout.tree_leaf(REPO, AGENT, bad, TREE_ID)
    with pytest.raises(ValueError):
        layout.tree_leaf(REPO, AGENT, CREATED, bad)


def test_tree_dir_is_root_over_the_flat_leaf():
    assert layout.tree_dir(REPO, AGENT, CREATED, TREE_ID, ROOT) == ROOT / LEAF


def test_tree_dir_uses_central_root_when_root_is_none(monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "/env/trees")
    assert layout.tree_dir(REPO, AGENT, CREATED, TREE_ID) == Path("/env/trees") / LEAF


def test_created_from_leaf_recovers_the_timestamp():
    # `tree list`'s created column sources from the name: the <timestamp> group of the
    # <timestamp>-<uuid> tail.
    assert layout.created_from_leaf(LEAF) == CREATED


def test_created_from_leaf_handles_a_hyphenated_repo_head():
    # The leaf HEAD (<repo>-<agent>) may itself carry hyphens, so the stamp is matched
    # by the anchored tail, not by splitting on '-'.
    leaf = f"my-cool-repo-codex-{CREATED}-{TREE_ID}"
    assert layout.created_from_leaf(leaf) == CREATED


def test_created_from_leaf_is_none_for_an_old_nested_leaf():
    # An OLD nested Tree's leaf (WS02 reclaims those by attrition) does not match the
    # flat tail, so the column reads '-' rather than a fabricated date.
    assert layout.created_from_leaf("WS02-deadbeef") is None
    assert layout.created_from_leaf("sess-20260702-121314-4242") is None


def test_tree_kind_and_repo_dir_are_gone():
    # ADR-0074 removed the kind segment and the owner segment, so their parsers are
    # gone too — no reader is left for either. Pin their removal so a stale caller
    # fails loud rather than silently resurrecting the nested grammar.
    assert not hasattr(layout, "tree_kind")
    assert not hasattr(layout, "repo_dir")
    assert not hasattr(layout, "REVIEW_KIND")
    assert not hasattr(layout, "EPHEMERAL_KIND")
    assert not hasattr(layout, "WRITE_KIND")


# --------------------------------------------------------------------------
# shape exclusivity — exactly one shape, else ValueError
# --------------------------------------------------------------------------


def test_plan_rejects_no_shape():
    spec = _spec()
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
    spec = _spec(**over)
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
    assert p.dir == Path("/env/trees") / LEAF


# --------------------------------------------------------------------------
# identity threading (COR02-WS02, #252): one Repo, one flat leaf namespace
# --------------------------------------------------------------------------


class _CaseyGit:
    """A fake git boundary whose origin remote carries a MIXED-case slug."""

    def __init__(self, remote_url):
        self._remote_url = remote_url

    def remote_url(self, *, cwd, remote="origin"):
        return self._remote_url


def test_case_divergent_sources_land_one_repo_prefix():
    # The ADR-0024 disease this WS keeps out of the plumbing: a mixed-case ORIGIN
    # remote and a mixed-case API slug are ONE repo on GitHub, so the flat leaf's
    # <repo> prefix is IDENTICAL — the Repo identity normalizes, not each key site.
    from shipit.identity import resolve_repo

    from_origin = resolve_repo(
        "/checkout", boundary=_CaseyGit("https://github.com/AcMe/WiDgEt.git")
    )
    from_api_slug = repo_from_slug("ACME/Widget")

    def _plan_dir(repo):
        return plan(
            TreeSpec(
                repo=repo,
                agent=AGENT,
                created=CREATED,
                tree_id=TREE_ID,
                issue=7,
                root=ROOT,
            )
        ).dir

    assert _plan_dir(from_origin) == _plan_dir(from_api_slug)
    assert _plan_dir(from_origin) == ROOT / LEAF
