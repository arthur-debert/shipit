---
name: shipit-to-issues
description: Break a plan, spec, or PRD into independently-grabbable Work Streams on the project issue tracker using tracer-bullet vertical slices. Use when user wants to convert a plan into issues/work streams, create implementation tickets, or break down work.
metadata:
    forked-from: https://github.com/mattpocock/skills (skills/engineering/to-issues)
---
# To Issues

Break a plan into independently-grabbable **Work Streams (WS)** using vertical slices (tracer bullets). Each Work Stream ships as one PR.

The issue tracker and triage label vocabulary should have been provided to you — run `/setup-matt-pocock-skills` if not.

## Process

### 1. Gather context

Work from whatever is already in the conversation context. If the user passes an issue reference (issue number, URL, or path) as an argument, fetch it from the issue tracker and read its full body and comments. The parent is normally the **epic issue** produced by `/shipit-to-prd`, which links to the authoritative PRD in `docs/prd/`.

### 2. Explore the codebase (optional)

If you have not already explored the codebase, do so to understand the current state of the code. Issue titles and descriptions should use the project's domain glossary vocabulary (`CONTEXT.md`), and respect ADRs in the area you're touching.

### 3. Draft the Work Streams (vertical slices)

Break the plan into **tracer bullet** Work Streams. Each WS is a thin vertical slice that cuts through ALL integration layers end-to-end, NOT a horizontal slice of one layer.

<work-stream-rules>
- Each WS delivers a narrow but COMPLETE path through every layer (schema, API, UI, tests)
- A completed WS is demoable or verifiable on its own
- Size each WS as the **thinnest *coherent, independently reviewable* PR** — prefer thin, but each WS must stand on its own as one reviewable PR, not a sub-fragment of one
- WS may touch overlapping files; do not try to make them file-disjoint (that turns slicing into an NP-hard problem). File contention is resolved at merge time, not by pre-partitioning.
- Favor making WS01 a **walking skeleton**: the thinnest end-to-end thread that proves the architecture is wired together
</work-stream-rules>

All Work Streams are AFK (implemented and merged by agents without mid-stream human interaction). The only human gates are the upstream PRD approval and the final epic→main merge.

### 4. Quiz the user

Present the proposed breakdown as a numbered list. For each WS, show:

- **Title**: short descriptive name
- **Blocked by**: which other WS (if any) must complete first
- **User stories covered**: which user stories this addresses (if the source material has them)

Ask the user:

- Does the granularity feel right? (too coarse / too fine)
- Are the dependency relationships correct?
- Should any WS be merged or split further?

Iterate until the user approves the breakdown.

### 5. Publish the Work Streams to the issue tracker

For each approved WS, publish a new issue to the issue tracker. Use the issue body template below. These are considered ready for AFK agents, so publish them with the correct triage label unless instructed otherwise.

Make each WS issue a **sub-issue of the epic issue** (improves progress tracking in the GitHub UI). Publish in dependency order (blockers first) so you can reference real issue identifiers in the "Blocked by" field.

The WS code (`WSnn`, scoped per epic+repo) is assigned here; the epic code comes from the human. Use the identifier in the title: `<REPO>-<EPIC>-<WSnn>: Epic: <Epic Name> - Workstream: <WS Name>`.

<issue-template>
## Parent

A reference to the epic issue on the issue tracker.

## What to build

A concise description of this vertical slice. Describe the end-to-end behavior, not layer-by-layer implementation.

Avoid specific file paths or code snippets — they go stale fast. Exception: if a prototype produced a snippet that encodes a decision more precisely than prose can (state machine, reducer, schema, type shape), inline it here and note briefly that it came from a prototype. Trim to the decision-rich parts — not a working demo, just the important bits.

## Acceptance criteria

- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Criterion 3

## Blocked by

- A reference to the blocking ticket (if any)

Or "None - can start immediately" if no blockers.

</issue-template>

Do NOT close or modify the parent epic issue.
