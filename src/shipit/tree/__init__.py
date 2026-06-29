"""``shipit tree`` — isolated Trees (independent dissociated clones).

A Tree is a fully-independent checkout of the repo that a write-session works in,
so concurrent agents (and the human) never collide on one shared working tree
(PRD docs/prd/where-to-do-work.md, ADR-0014). This package holds the pieces the
``shipit tree create|list|remove|gc`` surface is built from; WS01 ships the
thinnest end-to-end thread — ``create`` for the ``--issue`` shape — as:

- :mod:`shipit.tree.layout` — pure planning: ``plan(spec) -> TreePlan{dir, branch,
  base}``. No I/O, table-tested.
- :mod:`shipit.tree.create` — the effectful orchestrator: clone (dissociated),
  fetch, checkout, emit READY. The git boundary lives in :mod:`shipit.gh`.

Later workstreams extend ``layout`` with the epic/ws and freeform shapes and add
``list`` / ``remove`` / ``gc`` (registry, cleanup, include) beside these.
"""

from __future__ import annotations
