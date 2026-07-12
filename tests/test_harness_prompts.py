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


# --- shepherd-per-PR (ADR-0035): the fragments say the revised design --------


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


def test_shepherd_prompt_orders_addressing_by_severity_and_never_classifies():
    """ADR-0044: findings arrive pre-classified, so the shepherd's
    classification step is GONE from the generated prompt — no `pr classify`
    command, no nitpick|substantive vocabulary — replaced by
    address-in-severity-order guidance that still resolves every thread."""
    prompt = render(load_role_defs()).role_prompts[Role.SHEPHERD]
    assert "severity order" in prompt
    assert "critical, then major, then minor, then nit" in prompt
    assert "pre-classified" in prompt
    # severity orders the round's work; it never waives the minor/nit threads
    assert "still end resolved" in prompt
    assert "shipit pr classify" not in prompt
    assert "nitpick|substantive" not in prompt


def test_no_shipped_surface_instructs_classification():
    """The classify verb is the DORMANT correction path (ADR-0044): absent from
    every composed role prompt, the union, and the committed agent defs — the
    decision records alone still describe it."""
    rendered = render(load_role_defs())
    surfaces = [*rendered.role_prompts.values(), rendered.agents_union]
    for surface in surfaces:
        assert "shipit pr classify" not in surface
        assert "nitpick|substantive" not in surface


def test_no_shipped_surface_says_fresh_shepherd_per_round():
    """The previous design must not survive in ANY composed prompt, the union, OR
    the committed agent-def frontmatter — the issue's residual-phrasing
    acceptance, pinned at every surface it could regress in. The frontmatter
    `description` is added at the boundary (not by :func:`render`), so a render-
    only guard would miss a stale phrase in `_AGENT_DESCRIPTIONS`; the committed
    agent-defs (frontmatter included) are guarded here for that reason."""
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


# --- session-learning persistence (RVW02 WS05, issue #458) -------------------


def test_coordinator_prompt_carries_the_promotion_clause():
    """Issue #458: session auto-memory is keyed to the ephemeral Tree's PATH, so
    it dies with the tree. The coordinator's prompt must carry the end-of-epic /
    end-of-session promotion clause — durable learnings land in the repo before
    the session ends, never only in session memory."""
    prompt = render(load_role_defs()).role_prompts[Role.COORDINATOR]
    assert "Promoting durable learnings INTO THE REPO" in prompt
    assert "scratchpad, never an archive" in prompt
    # The clause must NAME each promotion target via its source -> destination
    # mapping, not merely mention the words: bare "ADR"/"CONTEXT.md" also appear
    # in the planning-docs bullet and the epic-topology text, so they'd pass even
    # if the clause dropped its mappings. Pin the full mapping phrases (each is
    # clause-unique); normalize the renderer's `\` escaping of `>` so the asserts
    # read as authored.
    clause = prompt.replace("\\", "")
    assert "a process rule -> the relevant role .lex" in clause
    assert "a decision -> an ADR" in clause
    assert "vocabulary -> CONTEXT.md" in clause
    assert "an open investigation -> a tracker issue" in clause


def test_promotion_clause_is_coordinator_scoped():
    """The clause is the coordinator's job (it owns the session wrap-up); no
    subagent prompt carries it — a subagent never owns end-of-session wrap-up."""
    rendered = render(load_role_defs())
    for role in SUBAGENT_ROLES:
        assert "Promoting durable learnings" not in rendered.role_prompts[role]


def test_docs_state_the_memory_orphaning_constraint_once():
    """The WHY lives in docs/dev (issue #458 acceptance): one subsection naming
    the mechanism — path-keyed session auto-memory orphaned when the ephemeral
    tree is gc'd — so a human knows why the promotion rule exists."""
    epics = (_ROOT / "docs" / "dev" / "epics.lex").read_text(encoding="utf-8")
    # "once", not merely "present": a duplicated subsection or mechanism string
    # is the regression this guards, so assert the count, not membership.
    assert epics.count("Session memory dies with the Tree") == 1
    assert epics.count("~/.claude/projects/<path-slug>/memory/") == 1  # the mechanism


# --- brief templates (RVW02 WS04): the coordinator-filled task layer ---------


@pytest.mark.parametrize("role", list(BRIEF_ROLES))
def test_brief_template_carries_every_mandatory_slot(role):
    """The four mandatory slots — issue ref, verify commands, governing docs,
    decision boundaries — ship in EVERY brief template; an edit that drops one
    fails here, so a coordinator can never be handed a slotless template."""
    template = load_brief_template(role)
    for slot in MANDATORY_BRIEF_SLOTS:
        assert slot in template


def test_shepherd_brief_also_names_its_pr_slot():
    """A shepherd is briefed cold with the PR (ADR-0035), so its template carries
    the PR slot on top of the four mandatory ones."""
    assert "{{pr}}" in load_brief_template(Role.SHEPHERD)


