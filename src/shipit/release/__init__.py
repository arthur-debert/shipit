"""The Release pipeline's pure cores and closed registries, stage by stage."""

from __future__ import annotations


class ReleaseError(RuntimeError):
    """A release-stage domain refusal: exit 1, one ``error: …`` line, never a traceback.

    USAGE errors die at the click boundary as exit 2 instead.
    """
