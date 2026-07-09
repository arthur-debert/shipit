Branching / PR / Issue Naming

This document expands [../../AGENTS.lex] §3 (the always-on summary) with the
full naming grammar: the identifier form and its THEME registry, multi-repo
prefixes, FX rounds, title and branch forms, and the slash-collision rationale.

Work is organised as epics (a large feature or change) made of work streams (a
related set of changes that ships as one PR). An epic may span both repos; each
repo carries its own epic branch, its own WS, and its own epic PR.

Codes are assigned by the human — repo codes once per project (usually already
set) and epic codes at epic creation — never invented by an implementing agent
mid-stream. Agents derive WS codes and all names from them.

1. Identifier

    Used in plain language: issue and PR titles, commit logs, cross-references.
    Form: `REPO-EPIC-WSnn[-FXnn]` — e.g. `APP-GPU02-WS03-FX02`.

    - Epic: a registered THEME (3 uppercase letters) + NN — e.g. `GPU02`
        (there was a GPU01). A roadmap stage, if any, is metadata in the epic
        body, not the code.
    - Repo: 3 uppercase letters — `APP` (phos-app), `COR` (phos-core). Only
        for multi-repo projects.
    - Workstream: `WSnn`, scoped per (epic, repo) — both repos may have a
        WS01 in the same epic.
    - Fix round: `FXnn`, scoped per PR, review-phase only (squashed away on
        merge) — the Nth review-response round.

2. Titles

    Form: `<identifier>: Epic: <Epic Name> - Workstream: <WS Name>` — e.g.
    `APP-GPU02-WS03: Epic: GPU Rendering - Workstream: Tiling`.

3. Branches

    Inside one repo, so no repo prefix; slash-namespaced; no fix round. Form:
    `EPIC/WSnn` — e.g. `GPU02/WS03`. The epic (umbrella) branch is
    `EPIC/umbrella` — e.g. `GPU02/umbrella` — NOT the bare epic code. Slashes
    group every branch of one epic under a single `EPIC/` ref directory, which
    sorts and greps cleanly. The epic branch is `EPIC/umbrella` rather than bare
    `EPIC` precisely to dodge the git ref collision: a bare `refs/heads/GPU02`
    file cannot coexist with the `refs/heads/GPU02/WS03` directory, so the
    umbrella name keeps the epic branch a sibling of its workstreams under
    `refs/heads/GPU02/`. Standalone (non-epic) work uses `issues/<id>/<session>`
    — e.g. `issues/433/work` — where `<session>` defaults to `work`. The
    `<session>` suffix is there for the SAME ref-collision reason as the epic
    umbrella name: a bare `issues/<id>` branch would occupy `refs/heads/issues/433`
    as a ref FILE, which cannot coexist with the `refs/heads/issues/433/` ref
    DIRECTORY a sibling session needs — so the suffix keeps `issues/433/` a
    directory and lets a +1 session on one issue (e.g. `issues/433/onboard`)
    coexist with the default `issues/433/work`. The
    plain-language identifier (§1) stays hyphenated — `GPU02-WS03` in titles,
    logs, cross-refs — only the git branch form is slashed.

    A coordinator session Tree is born on `ephemeral/<id>` — e.g.
    `ephemeral/sess-20260702-121314-4242` — cut from `origin/main`, where `<id>`
    is the per-launch session id (the `claude --worktree <id>` value, minted by
    `agent-start claude`). The slash groups every session branch under the `ephemeral/`
    ref directory, mirroring the Tree dir `<root>/<org>/<repo>/ephemeral/<id>`.
    Unlike every other form, this branch is NOT the work's name: a session Tree
    is ephemeral-by-path, work-by-branch (ADR-0027). The dir leaf is the SESSION
    — disposable, never renamed — while the branch mirrors it only at birth and
    is expected to move to the real work (`EPIC/umbrella`, `docs/<slug>`,
    `issues/<id>/<session>`, …) inside the fixed dir as the session discovers
    what it is doing; `shipit tree list` reads the live branch, so nothing is
    lost but the cosmetic dir-branch symmetry.

    Grandfathered: epics already in-flight when this slash scheme landed keep
    their original hyphen form (bare `EPIC` epic branch, `EPIC-WSnn` workstreams)
    — e.g. `HAR02` / `HAR02-WS03`. The slash/umbrella form applies to epics
    created after; in-flight epics are NOT retroactively renamed (see ADR-0016).
