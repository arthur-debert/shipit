# Session eval is objective-first, local-only, GitHub-native if ever shared

HAR02 must surface how a **Run** went — to catch broken runs and, more importantly, to give
*data* for iterating the harness itself — without anyone reading full transcripts. Two forces
shape the answer: the eval tooling lane is either heavyweight tracing platforms (LangSmith is
proprietary SaaS, enterprise-only self-host; Langfuse/Phoenix are OSS but app-observability
shaped and add infra) or eval frameworks; and the research is pointed that a *same-model
self-judge is not independent* — it carries measurable upward self-preference bias. Meanwhile
every run's transcript + `.meta.json` is **already on disk**.

**Decision.**

- **Objective-first.** An **Eval record**'s fields are extracted *deterministically by code*
  from the on-disk transcript + `.meta.json` (tool-call vector, step count, stuck-loop
  fingerprints,
  `--no-verify` / workaround greps, **break-glass** uses, `model`, `permissionMode`). A
  subjective **agent-as-judge** verdict is **deferred to HAR04** and layered on top
  (different model family, evidence-required, anchored-binary), never the primary signal.
- **No platform.** No LangSmith / Langfuse / Phoenix — wrong *kind* (live tracing, not
  transcript metric/rubric extraction) and wrong weight for a personal portfolio.
- **Local, never committed.** Records are JSONL (OpenTelemetry `gen_ai.*` field *names* as
  vocabulary, not a collector) in a harness-owned local store, each `git.commit`-stamped so
  it correlates to repo state *without* entering the tree. Process telemetry is not product.
- **GitHub-native if ever shared.** Should cross-machine/team trend be wanted, the substrate
  is GitHub (an epic issue's run comments, or Pages) — never self-hosted infra. This mirrors
  the PR engine's value: "the PR + check runs are the WHOLE store — no daemon."

## Considered options

- **A tracing/eval platform.** Rejected: LangSmith is SaaS-only for any low-ceremony use;
  even OSS Langfuse/Phoenix add a service and are shaped for production LLM traffic, not
  on-disk transcript extraction.
- **A same-model self-judge as the primary metric.** Rejected: non-independent and
  upward-biased (Panickssery 2024; Wataoka 2024). Objective extraction is the floor; the
  judge is deferred and de-biased in HAR04.
- **Commit eval records into the repo** for free version-controlled trend. Rejected: couples
  process telemetry to product history and dirties every PR; correlation is recovered by the
  stamped `git.commit` instead.

## Consequences

- HAR02 ships with **zero new infra and zero repo footprint**; the on-disk transcript + meta
  are the whole input, and `shipit eval` reads them at a run's terminal hook.
- Code↔eval correlation is via the stamped `git.commit`, enabling "changed the implementer
  prompt at commit X → adherence moved" without committing the records.
- A shared view is a deferred, GitHub-native add-on — explicitly not built at rung 2.
