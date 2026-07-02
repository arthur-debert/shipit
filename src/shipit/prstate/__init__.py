"""shipit.prstate — a reviewer-agnostic GitHub PR state engine.

The stable core of the development-workflow layer: a read-only model of where
a PR stands (which reviewers are pending/done, which threads are open, whether
it is mergeable), with reviewer-specific mechanics isolated in swappable
adapters so the core never names a reviewer.

Copied from release-core (ADR-0001: shipit reuses the engine by COPY, never a
wheel dependency); the pure core is near-byte-for-byte, only relative imports
are rewritten to `shipit.prstate`. The engine's former second `gh` boundary
(`shipit.prstate.ghapi`) is gone: PROC02-WS01 (ADR-0028, glassbox PRD) merged
it into the ONE gh Tool adapter, `shipit.gh` — which carries the GraphQL +
PR-act calls the engine needs, the pagination-merging helper (defined exactly
once), and the per-tool timeout defaults. The stdlib-only guarantee that once
justified two boundaries holds trivially across the merge: `shipit.gh` (like
`shipit.execrun`/`shipit.redact` under it) is itself stdlib-only.

Boundary discipline: every GitHub call goes through the `shipit.gh` adapter
(shell out to `gh`, executed by the one Exec runner `shipit.execrun` —
ADR-0028); everything else is pure transformation over recorded data, so it
unit-tests against captured JSON with no network. stdlib only — no
third-party runtime deps.
"""
