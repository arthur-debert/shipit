shipit Lessons Learned

This document preserves the reasoning chain and history behind shipit's design
— the "why behind the why". The other dev docs describe the final state and the
path to it: [./architecture.lex] holds the decisions, [./workflows.lex] the CI
pipeline, [../legacy-prd/FUTURE_WORK.md] the high-level map. This one is the exception:
it is allowed to discuss rejected alternatives, raw research findings, and
unsolved problems, so that a future session starting with a fresh context can
recover not just WHAT was decided but WHY, and what was consciously left open.

When the final-state docs and this one disagree, the final-state docs win — they
are maintained; this is a record of a moment. Read it for rationale, not for
current truth.

1. Why shipit exists — the four root causes of release's pain

    shipit is a deliberate restart, not a refactor. release (its predecessor)
    became a design and maintenance sink for four distinct reasons, and naming
    them is what keeps shipit from repeating them.

    a. Bad initial understanding of the problem space:

        The needs and the way agents actually use the tooling were not yet
        understood when release's goals were set, so the early design solved a
        problem that turned out not to be the real one.

    b. Overly complex goals with no incremental complexity ladder:

        release aimed at the full end state from the start, with no ordering
        that left a working, useful tool at each step. Complexity arrived all at
        once and never had a floor to stand on.

    c. Reimplementing tools that already existed:

        The most expensive mistake. release hand-built a bootstrap / resolver /
        drift subsystem (an isolated-venv wheel resolver, dependency fetchers, a
        sync-and-drift-check engine) instead of using an off-the-shelf
        provisioner. That subsystem was a large fraction of the accidental
        complexity, and it was all reinvention.

    d. Weak code review letting drift creep in:

        Code inconsistent with the goals and design was allowed in over time,
        and the cost compounded.

    What shipit's architecture answers, and what it cannot:

        The architecture answers (a), (b), and (c) structurally — explicit
        configuration replaces guessing at needs; the build sequence is an ordered
        ladder; pixi replaces the reinvented subsystem. But (d) is a process
        discipline, not an architecture: the design can only *help* — a smaller
        surface and visibly-tracked drift give review less to miss — it cannot
        guarantee. The real defense against (d) is keeping the package small and
        dogfooding the lint checks on shipit itself from the first commit.

