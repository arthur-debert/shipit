# shipit

shipit standardizes work across a personal portfolio of repos: provisioning,
the dev workflow + skills, a multi-language lint check, GitHub repo setup, pixi
tooling, the PR-review state machine, and the build/release workflows. This
glossary fixes the language of that domain — especially the PR-flow vocabulary
inherited from release-core and the build/release vocabulary.

## Language

### Core identities

**Repo**:
A GitHub repository as shipit's identity value object — `(owner, name)`, derived
*locally* from the origin remote (`git remote get-url origin`), never from a live
API call, and **lowercase-canonical** (GitHub owners/names are case-insensitive),
so case-varying origins or API slugs share one identity instead of forking one
repo's Tree paths and log dirs. An `owner/name` string becomes a Repo only
through the one canonical parser (`identity.repo_from_slug`). It is the stable
key every Repo-scoped join uses — notably the **eval record** store. The
**path→toolchain map** is the *same* repo's content; identity and content are two
facets of one noun.
*Avoid*: "org/repo" as the identity pair (an owner may be a user, not an org);
keying a Repo by filesystem path (that scatters one repo across every clone — the
bug WS-Repo removed); hand-splitting a slug at a call site (each split is a place
for the case-divergence bug to come back).

**Owner**:
The account that owns a **Repo** — `(login, kind)`. `login` is always known
offline; `kind` is an OPTIONAL, lazily-resolved enrichment and is **not** part of
Repo identity/equality (so the store key is stable whether or not kind is known).
*Avoid*: "org" as a synonym for owner (an organization is one **OwnerKind**, not
the whole concept).

**OwnerKind**:
The closed registry of what an **Owner** can be — `user | organization` (mirrors
**Role** / **Toolchain**: adding one is an entry, nothing downstream changes).
Names the capabilities that exist only on organizations (org rulesets, Actions org
policy) so future org-only features have a place to hang. Resolved via API on
demand, never required to identify a **Repo**.
*Avoid*: a boolean `is_org` (a closed set reads clearer and extends cleanly).

