- `tree gc` now **reclaims a Tree on measured activity rather than proxies for
  it**, closing a bug that deleted a live session's worktree (#1018). One rule
  decides every Tree kind — review, ephemeral, and write alike:

  ```text
  KEEP  if  dirty  ||  unpushed  ||  idle < 48h
  ```

  The three ladders this replaces read fifteen inputs between them — a pidfile,
  a `ps` probe, the PR's state, the Tree's kind, and four separate time windows
  — to answer one question none of them measured: *is anyone working here?* The
  ephemeral ladder answered it from the clone root's mtime, which does not move
  when an agent edits under `src/` (measured lag: up to **10 hours**), and its
  last rung read age alone — so a single liveness false-negative deleted a clean,
  live Tree. `idle` is now measured directly, as the newest of any file's mtime
  under a pruned walk and `HEAD`'s commit stamp, so both an agent editing files
  and an agent committing deletions are seen.
  **Unknown is never idle.** A `git status` or `git rev-list` that fails, a walk
  that hits an unreadable directory or finds no eligible file, a `stat` that
  raises — each one KEEPS the Tree and is reported. A wrongly-kept Tree costs
  disk until the next sweep; a wrongly-deleted one costs work that no longer
  exists. That asymmetry is the whole design, and it matters more now that the
  sweep is on its way to running unattended (#1017).
  **48h is deliberately above the observed band, not inside it.** Across a live
  fleet, idle time separates with no overlap: every live Tree measured under 1h,
  every dead one over 41h. A Tree idle 41–48h simply waits for the next sweep,
  while the margin over the busiest live Tree stays 48×.
  The walk that measures this **prunes** `.git`, `.pixi`, `node_modules`,
  `target`, `.venv`, `dist`, `build`, and `__pycache__` — `.pixi` alone is ~97%
  of a Tree's file count, and unpruned the walk would cost more than everything
  it replaces. Measured across a live 155-Tree fleet: **6.8s end to end**, at
  ~7ms per Tree versus ~425ms unpruned.
  Acquiring a shared read-only review Tree now records activity, because a
  reviewer only ever *reads*: refreshing an already-current Tree rewrites no
  file, so an aged shared Tree handed to a reviewer could be reclaimed out from
  under the review that was using it.
  `--threshold` now sets the idle boundary. The `stale` bucket is gone: with one
  rule there is no ambiguous middle for a human to adjudicate.
