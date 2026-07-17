# Rethinking the Tree

## Context

Trees have been running the fleet for long enough to judge them against what
they were designed to do. The isolation half works: ADR-0027's session Tree
means two coordinator sessions on one repo never share a working tree, index, or
HEAD, and that has held. The half built *around* isolation — a nested directory
hierarchy for navigation, a liveness-and-PR-state ladder for reclaim, and the
implicit assumption that a session's identity is its path — has not.

Three failures, each surfaced independently, share one root: **the system infers
what it could measure, and encodes in paths what it then refuses to trust.**

1. **`gc` deleted a live session's worktree** (#1018) — a real Claude process at
   ~9h elapsed, its working directory removed out from under it. This is
   data-loss-adjacent under manual `gc` and catastrophic under the #1017
   auto-trigger, which is blocked on it.
2. **Session memory is silently discarded.** The real shipit memory store froze
   on Jul 6 — the day session Trees took over — and 44 memory files are stranded
   across 23 throwaway per-session stores. Two sessions independently re-derived
   the same five memories because neither could see the other's.
3. **The hierarchy is written on create and ignored on read.** `_repo_slug`
   resolves repo identity from the origin remote precisely because the path
   "is not a reliable identity," and five clones in the live root sit at a depth
   the hierarchy does not describe.

## Problem

**Reclaim measures proxies, and every proxy is wrong.** The ephemeral ladder's
`age` comes from the clone *root directory's* mtime, which does not move when an
agent edits or commits under `src/` — measured against the live fleet, it lags
real activity by up to 10 hours. The write ladder was patched for this
(`max(root_mtime, last_commit)`); the ephemeral ladder never was. On top of that,
its last rung reads age *only*, never liveness, so any liveness false-negative —
and there are five documented ways to produce one — deletes a clean live Tree.
The result is 841 lines, 15 decision inputs, 176 tests, four tunable time
windows, a pidfile, a `ps`/`jc` probe, and a `gh` network round-trip, all
approximating one question the filesystem can answer directly: *has anyone
touched this recently?*

**Memory and resume are keyed on a directory that is different every session.**
Claude Code stores transcripts and auto-memory under
`~/.claude/projects/<cwd-slug>/`. A Tree per session means a new cwd per session,
so every launch gets a brand-new empty namespace. Nothing is read back; resume
cannot find a transcript from any directory but the one that wrote it. This is
already documented as unfixable in `coordinator.lex`, which instructs every
coordinator to hand-sweep learnings into the repo before ending — a workaround
that taxes every session's final turn and has demonstrably failed.

