"""Secrets derivation — requirements from declarations (TOL02-WS02, PRD 43-46).

The requirements/sources split that gives the fleet one secret map with three
consumers that cannot drift. Registry entries declare the GitHub secret NAMES
they require (this module's closed tables); the repo's ``.shipit.toml
[secrets]`` table keeps per-repo SOURCES only (the table key IS the GitHub
secret name mapped to one doppler/env/prompt source — architecture.lex §6, the
resolution pipeline in :mod:`shipit.secretsrc` unchanged). Deriving the
required set is a pure traversal of the repo's declarations — the artifact
map's endpoints, its ``sign`` declarations, and the prepare stage's push —
consumed identically by:

1. ``gh-setup``'s secrets sync (:mod:`shipit.ghsetup`): a required name with
   no declared source is a SYNC-TIME error naming the requiring entry (story
   45), and a declared/pushed secret nothing requires is flagged as an orphan
   — the derived set replaces push-everything-in-``[secrets]``, so a repo can
   never under- or over-provision (story 44).
2. preflight presence validation (:mod:`shipit.release.preflight`): the plan's
   ``secrets`` field is built from these same tables, scoped to the plan's
   live endpoints and stages (story 28's hard fail consumes it).
3. the cross-org caller's generated ``secrets:`` block
   (:func:`secrets_block`): ``secrets: inherit`` dies at the org boundary, so
   cross-org consumers list every derived name explicitly (story 46); the
   WS06 caller generation renders this function's output.

Adding an endpoint adapter later is its :data:`ENDPOINT_SECRETS` entry (plus
the adapter itself, WS05) — the derivation functions never change (story 43).

Pure module: no I/O, no config parsing (it consumes the typed
:class:`~shipit.config.Artifact` / :class:`~shipit.config.SecretSource`
values), fixture-tested over the PRD Testing Decisions' named cases — sync
set, validation set, orphans, missing-source errors.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..config import Artifact, SecretSource

#: The prepare stage's requirement: its bump commit + tag push must pass the
#: branch ruleset, so every shipit-released repo needs the push token
#: (PRD story 24/43). Required of every RELEASE-CAPABLE repo — one whose
#: artifact map declares a distribution endpoint; a repo that never releases
#: has nothing required of it.
PREPARE_SECRETS: tuple[str, ...] = ("RELEASE_TOKEN",)

#: The sign-mac stage's requirement names: the Apple signing certificate pair
#: plus the App Store Connect notary trio. ONE spelling per secret — the
#: legacy workflows carried two cert spellings (``APPLE_CERTIFICATE_P12_BASE64``
#: on the rust/go paths, ``APPLE_CERTIFICATE`` on tauri/electron); TOL02
#: unifies on ``APPLE_CERTIFICATE`` (architecture.lex §6's exemplar), recorded
#: here rather than preserved twice.
SIGN_MAC_SECRETS: tuple[str, ...] = (
    "APPLE_CERTIFICATE",
    "APPLE_CERTIFICATE_PASSWORD",
    "ASC_API_KEY_BASE64",
    "ASC_API_KEY_ID",
    "ASC_API_ISSUER_ID",
)

#: Per-endpoint requirement declarations (story 43), keyed by exactly the
#: closed :data:`shipit.config.ENDPOINTS` set (drift-guarded by test).
#: ``gh-release`` requires nothing extra: the workflow's ambient
#: ``GITHUB_TOKEN`` publishes the GitHub release.
ENDPOINT_SECRETS: dict[str, tuple[str, ...]] = {
    "gh-release": (),
    "crates": ("CRATES_IO_KEY",),
    "pypi": ("PYPI_TOKEN",),
    "npm": ("NPM_TOKEN",),
    "brew": ("HOMEBREW_TAP_TOKEN",),
}

#: The pypi adapter's testpypi flag adds this name to the pypi entry's
#: requirements when the flag lands with the adapter itself (WS05, PRD story
#: 34/43). Registered now so the name has one home; nothing derives it until
#: the flag is declarable.
TESTPYPI_SECRET: str = "TESTPYPI_TOKEN"

#: Names tolerated in ``[secrets]`` without a requiring entry — never flagged
#: as orphans (and, unlike requirements, never demanded). ``SCCACHE_GCS_KEY``
#: is the optional build-cache credential (legacy inventory: optional
#: everywhere): a repo declares it to feed remote sccache, and no release
#: stage depends on it. ``RELEASE_TOKEN`` is here for the
#: NOT-yet-release-capable repo (no endpoints declared) that provisions its
#: push token ahead of its artifact map — declaring early is preparation,
#: not drift; once an endpoint exists the name graduates to a requirement.
TOLERATED: tuple[str, ...] = ("SCCACHE_GCS_KEY", "RELEASE_TOKEN")


@dataclass(frozen=True)
class Requirement:
    """One derived secret requirement: the GitHub secret ``name`` (the
    ``[secrets]`` table-key vocabulary) and the ``required_by`` registry
    entry that declared it — the error-message anchor story 45 demands."""

    name: str
    required_by: str


def requirements(artifacts: Sequence[Artifact]) -> tuple[Requirement, ...]:
    """The full derived requirement set — the repo's declarations traversed.

    Order is deterministic: prepare's push first (required as soon as any
    artifact declares an endpoint — the repo is release-capable), then per
    artifact in declaration order its endpoints (each via
    :data:`ENDPOINT_SECRETS`) and its ``sign`` declaration
    (:data:`SIGN_MAC_SECRETS`). A name required by several entries appears
    once per requiring entry — collapse with :func:`required_names` when
    only the name set matters.
    """
    release_capable = any(artifact.endpoints for artifact in artifacts)
    reqs: list[Requirement] = [
        Requirement(name=name, required_by="prepare push")
        for name in (PREPARE_SECRETS if release_capable else ())
    ]
    for artifact in artifacts:
        for endpoint in artifact.endpoints:
            for name in ENDPOINT_SECRETS[endpoint]:
                reqs.append(
                    Requirement(
                        name=name,
                        required_by=f"endpoint {endpoint} (artifact {artifact.name})",
                    )
                )
        if artifact.sign:
            reqs.extend(
                Requirement(
                    name=name,
                    required_by=f"sign-mac stage (artifact {artifact.name})",
                )
                for name in SIGN_MAC_SECRETS
            )
    return tuple(reqs)


def required_names(artifacts: Sequence[Artifact]) -> tuple[str, ...]:
    """The derived requirement NAMES, deduplicated, first-seen order."""
    seen: dict[str, None] = {}
    for req in requirements(artifacts):
        seen.setdefault(req.name)
    return tuple(seen)


def missing_sources(
    artifacts: Sequence[Artifact], sources: Sequence[SecretSource]
) -> tuple[Requirement, ...]:
    """The requirements no ``[secrets]`` entry sources — story 45's sync-time
    error set, each naming its requiring entry. One entry per (name,
    requiring entry) pair, so the report shows every declaration that goes
    unserved."""
    declared = {source.name for source in sources}
    return tuple(req for req in requirements(artifacts) if req.name not in declared)


def orphans(
    artifacts: Sequence[Artifact],
    sources: Sequence[SecretSource],
    *,
    extra_required: Iterable[str] = (),
) -> tuple[str, ...]:
    """The declared ``[secrets]`` names NOTHING requires (story 45's orphan
    flag), in declaration order.

    ``extra_required`` is the caller's non-release requirement set — gh-setup
    passes the seeded App-secret names
    (:func:`shipit.config.seeded_app_secrets`), which the review funnel
    requires outside this module's registries. :data:`TOLERATED` names are
    exempt by definition.
    """
    keep = set(required_names(artifacts)) | set(extra_required) | set(TOLERATED)
    return tuple(source.name for source in sources if source.name not in keep)


def secrets_block(artifacts: Sequence[Artifact]) -> str:
    """The cross-org caller's ``secrets:`` block (story 46), rendered from
    the same derivation gh-setup syncs — the two cannot drift.

    ``secrets: inherit`` only propagates within one owner, so a cross-org
    consumer's caller workflow lists every derived name explicitly with the
    mapped (GitHub) name. The WS06 caller generation embeds this text
    verbatim; no trailing newline (the embedder owns layout).
    """
    lines = ["secrets:"]
    lines.extend(
        f"  {name}: ${{{{ secrets.{name} }}}}" for name in required_names(artifacts)
    )
    return "\n".join(lines)
