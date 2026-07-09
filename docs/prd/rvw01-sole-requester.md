# RVW01: Reviewer symmetry — the engine as sole requester

> Spec for epic RVW01. Decision record: ADR-0031 (the engine is the sole
> requester for required reviewers). Depends on #347 landing first.

## Problem Statement

Requesting reviews is asymmetric: Copilot is auto-requested by GitHub (an
Actions workflow at PR `opened`) while every other required reviewer is
requested by the PR state engine off the Roster. Every agent driving a PR has
to re-derive the special case — "is Copilot already handled? do I request it?
will it re-review this push?" — burning tokens and time on ambiguity the
tooling should have removed. Worse, a requester the engine doesn't control
produces review rounds the engine can't count: the per-reviewer `rerun` policy
is unenforceable for exactly one reviewer, and a push on the last sanctioned
round can draw a review the breaker rules never authorized. The confusion is
structural, not a prompting problem — as long as two requesters exist, the
"request reviews" step cannot be described simply.

## Solution

One requester. Every required reviewer — Copilot included — is requested by
the PR state engine through its reviewer adapter off the Roster (the **sole
requester** rule, ADR-0031). Every GitHub-side auto-request for a required
reviewer is switched off and kept off: the Copilot caller workflow is deleted
and its removal carried across the portfolio by a new retired-files mechanism
in `shipit install`; the managed branch-protection ruleset template is amended
to pin automatic Copilot review off (today it omits the parameter), so
re-running `shipit gh-setup` erases any hand-enabled drift; the operator
switches off the account-level auto-review setting once. Copilot then obeys the same Roster policy as everyone (`rerun`,
wait window, rounds), and "request reviews" becomes one uniform loop with no
per-reviewer folklore. The first request after PR-open moves to the
implementer, who runs the engine's next-action verb once before handing back —
no latency, no new request path.

## User Stories

1. As a coordinator agent, I want every required reviewer requested through
   the same engine verb, so that I never spend context deriving which
   reviewers are "already handled" by GitHub-side configuration.
2. As a coordinator agent, I want the engine's round count to include every
   review that actually happens, so that the round-cap and all-nitpick
   breakers describe reality rather than a subset of it.
3. As a shepherd agent, I want re-requests after a push to follow only the
   Roster's `rerun` policy, so that "push the round's commits and re-request
   if the engine says to" is the complete rule with no Copilot exception.
4. As a shepherd agent, I want no surprise reviews arriving on a head after
   the breaker rules declared the PR done, so that I stop when the engine says
   stop.
5. As an implementer agent, I want to place the initial review requests by
   running the engine's next-action verb once after opening the draft PR, so
   that reviews are in-flight before I hand back and no coordinator heartbeat
   is spent on the first request.
6. As an implementer agent, I want the first-request step to be an engine
   verb rather than per-reviewer commands, so that my role prompt stays one
   line and never enumerates reviewers.
7. As an operator, I want Copilot's re-review behavior to be a visible,
   per-repo Roster flag instead of invisible GitHub configuration, so that I
   can flip one config line to change it and read the config to know it.
8. As an operator, I want this repo's Copilot to be set head-strict
   (`rerun = true`) for now, so that every push generates review traffic that
   exercises the round counter and the all-nitpick breaker while those rules
   are being tuned.
9. As an operator, I want `shipit gh-setup` to keep automatic Copilot review
   pinned off in the branch ruleset, so that a hand-edit in the GitHub UI
   cannot silently reintroduce a second requester.
10. As an operator, I want repos to shed the retired Copilot workflow when
    they run `shipit install`, so that rolling the portfolio over is
    onboarding, not a manual sweep.
11. As an operator, I want the retired-files pass to refuse to delete a file
    whose content differs from every known pristine version, so that a local
    modification is surfaced as a warning instead of destroyed.
12. As a portfolio repo maintainer, I want the retired-files manifest to be a
    general mechanism, so that the next piece of release-sync debris has a
    disposal path without new machinery.
13. As a future contributor, I want an ADR explaining why this repo has no
    Copilot review workflow, so that I don't "fix" the absence by adding one
    back.
14. As a future contributor, I want the glossary to define the sole-requester
    rule and its best-effort carve-out, so that "why is Gemini allowed to
    auto-trigger but Copilot isn't?" has a one-paragraph answer.
15. As a reviewer-adapter author, I want "required implies engine-requestable"
    to be an explicit rule, so that I never ship an adapter that a Roster can
    require but the engine cannot request.
16. As an agent reading `pr status`, I want reviewer lifecycle states that are
    fully explained by engine actions, so that a `not_requested` Copilot means
    exactly what it says rather than "GitHub may have plans."
17. As the operator of the eval/telemetry loop, I want review requests to flow
    through one instrumented path, so that the upcoming dev-cycle event log
    can emit `review.requested` events from a single seam.
18. As an operator, I want the dev-cycle canon (the portfolio dev-cycle doc
    and my global agent instructions) updated in the same epic, so that no
    session is re-armed with the dead "Copilot fires at `opened`" fact.

## Implementation Decisions

