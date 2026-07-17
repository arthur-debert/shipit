# Trees are flat: one directory, one self-describing name

> **Amends ADR-0014** (the central root survives; only its interior shape
> changes), **ADR-0018** (shared read-only review Trees become per-Run Trees),
> and **ADR-0027** (the `ephemeral/<id>` dir shape). The dir grammar lands as
> `docs/dev/naming.lex` §4. Branch grammar (ADR-0016, ADR-0026) is **unchanged** —
> refs do not move.

Trees live at `<root>/<owner>/<repo>/<kind>/[<code>/]<leaf>` — five shapes at two
different depths, whose kind segment must be recovered by positional
string-parsing, whose owner segment is written on create and deliberately
ignored on read, and whose leaf is chosen by an agent that does not yet know what
it is going to do. The hierarchy costs real code and returns navigation that
does not survive contact with how sessions are actually spawned.

## Context

**No ADR ever chose nesting.** This is the finding that reframes the decision.
ADR-0014's "Considered options" is a three-way between `git worktree`,
`--reference` without `--dissociate`, and `.claude/worktrees/` — a *clone
strategy* and *location* comparison. Its recorded rationale, "one place to list
and clean across repos and agents," argues for a **central root**, not for depth.
`<owner>/<repo>/<kind>/…` arrives in that ADR fully formed, as an unargued
parenthetical. ADR-0018 is the closest thing to a justification and explicitly
disclaims itself: naming "mirrors the slash-branch namespace (**context, not a
new decision here**)."

So the dirs mirror the refs — and the refs are slashed because git cannot hold a
`refs/heads/GPU02` file and a `refs/heads/GPU02/WS03` directory at once
(ADR-0016). **That constraint has no filesystem analogue.** The shape was
inherited from a problem it does not have.

**The path is already distrusted as identity, in shipit's own code.**
`_repo_slug` (`registry.py:256-276`) resolves the repo from the **origin
remote**, and its docstring says why: the path shape "is not a reliable identity:
real fleets carry hash-named roots (`<root>/2f86/shipit/…`) whose first segment
is no GitHub owner at all. Parsing those would either drop them from the batch or
build a bogus `2f86/shipit` slug; reading the remote makes them ordinary." The
owner/repo segments are written by `repo_dir` and then not read back. The live
root confirms it: five such roots exist right now (`2f86`, `5c08`, `8f0e`,
`aebc`, `bf39`), each a **native** `git worktree` of the main checkout — made by
tooling shipit does not control, flat, at a depth the hierarchy does not
describe, up to six days old. Someone already learned this lesson; this ADR
finishes acting on it.

**The depth is not even uniform.** Of 147 clones in the live root, 90 are
4-segment (`branches`/`ephemeral`/`review`) and 52 are 5-segment
(`epics`/`issues`), plus the 5 two-segment strays. `tree_kind`
(`layout.py:100-131`) must therefore check grandparent depth before parent name —
because an epic could legitimately be *named* `ephemeral` — which is 9 lines of
code under 23 lines of docstring explaining the hazard.

**Agent identity already exists, accidentally.** The `ephemeral` leaf is
`sess-<timestamp>-<pid>` for Claude and `codex-<timestamp>-<pid>` for codex —
minted in two places (`session/bootstrap.py:64-66` in Python,
`data/bootstrap/agent-start:121` as a bash `date -u` one-liner) and
reverse-engineered back into a backend by `resume.py:24`'s prefix table. The
agent and the timestamp are already in the name. They are simply there by
convention, for one kind out of five.

**And `tree list` has no time at all.** Its `AGE` column is
`now - root_mtime` — the clock ADR-0072 documents as lagging real activity by up
to 10 hours. Creation time is recorded nowhere except, as text, inside an
`ephemeral` leaf.

## Decision

**One flat directory of self-describing Trees.**

```text
<root>/<repo>-<agent>-<timestamp>-<id>
```

e.g. `~/workspace/trees/shipit-claude-20260717-081333-619cf51a-f501-44dc-992f-74df773204aa`

