<!-- markdownlint-disable MD013 -->

# shipit

shipit standardizes agent-driven development across a personal portfolio of repositories: planning, isolated workspaces, role-scoped agents, checks, review state, and release flow. This glossary keeps only the domain language a shipit contributor needs to speak clearly; implementation detail belongs in code, ADRs, and focused docs.

This file is the single truth holder for that language, across docs, code, and agent interaction: every entry is one short description; Specs and ADRs deep-dive and link back here, never redefine. Terms enter only when shared language is actually needed — adding glossary entries is not a ritual start of new work, and decisions (ADRs) and feature definitions (Specs) are never bolted in.

## Language

### Core Identities

**Repo**:
A GitHub repository in canonical `owner/name` form. The repo, not a local path, is the stable identity used for Tree locations, logs, eval records, and portfolio operations.
_Avoid_: "org/repo" when the owner might be a user; using checkout paths as repo identity.

**Owner**:
The GitHub account that owns a Repo. Its login identifies the owner; whether it is a user or organization is enrichment, not part of Repo identity.
_Avoid_: "org" as the general noun.

**OwnerKind**:
The closed set for whether an Owner is a user or organization. It is enrichment for owner-specific capabilities, not part of Repo identity.
_Avoid_: boolean `is_org` language.

**WorkingDir**:
An existing on-disk checkout of a Repo at a branch and commit. A Tree has a WorkingDir, but a human or CI checkout can also be a WorkingDir without being a Tree.
_Avoid_: using WorkingDir as the repo identity.

**Main checkout**:
A WorkingDir that is not a Tree: the ordinary human or CI checkout of a Repo. It can be read by ambient investigation, but agent Runs should use Trees for isolated work.
_Avoid_: treating the main checkout as a Tree or as a safe shared write workspace.

**Sha**:
A full git commit object id used when shipit needs commit identity, review staleness, or Tree provenance. Prefix matching is an explicit question, not normal equality.
_Avoid_: "commit" when the value only names the commit.

**Portfolio**:
The version-controlled fleet manifest in `.shipit.toml` that lists the repos shipit manages. Status tables and tracking issues are views over the portfolio, not the source of truth.
_Avoid_: reconstructing the fleet from local sibling directories or memory.

**Shipit pin**:
The one Shipit identity a consumer Repo locks to in `.shipit.toml`: normally a semantic release Version, or exceptionally a full git Revision for unreleased testing. The pin ties the installed tool and managed files to one build.
_Avoid_: storing both Version and Revision, or using a branch as a pin.

**PR**:
A GitHub pull request identified by Repo plus number, with cheap core state such as head SHA, base branch, draft status, and merge state. Richer readiness or review views compose a PR instead of replacing it.
_Avoid_: bare PR numbers in service signatures.

**PrId**:
The identity half of a PR: Repo plus number, with nothing fetched. Verbs mint it at the boundary so services do not re-resolve ambient repo context.
_Avoid_: bare PR numbers traveling alone.

### PR Flow

**PR state engine**:
The reviewer-agnostic logic that reads a PR snapshot and reports the single next action. It reports readiness; callers perform mutations such as requesting reviewers or flipping draft to Ready.
_Avoid_: "PR bot", "review automation".

**Next action**:
The one instruction the PR state engine emits for the current PR state, such as request review, wait, address threads, or flip to Ready.

**Required reviewer**:
A reviewer whose review funnel must settle before the PR can be Ready. The required set is policy from configuration, not an approving-review requirement.
_Avoid_: "approver".

**Best-effort reviewer**:
A reviewer whose absence, timeout, failure, or in-flight state never holds Ready. Its signal is still surfaced, but it cannot block the PR lifecycle.

**Reviewer adapter**:
The boundary that knows how one reviewer is requested and how its review signal is read. Adding a reviewer means adding an adapter, not changing the PR state engine.

**App reviewer**:
A reviewer represented by GitHub's native review-request and review signals, such as Copilot or CodeRabbit. Contrast Local-agent reviewer.

**Local-agent reviewer**:
A shipit-run reviewer that reviews a PR locally and posts as a GitHub App bot. Because GitHub cannot natively request that bot as a reviewer, shipit tracks its funnel with a Check Run.

**Roster**:
The resolved reviewer configuration for a PR flow: required/best-effort status, rerun behavior, wait windows, and reviewer options. It is read at the boundary and passed as one value.
_Avoid_: separate ad hoc reviewer setting lookups.

