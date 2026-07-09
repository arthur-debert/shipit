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
severity-override verb but is deliberately UNDOCUMENTED in role prompts and
docs — a dormant correction path kept warm so we can cheaply re-document it if
reviewer-emitted severities prove unreliable, not a step any agent is told to
take. The binary vocabulary could not express the new stopping rule (a round
with no major+ finding stops the loop, while `minor` findings still require
thread resolution but never mint rounds), so extending it was the same rewrite
in a compatibility costume — rejected per the no-backwards-compat principle.
The major/minor boundary is the merge-block test: would a competent reviewer
hold the merge for this? Category and confidence ride along informational-only;
Severity is the engine's sole routing key.
