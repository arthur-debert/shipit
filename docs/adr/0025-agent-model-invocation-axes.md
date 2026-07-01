# Agent axes: Backend, Model, Invocation (and Backend ⊥ Reviewer)

> **Status: Proposed.** Core Model epic (COR01). Refines ADR-0020 (backend adapter) and
> ADR-0005/0006 (reviewer funnel); anchors WS-Reviewer.

The agent layer is modeled as **orthogonal axes sharing a single identity** — `Backend`,
`Model`, `Invocation` — rather than one merged "reviewer/backend" type.

## Context

"codex"/"agy" appeared as **three parallel class hierarchies** — `ReviewerAdapter` (the PR
funnel), `BackendAdapter` (spawn/launch, ADR-0020), and a **vestigial `Backend` ABC**
(`review/backends/`, already gutted) — with ~6 identity aliases per agent and two separate
`MODEL_ALIASES` maps. The naive fix (merge into one "reviewer identity") conflates
orthogonal axes: some reviewers are **App reviewers** (copilot) with *no* backend, and
backends also serve implementer/shepherd roles. Separately, the invocation config (model,
reasoning) was about to be folded into an "AgentConfig" as if the **model belonged to the
agent** — but a model of one provider can run under a backend of another.

## Decision

- **`Backend`** — the agent harness/CLI (`claude | codex | antigravity`), a closed
  registry; owns *how-to-launch* and **one identity** (canonical name + all aliases:
  funnel login, check-run name, spawn `--backend` token, Doppler prefix) defined **once**
  and **shared with the `Reviewer` adapter**. **Backend ⊥ Reviewer** (launch axis vs
  PR-funnel axis — shared *identity*, not behaviour) and **Backend ⊥ Role**.
- **`Model`** — the LLM = `(id, provider, reasoning_capability)`, **decoupled from
  Backend**. **`Provider ∈ {anthropic, openai, google, …}`** (closed registry). Reasoning
  *capability* is intrinsic to the Model.
- **`Invocation`** — the configured launch of one **Run** = **Backend × Model ×
  ReasoningLevel** (+ `permission_mode`). **`ReasoningLevel ∈ {low, medium, high}`** is
  chosen *per-invocation* (distinct from the Model's capability), normalized so eval
  compares across backends. Backend×Model validity is a **lookup, not a structural
  constraint**. Invocation is threaded **spawn → Run → eval record** (observed config
  extracted from the transcript alongside the intended) and becomes a **group-by dimension
  for `shipit eval report`** — the data that lets the harness compare configurations.
  Distinct from **`Variant`** (the prompt/policy content-hash axis).
- The **vestigial `Backend` ABC is deleted**.

## Considered options

- **Merge `ReviewerAdapter` + `BackendAdapter` into one class** — rejected: conflates
  orthogonal axes; App reviewers have no backend.
- **Model as a property of Backend** — rejected: they are decoupled; a Claude model under
  a non-Claude backend must be expressible.
- **Fold model/reasoning into an "AgentConfig"** — rejected: implies the model belongs to
  the agent.

## Consequences

WS-Reviewer deletes the dead ABC, extracts the shared agent-backend identity (referenced
by both the funnel and launch axes), keeps the two axes distinct, and lands the single
required-reviewer default. `Model`/`Invocation` give the eval loop its config dimensions;
the config *optimizer* is deliberately future work (rich model now, build later).
