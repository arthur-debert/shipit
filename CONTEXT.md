# shipit

shipit standardizes work across a personal portfolio of repos: provisioning,
the dev workflow + skills, a multi-language lint gate, GitHub repo setup, pixi
tooling, and the PR-review state machine. This glossary fixes the language of
that domain — especially the PR-flow vocabulary inherited from release-core.

## Language

### PR flow

**PR state engine**:
The reviewer-agnostic core that reads where a PR stands and reports the single
next action. Lives at `src/shipit/prstate/`. A pure function from a snapshot to
a state; it never names a reviewer and never mutates (it *reports* READY; a
caller does the flip).
*Avoid*: "PR bot", "review automation".

**Reviewer adapter**:
The only place that knows one reviewer's mechanics (how to detect its review,
how to request it). Adding a reviewer is adding an adapter to the registry;
nothing downstream changes.

**Required reviewer**:
A reviewer in the **required set** — every one must be **settled** before a PR can be Ready.
The set is policy (config), not code.
*Avoid*: "approver" (this fleet requires 0 approving GitHub reviews).

**Best-effort reviewer**:
A reviewer that never **holds** Ready — an absent or in-progress one never keeps
the PR from Ready. The opposite of a **required reviewer**.

**App reviewer**:
A reviewer addressed through a GitHub `review_requested` edge (Copilot,
CodeRabbit). Contrast **local-agent reviewer**.

**Local-agent reviewer**:
A reviewer whose review is generated locally (`agy-local`, `codex-local`) — an
agent reviews the diff — and posted as a GitHub-App bot identity. GitHub gives it
no native `review_requested` edge (a custom App bot cannot be assigned as a
requested reviewer), so its **review funnel** is tracked by a shipit-authored
signal rather than read from a native reviewer edge. Contrast **App reviewer**.

**rerun**:
A per-reviewer policy flag. `rerun=false` (the default for everyone:
review-once) — a review on any commit counts as done and is never stale after a
push. `rerun=true` (head-strict) — the review must be on the current head, so a
push re-stales it and the reviewer is re-requested.

**Review round**:
One iteration of the review loop, keyed by head SHA — all required reviewers'
findings on the same head fold into one round. Not one review object.

**Review funnel**:
The stages a single reviewer's review passes through on a PR — *requested* →
*in-flight* → *posted*, or a terminal *failed* / *empty* / *timed-out*. The engine
reads the funnel uniformly across reviewer kinds: from native GitHub signals for an
**App reviewer** (its `review_requested` edge, then its review object) and from a
shipit-authored signal for a **local-agent reviewer** (which has no native edge).

**Breaker** (stopping rule):
The rule that ends the review loop instead of iterating forever: stop when 6
rounds are reached, or when the latest round is all nitpicks.

**Nitpick**:
A comment about wording, naming, or style with no correctness, behavioral, or
security impact. A round that is all nitpicks trips a **breaker**.

**Reviewed**:
Every required reviewer **settled** — its **review funnel** reached a recorded
outcome (posted / empty / failed / timed-out) — and every thread from a *posted*
review resolved. A failed / empty / timed-out reviewer is settled but
**non-blocking**: it does not hold Ready, though the PR surfaces it as **degraded**
(visible, never silently "fine").

**Mergeable**:
The PR's merge state permits merging — no conflict, not behind its base, no
unsatisfied branch-protection rule. Keyed off GitHub's authoritative merge-state
signal (`mergeStateStatus == CLEAN`), NOT the async-stale `mergeable` boolean,
which reads optimistically before a recompute lands.

**Ready**:
All three pillars satisfied — the generic, obvious work is done:
(1) the code is correct — **Reviewed** (written, reviewed, every thread
addressed); (2) the checks pass — CI green; (3) the PR is **Mergeable**. This is
exactly the order the engine checks the pillars. Flipping draft→Ready is the one signal that says
"done iterating — a human can validate and merge".

**Wait window**:
How long a *requested but silent* reviewer **holds** a PR before it **times out**
and **settles** (non-blocking, surfaced as degraded). Aged from the reviewer's own
request timestamp; uniform across reviewer kinds; **20m** default, per-reviewer
override. The engine reads it statelessly — "now" is an input, not a clock it
keeps.

**Holds / Settled**:
A reviewer or pillar **holds** a PR when its condition keeps the PR from Ready; it
is **settled** once that condition is met. The PR is **Ready** when nothing holds
it. Deliberately distinct from the **gate** — the lint/test hard check, a separate
pass/fail concept — so readiness is spoken of as *holds / settled / pillars*,
never "gating".

**Next action**:
The single instruction the **PR state engine** emits for a PR's current state
(request a review, address threads, wait for CI, flip to Ready, …).
