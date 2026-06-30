"""``spawn/backends/codex`` — the ``codex`` backend adapter (ADR-0020 §codex, WS02).

The second :class:`~shipit.spawn.backends.base.BackendAdapter`, wired from the WS00
spike's **probed** findings (`codex-cli` 0.139.0 — ADR-0020 §Decision-per-backend),
NOT from guessed flags. Three facts are load-bearing and non-obvious:

- **The bypass posture is mandatory for a WRITE Run.** ``codex exec`` defaults to a
  ``workspace-write`` sandbox under which the spike confirmed codex can edit files but
  is **denied ``.git/index.lock``** (``Operation not permitted``) and has **no network**
  — so it cannot ``git commit``, ``git push``, or run ``gh``. A Run whose deliverable is
  a *draft PR* therefore needs ``--dangerously-bypass-approvals-and-sandbox``: the flag
  codex documents for *"environments that are externally sandboxed"*, which our chmod'd
  Tree (ADR-0018) **is**. With the flag the spike landed a real commit; without it the
  Run cannot produce its result. ``--skip-git-repo-check`` lets codex run in the Tree
  without re-litigating that it is a git checkout.
- **There is NO native ``--agent`` / ``--system-prompt``.** codex cannot be handed a
  role identity by flag (the way ``claude --agent <role>`` is), and it does **not** run
  under the shipit harness or its ``PreToolUse`` guard at all — it is a foreign runtime.
  So the role is conveyed the only native way the spike validated: **prepended to the
  task prompt** (:func:`_role_preamble`). Writing the role into an ``AGENTS.md`` /
  ``experimental_instructions_file`` in the Tree was rejected — it would pollute the PR.
- **codex auth is ChatGPT OAuth** (tokens in ``~/.codex/auth.json`` / ``$CODEX_HOME``),
  inherited by the child. The spike found a bogus ``OPENAI_API_KEY`` did *not* break
  codex 0.139 on the probe box (it preferred the stored tokens), but the safe
  generalization of ADR-0019 §3 still holds: :meth:`child_env` scrubs ``OPENAI_API_KEY``
  and ``CODEX_API_KEY`` so a stale key can never shadow the login, and **no** secret is
  ever written into the Tree.

**Read-only / reviewer posture is WS04's job, not this WS's.** codex's reviewer
constraint is a ``--sandbox read-only`` *flag*, not a tool allow-list — so
:attr:`reviewer_tools` is ``None`` (read-only rides the chmod'd Tree, ADR-0018, plus
the flag as defense-in-depth). This adapter builds only the **write** argv; the
reviewer launch path (the ``--ephemeral --sandbox read-only`` variant the ADR records)
lands in WS04 and is deliberately not implemented here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .base import BackendAdapter

#: The codex auth-env vars :meth:`CodexAdapter.child_env` scrubs (ADR-0020 §codex
#: Auth, generalizing ADR-0019 §3): codex authenticates via ChatGPT OAuth, and a stale
#: ``OPENAI_API_KEY`` / ``CODEX_API_KEY`` left in the env can shadow that login. Scrub
#: them so the OAuth session wins; everything else inherits.
AUTH_ENV_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY")

#: The default codex model for a write Run — the capable "pro" tier (mirrors the legacy
#: review alias ``pro -> gpt-5.5`` in :mod:`shipit.review.backends.codex`, kept as a
#: local constant rather than imported across subsystems). The seam passes no per-Run
#: model, so a sane capable default is pinned here.
DEFAULT_MODEL = "gpt-5.5"


def _role_preamble(role: str) -> str:
    """The role line prepended to a codex prompt (the no-``--agent`` conveyance).

    codex has no native agent/system-prompt flag and does not run under the shipit
    harness, so the role is folded into the task prompt itself (ADR-0020 §codex
    Role/instruction conveyance — prompt-prepend is the recorded mechanism). The PR
    contract / draft-and-stop discipline already rides the task text from
    :func:`shipit.spawn.launch.write_task`; this preamble names the role the child is
    acting as so its judgement is anchored to it.
    """
    return f"You are acting as the '{role}' role for this Run."


class CodexAdapter(BackendAdapter):
    """The headless-``codex`` backend (ADR-0020 §codex), adapter #1 of the seam."""

    name = "codex"

    def build_command(
        self,
        task: str,
        role: str,
        *,
        tools: tuple[str, ...] | list[str] | None = None,
        cwd: str | Path | None = None,
    ) -> list[str]:
        """The exact ``codex exec`` WRITE argv ADR-0020 §codex specifies.

        ``codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox
        --model <id> "<role-preamble + task>"``. The task prompt is the first positional
        arg, with the role prepended (:func:`_role_preamble`) because codex has no
        ``--agent`` flag. The bypass flag is load-bearing: codex's default
        ``workspace-write`` sandbox blocks ``.git`` writes and the network, so a Run that
        must ``git commit`` / ``git push`` / ``gh pr create`` needs the unsandboxed
        posture — the chmod'd Tree (ADR-0018) is the external sandbox that flag documents.

        ``tools`` is accepted for seam parity but **ignored**: codex has no tool
        allow-list (its read-only posture is the ``--sandbox read-only`` flag, surfaced
        by :attr:`reviewer_tools` as ``None``). This builds the WRITE argv only.

        ``cwd`` is accepted for the seam (ADR-0020) but **ignored**: like ``claude``,
        codex roots in the Tree through the OS process ``cwd`` that
        :func:`shipit.spawn.launch.launch` sets, so no path belongs in its argv (unlike
        ``agy``, which ignores process ``cwd`` and is handed the Tree via ``--add-dir``).

        WS04: the reviewer variant (``--ephemeral --sandbox read-only`` against a shared
        read-only Tree, ADR-0020 §codex reviewer Run) is a separate launch path and is
        NOT branched here — do not fold a reviewer mode into this write argv.
        """
        del cwd  # codex roots via the process cwd; no path belongs in its argv.
        prompt = f"{_role_preamble(role)}\n\n{task}"
        return [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            DEFAULT_MODEL,
            prompt,
        ]

    def child_env(self, parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """The child's environment: the parent's, with codex auth-env vars REMOVED.

        ADR-0020 §codex Auth (generalizing ADR-0019 §3): codex authenticates via ChatGPT
        OAuth (tokens in ``$CODEX_HOME``), so a stale ``OPENAI_API_KEY`` / ``CODEX_API_KEY``
        in the env could shadow that login. Scrubbing both (:data:`AUTH_ENV_VARS`) makes
        the OAuth session win; everything else inherits and no secret is written to the
        Tree. ``parent_env`` defaults to the live :data:`os.environ` and is injectable
        for tests; the returned dict is always a fresh copy, never the caller's.
        """
        source = os.environ if parent_env is None else parent_env
        return {key: value for key, value in source.items() if key not in AUTH_ENV_VARS}

    @property
    def reviewer_tools(self) -> None:
        """``None`` — codex has no tool allow-list (ADR-0020 §codex Read-only posture).

        codex's reviewer constraint is the ``--sandbox read-only`` flag, not a tool
        allow-list, so there is nothing to hand :meth:`build_command`'s ``tools``. A
        reviewer Run's read-only guarantee rests on the chmod'd shared Tree (ADR-0018,
        the load-bearing guard), with ``--sandbox read-only`` as defense-in-depth — wired
        by WS04, not here.
        """
        return None
