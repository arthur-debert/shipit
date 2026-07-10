# OBS02 — Review funnel

> Epic: **OBS02** · Status: planned · Plan: `docs/legacy-prd/FUTURE_WORK.md`
> ADR: `docs/adr/0005-local-review-funnel-via-check-runs.md`
> Glossary: `CONTEXT.md` (Review funnel, Holds / Settled, Local-agent reviewer)

## Problem Statement

A **local-agent reviewer** (`codex-local`, `agy-local`) has **no native pre-post
signal** on the PR. GitHub refuses to assign a custom App bot as a requested
reviewer — verified empirically on a canary PR: GraphQL `requestReviews(userIds:…)`
rejects Bot nodes (there is no `botIds` input, the `BOT_…` id `NOT_FOUND`s);
`gh pr edit --add-reviewer adr-codex-review` doesn't resolve the login; the REST
`POST …/requested_reviewers` returns 200 but is a **silent no-op**
(`reviewRequests` stays empty); and `suggestedActors(CAN_BE_ASSIGNED)` omits them
entirely.

So until a local review actually **posts**, there is no record on the PR that it
was ever requested. A review that **failed**, came back **empty**, or **timed
out** is indistinguishable from "never requested" — the PR parks silently with no
signal to a human or to an agent about why. That silent-park is exactly the
failure mode the observability spine exists to kill: a local reviewer's **review
funnel** has a visible *posted* terminal but no visible *requested* or *in-flight*
or *failed* state.

The **App reviewers** (Copilot) do not have this gap — they ride a native
`review_requested` edge and a native review object, so their funnel is already
readable from GitHub. The hole is local-agent-shaped.

## Solution

Per **ADR-0005**, give the local-review funnel a **native, timestamped
stand-in for the `review_requested` edge GitHub denies these bots**: a **GitHub
Check Run authored by the reviewer's own App**.

- **On kickoff**, shipit creates a check run named `review: <reviewer>` with
  `status=in_progress` and `started_at=now`, authored by the reviewer's App
  (`adr-codex-review[bot]` / `adr-agy-review[bot]`). This is the *requested /
  in-flight* breadcrumb that previously did not exist.
- **On completion**, shipit transitions that same run to its terminal conclusion:
  `completed/success` (alongside the structured review the reviewer posts —
  *including* a clean zero-findings review), `completed/failure` for a **failed**
  run (agent errored) or an **empty** one (no parseable review — the agy mode —
  treated as degraded, NOT success), or `completed/timed_out` — each carrying an
  `output` message. (`completed/neutral` is an acceptable alternative for *empty*;
  the load-bearing point is it is not `success`.)
- The funnel check run is **non-required**: it is *visible but never blocks*. A
  failed local review must be *seen*, not *hold* — the Ready pillar is "every
  required reviewer **settled** (outcome recorded) + threads resolved", not "every
  review **succeeded**".

**Gap-fill only.** This adds *nothing* to the App-reviewer path: Copilot keeps its
native `review_requested` edge and its review object. OBS02 only supplies the
missing native breadcrumb for the bots that lack one. The engine then sees one
**review funnel** across both reviewer kinds — native-edge inputs for app
reviewers, check-run inputs for local-agent reviewers. **That normalization into a
single funnel view, and the readiness change that consumes it, is OBS04's job** — OBS02
only *produces* the check-run breadcrumb; it does not change how the engine reads
or holds Ready.

**The structured-review POST already exists and stays unchanged.**
`src/shipit/review/post.py` (`build_review_payload` / `post_review`) already posts
the local review AS the bot through the official Reviews API — byte-for-byte the
shape Copilot produces — and `_LocalReviewAdapter` (`src/shipit/prstate/reviewers.py`,
`has_requested_edge = False`) already detects that posted review. OBS02 wraps the
**check-run lifecycle** *around* that existing post; it does not touch the post
itself.

## Prerequisite — `checks:write` re-grant (call out prominently)

