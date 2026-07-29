"""Secrets derivation: requirements from declarations.

Registry entries declare the GitHub secret NAMES they require; a repo's
``[secrets]`` table keeps per-repo SOURCES only. Pure — no I/O.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from ..agent import backend as _backend
from ..config import Artifact, SecretSource

#: Required of every RELEASE-CAPABLE repo — one declaring an endpoint.
PREPARE_SECRETS: tuple[str, ...] = ("RELEASE_TOKEN",)

#: The sign-mac stage's unconditional requirement; the notary credentials are
#: an either-satisfies set, not a conjunction, so they are elsewhere.
SIGN_MAC_CERT_SECRETS: tuple[str, ...] = (
    "APPLE_CERTIFICATE",
    "APPLE_CERTIFICATE_PASSWORD",
)

#: Required names whose EMPTY value is VALID presence — the ONE authority, so
#: no consumer drifts from the signer. Still synced; only the DEMAND relaxes.
EMPTY_VALID_SECRETS: frozenset[str] = frozenset({"APPLE_CERTIFICATE_PASSWORD"})

#: Takes precedence when both trios are complete.
ASC_NOTARY_SECRETS: tuple[str, ...] = (
    "ASC_API_KEY_BASE64",
    "ASC_API_KEY_ID",
    "ASC_API_ISSUER_ID",
)

APPLE_ID_NOTARY_SECRETS: tuple[str, ...] = (
    "APPLE_ID",
    "APPLE_PASSWORD",
    "APPLE_TEAM_ID",
)


@dataclass(frozen=True)
class SecretAlternative:
    label: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class AlternativeSet:
    """One requirement satisfied by ANY complete alternative, in precedence order."""

    label: str
    alternatives: tuple[SecretAlternative, ...]

    def names(self) -> tuple[str, ...]:
        """Every name any alternative consumes, flat, first-seen order."""
        seen: dict[str, None] = {}
        for alt in self.alternatives:
            for name in alt.names:
                seen[name] = None
        return tuple(seen)

    def satisfied(self, present: Collection[str]) -> bool:
        return any(
            all(name in present for name in alt.names) for alt in self.alternatives
        )

    def describe_gap(self, present: Collection[str]) -> str:
        """What is missing from EACH alternative; meaningful only when unsatisfied."""
        gaps = []
        for alt in self.alternatives:
            missing = ", ".join(n for n in alt.names if n not in present)
            gaps.append(f"{alt.label} (missing: {missing})" if missing else alt.label)
        return f"{self.label}: one complete set needed — " + " or ".join(gaps)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "alternatives": [
                {"label": alt.label, "names": list(alt.names)}
                for alt in self.alternatives
            ],
        }


#: EITHER complete trio satisfies it.
NOTARY_SECRETS: AlternativeSet = AlternativeSet(
    label="notary credentials",
    alternatives=(
        SecretAlternative(label="ASC API-key trio", names=ASC_NOTARY_SECRETS),
        SecretAlternative(label="Apple-ID trio", names=APPLE_ID_NOTARY_SECRETS),
    ),
)

#: Per-endpoint requirements, keyed by exactly :data:`shipit.config.ENDPOINTS`.
#: These are GITHUB SECRET NAMES, which may differ from the source key.
ENDPOINT_SECRETS: dict[str, tuple[str, ...]] = {
    "gh-release": (),
    "crates": ("CARGO_REGISTRY_TOKEN",),
    "pypi": ("PYPI_TOKEN",),
    "npm": ("NPM_TOKEN",),
    "vscode-marketplace": ("VSCE_PAT",),
    "open-vsx": ("OVSX_PAT",),
    "brew": ("HOMEBREW_TAP_TOKEN",),
    # The ambient GITHUB_TOKEN cannot dispatch cross-repo.
    "notify-downstreams": ("DOWNSTREAM_DISPATCH_TOKEN",),
    # The producer's WRITE pair for the channel bucket; readers are authless.
    "conda": ("ARTIFACT_CHANNEL_KEY_ID", "ARTIFACT_CHANNEL_SECRET_KEY"),
    # zed only RENDERS registry coordinates for a manual PR — no push.
    "zed": (),
}

#: Nothing derives this as a requirement yet.
TESTPYPI_SECRET: str = "TESTPYPI_TOKEN"

#: Tolerated in ``[secrets]`` with no requiring entry: never orphaned, never
#: demanded.
TOLERATED: tuple[str, ...] = ("SCCACHE_GCS_KEY", "RELEASE_TOKEN")


@dataclass(frozen=True)
class Requirement:
    name: str
    required_by: str


def reviewer_requirements(reviewers: Sequence[str]) -> tuple[Requirement, ...]:
    """One (PEM, App-id) pair per declared FUNNEL reviewer; a hosted one contributes none."""
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
    """The full derived requirement set, one entry per (name, requiring entry).

    ``reviewers`` is the PROVISIONING scope; release-only consumers leave it empty.
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
        # Only the CERT PAIR is unconditional; notary is either-satisfies.
        if artifact.sign:
            reqs.extend(
                Requirement(
                    name=name,
                    required_by=f"sign-mac stage (artifact {artifact.name})",
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
    sets: AlternativeSet
    required_by: str


def alternative_requirements(
    artifacts: Sequence[Artifact],
) -> tuple[AlternativeRequirement, ...]:
    """One :data:`NOTARY_SECRETS` entry per signing artifact, declaration order."""
    return tuple(
        AlternativeRequirement(
            sets=NOTARY_SECRETS,
            required_by=f"sign-mac stage (artifact {artifact.name})",
        )
        for artifact in artifacts
        if artifact.sign
    )


def accepted_names(
    artifacts: Sequence[Artifact], *, reviewers: Sequence[str] = ()
) -> tuple[str, ...]:
    """Every ACCEPTED name — required plus every live alternative's; only the required
    ones are individually demanded.
    """
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
    """The alternative requirements no declared source completes."""
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
    """The requirements no ``[secrets]`` entry sources; :data:`EMPTY_VALID_SECRETS`
    names are never reported, so a passwordless repo provisions cleanly.
    """
    declared = {source.name for source in sources}
    return tuple(
        req
        for req in requirements(artifacts, reviewers=reviewers)
        if req.name not in declared and req.name not in EMPTY_VALID_SECRETS
    )


def orphans(
    artifacts: Sequence[Artifact],
    sources: Sequence[SecretSource],
    *,
    reviewers: Sequence[str] = (),
) -> tuple[str, ...]:
    """The declared ``[secrets]`` names NOTHING requires, in declaration order."""
    keep = set(accepted_names(artifacts, reviewers=reviewers)) | set(TOLERATED)
    return tuple(source.name for source in sources if source.name not in keep)


def secrets_block(artifacts: Sequence[Artifact]) -> str:
    """The cross-org caller's ``secrets:`` block, from the same derivation.

    Empty when nothing is required: a bare ``secrets:`` key parses as ``null`` and
    GitHub Actions rejects it. Lists the ACCEPTED names, and takes NO ``reviewers``
    — those credentials are provisioning, not the release contract.
    """
    names = accepted_names(artifacts)
    if not names:
        return ""
    lines = ["secrets:"]
    lines.extend(f"  {name}: ${{{{ secrets.{name} }}}}" for name in names)
    return "\n".join(lines)
