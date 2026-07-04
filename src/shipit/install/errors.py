"""The install domain's refusal type (ADR-0030 two-tier exit contract)."""

from __future__ import annotations


class InstallError(RuntimeError):
    """A runtime refusal from the install domain.

    Raised for outcomes the invocation cannot proceed past — a target that is
    not a directory, a ``--local``/``--push`` run in detached HEAD. A member of
    the CLI error shell's known set (:data:`shipit.verbs._errors.KNOWN_ERRORS`),
    so at the CLI it renders as one ``error: …`` stderr line + exit 1; a direct
    API caller catches it as a typed exception.
    """
