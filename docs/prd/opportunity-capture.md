# Opportunity Capture

> Authoritative spec for capturing actionable, out-of-scope improvement observations
> from agent Runs. Decision record: [ADR-0042](../adr/0042-opportunity-store-direct-data-commits.md).
> Vocabulary: `CONTEXT.md` (**Opportunity**, **Opportunity store**, **Run**, **Repo**,
> **PRD**, **Epic issue**).

## Problem Statement

Shipit agents already produce useful codebase observations while they implement,
review, and shepherd work: tests that should be hardened, refactors that would simplify
future changes, docs that are misleading, and tool-safety gaps. The dev cycle correctly
keeps those agents focused on the authorized task, so these observations usually stay in
the transcript and disappear.

The missing product capability is a cheap way to preserve these observations without
turning GitHub Issues into an untriaged inbox, adding infrastructure, or spending more
agent tokens than the observation is worth. A captured observation must remain clearly
separate from executable work until it is triaged and promoted into the normal shipit
issue and PR lifecycle.

## Solution

Add **Opportunity Capture**: a `shipit opportunities` command group and narrow role-prompt
guidance that let agents and humans record actionable, evidenced, out-of-scope
improvement observations into a separate GitHub-backed **Opportunity store**.

The v1 loop stays intentionally small. A producing Run captures an Opportunity with a
small required front matter header and markdown body. The store organizes Opportunities
through a simple lifecycle: `inbox`, `triaged`, `ready`, `opened`, and `archive`.
Maintainers can list, triage, mark ready, and promote a ready Opportunity into a normal
GitHub issue with existing labels such as `feature`, `small`, and `ready-for-agent`.
Only the promoted issue enters the normal delegated PR lifecycle.

## User Stories

1. As an implementer agent, I want to capture an actionable improvement I notice while
   working on another task, so that I do not side-quest or lose the observation.
2. As a shepherd agent, I want to preserve review-round follow-up ideas that are outside
   the PR's scope, so that review addressing stays focused.
3. As a coordinator, I want captured Opportunities stored outside the product repo, so
   that product history stays focused on product changes.
4. As a maintainer, I want raw Opportunities kept out of GitHub Issues, so that the human
   issue tracker remains readable.
5. As a maintainer, I want capture to require concrete evidence, so that the store does not
   become a vague thought log.
6. As a maintainer, I want every Opportunity to name its Repo and source, so that I can
   later validate whether it is still relevant.
7. As a maintainer, I want Opportunity capture to be cheap while the agent has context, so
   that the system preserves useful observations without a separate interview.
8. As a maintainer, I want a lifecycle separating inbox, triaged, ready, opened, and
   archived Opportunities, so that I can tell whether an item is raw, classified,
   executable, promoted, or dead.
9. As a maintainer, I want to list Opportunities by Repo and tags, so that I can review
   relevant candidates without scanning the whole store.
10. As a maintainer, I want triage metadata to be added later rather than required at
    capture, so that producing agents do not fabricate value or complexity judgments.
11. As a maintainer, I want ready Opportunities promoted into normal GitHub issues, so that
    execution uses the existing shipit issue and PR lifecycle.
12. As a maintainer, I want promoted issues linked back to their source Opportunity, so that
    the evidence and provenance remain available.
13. As a maintainer, I want promotion to apply existing labels, so that agents can pick up
    the work through the same issue vocabulary they already understand.
14. As a future curator agent, I want a stable schema and lifecycle, so that later batched
    curation can group, validate, and retire Opportunities without changing raw capture.
15. As a future implementer, I want stale or superseded Opportunities archived rather than
    silently deleted, so that the store remains auditable.
16. As an operator, I want no local service, database, or new provider, so that Opportunity
    Capture fits shipit's no-infra posture.
17. As a portfolio maintainer, I want each consumer repo to declare its Opportunity store,
    so that the feature can work across repos without hard-coding one private store.
