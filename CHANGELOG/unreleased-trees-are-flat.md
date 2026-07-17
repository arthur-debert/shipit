- tree: Trees are now **flat and self-describing** — one directory per Tree named
  `<repo>-<agent>-<timestamp>-<id>` under the central root, replacing the five
  nested `<owner>/<repo>/<kind>/[<code>/]<leaf>` shapes at two depths (ADR-0074,
  #1025). Repo leads the name so `ls | grep <repo>` is the tooling-free narrowing
  the hierarchy promised; `<agent>` is the backend binary (`claude`/`codex`/`agy`),
  minted once from the backend registry rather than smuggled in as a session-id
  prefix; `<timestamp>` (`%Y%m%d-%H%M%S`) gives `tree list` its first real
  **created** column; and `<id>` is a full UUID — never a pid, never truncated. Its
  provenance follows the creator: a coordinator session Tree carries the harness
  session UUID from the `WorktreeCreate` payload (so the dir name IS the
  `claude --resume` handle), while every spawned-Run and native-helper Tree mints
  its own. The `<kind>` and `<owner>` segments and `tree_kind()` are gone (reclaim
  is one uniform activity-based rule since ADR-0072, and repo identity comes from
  the origin remote); `session/current.py` now resolves a Tree from cwd with no
  depth arithmetic; and `resume.py` reads the backend from a recorded field instead
  of reverse-engineering it from the id prefix.
- tree: **review Trees are per-Run**, not shared. ADR-0018's read-only *mode*
  stands — a reviewer still gets a chmod'd read-only clone — but the deterministic
  `(repo, branch)` sharing is dropped along with its reuse/refresh/acquisition-stamp
  machinery: each reviewer Run gets its own flat Tree, dated by its own files like
  every other Tree (#1025). Old nested Trees are not migrated — they are reclaimed
  by attrition and coexist with the flat shape (`registry.scan` walks for `.git`
  markers and never parsed depth). Branch names are unchanged.