**WorkingDir**:
An on-disk checkout embodying a **Repo** at a revision — `(path, repo,
revision{branch, commit})`. The single resolver for "what repo + revision is
checked out at this path," replacing the scattered `git rev-parse
--show-toplevel` re-derivations. A **Tree** *has* a WorkingDir (values compose);
the **main checkout** is a WorkingDir that is not a **Tree**.
*Avoid*: making Tree a *subclass* of WorkingDir (value objects compose, they do
not inherit); treating a WorkingDir as an identity (its **Repo** is the identity —
a WorkingDir is a *location*, so two clones of one repo are two WorkingDirs but
one Repo).

**Sha**:
A commit identity as a value object — a validated FULL git object id (40 or 64
hex chars), lowercase-normalized at construction, never silently compared
prefix-against-full: equality is full-vs-full and Sha-vs-Sha only (comparing
against a raw string *raises* rather than quietly answering false), and prefix
matching is the explicit `matches_prefix` ask. The identity that review
staleness ("is this review on the current head?") and Tree provenance key on —
`PR.head_sha` and a review's `commit_id` carry it.
*Avoid*: raw string SHAs compared with `==` (short-vs-full or case mismatch
silently flips staleness); "commit" as the noun (a Sha *names* a commit; the
commit is the git object).

**PR**:
A GitHub pull request as a value object — identity `(repo, number)` plus cheap
**core** state (`head_sha` (carried as a **Sha**, minted at the one wire read),
`base_ref`, `is_draft`, `merge_state`). The readiness
path and the review path build distinct richer **views** that *compose* a PR
(readiness view: + reviews / threads / funnel / timing; review view: + diff /
changed_files / workdir), never parallel half-overlapping snapshots.
*Avoid*: two competing PR snapshot types (`PullContext` / `PRContext`); a field on
the core that only one path populates (e.g. a defaulted `is_draft` — it belongs on
the view that fetched it); fetching `head_sha` more than one way.

### PR flow

**PR state engine**:
The reviewer-agnostic core that reads where a PR stands and reports the single
next action. Lives at `src/shipit/prstate/`. A pure function from a snapshot to
a state; it never names a reviewer and never mutates (it *reports* READY; a
caller does the flip).
*Avoid*: "PR bot", "review automation".

**Reviewer adapter**:
The only place that knows one reviewer's mechanics (how to detect its review,
how to request it). Adding a reviewer is adding an adapter to the registry;
nothing downstream changes.

**Required reviewer**:
A reviewer in the **required set** — every one must be **settled** before a PR can be Ready.
The set is policy (config), not code.
*Avoid*: "approver" (this fleet requires 0 approving GitHub reviews).

**Best-effort reviewer**:
A reviewer that never **holds** Ready — an absent or in-progress one never keeps
the PR from Ready. The opposite of a **required reviewer**.

**App reviewer**:
A reviewer addressed through a GitHub `review_requested` edge (Copilot,
CodeRabbit). Contrast **local-agent reviewer**.

**Local-agent reviewer**:
A reviewer whose review is generated locally (`agy-local`, `codex-local`) — an
agent reviews the diff — and posted as a GitHub-App bot identity. GitHub gives it
no native `review_requested` edge (a custom App bot cannot be assigned as a
requested reviewer), so its **review funnel** is tracked by a shipit-authored
signal — the **funnel check run** — rather than read from a native reviewer edge.
Requesting one runs **async**: the request opens that in-flight signal and
detaches the agent run (see **Detached review**), returning immediately — the
outcome is read LATER from the PR, never from the request. Contrast **App
reviewer**.

**rerun**:
A per-reviewer policy flag. `rerun=false` (the default for everyone:
review-once) — a review on any commit counts as done and is never stale after a
push. `rerun=true` (head-strict) — the review must be on the current head, so a
push re-stales it and the reviewer is re-requested.

**Review round**:
One iteration of the review loop, keyed by head SHA — all required reviewers'
findings on the same head fold into one round. Not one review object.

**Review funnel**:
The stages a single reviewer's review passes through on a PR — *requested* →
*in-flight* → *posted*, or a terminal *failed* / *empty* / *timed-out*. The engine
reads the funnel uniformly across reviewer kinds: from native GitHub signals for an
**App reviewer** (its `review_requested` edge, then its review object) and from a
shipit-authored signal — the **funnel check run** — for a **local-agent reviewer**
(which has no native edge).

**Funnel check run**:
The shipit-authored signal that stands in for the `review_requested` edge GitHub
denies a **local-agent reviewer** — a GitHub Check Run authored by the reviewer's
own App. Opened `in_progress` (with an honest `started_at`) when the review is
requested, transitioned to a terminal conclusion (success / failure / timed_out)
when it settles. The funnel's *empty* outcome — a degraded non-delivery, NOT a
clean zero-findings review (which posts as success) — is carried as conclusion
`failure` with an explicit `empty` reason in the run's output, so the distinct
terminal outcome the **Review funnel** lists survives the narrower check-run
conclusion vocabulary (see `_FUNNEL_TERMINAL` in `review/service.py`). The PR +
this check run are the WHOLE store — no daemon, no local
job state — so the engine stays stateless and any reader recovers the funnel
straight from the PR. (ADR-0005.)

**Detached review**:
How a **local-agent reviewer**'s request executes. The request does only the cheap
synchronous work — resolve `(repo, head_sha)`, **reconcile** against any in-flight
run, open the **funnel check run** `in_progress` — then spawns a DETACHED child
process that runs the agent, posts the verdict as the bot, and closes the SAME
check run to its terminal state. The request returns in-flight WITHOUT blocking on
the model run: the parent opens, the child closes, exactly ONE check run. The gap
between the open and the child's close is the *in-flight* window the **wait window**
ages a `started_at` against (a child that vanishes before closing is reaped there).
*Avoid*: "background job", "queue" — there is no daemon or job store, only the
detached process + the check run.

**Reconcile** (idempotent re-request):
Because the **funnel check run** IS the store, a re-request for a **local-agent
reviewer** whose run is still in-flight on the current head reconciles against that
run — reports in-flight, opens no second check run, spawns no second child — rather
than double-posting. Read-then-decide against the check run only; no local/daemon
state to consult.

**Breaker** (stopping rule):
The rule that ends the review loop instead of iterating forever: stop when 6
rounds are reached, or when the latest round is all nitpicks.

**Nitpick**:
A comment about wording, naming, or style with no correctness, behavioral, or
security impact. A round that is all nitpicks trips a **breaker**.

**Reviewed**:
Every required reviewer **settled** — its **review funnel** reached a recorded
outcome (posted / empty / failed / timed-out) — and every thread from a *posted*
review resolved. A failed / empty / timed-out reviewer is settled but
**non-blocking**: it does not hold Ready, though the PR surfaces it as **degraded**
(visible, never silently "fine").

**Mergeable**:
The PR's merge state permits merging — no conflict, not behind its base, no
unsatisfied branch-protection rule. Keyed off GitHub's authoritative merge-state
signal (`mergeStateStatus == CLEAN`), NOT the async-stale `mergeable` boolean,
which reads optimistically before a recompute lands.

**Ready**:
All three pillars satisfied — the generic, obvious work is done:
(1) the code is correct — **Reviewed** (written, reviewed, every thread
addressed); (2) the checks pass — CI green; (3) the PR is **Mergeable**. This is
exactly the order the engine checks the pillars. Flipping draft→Ready is the one signal that says
"done iterating — a human can validate and merge".

**Wait window**:
How long a *requested but silent* reviewer **holds** a PR before it **times out**
and **settles** (non-blocking, surfaced as degraded). Aged from the reviewer's own
request timestamp; uniform across reviewer kinds; **20m** default, per-reviewer
override. The engine reads it statelessly — "now" is an input, not a clock it
keeps.

**Holds / Settled**:
A reviewer or pillar **holds** a PR when its condition keeps the PR from Ready; it
is **settled** once that condition is met. The PR is **Ready** when nothing holds
it. Deliberately distinct from a **check**'s **blocking / advisory** role — readiness
(holds / settled / pillars) is about reviewers and merge-state, a separate layer
from whether a check blocks an operation; speak of it as *holds / settled /
pillars*, never "gating".

**Next action**:
The single instruction the **PR state engine** emits for a PR's current state
(request a review, address threads, wait for CI, flip to Ready, …).

### Planning

**PRD**:
The **feature definition** — the authoritative spec, a file in `docs/prd/`. The
*what & why* of a feature, written from the user's perspective (problem, solution,
user stories, decisions). Locked by a merged docs PR; produced by `/shipit-to-prd`.
*Avoid*: treating the **epic issue** as the spec — the PRD is the source of truth.

**Epic issue**:
An **execution tracker** — a GitHub umbrella issue that points to the **PRD** and
the relevant ADRs and carries a PRD summary plus the **Work Stream** topology and
progress (sub-issues). It tracks *how the work lands*, not *what to build*. One
feature may have several epic issues; produced by `/shipit-to-issues`.
*Avoid*: embedding the full PRD in the issue — it links to the PRD, never replaces it.

### Checks & enforcement

There is no "gate". A **check** has no inherent power to stop anything; *blocking* is a
relation between an **operation**, the context it runs in, and a check — decided by a
**policy**, never a property of the check itself. The recurring confusion ("what does a
test *gate*?") dissolves once these three are kept separate: a test is a check; what it
blocks, if anything, depends on which operation is asking and under what policy.

**Check**:
A verifiable yes/no verdict over the tree — `lint`, each `test` variant, `build`-succeeds,
`actionlint`, a **lane**'s result. Intrinsically just a question; on its own it stops
nothing. The very same check can be decisive in one place and informational in another.
*Avoid*: "gate", "gating" — a check carries no blocking power to name.

**Operation**:
An attempted transition someone (human or agent) wants to make. The unit that can
be *blocked*. Two families: **VCS operations** — *commit*, *push*, *open-PR*,
*flip-to-Ready*, *merge*, *release* — and **agent-action operations**, the
agent-harness extension — *edit* (a file write), *run* (a shell command), *spawn*
(a subagent), … Each operation runs the **checks** and **context predicates** its
**policy** binds, evaluated in that operation's context. The model is one and the
same across both families; the agent harness does not fork it.

**Policy** (per operation):
The binding that says, for one **operation** in a given context, whether each relevant
input is **blocking** (its failure stops the operation) or **advisory** (its failure
is recorded and surfaced — cf. **degraded** — but the operation proceeds). The input is
either a **check** (a yes/no verdict over the tree) or a **context predicate** (a fact
about the actor or context the operation runs in — e.g. the acting **role**). Enforcement is
contextual, not global: `lint` + the fast `test` set block at *commit/push*; an expensive
`test-e2e` / GPU lane is advisory there and blocking only at *open-PR* / *merge*; the
*edit* operation is blocking when the actor's **role** is the coordinator. A
**lane**'s `required` / `local` / `trigger` fields ARE its policy across operations —
`required` = blocking at *merge*, `local` = also enforced at *commit/push*, `trigger` =
which operations run it at all.
*Avoid*: one global "the gate" — there are as many enforcement sets as there are operations.

**Context predicate**:
A **policy** input that is a fact about the *actor or context* of an **operation** rather
than a verdict over the tree — the acting **role**, the **session** kind, an env marker.
The agent harness keys its enforcement on these (the coordinator cannot *edit*), reusing
the same blocking/advisory machinery as a **check**. Contrast **check** (tree-verdict).

**Blocking / Advisory**:
The two roles a **policy input** — a **check** or a **context predicate** — can play under a
**policy** for a given **operation**. *Blocking*: its failure (or, for a predicate, its
unmet condition) stops the operation. *Advisory*: its failure is surfaced but does not stop
it. A policy input is never blocking or advisory in the abstract — only for a named operation
in a named context. (So "pre-commit runs `lint` + `test`" means: the *commit* operation's
policy marks those two checks blocking — not that they are gates.)

### Agent harness

**Role**:
The function an acting agent plays in the dev cycle — and the **context predicate**
the agent harness keys enforcement on. A **closed registry** (mirrors **Toolchain** /
**Reviewer adapter**: adding one is an entry, nothing downstream changes):
`coordinator`, `implementer`, `shepherd`, `explorer`. Read from the acting session's
agent identity — *empty ⇒ `coordinator`* (the top-level, human-facing session), a
named subagent ⇒ that role. Two realizations of the same concept: the **coordinator**
is a *session-role* (the top-level session; it has no agent-def file — its prompt
arrives as injected context plus the enforcement **deny** reason), while
`implementer` / `shepherd` / `explorer` are *agent-def roles* (each a generated
prompt file whose body is that role's system prompt). The **policy** reads `role`
uniformly across both.
*Avoid*: "agent type" as the domain noun (that's the raw signal under the term);
"guard" (enforcement is a **policy**, not a thing with inherent power).

**Role definition**:
The single source of a **role**'s behavior — focused **lex** fragments (a shared
dev-cycle *base* plus one *overlay* per role) composed via lex includes. The source
of truth that feeds every other surface (the **role prompts**, the AGENTS.md
reference, the **deny** text); never handed to an agent verbatim. One edit re-flows
all derived surfaces, so the dev cycle is stated once.
*Avoid*: "charter" (dissolved — it was just the coordinator's role prompt).

**Role prompt**:
The generated, **role-scoped reduction** handed to exactly one **role** — `base +
that role's overlay only`, so an agent sees only what applies to it (seeing every
role invites mid-session role-drift). The `coordinator`'s is the broad slice (it must
know the cycle and the roles it delegates to); the others are narrow. Delivered on the
surface that *binds*: a subagent's system prompt (its agent-def body), or — for the
`coordinator` — injected context plus the enforcement **deny** reason. Contrast the
*ambient reference* (AGENTS.md): generated from the same **role definition**, but
read-then-lost, so it is never the surface behavior is relied on to arrive through.

