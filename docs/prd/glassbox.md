# Glassbox — observable, unified external-command execution

Authoritative spec. Decisions recorded in ADR-0028 (one Exec seam; Tool
adapters) and ADR-0029 (agents-first JSONL logging); vocabulary in
CONTEXT.md (**Exec**, **Tool adapter**, **Sha**).

## Problem Statement

shipit's entire job is orchestrating external processes — pixi, git, gh, and
the agent backends — two orchestration layers deep (subagent → coordinator →
human), across a growing fleet of consumer repos. That seam is a black hole:

- Four parallel subprocess conventions coexist (a raising runner, two
  independent gh error classes with different redaction behavior, rc-tuple
  returns, and the launch result), so identical failures surface four
  different ways and every new call site picks a convention by copy-paste.
- When a provisioning command fails, its stdout — where pixi writes its real
  diagnostics — is dropped, and nothing durable is written at all. The
  slowest, most failure-prone operations (Tree provisioning, `pixi install`,
  agent launches) leave no record and no timing; whole subsystems narrate
  only via `print()`, which vanishes with the terminal.
- The durable log is freeform text with no correlation fields, so an agent
  debugging a problem cannot slice the record to one PR, one Tree, or one
  Run without scraping prose — burning context tokens on parsing instead of
  diagnosis.
- Nothing sets a subprocess timeout, anywhere; a hung tool hangs its caller
  forever.
- Every layer above the seam hand-parses tool output with scattered string
  heuristics, and per-tool knowledge is split across competing half-modules.

Fleet adoption multiplies every one of these: when shipit runs in a dozen
repos, "a Tree creation failed slowly and silently somewhere" becomes a
recurring, undebuggable event.

## Solution

Turn the black hole into a glass box, in layers:

1. **One Exec seam.** Every external command shipit runs is an **Exec**
   through a single runner: normalized result or one `ExecError` (argv, rc,
   both output streams, duration), a generous overridable default timeout,
   and exactly one structured record per Exec. Central redaction masks every
   secret value the secrets layer has fetched, plus token/PEM patterns, on
   everything logged or raised.
2. **Agents-first JSONL logging.** The durable log becomes JSONL — one JSON
   object per record, human-readable `msg` inside, flat fields, and domain
   keys (`session`, `tree`, `pr`, `run`, `repo`) bound in context and
   propagated to child processes. An agent gets "everything about PR 231"
   with one jq expression; humans read the same log through the `logs` verb's
   renderer. Console stderr stays human.
3. **Lifecycle narration.** The silent subsystems (spawn, tree, prstate, the
   verbs) gain leveled, correlated log events on the fixed conventions —
   milestones at info, mechanics at debug, degradations at warning — so the
   record tells the story without token-burning verbosity.
4. **One Tool adapter per tool.** Per-tool knowledge (argv encoding,
   structured-output harvesting, semantic errors) consolidates into exactly
   one adapter per tool, in the tool's domain home. Adapters prefer the most
   structured output the tool offers (native JSON, then porcelain, then
   converted) and return existing core value objects — never parallel
   adapter-shaped types.
5. **Identity threading.** The core identities already minted (Repo, Backend)
   flow through the layers that today re-parse raw strings: Tree planning
   carries a Repo instead of two loose strings, the review funnel threads a
   Backend instead of a bare agent name, and the new **Sha** value object
   carries commit identity where staleness decisions hinge on it.

## User Stories

1. As an agent debugging a failed Tree provisioning, I want the failing
   command's argv, exit code, duration, and both output streams in one
   structured record, so that I can diagnose the failure without re-running
   it.
2. As an agent two layers deep in an orchestration, I want to slice the
   durable log to one PR, Tree, or Run with a single jq expression, so that I
   spend my context tokens on diagnosis rather than parsing prose.
3. As a coordinator agent, I want every Exec I trigger (directly or through a
   detached child) to carry my bound domain keys, so that the full causal
   story of my session is recoverable from the log after the fact.
4. As a maintainer, I want `pixi install` and `npm ci` output preserved on
   failure, so that a broken provisioning is diagnosable from the record
   instead of by reproduction.
5. As a maintainer, I want tree creation, provisioning steps, and agent
   launches timed, so that I can see where Tree birth spends its time and
   catch regressions.
6. As a maintainer, I want a hung external tool to die at a known timeout
   rather than hang forever, so that a stuck gh call can't wedge a session.
7. As an implementer (human or agent) adding a new call to an external tool,
   I want one obvious runner with one error type, so that I don't pick among
   four conventions or invent a fifth.
8. As a reviewer of shipit PRs, I want "tool argv built outside its Tool
   adapter" to be a statable defect, so that per-tool knowledge stops
   scattering.
9. As a maintainer, I want the two competing gh boundaries merged into one
   adapter, so that redaction and error behavior can't diverge between them
   again.
10. As a security-conscious maintainer, I want every secret value fetched by
    the secrets layer masked in everything logged or raised, so that a token
    can never leak into a log file or an exception message a subagent might
    echo.
