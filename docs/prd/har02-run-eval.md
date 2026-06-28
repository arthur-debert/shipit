# HAR02 — Objective run evaluation

> Epic HAR02 of the **agent harness** (rung 2 of HAR01–HAR04). Authoritative spec.
> Decision: [ADR-0013](../adr/0013-eval-objective-first-local-only.md). Vocabulary:
> `CONTEXT.md` (**run**, **eval record**, **role**, **break-glass**, **variant**).

## Problem Statement

The harness delegates work to subagent **runs**, but **neither the human nor the coordinator
reads a subagent's full transcript** — only its final summary returns. Two costs follow:

- **Broken runs hide.** A run that idled mid-task, looped, bypassed checks (`--no-verify`),
  or drifted role can report "done" and only be caught by chance. (Observed live: an
  implementer idled mid-rebase with conflict markers still in the tree and signalled
  "available"; it was caught only by manually re-reading git state.)
- **There is no data to iterate the harness.** When we change a **role prompt** or a
  **policy**, we have no signal whether it helped — so harness changes are guesswork. The
  whole point of HAR01–HAR04 is a loop we can *tune*, and tuning needs measurement.

What's missing is a cheap, per-run, aggregable record of *how a run went* — without standing
up observability infrastructure or reading transcripts by hand.

## Solution

At each **run**'s terminal lifecycle hook, deterministically extract **objective** metrics
from the transcript + `.meta.json` already on disk, and write one **eval record** per run:

- **Objective-first**: every field is extracted *by code* (tool-call counts, step count,
  stuck-loop fingerprints, `--no-verify` / workaround greps, **break-glass** uses, error /
  retry, exit-hygiene, plus `role` / `model` / `permissionMode` from meta). No model call, no
  judge — the subjective **agent-as-judge** is deferred to HAR04 (ADR-0013).
- **Local, never committed**: JSONL with OpenTelemetry `gen_ai.*` field names, in a
  harness-owned store keyed by repo, each record stamped with `git.commit` and a **variant**
  (the content-hash of the role prompt / policy that ran, + optional A/B label) so results
  attribute to the exact harness version — poolable across commits, separable within one.
- **No platform, no infra**: aggregation is DuckDB/SQL over the local JSONL. If shared trend
  is ever wanted, the substrate is GitHub-native (an epic issue's run comments, or Pages) —
  never self-hosted (ADR-0013).
- **Synchronous** in the terminal hook (objective-only ⇒ no model call ⇒ a few ms of parsing);
  the **Detached review** pattern is only needed once HAR04 adds the judge's model call.

The result: a broken run is detectable, and every harness change produces measurable,
attributable run data to iterate on.

## User Stories

1. As a maintainer, I want each run's broken-ness flagged (idled, looped, no progress), so
   that a failed delegation doesn't masquerade as "done."
2. As a maintainer, I want stuck-loop detection (same tool+args repeated, runaway iterations),
   so that a spinning agent is caught from the record, not by luck.
3. As a maintainer, I want every `--no-verify` / check-bypass recorded, so that a run that
   sidestepped the commit/push checks is visible.
4. As a maintainer, I want each run's tool-call vector, so that I can see whether an agent used
   the tools its role expects.
5. As a maintainer, I want break-glass uses counted per run, so that HAR01's escape hatch is a
   measured signal I can tighten on.
6. As a maintainer, I want per-role aggregation, so that I can ask "how are implementer runs
   doing" vs shepherd runs.
7. As a maintainer, I want each record stamped with the prompt/policy **variant** that ran, so
   that I can tell which version of a role prompt produced which results.
8. As a maintainer, I want to A/B two prompt variants and separate their run results, so that I
   can decide changes by data, not intuition.
9. As a maintainer, I want results to pool across commits when the prompt is unchanged, so
   that I get a stable baseline for a variant.
10. As a maintainer, I want records correlated to `git.commit` without being committed, so that
    process telemetry never dirties product history.
11. As a maintainer, I want to trend metrics over time with a SQL query, so that I can see the
    harness improving (or regressing) run over run.
12. As a maintainer, I want eval to add no infrastructure, so that the harness stays a personal
    portfolio tool, not a service to operate.
13. As a maintainer, I want exit-hygiene checked at the coordinator run's end (clean worktree,
    no stray processes), so that a run that left a mess is recorded.
14. As a maintainer, I want token usage captured when the transcript logs it, so that
    expensive runs surface.
15. As a maintainer, I want eval to run synchronously and fast, so that it's simple to reason
    about and never leaves a detached job to manage.
16. As a maintainer, I want the record format to use a standard field vocabulary, so that a
    future move to any OTel-aware tool is cheap.
17. As a maintainer, I want HAR02 to *only read* the on-disk transcript + meta, so that it
    requires no new instrumentation in the agents themselves.
18. As a maintainer, I want the objective record to stand alone, so that HAR04's judge layers
    on top without HAR02 depending on it.

