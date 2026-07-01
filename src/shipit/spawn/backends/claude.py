"""``spawn/backends/claude`` — the ``claude`` backend adapter, #0 (ADR-0019 / ADR-0020).

The first :class:`~shipit.spawn.backends.base.BackendAdapter`: it carries the ADR-0019
launch contract *verbatim* — the only thing the WS01 seam refactor does is move that
contract from module-level functions in :mod:`shipit.spawn.launch` to behind the adapter
boundary, with **zero behaviour change**. The argv, the auth-env scrub, and the reviewer
allow-list are unchanged; only their home moved.

Two of the contract's facts are non-obvious, load-bearing spike findings (ADR-0019 §2/§3)
that a paper decision would have missed:

- **``--agent <role>``** is not cosmetic: a headless ``claude -p`` child is a fresh
  *top-level* session, so its ``PreToolUse`` payload carries no ``agent_type`` — which
  :func:`shipit.harness.role.resolve_role` maps to ``coordinator``, the role the guard
  forbids from editing. The native ``--agent`` flag populates ``agent_type`` so the guard
  allows the spawned Run's own edits. No change to ``resolve_role`` is needed.
- **Scrubbing ``ANTHROPIC_API_KEY``** is a hard requirement: a stale/invalid key in the
  env takes precedence over the claude.ai OAuth/keychain login and breaks the child's auth
  ("Invalid API key"). Removing it makes the child use the keychain login the parent is
  already logged in with.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ...agent.backend import CLAUDE as _IDENTITY
from .base import BackendAdapter

#: The env var the ``claude`` adapter MUST remove from the child env (ADR-0019 §3): a
#: stale/invalid value takes precedence over the claude.ai OAuth/keychain login and
#: breaks auth. Scrubbing it is a hard contract requirement, not a nicety.
ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"

#: The read-only tool allow-list for a **reviewer** Run (ADR-0018 / ADR-0019 §4): a
#: reviewer reads the diff and code and posts a review, so it gets the read tools plus
#: ``Bash`` (to run ``gh pr diff`` and ``gh pr review``) but NOT ``Write`` / ``Edit`` —
#: the read-only posture rides the ``--tools`` allow-list, mirroring the reviewer
#: agent-def frontmatter. ``claude`` is the one backend with a native allow-list, so its
#: ``read_only=True`` posture is this tuple; codex/agy have none and instead build a
#: sandbox/permission posture (their adapters), with the chmod'd Tree as the FS guard.
REVIEWER_TOOLS = ("Read", "Grep", "Glob", "Bash")


class ClaudeAdapter(BackendAdapter):
    """The headless-``claude`` backend (ADR-0019), adapter #0 of the ADR-0020 seam."""

    name = _IDENTITY.name

    def build_command(
        self,
        task: str,
        role: str,
        *,
        read_only: bool = False,
        cwd: str | Path | None = None,
        output_schema_path: str | None = None,
    ) -> list[str]:
        """The exact ``claude`` print-mode argv ADR-0019 §1 specifies.

        ``claude -p "<task>" --agent <role> --permission-mode bypassPermissions
        [--tools "<allowlist>"] --output-format json``. Two args are load-bearing:
        ``--agent <role>`` populates the hook payload's ``agent_type`` so the
        coordinator-guard allows the Run's own edits (§2), and ``--permission-mode
        bypassPermissions`` is the write-Run mode (§4) — still bounded by the guard,
        which fires inside the child. ``-p`` makes it a blocking foreground Run;
        ``--output-format json`` yields the single result envelope the parent treats as
        the exit signal.

        ``read_only`` selects the posture (§4): a **reviewer** (``read_only=True``)
        narrows tool access to claude's native read-only allow-list (:data:`REVIEWER_TOOLS`
        — no ``Write`` / ``Edit``) via ``--tools "<comma-joined>"``; a **write** Run
        (the default) omits the flag and inherits the role's full toolset. claude is the
        one backend with a native allow-list, so it reads :data:`REVIEWER_TOOLS` itself —
        the seam carries only the boolean, since codex/agy have no allow-list to pass.

        ``cwd`` is accepted for the seam (ADR-0020) but **ignored**: ``claude`` roots
        in the Tree through the OS process ``cwd`` that :func:`shipit.spawn.launch.launch`
        sets, so it needs no path in its argv (unlike ``agy``, which ignores process
        ``cwd`` and is handed the Tree via ``--add-dir``).

        ``output_schema_path`` is accepted for the seam (TRE05-WS04b) but **ignored**:
        ``claude`` is not a funnel *capture* backend (the funnel runs codex / agy), so it
        never carries a native review-output schema. The argument exists only so codex
        can honour it uniformly across the seam.
        """
        # claude roots via the process cwd (no path in argv) and is not a funnel
        # capture backend (never schema'd) — both seam args are ignored.
        del cwd, output_schema_path
        cmd = [
            "claude",
            "-p",
            task,
            "--agent",
            role,
            "--permission-mode",
            "bypassPermissions",
        ]
        if read_only:
            cmd += ["--tools", ",".join(REVIEWER_TOOLS)]
        cmd += ["--output-format", "json"]
        return cmd

    def child_env(self, parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """The child's environment: the parent's, with ``ANTHROPIC_API_KEY`` REMOVED.

        ADR-0019 §3 (a load-bearing spike finding): a stale/invalid
        ``ANTHROPIC_API_KEY`` in the env takes precedence over the claude.ai
        OAuth/keychain login and breaks the child's auth. Scrubbing it is a hard
        contract requirement so the keychain login is used; everything else inherits.
        ``parent_env`` defaults to the live :data:`os.environ` and is injectable for
        tests.
        """
        source = os.environ if parent_env is None else parent_env
        return {key: value for key, value in source.items() if key != ANTHROPIC_API_KEY}
