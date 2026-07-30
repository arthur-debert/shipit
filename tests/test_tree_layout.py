from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shipit.identity import repo_from_slug
from shipit.tree import layout
from shipit.tree.layout import TreeSpec, plan, sanitize_slug

REPO = repo_from_slug("acme/widget")

ROOT = Path("/trees")

AGENT = "claude"
CREATED = "20260717-081333"
TREE_ID = "619cf51a-f501-44dc-992f-74df773204aa"

LEAF = f"widget-{AGENT}-{CREATED}-{TREE_ID}"


def _git_accepts_branch(branch: str) -> bool:
    return (
        subprocess.run(
            ["git", "check-ref-format", f"refs/heads/{branch}"],
            capture_output=True,
        ).returncode
        == 0
    )


def _spec(**over) -> TreeSpec:
    base = dict(repo=REPO, agent=AGENT, created=CREATED, tree_id=TREE_ID, root=ROOT)
    base.update(over)
    return TreeSpec(**base)


def _issue_spec(**over) -> TreeSpec:
    base = dict(issue=123)
    base.update(over)
    return _spec(**base)


def test_branch_is_issues_id_session_default_work():
    assert plan(_issue_spec()).branch == "issues/123/work"


def test_branch_uses_a_non_default_session():
    assert plan(_issue_spec(session="onboard")).branch == "issues/123/onboard"


def test_branch_session_is_sanitized_like_a_slug():
    assert plan(_issue_spec(session="Spike Two")).branch == "issues/123/spike-two"


def test_branch_slug_does_not_reach_the_issue_branch():
    p = plan(_issue_spec(slug="header-align"))
    assert p.branch == "issues/123/work"


def test_issue_branch_is_never_the_bare_issues_id_ref_collision_safety():
    for session in ("work", "onboard", "spike"):
        branch = plan(_issue_spec(session=session)).branch
        assert branch == f"issues/123/{session}"
        assert branch.count("/") == 2
        assert branch != "issues/123"


@pytest.mark.parametrize("bad_issue", [0, -1, -42])
def test_issue_rejects_non_positive_number(bad_issue):
    with pytest.raises(ValueError, match="positive integer"):
        plan(_issue_spec(issue=bad_issue))


@pytest.mark.parametrize("bad_session", ["", "   ", "///", " . / : "])
def test_issue_rejects_empty_session(bad_session):
    with pytest.raises(ValueError, match="session"):
        plan(_issue_spec(session=bad_session))


def test_dir_is_the_flat_leaf_for_the_issue_shape():
    assert plan(_issue_spec()).dir == ROOT / LEAF


def test_dir_is_shape_independent_every_shape_resolves_the_same_leaf():
    issue = plan(_issue_spec()).dir
    epic = plan(_epic_spec()).dir
    freeform = plan(_freeform_spec()).dir
    ephemeral = plan(_ephemeral_spec()).dir
    assert issue == epic == freeform == ephemeral == ROOT / LEAF


def test_dir_leaf_is_repo_first_then_agent_then_timestamp_then_id():
    name = plan(_issue_spec()).dir.name
    assert name == f"widget-{AGENT}-{CREATED}-{TREE_ID}"
    assert name.startswith("widget-claude-")
    assert name.endswith(TREE_ID)


def test_dir_session_does_not_change_the_flat_leaf():
    assert plan(_issue_spec(session="onboard")).dir == ROOT / LEAF


def test_dir_slug_does_not_ride_the_flat_leaf():
    p = plan(_issue_spec(slug="some words"))
    assert p.dir == ROOT / LEAF
    assert "some-words" not in p.dir.name


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


def test_sanitize_lowercases_and_dashes_separators():
    assert sanitize_slug("Header/Align: Foo.Bar") == "header-align-foo-bar"


def test_sanitize_collapses_runs_and_trims():
    assert sanitize_slug("  Lots   of   Space  ") == "lots-of-space"


def test_sanitize_all_separators_is_empty():
    assert sanitize_slug("  ///  ") == ""


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
    assert plan(_epic_spec(epic="GPU02")).branch == "GPU02/WS02"


def test_epic_base_is_origin_epic_umbrella():
    assert plan(_epic_spec()).base == "origin/HAR02/umbrella"


def test_epic_umbrella_base_helper_builds_origin_epic_umbrella():
    assert layout.epic_umbrella_base("HAR02") == "origin/HAR02/umbrella"
    assert plan(_epic_spec()).base == layout.epic_umbrella_base("HAR02")