## Implementation Decisions

- **The eval unit is the run** (per-agent transcript + meta), not the task or the session: the
  coordinator run is the session transcript; each subagent run is its own `agent-<id>`
  transcript. One **eval record** per run, fired at the run's terminal hook (a subagent at
  `SubagentStop`, the coordinator at `Stop`/`SessionEnd`).

- **Six modules**:
  1. **Run locator** — `locate_run(hook_input) → {transcript, meta}`: resolve the just-closed
     run's files from the hook payload. Boundary (filesystem).
  2. **Objective extractors** — `extract(transcript, meta) → metrics`: the deep core, a set of
     *composable pure functions* (tool-call vector; step/turn count; stuck-loop fingerprints —
     same tool+args hash >2× in a turn, or >8 iterations; `--no-verify` / workaround greps;
     break-glass count; error/retry; token totals if logged; meta passthrough role / model /
     permissionMode). Per-metric extractors live *inside* this module, not as separate modules.
  3. **Eval-record builder** — `build(metrics, meta, variant, commit) → record`: assemble the
     JSONL object with OTel `gen_ai.*` names for standard fields and `eval.*` for harness-local
     ones. Pure.
  4. **Variant resolver** — `variant_of(run) → content_hash + optional label`: the content-hash
     of the generated role prompt / policy that ran, reusing the **content-key** / pristine-hash
     machinery. Pure.
  5. **Hook boundary** — `shipit hook subagent-stop` / `shipit hook stop`: terminal-hook →
     locate → extract → build → write to the local store. Thin; **synchronous**.
  6. **Aggregator** — `shipit eval report`: a thin wrapper running DuckDB/SQL over the local
     JSONL store for summaries and trends.

- **Metric scope = transcript-cheap + meta + a cheap exit check.** HAR02 covers what the
  on-disk transcript/meta yields plus an exit-hygiene check (`git status --porcelain` + stray
  PIDs) at the coordinator run's end. **Needs-instrumentation** metrics (wall-clock latency,
  dollar cost, time-to-PR-ready, review rounds from VCS) are deferred — they require VCS/API or
  timing signals outside the transcript.

- **Storage**: harness-owned local store keyed by repo, JSONL append-only, OTel-named, each
  record `git.commit`- and `variant`-stamped. Never committed; no platform (ADR-0013).

- **Objective-only at rung 2**; the **agent-as-judge** verdict (a different model family,
  evidence-required, anchored-binary) is HAR04 and layers onto the same record.

## Testing Decisions

- **Good tests assert external behavior**: a fixture transcript in → the expected metrics /
  record out; never the parser's internals.
- **Modules tested: #1–#4.**
  - **#2 Objective extractors** — the bulk of the value: fixture transcripts (a clean run, a
    stuck-loop run, a `--no-verify` run, a break-glass run) → expected metric values. Each
    extractor gets its own table-driven case.
  - **#1 Run locator** — fixture hook payloads → the right transcript/meta paths, including the
    coordinator (session file) vs subagent (`agent-<id>`) split.
  - **#3 Eval-record builder** — assert the record shape: OTel-named fields present,
    `git.commit` + `variant` stamped, JSON parses.
  - **#4 Variant resolver** — *hash stability/poolability*: identical inputs → identical hash
    (runs pool); a changed prompt → a different hash (runs separate).
- **#5 Hook boundary / #6 Aggregator** get thin tests (one end-to-end "payload → record on
  disk"; one "store → expected aggregate row").
- **Prior art**: the PR state engine's pure-snapshot tests and the lint routing tests — the
  same pure-core / thin-boundary split.

## Out of Scope

- **HAR04** — the subjective **agent-as-judge** verdict and any model-call eval. HAR02 emits
  only code-extracted objective fields.
- **A shared / GitHub-native eval store** (issues, Pages) — deferred; local-only at rung 2.
- **Any tracing/observability platform** (LangSmith / Langfuse / Phoenix) and live tracing.
- **Needs-instrumentation metrics** — wall-clock latency, dollar cost, PR-lifecycle (time-to-
  ready, review rounds, CI pass-rate) from VCS/CI APIs.
- **Acting on the data** (auto-tightening break-glass, gating on a metric) — HAR02 *measures*;
  decisions stay human/feed later epics.

## Further Notes

- **HAR02 closes HAR01's loop**: break-glass frequency and role-drift incidents from these
  records are exactly the signals that decide whether to tighten HAR01's policy and whether
  `AGENTS.md` should carry the role map — turning "does it help?" from a guess into a query.
- The metric set is seeded from the research (IFEval-style verifiable-instruction decomposition;
  trajectory precision/recall; the stuck-loop fingerprint heuristic; guardrail-bypass counting)
  but kept to the transcript-cheap subset at rung 2.
- The judge is deferred deliberately: a same-model self-judge is non-independent and
  upward-biased, so the objective record must stand on its own first (ADR-0013).
