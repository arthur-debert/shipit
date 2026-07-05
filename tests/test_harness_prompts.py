"""Role-prompt generator: the reduction property + the derived surfaces.

The KEY test (ADR-0011) is the *reduction property*: a generated role prompt
contains its OWN overlay and NONE of the other roles' overlays — the mechanical
anti-drift guarantee — while the ``AGENTS.md`` union contains them all. Asserts
external behavior (the composed text, the committed files), never internal call
shapes. Mirrors the pure-core / thin-boundary split of ``test_prstate_state.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from shipit.harness.prompts import (
    SUBAGENT_ROLES,
    RoleDefs,
    load_coordinator_slice,
    load_role_defs,
    regenerate,
    render,
)
from shipit.harness.role import Role

# A synthetic fixture with a unique sentinel per fragment, so containment is
# unambiguous: no sentinel is a substring of another. The reduction property is a
# property of the COMPOSITION (render), provable on plain strings with no I/O.
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


# --- the reduction property (the anti-drift guarantee) -----------------------


@pytest.mark.parametrize("role", list(Role))
def test_role_prompt_contains_only_its_own_overlay(role):
    """Each role prompt embeds its OWN overlay and NONE of the others' — the
    mechanical reason an agent cannot drift into another role mid-session."""
    rendered = render(_FIXTURE)
    prompt = rendered.role_prompts[role]

    assert _FIXTURE.overlays[role] in prompt  # its own marching orders
    assert _FIXTURE.base in prompt  # plus the shared base, always
    for other in Role:
        if other is not role:
            assert _FIXTURE.overlays[other] not in prompt


def test_union_contains_every_overlay():
    """The AGENTS.md union is the one surface that carries ALL overlays (the
    non-binding reference) — base + every role's overlay + the role map."""
    rendered = render(_FIXTURE)
    assert _FIXTURE.base in rendered.agents_union
    assert _FIXTURE.role_map in rendered.agents_union
    for role in Role:
        assert _FIXTURE.overlays[role] in rendered.agents_union


def test_only_the_coordinator_carries_the_role_map():
    """The coordinator is the one broad slice — its prompt ALSO carries the role
    map; a subagent prompt does not (it has no one to delegate to)."""
    rendered = render(_FIXTURE)
    assert _FIXTURE.role_map in rendered.role_prompts[Role.COORDINATOR]
    for role in SUBAGENT_ROLES:
        assert _FIXTURE.role_map not in rendered.role_prompts[role]


# --- the same property on the REAL bundled fragments -------------------------


def test_reduction_property_holds_on_the_real_fragments():
    """The shipped fragments obey the reduction property too (not just the
    fixture): each real overlay lands only in its own role's prompt."""
    defs = load_role_defs()
    rendered = render(defs)
    for role in Role:
        prompt = rendered.role_prompts[role]
        assert defs.overlays[role] in prompt
        for other in Role:
            if other is not role:
                assert defs.overlays[other] not in prompt


def test_real_role_prompts_read_as_their_role():
    """Smoke check that the fragments say what their role is — the prompt opens
    by naming the role it scopes to."""
    rendered = render(load_role_defs())
    assert "You are the COORDINATOR" in rendered.role_prompts[Role.COORDINATOR]
    assert "You are an IMPLEMENTER" in rendered.role_prompts[Role.IMPLEMENTER]
    assert "You are a SHEPHERD" in rendered.role_prompts[Role.SHEPHERD]
    assert "You are an EXPLORER" in rendered.role_prompts[Role.EXPLORER]
    assert "You are a REVIEWER" in rendered.role_prompts[Role.REVIEWER]


# --- shepherd-per-PR (ADR-0035): the fragments say the reversed design -------


def test_shepherd_prompt_scopes_to_one_pr_across_rounds():
    """ADR-0035: the shepherd owns ADDRESSING for one PR across its whole review
    life — parked between rounds — not one round per agent."""
    prompt = render(load_role_defs()).role_prompts[Role.SHEPHERD]
    assert "ONE PR" in prompt
    assert "PARKED" in prompt


def test_shepherd_prompt_carries_the_root_cause_sweep_clause():
    """ADR-0035's whack-a-mole lesson at PR scale: a valid finding is an instance
    of a CLASS, and the shepherd sweeps the PR diff for the rest of the class."""
    prompt = render(load_role_defs()).role_prompts[Role.SHEPHERD]
    assert "INSTANCE OF A CLASS" in prompt
    assert "sweep the whole PR diff" in prompt


def test_no_rendered_surface_says_fresh_shepherd_per_round():
    """The reversed design must not survive in ANY composed prompt or the union —
    the issue's residual-phrasing acceptance, pinned at the render layer."""
    rendered = render(load_role_defs())
    surfaces = [*rendered.role_prompts.values(), rendered.agents_union]
    for text in surfaces:
        lowered = text.lower()
        assert "fresh shepherd" not in lowered
        assert "one review round" not in lowered


# --- the committed derived surfaces (no drift from the source) ---------------

_ROOT = Path(__file__).resolve().parents[1]
_GENERATED = _ROOT / "src" / "shipit" / "data" / "roles" / "generated"


def test_committed_coordinator_slice_matches_render():
    """The committed coordinator slice equals what ``render`` composes now — the
    .lex/.md-mirror guarantee, applied to the generated coordinator prompt: a
    fragment edit that was not regenerated fails this test."""
    expected = render(load_role_defs()).role_prompts[Role.COORDINATOR]
    assert load_coordinator_slice() == expected


@pytest.mark.parametrize("role", list(SUBAGENT_ROLES))
def test_agent_def_files_exist_with_the_role_prompt_body(role):
    """Each subagent role has a committed agent-def whose body is its role prompt
    (frontmatter names the role); the coordinator has none (top-level session)."""
    path = _ROOT / ".claude" / "agents" / f"{role.value}.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")  # YAML frontmatter
    assert f"name: {role.value}" in text
    assert load_role_defs().overlays[role] in text  # the role prompt is the body


def test_no_coordinator_agent_def():
    """ADR-0011: the coordinator is the top-level session and has NO agent-def."""
    assert not (_ROOT / ".claude" / "agents" / "coordinator.md").exists()


# --- regeneration records (LOG03: the writes are working-tree mutations) -----


def test_regenerate_records_one_info_record_per_written_file(tmp_path, caplog):
    """Every regenerated surface carries its own durable record with the path as
    a flat field — convention-level: matched by fields, not message text."""
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


def test_main_prints_one_line_per_regenerated_file(tmp_path, capsys, monkeypatch):
    """The print stays the user-facing surface: one stdout line per written file
    (the records are ADDITIVE — the CLI output did not change shape)."""
    from shipit.harness import prompts

    monkeypatch.setattr(prompts, "regenerate", lambda: regenerate(tmp_path))
    prompts.main()
    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    written = regenerate(tmp_path)  # same inputs → same surfaces
    assert len(out_lines) == len(written)
    for path in written:
        assert any(str(path) in line for line in out_lines)
