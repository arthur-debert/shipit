"""``shipit.repocreate`` — the repository-creation domain behind ``shipit repo new``.

A refusal raises :class:`CreationError`, but a tool failure propagates unchanged.
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
