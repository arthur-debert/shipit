"""``spawn/backends/antigravity`` — the ``antigravity`` (``agy``) backend adapter (ADR-0020).

The non-Claude **write** adapter for the Antigravity CLI. ``agy`` ≡ ``antigravity`` is a
WS00 spike finding (ADR-0020 §Decision-per-backend): there is **one** binary, ``agy``
(v1.0.14 at probe time), under two names — the user-facing ``--backend`` token is
``antigravity``, the binary it shells out to is ``agy``. This is the same CLI the
review funnel now drives THROUGH this very adapter's reviewer posture
(:mod:`shipit.review.producer`, WS04b — the old ``review/backends/agy.py`` wrapper is
retired); this is its **spawn-Tree write** counterpart.

Three of agy's launch facts are non-obvious, **probe-confirmed** spike findings a paper
decision would have missed (do NOT "simplify" them away):

- **agy IGNORES its process ``cwd`` for the workspace.** A bare ``agy --print`` rooted at
  the Tree wrote into ``~/.gemini/antigravity-cli/scratch/…`` and reported *"you didn't
  have an active workspace set"*. The child is rooted in the Tree ONLY by
  ``--new-project --add-dir <Tree>`` (establishes an active project + grants write access
  to that dir). So this adapter needs the Tree **path** — it reads it from
  :meth:`build_command`'s ``cwd`` and emits ``--add-dir <cwd>``; without it writes never
  land in the Tree. (This is why ADR-0020 threads ``cwd`` into the seam's ``build_command``.)
- **``--dangerously-skip-permissions``** is agy's ``bypassPermissions`` equivalent
  (auto-approve every tool/shell request). Without it a non-interactive ``--print``
  **write** Run stalls on permission prompts editing files. A **reviewer** Run
  (``read_only=True``, WS04a) **omits** it: WS04a probed agy 1.0.14 — a ``--print`` Run
  *without* ``--dangerously-skip-permissions`` still runs network shell commands (it ran
  ``curl https://api.github.com/zen`` and returned the result), so the reviewer can
  ``gh pr diff`` / ``gh pr review`` to self-post without the dangerous flag. Read-only is
  enforced by the chmod'd Tree (ADR-0020 §Decision 3); omitting the flag is best-effort
  defense-in-depth on top.
- **The model must be pinned to a capable, non-agentic name.** ``agy`` silently resolves a
  bare ``pro`` to Gemini Flash, which in ``--print`` mode goes **agentic** (runs
  shell/build instead of answering). :data:`MODEL_ALIASES` pins ``pro`` →
  ``Gemini 3.1 Pro (High)``.

Auth rides agy's Antigravity OAuth login (creds under ``~/.gemini/antigravity-cli`` +
``~/.antigravity``, inherited by the child). The adapter scrubs :data:`SCRUBBED_AUTH_ENV`
(``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``) so a stale key can't shadow that login, and never
writes a secret into the Tree (ADR-0020 §Decision 3 — auth hygiene).

agy has **no** granular native tool allow-list / read-only sandbox for a reviewer, so a
reviewer Run's read-only guarantee rides **solely** on the chmod'd shared read-only Tree
(ADR-0018), the load-bearing guard — the seam's ``read_only`` flag only drops the
write Run's ``--dangerously-skip-permissions`` (best-effort defense-in-depth).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ...agent.backend import ANTIGRAVITY as _IDENTITY
from .base import BackendAdapter

#: Legacy review aliases → agy's verbatim model names — sourced from the ONE
#: agent-backend identity registry (:data:`shipit.agent.backend.ANTIGRAVITY`), NOT a
#: duplicate table here (ADR-0025: one alias table, shared by the launch + funnel axes).
#: The default ``pro`` MUST resolve to a capable, NON-agentic model: a bare ``pro``
#: silently resolves to Gemini Flash, which in ``--print`` goes agentic (runs shell/build
#: instead of answering) and never returns — so ``pro`` is pinned to
#: ``Gemini 3.1 Pro (High)``. Spaces/parens are safe: the invocation is a plain argv list
#: (never shell-interpolated), so no quoting is needed.
MODEL_ALIASES = _IDENTITY.model_aliases

#: The default model alias — a sane, capable, non-agentic default for a write Run (from
#: the shared identity; see :data:`MODEL_ALIASES`). Resolved through
#: :func:`resolve_model` at construction.
DEFAULT_MODEL = _IDENTITY.default_model

#: agy's ``--print`` timeout (default 5m). A big write Run can exceed that and return a
#: truncated result + ``timed out waiting for response``; 10m gives headroom. A consumer
#: with consistently large work raises it via the per-reviewer ``timeout`` option.
DEFAULT_TIMEOUT = "600s"

#: The env vars the ``antigravity`` adapter scrubs from the child env (ADR-0020
#: §Decision-per-backend, agy auth): a stale ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` could
#: shadow agy's preferred Antigravity OAuth login, so both are removed so the login wins.
SCRUBBED_AUTH_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def resolve_model(model: str) -> str:
    """Map a model alias to agy's verbatim name (pass-through for an already-verbatim name).

    Delegates to the shared agent-backend identity so there is ONE alias table."""
    return _IDENTITY.resolve_model(model)


def role_prompt(task: str, role: str) -> str:
    """Convey ``role`` natively by **prepending** it to the ``--print`` task text.

    ``agy`` has NO native ``--system-prompt`` / agent-def flag (ADR-0020
    §Decision-per-backend), so — unlike ``claude``'s ``--agent <role>`` — the role rides
    in the prompt itself. Prompt-prepend is the chosen mechanism (the review funnel proves
    prompt-only conveyance works); writing an agent-def into the Tree would pollute the PR.
    """
    return f"You are acting as the '{role}' role for this Run.\n\n{task}"


class AntigravityAdapter(BackendAdapter):
    """The headless-``agy`` (Antigravity) **write** backend (ADR-0020 §Decision-per-backend).

    Carries per-run ``model`` / ``timeout`` config: the registry holds one default instance
    (constructed with :data:`DEFAULT_MODEL` / :data:`DEFAULT_TIMEOUT`) for the CLI gate, while
    a caller with different needs constructs its own per-run instance — the review funnel
    builds one per reviewer carrying that reviewer's model/timeout
    (:mod:`shipit.review.producer`). The ``--backend`` token is ``antigravity``
    (user-facing); the binary is ``agy``.
    """

    name = _IDENTITY.name

    def __init__(
        self, model: str = DEFAULT_MODEL, timeout: str = DEFAULT_TIMEOUT
    ) -> None:
        #: The agy-verbatim model name (alias resolved once at construction).
        self.model = resolve_model(model)
        #: The ``--print-timeout`` value (an ``agy``-style ``<N>s`` duration string).
        self.timeout = timeout

    def build_command(
        self,
        task: str,
        role: str,
        *,
        read_only: bool = False,
        cwd: str | Path | None = None,
        output_schema_path: str | None = None,
    ) -> list[str]:
        """The exact ``agy`` ``--print`` argv ADR-0020 §Decision-per-backend records.

        ``agy --new-project --add-dir <cwd> --model=<name> --print-timeout=<dur>
        [--dangerously-skip-permissions] --print "<role-prefixed task>"``, run with the
        OS process ``cwd`` = the Tree and stdin ``/dev/null`` (both owned by the shared
        :func:`shipit.spawn.launch` path). Every flag is load-bearing:

        - ``--new-project --add-dir <cwd>`` roots agy in the Tree. agy **ignores the
          process ``cwd``** for its workspace, so this is the ONLY thing that makes its
          writes land in the Tree (probe-confirmed); ``cwd`` is therefore **required** —
          a missing path is a programming error raised loud (cwd-rooting invariant,
          ADR-0020 §Decision 3), never a silent write to agy's scratch dir. Required for
          BOTH postures: the reviewer reads the diff inside the same rooted Tree.
        - ``--model=<name>`` pins a capable non-agentic model (see :data:`MODEL_ALIASES`).
        - ``--print-timeout=<dur>`` bounds the blocking ``--print`` Run.
        - ``--dangerously-skip-permissions`` (agy's bypassPermissions equivalent) is added
          ONLY for a **write** Run (``read_only=False``) — without it a non-interactive
          write Run stalls on permission prompts editing files. A **reviewer**
          (``read_only=True``) **omits** it: WS04a probe-confirmed agy still runs network
          shell commands (``gh pr diff`` / ``gh pr review``) without it, so the reviewer
          can self-post, and read-only is enforced by the chmod'd Tree (ADR-0020
          §Decision 3) — omitting the flag is best-effort defense-in-depth.
        - ``--print "<text>"`` is the headless invocation; the role is prepended to the
          task text (:func:`role_prompt`) since agy has no ``--agent`` flag.

        ``output_schema_path`` is accepted for the seam (TRE05-WS04b) but **ignored**:
        agy has no native ``--output-schema`` flag, so a capture reviewer's expected JSON
        shape is conveyed in the prompt prose instead (the funnel producer builds the
        agy task with the schema described inline). The argument exists only so codex —
        the one backend with a native schema flag — can honour it uniformly.
        """
        del (
            output_schema_path
        )  # agy has no native schema flag; schema rides the prompt.
        if cwd is None:
            raise ValueError(
                "antigravity (agy) build_command requires cwd (the Tree path): agy "
                "ignores its process cwd and roots only via `--add-dir <Tree>`; without "
                "it the Run's writes land in agy's scratch dir, not the Tree."
            )
        permission = [] if read_only else ["--dangerously-skip-permissions"]
        return [
            "agy",
            "--new-project",
            "--add-dir",
            str(cwd),
            f"--model={self.model}",
            f"--print-timeout={self.timeout}",
            *permission,
            "--print",
            role_prompt(task, role),
        ]

    def child_env(self, parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """The child's environment: the parent's, with :data:`SCRUBBED_AUTH_ENV` REMOVED.

        agy authenticates via its Antigravity OAuth login (creds under
        ``~/.gemini/antigravity-cli`` + ``~/.antigravity``, inherited by the child). A
        stale ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` in the env could shadow that login,
        so both are scrubbed so the login wins (ADR-0020 §Decision-per-backend, the
        generalization of ADR-0019 §3). No secret is ever written to disk in the Tree;
        auth stays in agy's own config dirs. ``parent_env`` defaults to the live
        :data:`os.environ` and is injectable for tests; the result is always a fresh dict.
        """
        source = os.environ if parent_env is None else parent_env
        return {
            key: value for key, value in source.items() if key not in SCRUBBED_AUTH_ENV
        }
