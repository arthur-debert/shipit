"""The one error type the repository-creation domain raises.

:class:`CreationError` is the domain's single handled-failure signal: a bad
request (name, parent, or destination), a conflicting profile contribution, a
failed staged Check, or a Git operation that could not complete. The verb maps
it to ``error: …`` + exit 1 through the shared CLI error shell; the orchestrator
catches it to guarantee the atomic-publish contract (remove the temporary
sibling, leave the destination in its preflight state) before it propagates.
"""

from __future__ import annotations


class CreationError(RuntimeError):
    """A handled repository-creation failure — never a partial published Repo."""
