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


class SelfCertError(InstallError):
    """Install's self-certification failed — the fail-closed refusal (ADR-0033).

    Raised by :func:`shipit.install.apply.apply` when any of the four staged
    postconditions misses (manifest/lint-env, delivered-files lint, live hooks,
    launcher pin resolution): no commit, no PR, and the message is the loud
    diagnostic listing every failed check. The managed set must never fail its
    own checks — the fix belongs in shipit, never in the consumer.
    """

    #: The coarse install step the failure-path flow event names (#434): a
    #: refusal of this type happened in self-certification, whatever outer step
    #: the emitting verb was tracking.
    step: str = "self-certification"
