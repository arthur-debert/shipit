# TOL01 — shipit CI tools: verbs, workflow blocks, fleet-verified

Status: planned. This PRD absorbs the build half of ADP02 (#422); ADP02 narrows
to pure adoption. Decisions of record: ADR-0039 (tools as verbs), ADR-0040
(workflow blocks, invariants in blocks), ADR-0041 (tag-authoritative version,
supplied not computed), on top of ADR-0007/0008/0009/0010.

## Problem Statement

Consumer CI still calls the legacy `arthur-debert/release` reusable workflows —
N per-stack pipelines (rust-ci, tauri-app, go-cli, …) each re-deriving
checkout → provision → run → collect, with producing logic trapped in YAML and
~39 repo-internal scripts. Nothing of it runs on a laptop, so every CI change is
a push-to-find-out loop, and every behavior lives twice (locally one way, in CI
another). shipit has specced the replacement but built only `shipit lint`:
there is no `shipit test`, `build`, `e2e`, `changelog`, or release pipeline, no
reusable workflows published from shipit, and no way to validate a workflow
before pushing it. Until those building blocks exist — and are proven on every
fleet repo — CI adoption (ADP02) cannot start, and any attempt would degenerate
into per-repo back-and-forth with shipit for adjustments.

## Solution

Ship the complete set of CI building blocks as **tools** — uniform shipit verbs
that walk the path→toolchain map and dispatch each **leg** to its producing
command — plus the thin reusable **workflow blocks** that route them in CI, and
verify every tool locally on every portfolio repo before ADP02 re-points a
single workflow.

One implementation everywhere: the same `shipit test` runs on a laptop, in a
lefthook hook, and inside a CI job; pixi tasks are thin one-line callers; YAML
carries routing only. The release pipeline decomposes into independently
invocable stages (`preflight → prepare → bundle → assert-bundle → sign →
publish`, with `build` and `changelog` as top-level tools), published both as
stage-level reusable workflows and as one composed chain, with the three scar
invariants enforced inside the blocks they protect. The legacy per-stack zoo
dissolves into declarations: a repo is a path→toolchain map plus an artifact
map (bundle, endpoints, signing, e2e harness); registries (toolchains, endpoint
adapters, secret requirements) carry all per-stack knowledge.

TOL01 is done when every tool is green locally on every portfolio repo it
applies to, and one rc has traversed the full pipeline on lex.

## User Stories

1. As a maintainer, I want `shipit test` to run every test leg of my repo with one command, so that a multi-toolchain repo needs no hand-chained scripts.
2. As a maintainer, I want `pixi run test` to be a thin caller of `shipit test`, so that laptop, hook, and CI run the identical implementation.
3. As a maintainer, I want registry defaults per toolchain (rust → cargo-nextest, go → go test, python → pytest), so that a standard repo declares nothing to get working tools.
4. As a maintainer, I want a per-path override for any tool's producing command, so that a nonstandard repo opts out per leg without forking the tool.
5. As a maintainer, I want passthrough args forwarded verbatim to the underlying command (`shipit test rust -- --no-capture`), so that uniformity never walls off the stack's own surface.
6. As a maintainer, I want passthrough on a multi-leg repo without a selector to hard-error listing the legs, so that flags are never broadcast to a leg they'd break.
7. As a maintainer of a single-leg repo, I want to omit the leg selector, so that the common case stays as short as today.
8. As an implementer agent, I want tool exit codes and reporting to be uniform across tools, so that I can script against them without per-stack knowledge.
9. As a shipit developer, I want `shipit build` to invoke the real builder (cargo, tauri, electron-builder) with pixi only provisioning, so that pixi is never the build backend.
10. As a maintainer, I want `shipit e2e` to build-or-reuse the artifact and inject it as `<NAME>_BIN` into my declared harness, so that e2e is one command locally with no manual artifact plumbing.
11. As a maintainer of a repo without e2e, I want no e2e declaration to mean no e2e lane, so that opting out is the absence of config, not a flag.
12. As a shipit developer, I want the artifact source behind e2e to be a seam (local build, CI artifact download, later a content-key hit), so that WF02 slots in without touching the tool's interface.

13. As a maintainer, I want the commit/push checks to be exactly the required∩local lanes (`lint` + fast `test`), so that lefthook and CI enforce one definition.
14. As a maintainer, I want lanes declared as `{run, required, local, trigger, runner, scope}` with `run` able to name a leg, so that per-toolchain CI jobs need no extra concept.
15. As a shipit developer, I want a pure lane planner mapping (lanes, event, path-diff) → job matrix, so that CI job emission is unit-testable from fixtures.
16. As a maintainer, I want an expensive lane to run thin on unrelated PRs and full on nightly/dispatch, so that coverage survives without taxing every PR.
17. As a maintainer, I want `actionlint` as a lint Lang, so that workflow YAML is gated by the same `shipit lint` as everything else.
18. As a maintainer, I want a fragment-sync check lane, so that a PR that edits the changelog without a fragment (or vice versa) fails before merge.

19. As a release engineer, I want each release stage independently invocable (`shipit release prepare`, `… bundle`, `… sign`, `… publish`), so that CI jobs, re-runs, and local debugging address one stage at a time.
20. As a release engineer, I want bare `shipit release` to chain the stages locally with the same skip/opt-in logic, so that a laptop rc-cut exercises the real pipeline.
21. As a release engineer, I want to pass either a version or a bump word (`shipit release 3.0.2` / `shipit release minor`), so that routine bumps don't require me to look up the last tag.
22. As a release engineer, I want the tag to be the version authority with manifests as projections, so that go's no-manifest model and rust's workspace bump are the same design, not an exception.
23. As a release engineer, I want prepare to be resumable (tag exists → skip bump, re-emit SHA), so that a mid-pipeline failure never wedges a release.
24. As a maintainer, I want the prepare bump commit to pass the same commit/push checks as any commit, so that the bot has no second path around policy.
25. As a maintainer of a Tauri app, I want per-leg bump adapters plus an artifact-declared bundle-config hook to cover the three-file lockstep, so that "tauri" never becomes a dispatch label.
26. As a maintainer, I want `shipit changelog` to refuse an empty release, coalesce fragments into the version section, and feed the same text to the tag annotation and the GH release, so that release notes exist exactly once.
27. As a release engineer, I want `preflight` to emit a machine-readable release plan (matrix, live stages, endpoints, required secrets), so that routing consumes a plan instead of re-deriving decisions in YAML.
28. As a release engineer, I want declared-signing-with-missing-secrets to hard-fail at preflight, so that a signing repo can never silently ship unsigned.
29. As a release engineer, I want an explicit `--unsigned` break-glass for local rc-cuts without certs, so that the escape is visible and logged, never ambient.
30. As a release engineer, I want `assert-bundle` to verify the bundle's main binary is the expected app before signing or publishing, so that a signed-and-notarized wrong binary can never ship again.
31. As a maintainer of a mac app, I want the signer to be a consumer-agnostic unit over a .app/.dmg pair (reopen → resign inner-first → reseal → notarize → staple), so that electron and tauri repos share one signing implementation.
32. As a release engineer, I want publish to refuse unless build+bundle succeeded and sign succeeded-or-was-skipped via explicit result inputs, so that a partial release is structurally impossible regardless of who composed the workflow.
33. As a release engineer, I want an `-release-rc` version to publish only to the GH release (as prerelease) and skip every external endpoint, enforced in the verb, so that live-fire pipeline verification is safe from a laptop too.
34. As a maintainer, I want endpoints declared per artifact with one adapter per endpoint (gh-release, crates, pypi, npm, brew), so that adding a distribution target is a declaration plus at most a registry entry.
35. As a release engineer, I want publish ordered (release endpoints before derived ones like brew) and idempotent-resumable, so that re-running after a partial publish converges instead of erroring.
36. As a maintainer of a multi-crate workspace, I want crates published in dependency order with already-published treated as success, so that resumption mid-workspace works.

37. As a maintainer, I want my repo's CI to be a thin caller of shipit-published reusable workflows, so that upgrading CI is bumping one version.
38. As a maintainer with a standard shape, I want one composed `wf-release` caller line; as one with an odd shape, I want to compose stage workflows directly — both sanctioned, so that composition is a spectrum, not a fork.
39. As a shipit developer, I want the scar invariants inside the blocks they protect, so that no consumer wiring mistake can bypass them.
40. As a maintainer, I want `shipit wf test` to run one workflow/job under act in a container with a crafted event payload, so that workflow changes are validated before any push.
41. As a maintainer, I want the act harness to print exactly what act cannot verify (macOS/Windows runners, GPU, cross-workflow cascade, partial workflow_call, dispatch UX), so that local green is trusted only where valid.
42. As a maintainer, I want required-check names held stable across cutover, so that branch protection never breaks mid-migration (consumed by ADP02).

43. As a maintainer, I want each registry entry (endpoint adapter, sign stage, prepare push) to declare the secret names it requires, so that the needed set is derived from what the repo actually ships.
44. As a maintainer, I want `gh-setup` to traverse my declarations, resolve each required secret from its `[secrets]` source (doppler unchanged), and sync to Actions secrets, so that a repo can never under- or over-provision secrets.
45. As a maintainer, I want a requirement with no declared source to fail at `gh-setup` time and orphaned pushed secrets to be flagged, so that drift is caught at sync, not at release.
46. As a cross-org consumer, I want the caller's `secrets:` block generated from the same derivation, so that the three consumers of the secret map cannot drift.

47. As the coordinator, I want every tool verified locally on every portfolio repo it applies to, with a per-tool × per-repo report, so that ADP02 re-points CI at known-good tools.
48. As the coordinator, I want TOL01's exit to include one rc traversing preflight → publish on lex, so that the pipeline is proven end-to-end on a real multi-artifact consumer before adoption starts.
49. As an implementer agent on a consumer repo, I want tool failures during verification fixed in shipit (registry/adapter), not patched per repo, so that fixes accrue to the fleet.

## Implementation Decisions

- **Tools are verbs (ADR-0039).** Each tool walks the path→toolchain map and
  dispatches per leg: registry default per toolchain, per-path override in
  `.shipit.toml`. Pixi tasks are thin one-line callers. Passthrough args
  forward verbatim; multi-leg passthrough requires a leg selector, hard error
  otherwise. Supersedes WF01's "consumer supplies `test`" line.
- **Command surface.** Top level: `lint` (existing, + actionlint Lang), `test`,
  `build`, `e2e`, `changelog`, `wf test`, and the `release` group with stages
  `preflight | prepare | bundle | assert-bundle | sign | publish`. `build` and
  `changelog` stay top-level because both have PR-time faces. The terminal
  stage is `publish` (the glossary's Release names the whole event); "package"
  is retired as a word — the stage is `bundle`.
- **Pure cores, effectful shells.** Deep modules with fixture-testable cores:
  toolchain registry, leg planner, release planner (preflight), version
  resolver, secrets derivation, lane planner, artifact-map parsing (typed
  values per ADR-0030). Shells execute through the one-exec seam (ADR-0028).
- **e2e is the artifact-consuming tool.** Consumer declares the harness
  (registry default: bats-run check-e2e); shipit resolves the binary via a
  three-source seam (local build / CI artifact / future content-key store) and
  injects `<NAME>_BIN` (contract kept from the legacy fleet deliberately).
  Environment-heavy test jobs (supage's emulator) are bespoke lanes, not e2e.
- **Versioning (ADR-0041).** One repo-level version; tag-authoritative;
  supplied as `<semver>` or bump word resolved against the latest tag; never
  inferred from fragments. Per-toolchain bump adapters project the tag decision
  into manifests; go's no-bump is a first-class adapter; bundle-level version
  files are bumped by an artifact-declared bundle-config hook. Prepare is
  idempotent-resumable per ADR-0009.
- **Preflight is the release-side planner.** Pure function from (artifact map,
  version, event) to a machine-readable plan: artifacts, OS×arch matrix, live
  stages, post-RC-guard endpoints, required secrets. The composed workflow
  consumes the plan as job outputs — the lane planner's release twin.
- **Publish walks the artifact map.** One endpoint adapter per distribution
  endpoint (closed registry: gh-release, crates, pypi + testpypi flag, npm,
  brew incl. private-repo strategy). Two-stage ordering: `release` endpoints
  before `derived` endpoints (brew needs final asset SHAs). Every adapter
  treats already-published as success. The RC guard lives centrally in the
  verb. Legacy stacks dissolve into declarations: a VS Code extension is an
  npm path + a vsix bundle + two endpoint adapters — added as registry entries
  when their repo migrates, not in TOL01.
- **Workflow blocks (ADR-0040).** Stage-level reusable workflows plus one
  composed chain, published from shipit at `@vN` (ADR-0010). Scar invariants
  live inside the blocks: publish takes upstream stage results as explicit
  inputs (ADR-0009 check); assert-bundle runs at the signer's entry and on the
  unsigned publish path. The composed workflow carries zero logic. Artifact
  hand-off between jobs (upload/download, keychain import, secret injection)
  is routing and stays YAML; `stage-assets` and `build-frontend` are not tools
  (the latter is just `build npm` — a leg).
- **Secrets: requirements + sources.** Registry entries declare required
  secret names; `[secrets]` keeps per-repo sources (doppler pipeline
  unchanged). The required set is derived by traversing the repo's
  declarations; consumed identically by gh-setup sync (with orphan flagging),
  preflight presence validation, and cross-org caller generation. Declared
  signing with missing secrets is a preflight hard fail; `--unsigned` is
  explicit break-glass.
- **Fleet verification is TOL01's exit.** A per-tool × per-repo local sweep
  across the portfolio (riding Tree/spawn machinery), evidence-verified like
  adoption, plus one rc cut through the full pipeline on lex. Failures are
  fixed in shipit's registries/adapters, never patched per repo.
- **Paper trail.** ADP02 (#422) build half is superseded by pointer to this
  PRD; WF01, pixi-test-build-release, and workflows-cutover carry
  reconciliation banners.

## Testing Decisions

Good tests assert external behavior at a module's interface — recorded inputs
to asserted outputs — never internal call shapes.

- **Pure cores — full unit coverage, fixture-driven:** toolchain registry
  dispatch, leg planner (incl. selector errors), release planner, version
  resolver (bump words, prerelease suffixes, resume detection), secrets
  derivation (sync set, validation set, orphans, missing-source errors), lane
  planner, artifact-map parsing. Prior art: the existing config and lint
  registry tests.
- **Tool verbs and adapters — through the exec seam:** recorded invocations
  asserted against expected command lines and env (e.g. `<NAME>_BIN`
  injection, passthrough placement, RC-guard endpoint skipping). Prior art:
  the lint tool-runner tests over the one-exec seam.
- **Workflows — actionlint + act smoke:** every published block passes the
  actionlint Lang; one `shipit wf test` smoke per block with a crafted event.
  The act-untestable surface (mac sign/notarize, dispatch UX, cascade) is
  printed, documented, and covered only by remote verification.
- **Signer and fleet sweep — evidence-verified:** the mac signer is proven by
  the lex rc cut (artifact inspected: right binary, signed, notarized); the
  fleet sweep's per-tool × per-repo report is the verification artifact, per
  the adoption doctrine of evidence over unit tests.

## Out of Scope

- **Adoption itself (ADP02):** re-pointing consumer thin callers, per-repo
  remote verification, legacy tooling removal, memory combing. The standing
  gate — release-core retires only after shipit cuts one real release of one
  real consumer — stays on ADP02.
- **WF02:** the content-key store and build-once reuse; TOL01 keeps only the
  artifact-source seam boundary.
- **WF03:** deep test/quality lanes (GPU, native WebDriver e2e).
- **WF06:** rebuilding the cascade; the existing cascade-handler keeps working
  against the dispatch contract.
- **Marketplace-class endpoint adapters** (VS Marketplace, Open VSX, Zed,
  tree-sitter bundles): registry entries added when their repo migrates.
- **Deploys that are not Releases:** supage's Cloud Run deploy, falala's APK,
  OIDC npm publishes outside the fleet contract.
- **Version inference** from fragments or commit messages.

## Further Notes

Legacy → TOL01 mapping (reference when porting; legacy units are forked by
copy per ADR-0001/0010, never depended on): rust-ci/go-ci check jobs →
`test`/`build` legs + lanes; bats-e2e → `e2e`; prepare-release{,-go,-npm,
-python,-tauri} → prepare bump adapters; changelog-check + roll → `changelog`;
setup-matrix + validate-apple-secrets + python-pkg preflight → `preflight`;
per-OS build matrices → `build` + plan matrix; packaging/tarball/deb/wheel →
`bundle`; sign-notarize-mac + unpack/enumerate/reseal helpers → `sign`;
create-release + `-release-rc` guard → `publish`; publish-crate / pypi / npm /
brew render+push → endpoint adapters; arm-gate → already subsumed by
`shipit lint`; nvim "tag is the release" → an artifact with zero build/bundle
and one endpoint.

The verification matrix doubles as ADP02's starting state: a repo whose tools
are all green locally is adoption-ready, and the sweep report is the checklist
seed.
