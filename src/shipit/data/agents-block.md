## Development workflow (managed by shipit)

<!-- shipit-managed; edit the surrounding AGENTS.md, not this block — `shipit install` regenerates it. -->

Every change ships as an agent-driven PR. The shipit **PR engine is authoritative**:
it reads where a PR stands and emits the **single next action**. Don't carry the policy
(reviewers, waits, breakers) in your head — run the tool and do what it returns.

**Planning a new feature/epic?** Run `/shipit-planning` first — it walks overview → ADRs → PRD → issues, checking in with you at the overview and the docs PR.

### Commands

```text
pixi run lint     # the commit/push checks — multi-language, hard fail, never skips (CI runs the same)
pixi run test     # the test suite (a commit/push check)
shipit pr status  # where the PR stands + the next action (read-only)
shipit pr next    # DO the next action, then report — the verb you loop on
shipit pr ready   # guarded flip draft→ready (refuses early); --undo reverts
```

PR number is optional (resolves the current branch's PR). Also: `shipit pr review
request`; setup/ops `shipit gh-setup` / `verify-apps` / `install` / `lint` / `logs`.

### The cycle: draft → address reviews → checks passing + mergeable → flip to ready

Open every change as a **DRAFT** PR. Loop `shipit pr next` — do the one thing it returns
(request a review, address threads, wait for CI) — until it reports **READY** and flips
draft→ready. **Stop at the flip**: the human verifies + merges; never auto-merge. A human
"changes needed" returns it to draft (`shipit pr ready --undo`); re-loop.

**Floor / ceiling:** committing, pushing, and opening the draft need no go-ahead; the
**only** step needing a human is the merge.

**Large work (epics):** the same cycle runs per workstream, but each workstream PR targets
the **epic branch**, not `main`. Subagents drive their WS PR to READY; the **coordinator
merges each READY WS PR into the epic branch** on its own authority — no human checkpoint for
intra-epic merges. The human checkpoint is the **umbrella PR** (epic branch → `main`), which the
coordinator shepherds to READY, then stops for the human to merge.

### Roles — always delegated, split so no one context carries the whole cycle

- **Coordinator** (the agent the human addresses): never implements. Delegates the work;
  owns every wait and the flip; spawns a fresh shepherd per review round; in an epic, merges
  READY workstream PRs into the epic branch.
- **Implementer** (subagent): implements + tests, gets the tests green (`pixi run test`;
  the commit/push hooks run the lint suite), opens the DRAFT PR with a `## Context` handoff
  note (why this approach, what's out of scope), then **stops at PR-open** — never handles
  a review round.
- **Shepherd** (fresh subagent, one per round): triages open threads — the local agent has
  the final word, so fix-or-pushback and resolve each — pushes the round's commits at once,
  hands back.

### Naming & references

Codes are **assigned by the human**, never invented mid-stream. Implementers use them in:

- **PR title** — epic work: `<identifier>: Epic: <Epic Name> - Workstream: <WS Name>`
  (e.g. `APP-GPU02-WS03: …`); a standalone PR: a plain summary.
- **Commit messages** — reference the GitHub issue (`#123`).
- **PR body** — `closes #123` (auto-closes the issue on merge to `main`) or `for #123`
  when it must not auto-close (e.g. a workstream PR landing on an epic branch).
