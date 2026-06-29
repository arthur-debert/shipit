"""``tree/layout`` — pure resolution of a Tree request into a concrete plan.

``plan(spec) -> TreePlan`` is the deep, pure heart of Tree creation: given a
:class:`TreeSpec` it resolves the three coordinates a clone needs — the **dir**
on disk, the **branch** to check out, and the **base** ref to cut it from — with
no I/O, so the truth table is unit-tested directly (Testing Decisions in the PRD).

WS01 resolves only the ``--issue`` shape (the thinnest end-to-end thread). The
full grammar — ``--epic E --ws N`` and ``--branch <freeform>`` (naming.lex §3) —
lands in WS02. :class:`TreeSpec` is kept minimal but structured so that adding a
shape there is adding a field plus a branch in :func:`plan`, not reshaping callers.

The two load-bearing invariants the tests pin (from the PRD):

- the **agent hash lands on the dir, never on the branch** — two sessions sharing
  one branch is fine, two Trees in one dir is not; the hash disambiguates the dir
  while the branch stays a stable, meaningful namespace; and
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
    """A request to materialize a Tree.

    WS01 carries only the ``--issue`` shape. ``org`` / ``repo`` namespace the dir
    under the central root; ``agent_hash`` disambiguates the dir for two Trees on
    one branch (it never reaches the branch). ``root`` overrides the central root
    for tests; ``None`` resolves :func:`central_root`.

    Later shapes (epic/ws, freeform) add their own fields here; :func:`plan`
    branches on which is set, so this stays the single typed entry point.
    """

    org: str
    repo: str
    agent_hash: str
    issue: int | None = None
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

    WS01 handles the ``--issue`` shape only; any other shape raises
    :class:`NotImplementedError` rather than guessing — WS02 replaces that guard
    with the epic/ws and freeform branches.
    """
    if spec.issue is None:
        raise NotImplementedError(
            "tree.layout.plan: only the --issue shape is supported in this "
            "workstream (epic/ws and freeform land in WS02)"
        )
    return _plan_issue(spec)


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
