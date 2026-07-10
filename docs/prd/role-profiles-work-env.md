# Role Profiles and Work Env Foundation

## Problem Statement

Shipit has a clear agentic development model, but several core execution concepts are still spread across prompts, hooks, spawn logic, Tree handling, tool execution, and review flow. That makes the system harder to reason about and easier to accidentally fork: a Role can be described one way in the prompt, enforced another way in a hook, and routed differently by spawn.

The immediate problem is that Shipit needs a stable foundation for its broader refactor: Role behavior must be authoritative in one place, and the execution context for Runs, Tools, Lanes, fleet verification, and CI must have a shared noun. Without that foundation, later work on Tree command execution, review-round records, PR-engine structured reasons, coordinator host adapters, and ADP02 workflow cleanup will keep rediscovering the same boundaries.

## Solution

Introduce **Role Profile**, **Tree Profile**, and **Work Env** as the foundation vocabulary and implementation shape.

The **Role Profile** registry is the fixed Shipit-owned registry describing the structural shape of each Role: Tree Profile, mutation rights, brief surface, generated prompt surface, and harness enforcement. It is authoritative: spawn, prompt generation, and enforcement read it rather than re-stating role shape locally.

A **Tree Profile** describes the execution shape a Role receives: `session`, `write`, `read-only`, or `ambient`. A Role Profile selects a Tree Profile, and the Tree Profile determines the shape of the Work Env.

A **Work Env** is the execution context Shipit uses to do work: a checkout plus the activated tools and paths for that checkout. Work Env is inferred from execution context, not consumer-configured. The first implementation focuses on Role Profiles and the Work Env resolution vocabulary; it does not attempt the full architecture refactor.

## User Stories

1. As a maintainer, I want each Role’s structural shape declared once, so that prompt generation, spawn, and enforcement cannot drift.
2. As a maintainer, I want Role Profiles to be fixed by Shipit, so that consumer repos cannot weaken core execution guarantees before the model is battle-hardened.
3. As a coordinator, I want implementers and shepherds to consistently receive write Tree-backed Work Envs, so that they can build, test, commit, and open PRs without touching another checkout.
4. As a coordinator, I want reviewers to consistently receive Read-only Tree-backed Work Envs, so that review happens against the correct branch without mutating the reviewed source.
5. As a coordinator, I want explorers to remain ambient, so that read-only investigation does not provision unnecessary Trees.
6. As a reviewer, I want my reviewed source of truth to remain a Read-only Tree, so that review output cannot accidentally mutate the PR under review.
7. As a future reviewer workflow, I want room for Review proposals, so that reviewers may eventually produce candidate diffs or stacked PRs without becoming implementers.
8. As a shepherd, I want Review proposals to be candidates only, so that I remain responsible for deciding whether and how to incorporate them.
9. As a maintainer, I want Work Env to cover Runs, Lanes, fleet verification, and CI execution, so that “where work runs” is described consistently across Shipit.
10. As a maintainer, I want Tools and Lanes to keep naming what runs, while Work Env names where it runs, so that ADP02 execution language stays precise.
11. As a CI maintainer, I want a CI checkout to be understood as a Direct-checkout Work Env, so that CI does not need Tree-specific language.
12. As a fleet operator, I want fleet verification cells to execute in Work Envs, so that local adoption evidence and CI adoption evidence use the same execution vocabulary.
13. As a developer, I want Tree-backed Work Env and Direct-checkout Work Env to be distinct, so that a Shipit-provisioned Tree is not confused with a human or CI checkout.
14. As a maintainer, I want Role Profile to sit above Role definition, so that structural enforcement and role prose can evolve independently.
15. As a maintainer, I want role prose to remain in Role definitions, so that prompt text does not become mixed into structural execution configuration.
16. As a maintainer, I want Role Profiles to point at brief surfaces, so that `spawn brief` derives availability from the same source as spawn and prompt generation.
17. As a maintainer, I want unknown or unsupported Roles to fail clearly at the Role Profile boundary, so that accidental stringly-typed roles do not reach backend launch.
18. As a maintainer, I want reviewer/read-only behavior to come from Role Profile, so that `reviewer` is no longer a hidden special case in spawn.
19. As a maintainer, I want Work Env inference to be non-configurable for now, so that consumers cannot create invalid execution combinations.
20. As a future maintainer, I want the rejected configurability decision recorded, so that “make it per-repo configurable” is not reintroduced without evidence.
21. As a maintainer, I want this PRD to stop at the foundation, so that the broader architecture refactor can be planned in focused follow-on work.

## Implementation Decisions

