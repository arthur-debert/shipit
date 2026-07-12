"""Secrets derivation — requirements from declarations (TOL02-WS02, PRD 43-46).

The requirements/sources split that gives the fleet one secret map with three
consumers that cannot drift. Registry entries declare the GitHub secret NAMES
they require (this module's closed tables); the repo's ``.shipit.toml
[secrets]`` table keeps per-repo SOURCES only (the table key IS the GitHub
secret name mapped to one doppler/env/prompt source — architecture.lex §6, the
resolution pipeline in :mod:`shipit.secretsrc` unchanged). Deriving the
required set is a pure traversal of the repo's declarations — the artifact
map's endpoints, its ``sign`` declarations AND its self-signing bundle
compositions (electron self-signs its darwin leg inside the bundler, so it
demands the SAME Apple cert pair + notary trio as ``sign = true`` — keyed on
the composition since electron refuses ``sign``, #790), the prepare stage's
push, and (a third derivation input, #740) the ``[reviewers]`` declarations,
whose funnel backends contribute their GitHub-App credential pair off the
Backend registry — consumed identically by:

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

Requirements come in two shapes, and only two: plain NAMES (required or not
— :class:`Requirement`) and ONE either-satisfies shape,
:class:`AlternativeSet` (#746) — a requirement any of its complete
alternative name sets satisfies. Today's single instance is the notary
credentials (:data:`NOTARY_SECRETS`: the ASC API-key trio OR the Apple-ID
trio, both first-class CI paths). All three consumers honour it: gh-setup
accepts either sourced trio (neither demanding nor orphaning the unused
one) and fails with ONE diagnostic naming what is missing from every
alternative when none is complete; preflight validates either env trio the
same way; the caller block forwards every alternative's names (an
unprovisioned name forwards empty — never forcing both trios). This is a
deliberate stop short of a general Boolean requirements language.

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

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from ..agent import backend as _backend
from ..config import Artifact, SecretSource
from . import bundle

#: The prepare stage's requirement: its bump commit + tag push must pass the
#: branch ruleset, so every shipit-released repo needs the push token
#: (PRD story 24/43). Required of every RELEASE-CAPABLE repo — one whose
#: artifact map declares a distribution endpoint; a repo that never releases
#: has nothing required of it.
PREPARE_SECRETS: tuple[str, ...] = ("RELEASE_TOKEN",)

#: The sign-mac stage's unconditional requirement names: the Apple signing
#: certificate pair. ONE spelling per secret — the legacy workflows carried
#: two cert spellings (``APPLE_CERTIFICATE_P12_BASE64`` on the rust/go paths,
#: ``APPLE_CERTIFICATE`` on tauri/electron); TOL02 unifies on
#: ``APPLE_CERTIFICATE`` (architecture.lex §6's exemplar), recorded here
#: rather than preserved twice. The notary credentials are NOT here: they are
#: an either-trio-satisfies requirement (:data:`NOTARY_SECRETS`, #746), not a
#: conjunction of names.
SIGN_MAC_CERT_SECRETS: tuple[str, ...] = (
    "APPLE_CERTIFICATE",
    "APPLE_CERTIFICATE_PASSWORD",
)

#: Notary alternative 1 — the App Store Connect API-key trio. Takes
#: precedence at the signer when both trios are complete
#: (:func:`shipit.release.sign.resolve_notary`; the drift-guard test in
#: ``tests/test_release_sign.py`` pins these spellings to the signer's).
ASC_NOTARY_SECRETS: tuple[str, ...] = (
    "ASC_API_KEY_BASE64",
    "ASC_API_KEY_ID",
    "ASC_API_ISSUER_ID",
)

#: Notary alternative 2 — Apple ID + app-specific password + team id: the
#: first-class CI alternative for repos without an ASC API key (#746).
APPLE_ID_NOTARY_SECRETS: tuple[str, ...] = (
    "APPLE_ID",
    "APPLE_PASSWORD",
    "APPLE_TEAM_ID",
)


@dataclass(frozen=True)
class SecretAlternative:
    """One complete way to satisfy an :class:`AlternativeSet`: a human
    ``label`` (the diagnostics' vocabulary) and the secret ``names`` that must
    ALL be present/sourced for this alternative to count."""

    label: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class AlternativeSet:
    """One requirement satisfied by ANY complete alternative name set (#746).

    The focused either-satisfies abstraction — deliberately NOT a general
    Boolean requirements language: plain names stay "required or not", and
    this one shape carries the single OR the model needs (the notary trios).
    ``label`` names the requirement itself; ``alternatives`` are the complete
    credential sets that satisfy it, precedence order (the signer's
    resolution order — first complete set wins).
    """

    label: str
    alternatives: tuple[SecretAlternative, ...]

    def names(self) -> tuple[str, ...]:
        """Every name any alternative consumes, flat, first-seen order — the
        provision/forward surface (all are accepted; none is individually
        demanded)."""
        seen: dict[str, None] = {}
        for alt in self.alternatives:
            for name in alt.names:
                seen[name] = None
        return tuple(seen)

    def satisfied(self, present: Collection[str]) -> bool:
        """Whether ``present`` (the sourced/provisioned name set) completes
        at least one alternative."""
        return any(
            all(name in present for name in alt.names) for alt in self.alternatives
        )

    def describe_gap(self, present: Collection[str]) -> str:
        """The one diagnostic for an unsatisfied set: what is missing from
        EACH alternative, so the reader can pick whichever is cheapest to
        complete. Meaningful only when :meth:`satisfied` is false."""
        gaps = []
        for alt in self.alternatives:
            missing = ", ".join(n for n in alt.names if n not in present)
            gaps.append(f"{alt.label} (missing: {missing})" if missing else alt.label)
        return f"{self.label}: one complete set needed — " + " or ".join(gaps)

    def to_dict(self) -> dict:
        """The ``--json`` projection (the release plan embeds it)."""
        return {
            "label": self.label,
            "alternatives": [
                {"label": alt.label, "names": list(alt.names)}
                for alt in self.alternatives
            ],
        }


#: The sign-mac stage's notary requirement (#746): EITHER complete trio
#: satisfies it — the ASC API-key trio (precedence, matching the signer's
#: resolution) or the Apple-ID trio. Both are first-class CI paths; gh-setup
#: accepts whichever the repo sources without demanding or orphaning the
#: other, and the workflow chain forwards both.
NOTARY_SECRETS: AlternativeSet = AlternativeSet(
    label="notary credentials",
    alternatives=(
        SecretAlternative(label="ASC API-key trio", names=ASC_NOTARY_SECRETS),
        SecretAlternative(label="Apple-ID trio", names=APPLE_ID_NOTARY_SECRETS),
    ),
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
    (:data:`SIGN_MAC_CERT_SECRETS`), then per declared reviewer its credential
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
        # Only the CERT PAIR is an unconditional requirement: the notary
        # credentials are an either-trio-satisfies alternative set (#746),
        # derived separately by :func:`alternative_requirements`. An electron
        # bundle self-signs its darwin output at BUNDLE time (electron-builder's
        # CSC path) — the SAME cert pair, keyed on the composition instead of
        # `sign = true` (which electron refuses); mutually exclusive with it
        # (:func:`shipit.release.bundle.artifact_self_signs_mac`, #790).
        if artifact.sign:
            reqs.extend(
                Requirement(
                    name=name,
                    required_by=f"sign-mac stage (artifact {artifact.name})",
                )
                for name in SIGN_MAC_CERT_SECRETS
            )
        elif bundle.artifact_self_signs_mac(artifact):
            reqs.extend(
                Requirement(
                    name=name,
                    required_by=f"electron bundle self-sign (artifact {artifact.name})",
                )
                for name in SIGN_MAC_CERT_SECRETS
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


@dataclass(frozen=True)
class AlternativeRequirement:
    """One derived either-satisfies requirement (#746): the
    :class:`AlternativeSet` and the ``required_by`` registry entry that
    declared it — the same error-message anchor plain requirements carry."""

    sets: AlternativeSet
    required_by: str


def alternative_requirements(
    artifacts: Sequence[Artifact],
) -> tuple[AlternativeRequirement, ...]:
    """The either-satisfies requirements the declarations contribute (#746):
    one :data:`NOTARY_SECRETS` entry per SIGNING artifact, declaration order
    — today's ONE alternative-shaped requirement (the notary trios). A signing
    artifact is one that declares ``sign = true`` (the rust reopen→reseal path)
    OR whose bundle composition self-signs its own darwin output
    (electron-builder's notarize path — :func:`shipit.release.bundle.artifact_self_signs_mac`,
    TOL02-WS14 #790): both notarize a darwin ``.dmg``, so both need the notary
    trio. Consumed beside :func:`requirements` by gh-setup's sync (either
    sourced trio satisfies it) and preflight's presence validation (either env
    trio)."""
    out: list[AlternativeRequirement] = []
    for artifact in artifacts:
        if artifact.sign:
            out.append(
                AlternativeRequirement(
                    sets=NOTARY_SECRETS,
                    required_by=f"sign-mac stage (artifact {artifact.name})",
                )
            )
        elif bundle.artifact_self_signs_mac(artifact):
            out.append(
                AlternativeRequirement(
                    sets=NOTARY_SECRETS,
                    required_by=f"electron bundle self-sign (artifact {artifact.name})",
                )
            )
    return tuple(out)


def accepted_names(
    artifacts: Sequence[Artifact], *, reviewers: Sequence[str] = ()
) -> tuple[str, ...]:
    """Every name the derivation ACCEPTS, deduplicated, first-seen order: the
    required names plus every live alternative set's names (#746). This is
    the provision/forward surface — a declared source with one of these names
    is never an orphan, and the cross-org caller forwards all of them — while
    only :func:`required_names` are individually DEMANDED (an alternative's
    names are demanded as either-complete-set, never one by one)."""
    seen: dict[str, None] = dict.fromkeys(
        required_names(artifacts, reviewers=reviewers)
    )
    for alt_req in alternative_requirements(artifacts):
        for name in alt_req.sets.names():
            seen[name] = None
    return tuple(seen)


def unsatisfied_alternatives(
    artifacts: Sequence[Artifact], sources: Sequence[SecretSource]
) -> tuple[AlternativeRequirement, ...]:
    """The alternative requirements NO complete alternative satisfies from
    the declared ``[secrets]`` sources — the either-set twin of
    :func:`missing_sources` (#746). One entry per requiring declaration
    (mirroring the plain report); a partial trio beside a complete other trio
    is satisfied, never an error. Render each gap with
    ``req.sets.describe_gap(declared)`` — ONE diagnostic naming what is
    missing from every alternative."""
    declared = {source.name for source in sources}
    return tuple(
        alt_req
        for alt_req in alternative_requirements(artifacts)
        if not alt_req.sets.satisfied(declared)
    )


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
    pushed. :data:`TOLERATED` names are exempt by definition. An alternative
    set's names (#746) are kept whenever the set is live — a signing repo may
    source EITHER notary trio, or even both, without the unused one being
    flagged; on a non-signing repo they are normal orphans.
    """
    keep = set(accepted_names(artifacts, reviewers=reviewers)) | set(TOLERATED)
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

    Lists the ACCEPTED names (:func:`accepted_names`), not just the demanded
    ones: an alternative set's every name is forwarded (#746) so the caller
    works with whichever notary trio the repo provisioned — forwarding an
    unprovisioned name passes empty, which preflight/the signer already treat
    as absent, so listing both trios never forces both.
    """
    names = accepted_names(artifacts)
    if not names:
        return ""
    lines = ["secrets:"]
    lines.extend(f"  {name}: ${{{{ secrets.{name} }}}}" for name in names)
    return "\n".join(lines)
