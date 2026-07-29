"""The PR-state engine's one semantic error, distinct from a transport
failure. See docs/adr/0028-one-exec-seam-tool-adapters.md."""

from __future__ import annotations


class PrStateError(RuntimeError):
    """The engine failed in an expected way; the message is user-renderable.

    Raised for semantic (non-transport) failures — a GraphQL payload
    carrying ``errors``, unusable ``gh`` output, a dropped review-request
    edge — which the ``pr`` verbs catch and render as a clean CLI error.
    """
