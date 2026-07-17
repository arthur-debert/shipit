# Tree reclaim is activity-based: measure the work, not its proxies

> **Supersedes the reclaim ladder in ADR-0027's Consequences** (the five-rung
> `ephemeral` rule and the pidfile liveness it rests on) and **the `stale` bucket
> and merged-PR rungs of the write and review ladders**. ADR-0027's Decision —
> the ephemeral session Tree, ephemeral-by-path/work-by-branch, minted through
> `WorktreeCreate` — is untouched. This ADR replaces only how such a Tree is
> reclaimed.

`shipit tree gc` deleted the worktree of a **live** Claude session (#1018) — the
process still running at ~9h elapsed, its working directory removed out from
under it. The session was doing external `gcloud`/`gsutil` provisioning, so its
git tree stayed clean and it had no PR. Three of the ladder's five ephemeral
rungs were structurally unable to fire, and the fourth — the only thing standing
between a clean session and deletion — depended on a liveness probe that read
false. This is data-loss-adjacent under manual `gc` and catastrophic under
the #1017 auto-trigger, which is blocked on this ADR.

## Context

The ephemeral ladder (`_ephemeral_bucket`, `src/shipit/tree/cleanup.py:447-505`)
failed in two independent ways, and the second is the interesting one.

**The proximate bug: rung 5 never checks liveness.** For a clean, PR-less,
>1h-old Tree, rungs 1, 2 and 4 do not fire, so rung 3 (`live and age <=
hard_cap`) is the sole protection — and it requires `live == True`. Any liveness
false-negative falls straight through to rung 5, which reads age *only*.
`session/liveness.py` documents the assumption in as many words: a missing
pidfile "reads as NOT live — the safe direction, **because the pure ladder still
protects such a Tree through its liveness-independent rungs**." That is false for
a clean Tree. Rung 1 (dirty/unpushed) doesn't protect it, and rung 5's grace
window is a *lower bound that enables* deletion, not an upper bound that
prevents it. "Not live is the safe direction" holds only for a dirty Tree; for a
clean one it is the dangerous direction.

**The root cause: the ladder measures proxies, and every proxy is wrong.** The
`age` fed to rung 5 is `now - record.mtime` (`cleanup.py:500`), where `mtime` is
`stat()` on the **clone root directory** (`registry.py:360`). A directory's mtime
bumps only when an entry is added or removed *in that directory*, so an agent
editing and committing under `src/` never touches it. `TreeRecord`'s own
docstring concedes this (`registry.py:142-147`), and the **write** ladder was
patched for it — `_idle_seconds` (`cleanup.py:402-426`) takes
`max(root_mtime, last_commit)`. **The ephemeral ladder never got that patch.** It
still runs raw root mtime against a 1h grace window. Measured against the live
fleet, root mtime lags real file activity by **up to 10 hours**.

So the system spent a pidfile, a `ps`/`jc` probe, a create-time tolerance, an
argv host allow-list, a PR-state network read, and four tunable time windows
approximating a single question — *has anyone touched this recently* — which the
filesystem already knows and nothing ever asked.

**The measurement that settles it.** Across every ephemeral Tree in the live
fleet, idle time (newest file mtime, build/env dirs pruned) separates with no
overlap:

| population | idle time |
| ---------- | --------- |
| every live Tree | **< 1h** |
| every dead Tree | **> 41h** |

**Any threshold in the open band 1h–41h separates the fleet perfectly.** There is
no ambiguous middle — and managing an ambiguous middle is what the entire
841-line, 15-input, 176-test apparatus exists to do.

48h is deliberately set **above** that band, not inside it. Being above it costs
nothing that matters and buys margin on the only error that does: a Tree idle
41–48h is dead but reclaimed on the *next* sweep rather than this one, while the
safety property — never delete a live Tree — holds with a **48× margin** over
the busiest observed live Tree. The threshold is chosen against the *live*
distribution, where the consequence is unrecoverable, not against the dead one,
where the consequence is a few hours of disk.

ADR-0027 rejected age-alone for a reason that was correct at the time and is now
obsolete: a session Tree "is often *clean* (a planning session that hasn't
committed), so 'clean + aged' alone would let `gc` delete a Tree out from under a
live idle session." That is true of **creation**-age and of **root-mtime**-age.
It is not true of **activity**-age, which is precisely the signal the ladder
lacked and liveness was hired to fake.

## Decision

**A Tree is kept if it has local work or recent activity; otherwise it is
removed.** One rule, all kinds:

```text
KEEP  if  dirty  ||  unpushed  ||  idle < 48h
```

- **`dirty`** — `git status --porcelain` is non-empty. The existing floor,
  unchanged.
- **`unpushed`** — local commits present on no remote (`git rev-list HEAD --not
  --remotes`). Retained deliberately; see Consequences.