18. As a contributor, I want the CLI and docs to use the same domain noun, so that
    "Opportunity" means one thing across glossary, PRD, and commands.

## Implementation Decisions

- **Command group**: add `shipit opportunities` as the user-facing CLI. The command group
  owns capture, list, triage/ready transitions, archive, and promotion.
- **Configuration**: read the store location from `[project.opportunities]` in
  `.shipit.toml`, using the existing consumer-owned escape hatch rather than adding a new
  top-level config table.
- **Store boundary**: the **Opportunity store** is a separate GitHub repo. Shipit treats it
  as operational data and writes direct commits with pull/rebase/push retry. Product repos
  receive no raw Opportunity files.
- **Lifecycle**: v1 exposes `inbox`, `triaged`, `ready`, `opened`, and `archive` directories.
  The directory is the lifecycle authority; front matter mirrors that state so tools can
  validate moves, and mismatches are validation errors rather than silently reconciled.
- **Capture schema**: capture requires a small front matter header: schema version, Repo,
  source, tags, lifecycle status, and creation timestamp. The body carries the observation,
  evidence, and suggested next step. Value, complexity, and confidence are triage outputs,
  not capture requirements.
- **Promotion**: promoting a ready Opportunity creates a normal GitHub issue in the target
  Repo, links back to the Opportunity, records the issue number, moves the file to `opened`,
  and applies existing labels such as `feature`, `small`, and `ready-for-agent` when
  appropriate.
- **Role prompt guidance**: implementer and shepherd role definitions gain a narrow clause:
  capture only actionable, evidenced, out-of-scope improvements, and never let capture
  distract from the current task.
- **Module boundaries**: build a deep Opportunity domain module for schema/lifecycle
  validation and rendering; a Git-backed store module for checkout/sync/path allocation and
  commits; a thin CLI shell; and small role-prompt changes through the existing generated
  role prompt system.
- **Adapter posture**: Git and GitHub interactions route through shipit's existing Tool
  adapters. Command modules do not build raw `git` or `gh` argv directly.
- **v1 is manual after capture**: no scheduled curation, no background implementation, and
  no agent-generated grouping in the first release.

## Testing Decisions

- Good tests assert externally visible behavior: schema validation, lifecycle moves, rendered
  CLI output, promoted issue payloads, and store synchronization decisions. They should not
  assert incidental file-writing internals.
- Test the Opportunity domain module with table-driven cases for valid capture, malformed
  front matter, illegal lifecycle transitions, missing evidence, and ready/opened metadata.
- Test the store module with fake git adapters or temporary local repos, covering path
  allocation, clean direct commits, pull/rebase/push retry behavior, and conflict/error
  reporting without using real GitHub.
- Test CLI verbs for create, list, triage/ready, archive, and promote, including user-facing
  errors when configuration is missing or an Opportunity is not promotable.
- Test promotion with a fake GitHub adapter, verifying the issue title/body/labels and the
  file move to `opened`.
- Test generated role prompts to ensure the Opportunity guidance lands in implementer and
  shepherd prompts without expanding coordinator implementation authority.
- Prior art: follow the existing pure-core/thin-boundary style used by PR state, eval, git,
  and gh adapter tests.

## Out of Scope

- Agent curation, clustering, validation against current `main`, or scheduled indexing.
- Automated implementation of ready Opportunities.
- PR-per-Opportunity or PR-per-batch review workflow for the store repo.
- New GitHub labels beyond the existing shipit label vocabulary.
- A database, local service, GitHub App, Pages site, or hosted dashboard.
- Treating Opportunities as eval records or harness metrics.
- Making raw Opportunities close issues or authorize implementation.

## Further Notes

- The first success criterion is high-signal capture without task distraction. Automation
  should only expand after captured Opportunities prove useful enough to justify curation
  cost.
- The lifecycle deliberately leaves room for future batched curator agents, but v1 should
  stay deterministic wherever possible.
- The PRD precedes `/to-issues`; Work Streams should be derived from this file after the
  docs PR merges.
