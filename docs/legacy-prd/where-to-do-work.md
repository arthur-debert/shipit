# Where To Do Work — isolated Trees for concurrent agents

## Problem Statement

Today every agent (and the human) works in **one shared checkout** on the dev tree.
In the early days that was fine — one human, one primitive agent. It no longer is:

- **Concurrent agents and the human collide on the same files.** Two sessions editing
  the same working tree ruin each other's work — minutes lost at best, occasionally
  far more, and the damage is silent until something doesn't build.
- **Every write-session pays a discovery tax.** An agent spends a minute or two and
  ~1k tokens at the start of each task just figuring out *where* to work — checking the
  tree, `git status`, which branch, whether it's dirty.
- **The default tooling makes its own mess.** Claude Code's native `git worktree`
  feature drops checkouts into `.claude/worktrees/` (source-controlled territory) and,
  when an agent can't infer a branch name, invents one from the worktree hash. This
  repo currently carries **25 nested worktrees (14 GB working tree, vs a 22 MB `.git`)**
  and **32 `worktree-agent-*` branches** — direct evidence of both failure modes.
- **Inconsistency is the real cost.** There are many valid setups, agents can't infer
  ours from pre-training, so each one explores and picks differently. With consumer-repo
  fanout imminent, locking this down *now* — before the blast radius grows — is what
  keeps the rollout smooth.

## Solution

Give every write-session its **own isolated Tree** — a fully-independent clone of the
repo — provisioned and assigned by the **coordinator**, named consistently, and cleaned
up on a simple policy. The agent starts in a *ready* Tree (branch checked out, deps
installed, secrets in place) and never has to decide where to work.

Concretely, a new `shipit tree` command surface (`create | list | remove | gc`) makes
Trees a first-class, standalone primitive:

- The **coordinator** provisions its own epic Tree at session start, then hands each
  **implementer** / **shepherd** a ready Tree for its **Run**. **Explorers** are exempt
  (read-only work needs no isolation).
- Trees live under one **central root outside every repo** (`~/workspace/trees/<org>/<repo>/…`),
  so cleanup and inspection are uniform across repos and agents — never inside `.claude/`.
- A Tree is an **independent dissociated clone**, not a git worktree (ADR-0014), so it
  can sit on `main`, two Trees can share a branch, there's no shared-`gc` corruption, and
  `rm -rf` is a safe delete.
- Build artifacts are per-Tree, with **sccache** as the cross-Tree cache (ADR-0015).
- The native `git worktree` path is **denied** (not redirected) so agents can't drift
  back to the old mess.

The structure deliberately mirrors how work is already organized (epics, work streams,
issues) — the same workflow pillars showing up in the tool surface are guardrails, not
busywork. It does imply every piece of work has a GitHub issue or epic/WS to hang off;
that's an accepted, mild cost (and a useful record of provenance).

## User Stories

1. As a **coordinator**, I want to create an isolated Tree for a work stream with one
   command, so that I can hand a subagent a ready place to work without it improvising.
2. As a **coordinator**, I want `shipit tree create --epic HAR02 --ws 02` to resolve the
   branch, directory, and base ref for me, so that naming is consistent across every
   agent instead of 2000 agents getting creative.
3. As a **coordinator**, I want to create a Tree for a bare issue
   (`shipit tree create --issue 433`), so that one-off fixes get the same isolation as
   epic work.
4. As a **coordinator**, I want my own epic Tree at session start, so that I manage my
   own branch in isolation while delegating work stream Trees to subagents.
5. As an **implementer**, I want to start already inside a provisioned Tree on my branch,
   so that I spend zero tokens discovering where to work and start coding immediately.
6. As an **implementer**, I want the Tree's dependencies already installed
   (`shipit install` + `pixi`/`npm`), so that my first build/test just works.
7. As an **implementer**, I want gitignored-but-needed files (`.env`, Doppler config, the
   phos SAML model) already present in my Tree, so that the app actually runs without me
   hunting for secrets.
8. As a **shepherd**, I want a fresh Tree for a review round on an open PR, so that I
   address review feedback without colliding with whoever is on another branch.
