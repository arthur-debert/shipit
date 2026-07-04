# The engine is the sole requester for required reviewers

> **Status: Proposed.** Epic RVW01 (reviewer symmetry); depends on #347
> (request actuator env coupling / false success) landing first.

Every **required reviewer** is requested by the PR state engine and nothing
else — Copilot included, through its reviewer adapter off the Roster. We kill
every GitHub-side auto-request for a required reviewer: the
`copilot-review.yml` caller workflow is deleted (retired portfolio-wide via a
`shipit install` retired-files mechanism), and the account-level Copilot
auto-review setting is switched off. (An earlier draft also pinned
`automatic_copilot_code_review_enabled: false` in the managed ruleset
template, but the rulesets REST endpoint rejects that parameter outright —
422 `Unexpected parameter`, #438 — so the template omits it; a guard against
GitHub-side auto-review, if ever wanted, must be a verification read, not a
write.) No engine change is needed to take over: `CopilotAdapter.request()`
(`gh pr edit --add-reviewer @copilot`) already works and the request loop
already treats every roster entry uniformly — the workflow was the *second*
requester, not the only one.

The asymmetry was the cost: with GitHub requesting Copilot on its own schedule,
"request reviews" was special-cased per reviewer (agents burned tokens
re-deriving "is Copilot handled?"), the per-reviewer `rerun` policy was
unenforceable for exactly one reviewer, and GitHub-triggered re-reviews minted
review rounds outside the engine's round count — e.g. a push on the last of 6
rounds drawing a 7th review the breaker rules never sanctioned. One requester
means the engine's `to_request` / `rerun` / round-cap semantics are the whole
story.

Carve-out: an inherently auto-triggering reviewer (Gemini) may exist only as
**best-effort** — it is outside the request system and `build_rounds` filters
to required reviewers, so its unsolicited reviews never mint rounds. Requiring
a reviewer the engine cannot request is the anti-pattern this decision kills.

## Considered options

- **Keep the workflow, teach the engine to skip an already-requested
  Copilot** — rejected: preserves the special case in the one place we want
  uniformity, and leaves rounds mintable outside the engine.
- **Ruleset-level automatic Copilot review** (`pull_request` rule parameter) —
  rejected for the same reason, with worse visibility: repo-UI config drift
  instead of a versioned workflow file. Pinning it false in the template is
  not even possible: the rulesets REST endpoint rejects the parameter (#438).
- **Portfolio-wide manual sweep** of the caller workflows — rejected:
  removal rides `shipit install` (retired-files, pristine-sha-guarded) so
  onboarding a repo IS the cleanup, and the mechanism serves the next piece of
  release-sync debris too.

## Consequences

- PR-open no longer requests anyone. The implementer runs `shipit pr next`
  once after opening the draft PR (the PR number resolves from the current
  branch) — the first request lands with zero coordinator latency, and the
  engine still decides *what* to request.
- Copilot now obeys `rerun` like every reviewer (shipped default `false` =
  review-once). This repo opts into `copilot = { rerun = true }` in
  `.shipit.toml` to generate per-push review traffic that exercises the round
  counter and the all-nitpick breaker; expect `round-cap` breakers to fire
  sooner while that is on.
- The dev-cycle canon changes: `arthur-debert/release docs/dev-cycle.lex` and
  the operator's global `~/.claude/CLAUDE.md` must drop the
  "Copilot fires at PR `opened` via workflow" fact when this lands.