- **Repo first**, because it is the axis on which a human narrows, and a plain
  `ls` then groups by it. **Agent second** — `claude` / `codex` / `agy`, the
  three supported backends (`agent/backend.py`; note the `--backend` token is
  `antigravity` while the CLI binary and funnel agent name are `agy` — the
  **binary name** goes in the leaf, matching `claude` and `codex`).
  **Timestamp** in the existing `%Y%m%d-%H%M%S` form, so lexical sort is
  chronological within a repo.
- **`<id>` is a full UUID, minted by whoever creates the Tree.** Never a PID,
  never truncated. Its *source* differs by creation path, and that is a
  consequence of the lifecycle, not a compromise:

  - **A session Tree carries the harness's own session UUID** — e.g.
    `619cf51a-f501-44dc-992f-74df773204aa`. It is available because the
    `WorktreeCreate` payload supplies `session_id` at the moment the hook mints
    the Tree (ADR-0027). **This is the case a human resumes**, and here the
    directory name IS the resume handle.
  - **A spawned Run Tree carries a shipit-minted UUID.** It has no native
    session id to carry: `shipit spawn subagent` orders its work
    `… → Tree → launch` (`spawn/subagent.py`), so the Tree exists *before* the
    backend does and no UUID has been generated yet. Codex reviewer Runs are
    ephemeral and may never mint a durable one at all. These Runs are resumed
    through shipit's own logs (`session/resume.py`, which already keys on shipit
    session ids and treats `ResumeTarget.tree` as a recorded field, not a lookup
    key) — never by a human reading a directory name.

  So the *grammar* is one shape with no exceptions; only the id's provenance
  varies, and the path never encodes which. This is not a fallback ladder — it
  is "the creator supplies the id," and the two creators genuinely have
  different identity available. Renaming a Tree once the native id is learned is
  not an option: ADR-0027 rejected it (the running process holds its cwd inode;
  anything resolving `$PWD` breaks), and this ADR does not reopen it.

  **Not the PID**, because PIDs are reused: the same token eventually names two
  unrelated sessions. That ambiguity is what forced the liveness probe's
  create-time tolerance; ADR-0072 deletes the probe, but the ambiguity in the
  *name* would outlive it.

  **Not truncated**, because a prefix is not a resume handle. Measured against
  Claude Code 2.1.212: `--resume` accepts a full UUID or a session title and
  rejects an 8-hex prefix outright ("is not a UUID and does not match any
  session title"). Session titles are derived from the Tree dir basename plus an
  unpredictable 2-char suffix, so they need a lookup too. Truncating would force
  `ls`-ing the Session store to recover the full id before every resume — buying
  back the very tooling this shape exists to remove.

  The cost is a ~66-character leaf. That is real and accepted: repo-first
  prefixing means `ls | grep shipit` narrows on the head of the name, so the
  long tail never obstructs the axis a human actually scans on.
- **The name is for humans and `tree list`; `gc` does not read it.** ADR-0072
  reclaims on measured activity. The timestamp finally gives `tree list` a real
  created column — which it has never had — without becoming a reclaim signal.
  Keeping naming and reclaim policy independent is a property this ADR
  deliberately preserves.
- **No owner segment.** Repo identity comes from the origin remote, as
  `_repo_slug` already does. The owner is recoverable from the remote when
  anyone actually needs it, which is rarely.
- **No kind segment.** With ADR-0072 reclaiming every kind identically and
  review Trees no longer shared, `tree_kind()` has no readers. Read-only-ness is
  a create-time argument and remains observable from the directory mode; it does
  not need to be encoded in, or parsed back out of, a path.
- **Review Trees are per-Run, like every other Tree.** ADR-0018's write/read-only
  *mode* distinction stands — reviewers still get a chmod'd read-only clone. Only
  the **sharing** is dropped.
- **One shape, no exceptions.** `epics`, `issues`, `branches`, `ephemeral`, and
  `review` collapse to the single leaf above.

## Considered options

- **Keep the hierarchy, fix the spawn discipline** — make agents name Trees
  consistently so the nesting means something. Rejected: it has been tried
  implicitly and lost. The coordinator is launched *before* it knows what it will
  do (ADR-0027's own premise: "at launch the work is almost always unknown"), so
  it cannot name its Tree after work that does not exist yet. ADR-0027 already
  rejected renaming-once-known as fragile and pointless. A hierarchy whose
  segments cannot be filled in at the only moment they can be written is not a
  hierarchy.
- **`<agent>-<repo>-<timestamp>-<id>` (agent first)** — rejected on sort order
  alone. Agent-first groups a `ls` by backend, which is never the question; it
  scatters one repo's Trees across the listing.
- **Keep `<repo>/` as a single directory level, flatten the rest** — a genuine
  middle, and the closest call here. Rejected because it re-introduces the thing
  that broke: a path segment that must be *parsed* to be used, and that
  `_repo_slug` would still refuse to trust. A prefix in the leaf gives the same
  `ls` grouping with no parsing and no depth arithmetic.
- **Keep shared review Trees, add a second derivable shape** — sharing per
  `(repo, branch)` requires a *derivable* path, so a `<timestamp>-<id>` leaf
  breaks it: two reviewers would compute different dirs and silently stop
  sharing. Preserving it means two shapes, i.e. not flat. Rejected as not worth
  it — a reviewer's clone is cheap (ADR-0015's per-Tree `target/` + sccache
  already absorbs the build cost), and one uniform shape is worth more than a
  deduplicated checkout.

## Consequences

- **The blast radius is two files for the *naming* change, plus a real
  behavioural change for dropping sharing.** The naming half: `tree/layout.py` —
  `tree_kind`, `repo_dir`, and `plan`'s four leaf builders collapse to one; this
  is the chokepoint and it is well-factored. And `session/current.py:64-66`, the
  only place outside `layout.py` that knows the nesting arithmetic — it hardcodes
  `parts[:4]` and becomes `parts[:1]`. Roughly six further files call
  `tree_kind()` mechanically; three more only ask `is_relative_to(central_root())`
  and are untouched.

  **Dropping shared review Trees is NOT test churn**, and an earlier draft of
  this ADR wrongly counted it as such. Sharing is implemented behaviour across
  three modules: `tree/readonly.py` (`readonly_plan`'s deterministic
  branch-keyed dir via `_branch_hash`, `_reuse_or_refuse`'s reuse path,
  `_refresh_readonly`'s refresh race, and atomic shared creation),
  `harness/roleprofile.py` (the `SharedReadOnlyTree` checkout strategy), and
  `spawn/subagent.py` (which dispatches on it at two sites). Removing sharing
  deletes the reuse/refresh machinery and collapses that strategy to a per-Run
  read-only Tree — a behavioural change with its own tests, not a rename. It is
  the larger half of WS06 and must be scoped as such.
- **`registry.scan` needs no change at all.** It walks for `.git` markers and
  never parses depth (`registry.py:207-220`), so a flat root works unchanged —
  quiet corroboration that the nesting was never load-bearing on the read path.
  A flat root makes the walk a single `listdir` in due course.
- **Test churn is the real cost.** `test_tree_layout.py` is a truth table over
  dir shapes and is substantially rewritten; `test_session_current.py`,
  `test_tree_create.py`, `test_hook_worktreeremove.py`, and
  `test_tree_readonly.py` assert concrete nested paths.
- **The existing fleet is not migrated.** Old nested Trees are reclaimed by
  ADR-0072 on their own schedule; new Trees are flat. `registry.scan`'s
  depth-agnostic walk sees both, so the two coexist without a compatibility
  layer and the nested shape disappears by attrition.
- **Agent identity gets promoted from convention to field.** The `sess-`/`codex-`
  prefix split across a Python module and a bash one-liner becomes one
  deliberate `<agent>` slot, minted in one place. `resume.py`'s prefix table
  stops being reverse-engineering.
- **`~/workspace/trees/` becomes one directory with hundreds of entries** — 140
  today, 526 at the worst observed. That is unremarkable for `ls`, and it is the
  point: `ls | grep shipit` is the naked, tooling-free narrowing that the
  hierarchy promised and did not deliver.
- **Cosmetic loss, already accepted.** ADR-0027 noted that a session Tree's dir
  and branch stop mirroring once the session switches to real work. Flattening
  generalises that to every kind: the dir records *who and when*, git records
  *what*. `tree list` reads the branch from live HEAD, so the real branch is
  always shown.
