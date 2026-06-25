shipit

A small set of utilities and agent harnesses to standardize work across my portfolio
of personal projects (arthur-debert, lex-fmt and phos-editor on gh)

Scope

    1. Provisioning: 
      1.1 Ensure that os level dependencies are installed in a pinned version.
    1. Development Workflow: 
        1.1 The how to full for development workflow.
        1.1 The skills for development (shipt-to-PRD, shipit-to-issues, shipt-grill-with-docs)
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

  2. PR Reviews

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
    - [./docs/dev/ROADMAP.lex] — the incremental, verifying build sequence
      (Spike 0 through cutover).
    - [./AGENTS.lex] — the dev-cycle and PR-review policy agents follow.



