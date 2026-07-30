- harness: the `PreToolUse` guard now sees `Bash` and `Agent` calls (#1182). The
  managed matcher was `Edit|Write|MultiEdit|NotebookEdit`, so on the Claude Code
  path the two deny rules written to enforce ADR-0014 — `git worktree add` and
  `EnterWorktree` — were unreachable dead code. The managed entry splits in two,
  letting the host's own matcher pick the failure mode: the edit entry is
  unchanged byte for byte and still **refuses** the call when the guard cannot run
  (ADR-0038), while a second entry matching `Bash|Agent` runs `shipit hook
  bashguard` and **allows** it. The asymmetry is the point (ADR-0080): failing
  closed on edits costs "you cannot edit", failing closed on `Bash` would cost
  every shell command in the session including the ones that repair pixi, and
  `Bash` is 100% unchecked today, so allowing is never worse than the status quo.
- harness: a subagent spawn for a role that requires its own Tree is refused
  unless it passes `isolation`. Omitting the parameter runs the subagent with the
  **caller's** checkout as its cwd, which is the wholesale-sharing failure two
  concurrent write Runs stomp each other through. The rule is derived from the
  `roleprofile` registry's `tree_backed` flag rather than a second hand-kept role
  list, so `explorer` — the one role that runs in the ambient WorkingDir — passes
  by construction. It covers shipit's five roles only: `general-purpose`,
  `claude`, `Explore`, `Plan` and `fork` resolve to no role profile and are always
  allowed, `fork` necessarily so, since it inherits the parent's context and
  cannot be isolated.
- harness: the worktree deny rules now fire on codex too. They matched only a
  tool named `bash`, and codex names its shell tool `exec_command` and puts the
  command under `tool_input.cmd` rather than `tool_input.command` (both observed
  on codex-cli 0.146.0). Tool names now come from a `_SHELL_TOOLS` registry, the
  same shape `_EDIT_TOOLS` already used to carry codex's `apply_patch` beside
  Claude Code's `Edit`, and the payload projection reads either command key. The
  codex entry itself stays matcherless and fail-closed, because codex only honours
  a project `hooks.json` whose trust was granted in its TUI, so its matcher
  behaviour could not be verified — and a wrong matcher there would silently
  disable the codex edit guard.
- harness: the `PreToolUse` payload's `cwd` is read for the first time and carried
  into the deny decision and its log line, so a deny now says which checkout the
  call came from.
