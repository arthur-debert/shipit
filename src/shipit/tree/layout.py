"""``tree/layout`` — pure resolution of a Tree request into a concrete plan.

``plan(spec) -> TreePlan`` is the deep, pure heart of Tree creation: given a
:class:`TreeSpec` it resolves the three coordinates a clone needs — the **dir**
on disk, the **branch** to check out, and the **base** ref to cut it from — with
no I/O, so the truth table is unit-tested directly (Testing Decisions in the PRD).

WS01 resolved only the ``--issue`` shape (the thinnest end-to-end thread). WS02
completes the grammar — ``--epic E --ws N [--slug S]`` and ``--branch <freeform>``
(naming.lex §3) — so :func:`plan` now resolves EVERY spec shape. :class:`TreeSpec`
stays a single typed entry point: adding a shape is adding a field plus a branch
in :func:`plan`, not reshaping callers.

The three load-bearing invariants the tests pin (from the PRD):

- the **agent hash lands on the dir, never on the branch** — two sessions sharing
  one branch is fine, two Trees in one dir is not; the hash disambiguates the dir
  while the branch stays a stable, meaningful namespace;
- the **git branch form is slash-namespaced** (naming.lex §3): a work stream is
  ``EPIC/WSnn`` cut from ``origin/EPIC/umbrella``, siblings under ``refs/heads/EPIC/``
  — the umbrella name dodges the bare-``EPIC`` ref/dir collision. The plain-language
  identifier stays hyphenated (``EPIC-WSnn`` in titles/logs); only the branch slashes; and
- slug sanitization (lowercase; ``/`` ``.`` ``:`` and space → ``-``) lives HERE,
  so every shape that grows a slug gets the same normalization for free.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

#: Env override for the central root all Trees live under. Unset → the default
#: below. A module constant + env override is the whole config surface for WS01
#: (the PRD's "keep it simple"); a richer config binding can supersede it later.
CENTRAL_ROOT_ENV = "SHIPIT_TREES_ROOT"

#: The default central root: one predictable place OUTSIDE every repo, so cleanup
#: and inspection are uniform across repos and agents — never inside ``.claude/``
#: (PRD Solution; ADR-0014).
DEFAULT_CENTRAL_ROOT = "~/workspace/trees"

#: Characters a slug is normalized on: path/ref separators, dots, colons, and
#: whitespace all collapse to ``-`` so a slug is safe in both a branch ref and a
#: directory name.
_SLUG_SEP = re.compile(r"[\s/.:]+")


def central_root() -> Path:
    """The central root every Tree lives under (env override, else the default).

    Expanded (``~`` and any ``$VARS``) and guaranteed absolute. Pure read of the
    environment — no directory is created here; the orchestrator makes the leaf.

    A non-absolute ``SHIPIT_TREES_ROOT`` is rejected with :class:`ValueError`
    rather than resolved against the cwd: a relative root would place Trees under
    wherever ``shipit`` happens to be invoked from — potentially inside the source
    checkout — which breaks the central-root/isolation invariant this whole
    feature rests on (PRD Solution; ADR-0014). The default already expands to an
    absolute path.
    """
    raw = os.environ.get(CENTRAL_ROOT_ENV) or DEFAULT_CENTRAL_ROOT
    root = Path(os.path.expandvars(raw)).expanduser()
    if not root.is_absolute():
        raise ValueError(
            f"{CENTRAL_ROOT_ENV} must be an absolute path so Trees live OUTSIDE "
            f"every repo (got {raw!r}); a relative root would place Trees under the "
            "current working directory and make cleanup cwd-dependent."
        )
    return root


def sanitize_slug(slug: str) -> str:
    """Normalize a free-text slug to the lowercase ``-``-joined form used in refs.

    Lowercases, collapses every run of separator characters (whitespace, ``/``,
    ``.``, ``:``) to a single ``-``, and trims leading/trailing ``-``. An empty or
    all-separator slug normalizes to ``""``.
    """
    collapsed = _SLUG_SEP.sub("-", slug.strip().lower())
    return collapsed.strip("-")


@dataclass(frozen=True)
class TreeSpec:
    """A request to materialize a Tree — exactly one of the three shapes is set.

    ``org`` / ``repo`` namespace the dir under the central root; ``agent_hash``
    disambiguates the dir for two Trees on one branch (it never reaches the
    branch). ``root`` overrides the central root for tests; ``None`` resolves
    :func:`central_root`. ``slug`` is the optional human label applied per shape.

    The three shapes :func:`plan` dispatches on (and validates as mutually
    exclusive):

    - **epic/work stream** — ``epic`` + ``ws`` both set (``--epic E --ws N``);
    - **issue** — ``issue`` set (``--issue N``); and
    - **freeform** — ``branch`` set (``--branch <name>``).
    """

    org: str
    repo: str
    agent_hash: str
    issue: int | None = None
    epic: str | None = None
    ws: int | None = None
    branch: str | None = None
    slug: str = ""
    root: Path | None = None


@dataclass(frozen=True)
class TreePlan:
    """The resolved coordinates for one Tree: where, on what branch, from what base."""

    dir: Path
    branch: str
    base: str


def plan(spec: TreeSpec) -> TreePlan:
    """Resolve ``spec`` into a concrete :class:`TreePlan` (pure, no I/O).

    Dispatches on which of the three mutually exclusive shapes the spec carries —
    epic/work stream (``epic`` + ``ws``), issue (``issue``), or freeform
    (``branch``). Exactly one must be set: zero shapes or more than one raises
    :class:`ValueError` rather than guessing, since the dir/branch/base each shape
    resolves to are genuinely different and a silent pick would mis-place a Tree.

    A partial epic shape — only ``epic`` or only ``ws`` — also raises: a work
    stream branch ``EPIC/WSnn`` is meaningless without both halves.
    """
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
        )
        if present
    ]
    if len(shapes) != 1:
        raise ValueError(
            "tree.layout.plan: exactly one shape must be set "
            "(--epic/--ws, --issue, or --branch); "
            f"got {shapes or 'none'}"
        )

    shape = shapes[0]
    if shape == "epic":
        return _plan_epic_ws(spec)
    if shape == "issue":
        return _plan_issue(spec)
    return _plan_freeform(spec)


def _plan_epic_ws(spec: TreeSpec) -> TreePlan:
    """Resolve the ``--epic E --ws N [--slug S]`` (work stream) shape.

    - **branch**: ``E/WSnn`` — the slash-namespaced work-stream form (naming.lex
      §3), ``ws`` zero-padded to two digits (``--ws 2`` → ``WS02``). The branch
      carries neither slug nor hash; ``E`` is the human-assigned epic code, kept
      verbatim (uppercase ``THEME+NN``).
    - **base**: ``origin/E/umbrella`` — a work stream is cut from its epic's
      umbrella branch, the sibling of every ``E/WSnn`` under ``refs/heads/E/``.
    - **dir**: ``<root>/<org>/<repo>/epics/<E>/WSnn[-<slug>]-<agent-hash>`` — the
      branch path under the ``epics`` kind, with the hash on the leaf. An optional
      sanitized slug rides on the DIR only (never the canonical branch), so a Tree
      reads as ``WS02-tiling-deadbeef`` on disk while the branch stays ``E/WS02``.
    """
    assert spec.epic is not None and spec.ws is not None  # guaranteed by plan()
    ws_code = f"WS{spec.ws:02d}"
    branch = f"{spec.epic}/{ws_code}"
    base = f"origin/{spec.epic}/umbrella"
    slug = sanitize_slug(spec.slug)
    leaf = (
        f"{ws_code}-{slug}-{spec.agent_hash}"
        if slug
        else f"{ws_code}-{spec.agent_hash}"
    )
    root = spec.root if spec.root is not None else central_root()
    directory = Path(root) / spec.org / spec.repo / "epics" / spec.epic / leaf
    return TreePlan(dir=directory, branch=branch, base=base)


def _plan_freeform(spec: TreeSpec) -> TreePlan:
    """Resolve the ``--branch <freeform>`` shape.

    - **branch**: the freeform name verbatim — the caller owns its meaning, so the
      planner reflects the request rather than mangling it (naming.lex §3 lists the
      freeform name as a branch form in its own right).
    - **base**: ``origin/main`` — freeform work, like a standalone issue, is cut
      from the default branch.
    - **dir**: ``<root>/<org>/<repo>/branches/<sanitized-branch>-<agent-hash>`` —
      the freeform name is sanitized into one safe leaf (slashes and other
      separators collapse to ``-``) so an arbitrary branch like ``spike/foo`` maps
      to a flat, predictable dir; the hash keeps duplicate Trees apart.
    """
    branch = spec.branch
    assert branch is not None  # guarded by plan(); narrows the type for callers
    root = spec.root if spec.root is not None else central_root()
    leaf = f"{sanitize_slug(branch)}-{spec.agent_hash}"
    directory = Path(root) / spec.org / spec.repo / "branches" / leaf
    return TreePlan(dir=directory, branch=branch, base="origin/main")


def _plan_issue(spec: TreeSpec) -> TreePlan:
    """Resolve the ``--issue N`` shape.

    - **branch**: ``fix/<n>-<slug>`` (naming.lex §3), or bare ``fix/<n>`` when no
      slug is given — never carries the agent hash.
    - **dir**: ``<root>/<org>/<repo>/issues/<n>-<agent-hash>`` — the hash lands on
      the dir leaf, keyed by issue, so duplicate Trees never collide on disk.
    - **base**: ``origin/main`` (a standalone issue is cut from the default branch;
      a work stream's epic-branch base is WS02's concern).
    """
    slug = sanitize_slug(spec.slug)
    branch = f"fix/{spec.issue}-{slug}" if slug else f"fix/{spec.issue}"
    root = spec.root if spec.root is not None else central_root()
    directory = (
        Path(root) / spec.org / spec.repo / "issues" / f"{spec.issue}-{spec.agent_hash}"
    )
    return TreePlan(dir=directory, branch=branch, base="origin/main")
