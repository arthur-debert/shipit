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

    Inside one repo, so no repo prefix; hyphen-separated; no fix round. Form:
    `EPIC-WSnn` — e.g. `GPU02-WS03`. The epic branch itself is the bare epic code
    — e.g. `GPU02`. Hyphen, not slash: a slash form (`GPU02/WS03`) collides with
    the bare epic branch in git — a ref cannot be both a file
    (`refs/heads/GPU02`) and a directory (`refs/heads/GPU02/WS03`), so the two
    branches cannot coexist.
