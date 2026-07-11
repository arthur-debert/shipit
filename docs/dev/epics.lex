Epic Topology

This document expands [../../AGENTS.lex] §2 (the always-on summary) with the
full epic topology: per-workstream delegation, integration ordering,
convergence, the docs pass, and the umbrella PR.

An epic — a feature comprising multiple PRs — is the SAME coordinator +
role-split model as the single-task cycle in [../../AGENTS.lex], differing only
in branch/merge topology. There is one overarching feature branch (the
*epic branch*) and one umbrella PR; the execution is a series of those same
single-task cycles, whose workstream PRs merge into the epic branch, and
the umbrella PR finally merges the epic branch to `main`. Delegation, the
implementer-stops-at-open rule, and the shepherd-per-PR round loop (ADR-0035)
are NOT epic-specific — they are the PR-shepherding role split
in [../../AGENTS.lex], applied here per workstream.

Before execution, a new feature is planned via `/planning` — the
orchestrator that drives ideation, the overview gate, the ADRs
(`/grill-me-with-docs`), the Spec under `docs/spec/` (`/to-spec`), the
docs PR, then epic/WS decomposition into issues (`/to-tickets`). Each Work
Stream is a vertical slice — a sub-issue of the epic, with blocked-by
dependencies.

1. Information gathering

    The coordinator is briefed as in the single-task cycle's information-gathering
    step in [../../AGENTS.lex] — via the epic tracker issue, the Spec, or a chat
    with the maintainer. It does the general reading/research and CREATES the epic
    branch (`EPIC/umbrella` — see [./naming.lex]). Its workspace needs no manual
    step: a coordinator session already runs inside its own ephemeral
    *session Tree* from launch (`claude --worktree`, usually via `agent-start claude` —
    ADR-0027), born on `ephemeral/<id>` off `origin/main`, so it creates and manages the epic
    branch by switching branches INSIDE that same Tree (ephemeral-by-path,
    work-by-branch: the dir stays, the branch becomes the work). The old
    session-start hand-run of `shipit tree create` is retired — that primitive is
    how Runs get their Tree minted FOR them (covered under Delegation per
    workstream — by `shipit spawn subagent` or the `WorktreeCreate` hook, never
    hand-created). The Tree is a dissociated clone, never a native `git worktree`
    (ADR-0014 / see [../legacy-prd/where-to-do-work.md]). It asks the maintainer for
    decisions/clarifications as needed.

2. Delegation per workstream

    The coordinator does NOT implement. It spins one IMPLEMENTER subagent per
    workstream — each scoped by its own Work Stream issue — and runs the
    [../../AGENTS.lex] role split for each: the coordinator SPAWNS the implementer
    with `shipit spawn subagent --repo R --epic E --ws N --issue I --role implementer`,
    which mints the ready *Tree* on the WS branch (`EPIC/WSnn` — see [./naming.lex])
    and roots the Run in it (ADR-0017 / ADR-0019) — no hand `shipit tree create`; the
    implementer stops at PR-open. The epic topology is that each WS PR targets the EPIC
    branch (not `main`): the `shipit spawn subagent` verb provisions the WS Tree off
    the epic-grouped base `origin/E/umbrella` and opens its draft PR against the epic
    branch `E/umbrella` (\#176, closed). It fail-closes if `origin/E/umbrella` is
    missing on the remote — a loud exit, never a silent fallback to `origin/main`. The
    coordinator owns the wait and the flip;
    ONE shepherd per workstream PR handles its addressing rounds — parked between
    rounds, resumed per round (ADR-0035). The round-cap / nitpick breaker
    (`round_cap` in `[reviewers]`, default 6) applies to every workstream PR.

    Parallel implementation, serialized integration. Subagents implement
    eligible workstreams concurrently per the dependency graph, but the
    coordinator merges into the epic branch one at a time. After each merge,
    in-flight WS branches MERGE the new epic head in (never rebase — see the
    currency rule in §3) and re-green before their own PR flips READY.
    Workstreams may overlap files; contention is resolved at merge time, never
    by pre-partitioning.

3. Integration

    The COORDINATOR merges each workstream PR into the epic branch once that PR
    is READY (CI green + reviewed + mergeable) — its own go/no-go, no user
    approval needed for these intra-epic merges. This is the one place the
    coordinator merges: workstreams INTO the epic branch, never the epic branch
    into `main`. The user's approval gate is the umbrella PR (§6), not the
    individual workstreams.

    A merge into the epic branch deterministically conflicts every remaining
    open WS PR touching a shared additive seam (a registry map, an error
    table) — that consequence needs no discovery. Immediately after each
    intra-epic merge, the coordinator re-checks every still-open WS PR
    (`shipit pr status`) and dispatches the re-merges in the same action,
    rather than waiting to be woken; reviewer waits are never left unwatched —
    `shipit pr wait` (ADR-0034) is the blocking watch (CLI02 retro: an 8-minute
    dead gap between a merge and the resulting conflict being handled).

    Epic-branch currency is MERGE-only: a WS branch takes the new epic head by
    merging `EPIC/umbrella` in, NEVER by rebasing onto it. Re-review rounds
    are head-strict incremental — each round reviews only
    `last-reviewed-head..new-head` (ADR-0043) — and a rebase rewrites the WS
    branch's history so the last-reviewed head is no longer an ancestor of the
    new one: the next round's range degenerates to the whole umbrella delta,
    and reviewers re-flag sibling workstreams' already-landed code as if it
    were this PR's (observed live on \#732; confirmed by the maintainer
    mid-epic). Merging keeps the range to the merge commit plus the genuine
    fixes, and the squash-merge at integration erases the merge commits
    anyway — the merge-commit noise never outlives the WS PR.

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

    Track defect CLASSES, not just defects. After roughly two same-class
    fixes, the coordinator stops and proposes a first-principles regroup —
    what the operation does, what it needs, what the consumer sees — instead
    of the next point fix: one decomposition beats N reactive workstreams
    (ADP00 retro; the regroup there became ADR-0033 and resolved the whole
    defect family the point fixes were gating). Every convergence workstream
    includes a root-cause second pass, and the coordinator volunteers
    burn-down / are-we-converging assessments at milestones without being
    asked.

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
    waits and flips, one shepherd owns the umbrella PR's review rounds — then
    flips it to READY and stops. The HUMAN merges the umbrella PR to `main`; the
    coordinator does not auto-merge it.

    Changelog and release come later: shipit has no `changelog` or `release` /
    `cut` command yet — they arrive with the Workflows epic;
    see [./workflows.lex]. Until then there is no changelog-fragment step in
    a PR and no release phase here.

7. Session memory dies with the Tree — promote learnings before wrapping up

    A coordinator session runs in an ephemeral session Tree (§1), and session
    auto-memory is keyed to the working-directory PATH
    (`~/.claude/projects/<path-slug>/memory/`). When the ephemeral tree is
    gc'd, memories written there are orphaned: a future session runs in a
    different tree, gets a different path slug, and never loads them.
    Confirmed live in the ADP00 retro — the epic's own feedback memory was
    unreachable to every successor session.

    Durable learnings must therefore be promoted INTO THE REPO before a
    session ends. The coordinator role carries the end-of-epic /
    end-of-session promotion clause: a process rule goes to the relevant role
    .lex (then `pixi run regen-roles`) or docs/dev/; a decision to an ADR;
    vocabulary to CONTEXT.md; an open investigation to a tracker issue.
    Session memory is a scratchpad for the session that wrote it, never an
    archive.