2. The foundation decision: pixi, and why not Devbox/Nix

    The foundation choice was researched head-to-head: pixi (conda-based) versus
    Devbox (Nix-based). pixi won, and the deciding axis was the one that decides
    it for this portfolio specifically — the rust + tauri/electron native-build
    reality. The decision is recorded in [./architecture.lex] §1; the reasoning
    is here.

    The decisive trap — alternative linkers on Nix:

        A committed `.cargo/config.toml` selecting `-fuse-ld=mold` or
        `-fuse-ld=lld` is a documented Nix landmine. mold and lld emit binaries
        with no RUNPATH; on Nix it is the `ld` *wrapper* that injects rpath, so
        the honest linkers produce output that fails to find its libraries at
        runtime. The canonical NixOS fix is literally "remove the
        cargo/config.toml". That exact forced-linker pattern is load-bearing
        across release's cargo lanes — so Nix would fight precisely the
        configuration this fleet depends on.

    The native-GUI escalation:

        - Tauri's AppImage bundling under Nix is community-escaped to Docker: it
          execs prebuilt FHS binaries (linuxdeploy, appimagetool) that do not
          resolve on the non-FHS store.
        - electron ships a prebuilt FHS binary that needs a hand-maintained
          LD_LIBRARY_PATH to run under Nix.
        - rustup ships its own prebuilt ld.lld whose ELF interpreter does not
          resolve on Nix.

        Every one of these is a place where Nix's hermeticity HELPS for declared
        system libs but HURTS for the prebuilt/compiled-native world this stack
        lives in.

    Devbox's secondary losses (independent of the native-build axis):

        - Task runner: a flat script map with no dependencies, arguments, or
          caching — versus pixi's real DAG (`depends-on`), MiniJinja arguments,
          and inputs/outputs fingerprint caching.
        - No native multi-environment — versus pixi's features / environments /
          solve-groups, which are exactly the lint-vs-test-vs-build dep-set split
          shipit needs.
        - Shipping a custom CLI means authoring a Nix flake — versus declaring a
          conda/PyPI dependency, which mirrors release-core's existing pip-wheel
          model almost 1:1.
        - CI caching is whole-Nix-store (cold-cost plus a lockfile-churn cache
          footgun) — versus setup-pixi's lockfile-keyed cache.
        - Maturity: Devbox is pre-1.0 after roughly three years; pixi is past
          1.0.

    The honest caveat:

        Devbox is not a bad tool. For a pure non-GUI portfolio — CLIs,
        libraries, shell tooling, no native-GUI compilation — it would WIN:
        nixpkgs breadth and commit-level pinning beat conda there. It loses for
        THIS stack specifically because of the tauri/electron desktop app plus
        the forced mold/lld linker, which are Nix's two worst cases at once.
        Choosing Devbox would have re-committed root cause (c) from [#1] in a new
        costume: adopting a tool whose hermeticity fights the stack, then
        hand-maintaining LIBRARY_PATH / LD_LIBRARY_PATH escape hatches and
        eventually writing raw flakes.

3. What pixi gives, and its sharp edges

    pixi was chosen for what is mature in it, with eyes open to what is not.

    The load-bearing, mature surfaces:

        - Tasks: a real runner — `depends-on` DAG, MiniJinja arguments,
          inputs/outputs fingerprint caching.
        - Features / environments / solve-groups: the lint / test / build
          dependency-set split from one manifest.
        - conda-forge provisioning of native tools (shellcheck, lefthook, node,
          go, rust, ripgrep, mold/lld, gtk3, webkit2gtk) cross-platform.
        - setup-pixi CI integration: lockfile-keyed caching and `--locked`.
        - pixi.lock reproducibility across platforms.

    The constraint that forced the binary model:

        pixi has NO cross-manifest task inheritance. A consumer cannot inherit
        or override a task that shipit defines elsewhere. So a rich task cannot
        be "shipped" by reference — the only choices are to template it into the
        consumer's pixi.toml (making the manifest a managed-but-edited file —
        drift, on the most important config file) or to put the logic in a
        binary and reference it from a trivial one-line task. shipit takes the
        second path. This is the direct cause of [./architecture.lex] §5.

    The constraint that keeps the real builders:

        pixi-build (the build backend) is preview-grade and emits conda
        packages — not wheels, not signed installers. So pixi is used to
        PROVISION and RUN the real builders (cargo, tauri, electron-builder),
        never as the build backend itself. "leverage pixi build" was a near-miss
        that would have walked straight into immature tooling.

    Sharp edges to remember:

        - pixi.lock is one file across all environments and platforms:
          merge-conflict-prone, and a change in one environment invalidates
          unrelated CI caches.
        - pixi is pre-1.0 on its action API — pin versions everywhere.
        - conda-first means PyPI sdist / git dependencies are the rough edge.

    The required-check-path seam:

        The tension "fast code should pop up everywhere" collides with pixi.lock
        pinning plus CI running `--locked` — a naive "shipit is a pixi dep"
        would make every fix a per-consumer lockfile bump. The resolution is the
        required-check-path line: required-CI-path tools are pinned in the lock
        (bumps arrive as auto-PRs); agent / PR-loop tooling lives OUTSIDE the
        locked env so it auto-updates fleet-wide. The full statement, including
        the accepted cost, is in [./architecture.lex] §2.

4. The design tensions and how each resolved

    Each of these was a real fork in the road. Recording the tempting-but-wrong
    branch matters as much as the resolution.

    Push versus pull:

        The tempting answer was to admin-push managed files to consumer main and
        reconcile drift afterward. release lived this and spent roughly two years
        migrating push -> pull, concluding on pull. The resolution shipit adopts
        in one decision is the slow/fast split: commit the tiny slow set and
        reconcile it by hash into a PR (never admin-push); ship fast-changing
        code through the package. The drift-detection engine release built is the
        smell, not the cure — uncontrolled mutation is the thing to avoid, so do
        not create it.

    "Retire release-core down to only YAML":

        A fantasy. The PR state-machine logic has to live somewhere; "no code in
        YAML" means it lives in one slim package, not that it evaporates. The
        endgame is *one slim versioned package + thin tasks + thin callers*.
        release-core is not retired so much as slimmed and renamed.

    Vendoring thick workflows into consumers:

        Tempting because it avoids the @v3 internal-ref drift that bit release
        repeatedly. But it trades a fixable drift (one ref to bump) for an
        unfixable one (N vendored copies, no single bump). The resolution is a
        thin reusable workflow (`setup-pixi` + `pixi run ci`) pinned by tag, with
        all behavior shipping via the pinned package — the YAML stays boring and
        stable, and upgrades are one bump.

    Config split-brain:

        Putting layout/policy config in pyproject.toml is Python-centric, and
        most repos in this portfolio are not Python; pixi.toml is strict about
        unknown tables. The resolution is a dedicated .shipit.toml plus an
        ownership line that prevents overlap: pixi.toml owns provisioning,
        .shipit.toml owns policy. Different layers, so no split-brain.

