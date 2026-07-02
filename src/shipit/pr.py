"""pr — shipit's canonical PR value object + the one PR-core boundary (ADR-0024).

The **deep module** for a GitHub pull request as shipit models it: ONE :class:`PR`
value object — identity ``(repo, number)`` composing the :class:`shipit.identity.Repo`
from WS01, plus the cheap **core** state every path needs (``head_sha``,
``base_ref``, ``is_draft``, ``merge_state``). It replaces the two competing
snapshots (``PullContext`` / ``PRContext``) that modeled a PR twice and read
``head_sha`` three ways.

The **readiness path** (:mod:`shipit.prstate`) and the **review path**
(:mod:`shipit.review`) never build parallel half-overlapping snapshots: each builds
a richer **view** that *composes* a :class:`PR` (readiness view: + reviews /
threads / funnel / timing; review view: + diff / changed_files / workdir). A view
may only expose a field its path actually fetched, so a core field can never be
defaulted-in on a path that never read it — the ``is_draft=False`` latent trap the
old light builder carried.

:func:`core_from_node` is the ONE place the core is read off the wire — off a GitHub
``pullRequest`` node, whether it arrived via ``gh pr view --json`` or GraphQL (both
use the same camelCase keys, see :data:`CORE_JSON_FIELDS`). Every builder funnels
its core through it, so ``head_sha`` is fetched exactly one way and no builder
hardcodes a core field.

Style per ADR-0021: a thin, frozen, composable value object with logic in free
functions over it (:func:`core_from_node`), so the module is unit-testable in
isolation with plain dicts and fixtures. Slug parsing is NOT here: an
``owner/name`` string becomes a :class:`~shipit.identity.Repo` through the one
canonical parser, :func:`shipit.identity.repo_from_slug`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .identity import Repo, Sha

#: The GitHub ``pullRequest`` node fields the PR core is read from. The
#: ``gh pr view --json`` field list AND a GraphQL ``pullRequest`` selection set
#: share these camelCase names, so ONE :func:`core_from_node` consumes either shape
#: — which is what lets every fetch path route its core through a single boundary.
CORE_JSON_FIELDS = (
    "number",
    "headRefOid",
    "baseRefName",
    "isDraft",
    "mergeStateStatus",
)


@dataclass(frozen=True)
class PR:
    """A GitHub pull request as shipit's value object — identity ``(repo, number)``
    plus cheap **core** state.

    Frozen and thin (ADR-0021), composing the :class:`shipit.identity.Repo` identity
    (ADR-0024): a PR is the same identity regardless of which VIEW enriched it, and
    every PR-scoped join keys on ``(repo, number)``.

    ``head_sha`` is a :class:`shipit.identity.Sha` (COR02): validated full hex,
    lowercase-normalized at the boundary, equality full-vs-full only — so a case
    or length mismatch can never silently flip a downstream comparison (most
    load-bearingly review staleness).

    The four core fields are **required** — no defaults. ``base_ref`` and
    ``merge_state`` are ``| None`` because GitHub itself returns them null (a base
    always exists, but ``mergeStateStatus`` is ``UNKNOWN``/absent until GitHub
    finishes computing it), NEVER because a builder may omit them. That is what
    forecloses the ``is_draft=False`` latent trap: a path that never fetched
    ``is_draft`` cannot construct a :class:`PR` at all, so a defaulted core field
    can never masquerade as a fetched one.
    """

    repo: Repo
    number: int
    head_sha: Sha
    base_ref: str | None
    is_draft: bool
    merge_state: str | None

    @property
    def slug(self) -> str:
        """The canonical ``owner/name`` GitHub slug of this PR's repo."""
        return self.repo.slug


def core_from_node(node: dict, repo: Repo) -> PR:
    """Build the :class:`PR` core from a GitHub ``pullRequest`` node — the ONE boundary.

    ``node`` is either a ``gh pr view --json`` dict OR a GraphQL ``pullRequest``
    node; both carry the same camelCase keys (:data:`CORE_JSON_FIELDS`). This is the
    single place ``head_sha`` — and the rest of the core — is read off the wire, so
    it is fetched exactly one way across the readiness and review paths (killing the
    old 3-ways/2-shapes divergence).

    ``number``, ``headRefOid`` and ``isDraft`` are read with ``node[...]`` (required
    keys) so a payload that omitted them fails LOUD here rather than silently
    defaulting a core field — the anti-``is_draft=False`` discipline enforced at the
    boundary. ``headRefOid`` is minted into a :class:`shipit.identity.Sha` HERE, so
    a malformed/abbreviated/empty head sha raises :class:`ValueError` at the one
    wire read instead of flowing on as a bogus commit identity. ``isDraft`` is
    additionally required to be a real ``bool``: a ``null``
    or non-bool value is a malformed node and RAISES rather than being silently
    coerced to ``False`` by ``bool(...)`` (which would undermine the very
    fail-loud-core invariant this boundary exists to enforce). ``baseRefName`` /
    ``mergeStateStatus`` use ``.get`` because GitHub itself returns them null.
    """
    is_draft = node["isDraft"]
    if not isinstance(is_draft, bool):
        raise ValueError(
            f"malformed PR node: isDraft must be a bool, got {is_draft!r} "
            f"({type(is_draft).__name__})"
        )
    return PR(
        repo=repo,
        number=node["number"],
        head_sha=Sha(node["headRefOid"]),
        base_ref=node.get("baseRefName"),
        is_draft=is_draft,
        merge_state=node.get("mergeStateStatus"),
    )
