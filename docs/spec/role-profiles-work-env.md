# Role Profiles and Work Environments

> Authoritative successor to the historical
> [Role Profiles and Work Env Foundation](../legacy-prd/role-profiles-work-env.md).
> Governing decisions:
> [ADR-0047](../adr/0047-role-profiles-and-work-env-are-not-consumer-config.md)
> and [ADR-0046](../adr/0046-review-proposals-do-not-land.md). Related execution
> decisions:
> [ADR-0018](../adr/0018-read-only-trees.md),
> [ADR-0022](../adr/0022-layer-boundary-model-vs-borrow-pixi.md),
> [ADR-0027](../adr/0027-coordinator-session-tree-ephemeral.md),
> [ADR-0028](../adr/0028-one-exec-seam-tool-adapters.md),
> [ADR-0039](../adr/0039-tools-as-verbs.md), and
> [ADR-0050](../adr/0050-review-scope-is-the-diff-context-is-the-checkout.md).

## Context

Shipit has five fixed Roles: coordinator, implementer, shepherd, explorer, and
reviewer. Role prose is already authored once in Lex and generated into
role-scoped prompt surfaces. Trees already have distinct session, writable, and
shared read-only forms. Spawned write Runs execute in their Tree's pixi
environment, coordinator sessions borrow pixi activation, reviewer Runs use an
unprovisioned read-only Tree with ambient tools, and fleet and CI execution have
their own established routing.

Several concerns that were future work in the historical proposal have since
shipped: the one-Exec seam, Tool verbs and Lane planning, review-round records,
the convergent review loop, coordinator host seams, and backend-neutral session
resume. These are now constraints and integration points, not work to recreate.

The structural definition of a Role nevertheless remains distributed. The Role
registry, generated prompt metadata, brief availability, harness enforcement,
spawn routing, and backend posture each restate part of the model. In particular,
detached spawn treats every role except the literal reviewer as a new writable
Run. That permits an explorer or unknown role to enter a write-Tree path and
treats a shepherd like a new implementer rather than a persistent actor attached
to an existing PR.

The glossary already reserves **Role Profile**, **Tree Profile**, and **Work
Env**. This Spec turns those nouns into one current implementation boundary while
preserving pixi, WorkingDir, Tree, Exec, Tool, Lane, Role definition, and host
adapters as the owners of their existing concerns.

## Problem

There is no authoritative structural answer to "how may this Role run?" A caller
must currently know several unrelated tables and special cases to determine:

- which checkout allocation and attachment strategy the Role needs;
- which mutations, execution, network access, and result-channel effects it
  requires;
- whether it has a generated agent surface or a task brief;
- which launch contexts support it; and
- whether its tools come from a provisioned checkout, borrowed activation, or
  the ambient host.

The duplication has already produced invalid combinations. Unknown roles are
accepted by detached spawn, explorer can receive a write Tree, and shepherd can
be routed through the new-branch/draft-PR implementer handshake. Reviewer remains
a string special case even though its checkout and result path are materially
different from all write Runs.

At the same time, a broad Work Env runner would now duplicate mature mechanisms.
Exec already owns external-process execution. Pixi owns environment activation
and environment identity. WorkingDir owns checkout location, and Tree adds
shipit-provisioned provenance and lifecycle. Tools and Lanes already describe
what runs. The missing abstraction is a resolved description of where and with
which activation work runs, not another executor.

## Goals

1. Declare each known Role's structural execution shape once in a fixed,
   Shipit-owned Role Profile registry.
2. Make role validation, generated surfaces, brief availability, launch routing,
   and enforcement consume that registry instead of local role-name tests.
3. Represent checkout strategy without mixing allocation, attachment, lifetime,
   and mutation into a single flat enum.
4. Give implementer, shepherd, reviewer, explorer, and coordinator distinct,
   valid launch contracts that match the current development cycle.
5. Define Work Env as a small resolved value composed from WorkingDir, optional
   Tree provenance, and pixi-owned activation or environment identity.
6. Infer Work Env from execution context; do not add consumer configuration for
   Role Profiles or Work Envs.
7. Preserve the existing Exec, Tool, Lane, Tree provisioning, pixi, review, and
   host-adapter boundaries.
8. Fail unsupported role/launch combinations clearly before provisioning or
   launching work.
