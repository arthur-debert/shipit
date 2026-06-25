"""shipit.prstate — a reviewer-agnostic GitHub PR state engine.

The stable core of the development-workflow layer: a read-only model of where
a PR stands (which reviewers are pending/done, which threads are open, whether
it is mergeable), with reviewer-specific mechanics isolated in swappable
adapters so the core never names a reviewer.

Copied from release-core (ADR-0001: shipit reuses the engine by COPY, never a
wheel dependency); the pure core is near-byte-for-byte, only relative imports
are rewritten to `shipit.prstate`. shipit keeps TWO `gh` boundaries (ADR-0002 /
PRD "two gh boundaries"):

  * `shipit.gh` — the verb-layer boundary (gh-setup / install).
  * `shipit.prstate.ghapi` — the ENGINE's own boundary, kept distinct: this
    subpackage is stdlib-only (it runs the same in CI / Claude Cloud / local),
    adding the GraphQL + PR-act calls the verb-layer boundary lacks. Both shell
    out to `gh`; the small REST/pagination overlap is intentional and
    load-bearing for the stdlib-only guarantee here. They are not merged.

Boundary discipline: every GitHub call goes through `ghapi` (shell out to
`gh`); everything else is pure transformation over recorded data, so it unit-
tests against captured JSON with no network. stdlib only — no third-party
runtime deps.
"""
