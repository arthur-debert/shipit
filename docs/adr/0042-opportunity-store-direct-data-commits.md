# Opportunity store uses direct data commits in a separate repo

> **Status: Proposed.** Opportunity Capture PRD; builds on ADR-0013's
> no-infra posture and the PR lifecycle's distinction between planning evidence
> and execution trackers.

Agents routinely notice test hardening, refactoring, documentation, and tool-safety
improvements while they are implementing or shepherding unrelated work. The current
cycle correctly prevents them from side-questing, but the observation is usually lost.
Putting every observation straight into GitHub Issues would preserve it, but it would
also flood the human issue tracker, spend tokens and GitHub API quota on low-grade
material, and blur the line between "observed" and "ready to execute."

**Decision.**

- **Use a separate Opportunity store repo.** Raw and triaged **Opportunities** live in a
  GitHub-backed repository outside the product repos. Product history stays focused on
  product changes, while the store still uses Git/GitHub as the durable substrate.
- **Treat the store as operational data.** Capturing an Opportunity writes a direct
  data commit to the store with pull/rebase/push retry. A PR-per-Opportunity or
  PR-per-batch workflow is rejected for the raw store because it defeats the cheap-write
  goal and recreates issue-tracker noise in another place.
- **Promote before execution.** An Opportunity becomes normal shipit work only when it is
  promoted into a GitHub issue with the existing label vocabulary. From there, the usual
  delegated draft-PR lifecycle applies unchanged.
- **Keep evaluation separate.** Opportunities are future-work candidates, not **eval
  records**. Eval remains local and objective-first; the Opportunity store preserves
  actionable observations that may later become product or harness work.

## Considered options

- **GitHub Issues as the raw inbox.** Rejected: issues are slow to bulk read/edit, consume
  API quota, and become unreadable to humans when used for untriaged agent observations.
- **Commit Opportunities into each product repo.** Rejected: couples process-generated
  backlog material to product history and forces every consumer repo to absorb the same
  feature shape.
- **Commit Opportunities into the shipit repo.** Rejected: simpler for dogfooding, but it
  makes shipit history the dumping ground for portfolio-wide observations.
- **PRs for every store update.** Rejected for raw capture: it preserves review mechanics
  at the cost of the feature's primary property, cheap capture while context is hot.
- **Local-only capture with later sync.** Rejected for v1: it keeps capture fast but loses
  the GitHub-backed durability and cross-machine visibility the feature is meant to provide.

## Consequences

- The store repo needs a clear data lifecycle and repair posture, because direct data
  commits bypass human review by design.
- Store commits are not product changes and do not authorize implementation. Promotion to
  a GitHub issue is the boundary where a candidate becomes executable work.
- The implementation can stay no-infra: Git and GitHub are the store, and shipit owns the
  small amount of synchronization and schema validation needed to keep the store healthy.