9. Make the complete Role registry and Work Env resolution behavior testable at
   module interfaces without live agent backends.

## Non-Goals

- Consumer-defined Roles, Role Profiles, checkout strategies, or Work Envs.
- A universal Work Env runner or a replacement for Exec.
- Reimplementing pixi activation, PATH calculation, package solving, or
  environment identity.
- Refactoring Tools, Lanes, workflow blocks, or the release pipeline around a new
  execution framework.
- Rebuilding review-round records, review experimentation, severity, breakers,
  or coordinator host adapters.
- Making Work Env a security sandbox.
- Implementing Review proposals or Proposal Work Envs.
- Completing a general PR-engine reason-code project.
- Reintroducing a persistent fleet executor; fleet coverage refers to the
  current one-shot fleet-sweep cells.

## Proposed Shape

Shipit gains two deep modules with small public interfaces.

The **Role Profile registry** is a total mapping from every fixed Role to its
structural profile. A profile selects a checkout strategy, declares its required
enforcement posture, identifies generated prompt and brief surfaces, restricts
valid launch contexts, and names the Run's result channel. Role definitions
remain the sole source of behavioral prose.

The checkout strategy is a structured closed value rather than the historical
flat `session | write | read-only | ambient` list. It distinguishes:

- a session Tree created for a coordinator;
- a new writable Tree and branch created for an implementer;
- a writable Tree attached to an existing PR for a shepherd;
- a shared read-only Tree pinned to an existing PR head for a reviewer; and
- an ambient WorkingDir with no Tree for an explorer.

The **Work Env resolver** returns the execution context for known boundaries. A
Work Env contains a WorkingDir, optional Tree identity/provenance, checkout
strategy, optional pixi Activation and EnvIdentity, and the selected execution
routing mode. Absence of pixi activation is explicit and valid, notably for
reviewer read-only Trees and non-pixi repositories.

Work Env is consumed by existing launch and planning paths. Commands still run
through Exec; pixi-backed execution still uses pixi's adapter; coordinator
activation still borrows `shell-hook`; CI still runs declared Lanes; fleet sweep
still provisions its cell Trees. Work Env makes those decisions explicit and
consistent without replacing their executors.

## User / Agent Stories

1. As a maintainer, I want every fixed Role to have exactly one Role Profile, so
   that adding or changing a Role cannot leave spawn, prompts, briefs, and
   enforcement inconsistent.
2. As a maintainer, I want Role Profiles fixed by Shipit, so that consumer
   configuration cannot weaken development-cycle guarantees.
3. As a coordinator, I want an isolated session Tree, so that concurrent
   coordinator sessions never share a working tree, index, or HEAD.
4. As an implementer, I want a new writable Tree on the intended issue or Work
   Stream branch, so that I can build, verify, commit, and open one draft PR
   without touching another checkout.
5. As a shepherd, I want a writable environment attached to the existing PR
   head and preserved across review rounds, so that I address the same PR rather
   than open a replacement PR.
6. As a reviewer, I want a shared read-only Tree pinned to the reviewed head, so
   that review context is correct and the reviewed checkout remains immutable.
7. As a reviewer service, I want reviewer output captured and posted through one
   product result path, so that generic self-posting and service-posting review
   semantics cannot drift.
8. As an explorer, I want ambient, read-oriented investigation with no provisioned
   Tree, so that open-ended research stays cheap and cannot accidentally become a
   write Run.
9. As a maintainer, I want unsupported Role and launch-context combinations to
   fail before Tree creation, so that a string typo cannot acquire write access.
10. As a hook maintainer, I want unknown native subagent identities to retain a
    safe non-coordinator fallback, so that an unrecognized worker is not
    mistakenly governed as the coordinator.
11. As a maintainer, I want enforcement posture expressed by operation and
    resource, so that checkout writes, command execution, network access,
    Git/GitHub mutation, and temporary or artifact writes are not collapsed into
    a misleading boolean.
12. As a maintainer, I want Work Env to compose pixi's Activation and EnvIdentity,
    so that Shipit never invents a competing environment model.
13. As a CI maintainer, I want a fresh Actions checkout plus its planned pixi
    environment represented as a non-Tree Work Env, so that CI and local
    execution use the same vocabulary without requiring the same executor.
