# Code Guidelines

How agents plan, structure, and ship work. Two tracks: a lightweight one for bug fixes and small issues, and a full design process for new features and larger projects.

## 1. Addressing Code Reviews

### 1.1. Addressing a Review Round

The local agent has more context than the reviewing agent, so it has the final word.

- The local agent decides whether a PR comment is valid. Valid comments are addressed in a commit; otherwise it pushes back with a rationale. 
- Each PR review comment is answered either with the commit that addresses it or a rationale for the pushback. 
- All PR review comments are marked resolved — including deferred nitpicks — so review status stays readable. 
- Push all commits for a round at once, so reviewers configured to re-run do so only once. 

### 1.2. Breaking a Review

We request re-review on new pushes (per-reviewer, opt-in), so a PR may go through multiple rounds. To avoid eternal nitpicking or loss of focus, break the review on whichever comes first:

- Six rounds have been performed, or 
- The current review has only nitpicks remaining. 

**Nitpick** — a comment about documentation wording, naming, or style with no correctness, behavioral, or security impact. Anything touching behavior, correctness, or security is not a nitpick and keeps the round open.

**On break** — the agent posts a one-line "deferred — not blocking this round" reply on each remaining nitpick and marks it resolved, then flips the PR to READY.

## 2. Planning / Structuring Tasks

### 2.1. Bug Fixes / Small Issues

Small, targeted changes get one GitHub issue each, labelled `bug` or `enhancement`, and ship over a single focused PR. This track skips the epic flow: branch `fix/<issue>` off main, PR titled `Fix #<issue>: <desc>`, the human merges to main.

For bugs, always write the test that captures the bug — and thus fails — first, then the fix, then watch the test pass. Reflect on the abstract root cause, not just the one instance: if there is an opportunity to fix the broader root cause or improve testing (e.g. property tests that catch many manifestations), do it.

### 2.2. New Features / Larger Projects

New functionality follows a design process across three stages.

#### 2.2.1. Setup

- Initial briefing: a user-started conversation that includes `/grill-with-docs`. 
- That lands needed changes to CONTEXT.md and a new ADR if relevant. 

#### 2.2.2. Planning

- `/adebert-to-prd` produces the PRD as a file under `docs/prd/` and opens an epic tracker issue pointing to it. The user reviews the PRD. 
- `/adebert-to-issues` turns the PRD into Work Streams (WS): each WS is a vertical slice, published as a sub-issue of the epic, with blocked-by dependencies. 
- The parallelizability and dependency map is recorded on the epic issue — execution detail belongs there, not in the PRD. 

#### 2.2.3. Execution

The agent that receives the go-ahead acts as a coordinating-only agent: it delegates Work Streams to subagents so its own context stays free during long sessions and it can hear and enforce the user's higher-level vision. One epic branch is created for the whole work.

Each WS is driven to a READY PR by its agent:

- Create the WS branch off the epic branch, not main. 
- Implement the change and its tests. 
- Open the PR, referencing the WS issue for auto-close, with context. The PR points to the PRD and issues and explains the implementation — it does not rehash the spec. 
- Await the agent PR reviews and address them (see Addressing Code Reviews) until the review breaks. 
- Ensure CI passes and the PR is mergeable, then flip it to READY using the state tool. 

**Parallel implementation, serialized integration.** Subagents implement eligible WS concurrently per the dependency graph, but the coordinator merges into the epic branch one WS at a time. After each merge, in-flight WS branches pull the new epic head and re-green before their own PR flips READY. WS may overlap files; contention is resolved at merge time, never by pre-partitioning.

**Merging.**

- Agents merge WS PRs into the epic branch, pushing back for more work if needed. 
- Once the epic branch holds all WS, the coordinator opens the epic-to-main PR for the full PRD and drives it through review, checks, and mergeability to READY. 
- The user does the final review and merge of the epic-to-main PR. Humans merge to main unless they request otherwise. 

## 3. Branching / PR / Issue Naming

Work is organised as epics (a large feature or change) made of work streams (a related set of changes that ships as one PR). An epic may span both repos; each repo carries its own epic branch, its own WS, and its own epic PR.

Codes are assigned by the human — repo codes once per project (usually already set) and epic codes at epic creation — never invented by an implementing agent mid-stream. Agents derive WS codes and all names from them.

### 3.1. Identifier

Used in plain language: issue and PR titles, commit logs, cross-references. Form: `REPO-EPIC-WSnn[-FXnn]` — e.g. `APP-GPU02-WS03-FX02`.

- Epic: a registered THEME (3 uppercase letters) + NN — e.g. `GPU02` (there was a GPU01). A roadmap stage, if any, is metadata in the epic body, not the code. 
- Repo: 3 uppercase letters — `APP` (phos-app), `COR` (phos-core). Only for multi-repo projects. 
- Workstream: `WSnn`, scoped per (epic, repo) — both repos may have a WS01 in the same epic. 
- Fix round: `FXnn`, scoped per PR, review-phase only (squashed away on merge) — the Nth review-response round. 

### 3.2. Titles

Form: `<identifier>: Epic: <Epic Name> - Workstream: <WS Name>` — e.g. `APP-GPU02-WS03: Epic: GPU Rendering - Workstream: Tiling`.

### 3.3. Branches

Inside one repo, so no repo prefix; slash-separated; no fix round. Form: `EPIC/WSnn` — e.g. `GPU02/WS03`. The epic branch itself is the bare epic code — e.g. `GPU02`.
