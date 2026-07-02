# Coordinator session Tree — ephemeral, work-by-branch, via `--worktree`

> **Amends ADR-0017.** That ADR demoted Claude Code's `WorktreeCreate` hook to "a
> convenience adapter for throwaway in-CC Claude helpers." This ADR elevates it: the same
> hook now also legitimately provisions the **coordinator's own session Tree** — the one
> Tree `shipit spawn subagent` structurally *cannot* mint, because it is the top-level
> session's, and the session's cwd is fixed before any shipit code runs.

The coordinator (the top-level, human-facing session) gets its **own isolated Tree** at
launch — a **session Tree** — so two sessions on one repo never share a working tree,
index, or HEAD. It is minted by launching `claude --worktree <id>` (or `-w`), which fires
shipit's existing `WorktreeCreate` hook; the hook returns a dissociated clone off
`origin/main` on branch `ephemeral/<id>`, and Claude Code adopts that path as the session
cwd. The session Tree is **ephemeral-by-path, work-by-branch**: its directory identity is
the *session* (`ephemeral/<id>`, disposable, never renamed); the *branch* checked out
inside it becomes the real work (`docs/<slug>`, `EPIC/umbrella`, …) as the session
discovers what it is doing.

## Context

Trees isolate every spawned **Run** (ADR-0014/0017/0018), but the **coordinator run**
itself has always executed in the plain repo checkout — its cwd. Nothing provisions a Tree
for the session itself; `shipit spawn subagent` provisions Trees *for the Runs the
coordinator launches*, never for the coordinator. So two coordinator sessions started in
the same checkout share one working tree: the moment one switches branches or dirties the
index, it corrupts the other. Parallel sessions on one repo — the thing Trees exist to
enable everywhere else — were impossible at the top level.

A live spike (Claude Code 2.1.198) settled the two facts the design turns on:

