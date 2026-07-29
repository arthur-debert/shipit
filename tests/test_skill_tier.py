from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from shipit import events, logread, logsetup
from shipit.identity import repo_from_slug
from shipit.install import units as install_units
from shipit.verbs import logevent, logs

REPO = repo_from_slug("acme/widget")

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

_EMIT_CALL = re.compile(r"shipit log event\s+(\S+)")


def _skill_text(name: str) -> str:
    return (
        install_units.skills_root()
        .joinpath(name, "SKILL.md")
        .read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(("skill", "expected"), sorted(PLANNING_EMISSIONS.items()))
def test_planning_skill_carries_its_emission_steps(skill, expected):
    called = set(_EMIT_CALL.findall(_skill_text(skill)))
    missing = expected - called
    assert not missing, f"{skill}/SKILL.md lacks emit steps for {sorted(missing)}"


def test_every_skill_emit_call_names_a_registered_skill_scripted_event():
    called: set[str] = set()
    root = install_units.skills_root()
    for skill_dir in root.iterdir():
        doc = skill_dir.joinpath("SKILL.md")
        if skill_dir.is_dir() and doc.is_file():
            called.update(_EMIT_CALL.findall(doc.read_text(encoding="utf-8")))
    assert called >= {"session.intent", "planning.spec.written"}
    unregistered = called - events.EVENT_NAMES
    assert not unregistered, f"skills emit unregistered events: {sorted(unregistered)}"
    wrong_tier = called - events.SKILL_SCRIPTED_NAMES
    assert not wrong_tier, f"skills emit non-skill-tier events: {sorted(wrong_tier)}"


def test_session_status_skill_wraps_the_flow_view():
    text = _skill_text("shipit-session-status")
    assert "shipit logs --flow --session current" in text
    assert "shipit logs --flow --epic" in text
    assert "--agent-ids" in text


def test_session_status_skill_is_in_the_managed_set():
    keys = {u.key for u in install_units.load_units()}
    assert ".agents/skills/shipit-session-status/SKILL.md" in keys
    assert not any(k.startswith(".shipit-skills/") for k in keys)
    assert not any(k.startswith(".claude/skills/") for k in keys)


def test_planning_leg_dry_run_renders_in_the_flow_view(tmp_path, capsys):
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
    assert out[0] == "planning session: reviewer symmetry"
    body = "\n".join(out[1:])
    assert "planning grill started" in body
    assert "ADR-0031: engine as sole requester" in body
    assert "Spec: docs/spec/reviewer-symmetry.md" in body
    assert "RVW01: Reviewer symmetry (#387)" in body
    assert "RVW01-WS01: walking skeleton (#388)" in body
