# The `PreToolUse` guard splits into two entries; only the edit entry fails CLOSED

> **Status: Accepted.** Fixes #1182. **Scoped amendment to ADR-0038** (the
> `PreToolUse` wrapper fails closed): ADR-0038's never-silent-allow invariant is
> retained verbatim for the **edit** entry, whose command is unchanged byte for
> byte. A **second** managed `PreToolUse` entry is added for the tool families
> ADR-0038 never covered, and that entry is **advisory on failure** — it exits 0.
> Also amends ADR-0012's tool-name registry (the codex adapter's shell tool name)
> and instantiates ADR-0014 and ADR-0047 by making their rules reachable.

The managed matcher was `Edit|Write|MultiEdit|NotebookEdit`, so on the Claude
path no `Bash`, `Agent` or `EnterWorktree` call ever reached the guard. Two rules
written to enforce ADR-0014 — `git worktree add` and `EnterWorktree` — were
therefore dead code, and the mandatory-isolation rule ADR-0047's registry makes
possible had nowhere to run. Widening the matcher raises the question ADR-0038
answered for edits: what happens when the guard cannot run?

## Decision

The managed `PreToolUse` entry becomes **two** entries, and Claude Code's own
matcher does the discrimination:

| matcher | command | on `rc != 0` |
| --- | --- | --- |
| `Edit\|Write\|MultiEdit\|NotebookEdit` | `shipit hook pretooluse` | `exit 2` — refuse (ADR-0038, verbatim) |
| `Bash\|Agent\|EnterWorktree` | `shipit hook bashguard` | `exit 0` — allow, with a message on stderr |

Both run the same total decider; `bashguard` exists as a second name only
because the install marker (`SETTINGS_HOOK_MARKER`) is matched as a **substring
of the command**, so a second command containing `shipit hook pretooluse` would
be stripped by the first unit and reconcile would flap forever.

### Why the asymmetry is not a weakening of ADR-0038

ADR-0038 is right for the case it was written for (#529: a silent fail-open let
an entire epic run unguarded). The severities are not symmetric.

- Fail-closed on **edits** protects the one thing the guard exists for. Its cost
  is "you cannot edit", which is recoverable from inside the session.
- Fail-closed on **Bash** would protect ADR-0014 hygiene — a stray `git worktree
  add`, recoverable, no data loss — and its cost is that *the session cannot run
  any shell command, including the ones that repair pixi.* That is not
  recoverable from inside the session; a human must fix it from outside.
- The trigger is realistic, not hypothetical: ADR-0038's own deliberately-open
  point is that a pixi-less consumer's guard never runs and only ever refuses.
- Decisive: **`Bash` is 100% unchecked today.** Fail-open for `Bash` is never
  worse than the status quo and never touches the edit guard, while fail-closed
  for `Bash` would invent a bricking mode that has never existed.

`Agent` rides with `Bash` on the same principle: new coverage must never regress
availability.

## What the second entry enforces

1. **ADR-0014**: `git worktree add` and `EnterWorktree` are refused. Trees are
   dissociated clones, not git worktrees. See the limits below — the shell-command
   half of this is a nudge, not a boundary.
2. **Mandatory isolation (ADR-0047)**: an `Agent` spawn whose `subagent_type`
   resolves to a Role whose profile is `tree_backed` is refused unless
   `tool_input` carries a **non-blank string** `isolation`. Absent, blank, and
   non-string all read as absent and are refused: a blank value is not a value,
   and coercing a non-string would turn truthy junk (`{"a": 1}`, `[1]`, `1`)
   into "isolated" — failing open on the one rule this adds. The string itself
   is never inspected, because which isolation modes exist is the harness's
   business, not shipit's; only its type is. The rule reads the `roleprofile`
   registry, so `explorer` (`AmbientWorkingDir` — no Tree, ever) passes by
   construction and needs no exception.

### A rule is only as live as the matcher that routes it

**The matcher is part of the rule.** A deny rule whose tool name no managed
matcher lists is dead code no matter how correct its logic, and a pure-verdict
unit test cannot detect that — it calls the decider directly, bypassing the
matcher that decides whether the decider ever runs. This ADR's first draft
shipped exactly that bug: it widened the matcher to `Bash|Agent` and claimed
`EnterWorktree` was now enforced, while `EnterWorktree` matched neither pattern
and stayed unreachable. So the tool names the rules fire on are declared as data
(`policy.CLAUDE_GUARDED_TOOLS`) and a wiring-level test asserts the managed
matchers cover that whole set.

`EnterWorktree` is routed by the matcher and denied by the decider; **whether
Claude Code emits a `PreToolUse` event for it at all is not established here.**
Listing it costs nothing if no event ever arrives, but this ADR does not claim
observed enforcement for that tool — only for `Bash` and `Agent`, both of which
were verified end-to-end against the checked-in wrapper commands.

