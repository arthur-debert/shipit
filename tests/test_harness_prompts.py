from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from shipit.harness import prompts
from shipit.harness.prompts import (
    BRIEF_ROLES,
    MANDATORY_BRIEF_SLOTS,
    SUBAGENT_ROLES,
    RoleDefs,
    load_brief_template,
    load_coordinator_slice,
    load_role_defs,
    regenerate,
    render,
)
from shipit.harness.role import Role
from shipit.harness.roleprofile import profile_for

_FIXTURE = RoleDefs(
    base="BASE-BODY: branch off origin/main and drive the draft PR.",
    role_map="MAP-BODY: implementer builds, shepherd reviews, explorer reads.",
    overlays={
        Role.COORDINATOR: "COORD-OVERLAY: orchestrate and delegate; never implement.",
        Role.IMPLEMENTER: "IMPL-OVERLAY: implement with tests and open one draft PR.",
        Role.SHEPHERD: "SHEP-OVERLAY: own addressing for one PR; park between rounds.",
        Role.EXPLORER: "EXPL-OVERLAY: read-only and search-scoped; mutate nothing.",
        Role.REVIEWER: "REVW-OVERLAY: read a PR head and post one review; mutate nothing.",
    },
)


@pytest.mark.parametrize("role", list(Role))
def test_role_prompt_contains_only_its_own_overlay(role):
    rendered = render(_FIXTURE)
    prompt = rendered.role_prompts[role]

    assert _FIXTURE.overlays[role] in prompt
    assert _FIXTURE.base in prompt
    for other in Role:
        if other is not role:
            assert _FIXTURE.overlays[other] not in prompt


def test_union_contains_every_overlay():
    rendered = render(_FIXTURE)
    assert _FIXTURE.base in rendered.agents_union
    assert _FIXTURE.role_map in rendered.agents_union
    for role in Role:
        assert _FIXTURE.overlays[role] in rendered.agents_union


def test_only_the_coordinator_carries_the_role_map():
    rendered = render(_FIXTURE)
    assert _FIXTURE.role_map in rendered.role_prompts[Role.COORDINATOR]
    for role in SUBAGENT_ROLES:
        assert _FIXTURE.role_map not in rendered.role_prompts[role]


def test_reduction_property_holds_on_the_real_fragments():
    defs = load_role_defs()
    rendered = render(defs)
    for role in Role:
        prompt = rendered.role_prompts[role]
        assert defs.overlays[role] in prompt
        for other in Role:
            if other is not role:
                assert defs.overlays[other] not in prompt


def test_real_role_prompts_read_as_their_role():
    rendered = render(load_role_defs())
    assert "You are the COORDINATOR" in rendered.role_prompts[Role.COORDINATOR]
    assert "You are an IMPLEMENTER" in rendered.role_prompts[Role.IMPLEMENTER]
    assert "You are a SHEPHERD" in rendered.role_prompts[Role.SHEPHERD]
    assert "You are an EXPLORER" in rendered.role_prompts[Role.EXPLORER]
    assert "You are a REVIEWER" in rendered.role_prompts[Role.REVIEWER]


def test_reviewer_prompt_uses_the_captured_review_service_contract():
    prompt = render(load_role_defs()).role_prompts[Role.REVIEWER]

    assert "structured review result" in prompt
    assert "shipit captures it and posts" in prompt
    assert "Do not run `gh pr review`" in prompt
    assert "Post exactly one review through the PR" not in prompt


def test_shepherd_prompt_scopes_to_one_pr_across_rounds():
    prompt = render(load_role_defs()).role_prompts[Role.SHEPHERD]
    assert "ONE PR" in prompt
    assert "PARKED" in prompt


def test_shepherd_prompt_carries_the_root_cause_sweep_clause():
    prompt = render(load_role_defs()).role_prompts[Role.SHEPHERD]
    assert "INSTANCE OF A CLASS" in prompt
    assert "sweep the whole PR diff" in prompt


