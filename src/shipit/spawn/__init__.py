"""``shipit spawn`` — shipit-owned subagent spawning (ADR-0017 / ADR-0019).

shipit owns the agent's Run environment: it creates the **Tree**, launches the
backend agent as a **child process rooted in it** (``cwd`` = Tree → no bash-cwd
footgun), and the Run reports back **through the PR**. This package holds the
launch machinery the ``shipit spawn subagent`` surface is built from, kept thin
and split along a pure/effectful seam so the contract is unit-tested without ever
spawning a real ``claude``:

- :mod:`shipit.spawn.launch` — the headless-``claude`` launch contract (ADR-0019):
  the pure argv/env builders (:func:`~shipit.spawn.launch.build_command`,
  :func:`~shipit.spawn.launch.child_env`) plus the injectable subprocess seam
  (:func:`~shipit.spawn.launch.launch`) that roots the child in the Tree with
  ``stdin`` from ``/dev/null`` and ``ANTHROPIC_API_KEY`` scrubbed.

Tree creation is REUSED wholesale from :mod:`shipit.tree.create` — spawning never
reimplements it. The ``verbs/spawn.py`` click group is the thin CLI over these.
"""

from __future__ import annotations