- **The Role Profile registry is fixed and Shipit-owned.** It is not consumer configuration. The registry describes each known Role’s Tree Profile, mutation rights, brief surface, generated prompt surface, and harness enforcement.

- **Role Profile sits above Role definition.** Role Profile owns structural shape and references behavioral text sources. Role definitions remain the source of prompt prose.

- **Tree Profile is a closed Role Profile value.** The initial values are `session`, `write`, `read-only`, and `ambient`.

- **Work Env is inferred, not configured.** Role Profiles, Trees, Direct checkouts, CI, and fleet runners determine Work Env. `.shipit.toml` continues to describe project shape and policy, not execution-model overrides.

- **Work Env has two checkout relationships.** A Tree-backed Work Env uses a Shipit-provisioned Tree. A Direct-checkout Work Env uses an existing `WorkingDir` that Shipit did not provision as a Tree, such as a human local checkout or CI checkout.

- **Spawn consumes Role Profile.** `spawn subagent` should derive write versus read-only versus ambient behavior from Role Profile rather than checking literal role names in multiple places.

- **Prompt and brief generation consume Role Profile.** Generated role surfaces and brief availability should come from the same Role Profile registry, while prose remains in Role definitions.

- **Harness enforcement consumes Role Profile.** Coordinator mutation guards and future role-specific enforcement should read Role Profile instead of duplicating role semantics.

- **Tools and Lanes use Work Env as substrate language.** Tool and Lane remain user-facing nouns for what runs. Work Env names where the execution happens.

- **Review proposals are future optionality.** Reviewer Runs may later produce Review proposals, but those proposals do not land themselves. The reviewed source remains the Read-only Tree, and the Shepherd decides whether and how to incorporate proposed changes.

- **The broader refactor is not included in this PRD.** This PRD establishes the Role Profile and Work Env foundation. It does not implement the full execution runner, review-round record overhaul, PR-engine structured reasons, coordinator host adapter, or ADP02 workflow factoring.

## Testing Decisions

- Tests should assert behavior at module interfaces, not implementation details. Role Profile tests should verify the registry’s externally visible mappings and invariants.

- Role Profile registry tests should cover every known Role and verify that each has exactly one Role Profile.

- Tree Profile tests should verify the expected mapping: coordinator to `session`, implementer and shepherd to `write`, reviewer to `read-only`, explorer to `ambient`.

- Spawn tests should verify that Role Profile drives Tree selection: implementer and shepherd get write Trees, reviewer gets a Read-only Tree, and explorer does not accidentally mint a write Tree.

- Prompt and brief tests should verify that generated surfaces and brief availability derive from Role Profile metadata while role prose still comes from Role definitions.

- Harness policy tests should verify coordinator mutation denial still works and that enforcement uses Role Profile semantics.

- Work Env resolver tests should cover Tree-backed and Direct-checkout inference for spawned Runs, Session Trees, local checkouts, CI checkouts, and fleet verification cells.

- Tool and Lane tests should verify existing behavior remains unchanged while execution can be described in terms of Work Env.

## Out of Scope

- Per-consumer Role Profile or Work Env configuration.
- Full Tree command runner or environment materialization refactor.
- Moving pixi install, node frozen install, hook activation, env scrub, or command execution behind a new runner interface.
- Review-round record implementation beyond reserving Review proposals in the language.
- PR-engine structured reason codes.
- Coordinator host adapter for Claude, Codex, or future hosts.
- Factoring `fleetsweep`, required-check discovery, workflow readers, or ADP02 workflow execution.
- Implementing Proposal Work Env.
- Letting reviewer-generated Review proposals land changes directly.

## Further Notes

This PRD is phase 1 of the broader refactor vision. The full vision still includes:

- A Work Env runner or Tree command runner that centralizes pixi install, node frozen install, hook activation, env scrub, and “run through pixi if provisioned.”
- A first-class review-round data model for RVW02: findings, dimensions, calibrator output, severity, variant, run id, and optional Review proposals.
- Structured PR-engine reason codes so lifecycle decisions and human-facing prose are separated.
- Coordinator host adapters, parallel to spawn backend adapters, for launch argv, liveness, hook strategy, environment handling, and session Tree behavior.
- ADP02 tool/workflow factoring after the CI adoption machinery settles: fleet sweep, checks, workflow readers, and workflow routing should be factored around stable Tool, Lane, Artifact, and Work Env nouns.

Relevant ADRs:

- ADR-0046: Review proposals do not land themselves.
- ADR-0047: Role Profiles and Work Env are Shipit-owned, not consumer-configured.