**rerun**:
A per-reviewer Roster setting for whether a push makes an earlier review stale. `rerun=true` requires review on the current head; `rerun=false` lets an earlier review count.

**Sole requester**:
The rule that shipit alone requests required reviewers. GitHub-side auto-requesting for required reviewers is excluded because it creates review rounds the engine cannot count.

**Review funnel**:
The lifecycle of one reviewer's review on a PR: requested, in-flight, posted, failed, empty, or timed out. The engine reads this uniformly for App reviewers and Local-agent reviewers.

**Funnel check run**:
The GitHub Check Run shipit uses as the request/in-flight/terminal signal for a Local-agent reviewer. It is the durable review-funnel store for that reviewer.

**Detached review**:
The execution shape for a Local-agent reviewer: the request opens the funnel signal and spawns the reviewer run, then returns while the child later posts and closes the signal.
_Avoid_: implying a queue or daemon exists.

**Reconcile**:
The idempotent re-request behavior for Local-agent reviewers. If an in-flight funnel check already exists for the current head, shipit reports that state instead of starting a duplicate review.

**Review round**:
One iteration of the review loop, keyed by PR head SHA. A round groups reviewer findings for that head; it is not the same thing as a single GitHub review object.

**Breaker**:
The stopping rule that ends repeated review rounds, either at the configured round cap or when no major-or-worse finding remains. It prevents endless review loops without hiding unresolved threads.

**Finding**:
A reviewer-reported, classified issue on a PR. A finding is the domain claim; a GitHub comment is only one carrier for it.
_Avoid_: "issue" for findings, because GitHub issues already use that word.

**Severity**:
The shared finding ladder: `critical`, `major`, `minor`, `nit`. Major-or-worse findings can mint more review rounds; minor and nit findings still need resolution but do not reopen the loop.
_Avoid_: ERROR/WARNING/INFO, "priority".

**Severity override**:
A deliberate correction to a Finding's emitted Severity. It is an exception path for wrong classifications, not a normal review-loop stage.

**Nitpick**:
The `nit` severity tier: wording, naming, or style with no correctness, behavior, or security impact.

**Dimension pass**:
One scoped finder inside a Local-agent reviewer, such as correctness, security, or test quality. Dimension passes search; they do not define severity.
_Avoid_: "sub-reviewer".

**Calibrator**:
The judge that deduplicates candidate findings, verifies evidence, normalizes severity, and decides what the reviewer posts. It judges findings; it does not originate them.

**Reviewed**:
All required reviewers have settled and every thread from posted reviews is resolved. Failed, empty, or timed-out required reviewers settle as degraded rather than silently passing.

**Mergeable**:
GitHub's authoritative signal says the PR can merge: no conflicts, not behind base, and no unsatisfied branch-protection rule.

**Ready**:
The PR is done iterating: Reviewed, checks green, and Mergeable. Flipping draft to Ready is the handoff for human validation and merge.

**Wait window**:
How long a requested but silent reviewer holds the PR before timing out and settling as degraded. The engine treats the current time as input rather than keeping its own clock.

**Holds / Settled**:
A reviewer or readiness pillar holds a PR when it prevents Ready; it is settled once it no longer does. Use this language for PR readiness instead of "gating".

### Planning

**Spec**:
The authoritative feature definition in `docs/spec/`: what is being built and why. A merged docs PR locks it before execution work is decomposed. Legacy PRDs live in `docs/legacy-prd/`.
_Avoid_: treating an epic issue as the spec.

**Epic issue**:
A GitHub tracker for how a Spec lands: work streams, progress, and links to the Spec and ADRs. It tracks execution, not the full feature definition.

**Work Stream**:
An independently grabbable slice of an epic that ships through the normal draft PR lifecycle. Work streams target the epic branch, not `main`.

**Opportunity**:
An evidenced improvement noticed during authorized work but kept out of that work's scope. It can later be triaged into an issue; it is not permission to side-quest.

**Opportunity store**:
The GitHub-backed backlog for Opportunities before they become execution issues. It keeps raw improvement observations out of product repos and product issue trackers.

### Checks & Enforcement

**Check**:
A verifiable verdict over a tree, such as lint, tests, build, actionlint, or a lane result. A check does not inherently block anything; policy gives it force for a specific operation.
_Avoid_: "gate" as a property of a check.

**Operation**:
An attempted transition that can be blocked, such as commit, push, open PR, flip to Ready, merge, release, edit, run, or spawn.

**Policy**:
The operation-specific binding that says which checks or context predicates are blocking and which are advisory. Enforcement is contextual, not global.
_Avoid_: "the gate".

