"""Tests for the skill-scripted tier (LOG04-WS05 / ADR-0032) — planning-skill
emissions and the ``/shipit-session-status`` wrapper.

Skills are DATA, so the tier is verified by inspection over the PACKAGED skill
files — the same tree ``shipit install`` distributes
(:func:`shipit.install.units.skills_root`),
never a hand-copied fixture: each planning skill carries its emission step
with a registered event name, every ``shipit log event`` call any skill makes
names a skill-scripted event (the constrained verb would honor nothing else's
``--about``), and the session-status skill both wraps the flow view and rides
the managed set. The end-to-end leg is a planning-cycle dry run: the emit verb
through the REAL logsetup pipeline, then the reader's ``--flow`` view over the
resulting records — the intent header opens the story and the planning
milestones render (prior art: ``test_logevent``'s pipeline tests,
``test_logs``' flow tests).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from shipit import events, logread, logsetup
from shipit.identity import repo_from_slug
from shipit.install import units as install_units
from shipit.verbs import logevent, logs

REPO = repo_from_slug("acme/widget")

#: Each planning skill and the registered event names its emission steps call
#: (the issue's checkpoint map): the orchestrator states the session's purpose
#: when the overview is blessed; the grill restates it at start (a later
#: intent supersedes at read time) and records grill start + each ADR; the Spec
#: and issue skills record their own artifacts.
PLANNING_EMISSIONS = {
    "planning": {"session.intent"},
    "grill-me-with-docs": {
        "session.intent",
        "planning.grill.started",
        "planning.adr.written",
    },
    "to-spec": {"planning.spec.written"},
    "to-tickets": {"planning.epic.minted", "planning.ws.minted"},
}

#: An emit-verb call as skill prose spells it — the name is the first token
#: after the verb (options like ``--about`` follow it).
_EMIT_CALL = re.compile(r"shipit log event\s+(\S+)")


def _skill_text(name: str) -> str:
    """The packaged ``SKILL.md`` of skill ``name`` — the distributed surface."""
    return (
        install_units.skills_root()
        .joinpath(name, "SKILL.md")
        .read_text(encoding="utf-8")
    )


# ==========================================================================
# The planning skills carry their emission steps (skills are data — inspect)
# ==========================================================================


@pytest.mark.parametrize(("skill", "expected"), sorted(PLANNING_EMISSIONS.items()))
def test_planning_skill_carries_its_emission_steps(skill, expected):
    called = set(_EMIT_CALL.findall(_skill_text(skill)))
    missing = expected - called
    assert not missing, f"{skill}/SKILL.md lacks emit steps for {sorted(missing)}"


def test_every_skill_emit_call_names_a_registered_skill_scripted_event():
    """No skill can instruct an unregistered (or wrong-tier) emission: every
    ``shipit log event <name>`` across ALL packaged skills is in the closed
    vocabulary AND in the skill-scripted subset — the tier whose ``--about``
    the constrained verb honors."""
    called: set[str] = set()
    root = install_units.skills_root()
    for skill_dir in root.iterdir():
        doc = skill_dir.joinpath("SKILL.md")
        if skill_dir.is_dir() and doc.is_file():
            called.update(_EMIT_CALL.findall(doc.read_text(encoding="utf-8")))
    # The tier exists at all — the planning family is actually scripted.
    assert called >= {"session.intent", "planning.spec.written"}
    unregistered = called - events.EVENT_NAMES
    assert not unregistered, f"skills emit unregistered events: {sorted(unregistered)}"
    wrong_tier = called - events.SKILL_SCRIPTED_NAMES
    assert not wrong_tier, f"skills emit non-skill-tier events: {sorted(wrong_tier)}"


# ==========================================================================
# /shipit-session-status — wraps the flow view, rides the managed set
# ==========================================================================


def test_session_status_skill_wraps_the_flow_view():
    text = _skill_text("shipit-session-status")
    assert "shipit logs --flow --session current" in text
    assert "shipit logs --flow --epic" in text
    assert "--agent-ids" in text


def test_session_status_skill_is_in_the_managed_set():
    keys = {u.key for u in install_units.load_units()}
    assert "skills/shipit-session-status/SKILL.md" in keys


# ==========================================================================
# End-to-end: a planning-leg dry run renders as the session story
# ==========================================================================


def test_planning_leg_dry_run_renders_in_the_flow_view(tmp_path, capsys):
    """The whole tier, externally: the emission sequence a planning leg's
    skills script (Spec -> grill/ADR -> epic/WS minting) through the real
    emit verb + logging pipeline, read back with the reader the session-status
    skill wraps — the intent opens the story, every milestone renders."""
    logsetup.configure_logging(
        env={"SHIPIT_LOG_CTX_SESSION": "sess-plan"}, repo=REPO, base_dir=tmp_path
    )
    leg = [
        ("session.intent", "planning session: reviewer symmetry"),
        ("planning.spec.written", "Spec: docs/spec/reviewer-symmetry.md"),
        ("planning.grill.started", None),
        ("planning.adr.written", "ADR-0031: engine as sole requester"),
        ("planning.epic.minted", "RVW01: Reviewer symmetry (#387)"),
        ("planning.ws.minted", "RVW01-WS01: walking skeleton (#388)"),
    ]
    for name, about in leg:
        assert logevent.run(name, about=about) == 0

    rc = logs.run(
        REPO,
        query=logread.build_query(flow=True, session="sess-plan"),
        base_dir=tmp_path,
        now=lambda: datetime.now(UTC),
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    # The header IS the crystallized intent (its --about line), not a guess.
    assert out[0] == "planning session: reviewer symmetry"
    body = "\n".join(out[1:])
    # Every milestone renders: --about lines verbatim for the skill-scripted
    # names that passed one, the composed domain phrase for the one that
    # did not.
    assert "planning grill started" in body
    assert "ADR-0031: engine as sole requester" in body
    assert "Spec: docs/spec/reviewer-symmetry.md" in body
    assert "RVW01: Reviewer symmetry (#387)" in body
    assert "RVW01-WS01: walking skeleton (#388)" in body
