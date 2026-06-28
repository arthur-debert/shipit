# OBS01 — Logging foundation

> Epic: **OBS01** · Status: planned · Plan: `docs/prd/FUTURE_WORK.md`
> Glossary: `CONTEXT.md`

## Problem Statement

shipit has **zero** logging. There is not a single `import logging` in the whole
package; everything it tells the world goes through `print()` — **74** call sites —
straight to stdout/stderr. That output is ephemeral: once a command returns, there is
no durable record of what shipit actually did. After the fact there is no way to inspect
a review run, reconstruct a `pr next` decision, or see which `gh` calls shipit made and
what they returned. When something parks or misbehaves, the only evidence is whatever
scrolled past in a terminal that is now gone.

This is tolerable only because today's PR-review path is synchronous and blocking — a
human is watching the one command run. The rest of the observability spine
(`docs/prd/FUTURE_WORK.md`) removes exactly that property: OBS03 makes local reviews
**async, fire-and-forget, detached** runs driven by subagents — work that happens
out of our control with no human watching. Shipping that on top of a package that keeps
no record is, in the maintainer's words, **"a death sentence."** A durable log is the
prerequisite for diagnosing everything OBS02–04 add, which is why the logging foundation
comes first on the spine.

## Solution

Introduce real Python `logging` and route shipit's output through it, with sinks chosen
for where shipit runs.

- **A predictable, bounded file sink — the diagnosis record.** Its directory is resolved
  by **`platformdirs.user_log_dir("shipit")`** — the standard cross-platform lib that
  applies the XDG / platform rules whether or not the `$XDG_*` variables are set
  (macOS → `~/Library/Logs/shipit`, Linux → `~/.local/state/shipit/log`). The sink is
  namespaced per repo as `…/<owner>/<repo>/`, so each repo's history is separable. We do
  **not** hand-roll platform branches and do **not** invent a custom override env var —
  the lib owns path resolution; that is the whole reason to take the dependency.
- **Bounded via `RotatingFileHandler`** (≈5 MB × 3 backups), so the log can never fill
  the disk. This is not hypothetical: an unbounded-log incident this session motivates
  the cap.
- **Level control split between the two surfaces.** The file sink is **verbose**
  (DEBUG/INFO) — it is the record you go back to. The **CLI stays quiet** by default:
  WARNING and above to stderr, so the user-facing surface is unchanged in spirit. A
  `-v/--verbose` flag raises the console level for an interactive debugging session.
- **A CI sink.** When running in CI, log to **stderr** (and optionally append a summary to
  `$GITHUB_STEP_SUMMARY`) so the run's record lands in the job log, where it is the
  durable artifact CI already keeps. It is stderr, not stdout, deliberately: GitHub
  Actions captures both streams into the job log, so the record lands there either way —
  and keeping it off stdout leaves stdout reserved for a command's own output (notably
  `--json`), which a log record on stdout would interleave with and corrupt.
- **Add `platformdirs` as a shipit dependency.**

### Why it is safe on the fast path

Logging is agent / PR-loop tooling, not artifact-producing or required-check logic. Per
`docs/dev/architecture.lex` §2 (the slow/fast split), it rides the **fast path** —
installed outside the locked env — so it auto-updates fleet-wide with zero per-consumer
lockfile churn. The accepted cost of that path (no per-consumer pin) is acceptable here
for the same reason it is for the PR state machine: logging is off the required-check
surface; a logging glitch is visible and retryable, it never fails a check or corrupts a
build. (See architecture.lex §2 for the full statement; it is not duplicated here.)

## User Stories

1. As an agent diagnosing a parked PR, I want a durable per-repo log I can open after the
   run, so that I can reconstruct what shipit did instead of relying on terminal scrollback
   that is already gone.
2. As an agent, I want the log path to be the standard platform location for `shipit`, so
   that I can find it the same way on macOS and Linux without per-machine configuration.