@pytest.mark.parametrize("bad_epic", ["", "  ", "HAR/02", "..", "a b"])
def test_epic_umbrella_base_rejects_unsafe_epic_code(bad_epic):
    with pytest.raises(ValueError):
        layout.epic_umbrella_base(bad_epic)


def test_epic_umbrella_base_none_raises_valueerror_not_typeerror():
    with pytest.raises(ValueError, match="epic code"):
        layout.epic_umbrella_base(None)


def test_issue_branch_helper_builds_issues_id_session():
    assert layout.issue_branch(123, "work") == "issues/123/work"
    assert layout.issue_branch(123, "onboard") == "issues/123/onboard"
    assert plan(_issue_spec()).branch == layout.issue_branch(123, "work")


@pytest.mark.parametrize("bad_issue", [0, -1, -42])
def test_issue_branch_helper_rejects_non_positive_issue(bad_issue):
    with pytest.raises(ValueError, match="positive integer"):
        layout.issue_branch(bad_issue, "work")


@pytest.mark.parametrize("not_an_int", [None, "5", 3.0])
def test_issue_branch_helper_non_int_issue_raises_valueerror_not_typeerror(not_an_int):
    with pytest.raises(ValueError, match="positive integer"):
        layout.issue_branch(not_an_int, "work")


@pytest.mark.parametrize("bad_session", ["", "   ", "///", " . / : "])
def test_issue_branch_helper_rejects_empty_session(bad_session):
    with pytest.raises(ValueError, match="session"):
        layout.issue_branch(42, bad_session)


@pytest.mark.parametrize("not_a_str", [None, 3.0, 7, ["work"]])
def test_issue_branch_helper_non_str_session_raises_valueerror_not_attributeerror(
    not_a_str,
):
    with pytest.raises(ValueError, match="session"):
        layout.issue_branch(1, not_a_str)


@pytest.mark.parametrize(
    "session, expected",
    [
        ("foo~bar", "foo-bar"),
        ("foo^bar", "foo-bar"),
        ("foo:bar", "foo-bar"),
        ("foo?bar", "foo-bar"),
        ("foo*bar", "foo-bar"),
        ("foo[bar", "foo-bar"),
        ("back\\slash", "back-slash"),
        ("foo.lock", "foo-lock"),
        ("foo..bar", "foo-bar"),
        ("@{tricky", "tricky"),
        ("a b c", "a-b-c"),
        (".leading.", "leading"),
        ("ctrl\x01char", "ctrl-char"),
    ],
)
def test_issue_branch_sanitizes_git_ref_invalid_chars(session, expected):
    assert layout.issue_branch(5, session) == f"issues/5/{expected}"


@pytest.mark.parametrize("bad", ["@{", "~", "^", "\\", "??", "***", "[", ":::"])
def test_issue_branch_rejects_session_that_is_all_invalid(bad):
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
    try:
        branch = layout.issue_branch(5, session)
    except ValueError:
        return
    assert branch.startswith("issues/5/")
    assert _git_accepts_branch(branch), f"git rejected {branch!r}"


def test_sanitize_slug_is_an_allowlist_to_a_z0_9_dash():
    assert sanitize_slug("Foo~Bar^Baz") == "foo-bar-baz"
    assert sanitize_slug("has space") == "has-space"
    assert sanitize_slug("a@{b") == "a-b"
    assert sanitize_slug("v1.2.lock") == "v1-2-lock"
    assert sanitize_slug("work") == "work"
    assert sanitize_slug("header-align") == "header-align"


def test_work_stream_branch_helper_builds_e_wsnn():
    assert layout.work_stream_branch("HAR02", 2) == "HAR02/WS02"
    assert layout.work_stream_branch("GPU02", 12) == "GPU02/WS12"
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
    with pytest.raises(ValueError, match="epic code"):
        layout.work_stream_branch(None, 2)


def test_non_epic_shapes_keep_origin_main_base():
    assert plan(_issue_spec()).base == "origin/main"
    assert plan(_freeform_spec()).base == "origin/main"


def test_epic_dir_is_the_flat_leaf():
    assert plan(_epic_spec()).dir == ROOT / LEAF


