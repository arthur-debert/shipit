Shepherd brief template

The task-specific half of a shepherd's ROUND-1 (cold) brief (RVW02, ADR-0035).
The general half — the dev cycle and the shepherd's slice — is the role prompt
the agent-def already loads; this template carries ONLY what varies per PR. The
coordinator expands it once, at the cold brief: print it with
`shipit spawn brief shepherd`, replace every `{{slot}}` with the PR's facts,
and hand the expanded skeleton to the shepherd. A between-rounds RESUME stays
the one-line brief restating the engine's verdict for the new round — this
template does not shape resumes. Every slot is MANDATORY — never brief a
shepherd with an unfilled or dropped slot; the shepherd role prompt tells it to
flag a missing slot rather than guess around it.

Brief skeleton — fill the slots, hand over everything below:

- PR: {{pr}} — the ONE PR this shepherd owns across its whole review life, and its Context note.
- Issue: {{issue}} — the issue that PR implements; findings are judged against it, not against scope creep.
- Verify commands: {{verify-commands}} — the EXACT commands that prove a round's fixes good BEFORE the push: the manual verify is the repo's test suite (in shipit: `pixi run test`), plus every role-relevant gotcha spelled out (in shipit: the lint gate — `pixi run -e lint lint`, the same command CI runs — is exercised by the commit/push git hooks, NOT run as a separate verify step; run `shipit lint --fix` manually only when you expect formatting damage, then let the hook be the check; a `.lex` edit under `src/shipit/data/roles/` regenerates its derived surfaces via `pixi run regen-roles`, and the mirrors commit WITH the source). Name them exactly — a shepherd must never have to guess how to verify.
- Governing docs: {{governing-docs}} — the epic's governing ADR/Spec list this PR answers to; the shepherd self-checks each round's diff against each named doc BEFORE pushing the round.
- Decision boundaries: {{decision-boundaries}} — what is already decided and must NOT be re-litigated: a finding that re-opens one of these gets a rationale reply (and its classification), never a fix that unwinds the decision.
