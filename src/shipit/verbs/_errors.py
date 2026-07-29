"""The CLI error shell — the runtime half of the two-tier exit contract.

See docs/adr/0030-cli-boundary-parse-to-values-typed-results.md.
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
from ..staging import StagingError
from ..tree.layout import LayoutError
from ..tree.removal import RemovalError
from ._context import NoAmbientRepoError

KNOWN_ERRORS: tuple[type[Exception], ...] = (
    execrun.ExecError,
    PrStateError,
    ConfigError,
    ChangelogError,
    RequiredReviewersConfigError,
    NoAmbientRepoError,
    NotReady,
    SpawnError,
    StagingError,
    InstallError,
    OpportunityError,
    LayoutError,
    RemovalError,
    UnknownEventError,
    EventNotRecordedError,
    SweepError,
    ReleaseError,
    ReviewError,
    CellError,
    FixtureError,
    ResumeError,
    CreationError,
)


def cli_errors[**P](run: Callable[P, int]) -> Callable[P, int]:
    """Wrap a verb's ``run()`` so a KNOWN_ERRORS exception prints one collapsed ``error: …`` line (notes folded in) and returns 1."""

    @functools.wraps(run)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
        try:
            return run(*args, **kwargs)
        except KNOWN_ERRORS as exc:
            parts = [str(exc), *getattr(exc, "__notes__", [])]
            message = " ".join(" ".join(parts).split())
            print(f"error: {message}", file=sys.stderr)
            return 1

    return wrapper
