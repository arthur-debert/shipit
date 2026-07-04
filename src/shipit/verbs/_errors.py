"""The CLI error shell (ADR-0030) — the runtime half of the two-tier exit contract.

One of the four seam pieces: ONE decorator, :func:`cli_errors`, wraps each
verb's ``run()`` and maps the known runtime exception set to a uniform
``error: …`` line on stderr + exit 1 — replacing the per-verb copies of the
same ``try/except`` block, so verb modules contain no error-mapping
boilerplate.

The two tiers (ADR-0030):

- **exit 2 — usage.** Argument errors, raised at parse and owned by click
  (the :mod:`._params` types fail there); they never reach this shell.
- **exit 1 — runtime.** The known exception set below, mapped here.
- **exit 0 — success.** The verb's own return.

Hook verbs are exempt — their fail-open/fail-closed canon is untouched, so
they never wear this decorator. An exception OUTSIDE the known set is a bug,
not an outcome: it propagates as a loud traceback rather than being dressed
up as a clean failure.
"""

from __future__ import annotations

import functools
import sys
from typing import Callable, ParamSpec

from .. import execrun
from ..config import ConfigError
from ..prstate.errors import PrStateError
from ..prstate.flip import NotReady
from ..prstate.reviewers_config import RequiredReviewersConfigError
from ..spawn.subagent import SpawnError
from ..tree.layout import LayoutError
from ..tree.removal import RemovalError
from ._context import NoAmbientRepoError

#: Preserves the wrapped ``run()``'s parameters through :func:`cli_errors`, so
#: mypy sees the original signature rather than an erased ``Callable[..., int]``.
P = ParamSpec("P")

#: The KNOWN runtime exception set — a failed boundary exec, a PR-state
#: violation, malformed/invalid config (both spellings), and the domain
#: refusals: the outside-a-checkout refusal the seam itself raises, the
#: engine's refused draft→ready flip (CLI01-WS03), the spawn pipeline's
#: refusal (CLI02-WS02), a misconfigured central Tree root, and a
#: refused/failed Tree removal (CLI02-WS03). Extended deliberately, one entry
#: per new domain refusal, as verbs adopt the shell.
KNOWN_ERRORS: tuple[type[Exception], ...] = (
    execrun.ExecError,
    PrStateError,
    ConfigError,
    RequiredReviewersConfigError,
    NoAmbientRepoError,
    NotReady,
    SpawnError,
    LayoutError,
    RemovalError,
)


def cli_errors(run: Callable[P, int]) -> Callable[P, int]:
    """Wrap a verb's ``run()`` in the uniform runtime-failure mapping.

    On a :data:`KNOWN_ERRORS` exception: one ``error: {exc}`` line to stderr,
    return 1. Everything else — including the return value on success — passes
    through untouched. The wrapped function keeps its signature (``ParamSpec``),
    so direct (non-click) callers and tests drive it exactly like the bare
    ``run()``, and mypy sees the original parameters.

    The message is collapsed to a single line before printing: some known
    errors (notably :class:`~shipit.execrun.ExecError`, which tails captured
    stdout/stderr) carry embedded newlines, and the ``error: …`` contract is
    ONE stderr line.
    """

    @functools.wraps(run)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
        try:
            return run(*args, **kwargs)
        except KNOWN_ERRORS as exc:
            message = " ".join(str(exc).split())
            print(f"error: {message}", file=sys.stderr)
            return 1

    return wrapper
