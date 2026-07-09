# Head-strict re-review (`rerun=true`) is the default

The review-once default (`rerun=false`: a review on any commit counts, never
stale after a push) existed as a cost/noise workaround from the era when
reviews landed uncoordinated from all over the place and every re-review was a
full-PR model run. Both premises are gone: the PR state engine is the sole
requester of required reviewers (ADR-0031), and, after round 1, each round
reviews only the fix range (`last-reviewed-head..new-head`) as a cheap single
incremental pass. We flip the default to `rerun=true` (head-strict): every push
re-stales required reviewers and the engine re-requests them, so mistakes made
while addressing a review are themselves reviewed — the property the
incremental-round design exists to deliver, and one a review-once reviewer
structurally never provides. The loop stays bounded by the Breaker (round cap,
or a round with no major+ Finding), and a fired breaker still suppresses all
re-requests so the final nit-fix push cannot reopen the loop. Review-once
remains available as an explicit per-reviewer opt-out for reviewers whose
re-runs stay expensive (full-diff app reviewers on metered plans).

Consequence: this ADR records the decision; the flip itself lands with the
RVW02 implementation (the incremental-round work is what removes the cost
premise). Until then the shipped code default remains review-once, and the
review-once-default rule written into `docs/dev-cycle.lex` (release repo — the
canonical dev-cycle doc), the global CLAUDE.md guidance, and shipit's role
docs stays accurate. Those docs must be updated in the same change that flips
the code default — a dedicated RVW02 workstream — and the canonical doc wins
on drift, so it flips before (or with) the code, never after.