@pytest.mark.parametrize("role", [r for r in Role if r not in BRIEF_ROLES])
def test_roles_without_a_brief_template_are_refused(role):
    """BRIEF_ROLES is a closed set (like the role registry): asking for any other
    role's template is a loud ValueError, not a FileNotFoundError surprise."""
    with pytest.raises(ValueError, match="no brief template"):
        load_brief_template(role)


def test_brief_slots_never_leak_into_a_composed_prompt_surface():
    """The template is the coordinator-FILLED half: an unfilled ``{{slot}}``
    placeholder must never compose into a role prompt or the union — the guard
    against wiring the brief fragments into the prompt generator by accident."""
    rendered = render(load_role_defs())
    for text in [*rendered.role_prompts.values(), rendered.agents_union]:
        for slot in MANDATORY_BRIEF_SLOTS:
            assert slot not in text


def test_roles_reference_their_brief_template():
    """The anti-forget clause (issue #457): the coordinator's prompt documents the
    expansion verb, and each briefed role names its own template — so a brief
    missing a slot is flagged by the briefed agent, never silently absorbed."""
    rendered = render(load_role_defs())
    assert "shipit spawn brief" in rendered.role_prompts[Role.COORDINATOR]
    assert "shipit spawn brief implementer" in rendered.role_prompts[Role.IMPLEMENTER]
    assert "shipit spawn brief shepherd" in rendered.role_prompts[Role.SHEPHERD]


# --- structural metadata DERIVES from the Role Profile registry (RPE01-WS02) -


def test_subagent_roles_derive_from_the_profile_registry():
    """SUBAGENT_ROLES is not a hand-listed table: it IS the roles whose profile
    declares a generated agent-def, so the generated-surface set cannot drift from
    the structural source (and a Role added with generates_agent_def=True is picked
    up with no edit here)."""
    assert SUBAGENT_ROLES == tuple(
        role for role in Role if profile_for(role).generates_agent_def
    )
    # The coordinator declares no agent-def, so it is absent by construction.
    assert Role.COORDINATOR not in SUBAGENT_ROLES


def test_brief_roles_derive_from_the_profile_registry():
    """Brief availability is the profile's has_brief_template, the SAME structural
    source the `shipit spawn brief` CLI choices read — so the two cannot disagree."""
    assert BRIEF_ROLES == tuple(
        role for role in Role if profile_for(role).has_brief_template
    )


@pytest.mark.parametrize("role", list(SUBAGENT_ROLES))
def test_frontmatter_tools_posture_derives_from_enforcement_posture(role):
    """The read-only `tools` allow-list is present in the frontmatter IFF the
    role's profile posture forbids checkout mutation — the structural tool posture
    is derived from the registry, never a per-role frontmatter table, so it cannot
    disagree with the enforcement posture."""
    frontmatter = prompts._frontmatter(role)
    parsed = next(yaml.safe_load_all(frontmatter))
    read_only = not profile_for(role).enforcement.checkout_mutation
    assert parsed["name"] == role.value
    assert parsed["description"] == prompts._AGENT_DESCRIPTIONS[role]
    assert ("tools" in parsed) is read_only
    if read_only:
        assert parsed["tools"] == prompts._READ_ONLY_TOOLS


# --- a Role cannot leave prompt/brief/enforcement metadata incomplete (crit. 6)


def test_declared_agent_def_surface_cannot_ship_incomplete():
    """Every Role that DECLARES a generated agent-def must have both its
    description prose AND a committed agent-def file; every Role that does not must
    have neither. Adding a Role with generates_agent_def=True but no description
    fails HERE (KeyError-free, mechanical), never silently at generation."""
    for role in Role:
        declares = profile_for(role).generates_agent_def
        assert (role in prompts._AGENT_DESCRIPTIONS) is declares
        agent_def = _ROOT / ".claude" / "agents" / f"{role.value}.md"
        assert agent_def.exists() is declares
        if declares:
            # The generator can build complete frontmatter for it — no missing
            # description, and a posture-derived (not table-lookup) tools line.
            parsed = next(yaml.safe_load_all(prompts._frontmatter(role)))
            assert parsed["name"] == role.value
            assert parsed["description"] == prompts._AGENT_DESCRIPTIONS[role]


def test_declared_brief_surface_cannot_ship_incomplete():
    """Every Role that DECLARES a brief template loads a template carrying all
    mandatory slots; every Role that does not is a loud refusal. A Role added with
    has_brief_template=True but no template file fails HERE."""
    for role in Role:
        if profile_for(role).has_brief_template:
            template = load_brief_template(role)
            for slot in MANDATORY_BRIEF_SLOTS:
                assert slot in template
        else:
            with pytest.raises(ValueError, match="no brief template"):
                load_brief_template(role)


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