9. As the **human**, I want my own checkout to be just another Tree (or my long-standing
   local checkout), so that agents never touch the files I'm editing.
10. As any write-session, I want to check out **any** branch including `main` in my Tree,
    so that I'm not blocked by the git-worktree "already checked out elsewhere" error.
11. As a **coordinator**, I want two Trees to be allowed on the same branch, so that when
    an agent dies I can spin a replacement Tree on the same branch without state-tracking
    gymnastics.
12. As a **coordinator**, I want each Tree's directory name to carry a unique agent hash
    while the branch name stays stable, so that duplicate Trees never collide on disk and
    the branch still reads as a meaningful namespace.
13. As any agent, I want a Tree's *directory path* and its **branch** to share one slash
    namespace (`…/epics/HAR02/WS02-<hash>` on disk, branch `HAR02/WS02`; the epic branch is
    `HAR02/umbrella`, standalone work is `issues/433/work`, per `naming.lex §3`), so that an
    epic's Trees and branches group cleanly under `HAR02/` without the epic branch
    colliding with its work-stream refs.
14. As a **coordinator**, I want `shipit tree list` to show every Tree with its branch,
    base, age, dirty state, and PR status, so that I can see the whole fleet at a glance.
15. As a **coordinator**, I want `shipit tree remove <id>` to safely delete one Tree, so
    that I can reclaim a finished workspace immediately.
16. As a **coordinator**, I want `shipit tree gc` to sweep only Trees whose PR is merged,
    working tree is clean, nothing is unpushed, and which are aged past a threshold, so
    that I never lose unmerged or in-flight work to cleanup.
17. As a **coordinator**, I want Trees whose state is ambiguous to be *listed as stale*
    rather than auto-deleted, so that cleanup is conservative by default.
18. As an **explorer**, I want to run read-only investigation in the main checkout without
    a Tree, so that the system doesn't provision isolation I don't need.
19. As any agent, I want `git fetch/pull/push` and all `gh` commands to work normally in a
    Tree, so that the PR flow is identical to a normal clone.
20. As a **coordinator**, I want the cost of a new Tree to be ~22 MB and a few seconds, so
    that spinning up isolation per work stream is never a reason to skip it.
21. As any agent, I want an attempt to call `EnterWorktree` or run `git worktree add` to be
    **denied with a message pointing me to `shipit tree create`**, so that I can't
    accidentally recreate the `.claude/worktrees` mess.
22. As the **human**, I want all Trees under one predictable root, so that I can audit,
    back up, or wipe agent workspaces without spelunking through each repo.
23. As a **coordinator**, I want Tree creation to be a standalone CLI primitive (not buried
    inside a subagent-spawn wrapper), so that I can provision a Tree by hand or from any
    tooling.
24. As a maintainer, I want the Rust cross-Tree build cache (sccache) configured so that a
    cold Tree's first build reuses cached compiler output, so that per-Tree `target/`
    isolation doesn't mean rebuilding the world.
25. As a maintainer, I want `shipit tree list` to derive state by scanning the central root
    (no manifest file), so that there's no separate state store to drift out of sync.

## Implementation Decisions

**New deep package `src/shipit/tree/`** (surface: `shipit tree create|list|remove|gc`),
built from small testable pieces in the `prstate` "snapshot → decision" idiom:

