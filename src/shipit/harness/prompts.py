"""``harness/prompts`` — compose the lex role fragments into the per-role prompts.

See docs/adr/0011-role-scoped-generated-prompts.md.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .role import Role
from .roleprofile import profile_for

logger = logging.getLogger("shipit.harness")

#: Every role whose Role Profile declares a generated agent-def.
SUBAGENT_ROLES: tuple[Role, ...] = tuple(
    role for role in Role if profile_for(role).generates_agent_def
)

#: Every role with a bundled `<role>-brief.lex` template.
BRIEF_ROLES: tuple[Role, ...] = tuple(
    role for role in Role if profile_for(role).has_brief_template
)

#: The literal `{{slot}}` placeholders the coordinator fills; every template ships all four.
MANDATORY_BRIEF_SLOTS: tuple[str, ...] = (
    "{{issue}}",
    "{{verify-commands}}",
    "{{governing-docs}}",
    "{{decision-boundaries}}",
)

_BASE_HEADING = "Dev cycle"
_ROLE_HEADING = "Your role"
_ROLEMAP_HEADING = "The roles you delegate to"

GENERATED_COMMENT = (
    "<!-- Generated from src/shipit/data/roles/ by `pixi run regen-roles` "
    "(shipit.harness.prompts). Do not hand edit — edit the .lex fragments and "
    "regenerate. -->"
)

_ROLES_PKG = "shipit.data"
_ROLES_REL = ("roles",)
_GENERATED_REL = ("roles", "generated")
_COORDINATOR_SLICE_NAME = "coordinator-prompt.md"
_UNION_NAME = "agents-union.md"

#: The tool allow-list emitted for a role whose posture forbids checkout mutation.
_READ_ONLY_TOOLS = "Read, Grep, Glob, Bash"

#: The "when to use" line shown in the agent picker; every subagent role needs one.
_AGENT_DESCRIPTIONS: dict[Role, str] = {
    Role.IMPLEMENTER: (
        "Implements one unit of work with tests and opens a single draft PR, "
        "then stops at PR-open. Use to build a change; not for review rounds."
    ),
    Role.SHEPHERD: (
        "Owns addressing for one PR across its review rounds; parked "
        "between rounds. Use one per PR: briefed cold with the PR number on "
        "round 1, resume the SAME agent for later rounds."
    ),
    Role.EXPLORER: (
        "Read-only, search-scoped investigator: searches and reports "
        "findings, mutates nothing. Use to answer a question about the code."
    ),
    Role.REVIEWER: (
        "Read-only, branch-pinned reviewer: reads a PR head in a shared "
        "read-only Tree and emits one structured review result for shipit to "
        "post, mutates nothing. Use to review a PR."
    ),
}


@dataclass(frozen=True)
class RoleDefs:
    """The already-read fragment bodies, stripped of autogen comment and ``# Title``."""

    base: str
    role_map: str
    overlays: dict[Role, str]


@dataclass(frozen=True)
class RenderedPrompts:
    """The generator's output: one prompt per role, plus the non-binding union reference."""

    role_prompts: dict[Role, str]
    agents_union: str


def _section(heading: str, body: str) -> str:
    return f"## {heading}\n\n{body.strip()}"


def render(defs: RoleDefs) -> RenderedPrompts:
    """Compose the fragments: each prompt is ``base + that role's overlay`` and nothing else."""
    role_prompts: dict[Role, str] = {}
    for role in Role:
        parts = [
            _section(_BASE_HEADING, defs.base),
            _section(_ROLE_HEADING, defs.overlays[role]),
        ]
        if role is Role.COORDINATOR:
            parts.append(_section(_ROLEMAP_HEADING, defs.role_map))
        role_prompts[role] = "\n\n".join(parts)

    union_parts = [_section(_BASE_HEADING, defs.base)]
    for role in Role:
        union_parts.append(_section(f"Role: {role.value}", defs.overlays[role]))
    union_parts.append(_section("Role map", defs.role_map))

    return RenderedPrompts(
        role_prompts=role_prompts,
        agents_union="\n\n".join(union_parts),
    )


_PREAMBLE_RE = re.compile(r"^<!--.*?-->\s*", re.DOTALL)
_H1_RE = re.compile(r"^#[^\n]*\n", re.MULTILINE)


def _fragment_body(markdown: str) -> str:
    """The fragment body: leading preamble comment and ``# Title`` removed."""
    text = _PREAMBLE_RE.sub("", markdown, count=1).lstrip()
    text = _H1_RE.sub("", text, count=1)
    return text.strip()


def _read_fragment(name: str) -> str:
    raw = (
        resources.files(_ROLES_PKG)
        .joinpath(*_ROLES_REL, name)
        .read_text(encoding="utf-8")
    )
    return _fragment_body(raw)


