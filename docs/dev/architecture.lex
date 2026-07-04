shipit Architecture

This document records the load-bearing design decisions and, for each, the
reasoning that makes it load-bearing. It describes the FINAL STATE shipit aims
to be — not any migration step. The incremental path to get there lives in
[../prd/FUTURE_WORK.md].

The short version: pixi is the substrate; what shipit manages is split by how
fast it changes; producing logic is pixi tasks and routing logic is thin YAML;
the durable code is one slim versioned package; and configuration is explicit.

1. pixi as the substrate

    shipit uses pixi (the conda-based provisioner) for four things at once:
    provisioning native tooling, running tasks, defining per-purpose
    environments, and integrating with CI. This deletes an entire class of
    work — the bespoke bootstrap/resolver/drift machinery — that a homegrown
    tool would otherwise reinvent.

    Why pixi and not Devbox/Nix:

        Nix's hermetic, non-FHS, rpath/ld-wrapper model fights compiled native
        toolchains exactly where this portfolio lives — alternative linkers
        (mold/lld), `-sys` crates, prebuilt rustup/electron/linuxdeploy
        binaries, and tauri's AppImage bundling. pixi sits on a normal FHS
        prefix and treats rust, gtk3, webkit2gtk, and electron as ordinary
        relocatable packages, so the hardest builds (rust CLIs, a tauri
        desktop app) do not become a fight with the provisioner.

    What pixi does NOT own: building and signing distributable artifacts. The
    `pixi build` backend is preview-grade and emits conda packages, not wheels
    or signed installers. shipit keeps the real builders (cargo, tauri,
    electron-builder) and uses pixi to PROVISION and RUN them — never to be the
    build backend. See [#3].

2. The slow/fast split

    This is the central decision. shipit manages two very different kinds of
    thing, and conflating them is what makes a fleet tool rot.

    Slow + file-structure-dependent:

        The bootstrap, the lefthook caller, the skills, the AGENTS.md block,
        the `./claude-start` launcher, the SessionStart activation hook.
        These rarely change and must live as real files in the consumer repo.
        They are committed into the consumer. On re-install, shipit
        hash-compares each managed file against the pristine hash stored in
        .shipit.toml at the previous install:

        - Unchanged in the consumer: overwritten silently.
        - Edited in the consumer: surfaced loudly (a stderr warning on the
          default working-tree refresh; the override diff in the `--pr`
          reconcile PR), never clobbered blind.

        By default the refresh lands in the WORKING TREE, uncommitted — the
        caller folds it into their own commit. Only `install --pr` stages the
        set on the `shipit/install` branch and opens the draft reconcile PR
        (the standalone onboarding flow); mid-workstream, install never
        branches, pushes, or opens a PR on its own (#359).

        Reconciliation is a hash compare, not a subsystem. The moment it grows
        features it has become the drift engine this design exists to avoid.

    Fast-changing code:

        PR-review adapters, bug fixes, state-machine tweaks. These change often
        and must NOT generate per-repo file churn. They ship through the
        pixi-installed `shipit` package (see [#4]), so a new adapter is live
        without editing any consumer file.

    The seam — how "fast pops up" survives pixi.lock pinning:

        A normal pixi dependency is pinned in pixi.lock, and CI runs
        `--locked`. So a naive "shipit is a pixi dep" would make every bug fix
        a lockfile bump — another PR per consumer, defeating the point. The
        discriminator that resolves this is the required-CI-path line:

        - On the required-check / build path: pinned in pixi.lock. Reproducible
          CI is the priority; version bumps arrive as auto-PRs. (Linters, the
          rust/node toolchain, lefthook.)
        - On the agent / PR-loop path: installed OUTSIDE the locked env (pixi
          global or at SessionStart), so it auto-updates fleet-wide with zero
          file changes. (The PR state machine, review adapters.)

    The accepted cost, stated plainly:

        The auto-updating surface has no per-consumer pin to roll back to, so a
        bad release breaks that surface everywhere at once. This is acceptable
        ONLY because it is kept off the required-check path: a PR-loop tool
        failing is visible and retryable; it does not fail a check or corrupt a
        build. The moment something on that surface becomes a required check,
        it must move into the lock.

3. The pixi-task / workflow-YAML boundary

    Every piece of CI logic falls on one side of a single line:

    - Artifact-PRODUCING logic is a pixi task. Building the frontend,
      compiling, bundling, asserting the bundle, staging assets, cutting the
      changelog — each is `pixi run <task>`, runnable locally AND in CI. This
      kills the "push an RC to find out if it works" loop: the same task runs
      on a laptop (or in local Docker) before anything reaches CI.
    - Artifact-ROUTING logic stays thin workflow YAML: the build matrix,
      cross-job artifact upload/download, secret injection, the macOS keychain
      import. None of this is pixi-shaped; it is GitHub Actions orchestration.

    Consequently the reusable workflow is essentially `setup-pixi` + `pixi run
    ci`. Behavior ships via the pinned package and the pixi tasks, so the YAML
    is boring and stable — consumers upgrade by bumping one version, and there
    are no thick vendored workflow copies to drift. The detailed pipeline lives
    in [./workflows.lex].

4. The shipit package: one slim binary

    The durable logic is one small binary, `shipit`, with git-style
    subcommands:

    - install — provision + reconcile the managed files in the working
      tree (`--pr` opens the standalone reconcile draft PR).
    - gh-setup — labels, ruleset, secrets.
    - lint — run the standardized checks.
    - pr status / pr next — the PR-lifecycle state machine.
    - changelog — coalesce unreleased fragments into a version.
    - release — drive the cut.

    It is distributed as a conda/PyPI dependency from a private index. It is
    the slimmed, renamed successor to release-core's PR state machine — KEEP
    that state machine; do not rewrite it, only re-skin its entry points.

    The endgame is *one slim versioned package + thin tasks + thin callers* —
    NOT "only YAML." Real logic has to live somewhere; "no code in YAML" means
    it lives in this package, not that it evaporates.

5. Why a binary, not templated tasks

    pixi has no cross-manifest task inheritance: a consumer cannot inherit or
    override a task that shipit defines elsewhere. The only ways to put a rich
    task into a consumer are to template it into the consumer's pixi.toml
    (which makes the manifest a managed-but-edited file — drift, on the most
    important config file) or to put the logic in a binary and reference it
    from a trivial one-line task.

    shipit takes the second path. The consumer's pixi.toml carries only thin
    lines:

    Consumer pixi.toml tasks:
        lint = "shipit lint"
        test = "<consumer-supplied>"

    All behavior lives in the versioned binary; the task line is stable and
    never drifts. This is the direct reason for the binary model in [#4].

6. Configuration home: .shipit.toml

    Configuration lives in .shipit.toml, NOT pyproject.toml. Most repos in this
    portfolio are not Python (rust CLIs, a tauri app, vscode extensions, go),
    so a pyproject-hosted table would be absent in the majority; and pixi.toml
    is strict about unknown tables. A dedicated file is uniform across every
    language.

    The ownership line keeps the two config files from overlapping:

    - pixi.toml owns provisioning: environments, dependencies, the thin task
      lines.
    - .shipit.toml owns policy: the path -> toolchain map, the secret map, the
      reviewers, and the shipit-version hash + per-file pristine hashes.

    They describe different layers, so there is no split-brain.

    The secret map:

        Some secrets come from Doppler, some do not, and the GitHub secret name
        often differs from the source name (a cross-org consumer needs
        `CARGO_REGISTRY_TOKEN` where crates.io's source key is `CRATES_IO_KEY`).
        Each entry maps a source to the gh secret name (the table key):

    .shipit.toml secret map:

        [secrets]
        CARGO_REGISTRY_TOKEN = { doppler = "CRATES_IO_KEY" }
        APPLE_CERTIFICATE    = { doppler = "APPLE_CERTIFICATE" }
        GH_PAT               = { env = "SHIPIT_GH_PAT" }
        MANUAL_TOKEN         = { prompt = true }

    :: toml ::

        This same table is the single source of truth for the cross-org
        workflow caller's `secrets:` block: `secrets: inherit` only propagates
        within one owner, so cross-org consumers (lex-fmt) must list each
        secret explicitly with the mapped name. gh-setup pushes the secrets and
        the workflow caller reads the same map — they cannot diverge.

7. The commit/push checks: one definition

    There is exactly one definition of these checks, invoked everywhere. lefthook is thin:
    it calls `pixi run lint` and `pixi run test`. shipit ships the lint tasks
    and the linter dependencies; the consumer supplies `test` (the pixi-test
    encapsulation — per-project test differences hide behind one task name, so
    lefthook stays dumb).

    They are hard-fail checks: a missing tool exits non-zero, never skips. CI runs the
    SAME `pixi run` invocations as the local pre-commit hook, so "CI is the
    source of truth" never becomes a second, divergent definition of the
    checks. The lint/fmt rules are fully standardized (rust, python, shell,
    markdown, yaml, json, go, lex); only `test` is consumer-owned.

    The managed lefthook config carries one non-check hook alongside the
    checks: post-commit runs `shipit log event commit.created --from-hook`, the
    hook-witnessed tier of the dev-cycle event log (ADR-0032 — local commits
    become visible in the flow view before any push). It is the checks'
    opposite in failure posture: fail-OPEN, exit 0 on any emission failure,
    because logging must never block or slow a commit.

    Writing `lefthook.yml` is not enough — a config without `lefthook install`
    leaves the checks dormant (empty `.git/hooks`, commits sail past lint). So
    activation is part of setup, never a remembered manual step: `shipit
    install` runs `lefthook install` after laying down the caller (the consumer
    leg), and shipit-self activates via the committed SessionStart hook in
    `.claude/settings.json` (`pixi run -e lint install-hooks`), so a fresh clone
    activates the checks on first agent session. Both routes are the one `lefthook install`
    the `install-hooks` pixi task wraps; activation is idempotent and never
    clobbers a pre-existing unrelated hook.