def test_epic_slug_does_not_ride_the_flat_leaf():
    p = plan(_epic_spec(slug="Tiling Pass"))
    assert p.dir == ROOT / LEAF
    assert "tiling-pass" not in p.dir.name
    assert p.branch == "HAR02/WS02"


def test_epic_requires_both_epic_and_ws():
    with pytest.raises(ValueError, match="both --epic and --ws"):
        plan(_epic_spec(ws=None))
    with pytest.raises(ValueError, match="both --epic and --ws"):
        plan(_spec(ws=3))


@pytest.mark.parametrize(
    "bad_epic",
    [
        "",
        "   ",
        "HAR 02",
        "HAR/02",
        "HAR.02",
        "..",
        "../evil",
    ],
)
def test_epic_rejects_unsafe_epic_code(bad_epic):
    with pytest.raises(ValueError, match="epic code"):
        plan(_epic_spec(epic=bad_epic))


@pytest.mark.parametrize("bad_ws", [0, -1, -12])
def test_epic_rejects_non_positive_ws(bad_ws):
    with pytest.raises(ValueError, match="positive integer"):
        plan(_epic_spec(ws=bad_ws))


def _freeform_spec(**over) -> TreeSpec:
    base = dict(branch="spike/foo")
    base.update(over)
    return _spec(**base)


def test_freeform_branch_is_verbatim():
    assert plan(_freeform_spec(branch="my/wild-Branch")).branch == "my/wild-Branch"


def test_freeform_base_is_origin_main():
    assert plan(_freeform_spec()).base == "origin/main"


@pytest.mark.parametrize("base", ["", "   "])
def test_freeform_explicit_blank_base_is_refused(base):
    with pytest.raises(ValueError, match="base override must not be empty"):
        plan(_freeform_spec(base=base))


def test_freeform_dir_is_the_flat_leaf():
    p = plan(_freeform_spec(branch="spike/foo"))
    assert p.dir == ROOT / LEAF


def test_freeform_dir_is_flat_regardless_of_branch_casing():
    p = plan(_freeform_spec(branch="Spike/Foo.Bar Baz"))
    assert p.dir == ROOT / LEAF


@pytest.mark.parametrize(
    "bad_branch",
    [
        "",
        "   ",
        "///",
        " . / : ",
    ],
)
def test_freeform_rejects_branch_that_sanitizes_to_empty(bad_branch):
    with pytest.raises(ValueError, match="freeform --branch"):
        plan(_freeform_spec(branch=bad_branch))


def _ephemeral_spec(**over) -> TreeSpec:
    base = dict(ephemeral="sess-20260702-121314-4242")
    base.update(over)
    return _spec(**base)


def test_ephemeral_branch_is_ephemeral_id():
    assert plan(_ephemeral_spec()).branch == "ephemeral/sess-20260702-121314-4242"


def test_ephemeral_base_is_origin_main():
    assert plan(_ephemeral_spec()).base == "origin/main"


def test_ephemeral_dir_is_the_flat_leaf_not_the_branch_id():
    p = plan(_ephemeral_spec())
    assert p.dir == ROOT / LEAF
    assert "sess-20260702-121314-4242" not in p.dir.name
    assert p.dir.name.endswith(TREE_ID)


def test_ephemeral_dir_and_branch_no_longer_mirror():
    p = plan(_ephemeral_spec(ephemeral="My Session"))
    assert p.branch == "ephemeral/my-session"
    assert p.dir == ROOT / LEAF
    assert p.dir.name != p.branch.split("/", 1)[1]


def test_ephemeral_branch_id_is_sanitized_like_every_other_ref():
    p = plan(_ephemeral_spec(ephemeral="Sess 42/Foo.Bar"))
    assert p.branch == "ephemeral/sess-42-foo-bar"
    assert p.dir == ROOT / LEAF


@pytest.mark.parametrize("bad_id", ["", "   ", "///", " . / : ", "@{", "~"])
def test_ephemeral_rejects_id_that_sanitizes_to_empty(bad_id):
    with pytest.raises(ValueError, match="session id"):
        plan(_ephemeral_spec(ephemeral=bad_id))


def test_ephemeral_branch_helper_builds_and_validates():
    assert layout.ephemeral_branch("sess-1-2") == "ephemeral/sess-1-2"
    assert plan(
        _ephemeral_spec(ephemeral="sess-1-2")
    ).branch == layout.ephemeral_branch("sess-1-2")