**Context predicate**:
A policy input that describes the actor or context of an operation, such as role or session kind, rather than a tree verdict.

**Blocking / Advisory**:
The two roles a policy input can play for one operation. Blocking stops the operation; advisory records and surfaces the result without stopping it.

**Commit/push checks**:
The checks policy marks blocking at commit and push, currently lint plus the fast test set and any local required lanes. They are one operation's blocking set, not a standalone gate.

### Agent Harness

**Role**:
The function an agent plays in the dev cycle, such as coordinator, implementer, shepherd, explorer, or reviewer. Role is also the context predicate enforcement uses.
_Avoid_: "agent type" as the domain noun.

**Role Profile**:
The fixed shipit-owned structural profile for a Role: checkout strategy, enforcement posture, generated/brief surfaces, supported launch contexts, and result channel. Role definitions remain the source of behavioral prose.
_Avoid_: "Role Policy"; policy is operation-specific.

**Checkout strategy**:
The structured checkout allocation and attachment shape selected by a Role Profile: session Tree, new write Tree, existing-PR write Tree, read-only Tree, or ambient WorkingDir. It is the implementation of the older Tree Profile summary, not a consumer-configurable value.
_Avoid_: using one flat "write" token when implementer and shepherd attachment differ.

**Launch context**:
The supported way a Role can start, such as host-session, detached spawn, or native subagent. Unsupported Role/launch pairs fail before Tree provisioning or backend launch.

**Result channel**:
Where a Run's result is expected to appear: the orchestration session, a new draft PR, commits and resolved threads on an existing PR, a posted review, or a coordinator report.

**Enforcement posture**:
A Role Profile's capability-shaped requirements for checkout mutation, command execution, network access, GitHub mutation, scratch writes, and code authorship. It is a policy input and defense-in-depth description, not a sandbox promise.
_Avoid_: a single "can mutate" boolean.

**Role definition**:
The Lex source that defines role behavior once and generates the role-specific prompts and reference docs.

**Role prompt**:
The generated, role-scoped instructions handed to one role. Agents receive only their own slice to reduce role drift.

**Run**:
One role's bounded execution with its transcript and metadata. A single task can contain several Runs, such as an implementer Run plus later shepherd Runs.
_Avoid_: "session" for the eval unit.

**Reviewer Run**:
A branch-pinned, read-only Run that reviews a PR and posts findings without mutating the reviewed checkout.

**Eval record**:
The harness-owned JSONL record summarizing one Run's observable behavior. It is local telemetry for comparing harness changes, not product repo content.

**Variant**:
The attribution on eval records for which harness inputs produced a Run, usually a content hash of the generated role prompt plus optional A/B label.
_Avoid_: conflating this with a test variant.

**Review-round record**:
The harness record of what one reviewer concluded for one Review round. Eval records describe run behavior; review-round records describe review output.

**Ground-truth fixture**:
The versioned, in-repo corpus of pinned historical PR ranges and their Ground-truth labels that review experiments are scored against. Scored results name the fixture version; numbers from different versions are not comparable.
_Avoid_: "test set", "benchmark" as the noun.

**Ground-truth label**:
One evidenced verdict in the Ground-truth fixture: a finding (its location, claim, and severity) judged real or not-real, carrying provenance — a fix commit, a confirmed thread, or a banked Adjudication. Labels sharing an explicit defect equivalence-family id are anchors of one defect and count once for recall.
_Avoid_: labels admitted on opinion without provenance; inferring label equivalence from cross-file similarity instead of the declared family.

**Adjudication**:
The one-time human-confirmed verdict on an emitted finding the fixture does not know, banked into the Ground-truth fixture as a new label or a phrasing alias. It grows the fixture as a side effect of running Cells.

**Cell**:
One declared review experiment: a versioned in-repo file naming its baseline cell and the single axis it changes, plus fixture subset, pipeline shape, Invocation, replicates, and Sweeps. Banked cell results are reused, never re-run.
_Avoid_: "experiment run" for the declaration.

**Sweep**:
One full review pass over a range inside a Cell. Multiple sweeps measure convergence over the same range; a Sweep is not a Review round, which is keyed to a live PR head and fix pushes.

**Break-glass**:
A visible, logged exception that allows an operation that policy would otherwise block. Its use is meant to be rare and measurable.
_Avoid_: silent overrides.

**Backend**:
The agent harness or CLI used to launch a Run, such as Claude, Codex, or Antigravity. It is orthogonal to Role and Model.