**Run**:
The unit the agent harness **evaluates** — one **role**'s bounded execution together
with its transcript (and `.meta.json`). **Not task-bound**: one piece of work spans
several runs — the `implementer` run that writes the code and each `shepherd` run that
addresses a review round are *distinct* runs, each its own **eval record**. The
`coordinator` run is the top-level session transcript; each subagent run is a separate
`agent-<id>` transcript. Eval fires at a run's *terminal* lifecycle hook (a subagent run
at `SubagentStop`, the coordinator run at `Stop`/`SessionEnd`) and emits one record, tagged by
`role` (from `meta.agentType`). Runs aggregate up by role, by **variant**, and over time
(`shipit eval report`).
*Avoid*: "session" as the eval unit — Claude Code shares one `session_id` across an agent
and its subagents, so the per-agent unit is the *run*, not the session.

**Eval record**:
The structured result of evaluating one **Run** — JSONL, one object per run, named with
OpenTelemetry `gen_ai.*` field vocabulary (a *naming* standard borrowed, never a running
collector). **Objective-first**: its fields are extracted *deterministically by code* from
the on-disk transcript + `.meta.json` — tool-call counts, step count, stuck-loop
fingerprints, `--no-verify` / workaround greps, **break-glass** uses, `model`,
`permissionMode`. A subjective agent-as-judge verdict is **deferred to HAR04** (a same-model
self-judge is non-independent — upward-biased — so it is layered on later, de-biased, never
the primary signal). Written to a **harness-owned local store, never committed** to the
product repo, each record `git.commit`-stamped so it correlates to repo state without
entering the tree. It also carries a **variant** attribution so results separate by which
version of each harness input produced them: a *derived content-hash* of the generated
**role prompt** (and policy) that ran — the **content-key** / pristine-hash idea applied to
prompts, so runs pool across commits when the input is unchanged and separate within one
commit when it differs — plus an optional explicit variant label for deliberate A/B runs. If
shared/structured trend is ever needed the substrate is GitHub-native (an epic issue's run
comments, or Pages) — never self-hosted infra. Aggregated with DuckDB.
*Avoid*: a tracing/observability platform (LangSmith et al.) — wrong *kind* (live tracing,
not transcript rubric/metric extraction) and wrong deployment for a no-infra dev harness.

