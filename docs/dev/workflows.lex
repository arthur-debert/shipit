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
        preflight -> prepare -> build[xOS] -> package[xOS] -> sign[mac] -> release

    - preflight: resolve whether signing is requested (one switch, referenced
      everywhere downstream), validate that the changelog has content, and
      validate Apple secrets only when signing is requested. Cheapest checks
      first, before any toolchain is installed.
    - prepare: bump the version, coalesce the changelog (see [#4]), commit, tag,
      push, and emit the release-notes artifact. For tauri this bumps THREE
      version files in lockstep (package.json + src-tauri/Cargo.toml +
      tauri.conf.json); they must stay in sync or the build breaks.
    - build [per OS]: build the frontend, then COMPILE ONLY (no bundle). The
      cross-job artifact is the ~tens-of-MB compiled binary + the built
      frontendDist — NOT the multi-GB target/ tree. This is the expensive,
      cache-warmed half (rust-cache + sccache).
    - package [per OS]: download the compile artifact, bundle it UNSIGNED, and
      assert the bundle's integrity before upload. Needs no rust toolchain.
    - sign [mac, optional]: only when signing is requested. See [#3.1].
    - release: publish the GitHub release from the notes + tag + bundles, with
      partial-release prevention (see [#3.3]).

    Per-platform fan-out is a matrix; opting out of a platform drops its matrix
    entry rather than leaving a dead job.

2. The producing / routing boundary, applied

    Each producing step is a pixi task, runnable on a laptop or in local Docker
    exactly as in CI:

    Producing (pixi tasks):
        - build-frontend — frontend/WASM build
        - build — compile only
        - bundle — bundle the compiled binary (unsigned)
        - assert-bundle — the integrity guard ([#3.2])
        - stage-assets — collect bundles for release
        - changelog — coalesce fragments ([#4])
        - create-release — publish

    Routing (thin workflow YAML):
        - the per-OS matrix
        - cross-job artifact upload / download
        - secret injection and presence checks
        - the macOS keychain import for signing

    These building blocks already exist in arthur-debert/release as
    bin-internal/*.sh scripts called by thin workflow steps, so the migration
    is mechanical: each script becomes a pixi task; the routing stays in YAML.

3. Design invariants the pipeline learned the hard way

    These three are not style preferences — each is a scar. Treat them as
    invariants any reimplementation must preserve.

    3.1. Sign and package are interleaved, not sequential

        The intuitive order — "sign the binary, then package it" — is wrong on
        macOS and the pipeline must not be built around it. tauri bundles the
        .app and .dmg together (coupled — they are not cleanly separable), and
        the package stage produces them UNSIGNED. The signer then REOPENS the
        package:

        - codesign the .app, nested Mach-O inner-first and the .app LAST (a flat
          sign leaves nested executables unhardened and notarization rejects it);
        - reseal the .dmg from the SIGNED .app via hdiutil — re-bundling would
          strip the signature;
        - codesign the resealed .dmg;
        - notarize + staple.

        So the model is *package(unsigned) -> sign-reopens-and-reseals*, never
        *sign -> package*. The package stage must also emit the inner .app as a
        reseal payload, because artifact upload does not preserve a .app's
        symlinks and exec bits.

        The mac signer is a CONSUMER-AGNOSTIC reusable unit: it operates on a
        .app/.dmg pair and makes no tauri (or electron) assumptions. Keep it
        that way — the only tauri-specific part is the bundler that produced the
        unsigned input, which lives in the caller.

    3.2. The integrity guard — signing is not integrity

        Before upload, assert that the bundle's MAIN binary is the expected app
        (mainBinaryName -> productName -> package name). This exists because a
        src-tauri crate with multiple binaries and no declared main once made
        the bundler pick the alphabetically-first one: a dev tool
        (gen_fixtures) shipped as the app's main executable, and it signed and
        notarized cleanly. Signature checks verify the signature, not that the
        artifact is the right binary. This verify stage is non-negotiable.

    3.3. Partial-release prevention

        The release job publishes ONLY when build and package succeeded and
        sign either succeeded (signed path) or was skipped (unsigned path). A
        FAILED sign or package blocks the release. This block cannot be a plain
        dependency — the default "a skipped dependency skips the dependent"
        would wrongly skip release on the unsigned path — so it is an explicit
        result check that accepts skipped-or-success for sign while still
        blocking on failure. Never ship a half-built set.

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
    release-notes artifact the release stage consumes.

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
    It is expressed as composable, opt-in jobs (build -> package -> sign ->
    release), each consuming the previous stage's artifacts. A consumer that
    needs signing wires in the sign job; one that does not, omits it and ships
    the unsigned bundles end-to-end with zero signing secrets.

7. Publishing reusable workflows — the publisher-side access surface

    Publishing reusable workflow blocks portfolio-wide (cross-owner `uses:`
    refs) requires a PUBLIC publisher repo — a private repo's workflows are
    shareable within its owner namespace ONLY, at any access level; the
    decision and its evidence live in ADR-0053
    [../adr/0053-shipit-is-public-vn-distribution-needs-a-public-publisher.md].
    Same-owner-only publishing from a private repo additionally needs the
    repo's Actions access level (`repos/{owner}/{repo}/actions/permissions/
    access`) at `user` (user-owned) or `organization` (org-owned) — it ships
    as `none`, which blocks even same-owner callers (TOL02-WS07 finding 5).
    gh-setup VERIFIES this and warns when a private `workflow_call` publisher
    sits at `none`, naming the fix; it never sets the level (#739).
