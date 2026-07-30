- harness: the `PreToolUse` guard now sees `Bash`, `Agent` and `EnterWorktree`
  calls (#1182). The managed matcher was `Edit|Write|MultiEdit|NotebookEdit`, so on
  the Claude Code path the two deny rules written to enforce ADR-0014 — `git
  worktree add` and `EnterWorktree` — were unreachable dead code. The managed entry
  splits in two, letting the host's own matcher pick the failure mode: the edit
  entry is unchanged byte for byte and still **refuses** the call when the guard
  cannot run (ADR-0038), while a second entry matching `Bash|Agent|EnterWorktree`
  runs `shipit hook bashguard` and **allows** it. The asymmetry is the point
  (ADR-0080): failing closed on edits costs "you cannot edit", failing closed on
  `Bash` would cost every shell command in the session including the ones that
  repair pixi, and `Bash` is 100% unchecked today, so allowing is never worse than
  the status quo. Because a rule is only as live as the matcher that routes it, the
  tool names the rules fire on are now declared as data and a wiring-level test
  asserts the managed matchers cover all of them — a pure-verdict test cannot catch
  an unrouted rule, since it bypasses the matcher entirely.
- harness: the `git worktree add` check no longer depends on where in the command
  the invocation sits. It asked whether a command *segment* began with `git`, so
  anything ahead of `git` defeated it: a newline (`git status\ngit worktree add x`),
  an escaped newline, a shell keyword (`if true; then git worktree add x; fi`), or a
  wrapper (`sudo`, `env`, `time`, `nice`, `xargs -I{}`). Six such bypasses were
  found in minutes, and the set is not enumerable. All of them predate this
  release; the matcher fix above is what made the rule live enough for them to
  matter.
  A second bypass of the same family: the check also walked `git`'s options to
  find the subcommand, and since `-C` consumes the following token,
  `git -C ";" worktree add ../t b` misaligned the walk. Under posix lexing a
  quoted `";"` and an unquoted `;` produce identical token streams, so that
  misalignment was not fixable by inspecting tokens — the information needed to
  tell them apart is gone before the walk starts.
  Both fixes were deletions. **Quoting already supplies the discrimination the
  structural logic was reaching for** — a quoted mention lexes to ONE word and
  can never match, while a real invocation is adjacent words. The check now
  matches adjacent `worktree` `add` words with a `git` word somewhere before
  them, counting and skipping nothing, so there is no alignment left to break.
  Line continuations are spliced out of the raw string first, which is the
  shell's own first lexical step. Segment tracking, the env-assignment prefix
  skip, the option walk and the escaped-newline special case all deleted
  themselves; the `git` word is kept so `grep worktree add file` does not match.
  It remains **best-effort, and evadable by construction**: `eval`, variable
  indirection and `sh -c` hide the words from any matcher that is not itself a
  shell. It is a hygiene nudge that redirects a cooperating agent to `shipit tree
  create`, not a security boundary. It errs toward denying, so an unquoted
  `echo git worktree add` and `git config -l ; worktree add` are now refused — a
  false positive costs one clear message, a false negative silently violates
  ADR-0014.
- harness: a subagent spawn for a role that requires its own Tree is refused
  unless it passes `isolation`. Omitting the parameter runs the subagent with the
  **caller's** checkout as its cwd, which is the wholesale-sharing failure two
  concurrent write Runs stomp each other through. The rule is derived from the
  `roleprofile` registry's `tree_backed` flag rather than a second hand-kept role
  list, so `explorer` — the one role that runs in the ambient WorkingDir — passes
  by construction. It covers shipit's five roles only: `general-purpose`,
  `claude`, `Explore`, `Plan` and `fork` resolve to no role profile and are always
  allowed, `fork` necessarily so, since it inherits the parent's context and
  cannot be isolated. A blank `isolation` reads as absent and is refused — passing
  the parameter as `""` is not passing it — while any non-blank value counts as
  isolated, because which isolation modes exist is the harness's business.
  Enforcement reaches the project Claude Code was launched in: a subagent's own
  process reads that project's settings, not its Tree's, so the entries are live
  for a Run only once installed there.
- harness: the worktree deny rules now fire on codex too. They matched only a
  tool named `bash`, and codex names its shell tool `exec_command` and puts the
  command under `tool_input.cmd` rather than `tool_input.command` (both observed
  on codex-cli 0.146.0). Tool names now come from a `_SHELL_TOOLS` registry, the
  same shape `_EDIT_TOOLS` already used to carry codex's `apply_patch` beside
  Claude Code's `Edit`, and the payload projection reads either command key. The
  codex entry itself stays matcherless and fail-closed, because a wrong matcher
  there would silently disable the codex edit guard; the remainder is tracked in
  #1186.
- harness: the `PreToolUse` payload's `cwd` is read for the first time and carried
  into the deny decision and its log line, so a deny now says which checkout the
  call came from.
