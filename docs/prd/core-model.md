# Core Model (COR01)

> Canonical value objects for shipit's core nouns + riding pixi primitives, under
> "one definition, routed everywhere." Spec for epic **COR01**.
>
> Grounded in `CONTEXT.md` (the *Core identities* + agent-harness vocabulary) and
> ADRs **0021** (value objects + functional core), **0022** (layer boundary),
> **0023** (stay Python, Rust deferred), **0024** (core identities), **0025** (agent
> axes).

## Problem Statement

shipit has no canonical value objects for its core nouns — **Repo**, **PR**,
**Reviewer** (and the agent that drives a **Run**). Each subsystem invents its own
identity key and its own snapshot type, so the same noun is modeled several
incompatible ways:

- **Repo is three things at once.** The **eval record** store keys by *resolved
  filesystem path* while `logsetup` keys by origin `owner/repo`. For one repo the
  telemetry and the logs live under keys that cannot be joined, and because a
  **Tree** is a fresh clone, every Run scatters a new eval store — verified as 60 of
  61 stores orphaned. Separately, `git rev-parse --show-toplevel` is re-implemented
  four times because the boundary function lacks a `cwd`.
- **PR is modeled twice.** A readiness snapshot and a review snapshot overlap on
  `number`/`head_sha`/`base_ref`, `head_sha` is fetched three different ways, and one
  builder hardcodes `is_draft=False` — a latent trap.
- **The reviewer/backend agents live in three parallel class hierarchies** (a PR-funnel
  adapter, a spawn/launch adapter, and a vestigial dead ABC) with ~6 identity aliases
  per agent and two model-alias maps. Adding or renaming a reviewer touches many places.
