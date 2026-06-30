"""Role-prompt generator: the reduction property + the derived surfaces.

The KEY test (ADR-0011) is the *reduction property*: a generated role prompt
contains its OWN overlay and NONE of the other roles' overlays — the mechanical
anti-drift guarantee — while the ``AGENTS.md`` union contains them all. Asserts
external behavior (the composed text, the committed files), never internal call
shapes. Mirrors the pure-core / thin-boundary split of ``test_prstate_state.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shipit.harness.prompts import (
    SUBAGENT_ROLES,
    RoleDefs,
    load_coordinator_slice,
    load_role_defs,
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
        Role.SHEPHERD: "SHEP-OVERLAY: address exactly one review round, then hand back.",
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
