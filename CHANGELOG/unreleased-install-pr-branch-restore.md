- `install --pr` now **returns the operator to their branch** when the
  reconcile adds a new managed path (#993). The reconcile commit is built on an
  isolated scratch index (#992), so a newly written managed file — the
  `.shipit-skills/` skill store, a fresh agent definition — sits on disk while
  the checkout's real index has never heard of it. Git refuses to switch away
  from a branch whose HEAD carries an untracked working-tree file
  (`error: The following untracked working tree files would be removed by
  checkout: .shipit-skills/…`), so the best-effort branch restore only logged
  the failure and left the operator sitting on the `shipit/install` scratch
  branch — the exact strand the #777 restore exists to prevent. (The pushed PR
  and the exit code were always correct, so scripted fan-out was unaffected.)
  The restore now stages shipit's own writes into the real index immediately
  before the switch, so the checkout is a plain branch change: the reconcile
  stays in the PR, the operator lands back on their branch, and their unrelated
  dirty or staged work is never touched.
