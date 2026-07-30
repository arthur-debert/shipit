# A cross-Tree write gets a nudge and a hand-back probe, not a boundary

> **Status: Accepted.** Fixes #1179. Adds a third rule to the **advisory**
> `PreToolUse` entry ADR-0080 created (`Bash|Agent|EnterWorktree`, fails OPEN);
> the fail-closed edit entry and ADR-0038's contract are untouched. Instantiates
> ADR-0014 (a Tree has one writer) for shell commands, and **corrects
> over-strong claims** in `AGENTS.lex` §1.2, ADR-0017 and `docs/dev/pixi.lex` §7.

A subagent launched with `Agent(isolation: "worktree")` wrote `src/shipit/git.py`
into the **coordinator's** checkout, which was sitting on an epic branch. The
mechanism is not a cwd bug: all 228 records of that Run's transcript carry its
OWN Tree as cwd. `isolation` isolates **cwd only** — the environment is inherited
wholesale, so the Run's `PATH`, `PIXI_*`, `CONDA_PREFIX` and `SHIPIT_LOG_CTX_*`
all pointed at the coordinator's Tree, a traceback printed a coordinator-Tree
path, and the agent took it for its repo root and `cd`-ed there.

That env inheritance is still live and was re-observed while writing this ADR: in
an isolated Run's shell, bare `python3 -c "import shipit; print(shipit.__file__)"`
imported the **spawning session's** editable source tree, and `PATH[0]` was that
session's `.pixi/envs/default/bin`. There is no seam to fix it on this path —
`WorktreeCreate` can only print a directory, never set the child's env — so this
ADR does not promise a scrub. (`shipit spawn subagent` does scrub, via
`launch.scrub_tree_env`; only the in-CC `Agent` path is exposed.)

## What Claude Code 2.1.220 already covers — measured, not read

Every row below was probed from a worktree-isolated Run (`T_own`) whose shared
checkout was the coordinator's Tree (`T_shared`), on Claude Code 2.1.220. This
matters because the fix's whole scope is "what the host does not already do", and
a coverage claim taken from a binary's string table is not a coverage claim.

