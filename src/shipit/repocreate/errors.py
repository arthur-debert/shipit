"""The one error type the repository-creation domain DEFINES.

:class:`CreationError` is the domain's own handled-failure signal — a
domain-level refusal: a bad request (name, parent, or destination), a
conflicting profile contribution, a failed staged Check, or a Git author/
committer identity that could not be resolved at preflight. It is NOT the only
exception :func:`~.create.create_repo` can raise: an underlying tool failure the
orchestrator does not re-wrap — notably an :class:`~shipit.execrun.ExecError`
from Git itself (a commit that cannot sign, or any other commit-time git error)
— propagates UNCHANGED. The atomic-publish rollback holds for that case just the
same: the orchestrator's handler catches EVERY failure (not only its own type),
removing the temporary sibling and leaving the destination in its preflight
state before the failure keeps propagating. The verb maps BOTH types to
``error: …`` + exit 1 through the shared CLI error shell (both are in its
known-error set).
"""

from __future__ import annotations


class CreationError(RuntimeError):
    """A handled repository-creation failure — never a partial published Repo."""