**Model**:
The LLM identity a Backend drives for a Run. Model choice is separate from backend launch mechanics.

**Provider**:
The vendor of a Model. It matters for model capability, auth, and billing, not repo or run identity.

**ReasoningLevel**:
The normalized thinking-effort setting chosen for an invocation. Each Backend maps it to its own native control.

**Invocation**:
The configured launch of a Run: Backend, Model, ReasoningLevel, and permission mode. It is a comparison axis for eval reporting.

### Execution & Logging

**Exec**:
One external binary invocation made by shipit, with argv in and a normalized result or error out. A Run may be launched by an Exec, but the Run is the transcript-bounded agent work.
_Avoid_: "Run" for subprocess calls.

**EnvIdentity**:
Pixi-owned environment metadata read from the materialized prefix, such as environment name and lock-file hash. It is not a pixi Run id, not a UUID, and not a full environment snapshot.
_Avoid_: inventing pixi invocation identities.

**Tool adapter**:
The boundary that knows one external tool's command shape, output parsing, and semantic errors. Callers should receive shipit domain values instead of parsing tool output themselves.

**File log**:
The durable per-repo JSONL diagnosis record that every shipit process writes. Human console output is a surface; the file log is the record.

**Domain keys**:
The closed correlation vocabulary on log records, such as session, tree, pr, run, repo, epic, ws, agent, and role. Keys are present only when bound.

**Dev-cycle event**:
A registered milestone recorded as a normal file-log record with an `event` field. It is how shipit reconstructs session, PR, and epic flow.

**Redactor**:
The central log processor that masks known secret values and credential patterns before any sink renders them.

**Lifecycle narration**:
The convention that important subsystem milestones are logged with domain phrasing, correlation keys, and useful levels, not only printed to the user.

**Work Env resolution record**:
The flat structured-log projection for where work ran and how execution was routed. Stable fields include `work_env_boundary`, `working_dir`, `working_dir_repo`, `working_dir_branch`, `working_dir_commit`, `checkout_strategy`, `routing`, `role`, `lane`, `tree_branch`, `tree_base`, `pixi_activation`, `pixi_environment_name`, and `pixi_environment_lock_hash`, plus boundary fields such as `ci_event`, `runner`, `required`, `fleet_repo`, and `tool`.
_Avoid_: full environment snapshots, secret values, or fabricated `pixi_run_id` fields.

### Trees

**Tree**:
A shipit-provisioned, isolated clone where a Run works. It is a real clone, not a Git worktree, and it is the unit `shipit spawn subagent` provisions.
_Avoid_: "worktree", "workspace".

**Session Tree**:
The coordinator's own Tree, minted at launch and then switched to the branch the session discovers it needs. Its path is session-shaped; its branch carries the work identity.

**Reclaim**:
Removing a Tree that is no longer in use. A Tree is kept if it is dirty, has unpushed commits, or is under the Idle threshold; otherwise it is reclaimed. One rule for every Tree kind (ADR-0072).
_Avoid_: "Sweep" (that is a review-lab term for a review pass over a range); "stale" as a bucket — reclaim is keep-or-remove, with no third state.

**Idle**:
How long since anything in a Tree was touched, measured as now minus the newest file mtime, over a walk that prunes `.git` and build/env dirs. Idle is the Tree's activity signal and the only clock reclaim reads.
_Avoid_: root-directory mtime (it does not move when an agent edits under a subdirectory, and lags real activity by hours); the creation timestamp in the Tree name (creation-age is not activity-age); liveness probes (activity is measured, never inferred).

**Session store**:
The per-repo directory holding a repo's session transcripts and memory, shared by every Tree of that repo and outliving all of them. Keyed on the origin remote, and linked into place when a Tree is created (ADR-0073).
_Avoid_: treating memory as per-session scratch that must be swept before a session ends; storing it inside a Tree, where reclaim would destroy it.

**Read-only Tree**:
A Tree mode for branch-pinned reviewers: per-Run, checked out read-only, and not provisioned with build tooling. Read-only is a mode, not a sharing arrangement — reviewers get their own Tree like every other Run.
_Avoid_: "explorer Tree"; explorers are ambient. "Shared read-only Tree"; sharing per repo and branch was dropped with the flat layout (ADR-0074).

**Review proposal**:
A candidate code change a Reviewer Run may produce as supporting output. It is never applied by the reviewer; a shepherd decides whether to use it.

