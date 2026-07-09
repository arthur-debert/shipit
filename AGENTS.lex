Code Guidelines

How agents plan, structure, and ship work. There is ONE dev/PR lifecycle:
draft-first, state-machine-driven, and ALWAYS delegated — the same flow whether a
change ships as one PR to `main` or as an epic of many PRs, which differ ONLY in
branch/merge topology, never in whether delegation happens.

This file is the always-on core. For the full epic topology see [./docs/dev/epics.lex];
for the naming grammar see [./docs/dev/naming.lex]. Pull them in when the task
calls for them.

The dev cycle is ALWAYS delegated:

    The agent the human addresses is a COORDINATING agent: it never implements.
    Regardless of task size it spawns subagents — there is no "small enough to do
    it myself" path. Deciding burns the coordinator's context (and it usually
    starts executing before it finishes deciding); a fixed always-delegate rule
    removes that and keeps the coordinator light for long, multi-PR efforts.

    Three roles, so no one context carries all of it:

    - an IMPLEMENTER subagent implements (+ tests), runs the checks green, opens a
        DRAFT PR, then STOPS AT PR-OPEN — it never sees a review round;
    - the COORDINATOR owns every wait and the flip;
    - ONE SHEPHERD per PR owns the review-addressing rounds (ADR-0035):
        briefed cold on round 1, PARKED between rounds, resumed with a
        one-line brief when the next round lands.

    Canonical source: `arthur-debert/release` `docs/dev-cycle.lex`; on drift, it
    wins.

The PR lifecycle (draft -> ready -> stop):

    Every change ships as a PR the agent drives. Open it as a DRAFT — WIP the
    agent owns. Shepherd the whole loop while it stays draft: request + address
    reviews, get CI green, make it mergeable. Flipping draft -> ready
    (`shipit pr ready`) is the ONE signal that means "done iterating — a human can validate
    and merge": it happens only when all three hold — reviews addressed, CI green,
    mergeable.

    Stop at that flip; do NOT merge. The HUMAN does the final read + merge, on
    explicit authorization only. A "changes needed" flips back to draft
    (`shipit pr ready --undo`); the loop repeats and re-flips when green.

    FLOOR / CEILING:
    - FLOOR: committing, pushing, opening the draft PR are the agent's OWN job,
        no go-ahead needed. "Stop at the ready flip" NEVER means "wait to be asked
        to commit" or "leave finished work uncommitted".
    - CEILING: the ONLY step needing a human is the merge. Drive everything up to
        and including the ready flip on your own authority, then stop.

