"""``shipit.repocreate`` — the repository-creation domain behind ``shipit repo new``.

A deep module the CLI is thin over (``docs/spec/repo-new.md``; ADR-0055–0063).
The public surface is small and value-typed (ADR-0030): :func:`create_repo`
takes a name, a parent, and the selected stacks and returns a
:class:`CreationResult`. A domain-level refusal raises :class:`CreationError`;
an underlying tool failure the orchestrator does not re-wrap — notably an
:class:`~shipit.execrun.ExecError` from Git (e.g. a commit that cannot sign) —
propagates unchanged. Both roll back with nothing partial ever published, and
both are handled callers must expect.

The internal layers:

- :mod:`.names` — the validated :class:`~.names.ProjectName` and its deterministic
  package/crate derivations (``<name>`` / ``lib<name>`` / crate identifiers).
- :mod:`.tomlio` / :mod:`.templates` — the two renderers ADR-0058 separates:
  structured data serialized once, authored text templated strictly.
- :mod:`.profiles` — the closed Creation-profile registry (ADR-0056/0063); the
  Rust profile's structured contribution.
- :mod:`.plan` — the central planner (ADR-0057): universal seed + profile
  contributions composed into one effect-free :class:`~.plan.CreationPlan`.
- :mod:`.create` — the effectful orchestrator: preflight, stage, install,
  provision, verify, commit, atomic publish (ADR-0059), with injectable effect
  seams (ADR-0062).
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
