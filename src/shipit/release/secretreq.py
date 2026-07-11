"""Secrets derivation — requirements from declarations (TOL02-WS02, PRD 43-46).

The requirements/sources split that gives the fleet one secret map with three
consumers that cannot drift. Registry entries declare the GitHub secret NAMES
they require (this module's closed tables); the repo's ``.shipit.toml
[secrets]`` table keeps per-repo SOURCES only (the table key IS the GitHub
secret name mapped to one doppler/env/prompt source — architecture.lex §6, the
resolution pipeline in :mod:`shipit.secretsrc` unchanged). Deriving the
required set is a pure traversal of the repo's declarations — the artifact
map's endpoints, its ``sign`` declarations, the prepare stage's push, and (a
third derivation input, #740) the ``[reviewers]`` declarations, whose funnel
backends contribute their GitHub-App credential pair off the Backend registry
— consumed identically by:

1. ``gh-setup``'s secrets sync (:mod:`shipit.ghsetup`): a required name with
   no declared source is a SYNC-TIME error naming the requiring entry (story
   45), and a declared/pushed secret nothing requires is flagged as an orphan
   — the derived set replaces push-everything-in-``[secrets]``, so a repo can
   never under- or over-provision (story 44). gh-setup is the PROVISIONING
   consumer: it is the one caller that passes ``reviewers`` (the validated
   roster's required names), so reviewer App credentials are demanded exactly
   where the repo runs App reviewers and orphan-flagged where it doesn't.
2. preflight presence validation (:mod:`shipit.release.preflight`): the plan's
   ``secrets`` field is built from these same tables, scoped to the plan's
   live endpoints and stages (story 28's hard fail consumes it).
3. the cross-org caller's generated ``secrets:`` block
   (:func:`secrets_block`): ``secrets: inherit`` dies at the org boundary, so
   cross-org consumers list every derived name explicitly (story 46); the
   WS06 caller generation renders this function's output.

Consumers 2 and 3 are the RELEASE-ONLY projection: they never see the
reviewer contribution (they take no ``reviewers``), because forwarding the
review funnel's App credentials into the reusable release workflow contract
would leak repository-provisioning concerns across the workflow-chain
boundary (ADR-0040) — the release chain has no use for a review bot's PEM.

Adding an endpoint adapter later is its :data:`ENDPOINT_SECRETS` entry (plus
the adapter itself, WS05) — the derivation functions never change (story 43).
Adding a funnel backend likewise is its Backend-registry entry alone: the
reviewer traversal reads the registry's Doppler-key aliases, never a copy.

Pure module: no I/O, no config parsing (it consumes the typed
:class:`~shipit.config.Artifact` / :class:`~shipit.config.SecretSource`
values and plain reviewer NAMES off an already-validated
:class:`~shipit.prstate.roster.Roster`), fixture-tested over the PRD Testing
Decisions' named cases — sync set, validation set, orphans, missing-source
errors, reviewer credential pairs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..agent import backend as _backend
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
#: closed :data:`shipit.config.ENDPOINTS` set (drift-guarded by test). These
#: are GITHUB SECRET NAMES (the ``[secrets]`` table key, architecture.lex §6),
#: which may differ from the source key: crates.io's token is sourced under
#: ``CRATES_IO_KEY`` but Cargo reads it as ``CARGO_REGISTRY_TOKEN`` (§6's
#: exemplar — ``CARGO_REGISTRY_TOKEN = { doppler = "CRATES_IO_KEY" }``), so the
#: REQUIRED name is the gh-secret one. ``gh-release`` requires nothing extra:
#: the workflow's ambient ``GITHUB_TOKEN`` publishes the GitHub release.
ENDPOINT_SECRETS: dict[str, tuple[str, ...]] = {
    "gh-release": (),
    "crates": ("CARGO_REGISTRY_TOKEN",),
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


def reviewer_requirements(reviewers: Sequence[str]) -> tuple[Requirement, ...]:
    """The credential requirements the ``[reviewers]`` declarations contribute
    (#740, option C) — one (PEM, App-id) pair per declared FUNNEL reviewer.

    ``reviewers`` are required-reviewer NAMES off the validated roster
    (:attr:`shipit.prstate.roster.Roster.required_names` — canonical lowercase
    adapter names). A local-agent reviewer's name IS its backend's funnel-agent
    alias (``codex`` / ``agy``), so the pair derives from the Backend registry
    (:pyattr:`~shipit.agent.backend.Backend.doppler_pem_key` /
    :pyattr:`~shipit.agent.backend.Backend.doppler_app_id_key`) — the GitHub
    secret NAME equals the Doppler key by seeding convention
    (:func:`shipit.config.secrets_scaffold`). A hosted reviewer (copilot,
    coderabbit, gemini) matches no funnel backend and contributes nothing —
    its credentials are the platform's, not ours to provision.
    """
    reqs: list[Requirement] = []
    for reviewer in reviewers:
        try:
            b = _backend.by_funnel_agent(reviewer)
        except KeyError:
            continue  # hosted reviewer — no App credential pair to provision
        reqs.extend(
            Requirement(
                name=name, required_by=f"reviewer {reviewer} ([reviewers] declaration)"
            )
            for name in (b.doppler_pem_key, b.doppler_app_id_key)
        )
    return tuple(reqs)


def requirements(
    artifacts: Sequence[Artifact], *, reviewers: Sequence[str] = ()
) -> tuple[Requirement, ...]:
    """The full derived requirement set — the repo's declarations traversed.

    Order is deterministic: prepare's push first (required as soon as any
    artifact declares an endpoint — the repo is release-capable), then per
    artifact in declaration order its endpoints (each via
    :data:`ENDPOINT_SECRETS`) and its ``sign`` declaration
    (:data:`SIGN_MAC_SECRETS`), then per declared reviewer its credential
    pair (:func:`reviewer_requirements`). A name required by several entries
    appears once per requiring entry — collapse with :func:`required_names`
    when only the name set matters.

    ``reviewers`` is the PROVISIONING-scope input (#740): gh-setup passes the
    roster's required names; the release-only consumers (preflight, the
    caller's :func:`secrets_block`) leave it empty on purpose — see the module
    docstring's ADR-0040 boundary.
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
        # `sign = true` always meets a darwin lane (config refuses it otherwise,
        # :class:`shipit.config.Artifact`), so the sign-mac names are genuinely
        # required — this cannot demand Apple secrets a plan would then skip.
        if artifact.sign:
            reqs.extend(
                Requirement(
                    name=name,
                    required_by=f"sign-mac stage (artifact {artifact.name})",
                )
                for name in SIGN_MAC_SECRETS
            )
    reqs.extend(reviewer_requirements(reviewers))
    return tuple(reqs)


def required_names(
    artifacts: Sequence[Artifact], *, reviewers: Sequence[str] = ()
) -> tuple[str, ...]:
    """The derived requirement NAMES, deduplicated, first-seen order."""
    seen: dict[str, None] = {}
    for req in requirements(artifacts, reviewers=reviewers):
        seen[req.name] = None
    return tuple(seen)


def missing_sources(
    artifacts: Sequence[Artifact],
    sources: Sequence[SecretSource],
    *,
    reviewers: Sequence[str] = (),
) -> tuple[Requirement, ...]:
    """The requirements no ``[secrets]`` entry sources — story 45's sync-time
    error set, each naming its requiring entry. One entry per (name,
    requiring entry) pair, so the report shows every declaration that goes
    unserved.

    With ``reviewers`` passed (gh-setup's provisioning scope, #740), a
    declared funnel reviewer whose credential pair has no ``[secrets]`` source
    is an error HERE — the sync fails loud where the repo opts the reviewer
    in, instead of the App breaking later at review-posting time."""
    declared = {source.name for source in sources}
    return tuple(
        req
        for req in requirements(artifacts, reviewers=reviewers)
        if req.name not in declared
    )


def orphans(
    artifacts: Sequence[Artifact],
    sources: Sequence[SecretSource],
    *,
    reviewers: Sequence[str] = (),
) -> tuple[str, ...]:
    """The declared ``[secrets]`` names NOTHING requires (story 45's orphan
    flag), in declaration order.

    An App credential rides the derived set like every other requirement
    (#740): declared reviewer → required, never an orphan; seeded-but-
    undeclared (e.g. the install scaffold's codex/agy pairs on a repo whose
    ``[reviewers]`` never opts them in) → a NORMAL orphan, flagged and not
    pushed. :data:`TOLERATED` names are exempt by definition.
    """
    keep = set(required_names(artifacts, reviewers=reviewers)) | set(TOLERATED)
    return tuple(source.name for source in sources if source.name not in keep)


def secrets_block(artifacts: Sequence[Artifact]) -> str:
    """The cross-org caller's ``secrets:`` block (story 46), rendered from
    the same derivation gh-setup syncs — the two cannot drift.

    ``secrets: inherit`` only propagates within one owner, so a cross-org
    consumer's caller workflow lists every derived name explicitly with the
    mapped (GitHub) name. The WS06 caller generation embeds this text
    verbatim; no trailing newline (the embedder owns layout).

    A repo that derives no requirement (a not-yet-release-capable map with no
    endpoints) yields the empty string — the block is omitted entirely rather
    than emitted as a bare ``secrets:`` key, which parses as ``secrets: null``
    and GitHub Actions rejects (it expects ``secrets`` to be a mapping).

    Deliberately takes NO ``reviewers``: this is the RELEASE-ONLY projection
    (#740 / ADR-0040 boundary). Reviewer App credentials are repository
    provisioning (gh-setup's scope) — forwarding them here would leak the
    review funnel's PEM/App-id into the reusable release workflow contract of
    every cross-org consumer.
    """
    names = required_names(artifacts)
    if not names:
        return ""
    lines = ["secrets:"]
    lines.extend(f"  {name}: ${{{{ secrets.{name} }}}}" for name in names)
    return "\n".join(lines)
