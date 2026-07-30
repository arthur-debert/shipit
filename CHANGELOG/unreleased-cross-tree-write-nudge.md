- harness: a shell command that runs a write-capable non-git command against a
  Tree other than the caller's own is now refused (#1179). A worktree-isolated
  Run wrote `src/shipit/git.py` into the coordinator's checkout while it sat on
  an epic branch, and nothing stopped it: `isolation` isolates **cwd only**, so
  the Run's `PATH`/`PIXI_*`/`CONDA_*` still named the spawning session's Tree,
  that Tree's absolute paths surfaced in the Run's own tool output, and the agent
  took one for its repo root and `cd`-ed there. Claude Code 2.1.220's native
  worktree guards cover the neighbouring shapes — a cross-checkout `cd` before
  **git**, a tool-level cwd outside the worktree, a cross-worktree edit-tool
  `file_path` — all measured, not assumed; the one they leave open is a
  **non-git** writer after an inline `cd`, which is exactly what did the write.
  The new rule closes that one shape and nothing more: reads across Trees stay
  allowed (that is how a coordinator inspects work), and `rm -rf <Tree>`,
  `chmod` and `git -C` stay allowed because they are how Trees are reclaimed,
  made read-only and inspected. It rides the advisory `PreToolUse` entry, so it
  fails open, and it matches text — a path assembled at run time or a writer it
  does not know walks past it. It is a hygiene nudge, not a security boundary;
  ADR-0083 lists the accepted false positives and the known evasions by input.
- harness: `SubagentStop` now reports the launch checkout's uncommitted paths as
  a Run hands back. The original leak was caught only by a coincidental
  `ruff format` failure on a file the coordinator never touched; a format-clean
  leaked write left no signal at all and would have been committed onto the epic
  branch as the coordinator's own work. The report is a poll, not an assertion —
  a coordinator's checkout is legitimately dirty most of the time — so it names
  the paths and never refuses anything.
- logs: a record is now attributed to the Tree its process ran in, rather than to
  whatever Tree the parent exported. A native subagent inherits
  `SHIPIT_LOG_CTX_SESSION`/`_TREE` from the session that spawned it, so in the
  incident window all 987 exec records carried the coordinator's identity —
  including 18 whose `cwd` was the subagent's Tree — and `shipit logs --flow`
  could not tell a Run's action from its spawner's. When the two differ, `tree`
  and `agent` are re-keyed onto the actual Tree (`agent` takes the Tree id,
  matching what `shipit spawn subagent` already binds), so `--agent-ids` and
  `--agent <id>` separate them; the inherited `session` is kept, because that one
  really is shared. **This narrows the ambiguity rather than removing it:** the
  Tree a process runs in is the acting Run only while that Run stays in its own
  Tree, and a cross-Tree `cd` — which is allowed — still misattributes. Read
  `--agent-ids` as "which Tree", not "which agent", until #1191 lands a per-Run
  identity.
- docs: three claims that outran their evidence now say what was measured.
  `AGENTS.lex` §1.2 kept "no bash-cwd footgun" (verified — all 228 records of the
  incident Run carried its own Tree) but dropped "concurrent agents never collide
  on one checkout", which is false: they collide through absolute paths.
  ADR-0017's "no validation, no footgun" now records that the `WorktreeCreate`
  hook can only return a cwd and has no seam for the child's env.
  `docs/dev/pixi.lex` §7's leaked-`PIXI_*` class is closed on the
  `shipit spawn subagent` path only — the in-CC `Agent` path has no equivalent
  seam, measured live on 2.1.220.