11. As an agent reading the log, I want records to carry a human-readable
    `msg` alongside structured fields, so that I (and the human I report to)
    can read the same record.
12. As a human, I want the `logs` verb to render the JSONL log legibly (and
    follow it live), so that the machine-first format costs me nothing.
13. As an agent, I want fields absent when unbound rather than null-stuffed,
    so that records stay small and every field present in a record is
    meaningful (jq presence filters behave the same either way).
14. As the eval/observability layer, I want Exec durations and outcomes as
    structured data, so that future analysis (e.g. provisioning-time trends)
    is a query, not a parse.
15. As a maintainer, I want gh calls to return typed core objects (PR, Repo,
    Sha) from their adapter, so that callers stop re-parsing JSON shapes at
    every site.
16. As the review-readiness engine, I want commit identity carried as a Sha
    value object, so that a short-vs-full or case mismatch can never silently
    flip a review's staleness.
17. As the Tree layer, I want Tree planning keyed by the Repo value object,
    so that owner-case or slug-format variance can't split one repo's
    identity across divergent Tree paths or log directories.
18. As the review funnel, I want the acting reviewer threaded as a Backend,
    so that its derived names (check-run name, funnel login, doppler keys)
    come from the one registry instead of string rebuilding at each site.
19. As a maintainer of the pixi boundary, I want pixi execution (install,
    run-wrapping) and the env-scrub rules to live with the pixi domain
    module, so that pixi knowledge stops leaking into the Tree code.
20. As an agent launched via spawn, I want launch semantics preserved — a
    nonzero child is a normal lifecycle outcome, not an exception — so that
    Run reporting keeps working unchanged over the new runner.
21. As a maintainer, I want the locale-fragile ps parsing replaced by a
    structured converter, so that session liveness doesn't depend on
    hand-parsing positional text.
22. As a fleet maintainer, I want all of the above to hold identically in
    consumer repos, so that debugging a shipit problem in any repo starts
    from the same record shape.
23. As a future contributor, I want the migration to be a hard cutover with
    no aliases or dual formats, so that there is exactly one way to run a
    tool and one log format to read.
24. As the coordinator of the work itself, I want the program layered so the
    logging spray parallelizes across subsystems after conventions are
    fixed, so that multiple agents can land it quickly without merge chaos.

## Implementation Decisions

**The Exec runner (PROC01).**

- One runner executes every Exec. Its result carries rc, stdout, stderr, and
  duration; its single transport error `ExecError` carries argv, rc, both
  streams, and duration. Missing binaries normalize into `ExecError` — never
  a raw OS exception. Semantic subclasses exist only where a caller genuinely
  branches on meaning; there is no per-tool exception hierarchy (ADR-0028).
- Default timeout: 5 minutes, per-call overridable, `None` allowed for
  legitimate long-runners (agent launches, cold provisioning). Timeout expiry
  is an `ExecError` with a timeout cause and partial output captured.
- Every Exec emits exactly one structured record: argv, cwd, rc,
  `duration_ms`; on failure, tails of both streams. Success at debug level,
  failure at error level.
- Redaction is central and fail-safe: the secrets layer registers every
  fetched value with the redactor; pattern rules (GitHub token prefixes, PEM
  blocks, Doppler token prefixes) catch inherited tokens. Redaction applies to
  everything the runner
  logs or attaches to errors. No redaction package is adopted (none is
  credible; ADR-0029 notes the survey).
- The spawn launch path becomes a consumer view over the runner, keeping its
  existing result semantics (nonzero child = normal lifecycle outcome,
  ADR-0019/0020).
- The old conventions — the raising proto-runner, both gh error classes, the
  rc-tuple returns, the raw one-off subprocess calls — are deleted with no
  aliases.

**Logging (LOG01 + spray).**

- The file log is JSONL: flat top-level fields `ts` (ISO-8601 UTC), `level`,
  `logger`, `msg`, domain keys present-when-bound (absent, not null), event
  extras flat. No OTel log model, no nesting (ADR-0029).
- Correlation is domain keys only — `session`, `tree`, `pr`, `run`, `repo` —
  no synthetic trace/span ids. Keys bind via context at the CLI entry and at
  the spawn/detach seams; they cross process boundaries via the environment
  and rebind at the child's logging setup.
- Built on structlog: processor pipeline (context-merge → redact → render)
  attached to the existing logging setup's handlers via its stdlib
  formatter, so rotation, per-repo paths, and idempotent handler naming
  survive; untouched stdlib call sites participate via the foreign-record
  chain, letting the spray proceed subsystem-by-subsystem.
- Console stderr stays human-formatted. The `logs` verb renders JSONL for
  humans (and passes raw for tooling). Hard cutover: no dual-format period;
  the verb reads JSONL only.
