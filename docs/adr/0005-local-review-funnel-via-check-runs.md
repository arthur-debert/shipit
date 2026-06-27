# Local-review funnel via App-authored check runs

shipit's local reviewers (`codex-local`, `agy-local`) post **structured reviews**
AS their GitHub Apps (`adr-codex-review[bot]` / `adr-agy-review[bot]`), through the
official Reviews API — byte-for-byte the same shape Copilot produces. But GitHub
gives those App identities **no native pre-post signal**: a custom App bot cannot
be assigned as a requested reviewer. Verified empirically (on a throwaway canary
PR):

- GraphQL `requestReviews(userIds:[…])` rejects Bot nodes (there is no `botIds`
  input) — `NOT_FOUND` on the bot's `BOT_…` id;
- `gh pr edit --add-reviewer adr-codex-review` — the login does not resolve;
- REST `POST …/requested_reviewers` — HTTP 200 but a **silent no-op**
  (`reviewRequests` stays empty);
- `suggestedActors(CAN_BE_ASSIGNED)` does not list them at all.

So until a local review *posts*, there is no record it was ever requested — a
failed, empty, or timed-out local review is indistinguishable from "never
requested," and the PR parks silently with no signal to a human or to an agent
(the failure mode the whole observability spine exists to kill).

## Decision

The local-review **funnel** — requested → in-flight → posted (success) /
failed / empty / timed-out — rides on **GitHub Check Runs authored by the
reviewer's App**. On kickoff shipit creates a check run (`status=in_progress`,
`started_at=now`); on completion it transitions the run to a terminal conclusion,
mapping the funnel outcome:

- **posted** (a structured review landed, *including* a clean zero-findings
  review) → `completed/success`;
- **failed** (the agent errored / crashed) → `completed/failure`;
- **empty** (the agent returned nothing parseable — the known agy mode) →
  `completed/failure` with an `output` reason of `empty` (treated as **degraded**,
  not success — it is a non-delivery, distinct from a clean zero-findings review);
- **timed-out** (exceeded the wait window) → `completed/timed_out`.

(`completed/neutral` is an acceptable implementer alternative for *empty*; the
load-bearing point is it is **not** `success`.) The check run is the **native,
timestamped stand-in for the `review_requested` edge GitHub denies these bots**.

App reviewers (Copilot) keep using their native `review_requested` edge + review
object; the engine normalizes native-edge and check-run inputs into **one funnel
view** — "isomorphic" at the engine's level, not on the wire (the normalization +
gate live in OBS04 / ADR-0006). The funnel check run is **non-required**: a failed local
review is *visible but non-blocking*, because the Ready gate is "every required
reviewer's outcome is **recorded** + threads resolved," not "every review
**succeeded**."

### Alternatives rejected

- **Native reviewer-API request for the bots** — the obvious path; empirically
  forbidden for App identities (above). Dead end, not a choice.
- **Bot marker comments** — an App-authored issue comment per stage. Works *today*
  with the App's existing `pull_requests:write`, but it is comment-noise on the PR
  and needs bespoke parsing; check runs are a native primitive the engine already
  consumes and carry first-class state + timestamps.

## Consequences

- The engine reads **check runs** (already in its snapshot) + **review objects**;
  no custom comment parsing is introduced.
- The check run's `started_at` gives OBS04's **wait window** a timestamp to age
  against, so the engine stays **stateless** (it takes "now" as input; it keeps no
  clock of its own).
- **Prerequisite / rollout step:** the review Apps need **`checks:write`**, which
  they currently lack — the granted set is only `contents:read`, `metadata:read`,
  `pull_requests:write` (verified on the codex App; a check-run create returns
  `403 Resource not accessible by integration`). Adding a permission scope requires
  the **install owner to re-consent**, so "add `checks:write` + re-authorize the
  install" is a manual owner action per App/owner, folded into the local-reviewer
  rollout (INS01 / issue #26). It cannot be automated by shipit (same class as the
  App install itself).
- Until the re-grant lands, a local review still **posts its review** (that path is
  unaffected); only the pre-post funnel visibility is absent, so OBS02–04 are
  gated on the re-grant for end-to-end verification.

## OBS03: the in-flight marker made real

OBS02 wrote the breadcrumb's two endpoints (create `in_progress`, transition to a
terminal conclusion) inside one synchronous flow. OBS03 makes the *in-flight*
marker an **honest** state by **detaching execution**: the request opens the check
run `in_progress` synchronously in the parent and returns immediately, and a
DETACHED child process runs the agent, posts the review, and closes the SAME run to
its terminal conclusion. So the open and the close straddle a real process boundary
— the run genuinely *is* in flight for the duration of the model run, not for the
blink of a synchronous call. The PR + check run remain the only store (no daemon,
no local job state), and a re-request reconciles against an already-in-flight run
rather than opening a second one.

This sharpens the load-bearing role of `started_at`: the one outcome OBS03 does
**not** itself close is a child that *vanishes* before reaching a terminal PATCH (a
crash in startup, OOM, a reboot — outside the child's own self-resolution guards),
which leaves the run stuck `in_progress`. That is the **vanished-process** case, and
the backstop is **OBS04's wait window** ageing `started_at` — exactly the timestamp
this ADR makes load-bearing. OBS03 relies on that window as the backstop; it does
not implement it.