**Proposal Work Env**:
An auxiliary Work Env a Reviewer Run may use to prepare or validate a Review proposal. It does not change the reviewed source of truth or grant landing authority.

**shipit-owned spawning**:
The rule that real Runs are launched through shipit's spawn verb, which provisions the right Tree and starts the backend in it. Agents do not self-provision Trees.

**Tree ownership**:
The role-keyed rule for who gets a Tree and who provisions it. Coordinators provision Trees for spawned Runs; spawned Runs start inside the Tree they receive and do not self-provision.

**Tree Profile**:
The user-facing summary of a Role's checkout family: session, write, read-only, or ambient. The implementation is the structured Checkout strategy, which preserves allocation, attachment, lifetime, and mutation as separate concerns.

**Work Env**:
The resolved execution context shipit uses for work: a WorkingDir, optional Tree provenance, Checkout strategy, optional pixi Activation and EnvIdentity, and an execution-routing decision. Work Env describes where and with which activation work runs; Exec, pixi, Tool adapters, Tree provisioning, CI, and fleet code remain the executors and owners of their existing mechanisms.
_Avoid_: "workspace", "working tree", "sandbox".

### Substrate

**Substrate**:
The isolated environment in which a Repo's tasks execute: a container by default, with the code mounted from the host. The host edits; the Substrate runs. CI legs and local dev loops use the same Substrate model.
_Avoid_: assuming host-installed toolchains; treating the host machine as the execution environment.

**Mac exception**:
The scoped carve-out from the Substrate for work that physically requires macOS: Darwin build/bundle release legs, CI signing/notarization, and locally launching a GUI app. Deliberately licensed to use cruder pinning than the Substrate because its blast radius is a few repos' GUI legs and inner dev loop.
_Avoid_: "exception lane" (Lane is a CI verification term); admitting a leg without a physical macOS requirement; accumulating general machinery here.

### Standardization

**Component**:
A reusable unit of repo composition: a toolchain, a dir layout at a declared mount point, and the Tool implementations it brings. A repo is a composition of Components under one Tool contract per Tool.
_Avoid_: treating the repo as the unit of tooling; inventing a per-repo variant where a Component fits; "component" for shipit's own internal units (those are subsystems — build, changelog, distribution).

**Subsystem**:
A standalone unit of shipit's own architecture — build, changelog, distribution, the orchestrator — coupled to its peers by process contracts and shared data types, never code. Release orchestrates subsystems; it does not do their work.
_Avoid_: "component" for these (Component is consumer-repo vocabulary).

**Runtime**:
Where an executing Artifact runs: native-cli, browser, electron, tauri, an editor host, service, or static-web. Runtime is an Artifact attribute — never a Component property — and it selects the e2e harness. Distribution is orthogonal: the same runtime can ship through many endpoints.
_Avoid_: "registry" as a runtime (publishing is a Distribution endpoint); coupling distribution to runtime.

**Tool contract**:
The fleet-wide contract for one Tool (the shipit verbs: lint, test, build, release): shared setup, invocation, and machine-readable result shape, with per-Component implementations. The verb itself remains the Tool (ADR-0039); a _task_ is a repo-level entry point that invokes a Tool, never a name for the verb. Human-facing output formats are presentation overrides, never a second machine contract.
_Avoid_: "task contract"; verb names as loose convention; per-repo result formats.

**Master task**:
The repo-level entry point that composes Component implementations of a Tool into the repo outcome. Single-component repos pass through to their one Component.

**Profile**:
The prescribed shape of a repo of its kind: a composition of Components, Menu selections, and registered contributions. Adoption state is a computable diff from Profile, not a judgment call.
_Avoid_: conflating with Creation profile (the repo-birth machinery; folding the two is a documented follow-up, not an assumption).

**Owned surface**:
The operations shipit owns fleet-wide — provision, lint, build, release, CI plumbing. Strictly enforced and identical across repos; no local variation.
_Avoid_: advisory or best-effort enforcement language.

**Extension point**:
A shipit-defined interface at a declared seam where a project contributes an implementation, from a shell script to a registered plugin — always invoked inside shipit's contract, never a bypass. A contribution that grows past a threshold graduates into a Menu item or is removed.
_Avoid_: "workaround", "escape hatch" as free-form bypasses; anything neither Owned surface nor a registered contribution is a violation.

**Menu**:
The registry of sanctioned options a repo selects from: CI runners, image layers, e2e harnesses. Selecting from the Menu is not variation; going off-menu is.