### The `git worktree add` rule is a hygiene NUDGE, not a security boundary

Matching a shell command is matching text, and a shell can hide text from any
matcher that is not itself a shell. `eval "git worktree add …"`, variable
indirection (`G=git; $G worktree add …`) and `sh -c '…'` all evade this rule by
construction, and no amount of case-patching closes that class — only executing
the command would. **This ADR does not claim the rule catches every route to a
native worktree.** It claims it catches the shapes a *cooperating* agent
actually types, which is what ADR-0014 hygiene needs: an agent that would have
reached for `git worktree add` gets redirected to `shipit tree create`.

That framing sets the error bias. Because the cost of a false positive is one
clear redirect message and the cost of a false negative is a silent ADR-0014
violation, the rule errs toward denying.

**The accepted false positives, by input.** These are refused and invoke no
worktree, so a reader finds them recorded rather than rediscovering them as
defects. The reasoning, reproductions and fix options live in **#1189**; this
list is the statement of what is accepted, not a re-argument of it:

| input | why it is refused |
| --- | --- |
| `echo git worktree add` | the words are unquoted, so they are indistinguishable from an invocation |
| `git config -l ; worktree add` | nothing is counted or skipped, so the `git` word and the pair need not belong to one command |
| `git "worktree\n" add x` | CR/LF at a quoted word's edge is stripped |
| `"git\n" worktree add x` | same |
| `git worktree "\n" add x` | a quoted CR/LF-only word strips to empty and drops out |

The last three qualify the "quoting supplies the discrimination" claim above:
**quoted content stays data EXCEPT for CR/LF at a word's edges.** The edge strip
exists to reproduce the shell's line-continuation word-join, but `shlex` reports
no provenance — a quoted `"\n"` and a syntactic continuation are the same
character in the same position by the time the words exist — so it cannot tell
them apart. Fixing that correctly needs a provenance-keeping tokenizer, which is
a design change rather than a normalization tweak; #1189 owns the decision,
including whether "document and close" is the right answer given the rule is not
a boundary. A quoted word whose CR/LF is *interior* keeps it, so a quoted mention
still stays one word and never matches.

**Every correctness round on this rule was a deletion, and that is the pattern
to keep.** Each shape failed because it tried to reason about a command's
*structure* from a token list, which is not enough information to do it with:

1. *Segment-relative:* asked whether a command segment STARTED with `git`, so
   anything ahead of `git` was a bypass — `sudo`, `env`, `time`,
   `if true; then …`, a newline, `xargs -I{}`. An unbounded set; review found
   six members in minutes. Deleted the segment machinery: **quoting already
   supplies the discrimination** the segment logic was reaching for, since a
   quoted mention lexes to ONE word and an invocation is adjacent words (with the
   CR/LF-at-word-edge exception recorded above). Position stopped mattering.
2. *Option-counting:* still walked `git`'s options to find the subcommand,
   remembering that `-C`/`-c` consume a following token. Under posix lexing a
   quoted `";"` and an unquoted `;` produce **identical** token streams, so
   `git -C ";" worktree add …` misaligned the walk and slipped through — and no
   token mutation could fix it, because the information needed to tell the two
   apart is gone before the walk begins. Deleted the walk: match adjacent
   `worktree` `add` words with a `git` word somewhere before them, and there is
   no alignment left to break.
3. *Token mutation:* stripping ALL shell punctuation from word edges (added to
   join an escaped newline to the following word) was what made `";"` vanish
   above. Deleted: only the continuation characters are stripped now, so a `;`
   word survives intact and cannot disappear.
4. *Custom newline lexing:* `\n\r` were put into `punctuation_chars` and taken
   out of `lexer.whitespace`, which broke shlex's native handling and forced a
   regex that spliced `\`+newline out of the raw string first. That splice let a
   comment swallow the following line: `# \<newline>git worktree add …` lexed to
   nothing and was allowed, while bash ends a comment at the newline regardless
   of the backslash and runs the next line — verified by an actual worktree
   appearing. Deleted both: the adjacency rule never needed newlines to be
   separators, because it does not care about command boundaries.

What remains is shlex's own POSIX lexing — `posix=True`, `whitespace_split`,
and `punctuation_chars` so a trailing `;` cannot hide inside the `add` word —
with exactly one reconciliation on top: continuation characters are stripped
from a word's **edges**. shlex keeps an escaped newline as a literal character
where the shell splices it out, and edge-stripping reproduces the shell in both
directions. `git \<newline>worktree add X` really does run `git worktree add X`
and create a worktree (the space before the backslash already ended the word),
so it denies; `git\<newline>worktree add X` joins into `gitworktree`, which is
not a command, so it does not. Both are pinned by a test citing that
verification, because one review round proposed flipping the first. The `git`
word is required so `grep worktree add file` does not match. The cost of that
reconciliation is the three quoted-CR/LF false positives tabled above, accepted
rather than fixed.