| probe | outcome |
| --- | --- |
| `ls T_shared/CHANGELOG` | allowed — cross-checkout **reads** are not guarded |
| `cd T_shared && pwd` | allowed |
| `cd T_shared && python3 -c …` | **allowed — this is the gap** |
| `cd T_shared && git status --short` | refused: `command_redirect` ("changes directory to the shared checkout … before running git") |
| `Write` with a `file_path` in `T_shared` | refused: "Edit the worktree copy of this file instead of the shared-checkout path" |
| `Write` with a `file_path` in a THIRD checkout | **not refused** — the write was attempted (it failed only on a read-only Tree's `EACCES`, and succeeded in a throwaway repo) |
| any command mentioning `$CLAUDE_PROJECT_DIR` | refused: "too complex to verify that it stays inside the worktree" |
| `echo $HOME` | allowed — the refusal above is not a general ban on expansion |
| a heredoc, or a `>>` redirect | refused: "too complex to verify" |
| `git worktree add` in a throwaway repo | allowed — shipit's own ADR-0014 rule was not live (see reach, below) |

Three consequences, and they are the whole design:

1. **The remaining gap is exactly one shape:** an inline `cd`, or an absolute
   path, putting a **write-capable non-git command** into a checkout that is not
   the caller's own Tree. `command_redirect` matches **git**; the tool-level-cwd
   guard (`shared_checkout`) was satisfied on all 228 incident records because
   the `cd` was inline in the command string.
2. **The edit-tool boundary is real, so shipit does not reimplement it** —
   #1179's ranked fix #2 is cancelled. But it is scoped to the **shared
   checkout**: a cross-worktree `file_path` into a *third* Tree is not refused.
   That remainder is recorded here and filed as **#1190**, not fixed by this ADR,
   because adding a rule to the fail-CLOSED edit entry for a hygiene concern is
   exactly the trade ADR-0080 declined.
3. **The host's guards only exist for an agent it considers isolated.** A host
   coordinator session has no isolation worktree, so *none* of the rows above
   protects a Run's Tree from the coordinator. shipit's rule is symmetric and
   covers that direction whenever the coordinator's own cwd is a Tree — which,
   per ADR-0047, it is.

## Decision

`decide_cross_tree_write` DENIES a shell tool call when **all** of:

- the payload's `cwd` resolves to a flat Tree (`session/current.containing_tree`)
  — the caller's own Tree. A blank `cwd` reads as unknown and never falls back to
  the process cwd, because the hook process runs in the **spawning** session's
  checkout and that fallback would name the wrong Tree;
- the command names a **different** Tree, compared by Tree id;
- and either the command runs a word from a registry of write-capable non-git
  commands, or a `>`/`>>` redirect targets that Tree.

**A Tree is identified by its leaf name, not a path prefix.** A flat leaf
(`<repo>-<agent>-<stamp>-<uuid>`, ADR-0074) carries a full UUID, so a mention is
never a coincidence, and matching the leaf catches `/abs/path/<leaf>`,
`../<leaf>` and a bare `<leaf>` without normalizing anything. `layout.find_flat_leaves`
scans; `layout.parse_flat_leaf` stays the recognizer.

The scan's repo segment is an **allow-list** — the alphabet a repo name may
contain — rather than a list of characters to exclude. The first draft excluded
shell punctuation and missed `<>[]{},`, which did not lose the mention (the repo
segment simply absorbed the stray character) but shifted the reported `start` by
one and corrupted the leaf name. That silently disabled the redirect check, so
`echo x >LEAF/f` was allowed while `echo x > LEAF/f` was refused — a false
negative turning on a single space. The excluded set is unbounded; the included
one is not.

**What the write registry deliberately omits.** `git` — the host refuses a
cross-checkout `cd` before git natively, and `git -C <other Tree>` is legitimate
traffic. `rm` — `rm -rf <Tree>` *is* how a Tree is reclaimed (ADR-0014), so
denying it would break Tree cleanup. `chmod` — that is how a reviewer's Tree is
made read-only. Each omission is a deliberate uncovered write, not an oversight.

### The error bias, and the accepted false positives by input

A false positive costs one refusal carrying a redirect message; a false negative
is a silent write onto another Run's branch. So the rule errs toward denying.
These inputs are refused and write nothing, recorded so a reader finds them
rather than rediscovering them as defects:

| input | why it is refused |
| --- | --- |
| `grep -rn python3 <other Tree>/src` | a write command's NAME as data; text cannot tell a search pattern from an invocation |
| `sed -n 1,5p <other Tree>/f` | `sed` counts unconditionally; requiring `-i` would mean walking options, which ADR-0080 deleted for good reason |
| `echo "cp a b" && ls <other Tree>` | a quoted mention is still text |
| `python3 -c pass && ls <other Tree>` | a writer and a foreign Tree in one command need not be related |
| `sed.orig`, `sed.py`, `python3.orig` + a foreign Tree | a DOTTED SUFFIX is not examined, so a backup or a filename that begins with a writer's name counts as one |

That last row is a deliberate choice, not an oversight. The same lookahead is
what lets `python3.13` count, and telling a version suffix from a `.orig` suffix
means more subtlety in a regex on the exact path where added cleverness has
repeatedly produced new defects (#1189 records five rounds of it). On a
deny-biased advisory rule an over-denial costs one message, so the contract is
corrected to say what the regex does instead of the regex being bent to match a
nicer contract.

### The known-evadable inputs, by input

| input | why it passes |
| --- | --- |
| `cd <other Tree> && ruby -e …` | the writer is outside the registry, and enumerating every interpreter is not achievable |
| `cd <other Tree> && ./tools/rewrite.sh` | a script's name says nothing about what it writes |
| `cd "$(cat /tmp/target)" && python3 …` | the path is never spelled, so there is no leaf to find |
| `eval "cd $T && python3 …"` | same — the *variable* hides the path, not `eval` |
| an edit-tool `file_path` into a third Tree | native coverage is shared-checkout-scoped; this rule is shell-only |

Note the fourth row carefully: a **literal** path inside `eval "…"` or `sh -c '…'`
IS caught, because this rule reads the RAW command text rather than its shell
words. That is the opposite of ADR-0080's `git worktree add` rule, and it is why
this rule is **not** a second consumer of `_shell_words` — #1189's trigger is not
tripped. Under POSIX lexing a quoted single word and an unquoted one produce
identical tokens, so tokenizing would buy no discrimination here while losing the
`eval`/`sh -c` literal catch.

### Enforcement reaches the launch project, and only after install

ADR-0080's finding is confirmed a second time, from the other side: the Run
writing this ADR carried **both** managed entries in its own Tree, the project it
was launched from carried only the edit entry, and `git worktree add` from that
Run was **not** denied. So a Run's guards come from the launch project's
settings, and **this rule is dead for every Run until `shipit install` has run in
the project the session was launched from.** A Tree-local install buys nothing.

## The detection backstop, and what it cannot tell

`SubagentStop` (`verbs/hook/eval.py`) reads `git status --porcelain` on the
launch checkout as a Run hands back and logs the uncommitted paths. This is the
honest complement to an evadable nudge: the incident was caught only by a
coincidental `ruff format` failure, and a format-clean leaked write leaves **no
signal at all** — it is committed onto the epic branch as the coordinator's own
work.

It is a **poll, not an assertion**: a coordinator's checkout is legitimately
dirty most of the time, so the probe cannot distinguish a leak from work in
progress. It therefore reports and never refuses — which is also required by
ADR-0038 §4, since this hook is additive and fails open. Its cost is one
`git status` per hand-back.

## Attribution: a Run's records name its own Tree

A native subagent inherits `SHIPIT_LOG_CTX_SESSION`/`_TREE`, so every record it
writes was filed under the coordinator: in the incident window all 987 exec
records carried the coordinator's session and Tree, **including 18 whose `cwd`
was the subagent's Tree**. `shipit logs --flow` therefore could not tell a Run's
action from the coordinator's.

`logsetup.rebind_own_tree` re-keys `tree` — and `agent`, to the Tree id, matching
what `shipit spawn subagent` already binds — onto the Tree the process is
running in whenever that differs from the inherited export. The inherited
`session` is kept, because it genuinely is shared.

**What that is, stated exactly, because the obvious stronger claim is false.**
It attributes a record to the Tree the process **ran in**, which equals the
acting Run only while that Run stays in its own Tree. Cross-Tree reads and an
inline `cd` are deliberately allowed, so both departures are reachable: a Run
that runs `cd <inherited Tree> && shipit …` still records as its spawner, and one
that does the same into a third Tree records as that third Tree. So this
**narrows** the ambiguity to the common case — a Run working where it was put —
rather than removing it. Removing it needs a per-Run identity captured at
launch, which the host does not provide (see the limit below); **#1191 owns
that**, and until it lands `shipit logs --flow --agent-ids` should be read as
"which Tree" and not as "which agent".

**The limit, filed as #1191:** a Run that never invokes a shipit CLI command
still leaves no record, because shipit only sees what runs through it — the
rebind sharpens the records that exist, it does not create records. Per-Run
identity that does not depend on that would need the host to export a subagent
id; 2.1.220 exports `CLAUDE_CODE_CHILD_SESSION=1` (a boolean, not an id) and a
`CLAUDE_CODE_SESSION_ID` that is the **parent's**, so it cannot be derived today.

## Rejected alternatives

**Tokenize the command with `_shell_words`.** It would make this the second
consumer of the helper that took five review rounds in #1185 (#1189's stated
trigger) and would buy nothing: POSIX lexing discards the quoting provenance that
would be the point, and it would lose the literal-path-inside-`eval` catch.

**A Tree-boundary check on the edit tools.** Natively covered for the shared
checkout, and it would not have caught this write (Bash → Python script).

**Treat any absolute path outside the caller's Tree as foreign.** Refuses
ordinary reads, `/tmp` scratch and every ambient-checkout path; the Tree-leaf
comparison is the precise version of the same idea.

**Refuse on the `SubagentStop` probe.** It cannot distinguish a leak from the
coordinator's own WIP, and a wrong refusal at hand-back strands a finished Run.

**Scrub the env at spawn.** The true root cause, and there is still no seam on
the in-CC `Agent` path to do it. Not promised here.

## Consequences

- The one shape the host leaves open is refused for `Bash` (and codex's shell
  tools), by a rule that fails open and can be walked past. **It is a hygiene
  nudge plus a detection probe, and it is not a security boundary.** Nothing in
  shipit stops a determined cross-Tree write.
- Every guarded shell call pays one extra `containing_tree` resolution; the rule
  short-circuits on the tool name, so the fail-closed edit path is untouched.
- Reads across Trees stay allowed, deliberately: reading another Run's Tree is
  how a coordinator inspects work.
- Three docs that claimed more than was true now say what was measured:
  `AGENTS.lex` §1.2 ("concurrent agents never collide" — they collide via
  absolute paths), ADR-0017 ("no validation, no footgun"), and
  `docs/dev/pixi.lex` §7 (the leaked-`PIXI_*` class is closed on the
  `shipit spawn subagent` path only).
