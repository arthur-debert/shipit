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

**Reviewer (read-only) posture — WS04a, probed, NOT the ADR's first guess.** The ADR
recorded ``--ephemeral --sandbox read-only`` for a reviewer Run, but that decision was
taken when the reviewer *returned* its findings on stdout (the funnel captured them). In
the spawn-Tree path the reviewer **self-posts** via ``gh pr review`` — which needs the
**network**. WS04a probed codex 0.139 directly and found ``--sandbox read-only`` **blocks
the network** (``curl … → Could not resolve host``), so a read-only-sandbox reviewer
**cannot post its review**. Per ADR-0020 §Decision 3 the load-bearing read-only guarantee
is the **chmod'd Tree** (the FS layer), not the native sandbox, so the chosen reviewer
posture is the *least-privilege codex sandbox that still grants the network*:
``--ephemeral --sandbox workspace-write -c sandbox_workspace_write.network_access=true``
(probe-confirmed to reach the network). It deliberately does **NOT** carry the write Run's
``--dangerously-bypass-approvals-and-sandbox``: the chmod'd Tree makes the workspace
non-writable (the real guard), and ``workspace-write`` still confines any escape to
``[workdir, /tmp, $TMPDIR]`` as best-effort defense-in-depth. Selected by ``read_only=True``
on :meth:`build_command`. **WS04a scope ends at the reviewer launch posture** — the funnel
replacement + check-run/readiness wiring is WS04b.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ...agent.backend import CODEX as _IDENTITY
from .base import BackendAdapter

#: The codex auth-env vars :meth:`CodexAdapter.child_env` scrubs (ADR-0020 §codex
#: Auth, generalizing ADR-0019 §3): codex authenticates via ChatGPT OAuth, and a stale
#: ``OPENAI_API_KEY`` / ``CODEX_API_KEY`` left in the env can shadow that login. Scrub
#: them so the OAuth session wins; everything else inherits.
AUTH_ENV_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY")

#: Legacy review aliases → Codex model ids — sourced from the ONE agent-backend
#: identity registry (:data:`shipit.agent.backend.CODEX`), NOT a duplicate table here
#: (ADR-0025: the alias table is defined once and shared by the launch + funnel axes).
#: The funnel's per-reviewer ``model`` config (``.shipit.toml [reviewers]``) speaks the
#: legacy ``pro`` / ``flash`` aliases, so a capture reviewer constructed with one
#: resolves it here; a write Run takes :data:`DEFAULT_MODEL`. A verbatim id passes through.
MODEL_ALIASES = _IDENTITY.model_aliases

#: The default codex model for a write Run — the capable "pro" tier (from the shared
#: identity). The registry instantiates :class:`CodexAdapter` with this; the funnel
#: constructs its own instance with the per-reviewer model. ``resolve_model`` leaves it
#: unchanged (already a verbatim id), so the write path is byte-for-byte unchanged. The
#: identity types ``default_model`` as ``str | None`` (a backend MAY require an explicit
#: model), but codex always pins one, so narrow to a definite ``str`` — the adapter's
#: ``model`` default expects a non-optional value.
assert _IDENTITY.default_model is not None
DEFAULT_MODEL: str = _IDENTITY.default_model


def resolve_model(model: str) -> str:
    """Map a legacy review alias to its Codex model id (pass-through otherwise).

    Delegates to the shared agent-backend identity so there is ONE alias table."""
    return _IDENTITY.resolve_model(model)


#: The codex ``-c`` override that enables outbound network inside the reviewer's
#: ``workspace-write`` sandbox (WS04a probe). Without it the sandbox blocks the network a
#: self-posting reviewer needs for ``gh pr diff`` / ``gh pr review`` (``read-only`` blocks
#: the network outright, with no override that re-grants it). Value is a TOML literal codex
#: parses (``foo.bar=true``).
NETWORK_ACCESS_OVERRIDE = "sandbox_workspace_write.network_access=true"


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

    name = _IDENTITY.name

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        #: The Codex model id (alias resolved once at construction). The registry's
        #: shared instance uses :data:`DEFAULT_MODEL`; the funnel constructs an instance
        #: with its per-reviewer ``model`` so the capture reviewer honours the config.
        self.model = resolve_model(model)

    def build_command(
        self,
        task: str,
        role: str,
        *,
        read_only: bool = False,
        cwd: str | Path | None = None,
        output_schema_path: str | None = None,
    ) -> list[str]:
        """The exact ``codex exec`` argv ADR-0020 §codex specifies, per posture.

        Common shell: ``codex exec --skip-git-repo-check <posture flags> --model <id>
        "<role-preamble + task>"``. The task prompt is the first positional arg, with the
        role prepended (:func:`_role_preamble`) because codex has no ``--agent`` flag.

        ``read_only`` selects the posture flags:

        - **write** (``read_only=False``, default): ``--dangerously-bypass-approvals-and-sandbox``.
          Load-bearing — codex's default ``workspace-write`` sandbox blocks ``.git`` writes
          and the network, so a Run that must ``git commit`` / ``git push`` / ``gh pr create``
          needs the unsandboxed posture; the chmod'd Tree (ADR-0018) is the external sandbox
          that flag documents.
        - **reviewer** (``read_only=True``): ``--ephemeral --sandbox workspace-write
          -c sandbox_workspace_write.network_access=true``. WS04a probed codex 0.139: a
          reviewer **self-posts** via ``gh pr review``, which needs the network, but
          ``--sandbox read-only`` (the ADR's first guess, taken when the funnel captured
          stdout) **blocks the network** — so the reviewer uses the least-privilege sandbox
          that still grants the network. It deliberately omits the write bypass flag: the
          chmod'd Tree is the load-bearing read-only guard (ADR-0020 §Decision 3), and
          ``workspace-write`` confines any escape to ``[workdir, /tmp, $TMPDIR]`` as
          best-effort defense-in-depth. ``--ephemeral`` skips session persistence.

        ``cwd`` is accepted for the seam (ADR-0020) but **ignored**: like ``claude``,
        codex roots in the Tree through the OS process ``cwd`` that
        :func:`shipit.spawn.launch.launch` sets, so no path belongs in its argv (unlike
        ``agy``, which ignores process ``cwd`` and is handed the Tree via ``--add-dir``).

        ``output_schema_path`` (TRE05-WS04b) — when given AND ``read_only`` — adds
        ``--output-schema <path>`` so codex enforces its structured output against the
        review JSON schema natively. It is the funnel **capture** reviewer's robustness
        win (ADR-0020 §migration-cost: *keep ``--output-schema`` on the codex reviewer*);
        the self-posting spawn-surface reviewer leaves it ``None`` and so omits the flag.
        It is never added to a WRITE Run (a write Run emits no captured JSON).
        """
        del cwd  # codex roots via the process cwd; no path belongs in its argv.
        prompt = f"{_role_preamble(role)}\n\n{task}"
        posture = (
            [
                "--ephemeral",
                "--sandbox",
                "workspace-write",
                "-c",
                NETWORK_ACCESS_OVERRIDE,
            ]
            if read_only
            else ["--dangerously-bypass-approvals-and-sandbox"]
        )
        if read_only and output_schema_path is not None:
            posture += ["--output-schema", output_schema_path]
        return [
            "codex",
            "exec",
            "--skip-git-repo-check",
            *posture,
            "--model",
            self.model,
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
