# shipit

shipit standardizes work across a personal portfolio of repos: provisioning,
the dev workflow + skills, a multi-language lint check, GitHub repo setup, pixi
tooling, the PR-review state machine, and the build/release workflows. This
glossary fixes the language of that domain — especially the PR-flow vocabulary
inherited from release-core and the build/release vocabulary.

## Language

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
user stories, decisions). Locked by a merged docs PR; produced by `/shipt-to-prd`.
*Avoid*: treating the **epic issue** as the spec — the PRD is the source of truth.

**Epic issue**:
An **execution tracker** — a GitHub umbrella issue that points to the **PRD** and
the relevant ADRs and carries a PRD summary plus the **Work Stream** topology and
progress (sub-issues). It tracks *how the work lands*, not *what to build*. One
feature may have several epic issues; produced by `/shipt-to-issues`.
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
An attempted transition someone (human or agent) wants to make — *commit*, *push*,
*open-PR*, *flip-to-Ready*, *merge*, *release*. The unit that can be *blocked*. Each
operation runs the checks its **policy** binds, evaluated in that operation's context.

**Policy** (per operation):
The binding that says, for one **operation** in a given context, whether each relevant
**check** is **blocking** (its failure stops the operation) or **advisory** (its failure
is recorded and surfaced — cf. **degraded** — but the operation proceeds). Enforcement is
contextual, not global: `lint` + the fast `test` set block at *commit/push*; an expensive
`test-e2e` / GPU lane is advisory there and blocking only at *open-PR* / *merge*. A
**lane**'s `required` / `local` / `trigger` fields ARE its policy across operations —
`required` = blocking at *merge*, `local` = also enforced at *commit/push*, `trigger` =
which operations run it at all.
*Avoid*: one global "the gate" — there are as many enforcement sets as there are operations.

**Blocking / Advisory**:
The two roles a **check** can play under a **policy** for a given **operation**. *Blocking*:
its failure stops the operation. *Advisory*: its failure is surfaced but does not stop it. A
check is never blocking or advisory in the abstract — only for a named operation in a named
context. (So "pre-commit runs `lint` + `test`" means: the *commit* operation's policy marks
those two checks blocking — not that they are gates.)

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

**Verb / task**:
A uniform-named pixi task — shipit's stable per-repo interface. `pixi run <verb>` means
the same thing in every repo → a consumer-supplied task; the per-**toolchain** mechanics
hide behind the name, so callers (lefthook, CI, agents) never branch on the stack. The
verb set: `lint`, `test` (+ **test variants**), `build`, `docs-build`, `release`, `fmt`,
`run`/`serve`, `docs-serve`, `clean`. Extra args reach the underlying tool via `pixi run
<verb> -- <native args>` (pixi appends them verbatim; shipit does not model the arg
surface). Full rationale: `docs/dev/verbs-tasks.lex`. *Avoid*: "script", "command" as the
dispatch unit — the verb is the stable name; the task body varies per repo.

**Test variant**:
A named `test-*` task (`test-e2e`, `test-wasm`, `test-tauri`) beside the fast `test`. WHICH
one runs WHEN is the **Lane** / **Scope** decision — the blocking commit/push set is the
fast `test` plus `lint`; variants are advisory at commit, blocking later in CI. *Avoid*: treating
a variant as its own blocking check — it is a lane, scheduled by trigger.

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
