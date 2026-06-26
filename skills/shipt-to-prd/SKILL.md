---
name: shipt-to-prd
description: Turn the current conversation context into a PRD, write it to docs/prd/, and open an epic tracker issue that points to it. Use when user wants to create a PRD from the current context.
metadata:
    forked-from: https://github.com/mattpocock/skills (skills/engineering/to-prd)
---
This skill takes the current conversation context and codebase understanding and produces a PRD. Do NOT interview the user — just synthesize what you already know. (The interview happens earlier, in `/shipt-grill-with-docs`.)

The issue tracker and triage label vocabulary should have been provided to you — run `/setup-matt-pocock-skills` if not.

## Process

1. Explore the repo to understand the current state of the codebase, if you haven't already. Use the project's domain glossary vocabulary (`CONTEXT.md`) throughout the PRD, and respect any ADRs in the area you're touching.

2. Sketch out the major modules you will need to build or modify to complete the implementation. Actively look for opportunities to extract deep modules that can be tested in isolation.

A deep module (as opposed to a shallow module) is one which encapsulates a lot of functionality in a simple, testable interface which rarely changes.

Check with the user that these modules match their expectations. Check with the user which modules they want tests written for.

3. Write the PRD using the template below. **The authoritative PRD is a file**, not an issue body:

   - Write it to `docs/prd/<epic-slug>.md`. This file is the single source of truth for the spec.
   - Then open the **epic tracker issue**. The issue does NOT embed the full PRD — it links to the `docs/prd/` file and carries a short summary plus the execution topology (which Work Streams are parallelizable / their dependencies — this is execution detail that belongs in the issue, not the spec). Apply the `ready-for-agent` triage label — no need for additional triage.
   - The epic code (`THEME+NN`, e.g. `GPU02`) is assigned by the human at epic creation. Use it in the issue title: `<REPO>-<EPIC>: Epic: <Epic Name>`.

<prd-template>

## Problem Statement

The problem that the user is facing, from the user's perspective.

## Solution

The solution to the problem, from the user's perspective.

## User Stories

A LONG, numbered list of user stories. Each user story should be in the format of:

1. As an <actor>, I want a <feature>, so that <benefit>

<user-story-example>
1. As a mobile bank customer, I want to see balance on my accounts, so that I can make better informed decisions about my spending
</user-story-example>

This list of user stories should be extremely extensive and cover all aspects of the feature.

## Implementation Decisions

A list of implementation decisions that were made. This can include:

- The modules that will be built/modified
- The interfaces of those modules that will be modified
- Technical clarifications from the developer
- Architectural decisions
- Schema changes
- API contracts
- Specific interactions

Do NOT include specific file paths or code snippets. They may end up being outdated very quickly.

Exception: if a prototype produced a snippet that encodes a decision more precisely than prose can (state machine, reducer, schema, type shape), inline it within the relevant decision and note briefly that it came from a prototype. Trim to the decision-rich parts — not a working demo, just the important bits.

## Testing Decisions

A list of testing decisions that were made. Include:

- A description of what makes a good test (only test external behavior, not implementation details)
- Which modules will be tested
- Prior art for the tests (i.e. similar types of tests in the codebase)

## Out of Scope

A description of the things that are out of scope for this PRD.

## Further Notes

Any further notes about the feature.

</prd-template>