- **ADR-0031 governs.** Every required reviewer is requested by the PR state
  engine and nothing else. An inherently auto-triggering reviewer (Gemini) may
  exist only as best-effort; rounds are already computed from required
  reviewers only, so its unsolicited reviews never mint rounds. Requiring a
  reviewer the engine cannot request is the killed anti-pattern.
- **No engine request-path change.** The Copilot adapter is already fully
  requestable and the request service already loops the Roster uniformly; the
  workflow was a *second* requester. The engine work in this epic is a
  regression pin, not a behavior change.
- **Three triggers die.** (1) The Copilot caller workflow is deleted from this
  repo and listed as retired. (2) The managed branch-protection ruleset
  template explicitly sets automatic Copilot code review to disabled — since
  `gh-setup` PUTs the full template over the live ruleset, re-running it is
  the enforcement. (3) The operator switches off the account-level Copilot
  auto-review setting (manual, one-time, outside the codebase).
- **Retired files is the one new module.** A packaged manifest of paths that
  must not exist, each with the set of known pristine content hashes. A pure
  decision core maps (file present?, actual hash, known hashes) to one of:
  delete (pristine match), warn-and-keep (modified — never destroy local
  edits), no-op (absent). A thin IO pass inside `shipit install` applies the
  decisions and reports them alongside the managed-file results. The manifest
  is data, so retiring the next file is an entry, not code.
- **Roster policy, not code, sets Copilot's cadence.** The shipped default
  stays `rerun = false` (review-once). This repo's config opts Copilot into
  `rerun = true` (head-strict) to generate per-push rounds for tuning the
  breaker rules; dialing it back later is a one-line config change.
  *(Superseded by ADR-0043 / RVW02: the default flips to `rerun = true` when
  the RVW02 incremental-round work lands; review-once becomes the explicit
  opt-out.)*
- **First request moves to the implementer.** The implementer role's final
  step after opening the draft PR is to run the engine's next-action verb
  once, then stop. The engine still decides what to request; the implementer
  only supplies the trigger at a moment it is guaranteed correct. The
  coordinator's wait loop is unchanged.
- **Rollout is mechanism-carried.** This repo cuts over in one PR (workflow
  deletion + template pin + roster flag + role prompt). Portfolio repos
  receive the same cutover by running `shipit install` (retired files) and
  `shipit gh-setup` (ruleset pin) — no coordinated sweep, no transition
  period, no fallback requester.
- **Ordering constraint.** #347 (the request actuator's env coupling and
  false-success reporting) must land first: once the engine is the only
  requester, a request that silently fails is a PR parked forever.

## Testing Decisions

- Tests assert external behavior — decision outcomes, payload contents,
  engine-reported state — never internals or rendered whitespace.
- **Retired-files decision core**: full matrix — absent path; pristine match
  (delete); modified content (warn, keep); multiple known hashes (any match
  deletes). Prior art: the install verb's existing managed-manifest tests and
  the house pattern of pure-core tests with a thin IO shell.
- **Ruleset payload**: the template carries the automatic-Copilot-review
  parameter disabled, and the payload builder preserves it when injecting
  required checks. Prior art: the existing pure ruleset-payload tests in the
  gh-setup suite.
- **Engine regression pin**: a snapshot with a required, never-requested
  Copilot yields Copilot in the engine's to-request set; with `rerun = true`
  and a review on a stale head, it yields a re-request. Prior art: the
  prstate engine's snapshot-to-state tests.
- Config (roster flag), data deletion (workflow file), and role prompt edits
  carry no new suites — the roster parser, CI, and the existing generated-
  prompt checks cover them.

## Out of Scope

- The coordinator outer loop (#343 gaps 1–4: epic-scoped sweep, heartbeat,
  shepherd-spawn verb, detached-reviewer liveness) — its own workstream.
- The dev-cycle event log epic (domain-key extension, event vocabulary,
  log filters) — planned separately; this epic only leaves it one clean
  request seam to instrument.
- Next-action re-ranking under failing checks (#352) and the configurable
  round cap (#350) — standalone issues already filed.
- Any change to CodeRabbit (already symmetric: requestable App via the same
  path) or Gemini (stays best-effort, auto-triggering, round-invisible).
- Tuning the nitpick marker list or breaker semantics — this epic only feeds
  them more traffic.
- Removing the reusable workflow from the portfolio's shared workflow repo —
  it stays until the last caller repo has cut over.

## Further Notes

- Evidence that motivated the shape: the live branch ruleset carries no
  Copilot auto-review parameter (the workflow was the actual requester), and
  on a recent three-round PR Copilot reviewed exactly once — so "Copilot
  re-reviews every push" was already folklore here. The epic replaces
  folklore with config.
- The dev-cycle canon updates (the portfolio dev-cycle doc and the operator's
  global agent instructions) are deliverables of this epic even though they
  live outside this repo: both currently state the at-`opened` workflow canon
  and the "Copilot needs nothing requested" special case.
- With this repo's `rerun = true`, expect round-cap breakers to fire sooner —
  that is the intended test signal, not a regression.
