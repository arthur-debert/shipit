# Session Bootstrap — the coordinator's own isolated, activated session

## Problem Statement

Trees (`docs/prd/where-to-do-work.md`, ADR-0014) gave every *spawned* Run an isolated,
provisioned checkout. But the one session that starts it all — the **coordinator**, the
top-level, human-facing Claude Code session — was left behind. It still runs in the plain
repo checkout it was launched from, un-isolated and un-activated. Two faces of one gap:

- **The coordinator collides with other sessions on the same repo.** Every spawned Run
  gets a Tree; the coordinator gets the shared working tree. The moment a second session is
  launched in the same checkout — or the coordinator switches a branch, dirties the index,
  or moves HEAD while another session is working — they silently corrupt each other. The
  isolation Trees provide everywhere else is exactly what is missing at the top. Concurrent
  coordinator sessions on one repo are effectively impossible today.

- **The coordinator pays a "meta" activation tax on every command.** The coordinator is a
  bare `claude` process with pixi absent from its process tree (like a spawned agent before
  ADR-0019's `pixi_wrap` fix). So its `shipit` / `python` / `pytest` / `ruff` resolve to
  the wrong environment — or not at all — unless every single Bash command is manually
  prefixed with `pixi run`. This is the source of the recurring friction: the heads-up
  memories, the `#210`-adjacent nags, the "why did that run outside the env" surprises.

Crucially, **this is not a shipit-self problem** — it is every managed repo's problem.
Working in `lex`, `dodot`, or any consumer, the human wants the root session to be equally
isolated and equally activated. The friction is felt wherever shipit is rolled out, so the
fix belongs in shipit's managed capability, not in a shipit-only hack.

The hard constraint that shapes the whole solution: **a Claude Code session's working
directory is fixed at process launch and immutable afterward — no hook can change it**
(verified against Claude Code docs and a live spike, CC 2.1.198: *"Changing the working
directory within a hook does not affect Claude Code's subsequent operations."*). So there
is **no in-session fix**: isolation must be established *before* the process starts. A
manual `shipit tree create` + `cd` cannot relocate a running session — the coordinator is
stuck wherever `claude` was launched.

## Solution

Give the **coordinator its own isolated, activated Session Tree at launch**, using the two
pre-launch seams Claude Code already exposes — and deliver it to **every managed repo** via
`shipit install`, dogfooded on shipit itself.

A **Session Tree** is the coordinator's own workspace: a fully-independent dissociated
clone (ADR-0014), minted at launch by `claude --worktree <id>` (or `-w`), which fires
shipit's *existing* `WorktreeCreate` hook. The hook returns the clone's path and Claude
Code adopts it as the session cwd — verified live: the top-level `--worktree` flag fires
the same hook as an in-session `Agent(isolation:"worktree")` spawn, and CC adopts a
hook-substituted dissociated-clone path as the **root** session's cwd.

The Session Tree is **ephemeral-by-path, work-by-branch** (ADR-0027):

- **Directory identity = the session.** `<root>/<org>/<repo>/ephemeral/<id>`, one per
  launch, disposable, **never renamed**. At launch the work is almost always unknown (a
  planning, triage, or exploration session before any epic/issue exists), so there is
  nothing to bind to but `main`; the Tree is *inherently* ephemeral.
- **Branch identity = the work.** The clone starts on `ephemeral/<id>` (base
  `origin/main`); as the session learns its task, the coordinator switches branches *inside
  the clone* (`git checkout -b docs/<slug> origin/main`, `git checkout -b EPIC/umbrella
  origin/EPIC/umbrella`, …). It is a full clone with `origin` set, so commit / push / PR /
  merge all work against the remote, isolated from every other session. There is **no
  mid-flight path move** (impossible — immutable cwd; unnecessary — switch the branch). The
  accepted consequence: once switched, dir name and branch stop mirroring; `shipit tree
  list` reads the branch from live git HEAD, so nothing is lost but cosmetic symmetry.

And give the coordinator an **activated environment for every Bash command**, without a
wrapper: a `SessionStart` hook writes the repo's activation into `CLAUDE_ENV_FILE`, which
Claude Code sources as a preamble before every Bash tool call. Because the activation is
**toolchain-aware** (pixi repo → `pixi shell-hook`; a repo with no activatable toolchain →
graceful no-op), the same mechanism serves shipit and `lex` and everything in between.

Cleanup is **liveness-based with robust, liveness-independent backstops**, because an
ephemeral Tree has no PR to key reclaim off and is often clean. A `gc` rule for the
`ephemeral` kind keeps live sessions, protects any uncommitted/unpushed work absolutely,
and still guarantees eventual reclaim of abandoned Trees.

The result: launch `claude` (or the shipped `./claude-start` alias), land in a ready,
isolated, activated clone of the repo, do the work, and let it be reclaimed automatically —
in any managed repo, with no per-command `pixi run` and no cross-session collisions.

## User Stories

1. As a coordinator, I want my top-level session to run in its own isolated clone, so that a second session on the same repo never corrupts my working tree, index, or HEAD.
2. As a human running two agents on one repo at once, I want each root session isolated, so that I can parallelize work on a repo without them stepping on each other.
3. As a coordinator, I want `shipit` / `python` / `pytest` / `ruff` to just work in my Bash commands, so that I never have to remember to prefix `pixi run`.
4. As a coordinator on a planning/triage session, I want a disposable ephemeral workspace off `main`, so that I can explore and plan before I know which epic or issue I'll work on.
5. As a coordinator, once I know the work, I want to switch my clone to the real branch (a `docs/<slug>` for a planning PR, an `EPIC/umbrella` to drive an epic), so that I can commit, push, and open/merge PRs without leaving my isolated Tree.
6. As a coordinator driving an epic, I want my session Tree to stand in for the old manual "hand-run `shipit tree create --epic` at session start" step, so that I no longer perform that step by hand.
7. As a coordinator, I want to open the planning docs PR (Leg A) from my session Tree, so that the PRD/ADR changes are pushed from an isolated clone like any other work.
8. As a coordinator, I want to keep spawning implementer/shepherd/reviewer Runs through `shipit spawn subagent` unchanged, so that Run isolation is untouched by this feature.
9. As a user working in a consumer repo (`lex`, `dodot`, …), I want the exact same isolated + activated root session, so that the capability is uniform across every managed repo, not shipit-only.
10. As a user in a non-pixi consumer repo, I want session activation to degrade gracefully to a no-op, so that the SessionStart hook never errors where there's no `pixi.toml`.
11. As a maintainer, I want the Session Tree + activation delivered by `shipit install` as managed hooks, so that adopting a repo turns the capability on with no manual wiring.
12. As a user, I want a `./claude-start` alias in the repo root, so that I can launch an isolated session by habit without remembering the `--worktree` flag.
13. As a user who prefers the raw flag, I want `claude -w <name>` to work identically, so that the alias is convenience, not a requirement.
14. As a coordinator, I want my ephemeral Tree provisioned (deps installed) like any Tree, so that the session starts ready.
15. As a coordinator, I want a Tree I abandon (closed terminal, crashed, rebooted) to be reclaimed automatically, so that ephemeral Trees don't accumulate and fill my disk.
16. As a coordinator, I want a Tree with uncommitted or unpushed work to **never** be auto-deleted, so that `gc` can never lose work that lives only in my clone.
17. As a coordinator, I want a live session's Tree to be kept even when it's clean and idle, so that `gc` never deletes a Tree out from under a running session.
18. As a coordinator, once my session's work is merged to `main`, I want its Tree reclaimable, so that finished work doesn't linger.
19. As an operator, I want a hard time cap (~4 days) that reclaims clean, pushed, abandoned Trees even if their liveness marker is wrong or stale, so that a forgotten pidfile can never strand a Tree forever.
20. As a coordinator, I want `shipit tree list` / `gc` to treat my ephemeral session Trees as a first-class kind, so that they show and reclaim distinctly from write/review Trees.
21. As a maintainer, I want the WorktreeCreate hook to tell my coordinator session apart from an in-CC `Agent(isolation:"worktree")` helper spawn, so that my session gets an ephemeral branch off `main` while helpers keep their `<epic>/agent-<id>` holding branch.
22. As a reader of the docs, I want CONTEXT.md, `naming.lex`, `epics.lex`, `pixi.lex`, and the relevant docstrings to describe the new model, so that the "coordinator gets an *epic* Tree at session start" / "WorktreeCreate is throwaway-only" / "tree↔branch always mirror" statements no longer contradict reality.
23. As a coordinator, I want the existing `pixi run shipit hook …` hooks to keep working whether or not session activation succeeded, so that activation is additive ergonomics and never load-bearing for hook correctness.
24. As a maintainer, I want native `git worktree` / `EnterWorktree` to remain denied while `--worktree` (which routes through the hook) is the supported path, so that no one drifts back to the old shared-worktree mess.
25. As an operator, I want deleting an ephemeral Tree's environment to be cheap, so that reclaiming and recreating session Trees doesn't waste bandwidth or disk (shared global pixi cache — mostly hard-linking).

## Implementation Decisions

### Layer A — coordinator environment activation (`shipit hook sessionstart`)

- A new `SessionStart` hook handler, **toolchain-aware**, emits the repo's activation
  script into the file named by `CLAUDE_ENV_FILE`. Claude Code sources that file as a
  preamble before **every** Bash tool call, so all coordinator Bash commands run activated.
- For a pixi repo it emits `pixi shell-hook` output (the pixi-blessed bridge for activating
  outside `pixi run`, per the verified pixi KB), activating the `default` environment
  resolved from the session's cwd (its own Session Tree when isolated, else the checkout).
  For a repo with no activatable toolchain it is a **graceful no-op**. The map from repo
  toolchain(s) → activation lines is a **pure function** (the deep core); the hook shell is
  thin.
- **Additive, never load-bearing.** The existing hooks keep their `pixi run` prefix
  unchanged: `pixi run` is robust to being invoked inside an already-activated same-project
  env, and keeping the prefix means the hooks work even if activation failed or is absent.
  Dropping the prefix (relying on ambient activation) is explicitly rejected as fragile.
- This is the coordinator-side twin of ADR-0019's `pixi_wrap`: the same "a bare `claude`
  needs the Tree's env made active" fix, applied to the top-level session via
  `CLAUDE_ENV_FILE` instead of re-expressing an argv through `pixi run`.

### Layer B — the Session Tree (`tree/layout` ephemeral shape + `WorktreeCreate` fork)

- **New `ephemeral` Tree kind** in the pure planner (`tree/layout`), a fourth shape beside
  issue / epic / freeform: dir `<root>/<org>/<repo>/ephemeral/<id>`, branch
  `ephemeral/<id>`, base `origin/main`. `id` is the session identifier (the `--worktree`
  value / a minted id), carried on both the dir leaf and the branch — the tree↔branch bond
  holds *at birth*, then the branch moves and the dir stays (by design, ADR-0027).
- **`--worktree` reuses the existing `WorktreeCreate` hook.** No new launch machinery: the
  top-level flag fires the same fail-closed hook, and CC adopts the printed dissociated
  clone path as the root cwd (verified). The hook's `_resolve_branch` gains a **fork**:
  - coordinator's *own* session Tree → the `ephemeral` shape (branch `ephemeral/<id>`, base
    `origin/main`);
  - in-CC `Agent(isolation:"worktree")` helper spawn → the existing `<epic>/agent-<id>`
    holding branch (unchanged).
- **Discriminator (settled — see
  `docs/dev/ses02-worktreecreate-discriminator-spike.md`).** The two invocations carry the
  same payload fields, but the spike's top-level `--worktree` payload had **no `prompt_id`**
  whereas ADR-0017 records in-CC spawns carrying one. Rule, confirmed live on CC 2.1.198:
  *`prompt_id` absent ⇒ coordinator session Tree*; fallback: a `./claude-start` name-prefix
  convention.
- **Elevation of the hook (amends ADR-0017).** The `WorktreeCreate` hook is no longer
  "throwaway-only": it legitimately owns the coordinator's *own* Session Tree — the one
  Tree `shipit spawn subagent` structurally cannot mint (it's the session's own, and the
  cwd is fixed before shipit runs). `shipit spawn subagent` remains the **sole** path for
  Runs the coordinator *launches* (implementer / shepherd / reviewer).
- **Substrate = dissociated clone** (ADR-0014), provisioned like any Tree (`tree/create`,
  gated on which manifests exist — pixi / npm / neither), so a consumer repo's Session Tree
  provisions correctly with no special-casing.

### Layer C — teardown (`gc` ephemeral ladder + fast-path hook)

- **Fast path:** a `WorktreeRemove` hook handler (and/or `Stop`/`SessionEnd`) removes the
  pidfile and the Tree on clean exit. But `WorktreeRemove` did **not** fire in headless
  mode in the spike, so the fast path is best-effort — **not** load-bearing.
- **Load-bearing cleanup:** extend the pure `tree/cleanup.classify` with a rule for the
  `ephemeral` kind and a new `live_sessions` input (path → is-live). Precedence ladder,
  first match wins:
  1. **dirty or unpushed → KEEP** (absolute floor; the existing removal gate). "Unpushed"
     means "has local commits not present on any remote," **not** "lacks an upstream branch":
     a fresh `ephemeral/<id>` has no upstream, so an upstream-based test would keep every
     session Tree forever — the missing upstream must never by itself block reclaim.
  2. **branch has a merged PR → REMOVABLE** (reuse the existing merged-PR classify — covers
     the case where the session did real work on a real branch that landed).
  3. **live ∧ younger than the hard cap → KEEP.**
  4. **hard time cap (~4 days) ∧ clean ∧ pushed → REMOVABLE even if the pidfile claims
     live** (escape hatch — a clean session idle for days is abandoned; overrides liveness
     so a wrong/stale pidfile can never strand a Tree forever).
  5. otherwise (not live, clean, pushed) → **REMOVABLE**, past a short grace window so a
     just-launched session isn't raced before its pidfile lands.
- **Liveness = a pidfile** (`session/liveness`, new deep module) the `SessionStart` hook
  writes into the Tree, recording the `claude` session's **PID**, its **`session_id`**, and
  the **PID's process create-time read from the OS at write time** (not wall-clock "now").
  A Tree is live iff: PID alive **and** the process looks like the recorded Claude Code
  session **and** its create-time matches the recorded value **within a small tolerance**
  (the hook fires slightly after the process starts, so the two are close but not equal —
  tolerance absorbs that plus clock granularity). The "looks like Claude Code" test must
  **not** assert the OS process name is exactly `claude` — Claude Code is a Node.js app, so
  the reported name is often `node`; match the **command line** (argv contains the `claude`
  entrypoint), with create-time-within-tolerance as the primary per-PID identity so a
  `node`-named live session is never misread as dead. Immune to reboot / PID-reuse at
  near-zero cost; PID-reuse is
  the *safe* direction (`gc` deletes directories, never processes — a false "alive" only
  lets a dead Tree linger, it never deletes a live one). The write is effectful; the
  `is_live(record, probe)` decision is pure with an injectable process-probe seam.
- pixi provides **no** liveness / env-GC / session state (verified KB — only static state,
  keyed on nothing joinable), so the pidfile fills a real gap rather than reinventing pixi.

### Layer D — ergonomics (`./claude-start`)

- A thin `./claude-start` in the repo root: `exec claude --worktree "<minted-id>" "$@"`.
  Shipped into managed repos by `shipit install` (the `data/bootstrap/shipit` bootstrap-file
  pattern). Optional sugar — `claude -w <name>` works identically without it.

### Delivery — not shipit-specific

- All of the above (the SessionStart / WorktreeCreate / WorktreeRemove hook wiring and the
  `./claude-start` file) is part of the **managed hook set `shipit install` lays into every
  managed repo**. shipit-self is the first install (its committed `.claude/settings.json`),
  but the capability is general.

### Documentation reconciliation (first-class, not an afterthought)

- The current docs encode the *opposite* model in several places; correcting them is part
  of the work, gated on the same PR discipline as code:
  - **CONTEXT.md** — Session Tree term added; "epic Tree at session start" and
    "throwaway-only hook" framings corrected (done this session).
  - **`docs/dev/naming.lex`** — add the `ephemeral/` path+branch shape and the
    dir=session / branch=work split.
  - **`docs/dev/epics.lex`** — retire the manual "hand-run `shipit tree create --epic` at
    session start" step (the Session Tree replaces it).
  - **`docs/dev/pixi.lex`** — the coordinator-activation story (Layer A).
  - **Docstrings** — `tree/layout.py`, `verbs/hook/worktreecreate.py`,
    `harness/worktree_adapter.py` (the "throwaway helper" framing and the
    hash-on-dir/branch invariants).

## Testing Decisions

Good tests here assert **external behavior**, not implementation: given inputs, the pure
cores produce the right plan / partition / decision / activation lines — never "was this
private helper called." shipit's `tree/layout` and `tree/cleanup` truth-table suites are
the prior art to mirror.

- **`tree/layout` ephemeral shape** — unit truth table: an ephemeral spec resolves to the
  right dir / branch / base; id normalization / rejection of degenerate ids matches the
  existing shapes' boundary tests.
- **`tree/cleanup` ephemeral ladder** — unit truth table over the full precedence ladder:
  dirty/unpushed keep; merged-PR removable; live+young keep; hard-cap override removable;
  not-live+clean+pushed removable; the short-grace boundary. Extends the existing `classify`
  table with the `live_sessions` input, mirroring how `live_reviews` was added for read-only
  Trees.
- **`session/liveness` decision** — unit: `is_live` with a **faked process-probe** across
  PID-dead, PID-alive-but-not-claude, create-time-mismatch, and within-tolerance-match; no
  real processes spawned.
- **`hook/sessionstart` activation emitter** — unit: pixi repo → the expected `pixi
  shell-hook` invocation lines; non-pixi repo → empty/no-op; the toolchain→activation map is
  exercised directly.
- **`hook/worktreecreate` fork** — unit: given a coordinator-session payload vs an in-CC
  helper payload (per the chosen discriminator), the resolved branch is `ephemeral/<id>` vs
  `<epic>/agent-<id>` respectively, reusing the existing worktreecreate test harness.
- **`shipit install`** — extend existing install tests to assert the new managed hooks and
  the `./claude-start` file are laid down (and idempotently).
- **Live integration (opt-in, like the existing spawn/dogfood harness):** a real
  `claude --worktree` launch lands the root session in the ephemeral Tree with the env
  activated — the end-to-end proof, kept off CI (token/real-process spend) like the
  `dogfood` env harness.

## Out of Scope

- **Binding the Session Tree to an epic/issue at launch** (`-w TRE05` → check out the
  umbrella). The work is usually unknown at launch and a full clone switches branches
  freely, so one ephemeral shape is the model; launch-time binding is explicitly not built.
- **Renaming the Tree path mid-session** to reflect the discovered work — impossible
  (immutable cwd) and pointless for a disposable Tree; branch-switch within the fixed path
  is the mechanism.
- **A bespoke launcher script** that sets cwd + env then `exec claude` — unnecessary, since
  `--worktree` + the existing hook + `CLAUDE_ENV_FILE` do the job; `./claude-start` is a
  thin alias only.
- **Changes to `shipit spawn subagent`** or to Run (implementer/shepherd/reviewer)
  isolation — untouched.
- **Non-pixi toolchain activation beyond a no-op** (npm/cargo/etc. activation specifics) —
  the emitter is extensible per toolchain, but only the pixi case (and the graceful no-op)
  is built now.
- **A subjective/agent-judge signal** for whether a session Tree was "productive" — this
  feature is isolation + activation + cleanup, not evaluation.

## Further Notes

- **Relationship to ADRs:** new **ADR-0027** (coordinator Session Tree — ephemeral,
  work-by-branch, via `--worktree`); it **amends ADR-0017** (elevates the `WorktreeCreate`
  hook from throwaway-only to also owning the coordinator's Session Tree) and lives under
  **ADR-0014** (dissociated-clone substrate, still the rule; native worktree still denied).
- **Why the WorktreeCreate hook, not `shipit spawn`:** `spawn` provisions Trees *for other
  Runs* by passing intent as arguments; it structurally cannot provision the coordinator's
  *own* Tree, because that Tree is the top-level session's and the session cwd is fixed
  before any shipit code runs. `--worktree` is the only pre-launch seam.
- **The one build-time spike to run first (settled):** confirm the coordinator-vs-helper
  discriminator in the `WorktreeCreate` payload (candidate: absence of `prompt_id`) by
  comparing a real in-CC `Agent(isolation:"worktree")` spawn payload against a top-level
  `--worktree` one. Ran as `docs/dev/ses02-worktreecreate-discriminator-spike.md` —
  verdict: *`prompt_id` absent ⇒ coordinator*, confirmed live on CC 2.1.198. Everything
  else in Layer B is verified.
