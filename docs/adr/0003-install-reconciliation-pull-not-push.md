# Install reconciliation is pull, never push

`shipit install` provisions and reconciles its managed "slow set" (the skills, the
AGENTS.md block, the lefthook caller) into a consumer repo by **hash-compare and PR** —
it stages the changes onto a branch and opens a DRAFT PR for a human to merge. It NEVER
admin-pushes to the consumer's default branch. `--push` is the sole break-glass, reserved
for bootstrapping a repo that cannot yet run the PR loop.

The reconciliation itself is a **hash compare, not a drift subsystem**. Per managed unit
there are exactly four outcomes, decided by comparing the consumer's current hash against
the pristine hash stored in `.shipit.toml` at the previous install:

- absent in the consumer → **ADD** it; record its hash.
- present and unchanged (hash == stored pristine) → **NOOP**: overwrite silently with the
  new shipit content; update the stored pristine.
- present and unchanged where shipit's content differs → **UPDATE**: same silent path,
  carrying the new content forward.
- present and consumer-edited (hash != stored pristine) → **OVERRIDE**: do not clobber;
  surface the divergence in the PR and leave the decision to the human.

This is deliberately kept to a hash compare. The moment it grows features it becomes the
drift engine this design exists to delete (release-core's `sync.py`). Full rationale —
push-versus-pull and why the drift engine is the anti-goal — lives in
`docs/dev/architecture.lex §2` and `docs/dev/lessons-learned.lex §4`; it is not duplicated
here.

## Consequences

- Every install lands as a reviewable PR, so the consumer's branch protection and human
  review govern what enters the repo; shipit never bypasses them.
- A consumer-edited managed file is preserved, never silently overwritten — the override is
  visible in the PR diff.
- Churn tracks shipit's cadence, not invocation count: a re-install with no content change
  is a clean no-op (no PR or an empty one).
- The reconciler must stay feature-poor on purpose; any pressure to add merge/3-way/auto-
  resolve behavior is a signal it is regressing toward the drift engine and must be resisted.
