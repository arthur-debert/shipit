"""Signing posture: the declared per-repo stance, checked against a repo's live Actions secret NAMES.

Names only — a value never enters this module. The canonical secret set is
:mod:`shipit.release.secretreq`'s; this module is where a fleet repo's
divergence from it becomes a finding.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from .secretreq import (
    APPLE_ID_NOTARY_SECRETS,
    ASC_NOTARY_SECRETS,
    SIGN_MAC_CERT_SECRETS,
)

POSTURE_SIGNED = "signed"
POSTURE_UNSIGNED = "unsigned"
POSTURE_NOT_APPLICABLE = "not-applicable"

#: ``signed`` = ships notarized macOS artifacts; ``unsigned`` = could sign and
#: deliberately does not; ``not-applicable`` = produces nothing signable.
POSTURES: tuple[str, ...] = (POSTURE_SIGNED, POSTURE_UNSIGNED, POSTURE_NOT_APPLICABLE)

#: The full set a ``signed`` repo carries: the cert pair plus the canonical trio.
CANONICAL_SIGNING_SECRETS: tuple[str, ...] = (
    *SIGN_MAC_CERT_SECRETS,
    *ASC_NOTARY_SECRETS,
)

#: Pre-homogenization name -> the canonical name that replaces it.
LEGACY_ALIASES: dict[str, str] = {
    "APPLE_CERTIFICATE_P12_BASE64": "APPLE_CERTIFICATE",
}

#: Signing-adjacent names no shipit stage reads — leftovers of retired workflows.
STRAY_SIGNING_SECRETS: tuple[str, ...] = (
    "APPLE_APP_SPECIFIC_PASSWORD",
    "APPLE_SIGNING_IDENTITY",
)

KIND_LEGACY = "legacy-name"
KIND_MISSING = "missing"
KIND_NONCANONICAL_NOTARY = "noncanonical-notary"
KIND_STRAY = "stray"
KIND_UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class Finding:
    """One repo's divergence from the fleet posture, anchored on the secret name that carries it."""

    kind: str
    secret: str
    detail: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "secret": self.secret, "detail": self.detail}


def validate_posture(value: str) -> str:
    """``value`` unchanged when it names a posture; raises ValueError otherwise."""
    if value not in POSTURES:
        raise ValueError(
            f"unknown signing posture {value!r}; one of: {', '.join(POSTURES)}"
        )
    return value


def signing_names(names: Collection[str]) -> tuple[str, ...]:
    """The signing-related subset of ``names``, sorted — every name any posture check cares about."""
    known = {
        *CANONICAL_SIGNING_SECRETS,
        *LEGACY_ALIASES,
        *APPLE_ID_NOTARY_SECRETS,
        *STRAY_SIGNING_SECRETS,
    }
    return tuple(sorted(name for name in names if name in known))


def _signed_findings(present: Collection[str]) -> list[Finding]:
    findings = [
        Finding(
            kind=KIND_LEGACY,
            secret=legacy,
            detail=f"pre-homogenization name — re-push the value as {canonical} and delete this one",
        )
        for legacy, canonical in LEGACY_ALIASES.items()
        if legacy in present
    ]
    superseded = {
        LEGACY_ALIASES[legacy] for legacy in LEGACY_ALIASES if legacy in present
    }
    findings.extend(
        Finding(
            kind=KIND_MISSING,
            secret=name,
            detail="required by the sign-mac stage; provision it under this canonical name",
        )
        for name in CANONICAL_SIGNING_SECRETS
        if name not in present and name not in superseded
    )
    findings.extend(
        Finding(
            kind=KIND_NONCANONICAL_NOTARY,
            secret=name,
            detail="the fleet notarizes with the ASC API-key trio only; retire the Apple-ID trio",
        )
        for name in APPLE_ID_NOTARY_SECRETS
        if name in present
    )
    findings.extend(
        Finding(
            kind=KIND_STRAY,
            secret=name,
            detail="no shipit stage reads this; delete it",
        )
        for name in STRAY_SIGNING_SECRETS
        if name in present
    )
    return findings


def conformance(posture: str, names: Sequence[str]) -> tuple[Finding, ...]:
    """Every way this repo's secret names diverge from ``posture``; empty means conforming."""
    validate_posture(posture)
    present = set(names)
    if posture == POSTURE_SIGNED:
        return tuple(_signed_findings(present))
    return tuple(
        Finding(
            kind=KIND_UNEXPECTED,
            secret=name,
            detail=f"declared {posture} — no signing secret should remain; delete it",
        )
        for name in signing_names(present)
    )
