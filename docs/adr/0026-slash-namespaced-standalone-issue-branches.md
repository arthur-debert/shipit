# Standalone-issue branches are `issues/<id>/<session>`

Standalone (non-epic) work uses the branch form **`issues/<id>/<session>`** — e.g.
`issues/210/work`, where `<session>` defaults to `work`. This **replaces** the former
`fix/<issue>-<slug>` form (`naming.lex §3`) outright: there is no compatibility with the
old spelling, and the standalone-issue Tree is now wired into `shipit spawn subagent` (not
just `shipit tree create`), so `shipit spawn subagent --issue N` can provision an
issue-shaped Tree with no epic.

This mirrors the `EPIC/umbrella` + `EPIC/WSnn` decision (ADR-0016): the same
slash-namespacing, the same file-vs-directory ref-collision reasoning, applied to the
standalone-issue form.

## Why the `<session>` suffix (and never a bare `issues/<id>`)

A bare `issues/<id>` branch would occupy `refs/heads/issues/<id>` as a git ref **file**. A
ref file cannot coexist with a ref **directory** of the same name — exactly the collision
ADR-0016 records for a bare `EPIC` epic branch against `EPIC/WSnn`. So a bare
`issues/210` branch would *block* any sibling `issues/210/<other>` ref.

The `<session>` leaf (default `work`) is the fix, the direct analog of the `umbrella`
leaf: it keeps `issues/<id>/` a ref **directory**, so a second, concurrent session on the
same issue — an onboarding pass, a spike, a follow-up — coexists as
`issues/<id>/onboard` alongside `issues/<id>/work`. Without the suffix the first session
would foreclose the second. This ref-collision avoidance is the whole reason the suffix
exists; it is not cosmetic.

## Shape (mirrors the epic work-stream shape)

- **branch** (stable, no hash): `issues/<id>/<session>` — the `<session>` plays the
  structural role `WSnn` plays in the epic shape. The branch carries neither slug nor
  agent hash.
- **base**: `origin/main` — a standalone issue is cut from the default branch (an epic
  work stream's `origin/EPIC/umbrella` base is the epic shape's concern, not this one).
  The general convention is `origin/main`; a particular PR may be *stacked* on another
  branch, but that is a property of how the PR is opened, not of the branch form.
- **dir**: `<root>/<org>/<repo>/issues/<id>/<session>[-<slug>]-<agent-hash>` — the branch
  path under the `issues` kind, with the agent hash on the dir **leaf** for disk-collision
  safety (never on the branch) and an optional sanitized slug riding the dir only. A Tree
  reads as `work-header-align-deadbeef` on disk while its branch stays `issues/210/work` —
  exactly as `WS02-tiling-deadbeef` maps to branch `EPIC/WS02`.

The plain-language identifier (`naming.lex §1`) is unaffected; only the git branch form is
slashed, as with the epic forms.

## Consequences

- `tree.layout.plan` resolves the issue shape to `issues/<id>/<session>`; the grammar +
  validation live in one helper, `tree.layout.issue_branch(issue, session)` (the analog of
  `epic_umbrella_base`), so the pure planner and the `shipit spawn subagent` reviewer path
  — which pins an existing issue head without going through `plan` — agree by construction.
- `issue` must be a positive integer and `<session>` must be non-empty after slug
  sanitization; either failure is a clean `ValueError` at the invariant boundary (a bare
  `issues/<id>/` ref is never produced).
- `shipit spawn subagent` now dispatches on shape: `--epic E --ws N` keeps the epic path
  (base `origin/E/umbrella`), while `--issue N` with neither `--epic` nor `--ws` takes the
  standalone-issue path (base `origin/main`). Incomplete or empty combinations fail loud.
- No grandfathering: unlike ADR-0016 (which grandfathered in-flight epics), the old
  `fix/<issue>-<slug>` form is simply removed — the repo's no-backwards-compat rule.
