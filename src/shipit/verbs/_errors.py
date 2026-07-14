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
from collections.abc import Callable

from .. import execrun
from ..changelog import ChangelogError
from ..config import ConfigError
from ..events import EventNotRecordedError, UnknownEventError
from ..fleetsweep import SweepError
from ..install.errors import InstallError
from ..opportunities import OpportunityError
from ..provision.lexd import ProvisionError
from ..prstate.errors import PrStateError
from ..prstate.flip import NotReady
from ..prstate.reviewers_config import RequiredReviewersConfigError
from ..release import ReleaseError
from ..repocreate import CreationError
from ..review.cell import CellError
from ..review.diff import ReviewError
from ..review.groundtruth import FixtureError
from ..session.resume import ResumeError
from ..spawn.subagent import SpawnError
from ..tree.layout import LayoutError
from ..tree.removal import RemovalError
from ._context import NoAmbientRepoError

#: The KNOWN runtime exception set — a failed boundary exec, a PR-state
#: violation, malformed/invalid config (both spellings), and the domain
#: refusals: the outside-a-checkout refusal the seam itself raises, the
#: engine's refused draft→ready flip (CLI01-WS03), the spawn pipeline's
#: refusal (CLI02-WS02), the install domain's refusals (CLI02-WS01), a
#: misconfigured central Tree root, a refused/failed
#: Tree removal (CLI02-WS03), and the constrained dev-cycle write path's two
#: refusals (an out-of-vocabulary event name, an emission that failed past
#: validation — CLI02-WS05), the tool-provisioning refusal (unsupported
#: platform, checksum mismatch, malformed release — ADP00-WS03), and the
#: changelog refusals (empty release, invalid version, unsyncable tree —
#: TOL01-WS06), and the fleet sweep's refusals (a missing source checkout, an
#: unresolvable candidate build, a selector outside the declared portfolio —
#: TOL01-WS07), and the review path's precondition refusals (a bad commit
#: range / unknown revision / repo-less checkout in `pr review replay` —
#: RVW02-WS03), and the Review Lab's refusals (an untrustworthy cell file /
#: unfair pair / missing checkout in `lab run`/`lab report`, and an
#: untrustworthy ground-truth fixture — RVW03-WS07), and the release
#: stages' refusals (a no-op bump, a manifest a projection cannot rewrite,
#: a prepare outside a checkout / on a detached HEAD — TOL02-WS01; a bundle
#: composition over missing build outputs, an unresolvable assert-bundle
#: expected name — TOL02-WS03). Extended deliberately, one entry per new
#: domain refusal, as verbs adopt the shell.
KNOWN_ERRORS: tuple[type[Exception], ...] = (
    execrun.ExecError,
    PrStateError,
    ConfigError,
    ChangelogError,
    RequiredReviewersConfigError,
    NoAmbientRepoError,
    NotReady,
    SpawnError,
    InstallError,
    OpportunityError,
    LayoutError,
    RemovalError,
    UnknownEventError,
    EventNotRecordedError,
    ProvisionError,
    SweepError,
    ReleaseError,
    ReviewError,
    CellError,
    FixtureError,
    ResumeError,
    CreationError,
)


def cli_errors[**P](run: Callable[P, int]) -> Callable[P, int]:
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
