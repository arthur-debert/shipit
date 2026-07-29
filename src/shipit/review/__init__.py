"""Local code review: resolve a PR's diff, run a pluggable agent backend over
it, and post the verdict as that agent's own GitHub App identity.

``prstate.reviewers`` lazy-imports this package; it never imports ``prstate``.
"""

from __future__ import annotations
