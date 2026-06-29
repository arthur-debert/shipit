Epic Topology

This document expands [../../AGENTS.lex] §2 (the always-on summary) with the
full epic topology: per-workstream delegation, integration ordering,
convergence, the docs pass, and the umbrella PR.

An epic — a feature comprising multiple PRs — is the SAME coordinator +
role-split model as the single-task cycle in [../../AGENTS.lex], differing only
in branch/merge topology. There is one overarching feature branch (the *epic
branch*) and one umbrella PR; the execution is a series of single-task cycles
(again [../../AGENTS.lex]) whose workstream PRs merge into the epic branch, and
the umbrella PR finally merges the epic branch to `main`. Delegation, the
implementer-stops-at-open rule, and the fresh-shepherd-per-round are NOT
epic-specific — they are the PR-shepherding role split in [../../AGENTS.lex],
applied here per workstream.

Before execution, a new feature is planned via `/shipit-planning` — the
orchestrator that drives ideation, the overview gate, the ADRs
(`/shipit-grill-with-docs`), the PRD under `docs/prd/` (`/shipit-to-prd`), the
docs PR, then epic/WS decomposition into issues (`/shipit-to-issues`). Each Work
Stream is a vertical slice — a sub-issue of the epic, with blocked-by
dependencies.

1. Information gathering

    The coordinator is briefed as in the single-task cycle's information-gathering
    step in [../../AGENTS.lex] — via the epic tracker issue, the PRD, or a chat
    with the maintainer. It does the general reading/research, CREATES the epic
    branch (`EPIC/umbrella` — see [./naming.lex]) by provisioning its own isolated
    *Tree* to manage that branch (`shipit tree create`; a dissociated clone, never
    a native `git worktree` — ADR-0014 / [../prd/where-to-do-work.md]), and asks the
    maintainer for decisions/clarifications as needed.

2. Delegation per workstream

    The coordinator does NOT implement. It spins one IMPLEMENTER subagent per
    workstream — each scoped by its own Work Stream issue — and runs the
    [../../AGENTS.lex] role split for each: the coordinator CREATES the WS branch
    off the epic branch by provisioning the implementer with a ready *Tree*
    (`shipit tree create --epic E --ws N` → branch `EPIC/WSnn` — see [./naming.lex]); the implementer stops
    at PR-open, the only topology change being that its draft PR targets the EPIC
    branch (not `main`); the coordinator owns the wait and the flip; a fresh
    shepherd handles each addressing round. The 6 / nitpick breaker applies to
    every workstream PR.

    Parallel implementation, serialized integration. Subagents implement
    eligible workstreams concurrently per the dependency graph, but the
    coordinator merges into the epic branch one at a time. After each merge,
    in-flight WS branches pull the new epic head and re-green before their own PR
    flips READY. Workstreams may overlap files; contention is resolved at merge
    time, never by pre-partitioning.

3. Integration

    The COORDINATOR merges each workstream PR into the epic branch once that PR
    is READY (CI green + reviewed + mergeable) — its own go/no-go, no user
    approval needed for these intra-epic merges. This is the one place the
    coordinator merges: workstreams INTO the epic branch, never the epic branch
    into `main`. The user's approval gate is the umbrella PR ([#6]), not the
    individual workstreams.

4. Convergence — clearing the fallouts

    Once the initial workstreams are merged into the epic branch, the
    coordinator gathers the fallouts: follow-ups filed as GitHub issues during
    execution, plus things that surfaced while implementing. It opens one final
    workstream and assigns a subagent to clear them.

    Workstream agents deliberately do NOT side-quest every little thing they find
    — that restraint is correct — but the epic must not merge with a pile of
    decoupled follow-ups trailing behind it. Clear only what belongs to this
    epic: something that surfaced as obviously part of the feature -> do it now,
    in the convergence workstream; a related-but-separate feature -> leave it as
    a filed issue.

5. Documentation pass

    When the convergence workstream merges, the coordinator delegates an
    exploration agent to find what the feature changed in the docs — out-of-code
    docs under `docs/` and docstrings, especially module-level ones that capture
    design, trade-offs, and pointers — and to make those changes on a dedicated
    PR.

6. The umbrella PR

    With the work and docs in, the coordinator opens the feature's umbrella PR.
    It double-checks which issues the PR actually closes, writes a high-level
    description of the whole epic pointing to the related issues, and drives the
    PR (epic branch -> `main`) through the SAME role split — the coordinator
    waits and flips, a fresh shepherd handles each review round — then flips it
    to READY and stops. The HUMAN merges the umbrella PR to `main`; the
    coordinator does not auto-merge it.

    Changelog and release come later: shipit has no `changelog` or `release` /
    `cut` command yet — they arrive with the Workflows epic (see
    [./workflows.lex]). Until then there is no changelog-fragment step in a PR
    and no release phase here.