- **`tree/layout.py` (deep, pure, rarely changes).** `plan(spec) -> TreePlan{dir, branch,
  base}`. Resolves the three spec shapes — `--epic E --ws N [--slug S]`,
  `--issue N [--session S]`, `--branch <freeform>` — into:
  - **branch** (stable, no hash): `EPIC/WSnn` (e.g. `HAR02/WS02`), `issues/<id>/<session>`,
    or the freeform name — the slash-namespaced grammar (`naming.lex §3`). The epic
    (umbrella) branch is `EPIC/umbrella`, never bare `EPIC`: that keeps the epic branch a
    sibling of its work streams under `refs/heads/HAR02/` instead of colliding with them
    (a ref can't be both a file and a directory), so the coordinator's epic branch and the
    WS branches off it coexist.
  - **dir**: the branch path plus a trailing `-<agent-hash>`, under
    `~/workspace/trees/<org>/<repo>/<kind>/…`. The dir carries the hash; the branch never
    does. (Rationale: two sessions on one branch is fine; two Trees in one dir is not.)
  - **base ref**: `origin/<EPIC>/umbrella` for a work stream, else `origin/main`.
  - Slug sanitization (lowercase, `/`,`.`,`:`,space → `-`) lives here.
- **`tree/create.py` (deep orchestrator).** `create(spec) -> Tree`. Pipeline:
  (1) source = `git clone --reference <local> --dissociate <github-url>` (ADR-0014); the
  reflink-template path is a deferred future optimization (ADR-0015), not built here.
  (2) `git fetch origin`; `git checkout -b <branch> <base>`.
  (3) apply `.treeinclude`.
  (4) provision: `shipit install` + the path's `pixi install` / `npm ci`.
  (5) emit a READY summary `{path, branch, base}`. The clone-strategy complexity hides
  behind this one call. `origin` always points at the GitHub URL so `gh` works unchanged.
- **`tree/registry.py` (scan-based, no manifest).** `scan(root) -> [TreeRecord]`. Walks the
  central root and reads each clone's branch, base, dirty flag, ahead/behind, and (via
  `gh.py`) PR state. There is deliberately **no manifest file** — the clones on disk are
  the whole store, consistent with shipit's stateless ethos (cf. the PR engine).
- **`tree/cleanup.py` (deep, pure).** `classify(records, now, pr_states) -> {removable,
  stale, keep}`. A Tree is **removable** only when its PR is merged on the remote AND the
  working tree is clean AND there are no unpushed commits AND its mtime is older than a
  threshold. Anything failing those but looking abandoned is **stale** (listed, never
  auto-removed). Everything else is **keep**. The effectful removal (`tree/registry` or the
  verb) consumes this decision; the decision itself is pure and table-tested.
- **`tree/include.py` (shallow).** `parse(.treeinclude) -> [path]` (gitignore syntax,
  repo-root file; patterns are evaluated **relative to the repo root**, like `.gitignore`,
  so a leading-`/` anchors to the repo root) then copy/reflink exactly those
  gitignored-but-needed files
  (`.env`, Doppler config, models) from the source checkout into the new Tree. A Tree is
  self-contained and disposable — secrets are **copied, not symlinked**.
- **`verbs/tree.py` (thin).** click wiring over the above, registered in `cli.py` beside
  the existing verbs.

**Enforcement — extend `src/shipit/harness/policy.py` (deny, not redirect).** Add
table-driven deny rules to the existing PreToolUse policy:

- the **`EnterWorktree`** tool call → deny;
- a Bash command matching **`git worktree add`** → deny;
- each with a **deny reason** redirecting to `shipit tree create` and citing ADR-0014.

Ordinary git/`gh` commands are unaffected. The coordinator never passes the `--worktree`
launch flag, so the native path is simply never invoked. Redirecting via the
`WorktreeCreate` hook was rejected — it would couple shipit to an undocumented Claude Code
hook contract; denial uses only the stable PreToolUse surface shipit already owns.

> **Corrected by Trees v2 / shipit-owned subagent spawning (ADR-0017).** The "undocumented
> `WorktreeCreate` hook contract" premise above is **wrong** — a feasibility spike on #139
> confirmed the hook **is documented and stable**, and a later live probe (Claude Code
> 2.1.196, pinned in `verbs/hook/worktreecreate.py`) verified its exact payload: CC fires
> the hook with the spawn id in the **`name`** field and then adopts the bare path the hook
> prints to stdout as the subagent cwd **without validating it** — which is precisely what
> lets the demoted hook relocate an in-CC spawn into a dissociated-clone Tree. More
> importantly the enforcement story moved on: this deny-only stance is now paired with a
> **positive** path — the coordinator provisions and launches every Run through
> `shipit spawn subagent`, which mints the Tree and roots the child agent in it (the native
> worktree path stays denied). The full successor Spec comes later via `/to-spec`; see
> ADR-0017 (spawning), ADR-0018 (write vs read-only Trees), and ADR-0019 (the
> headless-`claude` launch contract that settles the launch mechanism ADR-0017 left open).

