"""``tree/create`` — the effectful orchestrator that materializes a Tree.

``create(spec, ...) -> Tree`` turns a pure :class:`~shipit.tree.layout.TreePlan`
into a real, independent checkout on disk and returns the READY summary
(``{path, branch, base}``). The clone-strategy complexity hides behind this one
call (PRD Implementation Decisions):

1. ``git clone --reference <local> --dissociate <github-url> <dir>`` — a tiny,
   instant, yet fully INDEPENDENT clone (ADR-0014); see
   :func:`shipit.gh.git_clone_dissociated`.
2. ``git fetch origin`` then ``git checkout -b <branch> <base>``.

WS01 stops there — the thinnest end-to-end thread. The deferred steps the full
pipeline will add (apply ``.treeinclude``; provision ``shipit install`` +
``pixi``/``npm``; sccache env) are out of scope here and are NOT stubbed.

Every git call goes through the :mod:`shipit.gh` boundary, so the orchestration
is the only thing this module owns; the integration smoke exercises the real git
path end to end, while the planning truth table is unit-tested in ``layout``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from .. import gh
from .layout import TreeSpec, plan

#: Bytes of randomness behind an agent hash → 8 hex chars. Enough to keep two
#: concurrent Trees for the same issue from colliding on disk without bloating the
#: dir name.
_HASH_BYTES = 4


@dataclass(frozen=True)
class Tree:
    """A materialized Tree — the READY summary a caller prints/consumes."""

    path: str
    branch: str
    base: str


def new_agent_hash() -> str:
    """A short random hex tag that disambiguates a Tree's directory (never its branch)."""
    return secrets.token_hex(_HASH_BYTES)


def create(spec: TreeSpec, *, source_repo: str, github_url: str) -> Tree:
    """Materialize the Tree described by ``spec`` and return its READY summary.

    ``source_repo`` is the local checkout whose object store seeds the clone
    (``--reference``); ``github_url`` is the remote the new Tree's ``origin`` points
    at. The leaf directory's parents are created first (``git clone`` makes only
    the leaf), then the clone is dissociated, fetched, and put on the planned
    branch cut from the planned base.
    """
    tree_plan = plan(spec)
    dest = tree_plan.dir
    dest.parent.mkdir(parents=True, exist_ok=True)

    gh.git_clone_dissociated(github_url, str(dest), reference=source_repo)
    gh.git_fetch(cwd=str(dest))
    gh.git_checkout_new_branch(tree_plan.branch, tree_plan.base, cwd=str(dest))

    return Tree(path=str(dest), branch=tree_plan.branch, base=tree_plan.base)


def create_from_source(spec: TreeSpec, *, source_repo: str | Path) -> Tree:
    """:func:`create` with ``github_url`` resolved from ``source_repo``'s ``origin``.

    The Tree clones from — and points ``origin`` at — exactly the URL the source
    checkout already uses, so auth and ``gh`` behave identically inside the Tree.
    """
    source = str(source_repo)
    url = gh.git_remote_url(cwd=source)
    return create(spec, source_repo=source, github_url=url)
