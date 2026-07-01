shipit

A small set of utilities and agent harnesses to standardize work across my portfolio
of personal projects (arthur-debert, lex-fmt and phos-editor on gh)

Scope

    1. Provisioning: 
      1.1 Ensure that os level dependencies are installed in a pinned version.
    1. Development Workflow: 
        1.1 The how to full for development workflow.
        1.1 The skills for development (shipit-planning, shipit-grill-with-docs, shipit-to-prd, shipit-to-issues)
      2.  Standarized Linting and Formatting via LeftHook
        2.1 Multi language / file types supported (rust, python, shell, markdown, yaml, json, go, lex)
      3. Github Repo Setup: 
        3.1. Ensure used issues labels are available
        3.2 Ensures a standarized ruleset for branches 
        3.3. Ensures repo secrets are available (fetched from local doppler config)
      4. Tooling: 
        4.1 Leverages pixi to offer pixi build, lint, test, and run commands for all supported languages.
      5. PR and Code Reviews
        A significant part of the value is the connection between skills, tooling, and review helpers that take the guesswork out of agents on how to handle it. 

  How It  Works

    1. Install : $ shipit install <path>

      1.0 Provisions the project (pixi installs the pinned toolchain).
      1.1. Runs the script that sets up the gh repo.
      1.2 Copies the skills.
      1.3. Copies the lefthook config.
      1.4. Stores a .shipit.toml file recording the shipit version (commit hash) that installed it and the per-file pristine hashes used for reconciliation.
      1.5 Sets up git commit hooks to run lefthook on pre-commit and pre-push.
      1.6 Adds to AGENTS.md a section on how the development workflow works (AGENTS.lex) and a short pixi command reference for shipit commands.

      This is the same command for fresh installs and updates.

      By default install does NOT touch the consumer's main branch: it
      stages the changes on a branch and opens a PR for a human to merge.
      shipit eats its own dog food — changes reach a consumer the same way
      the consumer ships code.

    Reconciliation (the slow/fast split):

        shipit splits what it manages by how often it changes.

        - Slow + file-structure-dependent (the bootstrap, the lefthook
          caller, the skills, the AGENTS.md block) is committed into the
          consumer. On re-install, shipit hash-compares each managed file
          against the pristine hash stored in .shipit.toml at the last
          install. Unchanged files are overwritten silently; a
          consumer-edited file is surfaced in the PR (showing the override),
          never clobbered and never admin-pushed.
        - Fast-changing code (PR-review adapters, bug fixes) ships through
          the pixi-installed `shipit` package, so it lands without per-repo
          file churn.

        See [./docs/dev/architecture.lex] for why this split holds and how
        it interacts with pixi.lock pinning.

    The `--push` flag is a break-glass escape hatch: it pushes straight to
    the repo's main, bypassing the PR workflow via ADMIN. It is not the
    default and should be reserved for bootstrapping a repo that cannot yet
    run the PR loop.

    Doppler-sourced secrets are re-applied on every install (a changed
    secret is set to its new value, which is the desired behavior).

    Lint — the standardized checks:

        `shipit lint` runs one multi-language checkset (python, shell, yaml,
        json, markdown and lex today; rust, go and more as consumers bring
        them) over the tracked tree. The SAME invocation runs in CI and in
        the lefthook pre-commit / pre-push hooks — one binary, one config,
        so they cannot drift into two definitions. It is a hard-fail check: a
        missing tool fails the run, it never skips. `shipit lint --fix` is
        the opt-in formatter pass; the bare check never mutates files.

        The orchestration lives in the binary, not in lefthook or a
        templated pixi task: pixi has no cross-manifest task inheritance,
        so lefthook and pixi stay thin callers (`pixi run lint`) while the
        per-language discovery and routing live in `shipit lint`. The
        linters themselves ride in as shipit's own pinned dependencies, so
        a consumer's pixi.toml carries only the stable `lint = "shipit
        lint"` task line, never a drifting list of tool pins. See
        [./docs/dev/architecture.lex] §5 and §7.

    Trees — isolated per-agent checkouts:

        `shipit tree` provisions and manages the isolated working trees agents
        work in. A Tree is a fully-independent clone of the repo under a central
        root outside any checkout (`~/workspace/trees/<org>/<repo>/…`), so
        concurrent agents (and the human) never collide on one shared working
        tree. It is a real clone, NOT a `git worktree` — the native
        `git worktree` path is denied so agents cannot drift back to the
        old `.claude/worktrees` mess (ADR-0014).

        The surface is four verbs:

        - `shipit tree create` provisions a ready Tree — its own clone, on a
          fresh branch, deps installed, gitignored-but-needed files copied in —
          then prints a READY summary. It takes exactly one of three shapes:
          `--issue N [--session S]` (branch `issues/<n>/<session>`, session
          default `work`, cut from `origin/main`), `--epic E --ws N` (branch
          `E/WSnn`, cut from `origin/E/umbrella`), or `--branch NAME` (verbatim,
          cut from `origin/main`).
        - `shipit tree list` renders the whole fleet — path, branch, base, age,
          dirty?, PR state — derived purely by scanning the central root (no
          manifest). A Tree whose PR state cannot be read shows `UNKNOWN`,
          distinct from a Tree with no PR.
        - `shipit tree remove <target>` deletes one Tree by path or directory
          name. A clean, fully-pushed Tree is a disposable clone, so it is
          removed without a prompt; but when the delete would discard work
          living ONLY in that clone — uncommitted changes or unpushed commits —
          it is gated behind a confirmation. `--yes`/`-y` skips that prompt;
          without a TTY and without `--yes` a risky remove is refused rather
          than silently destroying work — so a non-interactive caller must
          pass `--yes` explicitly to remove such a Tree.
        - `shipit tree gc` sweeps the fleet conservatively — it removes only
          Trees whose PR is merged, working tree clean, nothing unpushed, and
          aged past a threshold; ambiguous ones are listed as stale, never
          auto-removed. `--dry-run` previews the exact removable/stale/keep
          partition the real sweep would act on and deletes nothing;
          `--threshold <duration>` (e.g. `14d`, `36h`) overrides the default
          14-day age boundary. A Tree whose PR state is `UNKNOWN` is treated as
          stale (never auto-removed), and the sweep reports
          `swept N of M; K skipped (state unknown)` whenever any was seen.

        See [./docs/prd/where-to-do-work.md] for the full design, and
        [./docs/adr/0014-trees-dissociated-clones-central-root.md] +
        [./docs/adr/0015-tree-artifacts-per-tree-target-sccache.md] for the
        clone-over-worktree and per-Tree-cache rationale.

  2. PR Reviews

    Draft → shepherd → ready, then stop:

        Every change ships as a PR the agent drives. Open it as a DRAFT,
        then shepherd the whole loop while it stays draft — request and
        address reviews, get CI green, make it mergeable. Flipping
        draft → ready is the ONE signal that means "done iterating; a
        human can validate and merge", so it happens only when all three
        hold: reviews addressed, CI green, mergeable.

        The agent stops at that flip — it does NOT merge. But the FLOOR is
        the agent's own: committing, pushing, and opening the draft PR need
        no go-ahead — "stop at the ready flip" never means "wait to be
        asked to commit" or leave finished work uncommitted. The CEILING —
        the ONE step needing a human — is the merge: the human does the final
        read and merge unless they say otherwise. A human request for
        changes sends the PR back to draft and the loop repeats. The
        per-reviewer re-review and review-break rules are in [./AGENTS.lex].

    2.1 pixil shipit-request-review <pr_number> <reviewer>....

      2.1.1 This pixi extension will look into the projects .shipit.toml file to read which reviewers are used in the project , for now we have copilot, agy-local, and codex-local. 
      2.1.2 If no reviewers are specified, it will use the default reviewers in the .shipit.toml file.

    2.2. pixi shipit-pr-status

      Returns a json summary of the PR status according to our developement workflow: with fields for reviews_complete (each can be complete ,  pending, addressing needed or not needed), ci cheks (status and if failed, which ones), mergeable. 

      2.2 pixi shipit-pr-next-action

      A state machine that encodes the rules for the workflow development and runs the next action (request or re requrest reviews, check for ready status, if can be ready, flips the PR from draft to ready, etc).  Returns a useful json summary of the action taken and current status. Implemented on top of shipit-pr-status and shipit-request-review.

  See Also

    - [./docs/dev/architecture.lex] — the load-bearing design decisions and
      their rationale (pixi as substrate, the slow/fast split, the
      pixi-task / workflow-YAML boundary, the .shipit.toml config).
    - [./docs/dev/workflows.lex] — the composable CI design (the decomposed
      build → package → sign → release pipeline and its invariants).
    - [./docs/prd/FUTURE_WORK.md] — the high-level map of shipped + planned
      work (the retired roadmap's successor).
    - [./AGENTS.lex] — the dev-cycle and PR-review policy agents follow.