def test_ephemeral_branch_helper_non_str_raises_valueerror_not_attributeerror():
    with pytest.raises(ValueError, match="session id"):
        layout.ephemeral_branch(None)


@pytest.mark.parametrize(
    "session_id",
    ["sess-20260702-121314-4242", "My Session", "spike/foo", "a@{b", "v1.2"],
)
def test_ephemeral_branch_is_always_a_valid_git_ref_or_rejected(session_id):
    try:
        branch = layout.ephemeral_branch(session_id)
    except ValueError:
        return
    assert branch.startswith("ephemeral/")
    assert _git_accepts_branch(branch), f"git rejected {branch!r}"


def test_ephemeral_branch_prefix_constant_names_the_branch_segment():
    branch = plan(_ephemeral_spec()).branch
    assert branch.split("/", 1)[0] == layout.EPHEMERAL_BRANCH_PREFIX


def test_tree_leaf_builds_repo_agent_timestamp_id():
    assert layout.tree_leaf(REPO, AGENT, CREATED, TREE_ID) == LEAF


@pytest.mark.parametrize("agent", ["claude", "codex", "agy"])
def test_tree_leaf_accepts_all_three_backend_binary_names(agent):
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


@pytest.mark.parametrize(
    "bad_created",
    [
        "2026-07-17",
        "20260717081333",
        "20261317-081333",
        "20260717-250000",
        "20260717-081333-extra",
    ],
)
def test_tree_leaf_rejects_a_malformed_timestamp(bad_created):
    with pytest.raises(ValueError, match="created"):
        layout.tree_leaf(REPO, AGENT, bad_created, TREE_ID)


@pytest.mark.parametrize(
    "bad_id",
    [
        "d1",
        "619cf51a",
        "619cf51af50144dc992f74df773204aa",
        "619cf51a-f501-44dc-992f-74df773204aa-extra",
        "zzzzzzzz-f501-44dc-992f-74df773204aa",
    ],
)
def test_tree_leaf_rejects_a_non_uuid_id(bad_id):
    with pytest.raises(ValueError, match="tree_id"):
        layout.tree_leaf(REPO, AGENT, CREATED, bad_id)


@pytest.mark.parametrize("good", [TREE_ID, TREE_ID.upper()])
def test_is_full_uuid_accepts_a_full_uuid_either_case(good):
    assert layout.is_full_uuid(good) is True


@pytest.mark.parametrize(
    "bad", ["", "d1", "619cf51a", "not-a-uuid", None, 123, f"{TREE_ID}x"]
)
def test_is_full_uuid_rejects_non_uuids(bad):
    assert layout.is_full_uuid(bad) is False


def test_is_created_stamp_accepts_a_real_stamp():
    assert layout.is_created_stamp(CREATED) is True


@pytest.mark.parametrize(
    "bad",
    ["", "2026-07-17", "20261317-081333", "20260717-250000", None, 20260717],
)
def test_is_created_stamp_rejects_malformed_or_impossible(bad):
    assert layout.is_created_stamp(bad) is False


def test_parse_flat_leaf_recovers_all_four_coordinates():
    leaf = layout.parse_flat_leaf(LEAF)
    assert leaf is not None
    assert leaf.repo == "widget"
    assert leaf.agent == AGENT
    assert leaf.created == CREATED
    assert leaf.tree_id == TREE_ID


def test_parse_flat_leaf_handles_a_hyphenated_repo_head():
    leaf = layout.parse_flat_leaf(f"my-cool-repo-codex-{CREATED}-{TREE_ID}")
    assert leaf is not None
    assert leaf.repo == "my-cool-repo"
    assert leaf.agent == "codex"
    assert leaf.tree_id == TREE_ID


@pytest.mark.parametrize(
    "bad",
    [
        "widget",
        "acme",
        "epics",
        "review",
        f"widget-Claude-{CREATED}-{TREE_ID}",
        f"widget-claude-20261317-081333-{TREE_ID}",
        f"widget-claude-{CREATED}-d1",
        f"widget-claude-{CREATED}-619cf51a",
        "sess-20260702-121314-4242",
        None,
    ],
)
def test_parse_flat_leaf_is_none_for_non_conforming_names(bad):
    assert layout.parse_flat_leaf(bad) is None


def test_tree_dir_is_root_over_the_flat_leaf():
    assert layout.tree_dir(REPO, AGENT, CREATED, TREE_ID, ROOT) == ROOT / LEAF


