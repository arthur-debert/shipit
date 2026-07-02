# CLI boundary: parse-to-values, typed results, render at the edge

> **Status: Proposed.** Contract for the CLI/API separation epics (CLI01: seam +
> `PrId` + `Roster`; CLI02: verb promotions). Completes ADR-0002's verb
> convention upward, the way PROC03/ADR-0028 completed ADR-0024 downward.

The `shipit` CLI becomes a thin shell over the domain packages: click parses
argv **into value objects**, verbs call domain functions that return **typed
results**, and a verb-layer **renderer** is the only place a result becomes
terminal output.

## Context

ADR-0002 fixed the verb convention ("parse clean args → call engine helpers →
render") and ADR-0021 the modeling rule ("boundary functions do I/O and return
immutable snapshots; no mutable module-global state"). PROC03/ADR-0028 closed
the *bottom* edge: Tool adapters return `Repo`/`PR`/`Sha`, never re-parsed
strings. The *top* edge was never closed. Concretely: every `run()` returns a
bare `int` and interleaves printing, logging, and adapter calls; the pr verbs
carry four verbatim copies of one error-mapping `try/except`; "optional REPO
defaults to the current checkout" is implemented five different ways (three of
them via a per-fetch `gh repo view` shellout that contradicts ADR-0024's
offline identity source, paid twice per `pr next`); `--json` exists on 2 of ~20
commands; PR numbers travel as bare ints and reviewer config as three parallel
string-keyed dicts behind the exact module-global caches ADR-0021 names. Verb
tests pay the bill: 4–6 stacked monkeypatches per happy path and assertions on
rendered whitespace.

## Decision

1. **Parse to values at click.** A shared parameter library (custom
   `ParamType`s + reusable decorators) mints value objects at argv parse:
   `Repo` via `repo_from_slug`, the PR target as a `PrId`, the shared
   tree/spawn shape options. Construction-is-validation; a malformed argument
   is a click usage error, never verb-body code.
2. **Two-tier exit contract.** rc 2 = usage (argument errors, raised at
   parse); rc 1 = runtime failure; rc 0 = success. One `@cli_errors` shell on
   each verb's `run()` maps the known exception set (`ExecError`,
   `PrStateError`, `ConfigError`, domain refusals) to a uniform `error: …` on
   stderr + rc 1, replacing the copied per-verb blocks. Hook verbs are exempt —
   their fail-open/fail-closed canon is unchanged.
3. **Typed results, render at the edge.** Domain functions return frozen
   result values and keep the durable log twin (ADR-0029); they never print.
   Verbs render via pure `format_*(result) -> str` helpers and one shared
   `emit(result, format_text, as_json)`. Every user-facing read verb gets
   `--json`, serialized from the result's `to_dict()`. The agent-parsed
   mutation sentinels (`READY`, `SPAWNED`) are a stable surface and stay.
4. **Domain packages ARE the API.** The pure Python API is the domain packages
   themselves (`prstate/`, `tree/`, `spawn/`, plus new homes for the logic now
   squatting in `verbs/`); `verbs/` shrinks to click glue + renderers. No
   `shipit/api/` namespace, no facade module.
5. **Ambient identity is threaded, not re-derived.** The CLI root resolves the
   `WorkingDir` once (offline, per ADR-0024) onto click's context; shared
   params use it as the default an explicit REPO/PATH argument overrides. The
   per-fetch `gh.current_repo()` resolutions are deleted. Hooks (payload cwd)
   and detached children (explicit `--repo`) keep their own entry points.

The command/flag surface is otherwise frozen for these epics: external callers
(hook settings, pixi task blocks, role prompts, in-code argv literals) pin it,
so no renames — additions like `--json` only.

## Considered options

- **A `shipit/api/` namespace** — rejected: a parallel tree beside the domain
  packages with an arbitrary seam (where does `prstate` end and `api.pr`
  begin?).
- **A re-exporting facade (`api.py`)** — rejected: an indirection layer with no
  job of its own (the house no-adapters rule).
- **rc 1 for everything** (today's observable behavior) — rejected: forfeits
  parse-at-boundary, since click owns the error once a `ParamType` validates.
- **Prints staying in services** (status quo) — rejected: couples every outcome
  to a terminal; it is why verb tests monkeypatch the world and parse capsys.

## Consequences

- Verb modules become glue + renderers; the promoted logic lands in domain
  packages (CLI02 for `install`, `spawn`, `tree`, `gh-setup`, the `logs`
  reader). Verb-glue tests collapse toward the prstate style — typed value in,
  typed result out — plus a thin argv-wiring smoke layer.
- The rc 1→2 shift for argument errors is an observable change; tests and any
  callers asserting rc 1 on bad input are updated deliberately in the same
  change that moves the validation.
- ADR-0021 rule 4 is discharged for its named example: the reviewer
  required/rerun caches dissolve into the `Roster` value (see CONTEXT.md),
  loaded once at a boundary and passed.
- `PrId` (CONTEXT.md) replaces the bare-int PR number in service signatures;
  identity arrives typed from the top the way it already leaves the adapters
  typed at the bottom.
