Shipit CLI Human Help Draft

    This is a proposed long-form help map for `shipit <command> help`.
    The implemented slice in the current PR is `shipit lab help`,
    `shipit lab run help`, and `shipit lab report help`; the remaining
    command entries below are a draft map for future expansion. The help text
    is intentionally human-oriented: each entry starts with what the command is
    for, then names the important behavior and boundaries. The terse `--help`
    surface can stay focused on syntax and options.

    Source notes:
        The tree was derived from the Click command graph rooted at
        `src/shipit/cli.py`, then cross-checked against command module
        docstrings under `src/shipit/verbs/`.

    Hidden commands:
        `shipit pr review _run` exists as an internal detached review child
        entrypoint. It should not appear in human help except as a debugging
        implementation note.

    1. shipit

        Shipit is the portfolio standardization tool. It provisions repository
        conventions, runs the standardized lint/test/build/e2e tool surfaces,
        manages isolated Trees for agent work, drives draft PRs through the
        review loop, records the dev-cycle log, and supports measured review
        experiments.

        Use `shipit --version` when you need to identify the exact running
        build. Shipit repositories pin the binary by build commit, so the build
        identity is often more useful than the package version.

        1.1. build

            `shipit build` runs the repository's declared build legs.

            It reads `.shipit.toml [toolchains]` and dispatches each selected
            leg to the real builder for that toolchain, such as cargo, go
            build, uv build, or an npm build script. Pixi provisions the
            environment; it is not treated as the builder. If `[artifacts]`
            declares build targets, the build narrows to those targets.

            Run bare `shipit build` for every build leg. Pass a leg name or
            path to build one leg. Arguments after `--` are forwarded to the
            selected builder and require exactly one selected leg.

            `--version VERSION` supplies the release version for build targets
            that explicitly declare version injection.

        1.2. changelog

            `shipit changelog` owns release-note fragments and the rendered
            `CHANGELOG.md`.

            Feature and fix PRs add `CHANGELOG/unreleased-*.md` fragments.
            `CHANGELOG.md` is generated from those fragments and should not be
            hand-edited as the source of truth.

            1.2.1. check

                `shipit changelog check` verifies that `CHANGELOG.md` matches
                a fresh render of the fragments.

                Use it in PR checks and locally before pushing. It catches both
                missing renders after adding a fragment and hand-edits to
                `CHANGELOG.md` that do not have a fragment.

            1.2.2. render

                `shipit changelog render` regenerates `CHANGELOG.md` from the
                fragment directory.

                This is the normal fix after `changelog check` reports that
                the committed changelog projection is stale.

            1.2.3. coalesce

                `shipit changelog coalesce VERSION` cuts release notes for a
                supplied version.

                A prerelease version prints or writes the coalesced notes
                without consuming the fragments. A final version moves the
                fragments into `CHANGELOG/VERSION.md` and re-renders
                `CHANGELOG.md`. The version is supplied by the caller; this
                command does not infer bumps.

        1.3. ci

            `shipit ci` is the CI routing surface for repository lanes.

            It exists so GitHub Actions can ask shipit which standardized
            checks should run for the current event, instead of each workflow
            reimplementing path and lane policy.

            1.3.1. plan

                `shipit ci plan` emits the GitHub Actions job matrix for the
                repository's `[lanes]`.

                A workflow plan job captures the JSON and fans out into
                `pixi run <run>` jobs. Pull-request events can narrow work by
                base branch diff; non-PR events force full scope.

        1.4. e2e

            `shipit e2e` runs declared end-to-end harnesses for built
            artifacts.

            It reads `[artifacts.<name>.e2e]`, builds or resolves the artifact
            binary, injects its absolute path into the harness as
            `<NAME>_BIN`, and runs the harness from the repo root. A repo with
            no e2e declarations has no e2e lane and exits cleanly.

            Run bare `shipit e2e` for every declared harness. Pass an artifact
            name to run one. Arguments after `--` are forwarded to that one
            harness.

        1.5. eval

            `shipit eval` reads local objective-evaluation data.

            It is the reporting and adjudication side of the agent harness.
            Terminal hooks write never-committed JSONL stores; eval commands
            aggregate those stores, score review-round records against the
            ground-truth fixture, and bank human adjudication back into that
            fixture.

            1.5.1. report

                `shipit eval report` summarizes the local objective-eval store
                by role, variant, and time.

                Use it to inspect what agent runs produced locally. It is a
                read-only aggregation over the harness JSONL store.

            1.5.2. score

                `shipit eval score` scores banked review-round records against
                the in-repo ground-truth fixture.

                It reports recall, false positives, unadjudicated emissions,
                and near-misses per variant. Scoring is deterministic and
                token-free, so it is safe to rerun while developing review
                strategy.

            1.5.3. bank

                `shipit eval bank` records a human adjudication into the
                ground-truth fixture.

                This is the write side of review evaluation. Use it only after
                deciding whether a finding is a real label or a recognized
                alias for an existing label.

                1.5.3.1. label

                    `shipit eval bank label` adds a new adjudicated label to
                    the fixture.

                    A label names the pinned PR range, file and optional line
                    range, severity, verdict, claim, and provenance. This grows
                    the benchmark when a reviewed emission identifies a real
                    issue not already represented.

                1.5.3.2. alias

                    `shipit eval bank alias` adds an accepted near-miss phrasing
                    to an existing label.

                    Use it when an emitted finding is semantically the same
                    issue as a fixture label but uses different wording. Future
                    scoring can then recognize that phrasing without another
                    human decision.

        1.6. fleet

            `shipit fleet` verifies a candidate shipit build across the
            declared portfolio.

            It is for adoption and release confidence: instead of proving a
            change only in the shipit repo, it runs shipped tool verbs across
            the configured portfolio and reports the per-repo, per-tool matrix.

            1.6.1. sweep

                `shipit fleet sweep` runs applicable tool verbs against each
                portfolio repo in fresh Trees.

                Applicability comes from each target repo's own declarations:
                lint always applies, test and build apply where toolchains
                declare legs, e2e applies where artifacts declare harnesses,
                and changelog check applies where changelog fragments exist.

                The output is the evidence matrix: pass, fail, not-applicable,
                or expected-fail, with command and raw output for red cells.

        1.7. gh-setup

            `shipit gh-setup` makes a GitHub repository conform to portfolio
            standards.

            It applies and verifies repository rulesets, labels, secrets, and
            required checks from `.shipit.toml` or explicit options. The command
            is idempotent, so it is suitable for first setup and later drift
            repair.

        1.8. hook

            `shipit hook` is the agent harness entrypoint called by lifecycle
            hooks.

            Humans usually do not run these commands directly. They are the
            binary side of `.claude/settings.json`, translating hook payloads
            into allow/deny decisions, activation, Tree provisioning, liveness
            records, and cleanup.

            1.8.1. pretooluse

                `shipit hook pretooluse` evaluates a tool call before it runs.

                Its main human meaning is the coordinator guard: coordinator
                sessions are denied direct code edits, while role-scoped
                subagents may edit according to their role. Malformed input
                fails open so a broken hook does not strand the user.

            1.8.2. stop

                `shipit hook stop` evaluates a coordinator run at terminal
                Stop.

                It records and checks end-of-run harness expectations without
                blocking the host; failures are fail-open.

            1.8.3. subagent-stop

                `shipit hook subagent-stop` evaluates a subagent run at
                terminal SubagentStop.

                It is the subagent counterpart to `stop`, used for role-run
                accounting and harness checks.

            1.8.4. sessionstart

                `shipit hook sessionstart` activates repository tooling and
                records coordinator liveness.

                It writes activation into `CLAUDE_ENV_FILE`, exports log
                context, writes the session pidfile, emits a session event, and
                warns when the session appears to be running in a source clone
                instead of an isolated Tree. Each step fails open
                independently.

            1.8.5. worktreecreate

                `shipit hook worktreecreate` provisions an isolated Tree for a
                host WorktreeCreate request.

                It serves top-level coordinator launches and in-host subagent
                launches. On success it prints the Tree path for the host to
                adopt as cwd. It fails closed so a provisioning failure never
                falls back to a native git worktree.

            1.8.6. worktreeremove

                `shipit hook worktreeremove` reclaims a clean ephemeral session
                Tree on session exit.

                Cleanup is best effort and conservative. It removes only an
                ephemeral Tree under the central root with no local-only work;
                the garbage-collection ladder remains the load-bearing cleanup
                path.

        1.9. install

            `shipit install` vendors and reconciles shipit's managed set into a
            consumer repository.

            The managed set includes the launcher, hooks, workflow blocks,
            standard configs, role prompts, and other files shipit owns. By
            default the command refreshes the working tree and stops: no branch,
            no commit, no push, no PR. That makes it safe inside an existing
            workstream.

            `--pr` is the standalone reconcile flow: create or reuse the
            `shipit/install` branch and open a draft PR. `--local` commits on
            the current branch without pushing, which Tree provisioning uses.
            `--push` is an administrative break-glass path.

        1.10. lab

            `shipit lab` runs measured experiments on code-review strategies.

            The lab answers questions such as "does this review configuration
            find more real issues at the same budget?" A declarative cell
            defines one experimental axis and its baseline. The lab replays
            pinned PR ranges offline, banks review-round records, and reports
            convergence curves for recall, precision, token cost, and latency.

            The product surface remains `shipit pr review`; the lab is how
            changes to that review strategy earn promotion.

            1.10.1. run

                `shipit lab run CELL` executes one experiment cell over the
                offline replay driver.

                A cell is either an id under `lab/cells/` or a path to a cell
                file. Each fixture PR, replicate, and sweep point is keyed
                idempotently. Existing banked points are reused, not re-paid;
                `--force` is the explicit rerun path. `--checkout` supplies
                local clones that already contain the fixture-pinned commits.

            1.10.2. report

                `shipit lab report CELL` renders a cell's convergence curve
                from banked review-round records.

                It compares the cell against its baseline at equal budget and
                reports cumulative major-or-worse recall, false positives or
                precision, token cost, and latency per sweep point. Reporting
                is deterministic and token-free.

        1.11. lint

            `shipit lint` runs the standardized multi-language checks.

            This is the same hard-fail check surface used by CI and
            pre-commit. It does not silently skip missing tools. By default it
            checks only; `--fix` opts into formatters that edit files in place.

        1.12. log

            `shipit log` is the constrained write path for dev-cycle events.

            It records milestones into the durable per-repo JSONL log using a
            closed vocabulary. It is not a diary or arbitrary note-taking
            interface.

            1.12.1. event

                `shipit log event NAME` records one registered dev-cycle event.

                Domain keys are picked up from exported `SHIPIT_LOG_CTX_*`
                values and from the current branch where possible. Unknown
                event names are errors, preserving the log as structured
                process evidence.

        1.13. logs

            `shipit logs` locates and reads shipit's durable per-repo JSONL log.

            Use it to answer "what happened in this session, PR, epic, work
            stream, agent, or review round?" The default view prints the log
            path and recent rendered records. `--path` prints only the file
            path. `--raw` is for piping JSONL to tools. `--flow` renders a
            session story from event records.

            Filters such as `--session`, `--pr`, `--epic`, `--ws`, `--agent`,
            `--role`, `--reviewer`, `--run`, and `--round` compose as AND.

        1.14. pr

            `shipit pr` drives a draft pull request through review, CI, and the
            guarded ready flip.

            The PR engine is authoritative. It reports the current lifecycle
            state and the single next action. Humans and agents should use the
            state machine rather than re-deriving reviewer, CI, and mergeability
            policy by hand.

            1.14.1. status

                `shipit pr status [PR]` reports where the PR stands and what
                the next action is.

                It is read-only. If `PR` is omitted, shipit resolves the
                current branch's PR. A branch with no PR is a normal reported
                state, not a crash.

            1.14.2. review

                `shipit pr review` contains reviewer actions.

                Live PR work usually uses `request`; offline review experiments
                and replays use `replay`.

                1.14.2.1. request

                    `shipit pr review request [PR]` requests or re-requests
                    required reviewers and verifies that each request attached.

                    With no reviewer flag, it requests the required set that is
                    still pending on the current head. `--reviewer NAME` forces
                    one adapter.

                1.14.2.2. replay

                    `shipit pr review replay RANGE` reviews a commit range
                    offline and writes a review-round record.

                    Use `<base>..<head>` to review exactly that diff or
                    `<base>...<head>` to review from merge base. Nothing is
                    posted to GitHub. `--fanout` runs the configured dimension
                    passes and is the sanctioned offline driver for review-lab
                    experiment cells.

            1.14.3. next

                `shipit pr next [PR]` performs at most one engine-selected
                action, then reports the result.

                It may request reviews, report waiting or blocked state, or
                perform the guarded ready flip when the PR is actually ready.
                It is not a polling loop; use `pr wait` when blocking is
                desired.

            1.14.4. ready

                `shipit pr ready [PR]` flips a draft PR to ready only when the
                engine says the PR is ready.

                The ready pillars are reviewed, CI green, and mergeable. If any
                pillar is missing, the command refuses and reports the real
                state. `--undo` sends a ready PR back to draft and is always
                permitted.

            1.14.5. classify

                `shipit pr classify [PR]` lists finding severities or records a
                severity override for one finding.

                With no override flags it shows the latest round's findings,
                resolved severity, and source rung. With `--comment ID
                SEVERITY`, it records a write-once override in the dev-cycle
                log. Re-overriding the same finding is an error.

            1.14.6. wait

                `shipit pr wait [PR] --until STATE` blocks until the PR reaches
                a review-loop state.

                This is the one blocking PR verb. `--until reviews-in` waits
                until the latest requested reviews have landed. `--until ready`
                waits until the engine reports READY, but exits early when it
                observes `addressing`, because that state requires the caller to
                act. `--timeout` sets a hard deadline.

        1.15. provision

            `shipit provision` installs pinned external tools into the active
            pixi environment.

            It is for required-check tools that cannot ride conda-forge and
            therefore cannot be represented directly in `pixi.lock`. Each
            provisioned tool is pinned by shipit, checksum-verified, and
            installed idempotently into the invoking environment prefix.

            1.15.1. lexd

                `shipit provision lexd` installs the pinned `lexd` binary in
                the active pixi environment.

                It is a no-op when the pinned binary is already present.

        1.16. repo

            `shipit repo` groups the commands that create shipit-managed
            repositories.

            It is the creation side of adoption: where `shipit install`
            reconciles the managed set into an existing repository, `shipit
            repo` brings a new one into being with that baseline already in
            place and certified.

            1.16.1. new

                `shipit repo new --stack rust <name> [parent]` creates a new
                local Repo with a complete, verified, shipit-managed baseline.

                The destination is always `<parent>/<name>`; the command never
                guesses whether the positional path is a parent or an exact
                destination. `parent` defaults to the current directory and
                must already exist as a writable directory; the destination
                must be absent or an empty directory. `--stack` selects a
                Creation profile and is repeatable so the request can later
                describe a multi-toolchain Repo; at least one selection is
                mandatory, and v1 supports a single profile, `rust`.

                Creation scaffolds the consumer-owned project (a two-crate
                Cargo workspace for Rust), applies the managed baseline,
                resolves the pixi lockfile, and runs the lint, test, and build
                Checks. It stages the whole Repo in a sibling directory and
                publishes it with one atomic rename only after every Check
                passes, yielding a single initial commit on `main`; any failure
                removes the staging directory and leaves the destination
                untouched. Creation is local only — it creates no GitHub
                repository, remote, publishing endpoint, or release policy.

        1.17. session

            `shipit session` launches isolated, Tree-rooted coordinator
            sessions.

            It exists for hosts that need shipit to provision the top-level
            session Tree explicitly before launching the agent UI.

            1.17.1. codex

                `shipit session codex` launches an interactive Codex
                coordinator session in a fresh ephemeral Tree.

                It creates a recognizable session id, provisions an
                `ephemeral/<id>` Tree from `origin/main`, then replaces the
                process with `codex --cd <tree>`. Extra arguments are forwarded
                to Codex. On success, this command does not return.

        1.18. spawn

            `shipit spawn` launches backend-agent runs that shipit owns end to
            end.

            A spawn creates or selects the correct Tree, starts the backend
            agent rooted there, and verifies the expected reporting contract.
            Coordinators use this surface rather than hand-provisioning Trees
            or starting agents in shared checkouts.

            1.18.1. subagent

                `shipit spawn subagent` creates a Tree and launches a
                role-scoped backend-agent run.

                Standalone issue work uses `--issue N` and targets `main`.
                Epic work uses `--epic E --ws N` and targets the epic umbrella
                branch. The role controls the agent prompt and hook posture.
                Write roles must report through an open draft PR on the
                expected branch and base; spawn fails loudly if that contract is
                not met.

            1.18.2. brief

                `shipit spawn brief ROLE` prints the task-specific brief
                template for a role.

                Use it before cold-briefing an implementer or shepherd. Every
                placeholder slot is meant to be filled with concrete issue,
                verification, governing-doc, and decision-boundary context.

        1.19. test

            `shipit test` runs the repository's declared test legs.

            It reads `.shipit.toml [toolchains]` and dispatches each selected
            leg to its test command. Bare `shipit test` runs all legs, matching
            hooks and CI. A leg name or path selects one leg. Arguments after
            `--` are forwarded to that one leg.

            Missing test tools fail the check; they are not skipped.

        1.20. tree

            `shipit tree` manages isolated Trees, the independent clones where
            write sessions work.

            A Tree is disposable and isolated from the source checkout. It is
            how concurrent agents avoid colliding in one working tree.

            1.20.1. create

                `shipit tree create` provisions an isolated Tree and prints its
                READY summary.

                Exactly one shape is accepted. `--issue N` creates
                `issues/<n>/<session>` from `origin/main`. `--epic E --ws N`
                creates `E/WSnn` from `origin/E/umbrella`. `--branch NAME`
                creates that branch from `origin/main`. `--slug` affects only
                the directory name, not the branch.

            1.20.2. list

                `shipit tree list` scans the central root and lists every Tree.

                It reports path, branch, base, age, dirty state, and PR state
                from what the clones on disk say now. There is no separate
                manifest to trust.

            1.20.3. remove

                `shipit tree remove TARGET` deletes one Tree by path or
                directory name.

                The command refuses unknown or ambiguous targets. If the Tree
                still contains uncommitted changes or unpushed commits, removal
                is gated behind confirmation; `--yes` skips the prompt.

            1.20.4. gc

                `shipit tree gc` conservatively cleans the central Tree root.

                It deletes only Trees that are merged, clean, pushed, and older
                than the threshold. Stale or ambiguous Trees are listed, not
                removed. `--dry-run` shows the same partition without deleting
                anything.

        1.21. verify-apps

            `shipit verify-apps` checks whether local-agent reviewer GitHub
            Apps are live on a repo.

            It mints each App installation token and verifies the installation
            has `checks: write`. It does not create check runs or install the
            Apps. Use it to confirm that reviewer integrations are ready before
            relying on them in PR flow.

        1.22. wf

            `shipit wf` validates GitHub Actions workflow edits locally.

            It is a local pre-push confidence tool around `act`, not a complete
            replacement for a real GitHub Actions run.

            1.22.1. test

                `shipit wf test WORKFLOW` runs a workflow under `act` in
                shipit's stock Ubuntu container image.

                It can run the whole workflow or one job, using a crafted push,
                pull_request, or workflow_dispatch event. Each run reports the
                surface that `act` cannot test and therefore still requires a
                real push.
