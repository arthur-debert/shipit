# Branches are slash-namespaced with an `EPIC/umbrella` epic branch

Repo branches use a slash-namespaced grammar — `EPIC/WSnn` for work streams and
**`EPIC/umbrella`** for the epic branch (e.g. `GPU02/umbrella`, `GPU02/WS03`),
`fix/<issue>-<slug>` for standalone work. This **reverses** the previous documented
rule (`naming.lex` previously stated "`EPIC-WSnn`, hyphen, NOT slash; the epic branch
is the bare epic code") — a reversal worth recording because the old rule carried an
explicit git-collision rationale that a future reader would otherwise re-derive and
re-impose.

## Why reverse it

The old rule chose hyphens because a slash form *with a bare epic branch* collides in
git: a ref cannot be both a file (`refs/heads/GPU02`) and a directory
(`refs/heads/GPU02/WS03`), so `GPU02` and `GPU02/WS03` cannot coexist. The fix is not
to avoid slashes but to **avoid the bare epic ref**: naming the epic branch
`GPU02/umbrella` makes it a *sibling* of its work streams under `refs/heads/GPU02/`, so
nothing is both a file and a directory. With the collision gone, slashes earn their
keep — every branch of one epic groups under a single `GPU02/` ref directory (sorts and
greps cleanly), and the branch namespace mirrors the on-disk Tree layout
(`…/epics/GPU02/WS03-<hash>`, ADR-0014).

## Consequences

- The plain-language **identifier** (titles, logs, cross-refs) stays hyphenated
  (`GPU02-WS03`); only the git **branch** form is slashed. The two now use different
  separators by design.
- **In-flight epics are grandfathered, not renamed.** Epics already underway when this
  landed (HAR02, OBS04, PRF01, FLU01) keep the bare-`EPIC` / `EPIC-WSnn` form;
  retroactive renaming was rejected as high-risk (pending umbrella PRs, refs across
  local + remote) for negligible value (merged branches gain nothing). The new scheme
  applies to epics created after.
- No code change: the branch form is documented-convention only (pixi, the shipit CLI,
  and all code reference "the epic branch" abstractly, never the form), so this is a
  docs-only reversal.
