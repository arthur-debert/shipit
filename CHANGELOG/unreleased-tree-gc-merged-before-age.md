- `tree gc` now **reclaims a merged Tree without waiting out the age
  threshold** (#1009). The write ladder gated on age BEFORE it looked at the PR,
  so a Tree whose PR merged days ago — clean, nothing unpushed, the work safely
  on the remote — was kept purely because its directory mtime was under the
  two-week boundary. At a real merge rate that parks a fortnight of finished
  work: measured over a 503-Tree fleet, **421 Trees were kept by the age gate
  alone** while exactly one had a PR in flight, and the `kept: 500` the verb
  reported read as "500 Trees in use" when it only ever meant "500 Trees are
  recent". The gate was measuring throughput, not use.
  A merged PR is now decided FIRST, held only until the Tree has been **idle for
  12h**: the merge already proves the loss is safe, and the window covers the one
  thing age was really buying — a write Tree has no liveness signal (unlike an
  ephemeral session Tree, which has its pidfile), so an agent may still be
  working in a still-clean Tree whose PR has merged. That window's clock is time
  since the Tree's last local write, NOT time since the merge: what it needs to
  know is whether anyone is still working in the Tree, and idleness is the
  available proxy. Hours of idleness instead of weeks of age closes that hole
  without parking the fleet. This brings the write ladder in line with the
  ephemeral one (ADR-0027), which already checked the merge ahead of its liveness
  and age rungs.
  `--threshold` (14d by default) is unchanged and still governs the **unmerged**
  shapes — no PR, or a PR closed without merging — where age remains the only
  abandonment signal, and those still land in `stale` for a human rather than
  being deleted. Every never-lose-work guarantee is untouched: a dirty tree,
  unpushed commits, an unreadable commit list, an in-flight PR, or an unreadable
  PR state all still keep, whatever the Tree's age or merge state.