3. As a maintainer, I want the log namespaced per `<owner>/<repo>`, so that one repo's
   history does not bleed into another's.
4. As a maintainer, I want the file log bounded in size, so that a long-running or chatty
   session can never fill the disk.
5. As a user running a shipit command, I want the CLI quiet by default (warnings and
   errors only), so that normal output looks exactly as it does today.
6. As an agent debugging interactively, I want `-v/--verbose` to raise the console level,
   so that I can watch the detail live without editing config.
7. As a maintainer reading a CI job, I want the run's log in the job output, so that a CI
   failure leaves a record I can inspect without re-running.
8. As a security-conscious maintainer, I want secrets never written to any sink, so that a
   durable log does not become a durable leak.

## Implementation Decisions

- **Path resolution is `platformdirs`, full stop.** `platformdirs.user_log_dir("shipit")`
  gives the base directory; shipit appends `<owner>/<repo>/`. No platform `if` branches, no
  bespoke `SHIPIT_LOG_DIR` override — the lib is the single source of truth for where logs
  live.
- **Bounding is `RotatingFileHandler`** at ≈5 MB per file × 3 backups. The numbers are a
  starting point, not a config surface in this epic.
- **Two independent level controls**: file handler at DEBUG/INFO (the record); console
  handler at WARNING by default, raised by `-v/--verbose`. The CI sink is a stderr handler
  installed when a CI environment is detected.
- **`print()` → `logging` adoption is scoped to the key boundaries**, not a blanket
  sweep: the `gh` boundary (every call and its outcome), `prstate` (the lifecycle/next-
  action decisions), and `review` (the review runs). These are exactly the surfaces OBS02–04
  need to be observable.
- **Public CLI output behavior is unchanged.** What a user sees stays the same; it is
  merely **routed through logging** rather than written with bare `print()`. This epic does
  not redesign any command's human-facing output — it changes the plumbing under it.
- **No secrets to any sink.** Secret values handled via `[secrets]` / `secretsrc` are kept
  out of log records; this is an explicit constraint on the call-site adoption, not an
  afterthought.

## Work Streams

A hint at the decomposition, not a binding contract (execution topology lives on the epic
issue per `AGENTS.lex`, not here):

- **WS — file sink + config.** The `platformdirs`-resolved, per-repo, rotating file
  handler and the logging configuration that wires it up; add the `platformdirs` dependency.
- **WS — CI / console sinks + `-v`.** The quiet-by-default stderr console handler, the
  `-v/--verbose` level control, and the CI stderr (+ optional `$GITHUB_STEP_SUMMARY`) sink.
- **WS — call-site adoption.** Convert `print()` to `logging` at the `gh`, `prstate`, and
  `review` boundaries, preserving user-facing output and keeping secrets out of records.

## Testing Decisions

A good test asserts external behavior, in line with shipit's conventions:

- **The sink resolves the right directory** — given a `platformdirs` base, the path is the
  per-repo `…/<owner>/<repo>/` location (boundary injected; no real home writes).
- **Rotation caps size** — the handler is configured with the bound and rolls over rather
  than growing without limit.
- **CLI quiet by default / verbose with the flag** — default invocation emits nothing below
  WARNING to the console; `-v` raises it.
- **No secrets logged** — a record produced over a secret-bearing path does not contain the
  secret value.

## Out of Scope

This epic is the logging foundation only. The dependents that build on it are named, not
delivered here:

- **OBS02** — uniform funnel breadcrumbs (bot comments for requested / arrived / failed /
  empty) on the PR.
- **OBS03** — async, detached local review execution that posts back to the PR.
- **OBS04** — the state machine consuming breadcrumbs + timestamps with a wait window.

OBS01 depends on nothing and unblocks OBS02 → OBS03 → OBS04 (see `docs/prd/FUTURE_WORK.md`
for the spine). Funnel semantics, async execution, and engine changes are all theirs, not
this epic's.
