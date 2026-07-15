"""Consumer-side Artifact channel — ``[artifact-deps]`` → managed pixi blocks.

The CONSUMER half of the Artifact channel (ADR-0064/0065, ARF01-WS02 #952). A
downstream repo declares a cross-repo artifact-pinned dependency as
``.shipit.toml`` ``[artifact-deps.<pkg>]`` (parsed to a typed
:class:`shipit.config.ArtifactDep`), and ``shipit install`` PROJECTS it here
into managed pixi blocks so pixi resolves/locks/fetches it like any ordinary
dependency and a ``version`` bump re-resolves transparently.

Two halves, kept apart so the projection stays a PURE, network-free core:

- **tier derivation** (:func:`channel_url`) — the access tier is DERIVED from
  the producing repo's visibility (ADR-0065), never declared. WS02 serves the
  PUBLIC tier only: a public producing repo resolves to its authless HTTPS
  per-repo channel URL; a private one is refused with a pointer to WS04 (the
  private-tier ``[s3-options]`` + credentials workstream). The one
  network-touching read — the repo's visibility — lives in the install verb
  glue (``gh.repo_is_private``); this module maps an ALREADY-resolved boolean
  to a URL, so a test exercises it without a round-trip.
- **projection** (:func:`project`) — pure over ``(ArtifactDep, channel_url)``
  pairs: it emits the managed :class:`~shipit.install.units.Unit` blocks the
  reconcile then treats exactly like every other managed block (four-case
  hash-compare, idempotent reconcile-to-noop, ADD/UPDATE on a bump). No
  filesystem, no network — the projection is exercised entirely on values.

Projection shape (the WS02 design, documented for the shepherd): each distinct
TARGET (a declared ``feature`` name, or the default target when ``feature`` is
omitted) becomes one dedicated, shipit-reserved pixi FEATURE carrying that
target's channel URL(s) and version pin(s), wired into an ENVIRONMENT:

- the DEFAULT target (no ``feature``) → the ``shipit-artifacts`` feature, added
  to the ``default`` environment — so the pin resolves on a bare ``pixi
  install`` / ``pixi run`` (the Spec's headline consumer example);
- a NAMED ``feature`` F → the ``shipit-artifacts-<F>`` feature, added to a
  dedicated ``shipit-artifacts-<F>`` environment.

Reserved ``shipit-artifacts*`` feature/env names and EOF-appended feature
tables keep the projection collision-free: it never re-declares a table the
consumer or another managed block already owns, and never merges into an
existing array (which a marker block cannot do). BINDING a pin into a
pre-existing consumer environment (an array merge on that env's feature list or
on ``[workspace].channels``) is deliberately out of WS02 scope — see the PR
Context handoff.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ..config import ArtifactDep
from .units import PIXI_FILE, Unit

#: The public-tier Artifact channel host + bucket (ADR-0065 — the public-read,
#: authless tier; the private tier is WS04). The per-repo channel root is
#: ``<bucket>/<owner/name>`` (each repo the sole writer of its own repodata,
#: ADR-0064), reached over the authless HTTPS object-storage URL. These MIRROR
#: the producer-side constants the ``conda`` endpoint publishes to
#: (:data:`shipit.release.publish.PUBLIC_ARTIFACT_BUCKET`,
#: :data:`~shipit.release.publish.CONDA_S3_ENDPOINT`) — a drift test asserts the
#: two agree so the consumer can never read from a bucket the producer never
#: writes to.
PUBLIC_CHANNEL_HOST = "https://storage.googleapis.com"
PUBLIC_ARTIFACT_BUCKET = "shipit-artifacts-public"

#: The reserved pixi feature name the DEFAULT target's channel+pins land in, and
#: the environment prefix a NAMED feature target's isolated feature/env carry.
#: ``shipit-artifacts``-prefixed names are shipit's own namespace — never a
#: consumer feature — so the projection never collides with a consumer table.
DEFAULT_FEATURE = "shipit-artifacts"
DEFAULT_ENV = "default"

#: The managed environments block's unit key + anchor. One consolidated block
#: (all targets' env wiring) anchored under ``[environments]`` — a sibling of
#: the managed lint-env block under the same table header (the proven
#: coexisting-marker-blocks pattern).
ENVIRONMENTS_KEY = f"{PIXI_FILE}#shipit-artifact-deps-environments"
ENVIRONMENTS_ANCHOR = "[environments]"
ENVIRONMENTS_OPEN = (
    "# >>> shipit-managed artifact-dep environments "
    "(do not edit; regenerate via `shipit install`) >>>"
)
ENVIRONMENTS_CLOSE = "# <<< shipit-managed artifact-dep environments <<<"


class ArtifactChannelError(RuntimeError):
    """A cross-repo artifact-dep cannot be projected (e.g. a private-tier
    producing repo, which is WS04)."""


def public_channel_url(repo_slug: str) -> str:
    """The authless HTTPS per-repo channel URL for a PUBLIC producing repo.

    ``<host>/<bucket>/<owner/name>`` (ADR-0065) — the exact channel a consumer
    lists so pixi resolves the artifact with no credentials.
    """
    return f"{PUBLIC_CHANNEL_HOST}/{PUBLIC_ARTIFACT_BUCKET}/{repo_slug}"


def channel_url(repo_slug: str, *, private: bool) -> str:
    """Derive the channel URL from the producing repo's visibility (ADR-0065).

    ``private`` is the ALREADY-resolved visibility (the verb glue reads it once
    via ``gh.repo_is_private``); the tier is DERIVED from it, never declared.
    WS02 serves the public tier only, so a private producing repo is refused
    loudly with a pointer to WS04 rather than projecting an unauthenticated URL
    that would silently fail to resolve.
    """
    if private:
        raise ArtifactChannelError(
            f"artifact-dep on `{repo_slug}`: the producing repo is PRIVATE, whose "
            f"Artifact channel needs the private tier (S3-interop + credentials) — "
            f"not yet supported (ARF01-WS04). Only public producing repos are "
            f"consumable today."
        )
    return public_channel_url(repo_slug)


def _feature_name(feature: str | None) -> str:
    """The reserved pixi feature name a target's channel+pins land in."""
    return DEFAULT_FEATURE if feature is None else f"{DEFAULT_FEATURE}-{feature}"


