- **`tree gc` now makes ZERO network calls, and the dead reclaim machinery is
  gone** (#1022). ADR-0072 replaced the liveness-and-PR-state reclaim ladder with
  one activity-based rule (`KEEP if dirty || unpushed || idle < 48h`); the earlier
  work left that rule reachable but the machinery it superseded still on disk. This
  removes it, with no change to the reclaim rule itself:
  - `session/liveness.py` (the pidfile, the `ps`/`jc` fork, the create-time
    tolerance, the argv host allow-list) and `tree/provision.py` (the pre-pin
    provisioning-commit record reader) retire, along with their tests. The
    `SessionStart` hook no longer writes a pidfile and the `WorktreeRemove`
    fast-path teardown no longer reads one or carves out provisioning commits — its
    never-lose-work floor is now exactly gc's own (dirty or unpushed).
  - **The entire `gh` network dependency leaves the Tree scan.** The per-repo
    `PrIndex` batch that fed a signal reclaim no longer reads is deleted, so
    `tree gc` (and `tree list`, which shares the scan) reads only the local
    filesystem and `git`. On the largest fleet ever observed this was the
    difference between a >10-minute sweep and a ~22-second one; the cost was the PR
    read, and it is gone. A test asserts the gather makes no `gh` call.
  - `tree list` drops its **PR** column with the `gh` read; `TreeRecord` no longer
    carries `pr`/`pr_state`. The stale bucket, the per-kind gc dispatch, and the
    unreachable `live_reviews` review-Tree rung are gone with the ladder.
  - Net change is a deletion of roughly 2,000 lines across source and tests.