This is where the hardening stops. The rule's stated scope is a cooperating
agent, so a further adversarial input is out of scope by definition rather than
a defect to patch. Each round **removed** logic instead of adding a case, and
the fifth round's findings — quoted CR/LF being stripped, and a quoted empty
argument being dropped — were answered by documenting the limitation rather than
by changing behaviour: both are the same root cause, and a correct fix needs a
provenance-keeping tokenizer. **#1189 owns that root cause**, with the five-round
history and the unevaluated options; agy's trigger for spending the effort is a
second consumer of `_shell_words` appearing, not aesthetics. The next correctness
finding here belongs in that issue, not in another patch.

### Enforcement reaches the session CC was launched in, not every Tree

A subagent launched with `isolation` gets its own Tree as cwd, but its Claude
Code process resolves `.claude/settings.json` from the **project it was launched
from** — the coordinator's checkout — not from its Tree. So these entries are
live for a Run only once they are installed in that project. Observed while
verifying this change: a `git worktree add` issued from an isolated subagent
whose Tree carried the new entry was not denied.

The isolation rule covers shipit's five roles only. `subagent_type` is
frequently *not* a shipit role — `general-purpose`, `claude`, `Explore`,
`Plan`, `fork` — and those resolve to no profile and must be ALLOWED. `fork`
in particular can never be denied: it inherits the parent's context and cannot
be isolated.

## Rejected alternatives

**One entry with no matcher, sniffing the payload in the wrapper to pick an exit
code.** Rejected twice over. It puts tool-name policy into shell JSON matching,
which ADR-0012 forbids ("all rich logic in the versioned binary"), and a
substring match on the raw payload is **content-spoofable in both directions**:
a Bash heredoc containing the literal text `"tool_name":"Edit"` would fail
closed, and an Edit payload whose file content contains `"tool_name":"Bash"`
would fail OPEN — silently disabling the coordinator guard, which is exactly
the #529 failure ADR-0038 exists to prevent. Matcher dispatch keeps each
wrapper's exit code a **constant**.

**A shell-side pre-filter to cut latency.** Measured cost is ~0.27–0.30s warm per
invocation, and `./bin/shipit` directly is no faster than the `pixi run` wrap, so
there is no cheap win — and a pre-filter would put policy back in shell.
Accepted as-is.

**Fixing ADR-0038's pixi-less open point first.** Still open, still a larger
separate workstream; this ADR does not resolve it.

## The codex adapter

The rules were dead on the codex path too, for a **different** reason, and the
fix is in the binary rather than the wrapper.

shipit's managed codex entry (`codex-hooks-pretooluse.json`) carries **no
matcher**, so it fires on every tool call and the rules are already *reachable*
there. But `_matches_git_worktree_add` required the tool to be named `bash`, and
codex's shell tool is named **`exec_command`**, putting its command under
`tool_input.cmd` rather than `tool_input.command` (both observed on codex-cli
0.146.0, from codex's own `codex.tool_decision` / `codex.tool_result` records).
So the tool-name registry gains a `_SHELL_TOOLS` set — the same shape
`_EDIT_TOOLS` already uses to carry codex's `apply_patch` alongside Claude
Code's `Edit` — and the payload projection reads either command key.

The codex entry keeps **no matcher and `exit 2`**, unchanged: matcherless keeps
it a total decider, which is strictly safer, and it already gains the fix above.
Splitting it would mean putting a matcher on the entry that guards codex *edits*,
and a wrong one there would silently disable the codex coordinator edit guard —
the #529 failure again. The codex path therefore still carries the
fail-closed-on-shell exposure this ADR rejects for Claude Code. **That remainder,
and the trust-hash blocker that prevented verifying codex matcher behaviour, are
tracked in #1186.**

## Consequences

- ADR-0014's two deny rules and ADR-0047's isolation requirement are routed by a
  matcher and enforced on the Claude path for the first time — verified
  end-to-end for `Bash` and `Agent`, routed but not observed for
  `EnterWorktree`, and only in the project Claude Code was launched from.
- The shell-command rule is best-effort: it closes the whole wrapper/keyword
  class, and stays evadable through `eval`, variable indirection and `sh -c`.
  Treat it as a nudge that keeps a cooperating agent honest, and never as
  something a hostile caller cannot get past.
- An unquoted mention of `git worktree add` in a shell command now denies. The
  error bias is deliberate: a false positive costs one redirect message.
- The edit guard's contract is untouched: same matcher, same command, same
  `exit 2`, same sha.
- Every Bash, Agent and EnterWorktree call now pays one guard invocation
  (~0.3s warm).
- `payload["cwd"]` is read and threaded into the deny path, so a rule that needs
  to compare the caller's checkout against a path has the field available.
- Two managed units now share one event on one file, distinguished by `key` and
  `marker`. Neither marker may contain the other, or one unit will strip the
  other's entry on every install — asserted by the test suite.
