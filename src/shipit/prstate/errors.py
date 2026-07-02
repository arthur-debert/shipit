"""The PR-state engine's one semantic error.

ADR-0028 deleted the per-boundary transport errors (the two legacy ``GhError``
classes): a failed subprocess is the runner's single
:class:`shipit.execrun.ExecError`, raised by :mod:`shipit.execrun` and nothing
else. What remains here is the SEMANTIC error the engine genuinely branches on:
"the engine could not complete, in an expected, user-renderable way" — a
GraphQL response carrying errors, unparseable ``gh`` output, a review-request
edge GitHub silently dropped, a local-agent review that could not be started.
None of those is an Exec failure (some never ran a subprocess at all), so they
must not masquerade as :class:`~shipit.execrun.ExecError`; and the ``pr`` verbs
must be able to catch them — alongside ``ExecError`` — to render a clean
stderr + non-zero exit instead of a raw traceback, without swallowing real
bugs. That catch is the one place meaning is branched on, which is exactly the
bar ADR-0028 sets for a semantic error class to exist.
"""

from __future__ import annotations


class PrStateError(RuntimeError):
    """The engine failed in an expected way; the message is user-renderable.

    Raised for semantic (non-transport) failures of the PR-state machinery:
    a GraphQL payload carrying ``errors``, unparseable/unusable ``gh`` output,
    a silently-dropped review-request edge, a local-agent review request that
    failed. The ``pr`` verbs catch ``(ExecError, PrStateError)`` and render
    the message as a clean CLI error.
    """