The review Apps **cannot create check runs today.** Their granted permission set
is only `contents:read`, `metadata:read`, `pull_requests:write` — verified on the
codex App: a check-run create returns `403 Resource not accessible by
integration`. Creating check runs needs the **`checks:write`** scope, and adding
a permission scope to a GitHub App requires the **install owner to re-consent**
(re-authorize the install). That is a **manual owner action per App per owner** —
shipit cannot automate it (same class as the App install itself), and it is
**folded into the INS01 local-reviewer rollout (issue #26)**.

Consequence for this epic: until the re-grant lands, the local review **still
posts** (that path is unaffected by the missing scope) — only the funnel check run
cannot be created. So **OBS02's end-to-end verification is blocked by the
re-grant**; the code lands first, the live funnel turns on once the owner
re-authorizes.

## User Stories

1. As an agent driving a PR, I want to see whether a local review has been
   *requested / is in-flight* — not just whether it eventually posted — so that I
   know the review loop actually started and isn't silently absent.
2. As an agent, when a local review **fails / comes back empty / times out**, I
   want that outcome visible on the PR, so that I act on the real terminal state
   instead of waiting forever on a signal that will never arrive.
3. As a maintainer, I want a failed local review to be **visible, not silent**, so
   that a broken reviewer surfaces as *degraded* on the PR rather than parking it
   with no explanation.
4. As a maintainer, I want the funnel breadcrumb to be **non-blocking**, so that a
   failed or empty local review is seen but never holds the PR from Ready (Ready is
   *settled*, not *succeeded*).
5. As an agent, I want the local funnel to read the same way the app-reviewer
   funnel does, so that I reason about *requested → in-flight → posted / failed /
   empty / timed-out* uniformly across reviewer kinds (the normalization itself is
   OBS04).

## Implementation Decisions

### Check-run lifecycle as the funnel stand-in (ADR-0005)

- The funnel rides on **check runs authored by the reviewer's App**, created and
  transitioned via the App **installation token** — the existing
  `src/shipit/review/ghauth.py` path (Doppler-sourced PEM → in-memory RS256 JWT →
  installation token), the same auth `post_review(as_app=True)` already uses. The
  PEM never lands on disk.
- A check run is created at **kickoff** (`status=in_progress`, `started_at=now`)
  and **transitioned** at completion to a terminal `conclusion`. The same run
  carries the state through its whole life; shipit does not create a second run.
- **Funnel stages → check-run state** (the mapping ADR-0005 fixes):

  ```text
  requested / in-flight                        → status=in_progress, started_at=now
  posted (success, incl. clean zero-findings)  → completed / success   (+ the structured review POST)
  failed (agent errored)                       → completed / failure    + output message
  empty (no parseable review — agy mode)       → completed / failure    + output "empty" (degraded, NOT success)
  timed-out                                    → completed / timed_out  + output message
  ```

- The run is **non-required** — visible on the PR but never a required check, so it
  never blocks merge. (Cite ADR-0005 "Consequences"; not re-argued here.)
- **Timestamps are the load-bearing output.** `started_at` (and `completed_at`)
  are what **OBS04's wait window** ages against. OBS02 only *writes* correct,
  honest timestamps; the engine that *reads* them and applies the window stays
  stateless ("now" is its input) and is OBS04's concern.

### Boundary

- Check-run create/transition is a thin call through the App-token boundary, sited
  next to / alongside the existing `review/` post path so the kickoff that creates
  the `in_progress` run and the completion that posts the review + closes the run
  are the same flow.
- OBS02 changes **only** the *write* side (producing the breadcrumb). It does **not**
  change `prstate` reading, normalization, or the Ready pillars — those are OBS04.

## Work Streams (hint)

Execution topology (Work Streams + dependency waves) lives on the OBS02 epic
issue, not here. The shape:

- **WS — kickoff create.** Create the `in_progress` `review: <reviewer>` check run
  with `started_at=now`, authored via the App installation token, at local-review
  kickoff.
- **WS — terminal transition.** Transition the run to `success` (with the existing
  review POST, incl. a clean zero-findings review) / `failure` (a failed *or* empty
  run) / `timed_out`, with an `output` message and `completed_at`, on completion or
  failure.
- **WS — `checks:write` provisioning + verification harness.** Document the scope
  add + owner re-consent (folded into INS01 / #26) and a verification harness that
  exercises the full lifecycle on a canary PR. **Depends on the re-grant** for its
  end-to-end pass.

## Testing Decisions

A good test asserts the **breadcrumb shipit writes**, with the App-token boundary
faked — never live GitHub.

- The kickoff create emits a check run with the **right name** (`review: <reviewer>`),
  **status** (`in_progress`), and **`started_at`** timestamp.
- Each terminal transition produces the **right `status`/`conclusion`** and
  `output` message: posted→success, failed→failure, empty→success/neutral,
  timed-out→timed_out, each with `completed_at`.
- The run is created **non-required** (it does not appear as a required check / does
  not block merge).
- The existing structured-review POST (`post.py`) still fires unchanged on the
  success path — OBS02 wraps it, it does not replace it.
- No test re-asserts engine reading / normalization / readiness holds (that is OBS04).

## Out of Scope

- **Reading / normalizing the funnel and the Ready-pillar change — OBS04.** OBS02
  only *produces* the check-run breadcrumb; the engine consuming both native-edge
  and check-run inputs into one funnel view, the wait window, and the
  "requested + outcome-recorded + threads-resolved" readiness pillar are OBS04.
- **Async local execution — OBS03.** OBS02 wraps whatever execution model is in
  place; the fire-and-forget detached run is OBS03.
- **The actual App permission re-grant — INS01 / #26.** Adding `checks:write` and
  re-authorizing each install is a manual owner action; OBS02 documents and
  depends on it but does not perform it.
- **The structured-review POST itself.** Already shipped in `src/shipit/review/post.py`;
  unchanged here.

## Depends on

- **OBS01** (logging foundation) — the funnel writes log through the OBS01 sink.