**Variant**:
The attribution stamped on every **eval record** answering *which version of the harness
produced this run* — a *derived content-hash* of the generated **role prompt** that drove the
run (the **content-key** / pristine-hash idea applied to prompts, via the same
`shipit.config.content_hash` scheme `install` uses), plus an OPTIONAL explicit A/B **label**
(`SHIPIT_EVAL_VARIANT_LABEL`). Identical prompts hash identically, so runs *pool* across
commits when the input is unchanged; a changed prompt hashes differently, so runs *separate*
within one commit. `shipit eval report` groups by variant, which is what makes a prompt A/B
separable by data rather than intuition. *Avoid*: conflating it with a `test` **variant**
(`lint`/`test`/`build` axis) — same word, unrelated axis.

**Break-glass**:
A visible, logged escape hatch that lets an actor perform an **operation** its
**policy** would otherwise **block** — the deliberate exception, never the default.
Instances: `install --push` (bypass the PR loop straight to main); the
**coordinator** editing a code path (the `edit` operation is blocked for the
coordinator on **path→toolchain map** paths — implementation it should delegate —
unless a break-glass marker is present). Each use is recorded, so its frequency is a
signal the harness can measure (an HAR02 metric) and tighten on, rather than a silent
bypass. *Avoid*: "override", "force" as the noun — break-glass is logged and rare.

