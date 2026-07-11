"""``session/bootstrap`` — the Codex coordinator launch, as pure decisions (CDX01 #604).

A Claude launch (``./agent-start claude``) gets its isolated session Tree for
free: ``claude --worktree``
fires shipit's ``WorktreeCreate`` hook, which provisions the ephemeral Tree and
hands the path back for Claude Code to adopt as the session cwd (ADR-0027). Codex
has NO such pre-launch seam — no ``WorktreeCreate`` contract, no hook that can
substitute the cwd — but it has the one thing Claude lacks: shipit itself launches
the process (``shipit session codex``), so the Tree can be provisioned EXPLICITLY
first and Codex exec'd with both the OS cwd and ``--cd`` rooted in it.

This module is that launch's functional core (ADR-0021): three pure functions the
verb (:mod:`shipit.verbs.session`) composes with the effectful seams (the Tree
orchestrator, ``os.chdir``/``os.execvpe``), so the whole launch contract — the id
grammar, the argv posture, the env scrubs and exports — is unit-tested without a
Tree on disk or a real ``codex``:

- :func:`mint_session_id` — the per-launch session id, ``codex-<utc-stamp>-<pid>``.
  The same sortable stamp+pid grammar ``agent-start claude`` mints (unique across
  concurrent launches on one host), but prefixed ``codex-`` instead of ``sess-`` so
  the backend is legible everywhere the id lands: the Tree's dir leaf, the
  ``ephemeral/<id>`` birth branch, ``shipit tree list``, and every log/event record
  keyed on the session (the issue's "recognizable session id").
- :func:`codex_argv` — interactive ``codex`` rooted via ``--cd`` in the low-friction
  coordinator posture. The posture is the write-Run bypass flag, for the SAME probed
  reason as :class:`~shipit.spawn.backends.codex.CodexAdapter`'s write posture
  (ADR-0020 §codex): codex's own ``workspace-write`` sandbox denies ``.git`` writes
  and the network, so a coordinator that must ``git commit`` / ``git push`` / ``gh``
  cannot live under it — and the ephemeral Tree (a disposable dissociated clone,
  ADR-0014/0027) IS the "externally sandboxed environment" the flag documents.
- :func:`codex_env` — the child environment: the codex adapter's auth scrub (the
  API-billing keys removed so the ChatGPT subscription stays first-class,
  ``CODEX_ACCESS_TOKEN`` passing through — ADR-0020 §codex Auth, defined ONCE on the
  adapter), the launch-path project-pointer scrub (a parent ``PIXI_*``/Conda
  activation must not bind the session's own tool calls to the source checkout),
  the launch-seam agent-identity scrub (the inherited agent-identity keys
  ``SHIPIT_LOG_CTX_ROLE``/``_AGENT``/``_RUN`` from a spawned worker Run's shell
  must not disarm the new coordinator's edit guard nor mis-tag its log records
  with the worker's identity — the same scrub the managed ``agent-start``
  launcher performs, repeated here so a direct ``shipit session codex`` is
  covered too),
  plus the ``SHIPIT_LOG_CTX_SESSION``/``_TREE`` exports. Those exports are the codex
  counterpart of the SessionStart hook's ``CLAUDE_ENV_FILE`` log-context write
  (which codex has no equivalent of): every process the session runs inherits them,
  so each in-session shipit command rebinds the session identity at logging setup
  (:func:`shipit.logcontext.bind_from_env`) and the flow log slices by this session
  — the issue's "logs/events can identify the Codex session and Tree".
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from .. import logcontext, pixienv
from ..agent.backend import CODEX
from ..harness import activation as harness_activation
from ..spawn.backends.codex import CodexAdapter
from ..spawn.launch import scrub_tree_env

#: The minted id's prefix — what makes a Codex coordinator session recognizable on
#: disk and in the logs (vs the ``sess-`` a Claude launch mints): the prefix becomes the
#: ephemeral Tree's dir leaf and birth branch (``ephemeral/codex-…``, ADR-0027),
#: so ``shipit tree list`` and every session-keyed record say which backend owns it.
SESSION_ID_PREFIX = "codex"

#: The launch stamp's shape: a sortable UTC timestamp, the same grammar the managed
#: ``agent-start`` launcher mints for a Claude launch (``%Y%m%d-%H%M%S``).
_STAMP_FORMAT = "%Y%m%d-%H%M%S"

#: The low-friction coordinator posture (ADR-0020 §codex, probed on 0.139): codex's
#: sandboxes cannot host a coordinator — ``workspace-write`` denies ``.git`` writes
#: and the network (no commit, no push, no ``gh``), and ``read-only`` blocks the
#: network outright — so the session runs unsandboxed, with the ephemeral Tree as
#: the external isolation that flag documents. One flag, shared by the argv builder
#: and its tests.
BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"


def mint_session_id(*, now: float, pid: int) -> str:
    """The per-launch session id: ``codex-<utc-stamp>-<pid>``.

    Pure over an injected clock and pid (ADR-0021 — the verb passes ``time.time()``
    and ``os.getpid()``), so the grammar is asserted without freezing the real
    clock. Stamp+pid is unique across concurrent launches on one host (two launches
    in the same second are different processes), matching the Claude launch's minted
    ``sess-<utc>-<pid>``; the ``codex-`` prefix marks the backend (see
    :data:`SESSION_ID_PREFIX`). The result is already a pure ``[a-z0-9-]`` token,
    so it survives :func:`shipit.tree.layout.ephemeral_branch`'s normalization
    verbatim — the id on the Tree IS the id in the logs.
    """
    stamp = time.strftime(_STAMP_FORMAT, time.gmtime(now))
    return f"{SESSION_ID_PREFIX}-{stamp}-{pid}"


def codex_argv(tree: str | Path, extra: Sequence[str] = ()) -> list[str]:
    """The interactive ``codex`` argv, rooted in ``tree`` via ``--cd``.

    ``--cd`` names the Tree as the agent's working root explicitly (the verb ALSO
    ``chdir``s there before the exec, so the process cwd — which codex hook
    commands and child shells inherit — agrees; belt and suspenders, both pointing
    at the Tree). The posture is :data:`BYPASS_FLAG` (see the module docstring for
    the probed rationale). ``extra`` is the operator's own pass-through args
    (``./agent-start codex --model foo``), appended LAST so they land after the managed
    flags — an operator flag can therefore always extend or (where codex is
    last-wins) refine the posture. The binary name comes from the ONE backend
    identity registry (:data:`shipit.agent.backend.CODEX`, ADR-0025).
    """
    return [CODEX.binary, "--cd", str(tree), BYPASS_FLAG, *extra]


def codex_resume_argv(
    tree: str | Path, thread_id: str, extra: Sequence[str] = ()
) -> list[str]:
    """The first-class Codex resume argv, deliberately re-rooted in ``tree``.

    ``codex resume --cd <tree> <thread-id>`` preserves Codex's conversation
    identity while making shipit's session Tree identity explicit. The same
    low-friction coordinator posture is carried so a resumed coordinator can
    still commit, push, and run ``gh`` inside the replacement Tree.
    """
    return [CODEX.binary, "resume", "--cd", str(tree), BYPASS_FLAG, thread_id, *extra]


def activation_for_tree(tree: str | Path, *, runner=None) -> pixienv.Activation | None:
    """Capture pixi activation for ``tree`` when it has an activatable toolchain.

    Codex has no ``CLAUDE_ENV_FILE`` preamble, so the launch path applies the
    same pixi activation snapshot directly to the child env before ``execvpe``.
    Missing/non-pixi toolchains are a clean no-op; pixi failures propagate to
    the verb, which logs them and falls back to an unactivated launch.
    """
    toolchain = harness_activation.detect_toolchain(Path(tree))
    if toolchain is None:
        return None
    return pixienv.shell_hook(toolchain.manifest, runner=runner)


def codex_env(
    parent_env: Mapping[str, str],
    *,
    session_id: str,
    tree: str | Path,
    activation: pixienv.Activation | None = None,
) -> dict[str, str]:
    """The Codex session's COMPLETE child environment (for ``execvpe``).

    Four layers over ``parent_env``, each reusing the seam that already owns it
    so none can drift (module docstring):

    1. the codex adapter's auth scrub (:meth:`CodexAdapter.child_env` — the
       API-billing keys out, ``CODEX_ACCESS_TOKEN`` through);
    2. the launch-path project-pointer scrub
       (:func:`shipit.spawn.launch.scrub_tree_env` — leaked ``PIXI_*``/Conda
       activation vars out, so the session's own ``pixi``/``shipit`` calls resolve
       the Tree, not the parent checkout);
    3. the launch-seam agent-identity scrub: the inherited agent-identity keys
       ``SHIPIT_LOG_CTX_ROLE``/``_AGENT``/``_RUN`` (a coordinator started from
       inside a spawned worker Run's shell) are dropped — the session being
       launched IS a coordinator, a fresh agent. The pretooluse edit guard's
       ROLE fallback would otherwise silently resolve it to the worker's role
       and disarm; the worker's AGENT/RUN spawn ids would mis-tag the new
       coordinator's own log records with the worker's identity. Task-correlation
       keys (``PR``/``EPIC``/…) may still inherit — they describe the work, not
       who is doing it. The managed ``agent-start`` launcher scrubs the same
       keys; this covers a direct ``shipit session codex``;
    4. the ``SHIPIT_LOG_CTX_SESSION``/``_TREE`` exports (names from
       :data:`shipit.logcontext.ENV_PREFIX`, values matching what the SessionStart
       hook would export for this Tree — the leaf IS the session id, ADR-0027),
       so every in-session shipit process rebinds the session identity at
       logging setup.

    Returns a fresh dict, never the caller's mapping.
    """
    env = scrub_tree_env(CodexAdapter().child_env(parent_env))
    if activation is not None:
        env = pixienv.activated_env(env, activation)
    for key in ("ROLE", "AGENT", "RUN"):
        env.pop(logcontext.ENV_PREFIX + key, None)
    env[logcontext.ENV_PREFIX + "SESSION"] = session_id
    env[logcontext.ENV_PREFIX + "TREE"] = str(tree)
    return env


def format_launch(session_id: str, tree: str | Path, argv: Sequence[str]) -> str:
    """The one human-facing line-set printed before the exec replaces the process.

    The exec'd TUI takes the terminal over immediately, so this is the launch's
    only scrollback trace: which session id was minted, which Tree the session is
    rooted in, and the exact argv (``shlex``-joined, copy-pasteable) that took
    over. Pure string building — the verb owns the printing.
    """
    return f"codex session {session_id}\ntree {tree}\nexec {shlex.join(list(argv))}"