- **shipit reinvents machinery pixi already provides:** a hand-built sccache build env
  (that does not even reach the agent's own in-Tree `cargo`), an entirely idle task
  cache, and env identity re-derived by hand instead of read from pixi.

The cost is constant: a change to "which repo is this" or "who reviews" or "the PR
head" ripples across many sites, telemetry fragments, and the model drifts a little
further every epic.

## Solution

Introduce **canonical value objects for the core nouns and route every subsystem
through them** — the "one definition, routed everywhere" principle the codebase already
proves with `content_hash` and the shared env-scrub predicate — and **ride pixi
primitives instead of reinventing them**.

Concretely, four **deep modules**, each a thin functional core over one injected
boundary (git / gh / pixi-JSON):

- **`identity`** — `Repo` / `Owner` / `OwnerKind` / `WorkingDir` and their resolvers.
- **`agent`** — `Backend` / `Model` / `Invocation` and one shared agent-backend identity.
- **`pr`** — a single `PR` value object with composing readiness/review views.
- **`pixienv`** — pixi JSON → env-identity and activation value objects.

Everything is written as thin, composable value objects with logic as free functions
over them (ADR-0021); shipit borrows pixi's env/path model via pixi's JSON and owns the
git / GitHub / agentic layers (ADR-0022); it stays in Python, with a Rust rewrite held
as a separate spike (ADR-0023).

The user of this feature is primarily **the maintainer and the agent fleet** — a cleaner
model means fewer places to change, telemetry that joins, and a stable base for future
capability.

## User Stories

1. As the maintainer, I want one canonical `Repo` identity derived from the origin
   remote, so that every subsystem agrees on "which repo is this."
2. As the maintainer, I want the eval store keyed by `Repo` identity, so that a repo's
   eval records and logs join on the same key instead of scattering per clone.
3. As the eval harness, I want a Run's records to land under a stable Repo key
   regardless of which **Tree** it ran in, so that `shipit eval report` aggregates the
   whole repo, not one orphaned clone.
4. As a developer, I want `Repo` derived *locally* from `git remote get-url origin`, so
   that identity works offline and inside a Tree with no API call.
5. As a developer, I want `OwnerKind` (user vs organization) modeled but kept out of Repo
   identity, so that org-only capabilities have a home later without destabilizing the
   store key today.
6. As a developer, I want a single `resolve_working_dir(cwd)` that answers "what repo +
   revision is checked out here," so that the four hand-rolled `git rev-parse
   --show-toplevel` copies collapse to one.
7. As a maintainer, I want `WorkingDir` to compose a `Repo` (not inherit), so that a
   Tree, a read-only Tree, and the main checkout are all expressible as one location
   type without a class hierarchy.
8. As the coordinator, I want one `PR` value object with identity `(repo, number)` and a
   cheap core, so that any code needing "a PR" starts from one definition.
9. As a developer, I want the readiness path and the review path to build distinct views
   that compose a `PR`, so that neither carries a field it never fetched (no more
   defaulted `is_draft`).
10. As a developer, I want `head_sha` fetched exactly one way, so that "the head under
    review" cannot disagree between subsystems.
11. As the maintainer, I want the vestigial review `Backend` ABC deleted, so that dead
    code stops inviting a fourth hierarchy.
12. As the maintainer, I want one agent-backend identity (name + every alias) defined
    once and referenced by both the reviewer-funnel and the spawn-launch axes, so that
    renaming or adding a backend touches one place.
13. As a developer, I want `Backend` (the harness) kept orthogonal to `Reviewer` (the
    funnel role) and to `Role`, so that an App reviewer with no backend, and a backend
    that serves implementers, are both expressible.
14. As the harness, I want `Model` modeled separately from `Backend` with a `Provider`,
    so that a model of one provider running under another backend is expressible.
15. As the harness, I want `ReasoningLevel` chosen per `Invocation` (distinct from a
    Model's reasoning capability), normalized across backends, so that eval can compare
    "high reasoning" runs regardless of backend.
16. As the eval harness, I want each Run's `Invocation` (backend × model × reasoning)
    captured as *observed* from the transcript alongside the *intended* config, so that
    the record reflects what actually ran.
17. As the maintainer, I want `shipit eval report` to group by `Invocation`, so that I
    can see which backend/model/reasoning configuration performs best per role — the data
    that later drives configuration iteration.
18. As the maintainer, I want a single source for the required-reviewer default, so that
    a repo's required set does not depend on whether `install` happened to run.
19. As the maintainer, I want the sccache build env declared in pixi `[activation.env]`,
    so that pixi sets it on every activation and it reaches the agent's own in-Tree
    `cargo` — which it does not today.
20. As a developer, I want pixi's task `inputs`/`outputs` cache turned on for idempotent
    tasks, so that `provision-lexd` stops re-fetching lexd on every lint/fmt.
21. As a developer, I want env identity read from pixi (`conda-meta/pixi`, `pixi info
    --json`) instead of re-derived, so that shipit rides pixi's model rather than
    shadowing it.
22. As a developer, I want env handling expressed as pure transforms over immutable env
    snapshots, so that "what activation adds" comes from `pixi shell-hook --json`, not
    hand-derivation.
23. As a contributor, I want the core nouns to be thin value objects with functional
    logic, so that the model is testable and composable and resists drifting into deep
    OOP.
24. As a future contributor, I want the modeling style, the pixi layer boundary, and the
    language decision recorded as ADRs, so that I understand *why* the code looks the way
    it does before "fixing" it.
25. As the maintainer, I want the design captured language-agnostically, so that if a
    future Rust spike wins, the value objects and boundaries transfer.
26. As a reviewer of this epic, I want each WS to cite the ADR it implements, so that the
    spec and the execution tracker stay tied to the decisions.
27. As the maintainer, I want the parked pixi knowledge-base refresh to land with the
    WS-pixi-activation work, so that the substrate doc and the code it documents ship
    together.
28. As a developer, I want no backwards-compat shim for the eval re-key, so that the old
    path-keyed stores simply orphan (local, regenerable data) rather than growing a
    migration subsystem.

## Implementation Decisions

**Modeling style (ADR-0021).** Thin, composable value objects (frozen dataclasses by
default, not mandatory) with logic as free functions over them; mutable state isolated
at boundaries (boundary → immutable snapshot → functional core → effecting edge); no
mutable module-global state (the reviewer required/rerun caches become passed values or
memoized pure functions).

**Layer boundary (ADR-0022).** Env / paths / activation are **borrowed from pixi via its
JSON** (`info` / `list` / `shell-hook --json` + `conda-meta/pixi`); shipit never
re-derives activation and does **not** import `py-rattler`. git identity, GitHub, and the
agentic layer are **owned** as shipit value objects.

**Module `identity` (WS-Repo).**

- `Repo = (owner, name)`; identity derived locally from `git remote get-url origin`.
- `Owner = (login, kind)`; `OwnerKind ∈ {user, organization}` is optional, lazily
  resolved, and **excluded from identity/equality**.
- `WorkingDir = (path, Repo, revision{branch, commit})`; composes a `Repo`. A **Tree**
  *has-a* WorkingDir; the **main checkout** is a WorkingDir that is not a Tree.
- Resolvers: `resolve_repo(cwd)`, `resolve_working_dir(cwd)`, `resolve_owner_kind(repo)`
  (the only one that touches the API).
- The **eval store re-keys by `Repo` identity** (origin `owner/name`), replacing the
  resolved-path key. **No compat** — existing path-keyed stores orphan.
- The single git boundary gains a `cwd` parameter; the four re-implementations of
  `git rev-parse --show-toplevel` are removed in favor of it.

**Module `agent` (WS-Reviewer).**

- `Backend` (closed registry `claude | codex | antigravity`) owns launch behavior and one
  **identity** (canonical name + all aliases) defined once and shared with the reviewer
  funnel. `Backend ⊥ Reviewer` and `Backend ⊥ Role`.
- `Model = (id, provider, reasoning_capability)`, decoupled from Backend; `Provider ∈
  {anthropic, openai, google, …}` (closed registry).
- `Invocation = Backend × Model × ReasoningLevel (+ permission_mode)`; `ReasoningLevel ∈
  {low, medium, high}` chosen per invocation. Backend×Model validity is a lookup, not a
  structural constraint. `Invocation` is threaded spawn → Run → **eval record** (observed
  and intended) and becomes a group-by dimension for `shipit eval report`.
- The vestigial review `Backend` ABC and its dead impls are deleted.
- The required-reviewer default is defined in **one** place.

**Module `pr` (WS-PR).**

- `PR = identity (repo, number) + core (head_sha, base_ref, is_draft, merge_state)`.
- One `head_sha`/core fetch boundary. The readiness view (`+ reviews / threads / funnel /
  timing`) and the review view (`+ diff / changed_files / workdir`) **compose** a `PR`.
- The two competing snapshot types are replaced by `PR` + views.

**Module `pixienv` (WS-pixi-activation).**

- Value objects mirroring pixi's JSON: env identity from `conda-meta/pixi` (carries
  `environment_lock_file_hash`, distinct from the `.pixi-environment-fingerprint`), and an
  `Activation` snapshot from `shell-hook --json`.
- `sccache_env()` moves into pixi `[activation.env]` (a manifest change); the Python
  builder is removed.
- pixi task `inputs`/`outputs` are declared to enable the skip-if-unchanged cache,
  starting with `provision-lexd`.
- The parked pixi KB refresh (branch `docs/pixi-kb-refresh`) lands here.

## Testing Decisions

**What a good test is:** exercises **external behavior** through a module's public
interface, never its internals. Each of the four modules is a pure functional core over
an injected boundary, so tests feed a fake boundary (a stubbed remote URL / `rev-parse`
result, a fixture `gh` payload, a captured pixi-JSON blob) and assert the returned value
objects — no patching of private helpers, no reliance on a live repo or network.

**Modules under test (all four):**

- `identity` — `resolve_repo` / `resolve_working_dir` over stubbed git output (including
  the offline path); `OwnerKind` enrichment kept out of equality; and the **eval-store
  re-key regression** proving one repo's records land under one key across two clone
  paths (the load-bearing scatter bug).
- `agent` — the alias registry (a backend name → every alias); `Backend ⊥ Model`
  (a cross-provider pairing is expressible); `Invocation` captured observed-vs-intended.
- `pr` — `PR` core built once; readiness and review views compose it; the single
  `head_sha` fetch; a view cannot expose a field its path never fetched.
- `pixienv` — parse fixture `conda-meta/pixi` / `shell-hook --json` into env-identity /
  `Activation` value objects; env transforms are pure over an immutable snapshot.

**Prior art:** existing unit tests patch the `shipit.gh` boundary and assert pure logic
(`test_gh_setup`, the prstate tests); the eval tests already read fixture transcripts;
`test_spawn_launch` asserts argv/env construction over an injected runner. The Core Model
tests follow the same injected-boundary + fixture shape.

## Out of Scope

- **The configuration optimizer.** Core Model *captures* `Invocation` data and adds the
  eval report dimension; automatically iterating to an optimal backend/model/reasoning
  config is deliberately later (rich model now, build later).
- **The Rust rewrite** (ADR-0023) — a separate strategic spike; not this epic.
- **`Run` as a new value object** — Run is already canonical (`CONTEXT.md`); only its
  minor double role-resolution is tidied, not headlined.
- **The reviewer read-only-Tree bare launch** — accepted by design (no env to route
  into); only flips if a reviewer ever needs a Tree-pinned tool.
- **Provisioning-log capture** — a separate thin fix (stop discarding `run_provision`
  output); noted in the pixi KB, not part of Core Model.
- **HAR04 subjective agent-as-judge eval** — unrelated deferral.
- **Any migration shim** for the orphaned eval stores — no-compat by decision.

## Further Notes

- **One epic, four WS.** COR01 is a single umbrella; the WS (Repo / Reviewer / PR /
  pixi-activation) are independently landable, with WS-Repo (the `identity` module) the
  natural anchor since the eval re-key and the shared boundary underpin the others.
- **Design already committed** on branch `docs/core-model-planning` (commit `6a64c82`):
  the `CONTEXT.md` glossary + ADRs 0021–0025. This PRD is the spec that ties them
  together; the epic tracker issue + WS sub-issues are created later via
  `/shipit-to-issues`.
- **Known WS risks (to record, not blockers):** (1) whether pixi `[activation.env]`
  template vars can express a **per-Tree absolute path** for `SCCACHE_BASEDIRS` — verify
  before committing the sccache migration; (2) the `inputs`/`outputs` cache must be scoped
  so it cannot mask a real failure on the deliberately hard-fail lint path — start with
  `provision-lexd`, not the linters themselves.
- **WS-pixi-activation bundles the parked pixi KB refresh** (branch `docs/pixi-kb-refresh`
  @ `2568e06`) so the substrate doc ships with the code it documents.