**Backend**:
The agent harness/CLI that drives a **Run** — a closed registry `claude | codex |
antigravity` (ADR-0020). Owns *how to launch* (argv, auth-env, read-only posture)
and a single **identity** — its canonical name plus every alias (funnel login
`adr-<name>-review[bot]`, check-run `<name>-local`, spawn `--backend` token, CLI
binary, Doppler key prefix, model-alias table) defined **once** and shared with
the **Reviewer adapter**. The review funnel threads the Backend value object
itself, so every derived name comes only off its registry entry — a backend with
no funnel App (`claude`) simply has no funnel identity, and its funnel-only
aliases refuse to mint. Orthogonal to **Model** and to **Role**: one backend
serves implementer / shepherd / reviewer runs and can drive different models.
*Avoid*: conflating a **Backend** with a **Reviewer adapter** (launch axis vs
PR-funnel axis — they share *identity*, not behaviour) or with a **Model** (the
harness is not the LLM); passing a bare agent-name string where the Backend
identity should flow (retyped names are how alias tables drift apart).

**Model**:
The LLM a **Backend** drives — `(id, provider, reasoning_capability)`, identity =
the canonical model id. Decoupled from Backend: a model of one **Provider** may run
under a backend of another. Its reasoning *capability* (which **ReasoningLevel**s
it supports, if any) is intrinsic to it.
*Avoid*: treating the model as a property of the **Backend** — they are orthogonal
axes.

**Provider**:
The vendor of a **Model** — closed registry `anthropic | openai | google | …`. The
hook for auth / billing and cross-backend model use; never part of a **Repo** or
**Run** identity.

**ReasoningLevel**:
The thinking-effort knob chosen for one **Invocation** — closed registry `low |
medium | high`, normalized so eval compares across backends; each **Backend** maps
it to its native control. A *chosen level* (on the invocation), distinct from a
**Model**'s reasoning *capability*.

**Invocation**:
The configured launch of one **Run** — a **Backend** driving a **Model** at a
**ReasoningLevel** (plus `permission_mode`). Threaded spawn → Run → **eval
record** (the *observed* config extracted from the transcript, alongside the
*intended*), and a group-by dimension for `shipit eval report` — the data that
lets the harness compare configurations. Backend×Model validity is a lookup, not a
structural constraint.
*Avoid*: "AgentConfig" (implies the model belongs to the agent — it does not);
conflating it with **Variant** (the prompt/policy content-hash axis, a different
attribution).

### Execution (external commands)

**Exec**:
One execution of an external binary by shipit — argv in, run to completion, a
normalized result or error out, exactly one structured record of what happened.
Every subprocess shipit spawns is an Exec (pixi, git, gh, a **Backend** launch);
an agent **Run** is *started by* an Exec, but the Run is the transcript-bounded
work, not the process call.
*Avoid*: "Run" for a subprocess call (that's the agent-eval unit); "command" (a
CLI verb of shipit's own); "process" (the OS mechanism, not the bounded
call-with-result).

**Tool adapter**:
The only place that knows one external tool's mechanics — how to encode an
**Exec**'s argv, which structured output to harvest (native JSON, porcelain,
converted), and which failures translate to semantic errors. Adding a tool is
adding an adapter; nothing downstream changes (mirrors **Reviewer adapter** /
**Endpoint adapter**). The **Backend** adapter (ADR-0020) is a Tool adapter
specialization that additionally owns launch posture. Any tool argv built
outside its Tool adapter is a defect.
*Avoid*: two half-adapters for one tool (the two-`GhError` disease); "wrapper"
(an adapter is the registry pattern, not ad-hoc convenience).