**Config.** The central root defaults to `~/workspace/trees` and is overridable. `.treeinclude`
is a repo-root file in gitignore syntax (a separate file, not a `.shipit.toml` table, so it
stays close to `.gitignore`). The sccache settings (`SCCACHE_BASEDIRS`, `CARGO_INCREMENTAL=0`,
per-Tree `target/`) are environment/provisioning config applied by `create`, per ADR-0015.

**Ownership.** The coordinator provisions and assigns; implementer/shepherd Runs start
inside a ready Tree and never self-provision; explorers are exempt. (Extends the **Role**
registry in `CONTEXT.md`; no new ADR.)

## Testing Decisions

A good test here asserts **external behavior**, not internals: given an input spec/state,
the module returns the right plan/partition/decision — never "it called `git` with these
flags." Prior art: `prstate/state.py` (pure snapshot→state), `harness/policy.py` (deny
decisions), `config.py` (parse/validate), all table-driven.

Tested modules:

- **`layout.plan`** — every spec shape (epic+ws, issue, freeform); hash lands on the dir and
  never on the branch; base ref resolution (`origin/<EPIC>/umbrella` for a work stream vs
  `origin/main`); slug sanitization edge cases.
- **`cleanup.classify`** — the partition truth table: merged+clean+no-unpushed+aged →
  removable; dirty → keep; unpushed → keep; unmerged PR → keep; abandoned-but-ambiguous →
  stale (never removable).
- **`include.parse`** — `.treeinclude` gitignore-syntax → resolved file list, including
  negations and globs.
- **policy deny-rules** — `EnterWorktree` and `git worktree add` → deny with the redirect
  reason; ordinary `git status` / `gh pr create` → allow.
- **`registry.scan`** — fixture directory layouts → expected `TreeRecord`s (branch, dirty,
  ahead/behind), including a non-Tree dir being ignored.
- **One integration smoke for `create`** — a real `git clone` into a tmp dir: asserts the
  result is an independent clone (no `alternates` after `--dissociate`), is on the planned
  branch, `origin` points at the remote, and `.treeinclude` files were copied. Kept to a
  single happy-path; the strategy details are covered by the pure unit tests above.

## Out of Scope

- **The reflink "template Tree" warm-start.** Deferred to a future optimization (ADR-0015);
  v1 is dissociated-clone + sccache. No freshness daemon, no `cp -c` template path.
- **Runtime/container isolation** (ports, databases, per-Tree services). Trees give *file*
  isolation; the cloud-agent VM-per-task model is not in scope.
- **Non-APFS / cross-filesystem reflink handling.** Only relevant once the template lands.
- **Automatic issue creation.** A Tree presupposes an epic/WS or issue exists; minting the
  issue is the existing triage/planning flow, not `shipit tree`.
- **Multi-machine Tree distribution.** Trees are local to one host; syncing across machines
  is out of scope (origin remains the cross-machine sync point).
- **Migrating or cleaning up the existing 25 `.claude/worktrees` + 32 `worktree-agent-*`
  branches.** A one-time cleanup is worth doing but is separate operational work, not part
  of this feature.

## Further Notes

- The design was validated empirically in-session: a `--reference` clone into the central
  root was **1.9 MB and instant** (sharing objects via `alternates` before `--dissociate`);
  APFS reflink copied **200 MB in 0.00 s** with zero incremental disk (the basis for the
  deferred template). The current shared checkout measured **14 GB** working tree against a
  **22 MB** `.git`, which is the core argument for clones-over-worktrees (ADR-0014).
- This PRD is the spec; the epic issue + Work Stream sub-issues are produced later by
  `/to-tickets`. The epic code (`THEME+NN`) is assigned by the human at that step.
- Relevant ADRs: **0014** (Trees are dissociated clones in a central root, not worktrees),
  **0015** (per-Tree `target/` + sccache cross-Tree cache; template deferred).
