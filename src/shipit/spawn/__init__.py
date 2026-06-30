"""``shipit spawn`` — shipit-owned subagent spawning (ADR-0017 / ADR-0019).

shipit owns the agent's Run environment: it creates the **Tree**, launches the
backend agent as a **child process rooted in it** (``cwd`` = Tree → no bash-cwd
footgun), and the Run reports back **through the PR**. This package holds the
launch machinery the ``shipit spawn subagent`` surface is built from, split along the
ADR-0020 backend seam so the per-backend specifics are isolated from the shared plumbing
and the whole contract is unit-tested without ever spawning a real child:

- :mod:`shipit.spawn.backends` — the per-backend
  :class:`~shipit.spawn.backends.base.BackendAdapter` registry (ADR-0020): each adapter
  fills exactly what varies (``build_command`` — write-vs-reviewer posture via its
  ``read_only`` flag — and ``child_env``).
  ``claude`` is adapter #0 (ADR-0019). ``--backend`` resolves one from this registry.
- :mod:`shipit.spawn.launch` — the **backend-agnostic** launch machinery: the injectable
  subprocess seam (:func:`~shipit.spawn.launch.launch`) that roots the child in the Tree
  with ``stdin`` from ``/dev/null``, plus the English PR-contract prompts
  (:func:`~shipit.spawn.launch.write_task` / :func:`~shipit.spawn.launch.reviewer_task`).

Tree creation is REUSED wholesale from :mod:`shipit.tree.create` — spawning never
reimplements it. The ``verbs/spawn.py`` click group is the thin CLI over these.
"""

from __future__ import annotations