### Logging (the durable record)

**File log**:
The durable, per-repo, rotating diagnosis record every shipit process writes —
**JSONL**, one flat JSON object per record: `ts` (ISO-8601 UTC), `level`,
`logger`, `msg`, plus **domain keys** and event extras, all flat (ADR-0029,
agents-first). One processor pipeline in `logsetup` (context-merge → enrich →
**redactor**) feeds every sink; only the final renderer differs — the file gets
JSONL, the console/CI stderr surfaces stay human-formatted. `shipit logs` is its
reader: the default view renders records legibly, `--raw` passes stored lines
through for `jq`. The verb reads JSONL ONLY — hard cutover, no format sniffing;
pre-cutover freeform files age out via rotation.
*Avoid*: treating stderr as the record (it is the surface; the file is the
record); nesting or an OTel log model (fields stay flat and top-level).

**Domain keys**:
The CLOSED correlation vocabulary — `session`, `tree`, `pr`, `run`, `repo` —
bound via context (`logcontext`) at the CLI entry and the spawn/detach seams,
carried across process boundaries as `SHIPIT_LOG_CTX_*` env vars and rebound at
the child's logging setup, so a Run's records correlate to their parent.
Present-when-bound: an unbound key is ABSENT from the record, never null; an
unknown key name raises, so a typo cannot mint vocabulary. No synthetic
trace/span ids (ADR-0029).
*Avoid*: ad-hoc extras as correlation keys (extras describe the event; domain
keys join records); binding a synthetic `session` (the key binds only when a
seam knows the real identity).

