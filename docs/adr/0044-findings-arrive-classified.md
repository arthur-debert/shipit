# Findings arrive classified; the engine reads Severity directly

Review findings now carry a 4-tier Severity (`critical|major|minor|nit`) as
engine data, replacing the shepherd-recorded binary `nitpick|substantive`
verdict as the thing the Breaker and state machine read. A local-agent
reviewer emits severity in its output schema; an App reviewer's native format
is mapped to the ladder by its Reviewer adapter (adapter mechanics, per the
adapter rule); an unparseable finding defaults to `major` — fail-safe: it
forces a round rather than slipping the Breaker. Severity survives GitHub (the
threads remain the engine's only finding store) via a machine marker in the
comment body (`<!-- shipit:finding severity=… -->`), with a Conventional
Comments rendering as the human layer; precedence is marker → adapter mapping
→ `major` default → beaten only by a write-once **Severity override**.

Consequences: the CLASSIFY state is structurally unreachable (nothing can be
unclassified) and is removed; `shipit pr classify` survives as the
severity-override verb but is deliberately ABSENT from role prompts and
operator-facing guidance (decision records — this ADR and the RVW02 PRD —
still describe it) — a dormant correction path kept warm so we can cheaply
re-surface it if reviewer-emitted severities prove unreliable, not a step any
agent is told to take. The binary vocabulary could not express the new stopping rule (a round
with no major+ finding stops the loop, while `minor` findings still require
thread resolution but never mint rounds), so extending it was the same rewrite
in a compatibility costume — rejected per the no-backwards-compat principle.
The major/minor boundary is the merge-block test: would a competent reviewer
hold the merge for this? Category and confidence ride along informational-only;
Severity is the engine's sole routing key.

**Amendment (2026-07-11, #743) — the adapter unclassified-severity policy
rung.** "Every finding arrives classified" held only for reviewers with a
marker or a native vocabulary to map. Copilot has neither — no severity in its
comment bodies, none in its review/review-comment API metadata (verified
against captured REST and GraphQL payloads) — so every Copilot finding rode
the `major` fail-safe, any round containing one Copilot nit re-minted a round,
the no-major-finding Breaker could never fire on a Copilot-commenting PR, and
such loops always rode to the round cap (observed on three TOL02 PRs, ~2–4
extra rounds of pure nit churn each). The chain gains one rung: each Reviewer
adapter may declare an explicit **unclassified-severity policy** — what its
findings resolve to when neither the marker nor its native mapping decides.
Precedence is now marker → adapter mapping → adapter unclassified policy →
`major` default, still beaten only by the write-once Severity override.
Copilot's policy is `minor`: its unclassified findings are addressed and
their threads resolved before Ready like any finding, but they never mint
another round. The `major` fail-safe is unchanged for every reviewer without
an explicit policy, and an explicit or overridden `major` still forces a
round. Rejected alternatives: inferring severity from prose (an LLM
classifier at the funnel boundary — reintroduces the classification step this
ADR removed), and accepting the round cap as Copilot's de-facto breaker
(documents the waste instead of ending it).