1. **The session cwd is immutable after launch, and no hook can change it** (`hooks.md`:
   *"Changing the working directory within a hook does not affect Claude Code's subsequent
   operations."*). So there is **no in-session fix** — isolation must be established
   *before* the process starts. A manual `shipit tree create` + `cd` cannot relocate a
   running session.
2. **`claude --worktree` fires the same `WorktreeCreate` hook** as in-session
   `Agent(isolation:"worktree")` spawns, with the same payload (`{session_id,
   transcript_path, cwd, hook_event_name, name}`, `name` = the `--worktree` value), and CC
   **adopts the hook-printed path as the root session's cwd** — verified with a
   hook-substituted non-worktree dir. So shipit's existing fail-closed hook, unchanged in
   shape, already relocates the coordinator into a dissociated-clone Tree.

At launch the work is almost always **unknown** — the session may be for planning, triage,
or exploration before any epic/issue exists. So there is nothing to bind the Tree to but
`main`; a session Tree is *inherently* ephemeral.

## Decision

**The coordinator gets an ephemeral session Tree, minted through the `WorktreeCreate`
hook.** This draws a clean line through the two Tree entry points:

- `shipit spawn subagent` provisions Trees **for other Runs** (implementer / shepherd /
  reviewer) — intent passed as arguments, Claude-and-non-Claude backends, result via PR.
- the `WorktreeCreate` hook provisions the **coordinator's own** session Tree — because
  `--worktree` is the *only* pre-launch seam that can set the immutable root cwd. This is
  no longer "throwaway"; it is the coordinator's primary workspace.

The session Tree is **ephemeral-by-path, work-by-branch**:

- **Directory = the session.** `<root>/<org>/<repo>/ephemeral/<id>`, one per launch,
  disposable, **never renamed**. Renaming a live clone's dir is a dead end anyway — the
  running process keeps its cwd inode and anything resolving `$PWD` breaks — and it buys
  nothing, since the Tree is disposable.
- **Branch = the work.** The clone starts on `ephemeral/<id>` (base `origin/main`) and, as
  the session learns its task, the coordinator switches branches *inside the clone*
  (`git fetch && git checkout -b EPIC/umbrella origin/EPIC/umbrella`, or a `docs/<slug>`
  for a planning PR). It is a full independent clone with `origin` set, so commit / push /
  PR / merge all work against the remote, isolated from every other session.

There is **no mid-flight path move** — it is impossible (immutable cwd) and unnecessary
(switch the branch instead). The consequence, accepted deliberately: once the branch is
switched, the dir name (`ephemeral/<id>`) and the branch (`EPIC/umbrella`) **stop
mirroring**. `shipit tree list` reads the branch from live git HEAD, so it always shows the
real branch; nothing is lost but the cosmetic dir↔branch symmetry.

## Considered options

- **Manual `shipit tree create` + `cd` at session start** (the pre-existing "epic Tree at
  session start" step). Rejected as an isolation mechanism: a `cd` inside a running session
  cannot move the immutable session cwd, so the coordinator would still be rooted in the
  shared checkout. This ADR **retires that step for the coordinator's own workspace** — the
  session Tree replaces it.
- **Bind the session Tree to an epic/issue at launch** (`-w TRE05` → check out the
  umbrella). Rejected as the *model* (kept only as an optional convenience): the work is
  usually unknown at launch, and binding is unnecessary anyway — a full clone can switch to
  any branch mid-session. One shape (ephemeral) beats a launch-time dispatch on a name the
  user rarely knows yet.
- **Rename the Tree path once the work is known.** Rejected: fragile (cwd inode / `$PWD`),
  and pointless for a disposable Tree. Branch-switch within the fixed path is the mechanism.
- **A wrapper script that sets cwd + env, then `exec claude`.** Not needed for isolation:
  CC's own `--worktree` flag + the existing hook already do it. A thin `./claude-start`
  alias may still ship as ergonomics, but it wraps the flag, not bespoke worktree/env logic.

## Consequences

- **Reclaim is liveness-based with liveness-independent backstops — not PR-based.** An
  ephemeral session Tree has no PR, so the standard `gc` ladder (merged → removable, else
  stale) would strand it in *stale* forever; and it is often *clean* (a planning session
  that hasn't committed), so "clean + aged" alone would let `gc` delete a Tree out from
  under a **live** idle session. The `ephemeral` kind therefore gets its own `gc` rule — a
  precedence ladder (mirroring the read-only-Tree rule's shape), first match wins:

  1. **dirty or unpushed → KEEP.** Absolute floor — never lose local work (the existing
     removal gate, unchanged). **"Unpushed" means "has local commits not present on any
     remote," not "lacks an upstream-tracking branch."** A fresh `ephemeral/<id>` branch has
     no upstream initially, so an upstream-based test would read every session Tree as
     forever-unpushed and never reclaim it. The safe definition: a branch with no commits
     beyond its base ref that exist nowhere on a remote has nothing to lose — the missing
     upstream is not itself a reason to keep.
  2. **branch has a merged PR → REMOVABLE.** Once the session switched to real work
     (`docs/…`, `EPIC/umbrella`) and it merged, "done" is provable — reuses the existing
     merged-PR classify; nothing is lost, it is on `main`.
  3. **live session ∧ younger than the hard cap → KEEP.**
  4. **hard time cap (~4 days), clean + pushed → REMOVABLE even if the pidfile claims
     live.** A clean session idle for days is abandoned in practice; this escape hatch
     overrides liveness so a wrong/forgotten/stale pidfile can never strand a Tree forever.
  5. otherwise (not live, clean, pushed) → **REMOVABLE**, past a short grace window so a
     just-launched session is not raced before its pidfile lands.

  **Liveness** is a pidfile the SessionStart hook writes into the Tree recording the
  `claude` session's PID, its `session_id`, and the PID's **process create-time** (read
  from the OS at write time, *not* wall-clock "now" — the hook fires slightly after the
  process starts, so the two are close but not equal). `gc` treats a Tree as live only when
  the PID is alive **and** the process looks like the recorded Claude Code session **and**
  its create-time matches the recorded value **within a small tolerance** (absorbing
  measurement/clock granularity). The "looks like Claude Code" test must **not** assert the
  OS process *name* is exactly `claude`: Claude Code is a Node.js app, so the reported comm
  is often `node` (or a versioned node path). The robust check matches the **command line**
  (argv contains the `claude` entrypoint) — or, since the recorded **create-time is already
  a strong per-PID identity**, treats create-time-within-tolerance as the primary signal and
  the name/argv as corroboration, so a `node`-named live session is never misread as dead.
  This is immune to reboot / PID-reuse at near-zero cost: a reused PID belonging to some
  other process fails the create-time (and argv) test → treated as dead → reclaimable. PID reuse
  is the *safe* direction anyway (`gc` deletes directories, never processes: PID-alive →
  keep, so a false "alive" only lets a dead Tree linger, it never deletes a live one).
  pixi offers **no** liveness or env-GC signal (verified KB: pixi persists only static
  state, keyed on nothing joinable), so the pidfile fills a real gap rather than
  reinventing pixi.

  `WorktreeRemove` (and `Stop`/`SessionEnd`) remove the pidfile + Tree on the **fast path**,
  but `WorktreeRemove` did **not** fire in headless mode in the spike, so teardown cannot
  rely on it — the `gc` ladder above is the load-bearing cleanup, the hook only the fast
  path.
- **The docs encode the *opposite* model in several places and must be reconciled as part
  of this work, not after it:** CONTEXT.md ("coordinator provisions its own *epic* Tree at
  session start"; the tree↔branch mirroring invariant; the "throwaway adapter" language),
  `docs/dev/naming.lex` (needs the `ephemeral/` shape + the dir=session / branch=work
  split), `docs/dev/epics.lex` (the manual hand-run step), and the docstrings in
  `tree/layout.py`, `verbs/hook/worktreecreate.py`, `harness/worktree_adapter.py`.
- The `WorktreeCreate` hook must fork on **who it is serving**: the coordinator's own
  session (→ ephemeral branch off `main`) vs a spawned in-CC helper (→ the existing
  `<epic>/agent-<id>` holding branch). The `name` payload field is how they are told apart.
- Native `git worktree` / `EnterWorktree` stays **denied** (ADR-0014); `--worktree` routes
  through the hook, so it is the *supported* path, not the denied one.
- **This is not shipit-specific.** The session Tree + coordinator-env activation is a
  general capability delivered to **every managed repo** via the hook set `shipit install`
  lays down (shipit-self is just the first install): working in `lex`, the root session is
  equally a session Tree in `lex` with `lex`'s environment activated. Layer B is already
  repo-agnostic (the hook resolves org/repo from the ambient checkout; `tree create` gates
  provisioning on which manifests exist). Coordinator-env activation is therefore
  **toolchain-aware** — a `shipit hook sessionstart` that emits the right activation into
  `CLAUDE_ENV_FILE` (`pixi shell-hook` for a pixi repo, a graceful **no-op** where there is
  no activatable toolchain, extensible per toolchain), not a hardcoded `pixi shell-hook`
  that would error on a non-pixi consumer.

Layers A + D implemented in SES01 (WS01 `harness/activation.py` + `verbs/hook/sessionstart.py`;
WS02 `data/bootstrap/claude-start` + `shipit install` wiring); Layers B (ephemeral Tree) and C
(liveness/gc) pending (SES02).
