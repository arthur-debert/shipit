"""``session/bootstrap`` — the Codex coordinator launch, as pure decisions the verb composes."""

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

#: The minted id's prefix, vs the ``sess-`` a Claude launch mints.
SESSION_ID_PREFIX = "codex"

_STAMP_FORMAT = "%Y%m%d-%H%M%S"

#: Codex's own sandboxes deny ``.git`` writes and the network, so a coordinator
#: cannot live under one; the ephemeral Tree is the external isolation instead.
BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"


def mint_session_id(*, now: float, pid: int) -> str:
    """The per-launch session id: ``codex-<utc-stamp>-<pid>``, already a ``[a-z0-9-]`` token."""
    stamp = time.strftime(_STAMP_FORMAT, time.gmtime(now))
    return f"{SESSION_ID_PREFIX}-{stamp}-{pid}"


def codex_argv(tree: str | Path, extra: Sequence[str] = ()) -> list[str]:
    """The interactive ``codex`` argv rooted in ``tree``; ``extra`` is appended LAST."""
    return [CODEX.binary, "--cd", str(tree), BYPASS_FLAG, *extra]


def codex_resume_argv(
    tree: str | Path, thread_id: str, extra: Sequence[str] = ()
) -> list[str]:
    return [CODEX.binary, "resume", "--cd", str(tree), BYPASS_FLAG, thread_id, *extra]


def activation_for_tree(tree: str | Path, *, runner=None) -> pixienv.Activation | None:
    """Pixi activation for ``tree``, or ``None`` when it has no activatable toolchain."""
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
    """The Codex session's COMPLETE child environment; a fresh dict, never the caller's."""
    env = scrub_tree_env(CodexAdapter().child_env(parent_env))
    if activation is not None:
        env = pixienv.activated_env(env, activation)
    env = logcontext.scrub_env(env)
    env[logcontext.ENV_PREFIX + "SESSION"] = session_id
    env[logcontext.ENV_PREFIX + "TREE"] = str(tree)
    return env


def format_launch(session_id: str, tree: str | Path, argv: Sequence[str]) -> str:
    """The launch's only scrollback trace, built for the verb to print before the exec."""
    return f"codex session {session_id}\ntree {tree}\nexec {shlex.join(list(argv))}"
