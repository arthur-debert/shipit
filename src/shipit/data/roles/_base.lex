Shared dev cycle

There is ONE dev cycle, and it is ALWAYS delegated: draft first, driven by the
PR state engine, shepherded to ready. The agent the human addresses never
implements; it delegates to a role-scoped subagent. No task is "small enough to
do myself".

The cycle in one line: open a DRAFT PR, drive it (request reviews, address
rounds, get CI green and the branch mergeable), then flip draft to ready — the
one signal that a human can validate and merge. Stop at the flip; the human
merges.

Ground rules every role shares:

- Branch off the integration base, freshly fetched, never a stale local copy — and open the PR against that same base. Three shapes: a standalone ISSUE Run works on branch `issues/<id>/<session>` (session default `work`) cut from `origin/main`; a workstream of an epic works on branch `EPIC/WSnn` cut from the epic branch; a freeform branch is cut from `origin/main`.
- The PR engine is authoritative: run `shipit pr status` and `shipit pr next` and do what it reports; do not carry the reviewer, wait, or breaker policy in your head.
- Committing, pushing, and opening the draft PR need no human go-ahead; the only step that needs a human is the final merge.
- Stay in your role: do the slice your role owns and hand back; do not drift into another role's job.