5. The workflow reality — what release's tauri pipeline taught us

    The composable pipeline is not theoretical: the 7-stage shape (preflight ->
    prepare -> build -> package -> sign -> release) already exists in
    arthur-debert/release as thin steps calling `bin-internal/*.sh` scripts. The
    migration to pixi tasks is therefore mechanical, not a redesign. The full
    design is in [./workflows.lex]; the hard-won invariants are recorded here so
    they are never re-derived by accident.

    sign and package are interleaved, not sequential:

        The naive model is "build -> sign -> package". The real macOS flow is
        not separable that way. package emits an UNSIGNED .app + .dmg (tauri
        builds them coupled); the signer then REOPENS the package: codesign the
        .app (nested Mach-O inner-first, .app last) -> reseal the .dmg from the
        signed .app via hdiutil (re-bundling would strip the signature) -> sign
        the .dmg -> notarize + staple. Model it as "package(unsigned) ->
        sign-reopens-and-reseals".

    Signing is not integrity:

        phos once shipped a fully signed and notarized bundle whose main binary
        was a dev tool (gen_fixtures) — every signature check passed. So the
        pipeline MUST assert the bundled main binary's identity before upload. A
        green signature proves nothing about WHICH binary was signed.

    Partial-release prevention:

        The release stage publishes only when build and package succeeded and
        sign either succeeded or was skipped (the unsigned path). A FAILED sign
        or package blocks the release. Never ship a half-built asset set.

    Frontend builds per-platform:

        tauri wants frontendDist co-located at compile time, so the frontend
        builds inside each platform leg; the expensive part (the WASM bundle) is
        cached per-OS. "Build the frontend once and share it" is an optimisation
        tauri resists, and the payoff is small because the cheap part is the JS,
        not the WASM that is already deduped.

6. What shipit does NOT solve — open risks, recorded honestly

    The euphoria of deleting the provisioning/drift half should not hide the half
    that remains.

    The signing / release-pipeline complexity is untouched:

        pixi crushes provisioning, tasks, and environments — roughly the
        lint/test/boot half of release's cost. The OTHER half (Apple
        notarization, packaging, multi-platform matrix) is irreducible and pixi
        does nothing for it. shipit PORTS phos's composable jobs, and porting is
        not simplifying. The only relief is that it is solved once and copied,
        not reinvented.

    The dogfood blind spot persists, and is arguably sharper:

        shipit's own CI will not exercise consumer rust/tauri/node toolchains
        (it is the source repo, not a consumer), and the auto-updating fast
        surface breaks fleet-wide at once with no per-consumer rollback pin.
        canary/livefire fleet-verification was deliberately DROPPED as too
        expensive for a personal portfolio. The cheap mitigant that recovers
        most of its value: shipit's own CI runs the full loop against ONE real
        reference consumer before publishing the package.

    pixi is a new foundational dependency risk:

        release reimplemented things partly because it controlled them. shipit
        bets the floor on a pre-1.0, conda-first tool whose action API can break
        between minors and whose rust/tauri toolchain story is unproven for this
        stack. This is the right trade, but it is not free — Spike 0 is the
        load-bearing test of the whole premise, which is why it blocks everything
        in [../legacy-prd/FUTURE_WORK.md].

    Step 6 is the danger zone:

        Workflows + cutover is the exact spot release's complexity exploded, and
        it is where the plan's ladder is weakest — it carries the irreducible
        signing half plus the release-core cutover at once. Ladder it finest
        there; treat "retire release-core" as a celebration after a real cut,
        never a goal pushed toward.