def test_shepherd_prompt_orders_addressing_by_severity_and_never_classifies():
    prompt = render(load_role_defs()).role_prompts[Role.SHEPHERD]
    assert "severity order" in prompt
    assert "critical, then major, then minor, then nit" in prompt
    assert "pre-classified" in prompt
    assert "still end resolved" in prompt
    assert "shipit pr classify" not in prompt
    assert "nitpick|substantive" not in prompt


def test_no_shipped_surface_instructs_classification():
    rendered = render(load_role_defs())
    surfaces = [*rendered.role_prompts.values(), rendered.agents_union]
    for surface in surfaces:
        assert "shipit pr classify" not in surface
        assert "nitpick|substantive" not in surface


def test_no_shipped_surface_says_fresh_shepherd_per_round():
    rendered = render(load_role_defs())
    agent_defs = [
        (_ROOT / ".claude" / "agents" / f"{role.value}.md").read_text(encoding="utf-8")
        for role in SUBAGENT_ROLES
    ]
    surfaces = [*rendered.role_prompts.values(), rendered.agents_union, *agent_defs]
    for text in surfaces:
        lowered = text.lower()
        assert "fresh shepherd" not in lowered
        assert "one review round" not in lowered


def test_coordinator_prompt_carries_the_promotion_clause():
    prompt = render(load_role_defs()).role_prompts[Role.COORDINATOR]
    assert "Promoting durable learnings INTO THE REPO" in prompt
    assert "scratchpad, never an archive" not in prompt
    assert "Session store" in prompt
    assert "ADR-0073" in prompt
    clause = prompt.replace("\\", "")
    assert "a process rule -> the relevant role .lex" in clause
    assert "a decision -> an ADR" in clause
    assert "vocabulary -> CONTEXT.md" in clause
    assert "an open investigation -> a tracker issue" in clause


def test_promotion_clause_is_coordinator_scoped():
    rendered = render(load_role_defs())
    for role in SUBAGENT_ROLES:
        assert "Promoting durable learnings" not in rendered.role_prompts[role]


def test_docs_state_the_promotion_rationale_once():
    epics = (_ROOT / "docs" / "dev" / "epics.lex").read_text(encoding="utf-8")
    assert epics.count("Session memory outlives the Tree") == 1
    assert epics.count("~/.claude/projects/<path-slug>/") == 1
    assert epics.count("Session memory dies with the Tree") == 0


@pytest.mark.parametrize("role", list(BRIEF_ROLES))
def test_brief_template_carries_every_mandatory_slot(role):
    template = load_brief_template(role)
    for slot in MANDATORY_BRIEF_SLOTS:
        assert slot in template


def test_shepherd_brief_also_names_its_pr_slot():
    assert "{{pr}}" in load_brief_template(Role.SHEPHERD)


@pytest.mark.parametrize("role", [r for r in Role if r not in BRIEF_ROLES])
def test_roles_without_a_brief_template_are_refused(role):
    with pytest.raises(ValueError, match="no brief template"):
        load_brief_template(role)


def test_brief_slots_never_leak_into_a_composed_prompt_surface():
    rendered = render(load_role_defs())
    for text in [*rendered.role_prompts.values(), rendered.agents_union]:
        for slot in MANDATORY_BRIEF_SLOTS:
            assert slot not in text


def test_roles_reference_their_brief_template():
    rendered = render(load_role_defs())
    assert "shipit spawn brief" in rendered.role_prompts[Role.COORDINATOR]
    assert "shipit spawn brief implementer" in rendered.role_prompts[Role.IMPLEMENTER]
    assert "shipit spawn brief shepherd" in rendered.role_prompts[Role.SHEPHERD]


def test_subagent_roles_derive_from_the_profile_registry():
    assert SUBAGENT_ROLES == tuple(
        role for role in Role if profile_for(role).generates_agent_def
    )
    assert Role.COORDINATOR not in SUBAGENT_ROLES


def test_brief_roles_derive_from_the_profile_registry():
    assert BRIEF_ROLES == tuple(
        role for role in Role if profile_for(role).has_brief_template
    )


