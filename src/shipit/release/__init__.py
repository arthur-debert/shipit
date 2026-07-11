"""release — the Release pipeline's cores and registries (TOL02).

A **Release** carries ONE repo-level version whose authority is the git tag;
manifests are projections of the tag decision (ADR-0041). This package holds
the pipeline's pure cores and closed registries, stage by stage as the TOL02
work streams land:

- :mod:`.version` — the version resolver (WS01): supplied ``<semver>`` or bump
  word → the resolved version, prerelease flag, and resume verdict. Pure.
- :mod:`.bump` — the per-toolchain bump-adapter registry and the
  artifact-declared bundle-config hook (WS01): how the tag decision projects
  into manifests. Command literals + pure text rewrites; no I/O.

The effectful shells live in :mod:`shipit.verbs` (``shipit release prepare``
is :mod:`shipit.verbs.release`), executing through the one Exec seam
(ADR-0028) and the git adapter.
"""

from __future__ import annotations


class ReleaseError(RuntimeError):
    """A release-stage domain refusal — exit 1 via the shared CLI error shell
    (:mod:`shipit.verbs._errors`), one ``error: …`` line, never a traceback.

    Raised for runtime refusals of the release stages: a no-op bump (the
    manifests already carry the target version but its tag does not exist —
    re-running against a different release), a manifest a bump adapter cannot
    rewrite, a prepare invoked outside a git checkout or on a detached HEAD.
    USAGE errors (a malformed version argument) are NOT this class — they die
    at the click boundary as exit 2 (ADR-0030).
    """