1. The single-task cycle (one PR)

    The unit of work, identical whether it ships as one PR to `main` or as one
    workstream of an epic ([#2]). The coordinator delegates it; it never runs the
    steps itself.

    1.1. Information gathering (the coordinator)

        Align on what's to be done before any code is touched — and before
        delegating.

        - Task: a GitHub issue, a handoff artifact, or a maintainer message. A
            maintainer-directed quick fix needs NO issue first — a direct
            instruction is its own authorization; ship the fix PR.
        - Contextualize: read the description + related code/resources.
        - Clarify: if information is missing or a real PRODUCT/SCOPE decision
            exists, surface it — propose a preferred option, don't only ask. The
            dev cycle and the epic branch/merge topology ([#2]) are FIXED policy,
            never a choice to put to the human — never offer a PR-strategy menu.

        The coordinator reads/researches to brief the work, then delegates. It
        does not implement.

    1.2. Implementation (the implementer subagent)

        The coordinator SPAWNS an IMPLEMENTER to do the task + tests — shipit OWNS
        spawning (ADR-0017 / ADR-0019), so the *Tree* is always minted FOR the Run,
        never provisioned by hand. Two launch paths, both routing the Run into an
        isolated Tree: the `shipit spawn subagent --repo R --epic E --ws N --issue I --role implementer`
        verb (it resolves the base and creates the Tree, then roots a headless
        agent in it — for a work stream (`--epic E --ws N`) the verb cuts the WS Tree
        off the epic-grouped base `origin/E/umbrella` and its draft PR targets the
        epic branch `E/umbrella`, matching the coordinator-driven epic topology; it
        fail-closes loudly if `origin/E/umbrella` is absent on the remote — never a
        silent fallback to `origin/main` (#176, closed). For a standalone issue
        (`--issue N` with NO `--epic`/`--ws`) the same verb cuts `issues/<id>/<session>`
        (session default `work`) off `origin/main`, its draft PR targeting `main` — the
        single-issue analog of the work-stream path (ADR-0026)), or the in-CC `Agent(isolation:"worktree")` tool, whose spawn the
        `WorktreeCreate` hook auto-routes into a Tree. The coordinator never runs
        `shipit tree create` by hand to provision a Run and never points an Agent tool at
        an external checkout. A Tree is a dissociated clone rooted as the Run's cwd (no
        bash-cwd footgun), NOT a native `git worktree` — that path is denied (ADR-0014) —
        so concurrent agents never collide on one checkout;
        see [./docs/prd/where-to-do-work.md]. The implementer runs the checks (`shipit lint`)
        and tests (`pixi run test`) green BEFORE opening the PR — CI runs the same as
        required checks, so local green is necessary for CI green.

        Check fidelity: a check that reads ambient local state (a sibling
        checkout, a machine-only tool, an env var CI lacks) passes locally and
        lies about CI. If a check needs something, make CI provide it.

        For bugs: write the failing test first, then the fix, then watch it pass.
        Fix the abstract root cause, not just the instance.

    1.3. PR shepherding — the role split (draft-first, engine-driven)

        Why split: an implementer that also shepherds drags its full
        implementation context through every round and judges review comments
        worse — defending remembered choices instead of reading the diff. The
        detail each role adds beyond the shape above:

        - IMPLEMENTER: the DRAFT it opens links the issue (`for #<id>` /
            `closes #<id>`) and carries a `## Context` handoff note — why this
            approach, what's out of scope, what NOT to "fix" — written for the
            stranger who addresses the rounds.
        - COORDINATOR: the PR engine is STATELESS ("now" is an input); the ONE
            verb that blocks is `shipit pr wait` (ADR-0034). Drive with
            `shipit pr next` / `shipit pr status`, own every wait behind
            `shipit pr wait --until reviews-in|ready`, and flip with
            `shipit pr ready` once the engine reports READY (the guard refuses
            an early flip).
        - SHEPHERD: ONE per PR (ADR-0035), briefed cold with just the PR number +
            Context note on round 1, then PARKED between rounds and resumed
            with a one-line brief per round — it re-reads each round's findings
            from the PR, not from memory. Each round it triages open threads —
            fix, or reply with a rationale, resolving each — sweeps the PR diff
            for other instances of each finding's class, pushes the round's
            commits at once, hands back, and parks. The local agent has more
            context than the reviewer, so it has the final word; every thread
            ends resolved (including deferred nitpicks).

    1.4. Validation

        The single PR targets its base (`main`, or the epic branch for a
        workstream); the coordinator drives it to READY and stops — the HUMAN
        merges. More work needed -> back to draft (`shipit pr ready --undo`),
        re-green, re-flip.

    Engine-owned policy — trust the tool, don't carry it in your head:

        `shipit pr next` / `status` own the reviewer set, the re-request rules
        (per-reviewer, default review-once — a push does NOT re-stale a
        review-once reviewer), and the stopping breaker (stop at the configured
        round cap — `round_cap` in `[reviewers]` of `.shipit.toml`, default 6 — or when
        a round is all *nitpicks* — wording/naming/style with no correctness,
        behavior, or security impact). Do what the engine reports rather than
        re-deriving these; on break it routes straight to READY, and a real
        blocker (failing CI, conflict) still blocks on its own terms.

2. Epics (multiple PRs)

    An epic — a feature of multiple PRs — is the SAME coordinator + role-split
    model as [#1], differing ONLY in branch/merge topology: one *epic branch* +
    one umbrella PR; each workstream is a single-task cycle ([#1]) whose PR targets
    the epic branch (not `main`). The coordinator merges each READY workstream PR
    INTO the epic branch on its own authority (parallel implement, serial
    integrate); the HUMAN's one checkpoint is the umbrella PR (epic branch -> `main`).
    Convergence (clear epic-owned fallouts) and a docs pass precede the umbrella.

    This topology is FIXED policy, not a choice: the coordinator does NOT ask the
    human to pick a PR strategy (one big PR, one PR per workstream to `main`, an
    epic branch) — a multi-PR feature runs on the epic branch, full stop.

    A feature is planned before execution via `/planning` (ideation -> ADRs
    -> PRD -> docs PR, then epic/WS decomposition -> issues).

    Full topology — per-workstream delegation, integration ordering, convergence,
    the docs pass, the umbrella, changelog/release status. See [./docs/dev/epics.lex]
    before running an epic.

3. Naming

    Codes are assigned by the HUMAN — repo codes once per project, epic codes at
    epic creation — never invented by an implementing agent. Agents derive WS
    codes + names from them.

    Per-PR essentials:
    - Identifier (epic work): `REPO-EPIC-WSnn[-FXnn]` — e.g. `APP-GPU02-WS03`.
    - PR title (epic work): `<identifier>: Epic: <Epic Name> - Workstream: <WS Name>`;
        a standalone PR uses a plain summary.
    - PR body: `closes #<id>` (auto-closes on merge to `main`) or `for #<id>`
        when it must not (e.g. a WS PR onto an epic branch).
    - Branch: `EPIC/WSnn` (slash-namespaced); the epic (umbrella) branch is
        `EPIC/umbrella`, not bare `EPIC` (which would collide with the `EPIC/WSnn`
        refs). The plain-language identifier stays hyphenated (`EPIC-WSnn`).

    Full grammar + rationale — the THEME registry, multi-repo prefixes, FX rounds,
    the slash-collision reason. See [./docs/dev/naming.lex].

4. Role prompts (generated, role-scoped)

    Each role's binding prompt is GENERATED from focused lex fragments under
    [./src/shipit/data/roles] — a shared dev-cycle base plus one overlay per role
    — so the cycle is stated once and re-flows on a single edit
    (`pixi run regen-roles`). Each agent receives ONLY its own role's slice, never the
    others', which is what stops mid-session role drift (ADR-0011).

    The roles (one line each; non-binding map — the binding surfaces are the
    agent-defs and the coordinator deny reason):

    - coordinator — the agent the human addresses; orchestrates and delegates, never implements. Its slice rides the PreToolUse deny reason plus injected context (it has no agent-def).
    - implementer — builds the change with tests and opens the draft PR, then stops; agent-def [./.claude/agents/implementer.md].
    - shepherd — owns addressing for one PR across its review rounds, parked between rounds and resumed per round; agent-def [./.claude/agents/shepherd.md].
    - explorer — read-only investigator: searches and reports, changes nothing; agent-def [./.claude/agents/explorer.md].
