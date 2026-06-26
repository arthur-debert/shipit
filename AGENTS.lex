Code Guidelines

How agents plan, structure, and ship work. There is ONE dev/PR lifecycle:
draft-first, state-machine-driven, and ALWAYS delegated. It is the same flow
whether a change ships as one PR to `main` or as an epic of many PRs — those
differ ONLY in branch/merge topology, never in whether delegation happens.

The dev cycle is ALWAYS delegated:

    The agent the human addresses is a COORDINATING agent: it never
    implements. Regardless of task size it spawns subagents to do the work —
    there is no "small enough to just do it myself" path. Deciding whether a
    task is simple enough to do directly itself burns the coordinator's
    context, and the agent usually starts executing before it finishes
    deciding. A fixed always-delegate rule removes that failure mode and keeps
    the coordinator's context light for long, multi-PR efforts.

    The cycle is SPLIT across roles so no one context carries all of it:

    - an IMPLEMENTER subagent implements (and writes/updates tests), runs the
      gate green, and opens a DRAFT PR — then STOPS AT PR-OPEN; it never sees a
      review round;
    - the COORDINATOR owns every wait and the flip;
    - a FRESH SHEPHERD subagent handles each review-addressing round — one per
      round, briefed cold.

    The canonical statement of this cycle lives in `release/docs/dev-cycle.lex`;
    this document adapts it to shipit's `shipit pr` engine. On any drift, the
    canonical doc wins.

The PR lifecycle (draft -> ready -> stop):

    Every change ships as a PR the agent drives. Open it as a DRAFT — a draft
    is WIP the agent owns. Shepherd the whole loop while it stays draft:
    request and address reviews, get CI green, and make it mergeable. Flipping
    draft -> ready (`shipit pr ready`) is the ONE signal that means "done
    iterating — a human can validate and merge", so it happens only when all
    three hold: reviews addressed, CI green, mergeable.

    Stop at that flip; do NOT merge. The HUMAN does the final read and merge —
    merge only on explicit authorization. A human request for changes flips
    the PR back to draft (`shipit pr ready --undo`); the loop repeats and
    re-flips to ready when green.

    FLOOR and CEILING — what needs no go-ahead, and the one thing that does:

    - FLOOR: committing, pushing, and opening the draft PR are the agent's OWN
      job and need no human go-ahead. "Stop at the ready flip" NEVER means
      "wait to be asked to commit" or "leave finished work uncommitted" —
      finished work is committed, pushed, and opened as a draft PR without
      asking.
    - CEILING: the ONLY human-gated step is the merge. The agent drives
      everything up to and including the ready flip on its own authority, then
      stops.