- **`idle`** — `now - max(newest file mtime, HEAD's commit stamp)`, where the
  mtime comes from a **pruned** walk that skips `.git`, `.pixi`, `node_modules`,
  `target`, `.venv`, `dist`, `build`, `__pycache__`. The walk does not exist
  today and must be built.

  **Why the commit stamp is in there**, given that this ADR rejects it as a
  proxy two bullets down: because a file walk is structurally blind to work
  that removes files. Delete a tracked file, commit, push — the Tree is clean,
  fully pushed, and *every surviving file still carries its old stamp*. The
  removed entry bumped only its parent directory, and the commit landed in
  pruned `.git`. Measured on nothing but the walk, a Tree that was worked in
  seconds ago reads as idle and is deleted. That is #1018's exact shape,
  rediscovered (codex, #1029 review round 1).

  This is **not** the commit stamp deciding anything, which is the distinction
  the proxy bullet is drawing. `max` can only push `idle` *down* — it can only
  ever KEEP — so the stamp is a floor under the measurement, never a licence to
  remove. Each signal covers the other's blind spot: the walk sees uncommitted
  work the stamp cannot, the stamp sees deletions the walk cannot. Neither is
  trusted alone, and **either being UNKNOWN reads as KEEP** — never as "fall
  back to the other."

  Note what this restores: `max(root_mtime, last_commit)` is the patch the
  **write** ladder always carried and the ephemeral ladder never got — the
  omission this ADR's own Context names as half of #1018's root cause. Dropping
  it while keeping the hazard would have re-opened the bug this ADR exists to
  close.

Three signals. **No PR state, no pidfile, no `ps` probe, no kind dispatch.**

**UNKNOWN IS NOT FALSE — an unreadable signal KEEPS.** The rule above is written
over three booleans, and a boolean has nowhere to put "I could not tell." That
gap is a deletion licence, so it is closed here explicitly rather than left to an
implementer: **any signal that cannot be determined reads as KEEP, never as
`False`.** Concretely — `git status` or `git rev-list` failing, erroring, or
timing out; the walk hitting an unreadable dir, a broken symlink, or a
concurrent removal; a walk yielding no eligible files at all; a `stat` raising.
Every one of those keeps the Tree and is reported, not swallowed.

This preserves a property today's code has deliberately and that a naive
three-boolean rewrite would silently drop: `unpushed_shas` is `None` on failure
and reads as *has local work*, and a failed scan aborts rather than licensing
deletion. The asymmetry is the whole point — a wrongly-kept Tree costs disk
until the next sweep, a wrongly-deleted one costs work that no longer exists.
**The bias must be re-derived from the consequence, not inherited by accident**,
which is why it is a decision here and not a code comment. And it matters more
now than before: this ADR is what unblocks an *automatic* sweep (#1017), so
"unknown" stops being a human's judgement call and becomes a machine's default.

- **Activity is measured, never inferred.** Liveness and PR state were proxies
  for "is someone working here" — they answered a *different* question and hoped
  it correlated. The pruned walk answers it directly, and more truthfully than
  either: it observes an agent editing under `src/`, which root mtime cannot,
  and it observes a session that has committed nothing and opened no PR, which
  the PR read cannot.

  The commit stamp is the one former proxy that survives, and it survives
  **demoted**: not as an answer to "is someone working here," but as direct
  evidence of one specific event — a commit — that the walk structurally cannot
  see when that commit only removes files. It is a floor under the measurement
  (`max`, keep-only), never an input to the decision. The test that separates a
  proxy from a floor: a proxy can license a removal, and this cannot.
- **The threshold is one constant.** 48h replaces `DEFAULT_MAX_AGE_SECONDS`
  (14d), `MERGED_IDLE_GRACE_SECONDS` (12h), `EPHEMERAL_HARD_CAP_SECONDS` (4d),
  and `EPHEMERAL_GRACE_SECONDS` (1h). The grace window is unnecessary: a
  just-launched Tree is minutes idle, not 48 hours.
- **One rule for every kind.** `tree_kind()` dispatch (`cleanup.py:361-373`) is
  gone; `review`, `ephemeral`, and `write` Trees reclaim identically. The
  `stale` bucket ceases to exist — `Cleanup` becomes keep/removable.
- **The walk must be pruned, and the prune set is load-bearing.** Naive walks
  cost 191.7ms/Tree (17,374 files); pruned, 1.9ms (509 files). `.pixi` alone is
  ~97% of the file count. An unpruned walk is slower than everything this ADR
  deletes.

## Considered options

- **Fix rung 5 and keep the ladder** — add `live` to the rung that lacks it. The
  minimal patch, and it treats the symptom: the ladder would still rest on a
  liveness probe with five documented false-negative modes (`ps` timeout, `jc`
  parse failure, unparseable `etime`, >5s create-time drift, an argv host
  allow-list hardcoded to `{claude, claude-code, codex}` that any new backend or
  wrapper defeats), still read `age` from a clock that provably lags by 10h, and
  still cost a `gh` round-trip per repo. Rung 5 is where it surfaced, not where
  it lives.
- **Creation-timestamp reclaim** (age from the `<timestamp>` in the Tree name) —
  rejected as the *decision* signal. Creation-age is not activity-age: the
  #1018 session was 9 hours old and fully alive. This is ADR-0027 rung 4 with a
  worse clock, and it rebuilds the exact bug being fixed. The timestamp stays in
  the name for humans and `tree list` (ADR-0074); `gc` does not read it.
- **A cheap prefilter before the expensive check** (skip Trees younger than ~12h
  to avoid the walk) — safe by construction, since a keep-direction filter can
  only spare a Tree. Rejected as solving a cost that no longer exists. The
  >10-minute sweeps that motivated it were `gh`: one PR read per Tree at
  0.5–5s each (#1014 later batched these to one per repo). Deleting the PR
  signal takes a 526-Tree sweep from ~10 minutes to ~22 seconds; the prefilter
  would save a further few seconds on a detached background job. It is also
  aimed at the wrong target — after this ADR the walk (1.9ms) is the *cheapest*
  signal and `git status` (21.5ms) the dominant one. If a sweep ever does become
  slow, **root mtime is the better lever**: it is free (one `stat`, already
  gathered), always ≥ creation time, and strictly more informative than a parsed
  name. Deferred, not designed in — it would couple the naming scheme to gc
  policy, and keeping those independent is worth more than the seconds.
- **Keep liveness as a backstop under the activity rule** — rejected as
  redundant at a 48h threshold. Live Trees measure <1h idle; the margin is 48×.
  Liveness would fire only for a session that writes no file for two days, and
  it would fire *unreliably*, since that is exactly the regime its
  false-negatives inhabit.

## Consequences

- **Three modules retire.** `session/liveness.py` (467 lines, 57 tests) — the
  pidfile, the `ps`/`jc` fork, the 5s create-time tolerance, the argv host
  list. `tree/provision.py` (81 lines) — its `provision_shas` carve-out exists
  only to subtract shipit's own commits from the unpushed floor (ADR-0027's
  #232 amendment); it already reads a file nothing writes since ADR-0033 retired
  the writer. And `gc.pr_state` — with it, **the entire `gh` network dependency**,
  including the `PrIndex` batching apparatus (`registry.py:249-341`) that #1011
  built to feed a signal nothing now reads.
- **`.git/shipit-session.json` loses its only consumer.** It was written by the
  SessionStart hook and read solely by the ephemeral ladder. Note the harness
  already writes `~/.claude/sessions/<pid>.json` carrying `{pid, sessionId, cwd,
  status, updatedAt}` — a per-session heartbeat that is strictly better than the
  pidfile and needs no `ps` fork. If activity-based reclaim ever proves
  insufficient, **that** is the escape hatch, not a rebuilt pidfile. It is
  undocumented harness internals and Claude-only, which is why it is a noted
  fallback rather than a dependency.
- **841 lines and 15 decision inputs collapse to ~10 lines and 3 signals.** Of
  `cleanup.py` + `gc.py`, ~271 lines are code and ~464 are prose defending
  ladder subtleties that no longer exist. `sweep()` survives intact — #1012's
  streaming removals and loud partly-seen-fleet exit are orthogonal to the
  ladder and are kept.
- **A latent bug is mooted rather than fixed.** `live_reviews` is accepted by
  `classify` and branched on by `_review_bucket`, but `plan()` never forwards
  it, so **every review Tree reads `reviewer_live=False`** in production. The
  rung is unreachable outside tests. It disappears with the ladder.
- **Sweeps get fast enough to stop being a design constraint.** The full local
  check is ~4s across today's 140-Tree fleet and ~22s at 526 Trees — the largest
  fleet ever observed — versus 4–40 minutes for the un-batched `gh` reads it
  replaces. This is what unblocks #1017.
- **The unpushed floor is retained, and it is the one non-obvious keep.** A
  clean Tree whose commits were never pushed looks idle; without the floor, at
  48h it is deleted and those commits die with `.git` — unrecoverable. It costs
  one `git rev-list` (~10s at 526 Trees). Retaining it keeps ADR-0027's absolute
  floor intact in substance while dropping everything built around it.
- **Accepted residual risk: a live session that writes no file for 48h is
  reclaimed.** This is deliberate. The #1018 session — 9h of purely external
  `gcloud` work — survives comfortably; the rule reclaims only a session idle
  two full days, which is abandoned in practice. This is the one place the ADR
  trades a theoretical false-positive for the deletion of an entire subsystem,
  and the 48× margin is why.
- **`tree_kind()` loses its last gc consumer.** Combined with ADR-0074 dropping
  shared review Trees, kind has no remaining readers and becomes a name prefix
  rather than a directory level.
