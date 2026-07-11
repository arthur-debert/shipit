# shipit is public — @vN reusable-workflow distribution requires a public publisher

ADR-0010 publishes the reusable workflow blocks from `arthur-debert/shipit@vN`
and has every consumer carry only a thin caller; ADR-0033 pins each repo to a
shipit sha that `uv` resolves straight from the git repo, with private-repo git
credentials named as a hard prerequisite wherever a pin first resolves. Both
were written while shipit was PRIVATE, and TOL02-WS07's rc campaign
(issue #565) hit the wall where those two stances contradict each other: GitHub
shares a private repo's reusable workflows within the owner namespace ONLY.
The Actions access level `user` reaches user-owned repos and nothing else, and
there is NO credential mechanism for cross-owner `uses:` resolution — a
`uses:` ref to a private repo in another owner's namespace cannot be made to
work at any permission setting. The uv/pixi git-dependency pin hit the same
wall on org runners. So org-owned consumers — lex (`lex-fmt/lex`) was the live
case — could never call private shipit's blocks, and ADR-0010's consumer model
("consumers pin `arthur-debert/shipit/...@v1`") was unsatisfiable for every
non-user-owned consumer. Legacy `arthur-debert/release` is public for exactly
this reason.

## Decision

Owner decision (2026-07-11): **shipit is PUBLIC.** The full @vN distribution
model of ADR-0010 holds for ALL consumers, org-owned included, with no
credential machinery on the `uses:` leg and none on the launcher's pin
resolution.

Evidence — the decision was verified live in the same campaign, not just
reasoned (campaign comments on #565; findings recorded in
`docs/dev/tol02-ws07-go-no-go.md`): after the flip, lex's caller switched to
the real remote refs (`uses:
arthur-debert/shipit/.github/workflows/wf-release.yml@v1`) and the traversal
ran green end-to-end (lex-fmt/lex Actions run `29162869655`), with every
nested `@v1` block resolving cross-owner and the ADR-0033 launcher
uv-resolving the pin against the now-public repo — the vendored blocks, the
`SHIPIT_EXEC` bridge, and the committed wheel all removed from the consumer.

### Alternative rejected

**Scope the consumer set to user-owned repos only** (keep shipit private;
declare cross-owner consumers out of scope). Rejected: org-owned consumers
are real today (lex-fmt) and structural for the portfolio, so this forks the
distribution story into a first-class path and a vendor-everything path — the
thick-copy drift ADR-0010 exists to kill — and it caps adoption at an
accident of repo ownership rather than a technical boundary.

## Consequences

- ADR-0033's "private-repo git credentials" prerequisite is void: the launcher
  clones the public repo credential-less. The other half stands — `uv` must be
  provisioned wherever a pin first resolves (runner leg owned by ADP02).
- The publisher-side Actions access-level setting (WS07 finding 5: it shipped
  as `none`) stops being load-bearing for cross-owner consumers — a public
  repo's reusable workflows are callable by any repo — though the
  install/gh-setup surface may still manage it for hygiene.
- The repo's contents are world-readable. This changes nothing shipit does:
  no secrets live in-repo (consumer secrets ride GitHub secrets injected by
  the thin callers), and the packaging/pinning model is unchanged — only the
  repo's visibility flipped.
- `-release-rc` campaigns and consumers exercise the REAL distribution path;
  no vendored-blocks bridge remains as sanctioned practice.
