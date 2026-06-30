"""``spawn/backends/base`` — the ``BackendAdapter`` seam (ADR-0020 §Decision).

A spawned Run is *mostly* backend-agnostic: the Tree it is rooted in, the English
PR-contract prompts it is handed (:func:`shipit.spawn.launch.write_task` /
:func:`~shipit.spawn.launch.reviewer_task`), the subprocess that runs it
(:func:`~shipit.spawn.launch.launch`), and the PR it reports back through are *the
same* whether the child is ``claude``, ``codex``, or ``antigravity``. What actually
*varies* per backend is small and sharply bounded — exactly two things — and this
ABC is that boundary:

- **:meth:`build_command`** — the backend's headless argv (how the task prompt and
  the role are passed, the non-interactive/result mode, **and the write-vs-reviewer
  posture**). ``claude`` is ``claude -p … --agent <role> …``; a foreign runtime is
  shaped entirely differently. This is per-backend private business, discovered by
  spike (ADR-0020 §Decision-per-backend), NOT a paper guess at CLI flags.

  The **read-only posture** a *reviewer* Run carries *beyond* the chmod'd shared Tree
  (ADR-0018) is expressed as a single keyword-only flag on this method —
  ``read_only: bool`` — not as a separate seam member. ``read_only=False`` (the
  default) builds the **write** argv (the backend's bypass/skip-permissions posture so
  the Run can commit + push + open a PR); ``read_only=True`` builds the **reviewer**
  argv, and what that means is the adapter's private business: ``claude`` narrows to its
  read-only ``--tools`` allow-list; ``codex`` / ``agy`` have no granular allow-list, so
  their reviewer posture is *whatever lets the agent post its review through the PR while
  the chmod'd Tree remains the load-bearing FS guard* (ADR-0020 §Decision 3 — the native
  sandbox is best-effort defense-in-depth on top of the chmod, never the guarantee). The
  flag, not a tool tuple, is the signal **because a tool allow-list does not generalize**
  — codex/agy have none, so "non-``None`` ``tools``" could not distinguish a reviewer
  from a write Run for them.
- **:meth:`child_env`** — the backend's auth-env transform. Every backend has its own
  auth hazard (a stale var shadowing a logged-in session); the *principle* ("scrub the
  vars that would break this backend's preferred login") generalizes, the *specific
  var* does not — so the transform is the adapter's, not a shared constant. The same
  scrub applies to write and reviewer Runs (auth is posture-independent).

The cross-backend **invariants** (cwd = the Tree, PR-only result channel, fail-closed
Tree creation, FS-enforced read-only reviewers) live in the *shared* launch/verb code
and are NOT re-expressed per adapter — an adapter only fills the two holes above.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path


class BackendAdapter(ABC):
    """The per-backend launch seam: argv, auth-env, and read-only posture (ADR-0020).

    One concrete subclass per backend, registered by :data:`~shipit.spawn.backends`'s
    registry under :attr:`name` (the ``--backend`` token). Adapters are stateless —
    the registry holds a single shared instance per backend. Everything an adapter does
    NOT override (the Tree, the prompts, the subprocess, PR resolution) stays shared in
    :mod:`shipit.spawn.launch` and :mod:`shipit.verbs.spawn`; this class is *only* the
    two things that genuinely differ per backend.
    """

    #: The backend's ``--backend`` token (e.g. ``"claude"``). The registry key and the
    #: value echoed in the SPAWNED summary; concrete adapters set it as a class attr.
    name: str

    @abstractmethod
    def build_command(
        self,
        task: str,
        role: str,
        *,
        read_only: bool = False,
        cwd: str | Path | None = None,
        output_schema_path: str | None = None,
    ) -> list[str]:
        """The backend's headless argv for ``task`` under ``role``.

        Returns the exact non-shell argv to run as the child. ``role`` is conveyed
        however the backend conveys a system prompt / agent identity (for ``claude``,
        the native ``--agent`` flag; a backend with no such flag prepends the role to
        the prompt text).

        ``read_only`` selects the **posture**, and IS the write-vs-reviewer signal
        (ADR-0020 §Decision 3) — there is no separate ``tools`` argument, because a
        tool allow-list does not generalize (codex/agy have none, so it could not tell
        a reviewer from a write Run for them):

        - ``read_only=False`` (default) — the **write** argv: the backend's
          bypass/skip-permissions posture so the Run can edit, commit, push, and open a
          draft PR (claude ``bypassPermissions``; codex
          ``--dangerously-bypass-approvals-and-sandbox``; agy
          ``--dangerously-skip-permissions``).
        - ``read_only=True`` — the **reviewer** argv. What it constrains is the
          adapter's private business: ``claude`` narrows to its read-only ``--tools``
          allow-list; ``codex`` / ``agy``, having no allow-list, instead build the
          *least-privilege posture that still lets the agent post its review through the
          PR* (`gh pr review` needs the network — see the per-adapter docstrings for the
          probed posture). The load-bearing read-only guarantee is ALWAYS the chmod'd
          shared Tree (ADR-0018), at the FS layer; the backend's native restriction is
          best-effort defense-in-depth on top, never the guarantee.

        ``cwd`` is the **Tree path** the child is rooted in. Most backends honour the
        OS process ``cwd`` (which :func:`shipit.spawn.launch.launch` sets) and so
        ignore this argument — it is here for the load-bearing exception (ADR-0020
        §Decision-per-backend): ``agy`` IGNORES its process ``cwd`` for the workspace
        and must be handed the Tree explicitly (``--add-dir <cwd>``) or its writes land
        in ``~/.gemini/.../scratch`` instead of the Tree. An adapter that needs the
        path therefore reads it here; one that does not leaves it ``None``.

        ``output_schema_path`` is the path to a JSON-schema file a **capture
        reviewer** (the review funnel, TRE05-WS04b) wants the backend to enforce its
        structured output against. It is meaningful ONLY for a reviewer whose result
        is *captured* (shipit reads the agent's stdout and posts it via the
        ``review:`` check-run gate), NOT for the self-posting spawn-surface reviewer
        (which leaves it ``None``). Only ``codex`` has a native schema flag
        (``--output-schema``) to honour it; ``claude`` / ``agy`` have no native schema
        enforcement and IGNORE it (``agy`` instead carries the schema in its prompt
        prose, ``claude`` is never a funnel backend). The load-bearing constraint
        ADR-0020 §migration-cost pins is *keep codex ``--output-schema`` on the
        reviewer* — that robustness win rides this argument.
        """

    @abstractmethod
    def child_env(self, parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """The child's environment: the parent's, with this backend's auth hazards scrubbed.

        Returns a fresh dict (never the caller's). ``parent_env`` defaults to the live
        :data:`os.environ` and is injectable for tests. The adapter removes exactly the
        vars that would shadow its backend's preferred login and NEVER writes a secret
        to disk in the Tree (ADR-0020 §Decision 3 — auth hygiene). The scrub is the same
        for write and reviewer Runs — auth is posture-independent.
        """