@pytest.mark.parametrize("role", list(SUBAGENT_ROLES))
def test_frontmatter_tools_posture_derives_from_enforcement_posture(role):
    frontmatter = prompts._frontmatter(role)
    parsed = next(yaml.safe_load_all(frontmatter))
    read_only = not profile_for(role).enforcement.checkout_mutation
    assert parsed["name"] == role.value
    assert parsed["description"] == prompts._AGENT_DESCRIPTIONS[role]
    assert ("tools" in parsed) is read_only
    if read_only:
        assert parsed["tools"] == prompts._READ_ONLY_TOOLS


def test_declared_agent_def_surface_cannot_ship_incomplete():
    for role in Role:
        declares = profile_for(role).generates_agent_def
        assert (role in prompts._AGENT_DESCRIPTIONS) is declares
        agent_def = _ROOT / ".claude" / "agents" / f"{role.value}.md"
        assert agent_def.exists() is declares
        if declares:
            parsed = next(yaml.safe_load_all(prompts._frontmatter(role)))
            assert parsed["name"] == role.value
            assert parsed["description"] == prompts._AGENT_DESCRIPTIONS[role]


def test_declared_brief_surface_cannot_ship_incomplete():
    for role in Role:
        if profile_for(role).has_brief_template:
            template = load_brief_template(role)
            for slot in MANDATORY_BRIEF_SLOTS:
                assert slot in template
        else:
            with pytest.raises(ValueError, match="no brief template"):
                load_brief_template(role)


_ROOT = Path(__file__).resolve().parents[1]
_GENERATED = _ROOT / "src" / "shipit" / "data" / "roles" / "generated"


def test_committed_coordinator_slice_matches_render():
    expected = render(load_role_defs()).role_prompts[Role.COORDINATOR]
    assert load_coordinator_slice() == expected


@pytest.mark.parametrize("role", list(SUBAGENT_ROLES))
def test_agent_def_files_exist_with_the_role_prompt_body(role):
    path = _ROOT / ".claude" / "agents" / f"{role.value}.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert f"name: {role.value}" in text
    assert load_role_defs().overlays[role] in text


def test_no_coordinator_agent_def():
    assert not (_ROOT / ".claude" / "agents" / "coordinator.md").exists()


def test_regenerate_records_one_info_record_per_written_file(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="shipit.harness"):
        written = regenerate(tmp_path)
    per_file = [r for r in caplog.records if hasattr(r, "path")]
    assert len(per_file) == len(written)
    assert {r.path for r in per_file} == {str(p) for p in written}
    assert all(r.levelno == logging.INFO for r in per_file)


def test_regenerate_records_a_summary_with_the_count(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="shipit.harness"):
        written = regenerate(tmp_path)
    summaries = [r for r in caplog.records if hasattr(r, "files")]
    assert len(summaries) == 1
    rec = summaries[0]
    assert rec.levelno == logging.INFO
    assert rec.files == len(written)


def test_regenerate_writes_the_agy_native_reviewer_def(tmp_path):
    import yaml

    from shipit.harness.prompts import AGY_REVIEWER_DEF_REL
    from shipit.harness.role import Role

    written = regenerate(tmp_path)
    agy_def = tmp_path.joinpath(*AGY_REVIEWER_DEF_REL)
    assert agy_def in written
    text = agy_def.read_text(encoding="utf-8")

    front = next(yaml.safe_load_all(text))
    assert front["name"] == "reviewer"
    assert front["description"]
    assert "tools" not in front
    assert "Generated from src/shipit/data/roles/" in text

    defs = load_role_defs()
    assert defs.overlays[Role.REVIEWER].strip() in text
    assert "## Dev cycle" not in text


def test_main_prints_one_line_per_regenerated_file(tmp_path, capsys, monkeypatch):
    from shipit.harness import prompts

    monkeypatch.setattr(prompts, "regenerate", lambda: regenerate(tmp_path))
    prompts.main()
    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    written = regenerate(tmp_path)
    assert len(out_lines) == len(written)
    for path in written:
        assert any(str(path) in line for line in out_lines)
