pixi: the substrate — facts, contract, and gotchas

This is shipit's working knowledge of pixi: what pixi actually persists, what
identifiers it does and does not give us, what extension surface exists, and how
shipit's spawn/Tree/eval machinery is wired through it. It is a REFERENCE so no
agent has to rediscover pixi's model from scratch — that discovery is expensive
and the answers drift between releases.

Every claim here was verified by reading the pixi CLI surface, the on-disk
`.pixi/` tree, and shipit's own integration code — NOT from model memory, which
is stale for a fast-moving tool. It complements [./architecture.lex] section 1
("pixi as the substrate"), which records WHY pixi was chosen; this records HOW it
behaves and how we ride it.

:: note :: Facts verified against pixi 0.71.0 (the pinned version) on 2026-06-30. Upstream latest is 0.71.3 (2026-06-30); 0.71.1–0.71.3 are packaging/fix-only releases that do NOT touch run identity, logging, JSON output, the plugin/extension surface, activation, or the task-cache model — so bumping the pin re-verifies as a near-no-op. The launch bypass documented in [#7] was FIXED by PR #197 for write Trees; the section is kept as the record of the bug and marks what remains. pixi moves fast — when the pin bumps, re-run the checks in [#9] and update affected facts before trusting them.

1. What pixi is to shipit

    pixi is used for four things at once: provisioning native tooling, running
    tasks, defining per-purpose environments, and integrating with CI. shipit is
    a THIN layer on top — it must build ON pixi, not reinvent it.

    What pixi is the parent of:

        Provisioning:
            `pixi install` materialises a Tree's environment.

        The PreToolUse hook guard:
            The one Claude Code hook still fired as a manifest-pinned
            `pixi run` (#529). The other managed hooks ride the pinned
            launcher, `./bin/shipit hook <name>` (#491) — pixi provisions the
            env they resolve against but is not their parent process; see
            [#6].

        The write-Run agent session:
            As of PR #197, `shipit spawn` re-expresses the backend argv as
            `pixi run --manifest-path <tree>/pixi.toml -- <argv>` for a
            provisioned write Tree, so the agent runs INSIDE its Tree's env
            (see [#6], [#7]).

    What pixi is NOT the parent of:

        The reviewer (read-only) agent session. A reviewer Tree is clone+checkout
        with the working tree `chmod`'d read-only and NO `.pixi/envs/default`
        (`src/shipit/tree/readonly.py`), so routing it through `pixi run` would
        force a solve into a read-only dir. Its launch argv stays bare — no
        `pixi run` wrapping (though the spawn itself is still an Exec through the
        one runner, ADR-0028) — tolerable because a reviewer only runs
        `gh pr diff`/`gh pr review` off the ambient PATH, not the Tree's
        toolchain (see [#7]).

    What pixi does NOT own (carried from [./architecture.lex]): building and
    signing distributable artifacts. `pixi build` is preview-grade and emits
    conda packages. shipit keeps the real builders and uses pixi to PROVISION and
    RUN them.

    What Work Env adds above pixi:

        Work Env is shipit's resolved WHERE/ACTIVATION value over existing
        owners. It may carry pixi's `Activation` or `EnvIdentity`, but it never
        computes PATH, shells out to activate, invents an environment id, or
        becomes a runner. The routing decision says which existing mechanism the
        caller uses. `pixi-run` routes provisioned write Trees, CI Lane jobs,
        and provisioned fleet-sweep cells.

        `activation-snapshot` routes coordinator session Trees that borrowed
        `pixi shell-hook --json`.

        `ambient` routes reviewers, explorers, Main checkouts without a supplied
        activation, and non-pixi Trees.

        Pixi owns activation and environment identity. Exec remains the process
        seam. Work Env exists so spawn, session, review, CI, and fleet evidence
        all use the same vocabulary without sharing one universal executor.

2. What pixi persists — the data model

    pixi exposes rich STATIC environment metadata but almost no DYNAMIC run
    state. Environments are keyed by NAME (`default`, `lint`, `review`,
    `dogfood`), never by a UUID.

    The on-disk `.pixi/` tree (gitignored except `config.toml`):

        - `.pixi/envs/<env>/` — the materialised conda prefix per environment.
        - `.pixi/envs/<env>/conda-meta/.pixi-environment-fingerprint` — a short
          hex digest (e.g. `99b739d0fedb92eb`) tying a materialised prefix to its
          resolved lock. Present + matching = "this env is provisioned and
          consistent with the lock". It is a SYNC-STATE digest, not a stable
          identity: it changes when the lock changes.
        - `.pixi/envs/<env>/conda-meta/pixi` — a RICHER env-identity record (JSON)
          the bare fingerprint does not give you: `manifest_path`,
          `environment_name`, `pixi_version`, `environment_lock_file_hash`,
          `resolved_platform`, `minimum_supported_platform`. This is the natural
          thing to read to answer "which manifest / env / lock / pixi-version
          materialised this prefix?". NOTE its `environment_lock_file_hash` is a
          DIFFERENT digest from the fingerprint (observed `99f00798db0ea80c` vs
          the fingerprint's `99b739d0fedb92eb`) — do not conflate them. A sibling
          `conda-meta/pixi_env_prefix` points back at the prefix.
        - `.pixi/envs/<env>/conda-meta/<pkg>-<ver>-<build>.json` — one manifest
          per installed package. `conda-meta/history` is a stub.
        - `.pixi/task-cache-v0/<env>-<task>-<hash>.json` — `{"hash": "..."}`, the
          key pixi's skip-if-unchanged cache writes. It is invalidated on THREE
          conditions: env-package state changed, the task's declared
          `inputs`/`outputs` file fingerprints changed, or the command string
          changed. It records THAT a task ran with a given input hash — not when,
          nor its exit code, nor a run id. Only `pixi run <task>` writes these
          (and only for tasks that declare `inputs`/`outputs`); `pixi install`
          does not. shipit declares no `inputs`/`outputs`, so this cache is
          currently idle (see [#8]).

    `pixi.lock` (version 7) is the stable description of "this exact resolved
    environment": per-environment, per-platform, fully-resolved packages each
    with `sha256`/`md5`. There is NO top-level `content-hash` field (unlike
    Poetry). The closest thing to a per-env identity is `conda-meta/pixi`'s
    `environment_lock_file_hash`, the pair `(env name,
    .pixi-environment-fingerprint)`, or hashing the env's slice of `pixi.lock`
    yourself.

    What pixi does NOT persist:

        - No on-disk logs anywhere (`pixi info` reports no log dir; diagnostics
          go to stderr only).
        - No record of a given install/run attempt: no exit code, no timestamp,
          no duration, no run id. The fingerprint tells you the END STATE is
          consistent; it cannot tell you a specific `pixi install` exited 0.

3. Identifiers and correlation

    pixi assigns NO usable correlation key:

        - No `pixi run` invocation id (no flag, no output, no log).
        - No environment UUID — envs are keyed by name; the digests
          (`.pixi-environment-fingerprint` and `conda-meta/pixi`'s
          `environment_lock_file_hash`, which differ — see [#2]) are SYNC-STATE,
          not stable identity.
        - No install id.

    Therefore the only stable per-run identifier is Claude Code's own
    `session_id`, surfaced as the transcript filename (the coordinator run is
    `<session_id>.jsonl`; a subagent run is `agent-<id>.jsonl`). The eval spine
    already keys on it via `src/shipit/harness/eval/locate.py`. Any
    "integrated view" must join on shipit-owned keys (Tree path, branch, env
    fingerprint) plus this `session_id` — pixi offers nothing to join on. The
    agent's `--output-format json` envelope also carries a `session_id`; the
    spawn verb does not yet parse it from `LaunchResult.stdout` — that is the
    natural bridge point if a spawned Run's id must be learned by the parent.

4. The CLI surface that matters

    `pixi shell-hook`, `pixi list`, and `pixi info` all have machine-readable
    (`--json`) output; `pixi run` and `pixi install` do not. Verbosity flags
    (`-v`..`-vvvv`, `-q`) control human stderr text, not structured output. All
    three JSON reads are wrapped as structured adapter reads in
    `shipit.pixienv.read` (`shell_hook` / `list_packages` / `info`, PROC02-WS02),
    and the execution side (`install`, run-wrapping) lives beside them in
    `shipit.pixienv.run`.

    Commands shipit relies on or could use:

        - `pixi install` — materialise the env from the lock (what provisioning
          runs; default env when no `-e`). Human stderr only.
        - `pixi run [-e env] <cmd|task>` — activate the env, then exec. The ONLY
          way (besides `shell`/sourcing `shell-hook`) that pixi activation
          happens. No run id, no JSON. Useful flags: `--dry-run`/`-n` (print the
          exact command pixi would exec — a cheap way to learn the resolved
          argv), `--skip-deps`, `-x/--executable`, and the experimental
          activation cache (`--use-environment-activation-cache` / `--force-activate`,
          see [#7]).
        - `pixi info --json` — env metadata: `name`, `prefix`, `platform`,
          `dependencies`, `tasks`, `channels`, cache dirs. No hashes/ids.
        - `pixi list [-e env] --json` — installed packages (per-package
          `sha256`/`md5`/`timestamp`). A cheap "does this env resolve cleanly"
          health check.
        - `pixi shell-hook [--json]` — PRINT the activation script (PATH munge +
          env) so a non-pixi process can `source` it. The bridge for activating a
          Tree env outside `pixi run` (see [#7]).
        - `pixi exec` — run a command in an ephemeral throwaway env (not a
          workspace env). Not relevant to Tree runs.
        - `pixi workspace <channel|platform|version|environment|feature|export|...>`
          — structured manifest edits, scriptable instead of hand-editing TOML.

5. Extension surface — there essentially is none

    pixi offers NO plugin API, NO backend SPI, NO event/config hook a tool can
    live inside. Do NOT design a "pixi plugin". The integration is: declare
    behaviour in the manifest's lifecycle slots, and route execution THROUGH
    `pixi run` so those slots fire.

    The only real integration points:

        `[activation]` in pixi.toml:
            The one place to inject env/scripts pixi runs on EVERY activation —
            `[activation] scripts = [...]` and `[activation.env] KEY = "val"`, per
            feature/environment. Fires on every `pixi run`/`pixi shell`/
            `shell-hook`. Shipit now declares build environment values such as
            `CARGO_TARGET_DIR`, `SCCACHE_BASEDIRS`, and `CARGO_INCREMENTAL` in
            `[activation.env]`; `activation_scripts` may still be empty. This is
            where shipit-owned env belongs — but it only fires when execution goes
            through pixi or borrows pixi's shell-hook snapshot.

        Task fields (`depends-on`, `inputs`/`outputs`, `args`, `env`, `cwd`, `clean-env`):
            `depends-on` is pre-task chaining only (`pixi task add --depends-on`,
            plus `--env`, `--cwd`, `--clean-env`, `--args`) — there is NO native
            post-task or wrapper hook, so shipit behaviour can hang off the FRONT
            of a task, not wrap it. `inputs`/`outputs` are glob lists that drive
            the skip-if-unchanged cache ([#2]); `args` declares named args with
            defaults/validation; both support MiniJinja templating. An
            `[environments]` entry can set a `default-environment` for a task.

        External `pixi-<name>` subcommands:
            git/cargo-style dispatch — `pixi foo` execs a `pixi-foo` on PATH. This
            is dispatch convenience only: an external subcommand gets NO pixi
            internals, env callbacks, or activation injection. It would let `pixi
            shipit ...` work; it would not let shipit's concerns run inside pixi's
            lifecycle. (Note: `pixi --list` prints "Installed Commands:" = the full
            built-in command table, NOT an extensions-only view — it does not
            enumerate `pixi-<name>` externals.)

6. The shipit ↔ pixi contract today

    Layer 0 — `bin/setup-dev-env.sh` (managed unit, #547):
        pixi itself (and uv, which the ADR-0033 pinned `bin/shipit` launcher
        rides) arrives via the managed bootstrap `bin/setup-dev-env.sh`:
        reconcile-to-pin from sha256-verified GitHub release tarballs into
        `~/.local/bin` (the pin kept in lockstep with CI's `pixi-version`),
        then a best-effort `pixi install --locked` pre-solve. It runs from the
        managed SessionStart hook ahead of `shipit hook sessionstart` —
        fail-open, loud, idempotent. Everything else in this document assumes
        pixi exists; this unit is what makes that true on a fresh clone, a
        cloud session, or a stock Ubuntu box (proven from zero by
        `docker/verify-self-provision.sh` — see docs/dev/containers.md).

    Provisioning — `src/shipit/tree/create.py`:
        `_provision()` runs, each gated on a manifest existing: `shipit install .
        --local` (if `.shipit.toml`), `pixi install` (if `pixi.toml`, default
        env), the package-manager-aware frozen node install (if `package.json` —
        `node_install_argv()`, \#543: the `packageManager` pin first, the
        lockfile second, loud failure when neither decides — `npm ci` / `pnpm
        install --frozen-lockfile` / `yarn install --immutable` for Berry v2+ or
        `--frozen-lockfile` for classic v1, picked by the yarn major version,
        \#545). The `shipit
        install` and node-install
        steps funnel through the `run_provision()` seam, an Exec through the one
        runner (`shipit.execrun.run`, ADR-0028) with the generous explicit
        `PROVISION_TIMEOUT` (a cold frozen node install legitimately outlives the
        5-minute
        default). The pixi step instead runs through the pixi adapter,
        `shipit.pixienv.install()`, which carries pixi's own long-runner bound
        (`INSTALL_TIMEOUT`, 30 min — a cold solve+download outlives the default)
        — the pixi argv and its timeout live in the adapter, not the Tree code.
        Both paths share one narration (`_narrate_step`) and a durable record per
        step — timing on success, both stream tails on failure. A failed step
        raises the runner's single transport error, `ExecError`.

    Hooks — `.claude/settings.json`:
        The managed hook entries ride the pinned launcher `./bin/shipit hook
        <name>` (#491 dropped the `pixi run` wrap on the four additive hooks;
        the PreToolUse guard alone keeps a manifest-pinned `pixi run`, #529).
        shipit's own settings add a repo-local `SessionStart` entry, `pixi run
        -e lint install-hooks`; the managed `SessionStart` entry runs the
        Layer 0 bootstrap `./bin/setup-dev-env.sh` first (#547) and then the
        ADR-0027 activation, `./bin/shipit hook sessionstart`.

    Coordinator activation — `shipit hook sessionstart` (ADR-0027, Layer A):
        The top-level (coordinator) Claude Code session is a bare `claude` process
        with pixi absent from its process tree, so without help every coordinator
        Bash command needs a manual `pixi run` prefix — the coordinator-side twin
        of the agent-launch gap Work Env routing closes. The `SessionStart` hook closes
        it: it detects the toolchain governing the session's cwd (manifest
        discovery walks up, mirroring pixi's own), captures `pixi shell-hook
        --json` for the default env (borrow pixi's activation, never re-derive it
        — ADR-0022), renders the snapshot as `export KEY='value'` lines, and
        APPENDS them to the file named by `CLAUDE_ENV_FILE`, which Claude Code
        sources before every Bash tool call. Result: `shipit` / `python` /
        `pytest` / `ruff` resolve inside the repo's default env with no wrapper
        and no per-command prefix. The `--json` snapshot is rendered instead of
        the plain `shell-hook` script because that script ends in a `pixi()`
        shell FUNCTION wrapper, not pure exports (verified live, pixi 0.63+).
        Additive, never load-bearing: the committed hooks keep their `pixi run`
        prefix regardless, a repo with no activatable toolchain is a graceful
        no-op, and any failure fails OPEN (nothing written, exit 0, DEBUG log).
        Delivered to every managed repo by `shipit install` — the SessionStart
        hook line and the `./agent-start` launcher are managed units — so the
        capability is uniform, not shipit-only.

    Agent launch — `src/shipit/spawn/launch.py` + `src/shipit/spawn/subagent.py`:
        The per-backend `BackendAdapter` (`spawn/backends/`) builds the argv
        (`claude -p ... --output-format json`, or the codex/antigravity
        equivalents). The spawn boundary resolves a Work Env from the write
        Tree's provisioned-env sentinel. `launch.route_argv()` consumes that
        carried routing decision: `pixi-run` re-expresses the argv as `pixi run
        --manifest-path <tree>/pixi.toml -- <argv>`, while `ambient` keeps a
        non-pixi write Run bare. `scrub_tree_env()` drops the API key plus leaked
        `PIXI_*`/`CONDA_*` vars. The launch and provisioning scrubs share ONE
        predicate — `pixienv.is_leaked_env_var`, in the pixi adapter since
        PROC02-WS02; the wrapped argv and sentinel query live there too as
        `pixienv.run_argv` / `pixienv.has_default_env`, so they cannot drift.
        Reviewer Runs resolve a separate shared-read-only Work Env and launch
        through the review service with ambient tools (see [#7]).

    Work Env observability:
        Every boundary that resolves a Work Env records a flat, absent-not-null
        projection instead of an environment dump. The stable vocabulary is
        `work_env_boundary`, `working_dir`, `working_dir_repo`,
        `working_dir_branch`, `working_dir_commit`, `checkout_strategy`,
        `routing`, `role`, `lane`, `tree_branch`, `tree_base`,
        `pixi_activation`, `pixi_environment_name`, and
        `pixi_environment_lock_hash`, plus boundary-specific fields such as
        `ci_event`, `runner`, `required`, `fleet_repo`, and `tool`. The
        projection never includes secret values, full env snapshots, or a
        fabricated `pixi_run_id` — pixi has no such id.

7. Gotchas and known bugs

    Agent runs OUTSIDE its Tree's pixi env (was P0 — FIXED for write Trees by PR #197):
        Historically `child_env` did ZERO activation and `cwd=<tree>` does not
        activate pixi, so `<tree>/.pixi/envs/default/bin` was never on the child's
        PATH — the agent inherited the COORDINATOR's pixi env (a different Tree's
        `.pixi`) or the bare system env, and its `python`/`pytest`/`ruff`/`shipit`
        resolved to the WRONG environment (provisioned, then bypassed). FIXED:
        write-Run launch now routes through `pixi run --manifest-path
        <tree>/pixi.toml -- ...` with a CURATED env scrub (`scrub_tree_env`), so
        the agent lands in its own Tree's env. VERIFIED by spike (2026-06-30): use
        CURATED passthrough, NOT `--clean-env` — `--clean-env` was FALSIFIED (it
        strips `HOME`/`PATH`, so the child gets neither the Tree activation nor the
        `claude` binary, rc 127). Explicit `--manifest-path` overrides a leaked
        `PIXI_PROJECT_MANIFEST`, so the scrub is belt-and-suspenders. This revised
        ADR-0019, whose launch contract never considered activation.

        STILL OPEN, by design: the reviewer read-only Tree launch stays bare
        (no `.pixi/envs/default` to route into — a `chmod`'d clone). Tolerable
        because a reviewer only needs `gh` off the ambient PATH; it becomes a
        latent wrong-env bug only if a reviewer ever needs a Tree-pinned tool. The
        decision is centralised in the reviewer Work Env boundary, so a future
        "provision a read-only pixi env" change has one routing decision to update.

        Amortising activation cost: `pixi run` has an experimental activation
        cache (`--use-environment-activation-cache`, with `--force-activate` to
        bypass). Relevant if per-launch activation cost on the spawn path matters;
        mark experimental before relying on it.

    Leaked `PIXI_*` project pointers (#167) — CLOSED on the launch path:
        An inherited `PIXI_PROJECT_MANIFEST` makes a child's `pixi run` resolve
        the PARENT manifest and die. Provisioning always defended against this
        (`provision_env()` scrubs leaked `PIXI_*`); as of PR #197 the launch path
        does too, via the shared `is_leaked_env_var` predicate (`scrub_tree_env`),
        which now also strips the `CONDA_*` activation family — the predicate's
        home is `shipit.pixienv.scrub` (PROC02-WS02). The old asymmetry that made
        this a live bug is gone.

    Cross-filesystem cache (#119):
        Provisioning warns when the pixi/rattler cache and the Tree are on
        different filesystems (no reflink → slow copies).

    No provisioning logs — CLOSED (PROC01):
        `run_provision` used to discard the captured output, leaving no durable
        record of what `pixi install` printed during a Tree's provisioning. Every
        provisioning step is now an Exec through the one runner (ADR-0028): one
        structured record per step with cmd/rc/duration, both stream tails kept
        on failure — exactly where a broken `pixi install` writes its real
        diagnostics. No pixi cooperation was needed.

8. How to leverage pixi well

    Ranked, each with the concrete mechanism. Two items have since landed and are
    marked DONE below — the P0 launch-through-pixi (PR #197) and moving the build
    env into `[activation.env]` (COR01). The rest are live opportunities:

        1. DONE (COR01): shipit's hand-built build env (sccache/cargo) moved into
           pixi `[activation.env]`, so pixi sets it on EVERY activation instead of
           shipit recomputing what `[activation]` already computes. The old
           `sccache_env()` helper in `src/shipit/tree/create.py` is gone; the
           `CARGO_*` / `SCCACHE_BASEDIRS` build vars now come from
           `[activation.env]`, and inherited PARENT values are scrubbed at
           `is_leaked_env_var` so the per-Tree value stays authoritative. This also
           closed the gap where that env did NOT reach the agent's own in-Tree
           `cargo` (only the provisioning subprocess had got it).
        2. Turn on pixi's task `inputs`/`outputs` cache — it is entirely idle
           today. Every `pixi run lint` re-runs all linters even when nothing
           has changed. Declare `inputs`/`outputs` for pixi-native
           skip-if-unchanged. Scope carefully: lint is deliberately hard-fail/
           no-skip, so caching must not mask a real failure.
        3. Read pixi's persisted env identity instead of re-deriving it — prefer
           `conda-meta/pixi` (manifest path + env name + pixi version +
           `environment_lock_file_hash`) over the bare fingerprint; `pixi info
           --json` for prefix/env.
        4. Express the write-Run as a pixi task (`depends-on = ["install"]`) so
           "provision then run" is one pixi-owned entrypoint — LOW priority: the
           argv is dynamic per Run and pixi tasks are static in the manifest, so
           this likely is not worth replacing the thin Work Env + `route_argv`
           routing seam.
        5. If `default`/`review`/`dogfood` ever need to agree on package versions,
           use a pixi `solve-group` (every env currently has `solve_group: null`)
           rather than pinning by hand.

9. Refreshing this document

    When the pinned pixi version changes, re-verify before trusting the facts
    above. The checks, all cheap:

        - `pixi --version` — confirm the new pin; update the stamp.
        - `pixi --help`, `pixi run --help`, `pixi install --help` — re-check for
          any new JSON/structured-output or run-id flag ([#3], [#4]).
        - `pixi --help` / `pixi --list` — re-check the extension surface for any
          new plugin/hook mechanism ([#5]).
        - `pixi info --json`; `ls .pixi/envs/<env>/conda-meta/` and
          `cat .pixi/envs/<env>/conda-meta/pixi` — re-check the persisted state
          shape and the two digests ([#2]).
        - `pixi shell-hook --json` — re-check what activation injects ([#7]).
        - Re-read the integration seams: `_provision`/`run_provision`
          (`src/shipit/tree/create.py`), Work Env resolution
          (`src/shipit/workenv.py`), and `route_argv`/`scrub_tree_env`
          (`src/shipit/spawn/launch.py`).
