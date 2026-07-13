shipit CI Workflows

The composable CI design, in its final state. The worked example is the
tauri/phos-app stack — the most complex Kind in the portfolio — because if the
shape holds there it holds for the simpler Kinds (rust-cli, python-pkg,
vscode-ext). The architectural boundary this design rests on (producing logic
is locally runnable — a shipit verb behind a thin pixi task caller; routing
logic is thin YAML) is stated in [./architecture.lex#3].

:: note ::
    Reconciliation (TOL01, 2026-07). The PRD `docs/legacy-prd/tol01-ci-tools.md` and
    ADR-0039/0040/0041 supersede parts of this doc's vocabulary: producing
    steps are shipit verbs now (pixi tasks are thin callers); the "package"
    stage is renamed `bundle` and the terminal stage `publish` ("release"
    names the whole repo-level event); `stage-assets` is routing, not a
    producing task; `build-frontend` is the `build` verb's npm leg. The
    pipeline shape, the three scars ([#3]), and the changelog model ([#4])
    stand unchanged.
::

1. The decomposed pipeline

    A release is a chain of stages, each a separate job, passing artifacts
    forward:

    Pipeline:
        preflight -> prepare -> build(xOS) -> bundle(xOS) ->
        assert-bundle(xOS) -> sign(mac) -> publish

    - preflight: resolve whether signing is requested (one switch, referenced
      everywhere downstream), validate that the changelog has content, and
      validate Apple secrets only when signing is requested. Cheapest checks
      first, before any toolchain is installed.
    - prepare: bump the version, coalesce the changelog (see [#4]), commit, tag,
      push, and emit the release-notes artifact. For tauri this bumps THREE
      version files in lockstep (package.json + src-tauri/Cargo.toml +
      tauri.conf.json); they must stay in sync or the build breaks.
    - build (per OS): build the frontend, then COMPILE ONLY (no bundle). The
      cross-job artifact is the ~tens-of-MB compiled binary + the built
      frontendDist — NOT the multi-GB target/ tree. This is the expensive,
      cache-warmed half (rust-cache + sccache).
    - bundle (per OS): download the compile artifact and bundle it UNSIGNED.
      Needs no rust toolchain.
    - assert-bundle (per OS): assert the bundle's integrity before upload.
    - sign (mac, optional): only when signing is requested. See [#3.1].
    - publish: publish the GitHub release from the notes + tag + bundles, with
      partial-release prevention (see [#3.3]).

    Per-platform fan-out is a matrix; opting out of a platform drops its matrix
    entry rather than leaving a dead job.

2. The producing / routing boundary, applied

    Each producing step is a shipit verb behind a thin pixi task caller,
    runnable on a laptop or in local Docker exactly as in CI:

    Producing (shipit verbs; pixi tasks are thin callers):
        - build — frontend/WASM build plus compile
        - release bundle — bundle the compiled binary (unsigned)
        - release assert-bundle — the integrity guard ([#3.2])
        - release sign — reopen, sign, and reseal macOS bundles
        - changelog — coalesce fragments ([#4])
        - release publish — publish to the configured endpoints

    Routing (thin workflow YAML):
        - the per-OS matrix
        - cross-job artifact upload / download
        - stage-assets collection
        - secret injection and presence checks
        - the macOS keychain import for signing

    These building blocks already exist in arthur-debert/release as
    bin-internal/*.sh scripts called by thin workflow steps, so the migration
    is mechanical: each script becomes a shipit verb behind a thin pixi task;
    the routing stays in YAML.

    CI Lane Work Env:
        A CI Lane job is a Main-checkout Work Env, not a Tree. GitHub Actions
        provides the fresh checkout and runner; the Lane planner supplies the
        lane name, event, runner, required/advisory bit, and pixi environment
        name. Execution still happens through the workflow's existing
        `pixi run --locked` caller and shipit verb; Work Env only records the
        resolved `direct-checkout` / `pixi-run` evidence. Do not instantiate
        local Tree objects in CI just to share terminology, and do not invent a
        pixi Run id — pixi exposes environment metadata, not invocation
        identity.

    Lane self-provisioning — the checkout carries no provisioning inputs:
        A lane whose suite needs content the plain checkout does not carry —
        git submodules being the classic case (#759) — provisions it INLINE in
        the lane's own `run` task, never through a checkout knob on the block.
        The `wf-checks` checkout is `actions/checkout@v6` with NO `submodules:`
        input, and it stays that way: a `submodules:` (or any provisioning)
        input would push a producing decision into the routing-only block —
        the exact ADR-0040
        [../adr/0040-workflow-blocks-invariants-in-blocks.md] line this wf-*
        family holds — so the submodule-init step lives where the
        compile and the test do, behind `pixi run <lane.run>`, the shipit verb
        that a laptop run, a lefthook hook, and the CI job all land in
        (ADR-0039). That is what keeps laptop and CI identical: the
        provisioning travels with the task, not with one YAML's checkout step.
        The precedent is lex's
        `test-full` lane, which inits its `comms` submodule before
        `cargo nextest`; the legacy `release/rust-ci.yml` `submodules:` input
        (release#134) is the anti-pattern this replaces. The rule covers any
        suite-local provisioning a plain checkout omits, not submodules alone,
        and is the sanctioned pattern for the WS03–WS05 fleet rollout — a
        stated rule, not folklore. The local Tree-provisioning twin of this
        gap is tracked at #485.

3. Design invariants the pipeline learned the hard way

    These three are not style preferences — each is a scar. Treat them as
    invariants any reimplementation must preserve.

    3.1. Sign and bundle are interleaved, not sequential

        The intuitive order — "sign the binary, then bundle it" — is wrong on
        macOS and the pipeline must not be built around it. tauri bundles the
        .app and .dmg together (coupled — they are not cleanly separable), and
        the bundle stage produces them UNSIGNED. The signer then REOPENS the
        bundle:

        - codesign the .app, nested Mach-O inner-first and the .app LAST (a flat
          sign leaves nested executables unhardened and notarization rejects it);
        - reseal the .dmg from the SIGNED .app via hdiutil — re-bundling would
          strip the signature;
        - codesign the resealed .dmg;
        - notarize + staple.

        So the model is *bundle(unsigned) -> sign-reopens-and-reseals*, never
        *sign -> bundle*. The bundle stage must also emit the inner .app as a
        reseal payload, because artifact upload does not preserve a .app's
        symlinks and exec bits.

        The mac signer is a CONSUMER-AGNOSTIC reusable unit: it operates on a
        .app/.dmg pair and makes no tauri (or electron) assumptions. Keep it
        that way — the only tauri-specific part is the bundler that produced the
        unsigned input, which lives in the caller.

        The same reopen model covers the ARCHIVE composition's raw darwin CLI
        binaries (the legacy rust-cli sign steps' shape): the tarball is
        bundled unsigned, and the signer reopens it — codesign each Mach-O
        inside, notarize each as a zip (a bare binary has no staple target),
        re-emit the tarball. The tarball, not the loose staging binary, is
        what the signer reopens and what ships: artifact transport strips
        loose exec bits, the tar's own headers preserve them.

    3.2. The integrity guard — signing is not integrity

        Before upload, assert that the bundle's MAIN binary is the expected app
        (mainBinaryName -> productName -> package name). This exists because a
        src-tauri crate with multiple binaries and no declared main once made
        the bundler pick the alphabetically-first one: a dev tool
        (gen_fixtures) shipped as the app's main executable, and it signed and
        notarized cleanly. Signature checks verify the signature, not that the
        artifact is the right binary. This verify stage is non-negotiable.

    3.3. Partial-release prevention

        The publish job publishes ONLY when every LIVE upstream stage
        succeeded and sign either succeeded (signed path) or was skipped
        (unsigned path). A FAILED (or cancelled) stage blocks publish,
        always. This block cannot be a plain dependency — the default "a
        skipped dependency skips the dependent" would wrongly skip publish on
        the unsigned path — so it is an explicit result check in the publish
        verb, fed the stage results verbatim. Never ship a half-built set.

        Liveness is a PLAN fact, never read off the result strings (issue
        #745): a no-build plan (empty matrix — "the tag is the release") skips
        the build job, and a reusable-workflow caller whose only inner job was
        if-skipped concludes SKIPPED (canary-confirmed), so the gate accepts
        skipped for build/bundle exactly when the plan proves the stage
        non-live (empty matrix for build, no bundle stage in stages for
        bundle). A LIVE build/bundle still requires success, and the chain
        still carries zero logic: the plan's matrix and stages ride to the
        verb verbatim, and the verb derives the verdict.

4. The changelog model (generalizable, language-agnostic)

    Releases accumulate unreleased fragments under CHANGELOG/unreleased-*.md,
    one per feature/fix PR. At cut time the `shipit changelog` task:

    - refuses to release when no fragments exist (an empty release is almost
      always a mistake);
    - coalesces the fragments into the new version's CHANGELOG section;
    - feeds that same coalesced text to BOTH the git tag annotation and the
      GitHub release notes.

    This is one pixi task with zero per-language logic — fragments are plain
    markdown regardless of Kind. It runs in the prepare stage and emits the
    release-notes artifact the publish stage consumes.

5. Frontend / WASM note

    The frontend builds per-platform: tauri wants frontendDist co-located with
    the compile, so it is built inside each matrix leg rather than once and
    fetched. What is shared is the EXPENSIVE part — the WASM bundle — cached
    per-OS keyed on Cargo.lock, so a cache hit lets the frontend build
    short-circuit. "Build the frontend once and share it across legs" is an
    optimization tauri resists, and the payoff is small because the cheap part
    (TS -> JS) is not the cost; the WASM is, and it is already deduplicated.

6. Composability and scope

    The easy 80% generalizes by parameter: a generic workflow that runs a
    provided command (`pixi run build`, `pixi run test`) over a matrix. The
    hard 20% — signing, notarization, OS packaging, store distribution — does
    NOT generalize by parameter and must not be forced into a YAML config DSL.
    It is expressed as composable, opt-in jobs (build -> bundle -> sign ->
    publish), each consuming the previous stage's artifacts. A consumer that
    needs signing wires in the sign job; one that does not, omits it and ships
    the unsigned bundles end-to-end with zero signing secrets.

7. Publishing reusable workflows — the publisher-side access surface

    Publishing reusable workflow blocks portfolio-wide (cross-owner `uses:`
    refs) requires a PUBLIC publisher repo — a private repo's workflows are
    shareable within its owner namespace ONLY, at any access level; the
    decision and its evidence live in ADR-0053
    [../adr/0053-shipit-is-public-vn-distribution-needs-a-public-publisher.md].
    Same-owner-only publishing from a private repo additionally needs the
    repo's Actions access level
    (`repos/{owner}/{repo}/actions/permissions/access`) at `user` (user-owned)
    or `organization` (org-owned) — it ships
    as `none`, which blocks even same-owner callers (TOL02-WS07 finding 5).
    gh-setup VERIFIES this and warns when a private `workflow_call` publisher
    sits at `none`, naming the fix; it never sets the level (#739).

8. Per-stage dispatch (TOL02-WS09, ADR-0054)

    Owner requirement: a chained release must let an operator re-run exactly
    stage N fresh, by API, without re-running everything. GitHub's native
    re-run-failed-jobs only replays an existing run, and a full re-dispatch
    (which converges, ADR-0009) re-walks every stage. The decision is
    ADR-0054 [../adr/0054-per-stage-dispatch-self-sufficient-blocks.md];
    this section is the working contract.

    The stage blocks are self-sufficient standalone (shipit-side,
    @v1-inheritable):

    - Every block's plan facts are OPTIONAL. Omitted, an internal `plan` job
      re-derives them at the tag via `shipit release preflight --plan-only`
      (skips ONLY the secret-presence hard-fail: the plan job runs
      secret-free, presence was the source run's preflight's job, and each
      stage's verb still validates its own names). Same planner, run in the
      block — the ADR-0040 line holds: derivation lives shipit-side, never
      consumer YAML. The composed chain passes every fact explicitly, so its
      plan jobs are skipped no-ops.
    - The aligned stage-input contract: `prepare` dispatches on `version`
      (it CREATES the tag); `build`, `sign`, `publish` dispatch on `tag`
      alone (ADR-0041 — the version is read off `v<version>`), plus `run-id`
      on the artifact-consuming stages (`sign`, `publish`) naming the SOURCE
      run whose artifacts feed them. `checks` needs nothing: wf-checks takes
      no inputs and plans its own lanes.
    - Standalone `wf-publish` derives stage-result CLAIMS from plan liveness
      (live -> success, plan-proven non-live -> skipped): the honest
      statement of a re-dispatch — the operator asserts the source run
      completed its live stages — enforced by the source-run downloads
      failing loudly on any missing artifact, and a signed CLAIM checked PER
      sign-projection entry (a wildcard signed-* download passes on any
      match, so `signed-<artifact>-<platform>` is enumerated from the plan
      and a partially-signed source run is refused, never published mixed),
      then by the verb's scar-#3 gate, unchanged ([#3.3]).

    The blessed consumer surface is the routing-only `stage` choice caller
    (the ADP02-WS06 interim shape, now fully wireable): one workflow_dispatch
    caller with a `stage` choice input (`full | prepare | build | sign |
    publish`), one job per stage, each a single `uses:` line against the
    matching block gated `if: inputs.stage == '...'`, forwarding
    `version`/`tag`/`run-id`/`unsigned` verbatim. The caller never wires
    stage outputs — that is the consumer-owned wiring ADR-0040 forbids, and
    exactly what WS06 proved unwireable.

    The caller's secret grants are UNIFORM across its stage jobs (#896):
    every stage job forwards the SAME plan-required secret set as `full`,
    trimmed only to the names its block declares (GitHub refuses a caller
    forwarding an undeclared secret). wf-prepare declares the full universe
    — preflight re-derives and re-validates the WHOLE plan's secret set at
    every prepare entry, so a standalone `prepare` needs exactly `full`'s
    set; wf-sign-mac declares the whole Apple/notary set; wf-publish the
    endpoint tokens; wf-build nothing. `shipit wf test` lints a stage-choice
    caller against this rule and refuses the drifting shape.

    Sharp edges, by design:

    - Facts travel all-or-none: supplying `stages` while omitting
      `unsigned-matrix` fails loudly at expression evaluation, never
      publishes against a mixed plan.
    - ONE source run per dispatch: a standalone sign re-dispatch makes its
      OWN run a complete publish source — it lands its signed-* artifacts AND
      re-uploads the base families it did not itself produce (every bundle-*
      tree plus release-notes, carried from its source run by the
      `carry-bundles` / `carry-notes` jobs), so the follow-up publish
      dispatch names THAT run as `run-id` and finds all three artifact
      families there. Multi-run stitching is unsupported; the escape hatch is
      the full re-dispatch, which converges (ADR-0009). The carried
      duplication is accepted.
    - An --unsigned source run re-publishes with `unsigned: true`, or the
      re-derived plan claims a signed path and the signed-* download fails
      loudly.
    - Cross-run downloads ride the REST API: the standalone dispatch
      caller's job grants `actions: read` beside the stage's own needs.
      `wf-sign-mac` and `wf-publish` deliberately declare NO `permissions:`
      key — a called workflow can only downgrade the caller's token, and a
      key would strip that grant.
    - A too-narrow per-stage secret grant is INVISIBLE to every green
      full-chain run — wf-release forwards the secrets internally — and
      kills only the standalone dispatch (the #896 live fire: a `prepare`
      forwarding RELEASE_TOKEN alone fails preflight's whole-plan secret
      validation on any plan with sign or registry endpoints, and a `sign`
      job omitting ASC_API_KEY_BASE64 dies at notarize). The uniform grant
      rule above is the guard, and `shipit wf test` enforces it.
