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

:: note :: Verified against pixi 0.71.0 on 2026-06-30. pixi moves fast — when the pinned version bumps, re-run the checks in [#9] and update the affected facts before trusting them.

1. What pixi is to shipit

    pixi is used for four things at once: provisioning native tooling, running
    tasks, defining per-purpose environments, and integrating with CI. shipit is
    a THIN layer on top — it must build ON pixi, not reinvent it.

    What pixi is the parent of:

        - Provisioning — `pixi install` materialises a Tree's environment.
        - Hook invocations — every Claude Code hook fires as `pixi run shipit
          hook <name>` (a transient `pixi run` per firing).

    What pixi is NOT the parent of:

        The spawned agent session. `shipit spawn subagent` launches `claude -p`
        as a bare subprocess, NOT under `pixi run` (see [#6] and the bug in
        [#7]). So pixi is absent from the agent's process tree — it sits only in
        front of provisioning and each hook.

    What pixi does NOT own (carried from [./architecture.lex]): building and
    signing distributable artifacts. `pixi build` is preview-grade and emits
    conda packages. shipit keeps the real builders and uses pixi to PROVISION and
    RUN them.

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
        - `.pixi/envs/<env>/conda-meta/<pkg>-<ver>-<build>.json` — one manifest
          per installed package. `conda-meta/history` is a stub.
        - `.pixi/task-cache-v0/<env>-<task>-<hash>.json` — `{"hash": "..."}`, a
          content hash of a task's inputs so pixi can skip unchanged re-runs.
          Records THAT a task ran with a given input hash — not when, nor its
          exit code, nor a run id. Only `pixi run <task>` writes these; `pixi
          install` does not.

    `pixi.lock` (version 7) is the stable description of "this exact resolved
    environment": per-environment, per-platform, fully-resolved packages each
    with `sha256`/`md5`. There is NO top-level `content-hash` field (unlike
    Poetry). The closest thing to a per-env identity is the pair `(env name,
    .pixi-environment-fingerprint)` or hashing the env's slice of `pixi.lock`
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
        - No environment UUID — envs are keyed by name; the only digest is the
          sync-state fingerprint ([#2]), which is not a stable identity.
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

    Only `pixi list` and `pixi info` have machine-readable (`--json`) output;
    `pixi run` and `pixi install` do not. Verbosity flags (`-v`..`-vvvv`, `-q`)
    control human stderr text, not structured output.

    Commands shipit relies on or could use:

        - `pixi install` — materialise the env from the lock (what provisioning
          runs; default env when no `-e`). Human stderr only.
        - `pixi run [-e env] <cmd|task>` — activate the env, then exec. The ONLY
          way (besides `shell`/sourcing `shell-hook`) that pixi activation
          happens. No run id, no JSON.
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
            `shell-hook`. shipit currently declares none (`pixi shell-hook --json`
            shows `activation_scripts: []`). This is where shipit-owned env (e.g.
            the sccache build env) BELONGS — but it only fires when execution goes
            through pixi.

        Task `depends-on`:
            Pre-task chaining only (`pixi task add --depends-on`, plus `--env`,
            `--cwd`, `--clean-env`, `--args`). There is NO native post-task or
            wrapper hook — shipit behaviour can hang off the FRONT of a task, not
            wrap it.

        External `pixi-<name>` subcommands:
            git/cargo-style dispatch — `pixi foo` execs a `pixi-foo` on PATH
            (`pixi --list` shows installed extensions). This is dispatch
            convenience only: an external subcommand gets NO pixi internals, env
            callbacks, or activation injection. It would let `pixi shipit ...`
            work; it would not let shipit's concerns run inside pixi's lifecycle.

6. The shipit ↔ pixi contract today

    Provisioning — `src/shipit/tree/create.py`:
        `_provision()` runs, each gated on a manifest existing: `shipit install .
        --local` (if `.shipit.toml`), `pixi install` (if `pixi.toml`, default
        env), `npm ci` (if `package.json`). All funnel through one seam,
        `run_provision()`, which calls `proc.run` (captures stdout/stderr/exit)
        but DISCARDS the result — so on success pixi's output is thrown away; only
        a failure survives, inside the raised `ProcError`.

    Hooks — `.claude/settings.json`:
        Every hook is `pixi run shipit hook <name>` (and `SessionStart` is `pixi
        run -e lint install-hooks`). `shipit` is on PATH because the package is
        installed editable into each env, not because it is a pixi task. pixi is a
        transient wrapper per hook firing.

    Agent launch — `src/shipit/spawn/launch.py`:
        `build_command` constructs `claude -p <task> --agent <role>
        --permission-mode bypassPermissions \[--tools ...\] --output-format json` —
        with NO pixi prefix — and `_subprocess_runner` execs it directly via
        `subprocess.run(cwd=tree, env=child_env(), stdin=DEVNULL)`. `child_env`
        only drops `ANTHROPIC_API_KEY`. This is the path that bypasses pixi (see
        [#7]).

7. Gotchas and known bugs

    Agent runs OUTSIDE its Tree's pixi env (correctness bug, P0):
        `child_env` (`src/shipit/spawn/launch.py`) does ZERO activation, and
        `cwd=<tree>` does not activate pixi — so `<tree>/.pixi/envs/default/bin`
        is never on the child's PATH. The agent inherits the spawn process's
        environment: either the COORDINATOR's pixi env (a different Tree's
        `.pixi`) or the bare system env. Its `python`/`pytest`/`ruff`/`shipit`
        resolve to the WRONG environment — the Tree is provisioned, then bypassed.
        `pixi shell-hook --json` shows exactly what is missing (PATH prepend of
        the env bin, `CONDA_PREFIX`, `PIXI_PROJECT_*`). Fix: launch through pixi —
        `pixi run --manifest-path <tree>/pixi.toml claude -p ...`
        (API-key scrub still via `env=`; child JSON stays on stdout while pixi
        progress goes to stderr). VERIFIED by spike (2026-06-30): use CURATED
        passthrough, NOT `--clean-env`. `--clean-env` was FALSIFIED — it strips
        `HOME` and `PATH`, so the child gets neither the Tree activation (no
        `python`) nor the `claude` binary (`claude: command not found`, rc 127).
        CURATED works: pass `env=` the parent minus `ANTHROPIC_API_KEY` +
        `PIXI_*`/`CONDA_*`; the child lands in the Tree env, `HOME` intact, claude
        authenticates (rc 0), stdout JSON clean, stderr empty. Explicit
        `--manifest-path` already overrides a leaked `PIXI_PROJECT_MANIFEST`, so
        the scrub is belt-and-suspenders, not load-bearing. This revises ADR-0019,
        whose launch contract never considered activation.

    Leaked `PIXI_*` project pointers (#167):
        An inherited `PIXI_PROJECT_MANIFEST` makes a child's `pixi run` resolve
        the PARENT manifest and die. Provisioning DEFENDS against this —
        `provision_env()` scrubs leaked `PIXI_*` vars
        (`src/shipit/tree/create.py`) — but the launch path does NOT, so the
        agent is exposed to the exact leak class shipit already fixed once. The
        asymmetry is the tell that [#7]'s bypass is an oversight. routing through `pixi run --manifest-path` with the scrubbed env (see
        [#7]) closes this leak on the launch path too.

    Cross-filesystem cache (#119):
        Provisioning warns when the pixi/rattler cache and the Tree are on
        different filesystems (no reflink → slow copies).

    No provisioning logs:
        Because `run_provision` discards the captured output, there is today no
        durable record of what `pixi install` printed during a Tree's
        provisioning. The thin fix is to stop discarding it and log
        cmd/returncode/duration through the existing logsetup file sink — no pixi
        cooperation needed.

8. How to leverage pixi well

    Ranked, each with the concrete mechanism:

        1. Launch the agent THROUGH pixi, not around it (P0 correctness) —
           `pixi run --manifest-path <tree>/pixi.toml claude -p ...` (scrubbed env, no `--clean-env`)
           in `src/shipit/spawn/launch.py`. The agent's tools then resolve
           to its OWN Tree's env.
        2. Mirror the provisioning scrub on the launch path (drop
           `ANTHROPIC_API_KEY` + leaked `PIXI_*`/`CONDA_*`) — closes the #167 leak
           the launch path re-opened. `--clean-env` was falsified (strips
           `HOME`/`PATH`); see [#7].
        3. Move shipit's hand-built build env (sccache/cargo) into pixi
           `[activation.env]` so pixi sets it on activation — verify pixi template
           vars for per-Tree absolute paths first.
        4. Express the agent run as a pixi task (`depends-on = ["install"]`) so
           "provision then run" is one pixi-owned entrypoint — removes shipit's
           parallel env/PATH derivation entirely.
        5. Read pixi's persisted env identity instead of re-deriving it — `pixi
           info --json` for prefix/env, the `.pixi-environment-fingerprint` for
           "provisioned-and-consistent".

9. Refreshing this document

    When the pinned pixi version changes, re-verify before trusting the facts
    above. The checks, all cheap:

        - `pixi --version` — confirm the new pin; update the stamp.
        - `pixi --help`, `pixi run --help`, `pixi install --help` — re-check for
          any new JSON/structured-output or run-id flag ([#3], [#4]).
        - `pixi --help` / `pixi --list` — re-check the extension surface for any
          new plugin/hook mechanism ([#5]).
        - `pixi info --json`; `ls .pixi/envs/<env>/conda-meta/` — re-check the
          persisted state shape ([#2]).
        - `pixi shell-hook --json` — re-check what activation injects ([#7]).
        - Re-read the integration seams: `_provision`/`run_provision`
          (`src/shipit/tree/create.py`), `build_command`/`child_env`
          (`src/shipit/spawn/launch.py`).