- Spray conventions: lifecycle milestones at info (tree created, review
  posted, spawn launched — with durations where meaningful); mechanics at
  debug; degraded-but-continuing outcomes at warning; failures that
  propagate at error with the exception attached. User-facing verb output
  remains print/echo — but anything that is the only record of an action
  must also log.

**Tool adapters (PROC02).**

- Exactly one adapter per tool, living in the tool's domain home; no
  physical adapter layer/directory. The gh merge (two boundaries → one
  adapter) is mandatory; the git bypass in the review diff path routes
  through the git adapter; pixi execution and the env-scrub rules join the
  pixi domain module alongside its existing read side.
- Adapters harvest the most structured output the tool offers: native JSON
  (gh's json flags and GraphQL; pixi's list/info/shell-hook) over porcelain
  formats (git) over converted output (`jc`, evaluated first for the ps
  liveness probe). The duplicated gh pagination-merging helper collapses
  into the gh adapter.
- Per-tool timeout defaults live in adapters; raw runner calls state their
  own.

**Typed results & identity threading (PROC03 + the identity track).**

- Adapters return existing core value objects — PR, Repo, Backend — never
  dicts or parallel snapshot types (the avoided PullContext/PRContext
  disease). New types are minted only where exploration showed real bug
  risk.
- **Sha** is minted in the core-identities family: validated hex, lowercase
  normalized, equality refuses silent prefix-vs-full comparison (prefix
  matching is an explicit method). PR head/commit identity and the
  review-staleness comparison adopt it.
- Tree planning carries a Repo instead of two loose owner/name strings; the
  hand-rolled slug splitters route through the one canonical slug parser.
- The review funnel threads a Backend value object instead of a bare agent
  name; derived names (check-run name, funnel login, doppler key prefix)
  come only from the Backend registry.
- The Branch value object is explicitly deferred to the identity-threading
  track's backlog (it belongs to tree/naming, not the exec boundary).

**Sequencing.**

- LOG01 (logging infra) and PROC01 (runner) proceed in parallel — disjoint
  modules. The logging spray and identity threading follow, parallelizable
  per subsystem once conventions are fixed. PROC02 (adapters) then PROC03
  (typed results) complete the program. Where identity threading and PROC02
  touch the same review modules, the work streams are explicitly ordered.

## Testing Decisions

- Tests assert external behavior at module seams, not implementation: a good
  runner test injects a fake process and asserts the result/error/record
  contract; a good adapter test injects a fake runner and asserts argv
  encoding and typed returns; neither spawns real tools.
- Prior art to follow: the existing injectable-runner fixtures (the pixi
  read-side and launch tests) and the wiring-vs-engine test split the verb
  suites already use.
- Modules with isolated test suites: the Exec runner (result, error,
  timeout, capture, missing-binary, record emission), the redactor
  (registered values, patterns, applied-to-everything), log context
  (bind/rebind, env round-trip, absent-when-unbound), logsetup's JSONL
  pipeline (record shape via capture), the logs renderer, each Tool adapter,
  Sha, and the re-seated launch view.
- The logging spray is tested by convention, not per-line: no assertions on
  individual message strings; assertions that key lifecycle events exist and
  carry the required fields belong to the subsystem's existing tests.
- Live/guarded integration checks (real pixi/gh) remain the rare exception,
  following the existing guarded-test pattern.

## Out of Scope

- CLI shape-option unification (the tree/spawn `--epic/--ws/--issue`
  validation drift) — a separate small track.
- The TOML-writing replacement (tomlkit) and other package adoptions beyond
  structlog and the scoped jc evaluation.
- A GitHub-status enum module and cross-boundary golden-file drift tests —
  separate drift-guard track.
- The externalized-strings tail (reviewer prompt, deny reason, scaffolds)
  and generating the review schema prose from the schema.
- The Branch value object (deferred to identity-threading's backlog).
- The dev-only argparse harnesses (dogfood, funnel-verify) — untouched until
  they fold into the CLI tree.
- Synthetic trace ids, log shipping, or any observability platform — revisit
  only on demonstrated need (ADR-0029).
- Eval-record changes: the eval store keeps its own JSONL + OTel gen_ai
  vocabulary; glassbox does not touch it.

## Further Notes

- ADR-0028 (one Exec seam; Tool adapters) and ADR-0029 (agents-first JSONL
  logging) record the load-bearing decisions; CONTEXT.md gained **Exec**,
  **Tool adapter**, and **Sha**.
- The earlier pixi-encapsulation PRD (wf01) overlaps this program's PROC02
  slice; when PROC02 is planned, reconcile against it — glassbox's Tool
  adapter shape supersedes wf01's encapsulation half, while wf01's generic-CI
  half is untouched.
- Epic codes (LOG01, PROC01–PROC03, plus the spray and identity-threading
  epics) are assigned at issue-planning time; the layering above is the
  intended topology, one epic per layer, decomposed in the issues leg.
- The 5-minute default timeout means known long-runners must carry explicit
  overrides — set once at their adapter or call site during PROC01
  migration; a missed one surfaces loudly (a timeout error), not silently.
