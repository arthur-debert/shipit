# Trees are independent dissociated clones in a central root, not git worktrees

A **Tree** — the isolated checkout where one write-session works — is created as a
full, independent clone in a central out-of-repo root
(`~/workspace/trees/<org>/<repo>/…`), **not** as a `git worktree` inside the repo.
The clone is made cheap with `git clone --reference <local> --dissociate <github-url>`:
`--reference` borrows the local object store so the clone is fast and offline-ish,
`--dissociate` then copies the borrowed objects and cuts the link so the result is
genuinely independent. We swim against the ecosystem current (Claude Code's own
feature and every parallel-agent tool surveyed use `git worktree`) deliberately.

## Considered options

- **`git worktree`** (the ecosystem default). Rejected: its one benefit — a shared
  object store — is worth ~22 MB here (`.git` is 22 MB; the 14 GB working tree is
  build artifacts, which worktrees don't share anyway), while it imposes two costs we
  hit daily: the same branch cannot be checked out in two worktrees (an agent can't
  sit on `main`; duplicate Trees on one branch — routine after a crashed agent — are
  impossible), and a shared object store is a concurrent-`gc` corruption hazard.
- **`git clone --reference` without `--dissociate`.** Rejected: leaves the Tree
  coupled to the source repo's object lifetime (silent corruption if the source GCs a
  borrowed object) — re-introducing the very coupling we're escaping. `--dissociate`
  is load-bearing.
- **`.claude/worktrees/` (in-repo location).** Rejected: `.claude/` is source-
  controlled; nesting checkouts there has empirically bloated the working tree (25
  nested worktrees, 14 GB) and risks committing them. The central root keeps Trees
  out of every repo and gives one place to list/clean across repos and agents.

## Consequences

- A Tree can check out **any** branch including `main`, two Trees can share a branch,
  and `rm -rf` is a safe delete (no `git worktree prune` metadata to maintain).
- `origin` points at the GitHub URL in every Tree, so `git fetch/pull/push` and all
  `gh` commands work identically to a normal clone; `--reference` only accelerates the
  initial object transfer, it never changes the remote.
- Trees don't see each other's *unpushed* local commits (no shared object store) —
  aligned with the model, which synchronizes through origin (agents push; the
  coordinator merges via PRs on the remote), not through a shared `.git`.
- Cost is ~22 MB of objects per Tree (negligible) plus a few seconds of clone time —
  the latter erased for warm artifacts by reflink-from-template (see ADR-0015).
- This flips only for a repo with a GB-scale `.git`; none of the portfolio is close.
  Revisit if one appears.