def load_role_defs() -> RoleDefs:
    """Read the rendered ``.md`` mirrors (never the raw ``.lex``) into a :class:`RoleDefs`."""
    return RoleDefs(
        base=_read_fragment("_base.md"),
        role_map=_read_fragment("_rolemap.md"),
        overlays={role: _read_fragment(f"{role.value}.md") for role in Role},
    )


def load_brief_template(role: Role) -> str:
    """Read the bundled brief template; a role outside :data:`BRIEF_ROLES` raises."""
    if role not in BRIEF_ROLES:
        raise ValueError(
            f"no brief template for role {role.value!r} — "
            f"briefed roles: {', '.join(r.value for r in BRIEF_ROLES)}"
        )
    return _read_fragment(f"{role.value}-brief.md")


def _strip_generated_comment(markdown: str) -> str:
    """Drop the leading generated banner from a derived file, leaving the bare prompt."""
    return _PREAMBLE_RE.sub("", markdown, count=1).strip()


def load_coordinator_slice() -> str:
    """Read the COMMITTED coordinator slice, banner stripped — never a recomposition."""
    raw = (
        resources.files(_ROLES_PKG)
        .joinpath(*_GENERATED_REL, _COORDINATOR_SLICE_NAME)
        .read_text(encoding="utf-8")
    )
    return _strip_generated_comment(raw)


def _frontmatter(role: Role) -> str:
    """The agent-def frontmatter; ``tools`` is emitted only for a non-mutating posture."""
    lines = [
        "---",
        f"name: {role.value}",
        f"description: {json.dumps(_AGENT_DESCRIPTIONS[role])}",
    ]
    if not profile_for(role).enforcement.checkout_mutation:
        lines.append(f"tools: {_READ_ONLY_TOOLS}")
    lines.append("---")
    return "\n".join(lines)


def _agent_def(role: Role, prompt: str) -> str:
    return f"{_frontmatter(role)}\n\n{GENERATED_COMMENT}\n\n{prompt}\n"


#: Where AGY reads a ``--agent reviewer`` def from, relative to a checkout.
AGY_REVIEWER_DEF_REL = (".agents", "agents", Role.REVIEWER.value, "agent.md")


def _agy_frontmatter(role: Role) -> str:
    """The AGY custom-agent frontmatter: only ``name`` / ``description``, never ``tools``."""
    return "\n".join(
        [
            "---",
            f"name: {role.value}",
            f"description: {json.dumps(_AGENT_DESCRIPTIONS[role])}",
            "---",
        ]
    )


def _agy_agent_def(role: Role, overlay: str) -> str:
    """An AGY custom-agent def: frontmatter + banner + the role's overlay ALONE, no base."""
    return f"{_agy_frontmatter(role)}\n\n{GENERATED_COMMENT}\n\n{overlay.strip()}\n"


def _generated_doc(prompt: str) -> str:
    return f"{GENERATED_COMMENT}\n\n{prompt}\n"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def regenerate(repo_root: Path | None = None) -> list[Path]:
    """Regenerate every derived surface, from ALREADY-CURRENT ``.md`` mirrors."""
    root = repo_root if repo_root is not None else _repo_root()
    defs = load_role_defs()
    rendered = render(defs)
    written: list[Path] = []

    def _write_surface(dest: Path, text: str) -> None:
        dest.write_text(text, encoding="utf-8")
        logger.info("role surface regenerated at %s", dest, extra={"path": str(dest)})
        written.append(dest)

    agents_dir = root / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for role in SUBAGENT_ROLES:
        dest = agents_dir / f"{role.value}.md"
        _write_surface(dest, _agent_def(role, rendered.role_prompts[role]))

    agy_reviewer_dest = root.joinpath(*AGY_REVIEWER_DEF_REL)
    agy_reviewer_dest.parent.mkdir(parents=True, exist_ok=True)
    _write_surface(
        agy_reviewer_dest,
        _agy_agent_def(Role.REVIEWER, defs.overlays[Role.REVIEWER]),
    )

    generated_dir = root / "src" / "shipit" / "data" / "roles" / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)

    _write_surface(
        generated_dir / _COORDINATOR_SLICE_NAME,
        _generated_doc(rendered.role_prompts[Role.COORDINATOR]),
    )
    _write_surface(generated_dir / _UNION_NAME, _generated_doc(rendered.agents_union))

    logger.info(
        "role surfaces regenerated: %d file(s)",
        len(written),
        extra={"files": len(written)},
    )
    return written


def main() -> None:
    for path in regenerate():
        print(f"regenerated {path}")


if __name__ == "__main__":
    main()