def _env_name(feature: str | None) -> str:
    """The environment a target's feature is wired into — the default env for
    the default target, an isolated ``shipit-artifacts-<F>`` env for a named one."""
    return DEFAULT_ENV if feature is None else f"{DEFAULT_FEATURE}-{feature}"


def _toml_str_list(values: Sequence[str]) -> str:
    """A TOML inline array of double-quoted string VALUES (the channel URLs /
    feature names the projection emits are URL/identifier-safe, so no escaping
    is needed). Table headers and dependency KEYS go through :func:`_toml_key`
    instead — a dot is a key-path separator there, but harmless inside a string
    value."""
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


#: TOML bare-key shape — the identifier chars a table header or dependency key
#: may carry UNQUOTED (``A-Za-z0-9_-``). A projected name outside this set (a
#: dotted conda package like ``ruamel.yaml``, or a dotted ``feature``) MUST be
#: emitted as a QUOTED key, else TOML reads the dot as a key-path separator and
#: the one name splits into nested tables/keys — a silently wrong pixi manifest
#: (ARF01-WS02 review). Dots are legitimately valid: the producer's conda
#: package vocabulary admits them (``release.publish._CONDA_PACKAGE_NAME_RE``),
#: so ``config._FEATURE_NAME_RE`` deliberately keeps admitting them and quoting
#: at emission — not rejecting at parse — is what keeps them safe.
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(name: str) -> str:
    """One TOML key / table-name segment, double-quoted only when it must be.

    Bare when every char is bare-key-safe; quoted when it carries a dot (or any
    other non-bare char) so TOML treats it as ONE literal name rather than a
    dotted key-path. Projected names are constrained by ``config._FEATURE_NAME_RE``
    to ``[A-Za-z0-9._-]``, so the only unsafe-for-bare char is ``.`` and a quoted
    key can never need ``"``/``\\``/control-char escaping.
    """
    return name if _BARE_KEY_RE.fullmatch(name) else f'"{name}"'


