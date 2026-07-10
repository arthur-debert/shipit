# Review scope is the diff; context is the checkout

The review instructions contradicted themselves — "base your review solely on
the provided diff, do not run shell" in the shared instructions vs "run
`gh pr diff` and walk the checkout" in the wrapper — and the two arms diverged
on scope: dimension passes were restricted to findings the diff introduced or
exposed while the single pass was not, so the arms answered different questions
and their recall denominators were incomparable. We decided one canonical
baseline for **every** reviewer arm and pass: **report only on the diff; read
anything; run nothing.**

Scope: only findings the PR's diff introduced or exposed may be posted; purely
pre-existing issues route out-of-scope (the archetypal disposition of
ADR-0045) toward the Opportunity seam, for single-pass exactly as for
dimension passes. Context: reading callers, definitions, and neighboring code
in the checkout is encouraged — ADR-0045 already mandates
dependency-neighborhood reading because raw-hunk review is the documented
cross-file-regression failure mode — while executing build/test/shell beyond
reading remains forbidden (reviewer Runs are read-only; execution is cost and
side effects, not context). "Reviewer as whole-codebase auditor" was rejected
as a different product surface, not a review scope. Context *strategy* (how
much of the checkout to read, commit-walk priming, ascetic diff-only) is an
experiment axis varied by Cells against this one internally-consistent
baseline.
