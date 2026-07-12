"""release — the Release pipeline's cores and registries (TOL02).

A **Release** carries ONE repo-level version whose authority is the git tag;
manifests are projections of the tag decision (ADR-0041). This package holds
the pipeline's pure cores and closed registries, stage by stage as the TOL02
work streams land:

- :mod:`.version` — the version resolver (WS01): supplied ``<semver>`` or bump
  word → the resolved version, prerelease flag, and resume verdict. Pure.
- :mod:`.bump` — the per-toolchain bump-adapter registry and the
  artifact-declared bundle-config hook (WS01): how the tag decision projects
  into manifests. Command literals + pure text rewrites; no I/O.
- :mod:`.preflight` — the release-side planner (WS02): (artifact map,
  resolved version, event) → the machine-readable plan (OS×arch matrix, live
  stages, post-RC-guard endpoints, required secrets) the composed workflow
  consumes as job outputs. Pure.
- :mod:`.secretreq` — secrets derivation (WS02): registry entries declare
  the secret NAMES they require; traversing the repo's declarations yields
  the one required set gh-setup syncs, preflight validates, and the
  cross-org caller's ``secrets:`` block lists. Pure.
- :mod:`.bundle` — the closed bundle-composition registry (WS03): how a
  declared artifact composes build outputs into its distributable — unsigned
  for the reopen→reseal legs (archive, deb, wheel, mac-app), already-signed
  for electron (which self-signs inside its bundler, WS14). Command literals
  + compose functions, effectful only through the injected exec seam.
- :mod:`.integrity` — the assert-bundle pure core (WS03, workflows.lex
  §3.2): the expected-main-binary fallback chain and the bundle-tree check
  behind ``shipit release assert-bundle``.
- :mod:`.sign` — the consumer-agnostic mac signer unit (WS04, workflows.lex
  §3.1): reopen the unsigned ``.app``/``.dmg`` bundle, codesign inner-first
  with the ``.app`` last, reseal via ``hdiutil``, notarize + staple. Pure
  argument assembly + credential resolution; effectful through the injected
  exec seam AND caller-scoped filesystem I/O under the scratch dir (decoding
  credential material, staging the ``.dmg``); hard-fails on missing secrets
  (no warn-and-skip).
- :mod:`.publish` — the closed endpoint-adapter registry (WS05): how a
  declared Distribution endpoint (gh-release, crates, pypi, npm, brew)
  ships the staged Artifacts — plus the stage's pure cores: the scar-#3
  refusal gate, the central ``-release-rc`` guard, and the
  release-before-derived ordering plan.
- :mod:`.brew` — the brew formula render core (WS05): the shared formula
  template, the PascalCase class derivation, and the crate-metadata pull.
  Pure text; the effectful tap push is the brew adapter in :mod:`.publish`.
- :mod:`.provisioning` — the missing-tool → reconcile-remedy translation
  (#801): one map from a pixi-managed tool's argv head to the install
  reconcile that provisions it, shared by the prepare and publish verbs so a
  tool absent on the runner fails LOUDLY naming the fix, never as a raw 127.

The effectful shells live in :mod:`shipit.verbs` (``shipit release prepare`` /
``preflight`` / ``bundle`` / ``assert-bundle`` / ``sign`` / ``publish`` are
:mod:`shipit.verbs.release`), executing through the one Exec seam (ADR-0028)
and the git/gh adapters.
"""

from __future__ import annotations


class ReleaseError(RuntimeError):
    """A release-stage domain refusal — exit 1 via the shared CLI error shell
    (:mod:`shipit.verbs._errors`), one ``error: …`` line, never a traceback.

    Raised for runtime refusals of the release stages: a no-op bump (the
    manifests already carry the target version but its tag does not exist —
    re-running against a different release), a manifest a bump adapter cannot
    rewrite, a prepare invoked outside a git checkout or on a detached HEAD,
    a bundle composition over missing build outputs (no built binary, no
    ``.deb``/wheel/sdist produced, no coupled ``.app``/``.dmg`` pair or
    reseal payload), an assert-bundle whose expected name cannot be
    resolved (an unknown or unnamed artifact), the signer's refusals
    (missing signing/notary secrets — named, never skipped —, a tree with
    zero or multiple ``.app``/``.dmg``, no codesigning identity in the
    imported cert, a rejected or unconfirmed notarization), and the publish
    stage's refusals (the scar-#3 gate over the upstream stage results, a
    missing endpoint token, a failed external publish that is not the
    already-published resume case, a formula without its crate metadata).
    USAGE errors (a malformed version argument) are NOT this class — they
    die at the click boundary as exit 2 (ADR-0030).
    """