1. The single-task cycle (one PR)

    The unit of work, applied identically whether the task ships as one PR to
    `main` or as one workstream of an epic ([#2]). The coordinator delegates
    it; it does not run the steps itself.

    1.1. Information gathering (the coordinator)

        The coordinator aligns on what is to be done before any code is
        touched — and before delegating.

        - Task description: a GitHub issue, a handoff artifact, or an
          interactive maintainer message. A maintainer-directed quick fix does
          NOT require filing a GitHub issue first — a direct maintainer
          instruction is its own authorization; ship the fix PR. This
          overrides the default of filing one issue per change for that case.
        - Contextualization: read the description and the related code /
          resources.
        - Clarifications: if information is missing or a real decision point
          exists, surface it to the maintainer — and where possible propose a
          preferred option rather than only asking.

        The coordinator does the reading/research needed to brief the work,
        then delegates the implementation. It does not implement.

    1.2. Implementation (the implementer subagent)

        The coordinator CREATES the fix branch off `main` (`fix/<issue>` for
        an issue-scoped task) and spawns an IMPLEMENTER subagent to do the
        task, writing or improving tests where needed.

        The implementer runs the gate — `shipit lint` — and the tests — `pixi
        run test` — until both are green before opening the PR. CI runs the
        same gate plus the tests as required checks, so local green is
        necessary for CI green.

        Gate fidelity: a local check that reads ambient local state — a sibling
        checkout, a tool only your machine has, an env var CI doesn't set —
        passes locally and lies about CI. If a check needs something, make CI
        provide it rather than trusting that local green implies CI green.

        For bugs, write the test that captures the bug — and thus fails —
        first, then the fix, then watch it pass. Reflect on the abstract root
        cause, not just the one instance: if there is an opportunity to fix the
        broader root cause or improve testing, do it.

    1.3. PR shepherding — the role split (draft-first, engine-driven)

        The cycle is SPLIT across roles so no one context carries all of it. An
        implementer that also shepherds its own review rounds drags the full
        implementation context — exploration, dead ends, test output — through
        every round, and a long context is also a worse judge of review
        comments, defending remembered choices instead of reading the diff on
        its merits. So:

        - The IMPLEMENTER subagent STOPS AT PR-OPEN. It opens the PR as a DRAFT
          (linking the issue if relevant: `for #<id>` / `closes #<id>`) with a
          `## Context` handoff note in the PR body — why this approach, what is
          out of scope, what NOT to "fix" — written for a stranger, because a
          stranger is exactly who addresses the review rounds. Then it reports
          back and terminates. It never sees a review round.
        - The COORDINATOR owns every wait and the flip. shipit's PR engine is
          STATELESS — "now" is an input, there is no looping wait command
          (shipit deliberately has no `pr wait`; see the *Wait window* entry in
          CONTEXT.md and [./docs/adr/0006-readiness-with-degraded-reviewers.md]).
          So the coordinator does NOT block on an engine wait: it drives the PR
          with `shipit pr next` / `shipit pr status`, manages the waiting
          cadence itself, and flips with `shipit pr ready` once the engine
          reports READY. The guard refuses the flip early, so it can't fire
          prematurely. That hands the PR to a human; don't auto-merge.
        - A FRESH SHEPHERD subagent handles each ADDRESSING round. When `shipit
          pr status` reports addressing is needed, the coordinator spawns a
          shepherd whose brief is just the PR number and the Context note. It
          triages the open threads (fix or reply with rationale, resolving as
          it goes), pushes all commits for the round at once, re-requests
          review (`shipit pr review request`), hands back to the coordinator,
          and terminates. One fresh shepherd per round — each starts cold on
          the diff as it exists.

        On addressing a round (the shepherd's discipline):

            The local agent has more context than the reviewing agent, so it
            has the final word.

            - The local agent decides whether a PR comment is valid. Valid
              comments are addressed in a commit; otherwise it pushes back with
              a rationale.
            - Each PR review comment is answered either with the commit that
              addresses it or a rationale for the pushback.
            - All PR review comments are marked resolved — including deferred
              nitpicks — so review status stays readable.
            - Push all commits for a round at once, so reviewers configured to
              re-run do so only once.

    1.4. User validation

        For a single-PR task the one PR targets `main`; the coordinator drives
        it to READY and the HUMAN merges it. The coordinator stops at the READY
        flip and does not auto-merge — merge only on explicit authorization. If
        more work is needed, flip the PR back to draft (`shipit pr ready
        --undo`); only when the new changes + checks pass flip back to READY
        for re-validation.

    On re-requesting a review (per-reviewer, default review-once):

        Re-run-on-push is a PER-REVIEWER setting and defaults OFF (review once)
        for everyone. A review-once reviewer's review counts on ANY head and is
        never stale after a push — it is not re-requested. `rerun: true` is a
        per-reviewer opt-in: only those reviewers are re-requested after a push
        (their earlier-head review is stale, so `shipit pr status` advises
        RE-REQUEST). All reviewers are token-billed — local agents (agy-local /
        codex-local) cost a real model run each time — so re-reviewing each new
        head is explicit opt-in, not the default. Trust the next action `shipit
        pr status` reports rather than re-requesting manually.

    On stopping the review loop (the 6 / nitpick breaker):

        Each round, address every review comment, EXCEPT stop when EITHER:

        - 6 rounds have happened (there is no 7th round), or
        - the current round is all nitpicks — nothing that changes
          correctness, behaviour, or security.

        *Nitpick* — a comment about documentation wording, naming, or style
        with no correctness, behavioral, or security impact. Anything touching
        behavior, correctness, or security is not a nitpick and keeps the round
        open.

        On break, the shepherd posts a one-line "deferred — not blocking this
        round" reply on each remaining nitpick and marks it resolved. When
        either condition is hit on an otherwise-ready PR (CI green, mergeable),
        the engine routes straight to READY: the coordinator flips and hands to
        the human; it does NOT open another round. A real blocker (failing CI,
        conflict) still blocks on its own terms — the breaker never invents a
        block.

2. Epics (multiple PRs) — the topology

    An epic — a feature comprising multiple PRs — is the SAME coordinator +
    role-split model as [#1], differing only in branch/merge topology. There is
    one overarching feature branch (the *epic branch*) and one umbrella PR; the
    execution is a series of single-task cycles ([#1]) whose workstream PRs
    merge into the epic branch, and the umbrella PR finally merges the epic
    branch to `main`. Delegation, the implementer-stops-at-open rule, and the
    fresh-shepherd-per-round are NOT epic-specific — they are [#1.3], applied
    here per workstream.

    Before execution, a new feature is planned through shipit's design skills:
    `/shipt-grill-with-docs` lands the CONTEXT.md / ADR changes,
    `/shipit-to-prd` writes the PRD under `docs/prd/` and opens the epic
    tracker issue, and `/shipit-to-issues` turns the PRD into Work Streams
    (each a vertical slice, a sub-issue of the epic, with blocked-by
    dependencies).

    2.1. Information gathering

        The coordinator is briefed as in [#1.1] — via the epic tracker issue,
        the PRD, or a chat with the maintainer. It does the general
        reading/research, CREATES the epic branch (the bare epic code — see
        [#3]), and asks the maintainer for decisions/clarifications as needed.

    2.2. Delegation per workstream

        The coordinator does NOT implement. It spins one IMPLEMENTER subagent
        per workstream — each scoped by its own Work Stream issue — and runs
        the [#1.3] role split for each: the coordinator CREATES the WS branch
        off the epic branch (`EPIC-WSnn` — see [#3]); the implementer stops at
        PR-open, the only topology change being that its draft PR targets the
        EPIC branch (not `main`); the coordinator owns the wait and the flip; a
        fresh shepherd handles each addressing round. The 6 / nitpick breaker
        applies to every workstream PR.

        Parallel implementation, serialized integration. Subagents implement
        eligible workstreams concurrently per the dependency graph, but the
        coordinator merges into the epic branch one at a time. After each
        merge, in-flight WS branches pull the new epic head and re-green before
        their own PR flips READY. Workstreams may overlap files; contention is
        resolved at merge time, never by pre-partitioning.

    2.3. Integration

        The COORDINATOR merges each workstream PR into the epic branch once
        that PR is READY (CI green + reviewed + mergeable) — its own go/no-go,
        no user approval needed for these intra-epic merges. This is the one
        place the coordinator merges: workstreams INTO the epic branch, never
        the epic branch into `main`. The user's approval gate is the umbrella
        PR ([#2.6]), not the individual workstreams.

    2.4. Convergence — clearing the fallouts

        Once the initial workstreams are merged into the epic branch, the
        coordinator gathers the fallouts: follow-ups filed as GitHub issues
        during execution, plus things that surfaced while implementing. It
        opens one final workstream and assigns a subagent to clear them.

        Workstream agents deliberately do NOT side-quest every little thing
        they find — that restraint is correct — but the epic must not merge
        with a pile of decoupled follow-ups trailing behind it. Clear only what
        belongs to this epic: something that surfaced as obviously part of the
        feature -> do it now, in the convergence workstream; a
        related-but-separate feature -> leave it as a filed issue.

    2.5. Documentation pass

        When the convergence workstream merges, the coordinator delegates an
        exploration agent to find what the feature changed in the docs —
        out-of-code docs under `docs/` and docstrings, especially module-level
        ones that capture design, trade-offs, and pointers — and to make those
        changes on a dedicated PR.

    2.6. The umbrella PR

        With the work and docs in, the coordinator opens the feature's umbrella
        PR. It double-checks which issues the PR actually closes, writes a
        high-level description of the whole epic pointing to the related
        issues, and drives the PR (epic branch -> `main`) through the SAME role
        split — the coordinator waits and flips, a fresh shepherd handles each
        review round — then flips it to READY and stops. The HUMAN merges the
        umbrella PR to `main`; the coordinator does not auto-merge it.

        Changelog and release come later: shipit has no `changelog` or
        `release` / `cut` command yet — they arrive with the Workflows epic
        (see [./docs/dev/workflows.lex]). Until then there is no
        changelog-fragment step in a PR and no release phase here.

3. Branching / PR / Issue Naming

    Work is organised as epics (a large feature or change) made of work streams
    (a related set of changes that ships as one PR). An epic may span both
    repos; each repo carries its own epic branch, its own WS, and its own epic
    PR.

    Codes are assigned by the human — repo codes once per project (usually
    already set) and epic codes at epic creation — never invented by an
    implementing agent mid-stream. Agents derive WS codes and all names from
    them.

    3.1. Identifier

        Used in plain language: issue and PR titles, commit logs,
        cross-references. Form: `REPO-EPIC-WSnn[-FXnn]` — e.g.
        `APP-GPU02-WS03-FX02`.

        - Epic: a registered THEME (3 uppercase letters) + NN — e.g. `GPU02`
          (there was a GPU01). A roadmap stage, if any, is metadata in the epic
          body, not the code.
        - Repo: 3 uppercase letters — `APP` (phos-app), `COR` (phos-core). Only
          for multi-repo projects.
        - Workstream: `WSnn`, scoped per (epic, repo) — both repos may have a
          WS01 in the same epic.
        - Fix round: `FXnn`, scoped per PR, review-phase only (squashed away on
          merge) — the Nth review-response round.

    3.2. Titles

        Form: `<identifier>: Epic: <Epic Name> - Workstream: <WS Name>` — e.g.
        `APP-GPU02-WS03: Epic: GPU Rendering - Workstream: Tiling`.

    3.3. Branches

        Inside one repo, so no repo prefix; hyphen-separated; no fix round.
        Form: `EPIC-WSnn` — e.g. `GPU02-WS03`. The epic branch itself is the
        bare epic code — e.g. `GPU02`. Hyphen, not slash: a slash form
        (`GPU02/WS03`) collides with the bare epic branch in git — a ref cannot
        be both a file (`refs/heads/GPU02`) and a directory
        (`refs/heads/GPU02/WS03`), so the two branches cannot coexist.
