"""``spawn/backends/base`` — the ``BackendAdapter`` seam (ADR-0020 §Decision).

A spawned Run is *mostly* backend-agnostic: the Tree it is rooted in, the English
PR-contract prompts it is handed (:func:`shipit.spawn.launch.write_task` /
:func:`~shipit.spawn.launch.reviewer_task`), the subprocess that runs it
(:func:`~shipit.spawn.launch.launch`), and the PR it reports back through are *the
same* whether the child is ``claude``, ``codex``, or ``antigravity``. What actually
*varies* per backend is small and sharply bounded — exactly three things — and this
ABC is that boundary:

- **:meth:`build_command`** — the backend's headless argv (how the task prompt and
  the role are passed, the non-interactive/result mode). ``claude`` is ``claude -p
  … --agent <role> …``; a foreign runtime is shaped entirely differently. This is
  per-backend private business, discovered by spike (ADR-0020 §Decision-per-backend),
  NOT a paper guess at CLI flags.
- **:meth:`child_env`** — the backend's auth-env transform. Every backend has its own
  auth hazard (a stale var shadowing a logged-in session); the *principle* ("scrub the
  vars that would break this backend's preferred login") generalizes, the *specific
  var* does not — so the transform is the adapter's, not a shared constant.
- **:attr:`reviewer_tools`** — the **read-only posture** a reviewer Run carries
  *beyond* the chmod'd shared Tree (ADR-0018). A backend with a native tool allow-list
  returns it (``claude`` → its read-only tools); a backend with no such knob returns
  ``None`` — read-only then **rides solely on the chmod'd Tree**, which is the
  load-bearing, backend-agnostic guarantee. The native allow-list is defense-in-depth
  on top, best-effort (ADR-0020 §Decision 3).

The cross-backend **invariants** (cwd = the Tree, PR-only result channel, fail-closed
Tree creation, FS-enforced read-only reviewers) live in the *shared* launch/verb code
and are NOT re-expressed per adapter — an adapter only fills the three holes above.
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
    three things that genuinely differ per backend.
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
        tools: tuple[str, ...] | list[str] | None = None,
        cwd: str | Path | None = None,
    ) -> list[str]:
        """The backend's headless argv for ``task`` under ``role``.

        Returns the exact non-shell argv to run as the child. ``role`` is conveyed
        however the backend conveys a system prompt / agent identity (for ``claude``,
        the native ``--agent`` flag; a backend with no such flag prepends the role to
        the prompt text). ``tools`` narrows a reviewer Run's tool access when the
        backend supports an allow-list (the value of :attr:`reviewer_tools`); a write
        Run passes ``None`` and inherits the role's full toolset. A backend with no
        allow-list ignores ``tools`` — its read-only posture rides the Tree.

        ``cwd`` is the **Tree path** the child is rooted in. Most backends honour the
        OS process ``cwd`` (which :func:`shipit.spawn.launch.launch` sets) and so
        ignore this argument — it is here for the load-bearing exception (ADR-0020
        §Decision-per-backend): ``agy`` IGNORES its process ``cwd`` for the workspace
        and must be handed the Tree explicitly (``--add-dir <cwd>``) or its writes land
        in ``~/.gemini/.../scratch`` instead of the Tree. An adapter that needs the
        path therefore reads it here; one that does not leaves it ``None``.
        """

    @abstractmethod
    def child_env(self, parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """The child's environment: the parent's, with this backend's auth hazards scrubbed.

        Returns a fresh dict (never the caller's). ``parent_env`` defaults to the live
        :data:`os.environ` and is injectable for tests. The adapter removes exactly the
        vars that would shadow its backend's preferred login and NEVER writes a secret
        to disk in the Tree (ADR-0020 §Decision 3 — auth hygiene).
        """

    @property
    @abstractmethod
    def reviewer_tools(self) -> tuple[str, ...] | None:
        """The read-only tool allow-list for a reviewer Run, or ``None``.

        A backend with a native allow-list returns the read-only toolset (no
        ``Write`` / ``Edit``) to pass to :meth:`build_command`'s ``tools``; a backend
        without one returns ``None`` — the reviewer's read-only guarantee then rests
        solely on the chmod'd shared Tree (ADR-0018), which is the load-bearing,
        backend-agnostic enforcement. Any native restriction is defense-in-depth.
        """