7. Deliberate omissions — dropped from release on purpose

    These are conscious decisions, not oversights. Recording them stops a future
    session from "fixing" a gap that was intentional.

    - canary / livefire fleet-verification machinery: dropped. Too expensive for
      a personal portfolio. The reference-consumer CI loop in [#6] recovers most
      of the value at a fraction of the cost.
    - portfolio-level fleet commands (the orc tool, managed-repos.yaml): reduced
      to just two primitives — run-a-task-in-a-project and checkout-a-worktree.
      The elaborate fleet orchestration was complexity the portfolio's size does
      not justify.

8. Spike 0 outcome — pixi DOES run the rust + tauri toolchain (verified 2026-06-25)

    The premise question from [../legacy-prd/FUTURE_WORK.md] is answered yes: pixi-provisioned
    native deps built a real tauri bundle with the correct main binary on BOTH
    macos-latest and ubuntu-latest. Done on a throwaway phos-editor/app branch
    (spike/pixi-tauri), since torn down — phos main untouched. The foundational
    risk in [#6] ("pixi is a new foundational dependency risk") is retired for the
    rust + tauri stack; the floor holds.

    What provisioned cleanly from conda-forge:

        - Modern webkit is `webkit2gtk4.1` (2.48.5), linux-64 ONLY (macOS links
          the system WebKit.framework). This was the single biggest threat to the
          pixi premise — it is real, current, and present. It needs
          `[system-requirements] libc = glibc 2.34` or the linux-64 solve fails.
        - The forced mold/lld linkers from the committed .cargo/config.toml are
          ordinary conda packages — the Nix worst-case from [#2] is a non-issue on
          pixi's FHS prefix, exactly as predicted.
        - rust from conda (NO rustup) plus the wasm32 target as the conda package
          `rust-std-wasm32-unknown-unknown` (version-locked to `rust`).

    Two real provisioning gaps the spike hit and fixed — carry these into the
    shipit install / CI design, they do not solve themselves:

        wasm-bindgen is NOT on conda-forge:

            conda-forge ships no `wasm-bindgen-cli` package, and `wasm-pack --mode
            no-install` only appeared to work locally because a prior run had
            cached wasm-bindgen — a clean runner has nothing to find and fails
            ("Not able to find or install a local wasm-bindgen"). The fix that
            holds: `cargo install wasm-bindgen-cli` at the EXACT version pinned in
            the consumer's Cargo.lock, built with the conda cargo (no rustup
            anywhere). shipit must provision wasm-bindgen this way for every
            wasm-building consumer; pixi/conda does not give it for free.

        conda-forge's libNAME / NAME split breaks pkg-config closures:

            webkit2gtk-4.1's pkg-config closure failed because conda-forge splits
            some libraries into a `libNAME` runtime package (pulled in
            transitively by gtk/glib) and a `NAME` package that carries the `.pc`.
            Only the runtime halves arrived, so `pkg-config --exists
            webkit2gtk-4.1` died on `gio-2.0 -> zlib`, then `fontconfig -> expat`.
            Fix: add `zlib` and `expat` to the linux deps explicitly. Expect a
            short tail of these for any native-GUI consumer.

    Smaller facts worth keeping:

        - pixi `[activation.env]` OVERRIDES a shell/CI-exported variable. So a
          per-environment value (the spike's PHOS_CORE_PATH, pointing at the
          consumer's native-dep source) must NOT live there — default it in a
          script and let CI export the real path, or CI cannot redirect it.
        - setup-pixi was NOT blocked by the phos-editor org Actions policy (the
          worry flagged in [../legacy-prd/FUTURE_WORK.md] did not bite this time). RELEASE_TOKEN
          cloned the private phos-core at its pinned tag, serving BOTH the native
          git-dep patch and the from-source wasm build from one clone.
        - The producing-logic-runs-in-local-Docker property ([./architecture.lex] §3)
          paid off immediately: reproducing the linux-64 env in a `--platform
          linux/amd64` container gave ~2-minute pkg-config probe loops instead of
          ~25-minute CI round-trips while diagnosing the webkit closure.

    The native build must use the consumer's native-dep source AT THE PINNED
    version. The spike first failed compiling against a local phos-core dev
    checkout 6 commits past the tag (an enum variant had drifted); building
    against the exact pinned tag — what CI clones — compiled clean. shipit's
    install / CI must pin the native-dep source as firmly as it pins everything
    else.