**Redactor**:
The central masking processor (`shipit.redact`, ADR-0028/0029) in the one log
pipeline, so everything logged on ANY sink is masked before rendering: exact
values of every secret `secretsrc` fetches (registered at fetch time, held for
the process lifetime) plus pattern rules for GitHub token prefixes and PEM
blocks. Mask is `***`. No redaction package is adopted (none credible —
ADR-0029 records the survey).
*Avoid*: per-call-site scrubbing as the safety story (the pipeline seam is the
guarantee; `gh.py`'s argv masking is belt-and-suspenders for non-log channels);
"sanitize"/"filter" (redaction masks values, it does not drop records).

### Trees (where work happens)

**Tree**:
An isolated, fully-independent **clone** of one repo where one **Run** works, living
under a central root outside any repo (`~/workspace/trees/<org>/<repo>/…`). A Tree is
a real clone — its own `.git`, able to sit on `main` — NOT a git worktree (which
shares one object store and forbids the same branch in two places). Two modes (ADR-0018):
a **write Tree** (one per write-Run; `.treeinclude` + pixi + sccache; read-write) and a
**read-only Tree** (clone + checkout only, files read-only, shared per `(repo, branch)`).
The unit `shipit spawn subagent` provisions for a Run (ADR-0017).
*Avoid*: "worktree" for this unit (that names the git feature we deliberately reject —
see ADR-0014); note Claude Code's `WorktreeCreate` hook is the *harness event we adapt*
for both throwaway in-CC helpers and the coordinator's **Session Tree**, NOT our Tree
unit; "workspace" (collides with Cargo/pixi/editor "workspace").

**Session Tree** (a kind of **Tree**):
The **coordinator**'s own isolated workspace — the Tree the top-level session runs in,
minted at **launch** via `claude --worktree <id>` (which fires the `WorktreeCreate`
hook; ADR-0027). **Ephemeral-by-path, work-by-branch**: its directory identity is the
*session* (`…/ephemeral/<id>`, cut from `origin/main`, disposable, **never renamed**),
while the *branch* checked out inside it becomes the real work (`docs/<slug>`,
`EPIC/umbrella`, …) as the session discovers its task. There is **no mid-flight path
move** — the session cwd is immutable after launch, so the coordinator switches branches
*within* the clone instead. It is the one **Tree** `shipit spawn subagent` cannot mint
(it is the session's *own*, and the cwd is fixed before any shipit code runs). Reclaimed
by a liveness-based **gc** rule (no PR to key off), gated by the dirty/unpushed check.
*Avoid*: binding it to an epic/issue at launch (the work is usually unknown then; a full
clone switches branches freely); treating dir↔branch mirroring as an invariant here (it
holds at birth, then the branch moves and the dir stays — by design).

**Read-only Tree**:
A **Tree** mode for a **Reviewer** — clone + `git checkout` only (no `.treeinclude`, no
pixi provisioning), with working files `chmod`'d read-only. **Shared per `(repo, branch)`**:
N reviewers on one PR head share a single cheap clone, safe because none mutate it. The cut
is **branch-pinned-vs-ambient**, not read-vs-write — an ambient explorer still gets no Tree;
a branch-pinned reviewer gets this one (ADR-0018).
*Avoid*: "explorer Tree" (an explorer has no Tree at all); conflating it with a **write Tree**.

**Reviewer Run** (a kind of **Run**):
A branch-pinned, **read-only** Run — a PR reviewer (claude / codex / antigravity) that reads
the diff and code and **posts a review**, never executing or mutating. Spawned like any Run
via `shipit spawn subagent --role reviewer`, it gets a shared **Read-only Tree** and reports
back through the PR (a posted review). Contrast the **implementer** / **shepherd** write-Runs
(write Tree, report via a draft PR) and the **explorer** (ambient, no Tree).

**shipit-owned spawning**:
The model (ADR-0017) where the **coordinator** launches every real **Run** through a CLI —
`shipit spawn subagent --repo R --epic E --ws N --role ROLE [--backend claude|codex|antigravity]`
— passing intent as **arguments**, so shipit never infers it. The verb creates the **Tree**,
launches the backend agent as a **child process rooted in it** (cwd = the Tree → no bash-cwd
footgun), and the Run reports back **through the PR**. **Fail-closed**: a Tree-creation error
fails the spawn loud, never a silent fallback to a native worktree.
*Avoid*: "the worktree hook" as the spawn mechanism for **Runs** — the `WorktreeCreate`
hook mints Trees only in two cases: throwaway in-CC Claude helpers (epic-grouped
`<epic>/agent-<id>`, Claude-only) and the coordinator's own **Session Tree**
(`ephemeral/<id>`, ADR-0027); real *Runs the coordinator launches* go through
`shipit spawn subagent`.
*Epic inference* (#173, resolved): the hook infers the epic from **live git state**, not an
out-of-band set step. The `WorktreeCreate` payload carries the coordinator's `cwd`; the hook
reads that branch (`git -C <cwd> rev-parse --abbrev-ref HEAD`) and takes the prefix before
the first `/` as the epic per ADR-0016 (`TRE04/WS01` → `TRE04` → spawn branch
`TRE04/agent-<id>`). The `SHIPIT_EPIC` env var survives only as an *optional explicit
override* (wins over the inferred branch) for the rare cross-epic spawn. Safe fallback: with
no override and a detached / no-slash / unreadable branch (or a missing `cwd`), the spawn
lands on the epic-less `agent-<id>` holding branch and self-branches from there — it never
crashes the hook. (`harness/worktree_adapter.py` `resolve_epic`/`resolve_branch`;
`verbs/hook/worktreecreate.py` `_resolve_branch`.)

**Tree ownership** (extends the **Role** registry):
Who provisions a **Tree** and who merely works in one — the role-keyed half of the
Tree primitive. The **coordinator** works in its own **Session Tree** (an ephemeral Tree minted at launch
via `--worktree`, ADR-0027 — not the retired manual `shipit tree create --epic` hand-run),
and provisions/assigns Trees for *other* Runs by **spawning** (via `shipit spawn subagent`,
ADR-0017): a ready Tree minted for each Run it launches — a **write Tree** for an **implementer** / **shepherd**, a
shared **Read-only Tree** for a **Reviewer**. Those Runs START inside the Tree they were
handed and **never self-provision**. Only an **ambient** explorer survives the exemption:
open-ended (no branch) read-only investigation runs in the **main checkout with no Tree**
(the cut is branch-pinned-vs-ambient, not read-vs-write — a reviewer is read-only yet
branch-pinned, so it *does* get a Tree). Enforcement is the flip side — the native
`git worktree` / `EnterWorktree` path is **denied** (PreToolUse, ADR-0014) with a message
pointing at the shipit-owned spawn path, so no role can drift back to the old
shared-worktree mess.

### Build & release

**Toolchain**:
The build/test/provisioning ecosystem of one path in a repo — rust, npm, mkdocs,
go, wasm, … The axis shipit dispatches on: a closed registry like the lint
**Lang** set (adding one is adding an entry; nothing downstream changes).
*Avoid*: "Kind", "stack", "project type" as a *code switch*. "A tauri Kind" is
fine as informal shorthand for a recognizable composition, never a dispatch label.

**Path→toolchain map**:
The `.shipit.toml` declaration mapping each build-bearing path to its
**toolchain**. A repo *is* the set of these entries; shipit composes
provisioning / build / test / lint by walking the map. One repo routinely carries
several (a Tauri app = a rust path + an npm path + maybe an mkdocs path).

**Artifact**:
A produced, content-addressed, distributable unit. Produced by one or more
**toolchain** build targets plus an optional **bundle** step — many-to-many with
the map: one toolchain can yield several artifacts (a rust workspace → a CLI, an
LSP binary, a wasm package, library crates), and several toolchains can yield one
(rust binary + npm frontend → a Tauri app). A **distribution endpoint** attaches
to an artifact. *Avoid*: "build output" — an artifact is the named, addressable
thing, not raw output.

**Bundle**:
The optional composition step that combines toolchain outputs into one
**artifact** (`tauri bundle`, `electron-builder`). *Avoid*: "package" as the
*producing verb* — "package" is the pipeline stage that runs the bundle
(workflows.lex), not the producing task.

**Distribution endpoint**:
A place an **artifact** is published — crates.io, npm, brew, VS Marketplace,
Open VSX, Zed registry, nvim registries, GH release, App Store, … One artifact may
target several (a VS Code extension → Marketplace *and* Open VSX). *Avoid*:
"channel" (overloaded — conda / release channels).

**Endpoint adapter**:
The only place that knows one **distribution endpoint**'s mechanics (how to
publish to it). Adding an endpoint is adding an adapter to the registry; nothing
downstream changes. Mirrors **Reviewer adapter**.

**Content-key**:
The identity of an **artifact** for build-once reuse — a hash of the inputs that
*determine* it: toolchain identity, lockfiles, the artifact's declared input-glob
contents, build profile, and any bundle inputs. A hit on the content-key reuses a
prior build across workflows *and* across git revisions. An artifact that declares
no inputs falls back to the whole-tree commit SHA (always rebuild), so
under-declaration costs a rebuild, never a stale ship. *Avoid*: "cache key" — the
content-key is the artifact's identity, not merely a cache bucket.

**Lane**:
A declared CI test unit — `{ name, consumes an artifact, run = a pixi task,
required, local, trigger (pr / push / nightly / dispatch), runner, scope }`. The
generic CI workflow fans the lanes into jobs; each resolves-or-builds its
**artifact** by **content-key**, runs its harness, and posts results. The
**required** lanes feed the CI-green **Ready** pillar; the non-required ones
surface as signals (like **degraded**) but never **hold**. *Avoid*: "suite", "job"
— a lane may map to a GitHub check, but the lane is the *declaration*.

**Commit/push checks** (the set formerly called "the gate"):
The **checks** a **policy** marks **blocking** at the *commit* and *push* operations —
`lint` + the fast `test` set, the **lanes** that are both **required** and **local**.
Pre-commit / lefthook enforce exactly these; CI enforces a broader policy over *all*
lanes (commit/push checks ⊆ lanes), including non-local / non-required ones (GPU,
nightly native-e2e) that are advisory at commit but blocking later. A missing blocking
check hard-fails — one definition of each check, invoked everywhere (architecture.lex
§7). There is no standalone "gate" noun: this is just one **operation**'s blocking set.

**Scope** (thin / full):
A **lane**'s breadth for a given run, decided by a path-diff: *thin* runs the
minimal set for a PR touching unrelated paths; *full* runs everything (always on
nightly / dispatch / non-PR events). Keeps expensive lanes cheap on unrelated PRs
without dropping coverage on the changes that matter.

**Release**:
A repo-level versioned event that publishes the repo's declared **artifact** set to
their **distribution endpoints**. `shipit changelog` coalesces unreleased fragments
→ bump + tag → for each artifact: resolve-or-build by **content-key**, **bundle**,
sign, publish; the coalesced notes feed both the tag annotation and the GH release.
The build/sign half is an all-or-nothing barrier (publish nothing if any artifact
fails); the publish half is ordered + idempotent-resumable, because external
endpoints cannot be rolled back. *Avoid*: "deploy" — client artifacts are released,
not deployed.

**Cascade**:
The cross-repo release backbone. When a repo releases it fires a uniform
`upstream-released` signal; the **cascade-handler** opens a version-bump PR on each
declared downstream (decision in CI, tag-authoritative). Examples: phos-core →
phos-app, simple-gal → simple-gal-ui, lex/lexd → {vscode, nvim, zed, lexed}.
*Avoid*: "trigger chain", "webhook chain".

**Dependency mode** (source-pinned / artifact-pinned):
How a downstream consumes an upstream it depends on, declared per upstream edge.
*Source-pinned* — pins the upstream by ref/version and rebuilds from source (a bump
busts the **content-key** → rebuild; e.g. phos-app builds wasm from phos-core's
pinned tag). *Artifact-pinned* — fetches the upstream's released **artifact** by
version and does not rebuild it (cross-repo build-once reuse, the "intermediate
artifact" across repo lines; e.g. lexed fetches the lexd-lsp binary).
