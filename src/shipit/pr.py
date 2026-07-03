"""pr — shipit's canonical PR value objects + the one PR-core boundary (ADR-0024).

The **deep module** for a GitHub pull request as shipit models it: the
:class:`PrId` identity — ``(repo, number)``, nothing fetched — and ONE
:class:`PR` value object composing it plus the cheap **core** state every path
needs (``head_sha``, ``base_ref``, ``is_draft``, ``merge_state``). It replaces
the two competing snapshots (``PullContext`` / ``PRContext``) that modeled a PR
twice and read ``head_sha`` three ways.

:class:`PrId` is what a verb resolves at the CLI boundary and passes down
(ADR-0030): the PR target travels typed from the top — the repo rides along on
the identity, per ADR-0024's offline identity source — so knowing *which* PR
never requires a wire read, and no service re-derives the ambient repo per
fetch. :class:`PR` composes a PrId the way a view composes a PR — one noun at
two granularities, not a second snapshot type.

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

Style per ADR-0021: thin, frozen, composable value objects with logic in free
functions over them (:func:`core_from_node`), so the module is unit-testable in
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
class PrId:
    """The identity half of a :class:`PR` as its own value object — ``(repo,
    number)``, nothing fetched.

    What a verb resolves at the CLI boundary (the PR-target resolver mints it —
    explicit number vs the current branch's PR, repo from the root context) and
    passes down through the pr-family services, so the PR target travels typed
    instead of as a bare ``int`` whose repo gets re-derived along the way
    (ADR-0030). Composes the :class:`shipit.identity.Repo` identity from WS01;
    :class:`PR` composes a PrId the way a view composes a PR.

    Construction is the validity check (ADR-0030's construction-is-validation):
    ``number`` must be a real, positive ``int`` — the exact-type check rejects
    the ``bool`` subclass too, so a ``"7"``/``None``/``7.0``/``True`` from any
    feeder dies here instead of minting a corrupt identity.
    """

    repo: Repo
    number: int

    def __post_init__(self) -> None:
        if type(self.number) is not int:
            raise ValueError(
                f"PR number must be int, got {self.number!r} "
                f"({type(self.number).__name__})"
            )
        if self.number < 1:
            raise ValueError(f"PR number must be positive, got {self.number!r}")

    @property
    def slug(self) -> str:
        """The canonical ``owner/name`` GitHub slug of this PR's repo."""
        return self.repo.slug


@dataclass(frozen=True)
class PR:
    """A GitHub pull request as shipit's value object — a composed :class:`PrId`
    identity plus cheap **core** state.

    Frozen and thin (ADR-0021), composing its identity (ADR-0024) exactly the
    way a view composes a PR: a PR is the same identity regardless of which VIEW
    enriched it, and every PR-scoped join keys on the ``(repo, number)`` the
    :class:`PrId` carries. ``repo`` / ``number`` / ``slug`` are delegating
    properties, so the identity fields live ONCE, on ``self.id``.

    ``head_sha`` is a :class:`shipit.identity.Sha` (COR02): validated full hex,
    lowercase-normalized at the boundary, equality full-vs-full only — so a case
    or length mismatch can never silently flip a downstream comparison (most
    load-bearingly review staleness).

    The core fields are **required** — no defaults. ``base_ref`` and
    ``merge_state`` are ``| None`` because GitHub itself returns them null (a base
    always exists, but ``mergeStateStatus`` is ``UNKNOWN``/absent until GitHub
    finishes computing it), NEVER because a builder may omit them. That is what
    forecloses the ``is_draft=False`` latent trap: a path that never fetched
    ``is_draft`` cannot construct a :class:`PR` at all, so a defaulted core field
    can never masquerade as a fetched one.
    """

    id: PrId
    head_sha: Sha
    base_ref: str | None
    is_draft: bool
    merge_state: str | None

    @property
    def repo(self) -> Repo:
        """The PR identity's :class:`~shipit.identity.Repo` (via the composed id)."""
        return self.id.repo

    @property
    def number(self) -> int:
        """The PR number (via the composed id)."""
        return self.id.number

    @property
    def slug(self) -> str:
        """The canonical ``owner/name`` GitHub slug of this PR's repo."""
        return self.id.slug


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
    fail-loud-core invariant this boundary exists to enforce). ``number`` — the
    PR's identity field — is validated by :class:`PrId` itself (construction is
    the validity check: exact-``int``, positive), re-raised here with the wire
    context so a ``"7"``/``None``/``7.0`` from fixture or API drift dies at the
    one wire read instead of minting a corrupt identity.
    ``baseRefName`` / ``mergeStateStatus`` use ``.get`` because GitHub itself
    returns them null.
    """
    try:
        pr_id = PrId(repo=repo, number=node["number"])
    except ValueError as exc:
        raise ValueError(f"malformed PR node: {exc}") from exc
    is_draft = node["isDraft"]
    if not isinstance(is_draft, bool):
        raise ValueError(
            f"malformed PR node: isDraft must be a bool, got {is_draft!r} "
            f"({type(is_draft).__name__})"
        )
    return PR(
        id=pr_id,
        head_sha=Sha(node["headRefOid"]),
        base_ref=node.get("baseRefName"),
        is_draft=is_draft,
        merge_state=node.get("mergeStateStatus"),
    )