14. As a fleet operator, I want each fleet-sweep cell's provisioned Tree and pixi
    routing represented as a Work Env, so that adoption evidence says precisely
    where it ran.
15. As an operator, I want logs to carry existing repo, session, Tree, Run, Role,
    and environment identifiers when available, so that Role Profile and Work Env
    decisions can be diagnosed without introducing a pixi run identifier that
    does not exist.

## Design Decisions

### Role Profile is structural, Role definition is behavioral

The Role Profile registry is keyed by the closed Role type and is exhaustive.
It owns checkout strategy, enforcement posture, generated-surface metadata,
brief availability, supported launch contexts, and result-channel shape. It may
reference a Role definition but never contains role prose. Lex Role definitions
continue to generate the behavioral prompt content.

Public and programmatic structural boundaries accept Role values, not arbitrary
strings. Parsing an unknown role fails clearly before provisioning. The native
hook role resolver is the deliberate exception: an unknown non-empty native
subagent identity remains an unknown worker posture rather than falling through
to coordinator. That compatibility rule is local to hook interpretation and does
not make the unknown identity spawnable.

### Checkout strategy separates orthogonal concerns

Checkout strategy encodes allocation and attachment, while enforcement posture
encodes permitted effects. A session Tree is writable but has different lifetime
and branch behavior from an implementer Tree. Implementer and shepherd are both
writable, but one creates a branch and draft PR while the other attaches to an
existing PR and resumes across rounds. Reviewer is branch-pinned and read-only;
explorer is ambient and has no Tree.

The existing **Tree Profile** vocabulary remains the user-facing summary, but its
implementation is a structured closed value rather than four mutually exclusive
tokens that mix these axes.

### Role launch and result contracts are explicit

Each profile declares its supported launch contexts and result channel:

- coordinator: host-specific session launch into a session Tree; result is the
  human-facing orchestration session;
- implementer: detached or supported native write launch into a new write Tree;
  result is one verified draft PR;
- shepherd: launch or resume against an existing PR in its persistent write
  environment; result is commits and resolved review threads on that PR;
- reviewer: bounded launch against a shared read-only Tree; result is captured
  structured review output posted by the review service; and
- explorer: native ambient investigation; result is a report to the coordinator,
  with detached Tree-backed spawn rejected.

The historical generic reviewer self-posting path must converge on the product
review service or be retired. Two reviewer result contracts must not survive
behind the same Role.

### Enforcement posture is capability-shaped, not a mutation flag

A Role Profile describes required posture across at least checkout mutation,
command execution, network access, Git/GitHub mutation, and temporary or
artifact writes. This is a policy input, not a claim of sandbox security.

The read-only Tree remains the load-bearing checkout guard for reviewers, while
backend-native restrictions remain defense in depth. Reviewer access to network
or temporary output does not imply permission to mutate the reviewed checkout.
Explorer's ambient posture is likewise enforced by its supported tools and
launch context rather than by pretending an ambient checkout is a sandbox.

### Work Env composes existing value objects

Work Env is a resolved value over the existing WorkingDir abstraction. Optional
Tree provenance says whether Shipit provisioned the checkout and which lifecycle
owns it. Optional pixi Activation and EnvIdentity are borrowed from pixi's JSON
and on-disk metadata through the existing adapter. Execution routing records
whether the existing caller should use pixi-run wrapping, an activation snapshot,
or ambient tools.

Work Env never derives activation or PATH itself. It never reaches beneath pixi
to conda primitives. It does not create a new environment UUID or Run identifier;
pixi provides neither.

The ordinary human or CI checkout remains a **Main checkout** represented by a
WorkingDir. "Direct checkout" may describe its relationship to Work Env, but does
not become a competing checkout identity.

### Resolution is boundary-specific and inferred

Resolvers cover the contexts that exist today: coordinator session Trees, new
write Runs, shepherd PR attachment, reviewer read-only Trees, ambient explorer
investigation, Main checkouts, CI Lane jobs, and fleet-sweep cells. Inputs come
from the current execution boundary and existing typed values. No `.shipit.toml`
surface selects a Role Profile or Work Env.

The first implementation may expose boundary-specific constructors behind one
common value rather than force every executor through one universal resolver.
The invariant is one resulting model and shared decisions, not one oversized
function.

