<!-- markdownlint-disable MD013 -->

# shipit

shipit standardizes agent-driven development across a personal portfolio of repositories: planning, isolated workspaces, role-scoped agents, checks, review state, and release flow. This glossary keeps only the domain language a shipit contributor needs to speak clearly; implementation detail belongs in code, ADRs, and focused docs.

## Language

### Core identities

**Repo**:
A GitHub repository in canonical `owner/name` form. The repo, not a local path, is the stable identity used for Tree locations, logs, eval records, and portfolio operations.
_Avoid_: "org/repo" when the owner might be a user; using checkout paths as repo identity.

**Owner**:
The GitHub account that owns a Repo. Its login identifies the owner; whether it is a user or organization is enrichment, not part of Repo identity.
_Avoid_: "org" as the general noun.

**WorkingDir**:
An existing on-disk checkout of a Repo at a branch and commit. A Tree has a WorkingDir, but a human or CI checkout can also be a WorkingDir without being a Tree.
_Avoid_: using WorkingDir as the repo identity.

**Sha**:
A full git commit object id used when shipit needs commit identity, review staleness, or Tree provenance. Prefix matching is an explicit question, not normal equality.
_Avoid_: "commit" when the value only names the commit.

**Portfolio**:
The version-controlled fleet manifest in `.shipit.toml` that lists the repos shipit manages. Status tables and tracking issues are views over the portfolio, not the source of truth.
_Avoid_: reconstructing the fleet from local sibling directories or memory.

**Shipit pin**:
The shipit commit a consumer repo locks to, recorded in that repo's `.shipit.toml`. The pin ties the installed tool and managed files to one shipit revision.
_Avoid_: package version, branch name, or "shipit version" for this noun.

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

**PRD**:
The authoritative feature definition in `docs/prd/`: what is being built and why. A merged docs PR locks it before execution work is decomposed.
_Avoid_: treating an epic issue as the spec.

**Epic issue**:
A GitHub tracker for how a PRD lands: work streams, progress, and links to the PRD and ADRs. It tracks execution, not the full feature definition.

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
The declared execution shape of a Role: Tree profile, mutation rights, brief surface, generated prompt surface, and enforcement posture.
_Avoid_: "Role Policy"; policy is operation-specific.

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

### Trees

**Tree**:
A shipit-provisioned, isolated clone where a Run works. It is a real clone, not a Git worktree, and it is the unit `shipit spawn subagent` provisions.
_Avoid_: "worktree", "workspace".

**Session Tree**:
The coordinator's own Tree, minted at launch and then switched to the branch the session discovers it needs. Its path is session-shaped; its branch carries the work identity.

**Read-only Tree**:
A Tree mode for branch-pinned reviewers: shared per repo and branch, checked out read-only, and not provisioned with build tooling.
_Avoid_: "explorer Tree"; explorers are ambient.

**Review proposal**:
A candidate code change a Reviewer Run may produce as supporting output. It is never applied by the reviewer; a shepherd decides whether to use it.

**Proposal Work Env**:
An auxiliary Work Env a Reviewer Run may use to prepare or validate a Review proposal. It does not change the reviewed source of truth or grant landing authority.

**shipit-owned spawning**:
The rule that real Runs are launched through shipit's spawn verb, which provisions the right Tree and starts the backend in it. Agents do not self-provision Trees.

**Tree ownership**:
The role-keyed rule for who gets a Tree and who provisions it. Coordinators provision Trees for spawned Runs; spawned Runs start inside the Tree they receive and do not self-provision.

**Tree Profile**:
The declared checkout shape for a Role: session, write, read-only, or ambient. A Role Profile selects a Tree Profile.

**Work Env**:
The execution context shipit uses for work: a checkout plus the tools and paths activated for that checkout. A Work Env may be Tree-backed or a direct checkout, but it is not a security sandbox.
_Avoid_: "workspace", "working tree", "sandbox".

### Build & Release

**Toolchain**:
The build, test, and provisioning ecosystem for a path in a repo, such as Rust, npm, MkDocs, Go, or WASM. Shipit dispatches work by toolchain.
_Avoid_: "kind", "stack", or "project type" as a dispatch label.

**Path→toolchain map**:
The `.shipit.toml` declaration mapping build-bearing paths to their toolchains. Shipit walks this map for provisioning, build, test, and lint.

**Tool**:
A uniform shipit verb, such as `shipit lint`, `shipit test`, or `shipit build`, that walks the path→toolchain map and dispatches each leg.
_Avoid_: "task" for the verb.

**e2e**:
The artifact-consuming Tool: it runs a declared harness against a built Artifact instead of testing the source tree directly. A repo with no e2e declaration has no e2e lane.
_Avoid_: using e2e as the name for every environment-heavy integration test.

**Leg**:
One Tool applied to one path→toolchain entry, such as `test rust` or `build npm`. A leg is the unit for selection and passthrough arguments.
_Avoid_: "target".

**Artifact**:
A named, distributable build product. An Artifact may come from one toolchain, several toolchains, and an optional Bundle step.
_Avoid_: "build output".

**Bundle**:
The optional composition step that turns toolchain outputs into one Artifact, such as a Tauri or Electron bundle. It is also the corresponding release-pipeline stage.
_Avoid_: "package" for this stage.

**Distribution endpoint**:
A place an Artifact is published, such as crates.io, npm, a GitHub release, a marketplace, or an app store.
_Avoid_: "channel".

**Endpoint adapter**:
The boundary that knows how to publish to one Distribution endpoint. Adding an endpoint means adding an adapter, not changing release orchestration.

**Content-key**:
The identity shipit uses for build-once reuse of an Artifact, derived from the inputs that determine that Artifact. It is more than a cache bucket.
_Avoid_: "cache key".

**Lane**:
A declared CI verification unit with its run target, artifact consumption, required/local status, trigger, runner, and scope. A lane may map to a GitHub check, but the lane is the declaration.
_Avoid_: "suite", "job".

**Scope**:
The breadth of a lane run: thin for a path-diff-minimal run, full for all relevant coverage. Nightly, dispatch, and non-PR runs use full scope.

**Release**:
A repo-level versioned event that publishes the repo's Artifact set to its Distribution endpoints. Client artifacts are released rather than deployed.
_Avoid_: "deploy".

**Cascade**:
The cross-repo release flow where an upstream release opens version-bump PRs for declared downstream repos.
_Avoid_: "trigger chain", "webhook chain".

**Dependency mode**:
How a downstream consumes an upstream: source-pinned rebuilds from a ref or version, while artifact-pinned fetches a released Artifact by version.
