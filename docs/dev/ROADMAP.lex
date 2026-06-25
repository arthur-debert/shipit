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

    The `shipit install <path>` subcommand: vendor the small slow set into a
    consumer repo, recording per-unit pristine hashes in .shipit.toml. On
    re-install, hash-compare each managed unit against its stored pristine; open
    a PR with the changes (never admin-push), surfacing any consumer-edited unit
    as an override in the PR.

    After this step shipit is independently useful.

    Reuse the Step 1 skeleton — do NOT re-scaffold (Step 1 is merged):

        The CLI, the gh boundary, the config reader, and the test patterns
        already exist. install is a NEW verb in the SAME shape:

        - new verb `src/shipit/verbs/install.py`, attached in `cli.py` exactly
          like `gh-setup` (a thin click command forwarding to a `run(...)` that
          returns an int).
        - EXTEND `src/shipit/gh.py` with the git + PR primitives install needs
          (branch, add, commit, push, pr-create). COPY them slim from
          release-core's gh.py (`git_add`, `git_commit_paths`,
          `git_current_branch`, `git_default_branch`, `pr_create`) — boundary
          only, no logic.
        - EXTEND `src/shipit/config.py` to WRITE .shipit.toml, not just read it.
          tomllib is read-only — add a writer (the `tomli-w` dep, or hand-
          serialize the two small tables). This is the one genuinely new
          dependency decision.
        - keep the pure reconciliation logic (hash compare + per-unit decision)
          OUT of the gh/fs boundary so it is unit-testable, exactly as checks.py
          is split from its gh calls.

        Reference WITH CARE: release-core's `init` verb (verbs/init.py) is the
        closest analog — it installs a managed tree from the wheel bundle and
        commits only changed paths. But its model OVERWRITES managed paths and
        auto-commits to the branch; it does NOT do pristine-hash override
        detection and does NOT open a PR. Take its install-tree mechanics, NOT
        its overwrite-on-change behavior. Do NOT port sync.py — that 72k-line
        drift engine is precisely what this design exists to delete
        ([./lessons-learned.lex] §1c and §4 "Push versus pull").

    The managed set (what "slow" means here):

        Each managed unit is either a WHOLE FILE or a marker-delimited BLOCK in a
        consumer-owned file. The architecture's slow set is "the bootstrap, the
        lefthook caller, the skills, the AGENTS.md block" — but stage it to what
        exists now:

        - skills/ — whole files (the skills already in this repo: shipt-to-prd,
          shipt-to-issues, shipt-grill-with-docs, lex-primer — confirm the exact
          managed subset when defining the set). They
          must be bundled as PACKAGE DATA so the pip-installed `shipit` can vendor
          them — the same importlib.resources mechanism Step 1 used for
          `data/issue-labels.toml`. They are NOT packaged yet; add them.
        - the AGENTS.md block — a shipit-managed SECTION injected into the
          consumer's OWN AGENTS.md, delimited by markers. Adopt release's
          convention (sync.py): an opening `<!-- Managed by shipit; do not edit.
          Regenerate via shipit install. -->` and a closing marker. Hash the BLOCK
          content, not the whole file, since the consumer owns the rest.
        - the lefthook caller — DEFER to Step 3, where the lint gate it calls
          exists. Add it to the managed set then.

    The .shipit.toml manifest:

        Step 1 defined `[secrets]`. Step 2 adds two tables:

        manifest:

            [shipit]
            version = "<shipit commit hash that last wrote the set>"

            [managed]
            "skills/shipt-to-prd/SKILL.md" = "sha256:..."
            "AGENTS.md#shipit-block"       = "sha256:..."   # block, not whole file

        :: toml ::

        version pins the shipit commit that last wrote the set ([./architecture.lex]
        §6); [managed] is the pristine map the next re-install compares against.

    The reconciliation algorithm (a hash compare, not a subsystem):

        Per managed unit, exactly three cases — keep it this small (the moment it
        grows features it becomes the drift engine, [./lessons-learned.lex] §4):

        - absent in the consumer → ADD it; record its hash.
        - present, consumer-hash == stored pristine → UNCHANGED: overwrite with
          the new shipit content silently; update the stored pristine.
        - present, consumer-hash != stored pristine → CONSUMER-EDITED: do NOT
          clobber. Surface the override in the PR — show shipit's intended content
          against the consumer's edit and leave the decision to the human.

        A first install has no [managed] table, so every unit is the "absent"
        case. "Surface the override" means make the divergence visible in the PR
        (e.g. the PR body lists each overridden path with its diff), never a
        silent overwrite.

    PR mechanics — pull, never push:

        install stages onto a branch (e.g. `shipit/install`), commits the managed
        changes, pushes the branch, and opens a DRAFT PR for a human to merge —
        the same draft -> shepherd -> ready lifecycle shipit itself follows
        (AGENTS.lex). It NEVER admin-pushes to main. The `--push` flag is the
        sole break-glass: a straight push to main, reserved for bootstrapping a
        repo that cannot yet run the PR loop (the README). Support
        `--dry-run` (print the plan, touch nothing), as Step 1's verbs do.

    Open questions to settle with the maintainer BEFORE coding (one short fork
    each, as Step 1's COPY-vs-DEPEND was):

        - "bootstrap" is undefined: what file/mechanism makes `shipit` available
          in a consumer (a pixi dependency line? a bin launcher?)? It is listed in
          the slow set but has no source yet, and is likely entangled with the
          pixi integration deferred to Steps 5-6. Confirm whether Step 2 manages
          it or defers it.
        - the block marker exactly: confirm the marker text, and that block-
          hashing (not whole-file) is the AGENTS.md model.
        - self-install: shipit's own repo IS the source of skills + AGENTS, so
          decide whether `shipit install .` on shipit is a supported identity
          no-op or simply out of scope (test against a real consumer repo — the
          arthur-debert/release-canary-* repos are the standing throwaways).

    Verified by: a fresh install on a test consumer opens a PR that adds the
    managed set and writes the [shipit] / [managed] tables; a re-install after the
    consumer edits a managed file opens a PR that SHOWS the override rather than
    silently clobbering it; a re-install with no changes is a clean no-op (no PR,
    or an empty one), proving churn tracks shipit cadence, not invocation count.

3. Step 3 — lint / fmt

    The standardized multi-language gate: a `[feature.lint]` pixi environment
    that provisions the linters, a `shipit lint` subcommand that runs them over
    the tree, exposed as `pixi run lint`, and a thin lefthook caller that fires
    it on pre-commit and pre-push. This is shipit's FIRST pixi integration — the
    point where the substrate proven in Spike 0 stops being a spike and becomes
    a real dependency of shipit's own repo. The gate is dogfooded on shipit from
    this step forward ([./lessons-learned.lex] §1d).

    The one inversion to internalize BEFORE reading release-core's gate:

        release-core's gate (release_core/verbs/gate.py) runs `lefthook run
        pre-commit --all-files` — lefthook IS the orchestrator, carrying a
        per-language glob map and shelling each tool, while the `gate` verb only
        wraps it and parses its `GATE: OK` verdict. shipit INVERTS this. Because
        pixi has NO cross-manifest task inheritance ([./architecture.lex] §5),
        the rich logic cannot live in a pixi task templated into each consumer
        (that is drift on pixi.toml); it lives in the binary. So in shipit the
        orchestration moves OUT of lefthook and INTO `shipit lint`: lefthook is
        thin (it calls `pixi run lint`), pixi is thin (it runs `shipit lint`),
        and `shipit lint` does the per-language discovery, routing and
        aggregation. Do NOT reproduce release's lefthook-as-orchestrator shape,
        its toolset.py npm/pip/binary provisioning, or its verdict parsing —
        those three are exactly what pixi plus the binary model replace.

    What to reuse from release-core (the slim, valuable part):

        Take the per-language TOOL INVOCATIONS and version pins as the starting
        reference — they are battle-tested command lines, not orchestration:

        - python — `ruff check` + `ruff format --check`
        - rust — `cargo fmt --all -- --check` + `cargo clippy --all-targets
          --all-features -- -D warnings`
        - shell — `shellcheck --severity=info` (+ `shfmt -d` for formatting)
        - yaml — `yamllint`
        - json — `prettier --check`
        - markdown — `markdownlint`
        - go — `gofmt -l` + `go vet ./...` (+ `golangci-lint run` where present)
        - lex — `lexd check` (shipit-native; see the provisioning gap below)

        release-core's pins live in toolset.py (ruff 0.15.x, shellcheck 0.11,
        yamllint 1.38, prettier 3.x, markdownlint-cli 0.48, lefthook 2.1.9,
        golangci-lint 1.64); reuse them as a baseline but RE-PIN through
        conda-forge, not npm/pip — see the pixi integration below. Skip every
        release-specific lefthook command (workflow-action-major,
        consumer-contract-*, captured-fixtures-lint, lint-skills): those encode
        release's sync model, the thing shipit deleted.

    The shipit lint verb (the orchestrator):

        A NEW verb `src/shipit/verbs/lint.py`, attached in cli.py exactly like
        gh-setup (a thin click command forwarding to a `run(...) -> int`). The
        per-language orchestration is pure logic — keep it OUT of the subprocess
        boundary so it is unit-testable, the same split checks.py uses against
        its gh calls. The verb:

        - DISCOVERS files (whole tree via `git ls-files`, honoring ignores) and
          ROUTES each to a toolchain by extension and — for extensionless
          scripts — shebang (release routes shell this way; mirror it).
        - RUNS each language's tool(s), aggregates the results, and emits one
          verdict. It is a HARD gate ([./architecture.lex] §7): a missing tool
          exits non-zero, never skips. A clean run is `0`; any failure is `1`.
        - is the SAME definition everywhere. CI runs `pixi run lint` (= this
          verb) and the lefthook pre-commit hook runs `pixi run lint`; "both
          agree" because it is ONE binary with ONE config, not two transcriptions
          of the rules drifting apart.

    The pixi integration (shipit's first pixi.toml):

        Step 3 adds a pixi.toml to shipit with a `[feature.lint]` environment
        carrying the linter dependencies and a `lint = "shipit lint"` task. The
        linters are required-check-path tools, so they are PINNED in pixi.lock
        and CI runs `--locked` ([./architecture.lex] §2) — bumps arrive as
        auto-PRs, never silently. shipit's own CI flips here from the
        self-contained python job to `setup-pixi` + `pixi run lint`; the required
        check name (`check`) stays stable across the move — the ci.yml header
        comment already anticipates exactly this.

        The conda-forge provisioning reality: most linters are clean conda-forge
        packages (ruff, shellcheck, shfmt, yamllint, go, lefthook, actionlint,
        golangci-lint), but THREE are not — the same gap class Spike 0 hit with
        wasm-bindgen ([./lessons-learned.lex] §8):

        - prettier, markdownlint-cli — npm tools. Provision nodejs from
          conda-forge and `npm install -g` the pinned versions, or pick
          conda-native substitutes.
        - lexd — a cargo/rust binary (it lives at ~/.cargo/bin), NOT on
          conda-forge. `cargo install lexd` at a pinned version (the wasm-bindgen
          pattern from Spike 0), or vendor a prebuilt binary.
        - cargo fmt / clippy — components of the rust toolchain; confirm the
          conda-forge `rust` package carries them or add the rustup components.

        Resolving these is the central provisioning fork (below), not a detail:
        the hard-gate rule means an unprovisioned linter FAILS the gate, it does
        not quietly skip.

    The lefthook caller (the unit Step 2 deferred):

        A thin lefthook.yml with two hooks, each a one-line caller: pre-commit ->
        `pixi run lint`, pre-push -> `pixi run lint` (and `pixi run test` once
        Step 5 supplies test). lefthook itself comes from conda-forge pinned in
        pixi.lock, so release's "one runner on PATH" dance (toolset.py resolving
        the right lefthook past node_modules copies) dissolves — pixi provides
        exactly the pinned binary. `shipit lint` installs the git hooks (the
        release `--install-hook` equivalent) so a fresh clone is one command from
        a working gate.

        This lefthook caller is the managed unit Step 2 explicitly DEFERRED to
        Step 3 ([#2], "the lefthook caller — DEFER to Step 3"). Step 3 adds it —
        plus the `lint`/`test` task lines and the `[feature.lint]` deps — to
        install's managed set, so a `shipit install` provisions a consumer's gate
        the same way it provisions the skills and the AGENTS.md block.

    The consumer-side question — how the gate lands in a consumer's pixi.toml:

        install must get the `[feature.lint]` deps and the thin task lines into
        the CONSUMER's pixi.toml without making pixi.toml a managed-but-edited
        drift file — the precise hazard [./architecture.lex] §5 names (templating
        a task into the consumer's pixi.toml makes the manifest a managed-but-
        edited file, drift on the most important config file). The thin
        `lint = "shipit lint"` line is stable and safe; the dependency pin list
        is the open part. Settle whether this is a marker-delimited shipit BLOCK
        in pixi.toml (block-hashed like the AGENTS.md block and reconciled by Step
        2's algorithm) or another mechanism. This couples Step 3 to Step 2's
        reconciliation and should be settled with it.

    Dogfood scope — what shipit's own gate actually exercises:

        shipit's repo is python + lex + yaml + json + shell + markdown, so its
        own `pixi run lint` exercises only those legs; the rust, go and tauri
        toolchains it standardizes are NOT present here (the dogfood blind spot,
        [./lessons-learned.lex] §6). That is acceptable for Step 3 — the gate's
        SHAPE and the python/lex/shell/yaml/json/markdown legs are dogfooded for
        real; the compiled-language legs are first exercised against a real
        consumer when install carries the gate outward, and fully at Step 6's
        reference cut.

    Open questions to settle with the maintainer BEFORE coding (one short fork
    each, as Steps 1-2 had):

        - lexd and the conda-forge gap: how the three non-conda linters (lexd,
          prettier, markdownlint-cli) are provisioned in `[feature.lint]` —
          cargo/npm install at a pin, conda-native substitutes, or vendored
          binaries. lex is shipit-native and load-bearing for its own docs, so
          lexd cannot simply be dropped.
        - check vs fix: README §2 says "Linting AND Formatting". Decide whether
          the gate is check-only (`shipit lint`, with formatting offered
          separately as `shipit fmt` or a `--fix` mode) or one verb with a fix
          mode. Heed release's scar: `prettier --write` under --all-files
          silently rewrites untouched files, so the GATE stays check-only and any
          auto-fix is opt-in.
        - whole-tree vs staged: `shipit lint` defaults to the whole tree (what CI
          runs); confirm the pre-commit hook lints staged files only (re-staging
          any fixes, release's `stage_fixed`) while CI lints all — "both agree"
          means same tool + config + rules, not an identical fileset.
        - path -> toolchain map ownership: [./architecture.lex] §6 puts "the path
          -> toolchain map" in .shipit.toml. Decide whether routing is
          built-in-by-extension with an optional `[lint]` override, or fully
          config-driven. Keep the default zero-config.
        - consumer pixi.toml integration: the managed-block-vs-other-mechanism
          question above — settle alongside Step 2's reconciliation.

    Verified by: on shipit itself, `pixi run lint` and the lefthook pre-commit
    hook run the IDENTICAL gate and agree; CI runs the same `pixi run lint`
    under --locked; a file with a deliberate lint error fails the gate non-zero
    (proving the gate is hard, not advisory) and a clean tree passes; and a
    missing linter fails loudly rather than skipping. The consumer-install leg
    (the lefthook caller + task lines + feature deps added to install's managed
    set) is verified when Step 2's install carries them into a test consumer.

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