### Existing execution owners remain authoritative

Exec remains the only external-process seam. Tool adapters retain command and
parsing knowledge. Pixi retains provisioning, activation, environment identity,
and run wrapping. Tree modules retain checkout materialization and cleanup. Host
adapters retain host-specific configuration, payload translation, argv, and auth
posture. Tool and Lane retain what runs; Work Env names where and with which
activation it runs.

### Migration is fail-closed and behavior-preserving where valid

Profile consumers move onto the registry as one coherent migration, with
invariant tests preventing a half-migrated role. Existing valid coordinator,
implementer, and reviewer behavior remains stable. Invalid behavior becomes a
clear refusal: unknown roles, detached explorer spawn, and
shepherd-through-new-implementer-branch are not compatibility contracts.

## Alternatives Considered

### Keep the existing scattered constants

Rejected because the current contradictions demonstrate that tests local to each
table do not prove the cross-module role model is coherent.

### Keep a flat Tree Profile enum

Rejected because `session`, `write`, `read-only`, and `ambient` mix lifecycle,
access, allocation, and branch attachment. The enum cannot express the
implementer/shepherd distinction without new special cases.

### Make Role Profiles consumer-configurable

Rejected by ADR-0047. The fixed model must be battle-hardened before consumer
variation could justify weakening or extending it.

### Use one boolean mutation flag

Rejected because a reviewer may need network and temporary output while the
reviewed checkout stays immutable, and an explorer may read through Bash without
becoming a write Run.

### Build a universal Work Env runner

Rejected because it would duplicate Exec and flatten intentionally different
lifecycles: an unbounded agent Run, a bounded Tool Exec, CI workflow execution,
and fleet-sweep cells.

### Recompute environment activation in Shipit

Rejected by ADR-0022. Pixi's activation is borrowed through its supported JSON
and execution surfaces; Shipit does not maintain a rival PATH model.

### Give every Role a Tree

Rejected because explorer is ambient, while reviewer proves that branch pinning,
not read-only behavior alone, determines whether a Tree is needed.

## Risks And Rabbit Holes

- Turning Role Profile into a god object. Behavioral prose, backend argv, Tree
  materialization, and execution must remain in their existing modules.
- Treating capability declarations as a complete security boundary. Filesystem,
  host, and backend enforcement still carry the guarantees.
- Forcing one launcher to support every Role. Unsupported contexts should fail
  until their dedicated lifecycle exists.
- Losing the hook's unknown-worker safety while making public spawn fail closed.
  These boundaries intentionally have different error behavior.
- Accidentally provisioning a reviewer pixi environment. Reviewers currently use
  ambient read tools because their shared Tree is unprovisioned and read-only.
- Conflating Work Env with pixi environment or inventing a nonexistent pixi run
  identity.
- Expanding shepherd attachment into a second PR engine. The existing engine and
  one-shepherd-per-PR lifecycle stay authoritative.
- Folding review experiments, Proposal Work Env, or detailed PR reason codes into
  the foundation because the vocabulary is adjacent.
- Making CI instantiate local Tree objects merely to share terminology. A CI Work
  Env is a Main checkout plus its planned activation, not a simulated Tree.

## Cross-Cutting Concerns

### Security and secrets

Read-only reviewer Trees continue to omit `.treeinclude` material and pixi
provisioning. Write Trees retain existing inclusion and provisioning policy.
Backend auth scrubbing and environment-leak scrubbing remain authoritative.
Work Env values and logs must not contain secret values or full environment
snapshots.

### Observability

Role Profile and Work Env resolution decisions should use the existing structured
logging pipeline and domain keys. Records may identify Role, checkout strategy,
Tree, WorkingDir, pixi environment name or fingerprint, and routing decision when
available. They must not create a fake pixi run id.

### Compatibility and migration

Generated role artifacts remain generated from Lex. Existing config gains no new
required fields. Current valid CLI behavior remains stable except where it admits
an invalid role/launch combination. Error messages should name the Role, requested
launch context, and supported alternatives.

### CI and release

The implementation must pass the existing lint and test suites. Generated role
surfaces must be regenerated and drift-checked when their metadata source moves.
No workflow or release-pipeline redesign is required.