def test_tree_dir_uses_central_root_when_root_is_none(monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "/env/trees")
    assert layout.tree_dir(REPO, AGENT, CREATED, TREE_ID) == Path("/env/trees") / LEAF


def test_created_from_leaf_recovers_the_timestamp():
    assert layout.created_from_leaf(LEAF) == CREATED


def test_created_from_leaf_handles_a_hyphenated_repo_head():
    leaf = f"my-cool-repo-codex-{CREATED}-{TREE_ID}"
    assert layout.created_from_leaf(leaf) == CREATED


def test_created_from_leaf_is_none_for_an_old_nested_leaf():
    assert layout.created_from_leaf("WS02-deadbeef") is None
    assert layout.created_from_leaf("sess-20260702-121314-4242") is None


def test_tree_kind_and_repo_dir_are_gone():
    assert not hasattr(layout, "tree_kind")
    assert not hasattr(layout, "repo_dir")
    assert not hasattr(layout, "REVIEW_KIND")
    assert not hasattr(layout, "EPHEMERAL_KIND")
    assert not hasattr(layout, "WRITE_KIND")


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


def test_central_root_env_override(monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "/custom/trees")
    assert layout.central_root() == Path("/custom/trees")


def test_central_root_default_when_unset(monkeypatch):
    monkeypatch.delenv(layout.CENTRAL_ROOT_ENV, raising=False)
    assert layout.central_root() == Path("~/workspace/trees").expanduser()


def test_central_root_rejects_relative_override(monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "relative/trees")
    with pytest.raises(ValueError, match="absolute"):
        layout.central_root()


def test_plan_uses_central_root_when_spec_root_is_none(monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "/env/trees")
    p = plan(_issue_spec(root=None))
    assert p.dir == Path("/env/trees") / LEAF


class _CaseyGit:
    def __init__(self, remote_url):
        self._remote_url = remote_url

    def remote_url(self, *, cwd, remote="origin"):
        return self._remote_url


def test_case_divergent_sources_land_one_repo_prefix():
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


def test_flat_leaf_name_round_trips_the_parse():
    leaf = layout.parse_flat_leaf(LEAF)
    assert leaf is not None
    assert leaf.name == LEAF


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"cd /trees/{LEAF} && ls", [LEAF]),
        (f"cp x {LEAF}/y", [LEAF]),
        (f"cp x '{LEAF}/y'", [LEAF]),
        (f"cp x ../{LEAF}/y", [LEAF]),
        (f"cp /trees/{LEAF}/a /trees/{LEAF}/b", [LEAF, LEAF]),
        ("cd /trees && ls", []),
        ("", []),
        # A stamp of the right SHAPE but not a real calendar time is not a leaf.
        (f"cd /trees/widget-{AGENT}-20261399-081333-{TREE_ID}", []),
        # A truncated id is not a Tree id.
        (f"cd /trees/widget-{AGENT}-{CREATED}-619cf51a", []),
    ],
)
def test_find_flat_leaves(text, expected):
    assert [m.leaf.name for m in layout.find_flat_leaves(text)] == expected


def test_find_flat_leaves_keeps_a_hyphenated_repo_whole():
    """The scan must not start mid-directory, or two Trees of one repo compare unequal."""
    leaf = f"mkdocs-lex-{AGENT}-{CREATED}-{TREE_ID}"
    found = layout.find_flat_leaves(f"cd /trees/{leaf} && ls")
    assert [m.leaf.name for m in found] == [leaf]
    assert found[0].leaf.repo == "mkdocs-lex"


def test_find_flat_leaves_reports_where_each_mention_starts():
    command = f"echo hi > /trees/{LEAF}/x"
    (mention,) = layout.find_flat_leaves(command)
    assert command[mention.start :].startswith(LEAF)


@pytest.mark.parametrize("prefix", ["<", ">", "[", "]", "{", "}", ",", "*", "!", "$"])
def test_punctuation_touching_a_leaf_is_not_absorbed_into_the_repo_name(prefix):
    """It parses either way, so the failure is a shifted `start` and a corrupted name, not a miss."""
    command = f"echo x {prefix}{LEAF}/f"
    (mention,) = layout.find_flat_leaves(command)
    assert mention.leaf.name == LEAF
    assert command[mention.start :].startswith(LEAF)


def test_find_flat_leaves_ignores_a_non_string():
    assert layout.find_flat_leaves(None) == ()
