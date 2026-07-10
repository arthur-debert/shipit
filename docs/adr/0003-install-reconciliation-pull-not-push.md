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

## Resolved details (carried from docs/legacy-prd/install-reconciliation.md)

- **The bootstrap is `bin/shipit`** — a minimal launcher managed as a whole-file
  unit (`src/shipit/verbs/install.py:171-178`; content at
  `src/shipit/data/bootstrap/shipit`). It makes the `shipit` CLI reachable from a
  stable in-repo path and execs a `shipit` already on PATH. The pinned-version
  auto-provision (a pixi dependency on the shipit package at `.shipit.toml`'s
  `[shipit].version`) is deferred to the pixi integration (Step 5); the launcher
  fails loudly with exit 127 until then.
  **[Superseded by ADR-0033 (ADP00):** the launcher no longer execs a `shipit`
  on PATH — PATH is never consulted in a pinned repo. It resolves the full-sha
  `[shipit].version` pin and execs that build via `uv tool run` (pin-wins). The
  pin also did NOT land as a pixi dependency; "Step 5" as a distinct step is
  retired. The exit-127 fail-loud-toward-bootstrap posture is the one part that
  survives, now for a *pinless* repo.]
- **AGENTS.md block markers + block-hashing.** The managed region is fenced by
  `BLOCK_OPEN`/`BLOCK_CLOSE` (`install.py:46-47`:
  `<!-- Managed by shipit; do not edit. Regenerate via shipit install. -->` …
  `<!-- End shipit-managed block. -->`). shipit hashes the BLOCK INNER content,
  not the whole file — the consumer owns the rest (`desired_hash` /
  `consumer_hash`, `install.py:103-107`, `:331-339`).
- **Self-install** has no special-casing in the code — see the UNRESOLVED note in
  the PRD handoff; left out of this ADR deliberately.

## Consequences

- Every install lands as a reviewable PR, so the consumer's branch protection and human
  review govern what enters the repo; shipit never bypasses them.
- A consumer-edited managed file is preserved, never silently overwritten — the override is
  visible in the PR diff.
- Churn tracks shipit's cadence, not invocation count: a re-install with no content change
  is a clean no-op (no PR or an empty one).
- The reconciler must stay feature-poor on purpose; any pressure to add merge/3-way/auto-
  resolve behavior is a signal it is regressing toward the drift engine and must be resisted.
