"""``shipit.repocreate`` — the repository-creation domain behind ``shipit repo new``.

:func:`create_repo` refuses with :class:`CreationError`, but an underlying tool
failure (an ``ExecError`` from Git) propagates unchanged; both roll back.
"""

from __future__ import annotations

from .create import CreationResult, create_repo
from .errors import CreationError
from .names import ProjectName, validate_name
from .plan import CreationPlan, build_plan
from .profiles import resolve_profiles

__all__ = [
    "CreationError",
    "CreationPlan",
    "CreationResult",
    "ProjectName",
    "build_plan",
    "create_repo",
    "resolve_profiles",
    "validate_name",
]