def _feature_block(
    feature: str | None, resolved: Sequence[tuple[ArtifactDep, str]]
) -> str:
    """The inner text of one target's managed feature block: its channel URLs
    and its version pins, under the reserved ``shipit-artifacts*`` feature.

    Deterministic: channels de-duped in first-seen order (several pins from the
    same producing repo share one channel), pins in declaration order.
    """
    name = _toml_key(_feature_name(feature))
    urls: list[str] = []
    for _, url in resolved:
        if url not in urls:
            urls.append(url)
    lines = [
        f"[feature.{name}]",
        f"channels = {_toml_str_list(urls)}",
        "",
        f"[feature.{name}.dependencies]",
    ]
    lines += [f'{_toml_key(dep.package)} = "{dep.version}"' for dep, _ in resolved]
    return "\n".join(lines)


def _feature_unit(
    feature: str | None, resolved: Sequence[tuple[ArtifactDep, str]]
) -> Unit:
    """One EOF-appended managed block for a target's dedicated feature."""
    name = _feature_name(feature)
    return Unit(
        key=f"{PIXI_FILE}#{name}",
        dest=PIXI_FILE,
        kind="block",
        content=_feature_block(feature, resolved).encode("utf-8"),
        open_marker=(
            f"# >>> shipit-managed artifact-dep feature `{name}` "
            f"(do not edit; regenerate via `shipit install`) >>>"
        ),
        close_marker=f"# <<< shipit-managed artifact-dep feature `{name}` <<<",
        # No anchor: a fresh reserved `[feature.<name>]` table appends at EOF,
        # so it never re-declares a table the consumer already owns.
        anchor=None,
    )


def _environments_unit(features: Sequence[str | None]) -> Unit:
    """The one consolidated environments block wiring every target's feature
    into its environment, anchored under ``[environments]``."""
    lines = [
        f"{_toml_key(_env_name(f))} = {_toml_str_list([_feature_name(f)])}"
        for f in features
    ]
    return Unit(
        key=ENVIRONMENTS_KEY,
        dest=PIXI_FILE,
        kind="block",
        content="\n".join(lines).encode("utf-8"),
        open_marker=ENVIRONMENTS_OPEN,
        close_marker=ENVIRONMENTS_CLOSE,
        anchor=ENVIRONMENTS_ANCHOR,
    )


def project(resolved: Sequence[tuple[ArtifactDep, str]]) -> list[Unit]:
    """Project resolved ``(ArtifactDep, channel_url)`` pairs into managed pixi
    :class:`~shipit.install.units.Unit` blocks — the pure, network-free core.

    Groups the deps by TARGET (the declared ``feature``, or the default target)
    in first-seen order, emits one dedicated-feature block per target, and one
    consolidated environments block wiring them in. ``[]`` for no deps (a repo
    declaring no artifact pin projects nothing). The reconcile treats these
    exactly like every other managed block: idempotent reconcile-to-noop, and a
    ``version`` bump changes a feature block's inner text into a single UPDATE.
    """
    if not resolved:
        return []
    groups: dict[str | None, list[tuple[ArtifactDep, str]]] = {}
    for dep, url in resolved:
        groups.setdefault(dep.feature, []).append((dep, url))
    units = [_feature_unit(feature, pairs) for feature, pairs in groups.items()]
    units.append(_environments_unit(list(groups)))
    return units