**Canary instance**:
A generated consumer repo materializing one Profile — created from its creation profile, running the full shipit-managed surface in CI. It exists to fail a bad shipit release before the fleet sees it; an instance that cannot fail does not count.
_Avoid_: hand-maintained canaries; modeling fleet entropy instead of Profiles.

### Build & Release

**Creation profile**:
A shipit-owned creation-time recipe selected through `repo new --stack`, combining initial project files and declarations for one ecosystem. It is input to creation only; the completed Repo persists Toolchains and Artifacts, not a profile or project Kind.
_Avoid_: "Toolchain" for the source-layout recipe; persisting "stack" as a Repo type or dispatch label.

**Scaffold producer**:
Whatever builds a Creation profile's application tree. A profile has a default producer that renders shipit's own templates, and may accept an alternate one — `repo new --standout-wizard` runs the Standout wizard — whose output is imported back into ordinary owned files. Choosing a producer stays inside one profile: it adds no `--stack` value and opens no registry.
_Avoid_: "template"/"plugin" for a producer; treating an alternate producer as a new stack.

**Toolchain**:
The build, test, and provisioning ecosystem a Component's kind binds, such as Rust, npm, MkDocs, or Go. The kind names a Component's shape; the toolchain is the ecosystem it brings.
_Avoid_: Toolchain as the unit of composition (that is the Component); "stack" or "project type" as a dispatch label.

**Path→toolchain map**:
The current `.shipit.toml` realization mapping build-bearing paths to toolchains. It promotes to `[components]` declarations (the components Spec); new declarations go there.
_Avoid_: extending this map with new concepts.

**Tool**:
A uniform shipit verb — `shipit lint`, `shipit test`, `shipit build` — dispatched across a Repo's Components.
_Avoid_: "task" for the verb.

**e2e**:
The artifact-consuming Tool: it runs a harness against a built Artifact instead of testing the source tree directly. The harness follows from the Artifact's runtime; its lifecycle is synchronous or service (started once per suite).
_Avoid_: using e2e as the name for every environment-heavy integration test; designing a harness per repo.

**Leg**:
One Tool applied to one Component, such as `test rust` or `build npm` — the unit for selection and passthrough arguments. In release orchestration, a release leg is one platform's arch-bound build and bundle in the matrix.
_Avoid_: "target".

**Artifact**:
A named, distributable build product: bound to one Component, optionally composed by a Bundle step, declaring a runtime when it executes (libraries have none and are verified by unit tests alone).
_Avoid_: "build output".

**Bundle**:
The optional composition step that turns Component outputs into one Artifact, such as a Tauri or Electron bundle. It is also the corresponding release-pipeline stage. Bundlers package declared inputs; they do not build.
_Avoid_: "package" for this stage.

**Distribution endpoint**:
A place an Artifact is published, such as crates.io, npm, a GitHub release, a marketplace, or an app store.
_Avoid_: "channel".

**Endpoint adapter**:
The boundary that knows how to publish to one Distribution endpoint. Adding an endpoint means adding an adapter, not changing release orchestration.

**Artifact channel**:
The durable, per-producer store of published Artifacts that downstream repos consume in artifact-pinned mode. Its defining invariants, not its transport, are the concept: the location is derived from the producer Repo (never typed by hand), the version is stated in exactly one consumer-owned place, and integrity is verified at fetch time so a wrong name or version fails locally. Permanent and release-scoped, unlike an ephemeral CI job artifact that only chains one workflow run's jobs.
_Avoid_: defining it by its current realization (conda channels; the target realization is the producer's GH Release assets under standardized naming); "cache" or "sccache bucket"; "CI artifact".

**Lane**:
A declared CI verification unit with its run target, artifact consumption, required/local status, trigger, runner, and scope. A lane may map to a GitHub check, but the lane is the declaration.
_Avoid_: "suite", "job".

**Scope**:
The breadth of a lane run: thin for a path-diff-minimal run, full for all relevant coverage. Nightly, dispatch, and non-PR runs use full scope.

**Release**:
A repo-level versioned event that publishes the repo's Artifact set to its Distribution endpoints. Client artifacts are released rather than deployed.
_Avoid_: "deploy".

**Cascade** (retired):
The old cross-repo auto-bump flow removed by ADR-0077. Appears only in historical or superseded docs; pin bumps are a generic dependency bot's job now.
_Avoid_: building or referencing it as live machinery.

**Dependency mode**:
How a downstream consumes an upstream: source-pinned rebuilds from a ref or version, while artifact-pinned fetches a released Artifact by version.