### Performance

Role Profile lookup and Work Env resolution are local and deterministic. They
must not introduce provisioning, pixi solves, network reads, or process launches
merely to describe a context. Expensive facts are supplied by the boundary that
already obtained them.

## Testing / Verification

Testing is required for every agreed module and integration boundary. Unit and
integration tests use fakes at effectful edges; no live agent backend is required.

### Role Profile registry

- Assert the registry is total and one-to-one over the closed Role enum.
- Assert every Role has one checkout strategy, enforcement posture, launch-context
  set, generated-surface declaration, brief declaration, and result channel.
- Assert prompt/frontmatter and brief generation derive from profile metadata
  while prose still derives from Role definitions.
- Assert adding a Role without a complete profile fails tests.

### Checkout and launch validation

- Coordinator resolves to a session Tree contract.
- Implementer resolves to a new write Tree and draft-PR handshake.
- Shepherd resolves to an existing-PR write attachment and resumable identity,
  never a new draft-PR handshake.
- Reviewer resolves to a shared read-only Tree and captured review result path.
- Explorer resolves to ambient WorkingDir and detached spawn is rejected before
  Tree creation.
- Unknown public role input fails before provisioning or backend launch.
- Unknown native hook worker input does not become coordinator.

### Work Env value and resolution

- Cover session Tree, new write Tree, existing-PR write Tree, shared read-only
  Tree, ambient WorkingDir, Main checkout, CI Lane job, and fleet-sweep cell.
- Assert Tree provenance and WorkingDir identity compose rather than duplicate one
  another.
- Assert provisioned write contexts select existing pixi-run wrapping.
- Assert coordinator contexts consume existing activation snapshots.
- Assert reviewer and non-pixi contexts represent absent activation honestly and
  retain ambient-tool routing.
- Assert resolution is pure over supplied facts and performs no implicit process,
  filesystem mutation, provisioning, or network work.

### Integration regressions

- Replace literal reviewer routing, prompt/frontmatter role tables, brief-role
  tables, and enforcement role tests with profile-derived behavior.
- Preserve read-only Tree reuse and filesystem guard tests.
- Preserve environment scrub and pixi routing tests without duplicating their
  logic in Work Env.
- Preserve coordinator session launch behavior across Claude and Codex adapters.
- Verify CI planning and fleet-sweep execution consume or emit the same Work Env
  vocabulary without changing their established executors.
- Regenerate role surfaces and assert the generated set is clean.

Acceptance is the full existing lint and test suite plus focused module tests.
Live backend dogfood remains optional evidence, not a required test gate for this
structural refactor.

## Work Stream Hints

- Establish the Role Profile and structured checkout-strategy model first as a
  pure, exhaustively tested registry.
- Migrate prompt, brief, validation, enforcement, and spawn consumers through a
  thin vertical slice before removing duplicated constants.
- Add shepherd existing-PR attachment and converge reviewer result routing as
  explicit lifecycle slices rather than hiding them inside generic write/read
  classification.
- Introduce the Work Env value over existing WorkingDir and pixi values, then
  integrate current boundaries incrementally without replacing their executors.
- Finish with cross-boundary convergence, generated-surface regeneration, and
  durable documentation updates.

## Out Of Scope

- Per-repository execution-model overrides.
- New agent Roles or arbitrary custom role strings.
- A Proposal Work Env or reviewer-authored patch landing.
- Reviewer build/test execution from a read-only Tree.
- New pixi plugins, conda modeling, activation algorithms, or environment UUIDs.
- A persistent fleet runtime.
- Review Lab methodology or product review-strategy changes.
- New Tool, Lane, workflow-block, release, or PR-engine architecture.
- Human merging or any change to the draft-to-ready development-cycle ceiling.

## Further Notes

This Spec supersedes the historical planning artifact but does not delete or
rewrite it. ADR-0046 and ADR-0047 remain authoritative decisions. The historical
document's broader-future list is intentionally not copied: review-round records,
host seams, Exec, Tools, Lanes, and workflow blocks now exist, while Proposal Work
Env and any additional structured PR reason work remain independent follow-ups.

The next planning step is `/to-tickets`, which will choose the epic boundary and
turn this Spec into independently grabbable vertical Work Streams after the human
assigns the epic code.
