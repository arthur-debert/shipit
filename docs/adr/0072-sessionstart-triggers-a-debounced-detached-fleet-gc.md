# SessionStart triggers a debounced, detached fleet `tree gc`

`gc.plan_fleet` has exactly one caller: the manual `shipit tree gc` verb
(`src/shipit/verbs/tree.py`). Nothing else ever sweeps the fleet — no atexit
hook, no session boundary, no cron. So stale Trees accumulate until a human
remembers to run gc by hand, and on a real fleet nobody did: **526 Trees piled
up** (#1011), the central root at 99% full. ADR-0027 designed the ephemeral-Tree
gc ladder — the *policy* for what is safe to reclaim — but never the *trigger*
that fires it; `tree/cleanup.py` even carries the note that #1009's policy fix
"added no safety, only 421 parked Trees" precisely because nothing runs it.

Automating gc only became safe once its #1011 siblings landed. Before them, an
automatic sweep would have hidden a failure at machine speed: a gc that silently
no-ops on a drained `gh` budget (#1012), rendered nothing until it finished
(losing its audit trail on interrupt, #1012), and cost ~one `gh` call per Tree
(~512 calls, exhausting the GraphQL budget mid-run, #1014). With those merged —
gc now streams each removal, exits loud (non-zero) on a partly-seen fleet, and
costs ~one `gh` call per repo (~a dozen, not ~512) — a sweep is cheap, honest,
and interruptible. The trigger is the last piece.

## Decision

- **The SessionStart hook triggers the sweep** (`_maybe_sweep_fleet` in
  `src/shipit/verbs/hook/sessionstart.py`), as one more fail-open, additive step
  alongside activation, the liveness pidfile, and the advisories. SessionStart is
  already the ADR-0027 Tree-lifecycle/liveness boundary and already resolves
  `layout.central_root()`, so the trigger rides a boundary that already exists.
- **The sweep runs DETACHED** via `execrun.spawn_detached(["shipit", "tree",
  "gc"])`. A full sweep is tens of seconds on a large fleet; inlining it on the
  session-start latency path is unacceptable, and a fire-and-forget child keeps
  the hook thin. The child **inherits the parent environment unchanged**, so
  `SHIPIT_TREES_ROOT` reaches it and it sweeps the same fleet the trigger stamped.
- **A stamp at the central root debounces it.** The hook stats
  `<central_root>/.shipit-gc-stamp`; if its mtime is younger than the debounce
  window it no-ops, otherwise it **touches the stamp FIRST, then spawns**.
  Touch-before-spawn is what collapses the herd: every session — coordinator and
  subagent — fires this hook, so a second session starting mid-sweep sees the
  fresh stamp and no-ops. One stamp at the shared root governs the whole fleet.
- **The debounce window is 30 minutes** (`GC_SWEEP_DEBOUNCE_SECONDS = 30 * 60`).
  Long enough that a burst of session starts yields ~one sweep; short enough that
  stale Trees never pile up the way 526 once did.
- **No lock.** Concurrency is tolerated without corruption: `remove_tree` guards
  every removal with `os.path.lexists` (`tree/readonly.py`) and a lost race
  surfaces as a caught `GcFailure` WARNING (`tree/gc.py`), not a bad delete. So
  the debounce exists to avoid **wasted redundant scans, not to prevent
  corruption** — a residual race yielding two concurrent sweeps is harmless, and
  locking would add machinery for a hazard that does not exist.

### Alternatives rejected

- **The WorktreeCreate hook.** That hook is fail-CLOSED by contract (a Tree's
  creation must not proceed on a broken isolation guarantee), so hanging a sweep
  off it would risk the very launch it protects. A gc trigger must be fail-open;
  its boundary must be too.
- **The Stop / SubagentStop hook.** A viable alternative — it fires at a session
  boundary just as SessionStart does, and a sweep-on-exit reads naturally. It was
  not chosen only because SessionStart already resolves `central_root()` and is
  already the documented Tree-lifecycle boundary (ADR-0027), so the trigger costs
  no new wiring there. Either would work; this is a tie broken on proximity, not a
  rejection on merit.
- **cron / a system timer.** Off-convention for shipit, which has no scheduled
  background jobs — it would live outside the repo, outside the hook seam, and
  outside every consumer's install. A trigger that rides an existing hook installs
  and travels with the tool.

## Consequences

- The fleet is swept roughly every 30 minutes of session activity, with no human
  in the loop. Stale Trees are reclaimed continuously instead of accumulating to a
  disk-full crisis.
- The manual `shipit tree gc` verb is unchanged and remains the way to sweep on
  demand, see the streamed removals, or run `--dry-run`. The trigger is purely an
  additional caller of the same entrypoint.
- Debounce state is one file at the central root. It is best-effort: delete it and
  the next session start sweeps immediately; a wrong-future mtime would suppress
  sweeps until it passes, but nothing but this hook writes it.
- Because the child inherits the environment, a session whose `SHIPIT_TREES_ROOT`
  points at a non-default root sweeps THAT root — the same one its stamp lives in.
  The trigger and the sweep can never disagree about which fleet they mean.
