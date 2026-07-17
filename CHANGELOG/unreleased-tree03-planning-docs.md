- **TREE03 planning docs land: the Tree gets rethought** (#1019). Running Trees
  for a while exposed three failures with one root cause — the system infers
  what it could measure, and encodes in paths what it then refuses to trust.
  `tree gc` deleted a **live** session's worktree (#1018); session memory has
  been silently discarded since Jul 6 (44 files stranded across 23 throwaway
  stores); and the directory hierarchy is written on create and ignored on read.
  Three ADRs record the decisions, and `docs/spec/tree-rethink.md` is the
  authoritative Spec:

  - **ADR-0072 — reclaim is activity-based.** One rule for every Tree kind:
    `keep if dirty || unpushed || idle < 48h`, where idle is measured
    newest-file mtime over a pruned walk. Supersedes ADR-0027's five-rung
    ladder and the pidfile liveness beneath it. Across the live fleet, idle time
    separates with no overlap — every live Tree under 1h, every dead Tree over
    41h — so the threshold sits in a chasm, and the apparatus that existed to
    manage an ambiguous middle (a `ps`/`jc` probe, a PR-state network read, four
    tunable windows) is deleted rather than fixed.
  - **ADR-0073 — the session store is per-repo, not per-Tree.** Transcripts and
    memory are keyed on the session's cwd, and a Tree per session means a new
    empty namespace every launch. One store per repo, linked into place at
    tree-create, fixes memory and resume together.
  - **ADR-0074 — Trees are flat.** `<root>/<repo>-<agent>-<timestamp>-<id>`,
    one uniform shape. No ADR ever chose nesting: it was inherited from the
    branch grammar, which is slashed for a git ref-collision reason that has no
    filesystem analogue.

  Docs reconciled with the new model: `docs/dev/naming.lex` gains a §4 for the
  flat Tree-directory grammar, `CONTEXT.md` gains **Reclaim** / **Idle** /
  **Session store** and drops read-only-Tree sharing, and both
  `docs/dev/epics.lex` §7 and the coordinator role stop telling every session
  that its memory is doomed — memory now persists, so learnings get promoted to
  the repo because the repo is how knowledge reaches reviewers, not because
  memory leaks.
