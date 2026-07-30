"""``tree/layout`` — pure resolution of a Tree request into a concrete plan.

``plan(spec) -> TreePlan`` resolves the dir, branch, and base for every spec shape.
See docs/adr/0074-trees-are-flat.md.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..identity import Repo


class LayoutError(ValueError):
    """The central root is misconfigured."""


CENTRAL_ROOT_ENV = "SHIPIT_TREES_ROOT"

DEFAULT_CENTRAL_ROOT = "~/workspace/trees"

EPHEMERAL_BRANCH_PREFIX = "ephemeral"

#: A backend BINARY name — antigravity's binary and agent name are both ``agy``.
_AGENT_TOKEN = re.compile(r"[a-z0-9]+")

_CREATED_TAIL = re.compile(
    r"(?P<created>\d{8}-\d{6})-"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_CREATED_FORMAT = "%Y%m%d-%H%M%S"

_CREATED_STAMP_SHAPE = re.compile(r"\d{8}-\d{6}")

#: Never a pid (reused) and never truncated (``claude --resume`` rejects a prefix).
_FULL_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

_FLAT_LEAF = re.compile(
    r"(?P<repo>.+?)-"
    r"(?P<agent>[a-z0-9]+)-"
    r"(?P<created>\d{8}-\d{6})-"
    r"(?P<tree_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

#: Finds leaf CANDIDATES inside a larger text; :func:`parse_flat_leaf` stays the
#: recognizer. ``repo`` is the alphabet a repo NAME may contain, so any other
#: character ends the segment: a leaf inside a path is found whole rather than
#: starting mid-directory, and one preceded by shell punctuation (`>x`, `[x`,
#: `,x`) starts AT the leaf. An allow-list, not a list of characters to exclude,
#: because the excluded set is unbounded and each miss silently shifts `start`.
_FLAT_LEAF_IN_TEXT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*?-"
    r"[a-z0-9]+-"
    r"\d{8}-\d{6}-"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def is_full_uuid(value: object) -> bool:
    return isinstance(value, str) and _FULL_UUID.fullmatch(value) is not None


def is_created_stamp(value: object) -> bool:
    """Whether ``value`` is a real ``%Y%m%d-%H%M%S`` UTC calendar time, not just its shape."""
    if not isinstance(value, str) or not _CREATED_STAMP_SHAPE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, _CREATED_FORMAT)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class FlatLeaf:
    repo: str
    agent: str
    created: str
    tree_id: str

    @property
    def name(self) -> str:
        """The leaf directory name these slots compose."""
        return f"{self.repo}-{self.agent}-{self.created}-{self.tree_id}"


@dataclass(frozen=True)
class FlatLeafMention:
    """A flat Tree leaf found inside a larger text, and where it starts in it."""

    leaf: FlatLeaf
    start: int


def find_flat_leaves(text: object) -> tuple[FlatLeafMention, ...]:
    """Every flat Tree leaf named anywhere in ``text`` — bare, quoted, or inside a path.

    Text matching: a leaf named in a comment or a string literal reads the same
    as one named as a path.
    """
    if not isinstance(text, str):
        return ()
    return tuple(
        FlatLeafMention(leaf=leaf, start=match.start())
        for match in _FLAT_LEAF_IN_TEXT.finditer(text)
        if (leaf := parse_flat_leaf(match.group(0))) is not None
    )


def parse_flat_leaf(name: object) -> FlatLeaf | None:
    """The recognizer of "is this directory a flat Tree?" — ``None`` when it is not."""
    if not isinstance(name, str):
        return None
    match = _FLAT_LEAF.fullmatch(name)
    if match is None:
        return None
    created = match.group("created")
    if not is_created_stamp(created):
        return None
    return FlatLeaf(
        repo=match.group("repo"),
        agent=match.group("agent"),
        created=created,
        tree_id=match.group("tree_id"),
    )


def created_from_leaf(name: str) -> str | None:
    match = _CREATED_TAIL.search(name)
    return match.group("created") if match else None


def tree_leaf(repo: Repo, agent: str, created: str, tree_id: str) -> str:
    """``<repo>-<agent>-<timestamp>-<id>``; raises :class:`ValueError` on a bad slot."""
    if not isinstance(agent, str) or not _AGENT_TOKEN.fullmatch(agent):
        raise ValueError(
            "tree.layout.tree_leaf: agent must be a lowercase alphanumeric backend "
            f"binary name (claude/codex/agy, naming.lex §4); got {agent!r}."
        )
    if not is_created_stamp(created):
        raise ValueError(
            "tree.layout.tree_leaf: created must be a strict %Y%m%d-%H%M%S UTC stamp "
            f"(ADR-0074 / naming.lex §4); got {created!r}."
        )
    if not is_full_uuid(tree_id):
        raise ValueError(
            "tree.layout.tree_leaf: tree_id must be a full UUID (never a pid or a "
            f"truncated prefix; ADR-0074 / naming.lex §4); got {tree_id!r}."
        )
    return f"{repo.name}-{agent}-{created}-{tree_id}"


_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")

_EPIC_CODE = re.compile(r"[A-Za-z0-9]+")


def epic_umbrella_base(epic: str) -> str:
    """``origin/<epic>/umbrella`` — the base a work stream Tree is cut from."""
    if not isinstance(epic, str) or not _EPIC_CODE.fullmatch(epic):
        raise ValueError(
            "tree.layout.epic_umbrella_base: epic code must be a single alphanumeric "
            f"token (naming.lex §3 THEME+NN, e.g. 'HAR02'); got {epic!r}."
        )
    return f"origin/{epic}/umbrella"


def central_root() -> Path:
    """The absolute, expanded central root; a relative override raises :class:`LayoutError`."""
    raw = os.environ.get(CENTRAL_ROOT_ENV) or DEFAULT_CENTRAL_ROOT
    root = Path(os.path.expandvars(raw)).expanduser()
    if not root.is_absolute():
        raise LayoutError(
            f"{CENTRAL_ROOT_ENV} must be an absolute path so Trees live OUTSIDE "
            f"every repo (got {raw!r}); a relative root would place Trees under the "
            "current working directory and make cleanup cwd-dependent."
        )
    return root


def sanitize_slug(slug: str) -> str:
    """Allow-list free text to ``[a-z0-9-]``, a valid git ref component; may return ``""``."""
    collapsed = _SLUG_UNSAFE.sub("-", slug.strip().lower())
    return collapsed.strip("-")


def issue_branch(issue: int, session: str) -> str:
    """``issues/<id>/<session>`` — never a bare ``issues/<id>``, which blocks siblings."""
    if not isinstance(issue, int) or issue < 1:
        raise ValueError(
            "tree.layout.issue_branch: issue number must be a positive integer "
            f"(the issues/<id>/<session> grammar, naming.lex §3); got issue={issue!r}. "
            "Zero or negative values produce out-of-grammar branches like "
            "'issues/0/work'."
        )
    if not isinstance(session, str):
        raise ValueError(
            "tree.layout.issue_branch: session must be a string "
            f"(the issues/<id>/<session> grammar, naming.lex §3); got {session!r}."
        )
    normalized = sanitize_slug(session)
    if not normalized:
        raise ValueError(
            "tree.layout.issue_branch: session must contain at least one alphanumeric "
            f"character (it becomes the issues/<id>/<session> ref/dir leaf); got "
            f"{session!r}, which sanitizes to an empty name — a bare 'issues/<id>/' ref "
            "that reintroduces the file-vs-directory collision the session suffix dodges."
        )
    return f"issues/{issue}/{normalized}"


def work_stream_branch(epic: str, ws: int) -> str:
    """``<epic>/WSnn``, zero-padded."""
    if not isinstance(epic, str) or not _EPIC_CODE.fullmatch(epic):
        raise ValueError(
            "tree.layout.work_stream_branch: epic code must be a single alphanumeric "
            f"token (naming.lex §3 THEME+NN, e.g. 'HAR02'); got {epic!r}. The code "
            "becomes both a branch ref and a path segment, so empty/whitespace values "
            "and separators or '..' (a path-traversal risk) are rejected."
        )
    if ws < 1:
        raise ValueError(
            "tree.layout.work_stream_branch: work stream number must be a positive "
            f"integer (the WSnn grammar, naming.lex §3); got ws={ws!r}. Zero or negative "
            "values produce out-of-grammar branches like 'WS00'/'WS-1'."
        )
    return f"{epic}/WS{ws:02d}"


def ephemeral_branch(session_id: str) -> str:
    """``ephemeral/<id>`` — a birth branch only; the session moves it to the real work."""
    if not isinstance(session_id, str):
        raise ValueError(
            "tree.layout.ephemeral_branch: session id must be a string "
            f"(the ephemeral/<id> grammar, ADR-0027); got {session_id!r}."
        )
    normalized = sanitize_slug(session_id)
    if not normalized:
        raise ValueError(
            "tree.layout.ephemeral_branch: session id must contain at least one "
            f"alphanumeric character (it becomes the ephemeral/<id> ref AND the dir "
            f"leaf); got {session_id!r}, which sanitizes to an empty name — a bare "
            "'ephemeral/' ref and a leaf-less dir."
        )
    return f"{EPHEMERAL_BRANCH_PREFIX}/{normalized}"


@dataclass(frozen=True)
class TreeSpec:
    """A Tree request: exactly one of ``epic``+``ws`` / ``issue`` / ``branch`` / ``ephemeral``."""

    repo: Repo
    agent: str
    created: str
    tree_id: str
    issue: int | None = None
    epic: str | None = None
    ws: int | None = None
    branch: str | None = None
    base: str | None = None
    ephemeral: str | None = None
    slug: str = ""
    session: str = "work"
    root: Path | None = None


@dataclass(frozen=True)
class TreePlan:
    dir: Path
    branch: str
    base: str


def tree_dir(
    repo: Repo, agent: str, created: str, tree_id: str, root: Path | None = None
) -> Path:
    """``<root>/<leaf>``; ``root`` ``None`` resolves :func:`central_root`."""
    base_root = root if root is not None else central_root()
    return Path(base_root) / tree_leaf(repo, agent, created, tree_id)


def _tree_dir(spec: TreeSpec) -> Path:
    return tree_dir(spec.repo, spec.agent, spec.created, spec.tree_id, spec.root)


def plan(spec: TreeSpec) -> TreePlan:
    """Resolve ``spec`` into a concrete :class:`TreePlan`. Pure, no I/O."""
    has_epic = spec.epic is not None or spec.ws is not None
    if has_epic and (spec.epic is None or spec.ws is None):
        raise ValueError(
            "tree.layout.plan: the epic shape needs both --epic and --ws "
            f"(got epic={spec.epic!r}, ws={spec.ws!r})"
        )

    shapes = [
        name
        for name, present in (
            ("epic", has_epic),
            ("issue", spec.issue is not None),
            ("branch", spec.branch is not None),
            ("ephemeral", spec.ephemeral is not None),
        )
        if present
    ]
    if len(shapes) != 1:
        raise ValueError(
            "tree.layout.plan: exactly one shape must be set "
            "(--epic/--ws, --issue, --branch, or ephemeral); "
            f"got {shapes or 'none'}"
        )

    shape = shapes[0]
    if shape == "epic":
        return _plan_epic_ws(spec)
    if shape == "issue":
        return _plan_issue(spec)
    if shape == "ephemeral":
        return _plan_ephemeral(spec)
    return _plan_freeform(spec)


def _plan_epic_ws(spec: TreeSpec) -> TreePlan:
    """The work stream shape: ``E/WSnn`` cut from ``origin/E/umbrella``."""
    assert spec.epic is not None and spec.ws is not None
    branch = work_stream_branch(spec.epic, spec.ws)
    base = epic_umbrella_base(spec.epic)
    return TreePlan(dir=_tree_dir(spec), branch=branch, base=base)


def _plan_freeform(spec: TreeSpec) -> TreePlan:
    """The freeform shape: the branch name verbatim, cut from ``spec.base`` or ``origin/main``."""
    branch = spec.branch
    assert branch is not None
    sanitized = sanitize_slug(branch)
    if not sanitized:
        raise ValueError(
            "tree.layout.plan: freeform --branch must contain at least one "
            f"alphanumeric character (it becomes the branch ref); got {branch!r}, "
            "which sanitizes to an empty name — an unusable empty branch."
        )
    if spec.base is not None and not spec.base.strip():
        raise ValueError(
            "tree.layout.plan: freeform base override must not be empty; "
            "omit it to use origin/main"
        )
    base = spec.base.strip() if spec.base is not None else "origin/main"
    return TreePlan(dir=_tree_dir(spec), branch=branch, base=base)


def _plan_issue(spec: TreeSpec) -> TreePlan:
    """The standalone-issue shape: ``issues/<id>/<session>`` cut from ``origin/main``."""
    assert spec.issue is not None
    branch = issue_branch(spec.issue, spec.session)
    return TreePlan(dir=_tree_dir(spec), branch=branch, base="origin/main")


def _plan_ephemeral(spec: TreeSpec) -> TreePlan:
    """The session-Tree shape: ``ephemeral/<id>`` cut from ``origin/main``."""
    assert spec.ephemeral is not None
    branch = ephemeral_branch(spec.ephemeral)
    return TreePlan(dir=_tree_dir(spec), branch=branch, base="origin/main")
