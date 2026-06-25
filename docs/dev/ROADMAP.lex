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

    :: done :: VERIFIED 2026-06-25 on macOS + Linux CI — a real tauri bundle with the correct main binary built on both via pixi-provisioned native deps. Findings + two provisioning gaps fixed (wasm-bindgen, zlib/expat) recorded in [./lessons-learned.lex] §8. phos main untouched; spike branch torn down.

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

    The `shipit gh-setup <repo>` subcommand makes a GitHub repo conform to the
    portfolio standard in three idempotent passes — branch ruleset, issue labels,
    repo secrets. It is the roadmap's first useful increment, and because it has
    NO pixi dependence (pure GitHub + Doppler plumbing) it is also the right place
    to stand up the shipit CLI skeleton itself.

    Prerequisite — stand up the shipit CLI:

        There is no shipit code yet (only docs + skills). gh-setup is the first
        subcommand, so Step 1 also creates the `shipit` console-script entry with
        git-style subcommands ([./architecture.lex] §4). Do not invent the
        scaffold — reuse release-core's proven patterns. release-core is Python
        3.11+ / click / hatchling at
        `/Users/adebert/h/release/templates/commons/lib/release_core/`: a
        hierarchical click tree (cli_entry.py), a verb-per-module convention
        (verbs/<name>.py exposing `main(argv) -> int`, attached by
        cli/_helpers.py:wrap_verb), and a single GitHub boundary in gh.py
        (`rest()`, `secret_set()`, `secret_list()`, `repo_view()`).

    Decision to confirm with the maintainer first (the one real fork):

        How shipit reuses release-core — DEPEND on the published release-core
        package and re-skin its entry points, or COPY the handful of needed pieces
        (gh.py plus the two verbs named below) into a fresh slim shipit package.
        [./architecture.lex] §4 ("KEEP that state machine; do not rewrite it, only
        re-skin its entry points") leans toward reuse; the global no-adapters /
        no-backwards-compat principle leans toward a clean copy. This sets the
        package's shape, so settle it before writing code.

    The three passes — each idempotent (safe to re-run; install AND update share
    this command):

        a. Ruleset:

            Apply the standardized main-branch-protection ruleset. The captured
            shape is `gh/main-branch-protection.json`, but it is a CAPTURE from
            phos-app and carries fields that must be stripped or recomputed per
            target repo: `id`, `source`, and the hardcoded `required_status_checks`
            contexts (`app-ui-unit-test / check`, `tauri-wire-contract-test /
            check`). Port the auto-discovery from release-core's
            `release_core/verbs/apply_ruleset.py`
            — it resolves the required checks from the target repo's own workflows
            and PUT/POSTs `gh api repos/{repo}/rulesets`. The rest is fixed:
            target=branch, ref=~DEFAULT_BRANCH, pull_request (0 approvals),
            required_linear_history, non_fast_forward, deletion, admin bypass.

        b. Labels:

            Ensure the standard label set exists. Source is `gh/issue-lables.toml`
            (data to clean up while here: the filename is misspelled; the
            `duplicate-of` entry uses `descriptions=` not `description=`; no entry
            has a `color`). release-core has NO bulk-label verb, so this pass is
            net-new: read the TOML and `gh label create --force` (or the `gh api`
            equivalent) each one so it is created-or-updated idempotently. The set:
            bug, feature, ready-for-agent, small, needs-decision, duplicate-of.

        c. Secrets:

            Resolve each secret from the consumer's `.shipit.toml` `[secrets]` map
            and push it with `gh secret set`. The map schema is in
            [./architecture.lex] §6: each entry maps a gh secret NAME (the table
            key) to a source — `{ doppler = "KEY" }`, `{ env = "VAR" }`, or `{
            prompt = true }`. No `.shipit.toml` exists yet, so Step 1 also defines
            that file and its `[secrets]` table. Port the sourcing + `secret_set()`
            flow from release-core's
            `release_core/verbs/install_release_secrets.py`;
            Doppler resolution is `doppler secrets get <KEY> --plain --project
            github --config prd`. A changed secret is re-set to its new value (the
            desired behavior per `README.lex`); a missing OPTIONAL source is
            skipped, not fatal.

    Verified by: running `shipit gh-setup` against a test repo yields the
    main-branch-protection ruleset carrying THAT repo's own required checks (not
    phos's), the full six-label set, and every mapped secret present in repo
    settings — and a second run is a clean no-op (proving idempotence).

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