**The hierarchy cannot be filled in at the only moment it can be written.** The
coordinator is launched before it knows what it will do (ADR-0027's own premise),
so it cannot name its Tree after work that does not exist yet, and ADR-0027
already rejected renaming-once-known as fragile. Meanwhile the depth is not
uniform (90 four-segment clones, 52 five-segment, 5 two-segment strays), so kind
must be recovered by positional string-parsing that checks grandparent depth
before parent name.

## Goals

- **A live session's Tree is never reclaimed.** The #1018 gate; nothing else
  ships until this holds, verified against a real live session.
- **Reclaim on measured activity, not inference.** One rule, all kinds.
- **Memory and resume survive the Tree.** What a session learns outlives the
  workspace it learned it in.
- **A Tree's name tells a human what it is** with no tooling — `ls | grep shipit`.
- **Delete more than we add.** The simplification is the deliverable, not a side
  effect.
- **Claude and codex behave identically**, verified live on both.

## Non-Goals

- **Changing the branch grammar.** ADR-0016/0026 slash-namespaced refs are
  untouched; refs do not move.
- **Changing what a Tree *is*.** ADR-0014's dissociated clones in a central root
  and ADR-0027's ephemeral-by-path/work-by-branch session Tree both stand.
- **Migrating the existing fleet.** Old nested Trees are reclaimed on their own
  schedule and the shape disappears by attrition.
- **Fixing codex.** It has none of these bugs; the work is to confirm parity.

## Proposed Shape

Three separable changes, in ship order:

**1. Reclaim is activity-based** (ADR-0072). One rule replaces three ladders:

```text
KEEP  if  dirty  ||  unpushed  ||  idle < 48h
```

where `idle` is `now - (newest file mtime)` over a walk that prunes `.git`,
`.pixi`, `node_modules`, `target`, `.venv`, `dist`, `build`, `__pycache__`.
Three signals; no PR state, no pidfile, no `ps` probe, no kind dispatch.

The threshold is not a guess. Across the live fleet, idle time separates with no
overlap: **every live Tree measures under 1h; every dead Tree over 41h.** A 48h
threshold sits in a chasm, and managing an ambiguous middle is what the entire
ladder existed to do.

**2. The session store is per-repo** (ADR-0073). `tree create` plants
`~/.claude/projects/<tree-path-slug>` as a symlink to
`~/.claude/stores/<owner>/<repo>/`, before the session starts. Verified against
Claude Code 2.1.212: the symlink is followed rather than clobbered, and a session
started in one Tree resumes from a *different* Tree with its context intact. One
symlink fixes memory and resume together. Codex needs no change —
`~/.codex/memories/` is already one global store with cwd as an
`applies_to:` metadata filter rather than the storage key.

**3. Trees are flat** (ADR-0074). `<root>/<repo>-<agent>-<timestamp>-<id>`, one
uniform shape, e.g. `shipit-claude-20260717-081333-72218`. Repo first so `ls`
groups by the axis humans narrow on; agent and timestamp are already in the
ephemeral leaf today, by accident, and get promoted to deliberate fields.

## Design Decisions

The durable decisions live in the ADRs and those are authoritative:

- **ADR-0072** — Tree reclaim is activity-based. Supersedes ADR-0027's reclaim
  ladder and the pidfile liveness beneath it.
- **ADR-0073** — The session store is per-repo, not per-Tree. Amends ADR-0027.
- **ADR-0074** — Trees are flat. Amends ADR-0014, ADR-0018, ADR-0027.

Two decisions worth surfacing here because they cut across all three:

**The name is for humans; `gc` does not read it.** The timestamp in the Tree name
finally gives `tree list` a real created column, which it has never had. It is
deliberately *not* a reclaim signal: creation-age is not activity-age (the #1018
session was 9h old and fully alive), so reclaiming on it would rebuild the exact
bug being fixed. Keeping naming and reclaim policy independent is a property
worth protecting.

**The unpushed floor is retained.** A clean Tree whose commits were never pushed
looks idle; without the floor it is deleted at 48h and those commits die with
`.git`, unrecoverable. It costs one `git rev-list` (~10s across 526 Trees).

## Alternatives Considered

- **Fix the rung that failed and keep the ladder** — treats the symptom. The
  ladder would still rest on a liveness probe with five false-negative modes and
  a clock that lags by 10h.
- **Reclaim on the name's creation timestamp** — ADR-0027 rung 4 with a worse
  clock. Rejected; see above.
- **A cheap prefilter to skip young Trees before the expensive check** — safe by
  construction, but it solves a cost that no longer exists. The >10-minute sweeps
  that motivated it were `gh` (one PR read per Tree pre-#1014 at 0.5–5s each),
  not the filesystem. Deleting the PR signal takes a 526-Tree sweep from ~10
  minutes to ~22 seconds.
- **Keep the hierarchy and fix spawn discipline** — a hierarchy whose segments
  cannot be filled in at the only moment they can be written is not a hierarchy.
- **Copy memory into each new Tree at create** — forks the store; sharing is the
  requirement and copying is its opposite.

## Risks And Rabbit Holes

- **The store rides undocumented harness internals.** `~/.claude/projects/<cwd-slug>/`
  is not a published contract. The failure mode is benign and visible — the
  symlink stops being consulted and behaviour reverts to today's partition — and
  the alternative is a store that is already, measurably, dead.
- **`MEMORY.md` becomes contended.** One store per repo means two parallel
  sessions can both rewrite the index and the later write wins, losing a line.
  Transcripts are UUID-named and cannot collide. Accepted, not solved.
- **The prune set is load-bearing.** Naive walks cost 191.7ms/Tree against
  1.9ms pruned — `.pixi` alone is ~97% of the file count. An unpruned walk is
  slower than everything this work deletes.
- **Accepted residual: a live session writing no file for 48h is reclaimed.**
  The #1018 session (9h of purely external `gcloud` work) survives comfortably;
  the rule reclaims only a session idle two full days. If this ever bites,
  `~/.claude/sessions/<pid>.json` — which the harness already writes with
  `{pid, cwd, updatedAt}` — is a truer heartbeat than the pidfile ever was, and
  needs no `ps` fork. Noted as an escape hatch, not a dependency.
- **Test churn is the flat layout's real cost.** `test_tree_layout.py` is a truth
  table over dir shapes.

## Testing / Verification

**The load-bearing verification cannot be done by subagents.** A subagent
inherits an already-launched session, so the pre-launch cwd seam — `claude
--worktree` firing `WorktreeCreate`, the only thing that can set the immutable
session cwd (ADR-0027) — is structurally invisible to it. The store symlink is
planted at exactly that seam, and "gc must not delete a live session" is only a
real test against a real live session.

So the epic ends with a **cold root session** launched for both `claude` and
`codex`, each spawning subagents and running diagnostics, keeping its Trees for
external inspection, and reporting back. Asserted live:

1. The Tree is created flat, with the ADR-0074 name shape.
2. The store symlink is planted at create, and memory written mid-session lands
   in the per-repo store.
3. Resume works from a *different* Tree of the same repo.
4. **The #1018 regression, live:** a real `gc` sweep runs against a real live
   session and its Tree survives.
5. Codex parity on naming and tree create, with its global memory store
   untouched.

## Out Of Scope

- The #1017 auto-trigger itself. It stays blocked until #1018's regression test
  is green, then ships unchanged on top of this work.
- Migrating existing nested Trees.
- Any change to codex's memory or session storage.

## Further Notes

- **ADRs**: [ADR-0072](../adr/0072-tree-reclaim-is-activity-based.md),
  [ADR-0073](../adr/0073-the-session-store-is-per-repo-not-per-tree.md),
  [ADR-0074](../adr/0074-trees-are-flat.md).
- **Amends/supersedes**: ADR-0014 (central root interior), ADR-0018 (review Tree
  sharing dropped; read-only *mode* stands), ADR-0027 (reclaim ladder, dir shape,
  session store).
- **Issues**: #1018 (the live-deletion bug this closes), #1017 (auto-trigger,
  blocked on it), #1011/#1012/#1014 (the gc sweeps whose `gh` batching this
  makes moot).
- **Latent bug mooted rather than fixed**: `live_reviews` is accepted by
  `classify` and branched on by `_review_bucket`, but `plan()` never forwards it,
  so every review Tree reads `reviewer_live=False` in production. The rung is
  unreachable outside tests and disappears with the ladder.
