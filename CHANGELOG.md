<!-- generated - do not edit; fragments live in CHANGELOG/ (`shipit changelog render` regenerates this file) -->

# Changelog

## Unreleased

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
