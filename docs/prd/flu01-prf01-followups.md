# FLU01 — PRF01 review follow-ups

> Epic: **FLU01** · Status: planned · Plan: `docs/prd/FUTURE_WORK.md`
> Glossary: `CONTEXT.md` (PR-flow terms)
>
> **Since superseded in part (PROC02, 2026-07):** `src/shipit/prstate/ghapi.py`
> no longer exists — PROC02-WS01 (ADR-0028) merged it into the single `gh` Tool
> adapter, `src/shipit/gh.py`. Item 1 below was **delivered** by that merge:
> `graphql()` now lives in `shipit.gh` carrying exactly the purpose-built-scope
> docstring this item asked for. The other items' file/line references predate
> the adapter epic — re-verify against current code before working them.

This epic tracks the **valid-but-deferred** findings from the PRF01 epic-PR review
(GitHub issue **#24**). They are real improvements that were intentionally postponed
rather than landed during PRF01 — none is a regression, and none blocks the
observability spine (OBS01–OBS04). FLU01 sits **free-floating** in `FUTURE_WORK.md`:
it depends on nothing and nothing depends on it.

## Problem Statement

The PRF01 review surfaced four small, independent rough edges in the shipped PR-flow
code. Each is a localized hardening or ergonomics fix in already-working code — they
were deferred because none blocks PRF01's correctness on its happy path, and batching
them out of the epic kept that PR landable. They remain worth doing:

1. **`graphql()` reads as a general boundary but isn't one.** `src/shipit/prstate/ghapi.py:77`
   `graphql()` is shaped for the engine's own **cursor/pagination** queries: it omits
   `None` variables entirely (so a first-page `after: $cursor` defaults to null) and
   uses `-f` to force string types for `ID!` vars (`ghapi.py:83-88`). That is correct
   for every call the engine makes, but the docstring (`ghapi.py:78`) presents it as a
   plain "run a GraphQL query/mutation" helper — a future caller could reasonably reach
   for it as a general GraphQL boundary and be surprised by the None-omission and
   string-coercion behavior.

2. **The review diff can be computed against a stale base.** In
   `src/shipit/review/diff.py`, the `origin/<base>` fetch is best-effort
   (`_git(workdir, ["fetch", ..., "origin", base_ref], check=False)`, `diff.py:163`)
   and the merge-base resolution **silently falls back** when the base isn't reachable:
   `base_point` degrades from `origin/<base_ref>` to a local ref of the same name
   (`diff.py:196-197`), and `base_sha` degrades to the base tip when no merge-base is
   found (`diff.py:199-205`). A stale or missing local `origin/<base>` therefore yields
   a review diff computed against the wrong base, with no signal to the caller. (The PR
   **head** is already a hard precondition — `diff.py:169-191` fail loud — so this is the
   one remaining silent-fallback edge in the resolver.)

3. **The `review` extra is not materialized in any pixi env.** `pyjwt[crypto]` is the
   `review` optional extra (`pyproject.toml:23`, `review = ["pyjwt[crypto]>=2.8"]`),
   pulled in lazily only when a local review actually runs (`reviewers.py:333-339`). By
   design it is **off the locked CI path** (the required-check tools are conda-forge,
   pinned in `pixi.lock`; architecture.lex §2). The consequence: a pixi-provisioned
   agent environment cannot post a local review — `_LocalReviewAdapter.request()` hits
   the clean `pip install 'shipit[review]'` hint (`reviewers.py:336-339`) and stops.
   `pixi.toml` today defines only the `lint` feature/env (`pixi.toml:35-60`); there is
   no env that carries the `review` extra.

4. **The per-backend review timeout is hardcoded.** `src/shipit/review/backends/agy.py:57`
   pins `--print-timeout=600s`. The per-reviewer `model` is already configurable end to
   end — `reviewers_config.reviewer_run_options()` reads it from `.shipit.toml`
   (`reviewers_config.py:284`), `_LocalReviewAdapter.request()` threads it into
   `run_kwargs` (`reviewers.py:343-344`), and it flows `run_and_post` → `generate_review`
   → `get_backend(agent, model=...)` → `AgyBackend.__init__` (`service.py:46`,
   `backends/__init__.py:29`, `agy.py:50`). The timeout has no such knob: a consumer
   with consistently large diffs cannot raise it without editing code.

## Solution

Each item is a self-contained fix. They share no code and can ship as four small PRs /
Work Streams **or** as one batch — there is no ordering constraint between them.

### 1. `graphql()` doc-scope note (doc-only)

Tighten the `graphql()` docstring in `src/shipit/prstate/ghapi.py` to state that it is
**purpose-built for the engine's own cursor/pagination queries** — it omits `None`
variables and forces string types via `-f` — and is **not** a general-purpose GraphQL
boundary. No behavior change; the body already documents the two quirks inline
(`ghapi.py:83-88`). This is the smallest item: a comment/docstring edit so the next
reader knows the contract before reusing it.

### 2. Review diff stale-base hardening

Make the review base a trustworthy point rather than a silent fallback in
`src/shipit/review/diff.py`. **Recommended approach:** resolve the base SHA
**authoritatively from GitHub metadata** — `gh pr view`'s `baseRefOid` — the same way
the head is already resolved from `headRefOid` (`diff.py:159`, `diff.py:169-191`). That
makes the base a known commit object the resolver can fetch and verify, instead of
trusting whatever `origin/<base>` happens to point at locally. The alternative is to
promote the existing `origin/<base>` fetch + SHA to a **hard precondition** (fail loud
like the head path does) instead of `check=False` with a degrade. Either way the
silent stale-base degrade (`diff.py:196-197`, `diff.py:202-205`) goes away. Final
mechanism is left to implementation; the bar is "the review diff is never silently
computed against a stale/wrong base."

### 3. `[feature.review]` pixi environment (review extra, off the required path)

Add a dedicated **non-default** `[feature.review]` feature to `pixi.toml` that installs
the `review` extra (`pyjwt[crypto]`), and a matching entry in `[environments]` (e.g.
`review = ["review"]`), following the existing `lint` feature/env shape
(`pixi.toml:35-60`). It must stay **off the required-check path** — not folded into the
default env or the `lint`/`check` surface — so the locked CI checks are unchanged
(architecture.lex §2); it exists so an agent env that needs to post local reviews can
provision the extra via pixi instead of a manual `pip install`. (The extra itself is
already declared in `pyproject.toml:23`; this only makes it reachable through a pixi
env.)

### 4. Configurable per-backend review timeout

Add a `timeout` per-reviewer option, mirroring the `model`/`instructions` pattern that
already exists:

- Accept `timeout` in `src/shipit/prstate/reviewers_config.py` — add it alongside the
  reserved options (`_RESERVED_OPTIONS` / `_KNOWN_OPTIONS`, `reviewers_config.py:70-71`),
  parse + validate it (a duration string like `600s`, or seconds — implementation's
  choice, validated like the string fields in `_parse_options`/`reviewer_run_options`,
  `reviewers_config.py:209-242`, `reviewers_config.py:284-343`).
- Thread it through the same path `model` already takes: `_LocalReviewAdapter.request()`
  run_kwargs (`reviewers.py:341-346`) → `service.run_and_post` → `generate_review`
  (`service.py:46`) → `get_backend(..., timeout=...)` (`backends/__init__.py:29`) →
  the backend constructor.
- Default **600s** (today's hardcoded value, `agy.py:57`) when unset, so behavior is
  unchanged for any consumer that doesn't set it.

## Acceptance (per item)

1. `graphql()`'s docstring states it is engine-query-specific (None-omitting,
   string-forcing) and not a general boundary; no behavior change.
2. A stale or missing local `origin/<base>` no longer silently produces a wrong-base
   diff: the base is resolved authoritatively (`baseRefOid`) or the resolver fails loud,
   verified by a test exercising the stale/missing-base case.
3. A pixi env materializes `pyjwt[crypto]`: a `review`-feature env can import the lazy
   `review` path without the clean-install hint firing, and the required-check (`lint`)
   surface and the `pixi.lock` check are unchanged.
4. A `[reviewers]` entry can set `timeout`; it is parsed/validated (loud on bad input),
   threads through to the backend, and an unset value defaults to 600s.

## Out of Scope

- **The dispatcher finding (#24.1 — "dispatcher reads engine prose").** This is
  **absorbed into OBS04**, where the state machine is reworked to route on structured
  `TaskStatus` data instead of `next_action` prose. It is **not** part of FLU01.
- **agy's deeper `--print`-mode flakiness** beyond exposing the timeout knob (item 4):
  agy can still go agentic or truncate on a hard diff; FLU01 only makes the timeout
  configurable, it does not re-engineer the backend's reliability.
- Any broadening of `graphql()` into an actual general-purpose GraphQL boundary (item 1
  is doc-only — it scopes the existing helper, it does not generalize it).
