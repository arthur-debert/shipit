"""``shipit tree`` — isolated Trees (independent dissociated clones).

A Tree is a fully-independent checkout of the repo that a write-session works in,
so concurrent agents (and the human) never collide on one shared working tree
(PRD docs/prd/where-to-do-work.md, ADR-0014). This package holds the pieces the
``shipit tree create|list|remove|gc`` surface is built from, split along the
``prstate`` "snapshot → decision" seam — pure planners/classifiers table-tested in
isolation, the I/O kept thin at the edges:

- :mod:`shipit.tree.layout` — pure planning: ``plan(spec) -> TreePlan{dir, branch,
  base}`` for all three spec shapes (``--issue``, ``--epic E --ws N``, freeform
  ``--branch``). No I/O, table-tested.
- :mod:`shipit.tree.include` — pure matching: ``.treeinclude`` (``.gitignore``
  syntax) → the gitignored-but-needed files to copy into a fresh Tree.
- :mod:`shipit.tree.create` — the effectful orchestrator behind ``create``: clone
  (dissociated), fetch, checkout, apply ``.treeinclude``, provision (``shipit
  install`` + ``pixi``/``npm``) with the ADR-0015 build env, emit READY. The git
  boundary lives in :mod:`shipit.gh`; provisioning commands go through
  ``create.run_provision``.
- :mod:`shipit.tree.registry` — manifest-less fleet scan behind ``list``:
  ``scan(root) -> [TreeRecord]`` reads each clone's state straight off disk.
- :mod:`shipit.tree.cleanup` — pure ``gc`` partition: ``classify(records, now,
  pr_states) -> Cleanup`` splits the fleet into removable / stale / keep,
  conservative by default.

The ``verbs/tree.py`` click group is the thin CLI over these.
"""

from __future__ import annotations
