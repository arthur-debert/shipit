"""Role-prompt generator — compose the lex fragments into per-role prompts.

ADR-0011: role behavior has a SINGLE sliceable source — a shared dev-cycle
*base* fragment plus one *overlay* per **role** (plus the coordinator's *role
map*) — authored as focused lex under ``shipit.data.roles`` and mirrored to
``.md``. From that source we **generate** a reduced **role prompt** per role:
``base + that role's overlay only`` (the ``coordinator``'s also carries the role
map — it is the one broad slice). One edit to a fragment re-flows every derived
surface, so the dev cycle is stated once and the role prompts, the coordinator
deny reason, and the ``AGENTS.md`` reference can never disagree.

Pure core / thin boundary (the ``prstate`` shape):

  - :func:`render` is the PURE core — a function from a :class:`RoleDefs`
    (already-read fragment text) to the per-role prompts and the ``AGENTS.md``
    *union* (base + ALL overlays — the non-binding reference). No I/O, so the
    *reduction property* (a role prompt contains its OWN overlay and none of the
    others') is unit-testable on plain strings.
  - :func:`load_role_defs` / :func:`regenerate` are the BOUNDARY — they read the
    bundled fragment ``.md`` mirrors and write the derived surfaces (the subagent
    agent-defs, the bundled coordinator slice, the union reference).

lexd does NOT support includes, so the base+overlay COMPOSITION is done here in
Python (read each fragment, assemble), never by a lex include directive. Each
fragment is still authored in lex and mirrored to ``.md`` (the rendered prose the
generator reads); the leading autogen comment + the ``# Title`` H1 are stripped so
only the body composes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .role import Role
from .roleprofile import profile_for

#: The harness subsystem's logger. Regeneration mutates the working tree (the
#: agent-defs, the bundled generated docs), so each write gets a durable record
#: (LOG03) — the ``regenerated <path>`` print stays as the user-facing surface.
logger = logging.getLogger("shipit.harness")

#: The subagent roles — every role whose Role Profile DECLARES a generated
#: agent-def (``generates_agent_def``), DERIVED from the registry rather than
#: re-listed here (RPE01-WS02), so the set can never drift from the structural
#: source. Today that is every role except the coordinator, which is the
#: top-level session and has no agent-def (ADR-0011): its prompt rides the
#: injected context + the PreToolUse deny reason instead. Enum order is stable,
#: so the derived tuple keeps its historical ordering.
SUBAGENT_ROLES: tuple[Role, ...] = tuple(
    role for role in Role if profile_for(role).generates_agent_def
)

#: The roles whose spawn/cold briefs have a bundled BRIEF TEMPLATE (RVW02): the
#: task-specific half the coordinator fills at spawn/brief time — the general
#: half is the role prompt composed below. Authored as `<role>-brief.lex` beside
#: the role fragments (mirrored to `.md` by the same regen pipeline), read back
#: via :func:`load_brief_template`, printed by `shipit spawn brief <role>`. DERIVED
#: from each Role Profile's ``has_brief_template`` (RPE01-WS02) — the same
#: structural source the `spawn brief` CLI choices read — so brief availability
#: cannot drift from the registry.
BRIEF_ROLES: tuple[Role, ...] = tuple(
    role for role in Role if profile_for(role).has_brief_template
)

#: The MANDATORY brief slots — literal `{{slot}}` placeholders the coordinator
#: replaces (plain textual fill; the pipeline carries no template engine and
#: does not need one). Every brief template ships all four; the test suite pins
#: that, so a template edit cannot silently drop a slot.
MANDATORY_BRIEF_SLOTS: tuple[str, ...] = (
    "{{issue}}",
    "{{verify-commands}}",
    "{{governing-docs}}",
    "{{decision-boundaries}}",
)

#: Section headings the generator injects so the composed markdown is well-formed
#: regardless of the fragments (the fragments themselves are heading-free prose +
#: lists). Kept as constants because the deny-reason / agent-def assertions key
#: off them (e.g. the role-map marker in the coordinator slice).
_BASE_HEADING = "Dev cycle"
_ROLE_HEADING = "Your role"
_ROLEMAP_HEADING = "The roles you delegate to"

#: The "do not hand edit" banner on every GENERATED surface (the agent-defs, the
#: coordinator slice, the union) — the analogue of the lex→md mirror preamble, so
#: a hand edit to a derived file is obviously wrong and reconciles on regenerate.
GENERATED_COMMENT = (
    "<!-- Generated from src/shipit/data/roles/ by `pixi run regen-roles` "
    "(shipit.harness.prompts). Do not hand edit — edit the .lex fragments and "
    "regenerate. -->"
)

#: The bundled fragment package and the generated-output subdir within it.
_ROLES_PKG = "shipit.data"
_ROLES_REL = ("roles",)
_GENERATED_REL = ("roles", "generated")
_COORDINATOR_SLICE_NAME = "coordinator-prompt.md"
_UNION_NAME = "agents-union.md"

#: The read-only agent-def tool allow-list — the exact tools Claude Code grants a
#: role whose profile posture forbids checkout mutation (no `Write`/`Edit`), so a
#: read-only role's posture rides the tool allow-list, not just the prompt. A role
#: gets this line IFF ``not profile.enforcement.checkout_mutation`` (RPE01-WS02):
#: the structural tool posture DERIVES from the Role Profile, never a per-role
#: table, so it cannot disagree with the registry's enforcement posture.
_READ_ONLY_TOOLS = "Read, Grep, Glob, Bash"

#: Per-subagent-role agent-def DESCRIPTION — the "when to use" line Claude Code
#: shows in the agent picker. This is behavioral prose (the one authored field in
#: the frontmatter); the STRUCTURAL tool posture is derived from the Role Profile
#: in :func:`_frontmatter`, not stored here. Every :data:`SUBAGENT_ROLES` role
#: must have an entry — a test pins that, so a Role declaring a generated agent-def
#: can never ship without its description (RPE01-WS02).
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
        "read-only Tree and posts one review, mutates nothing. Use to review "
        "a PR."
    ),
}


@dataclass(frozen=True)
class RoleDefs:
    """The role-definition source — the already-read fragment bodies.

    ``base`` is the shared dev cycle; ``overlays`` maps EACH role (including the
    coordinator) to its scoped marching orders; ``role_map`` is the one-line-per-
    role map the coordinator carries. All are the stripped prose bodies (no
    autogen comment, no ``# Title`` H1) so :func:`render` composes them verbatim.
    """

    base: str
    role_map: str
    overlays: dict[Role, str]


@dataclass(frozen=True)
class RenderedPrompts:
    """The generator's output: the per-role prompts + the ``AGENTS.md`` union.

    ``role_prompts`` carries ALL four roles (the coordinator's is the deny
    reason / injected context; the three subagents' are their agent-def bodies).
    ``agents_union`` is base + every overlay + the role map — the NON-binding
    reference the reduction-property test asserts contains all overlays.
    """

    role_prompts: dict[Role, str]
    agents_union: str


def _section(heading: str, body: str) -> str:
    """One ``## heading`` + body block (markdownlint-clean: blank-separated)."""
    return f"## {heading}\n\n{body.strip()}"


def render(defs: RoleDefs) -> RenderedPrompts:
    """Compose the fragments into the per-role prompts + the union. PURE.

    Each role prompt is ``base + that role's overlay`` and nothing else — the
    coordinator's ALSO appends the role map, the one broad slice. The union is
    ``base + every overlay + role map``. The reduction property falls straight out
    of this shape: a role prompt embeds only its own overlay body, so it cannot
    carry another role's marching orders (the anti-drift guarantee), while the
    union embeds them all.
    """
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


# --------------------------------------------------------------------------
# Boundary — read the bundled fragments, write the derived surfaces
# --------------------------------------------------------------------------

_PREAMBLE_RE = re.compile(r"^<!--.*?-->\s*", re.DOTALL)
_H1_RE = re.compile(r"^#[^\n]*\n", re.MULTILINE)


def _fragment_body(markdown: str) -> str:
    """The prose body of a fragment ``.md`` mirror — preamble + ``# Title`` removed.

    The lex→md mirror is ``<!-- autogen -->`` + ``# Title`` + body; only the body
    composes into a prompt, so strip the leading comment and the single leading
    H1. Idempotent and tolerant of leading blank lines.
    """
    text = _PREAMBLE_RE.sub("", markdown, count=1).lstrip()
    text = _H1_RE.sub("", text, count=1)
    return text.strip()


def _read_fragment(name: str) -> str:
    """Read one bundled fragment ``.md`` mirror and return its composed body."""
    raw = (
        resources.files(_ROLES_PKG)
        .joinpath(*_ROLES_REL, name)
        .read_text(encoding="utf-8")
    )
    return _fragment_body(raw)


def load_role_defs() -> RoleDefs:
    """Read the bundled fragment mirrors into a :class:`RoleDefs`. BOUNDARY.

    Reads the rendered ``.md`` prose (not the raw ``.lex``) so prompts carry no
    lex markup. The fragment file names are fixed by the closed role registry.
    """
    return RoleDefs(
        base=_read_fragment("_base.md"),
        role_map=_read_fragment("_rolemap.md"),
        overlays={role: _read_fragment(f"{role.value}.md") for role in Role},
    )


def load_brief_template(role: Role) -> str:
    """Read the bundled BRIEF TEMPLATE for ``role``. BOUNDARY.

    The task-specific half of a spawn/cold brief (RVW02): the coordinator prints
    it (``shipit spawn brief <role>``), fills every ``{{slot}}``
    (:data:`MANDATORY_BRIEF_SLOTS`), and hands the expanded skeleton to the Run.
    Reads the fragment's rendered ``.md`` mirror like every other fragment, so
    the template carries no lex markup. Only :data:`BRIEF_ROLES` have one —
    any other role is a :class:`ValueError` (a closed set, like the registry).
    """
    if role not in BRIEF_ROLES:
        raise ValueError(
            f"no brief template for role {role.value!r} — "
            f"briefed roles: {', '.join(r.value for r in BRIEF_ROLES)}"
        )
    return _read_fragment(f"{role.value}-brief.md")


def _strip_generated_comment(markdown: str) -> str:
    """Drop the leading generated banner from a derived file → the bare prompt.

    The committed coordinator slice carries the :data:`GENERATED_COMMENT` so a
    hand edit is obviously wrong; the deny reason / injected context wants only
    the prompt prose, so the banner is stripped on read.
    """
    return _PREAMBLE_RE.sub("", markdown, count=1).strip()


def load_coordinator_slice() -> str:
    """Read the COMMITTED coordinator slice (the deny reason / injected context).

    The single source of truth for ``policy.COORDINATOR_DENY_REASON``: the bundled
    generated file, banner stripped. Reading the committed artifact (rather than
    recomposing) means the deny wall is byte-identical to what was reviewed and
    committed — the same guarantee the lex→md mirror gives.
    """
    raw = (
        resources.files(_ROLES_PKG)
        .joinpath(*_GENERATED_REL, _COORDINATOR_SLICE_NAME)
        .read_text(encoding="utf-8")
    )
    return _strip_generated_comment(raw)


def _frontmatter(role: Role) -> str:
    """The YAML agent-def frontmatter block for a subagent ``role``.

    ``name`` and ``description`` are always emitted; the ``tools`` allow-list is
    emitted IFF the role's Role Profile posture forbids checkout mutation — the
    structural read-only posture DERIVES from the registry (RPE01-WS02), so the
    generated frontmatter and the enforcement posture cannot drift apart.
    """
    lines = [
        "---",
        f"name: {role.value}",
        f"description: {_AGENT_DESCRIPTIONS[role]}",
    ]
    if not profile_for(role).enforcement.checkout_mutation:
        lines.append(f"tools: {_READ_ONLY_TOOLS}")
    lines.append("---")
    return "\n".join(lines)


def _agent_def(role: Role, prompt: str) -> str:
    """A subagent agent-def file body: frontmatter + banner + the role prompt."""
    return f"{_frontmatter(role)}\n\n{GENERATED_COMMENT}\n\n{prompt}\n"


def _generated_doc(prompt: str) -> str:
    """A bundled generated markdown doc: banner + the composed prompt."""
    return f"{GENERATED_COMMENT}\n\n{prompt}\n"


def _repo_root() -> Path:
    """The repo root in an editable checkout (src/shipit/harness/prompts.py)."""
    return Path(__file__).resolve().parents[3]


def regenerate(repo_root: Path | None = None) -> list[Path]:
    """Regenerate every derived surface from the fragments. BOUNDARY.

    Writes, and returns the paths of: the three subagent agent-defs
    (``.claude/agents/<role>.md``), the bundled coordinator slice
    (``src/shipit/data/roles/generated/coordinator-prompt.md``, read at import by
    ``policy``), and the union reference
    (``src/shipit/data/roles/generated/agents-union.md``). The fragment ``.md``
    mirrors must be current first (``tools/lex-convert-doc.sh`` / the pre-commit
    mirror step); ``tools/regen-roles.sh`` chains both.
    """
    root = repo_root if repo_root is not None else _repo_root()
    rendered = render(load_role_defs())
    written: list[Path] = []

    def _write_surface(dest: Path, text: str) -> None:
        # The one write seam: every regenerated surface lands through here, so
        # each working-tree mutation carries its own record (path as a field).
        dest.write_text(text, encoding="utf-8")
        logger.info("role surface regenerated at %s", dest, extra={"path": str(dest)})
        written.append(dest)

    agents_dir = root / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for role in SUBAGENT_ROLES:
        dest = agents_dir / f"{role.value}.md"
        _write_surface(dest, _agent_def(role, rendered.role_prompts[role]))

    generated_dir = root / "src" / "shipit" / "data" / "roles" / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)

    _write_surface(
        generated_dir / _COORDINATOR_SLICE_NAME,
        _generated_doc(rendered.role_prompts[Role.COORDINATOR]),
    )
    _write_surface(generated_dir / _UNION_NAME, _generated_doc(rendered.agents_union))

    # The regeneration milestone: how many derived surfaces are now on disk.
    logger.info(
        "role surfaces regenerated: %d file(s)",
        len(written),
        extra={"files": len(written)},
    )
    return written


def main() -> None:
    """``python -m shipit.harness.prompts`` — regenerate and report what changed."""
    for path in regenerate():
        print(f"regenerated {path}")


if __name__ == "__main__":
    main()
