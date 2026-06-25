shipit Roadmap

This is the incremental, verifying sequence for building shipit. Unlike
[./architecture.lex] and [./workflows.lex] — which describe the final state —
this document is explicitly about the PATH, ordered so that each step leaves a
working tool and is independently verifiable.

Two hard rules:

    - shipit must be a useful, shippable tool after Step 2, and nothing in a
      later step may break what an earlier step delivered.
    - Do NOT retire release-core until shipit has cut one real release of one
      real consumer (Step 6). The PR state machine is the crown jewel; it keeps
      running until its replacement is proven on a real artifact.

Each step below states what it builds and how it is verified. A step is not
done until its verification passes.

0. Spike 0 — prove pixi runs the rust + tauri toolchain

    This gates everything. The whole premise (pixi as substrate) rests on pixi
    cleanly provisioning and running the hardest toolchain in the portfolio. If
    pixi-managed rust + native GUI deps fight the build, the foundation must be
    reconsidered before any other step is worth starting.

    Where: on a phos-app BRANCH, in a worktree — never the live checkout, so
    ongoing development is undisturbed. The spike is a self-contained workflow
    with `on: push`, which runs from its own branch (the default-branch
    requirement only applies to workflow_dispatch, schedule, and
    reusable-from-main — a push-triggered workflow runs from the pushed branch).
    Inline steps only; NO nested reusable refs.

    What it does:

        - matrix [macos-latest, ubuntu-latest]
        - install pixi
        - provision rust + mold/lld + webkit2gtk/gtk via conda
        - build the frontend
        - run a REAL `tauri build`
        - assert the produced bundle has the correct main binary

    Watch for: the phos-editor org Actions policy may block
    prefix-dev/setup-pixi as an unverified marketplace action (it already
    blocks pnpm/action-setup). The fallback is pixi's curl installer; the spike
    is the right place to surface this.

    Verified by: a real, signable bundle builds on BOTH macOS and Linux using
    pixi-provisioned native deps; phos main is untouched; the branch is
    deletable with no residue.

1. Step 1 — gh-setup

    The `shipit gh-setup` subcommand: apply the branch ruleset, ensure the
    issue labels exist, and install repo secrets resolved from the .shipit.toml
    secret map (Doppler / env / prompt). No pixi dependence; pure GitHub +
    Doppler plumbing.

    Verified by: running it against a repo yields the expected ruleset, the
    full label set, and the mapped secrets present in the repo settings.

2. Step 2 — install + reconciliation

    The `shipit install` subcommand: vendor the small slow set (bootstrap,
    lefthook caller, skills, AGENTS.md block), recording per-file pristine
    hashes in .shipit.toml. On re-install, hash-compare each file; open a PR
    with the changes (never admin-push), surfacing any consumer-edited file as
    an override in the PR.

    After this step shipit is independently useful.

    Verified by: a fresh install opens a PR that adds the managed set; a
    re-install after a consumer edits a managed file opens a PR that shows the
    override rather than silently clobbering it.

3. Step 3 — lint / fmt

    A [feature.lint] environment carrying the linter dependencies, the `shipit
    lint` subcommand running the standardized multi-language gate, exposed as
    `pixi run lint`, and a thin lefthook caller wired to it.

    Verified by: the lefthook pre-commit hook runs the gate via pixi locally;
    CI runs the SAME `pixi run lint`; both agree.

4. Step 4 — PR flow

    `pixi add` the existing PR-state-machine package and expose thin `shipit pr
    status` / `shipit pr next` subcommands over it. Do NOT rewrite the state
    machine — re-skin its entry points only.

    Verified by: pr status / pr next drive a real PR through the review -> ready
    lifecycle on a test repo.

5. Step 5 — pixi test / build / run + changelog / release

    Encapsulate each project's test and build behind pixi tasks (the consumer
    supplies `test`; `build` runs the real builder). Build the changelog and
    release subcommands ON pixi tasks — NOT on the pixi-build backend. This step
    runs ALONGSIDE the existing release workflows; nothing is retired yet.

    Verified by: `pixi run test` and `pixi run build` work locally and in CI on
    at least two different Kinds; `shipit changelog` coalesces unreleased
    fragments into a version and feeds the tag + release notes.

6. Step 6 — workflows + cutover

    The thin reusable workflow (`setup-pixi` + `pixi run ci`); port the
    composable build -> package -> sign -> release jobs from
    [./workflows.lex]; parametrized command dispatch for the easy 80% and
    composable opt-in jobs for the signing 20%. This is where complexity has
    historically exploded, so ladder it finest here — land and verify one job
    boundary at a time.

    Verified by: shipit cuts one REAL release of one real consumer — the
    artifact inspected for the right binary, signed and notarized — BEFORE
    release-core is retired. Only after that real cut does the second hard rule
    release its hold.
