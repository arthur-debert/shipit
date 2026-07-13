<!-- generated - do not edit; fragments live in CHANGELOG/ (`shipit changelog render` regenerates this file) -->

# Changelog

## Unreleased

## 1.1.0 - 2026-07-13

- lanes: declared-secrets seam — a per-lane `secrets` allowlist routes one
  scoped token into a wf-checks lane, gated routing-only in the block so a
  private-source test surface can move onto a managed lane (#778)
- install: self-cert now gates shipped skill content against the delivered
  markdownlint config, so the managed set can't ship content that reds a
  consumer's lint gate (#777)
- install --pr: flow-robustness — restore the caller's branch, pre-clean stale
  lefthook `.old` hook backups before activation, and a transactional
  fail-closed that rolls back a half-applied write on self-cert failure (#777)
- wf-checks: document lane self-provisioning as the sanctioned rule for
  submodule- and system-dep-dependent suites (provision in the lane's own
  `run` task, not via a block knob) (#759)
- managed-content: qualify the adoption.md pointer and align the spec
  placeholder surfaced by consumer reviewers (#781)
- release: electron bundle composition + dmg/AppImage integrity tiers
  (TOL02-WS14, #790)
- release: tauri-cli bundle composition — darwin .app/.dmg + linux
  .AppImage/.deb (TOL02-WS15, #827)
- release: vscode-marketplace + open-vsx endpoints + per-target .vsix
  (TOL02-WS13, #789)
- release: tree-sitter composition + notify-downstreams cascade
  (TOL02-WS16, #792)
- release: wasm/npm build composition (wasm-pack → npm tarball)
  (TOL02-WS12, #788)
- release: wasm-pack mirrors the tarball's platform_independent guard (#828)
- sign: electron per-code-role JIT entitlements + top-level .app hardening
  (#829, #830)
- sign: validate reseal payload link targets in the mac-app leg (#812)
- review: `shipit pr review validate` + REVIEW_SCHEMA self-check (#826)
- RPE01: Role Profiles and Work Environments epic (#825)

## 1.0.0 - 2026-07-12

- First release of shipit as its own published artifact. The tag is the
  payload: consumers ride the `@v1` workflow refs (ADR-0010) and the git pin
  (ADR-0033); `advance-major` takes the floating `v1` branch over from this
  release on, retiring the manual branch-advance workaround.
- release: make the deb composition CI-viable — cargo-deb self-provisions
  through the managed pixi surface, the native triple-dir contract, and a deb
  tier in assert-bundle (#785)
- release: archive-leg mac codesign + notarize — raw darwin CLI binaries ride
  the same sign stage as mac-app bundles (TOL02-WS08, #800)
- release: per-stage dispatch — the wf-* stage blocks are self-sufficient
  standalone (plan facts re-derived at the tag when omitted), and the
  routing-only `stage` choice caller is the blessed consumer dispatch surface
  (TOL02-WS09, ADR-0054, #804)
- release: declare shipit's own release surface — the no-build `gh-release`
  artifact (the tag is the payload) plus the blessed stage-choice dispatch
  caller `shipit-release.yml`, cutting shipit through its own pipeline (#774)
- release: close the release-tool provisioning holes — rust (cargo-edit,
  cargo-deb) and twine ride the shipit-managed pixi blocks, uv joins the
  managed surface, a provisioning inventory + drift guard pins the set, and
  an unprovisioned tool fails loudly naming the install reconcile instead of
  installing at run time (TOL02-WS17, #797, #799, #803)
