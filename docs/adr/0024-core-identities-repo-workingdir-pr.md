# Core identities: Repo, WorkingDir, PR (origin-derived; identity vs content/views)

> **Status: Accepted (landed).** Landed by the Core Model epic (COR01: the value
> objects + the store re-key) and extended by Identity threading (COR02): `Sha`
> joins the identity module, `repo_from_slug` is the one canonical slug parser,
> and the identities are threaded through Tree planning, logging, and the PR paths.
> Completed by Typed results (PROC03, ADR-0028): the Tool adapters now RETURN
> these identities, so no caller re-parses a slug or sha at a call site.

shipit gains **canonical identity value objects** for its core nouns — `Repo`,
`WorkingDir`, `PR` — so each is defined once and every subsystem keys on the same thing.

## Context

shipit had no canonical value objects for its core nouns, so each subsystem invented its
own key. The worst was **Repo**: the eval store keyed by *resolved filesystem path* while
`logsetup` keyed by origin `owner/repo` — unjoinable, and every Tree clone scattered a new
eval store (60 of 61 stores orphaned). `git rev-parse --show-toplevel` was re-implemented
four times because `gh.repo_root()` lacked a `cwd`. **PR** was modeled twice
(`PullContext` / `PRContext`) with `head_sha` fetched three ways, and one builder hardcoded
`is_draft=False` — a latent trap.

## Decision

- **`Repo` = `(owner, name)`**, identity derived **locally** from the origin remote
  (`git remote get-url origin`) — offline, Tree-safe, deliberately *not* the API-based
  `gh.current_repo()`. **`Owner` = `(login, kind)`**; **`OwnerKind ∈ {user,
  organization}`** is an OPTIONAL, lazily-resolved enrichment and is **not** part of Repo
  identity/equality (so the store key is stable whether or not kind is known). The
  **path→toolchain map** is the *same* repo's content — identity and content are two
  facets of one noun.
- **`WorkingDir` = `(path, Repo, revision)`** — the single resolver for "what repo +
  revision is checked out at this path," replacing the 4× re-derivation. **Composition,
  not inheritance:** a **Tree** *has-a* WorkingDir; the **main checkout** is a WorkingDir
  that is not a Tree.
- **`PR` = identity `(repo, number)` + cheap core** (`head_sha`, `base_ref`, `is_draft`,
  `merge_state`). The readiness path and the review path build **distinct richer views
  that compose a `PR`**, never parallel half-overlapping snapshots. **One `head_sha`
  fetch** boundary.
- The **eval store re-keys by `Repo` identity**. No compat: existing path-keyed stores
  **orphan** (local, uncommitted, regenerable data).

## Considered options

- **API-derived repo identity** — rejected: needs network, breaks Tree/offline use.
- **`kind` in Repo identity** — rejected: re-orphans the store the moment kind is enriched.
- **One mega-`PR` object with optional-everything** — rejected: reintroduces the
  `is_draft=False` latent trap; a field belongs on the view that fetched it.
- **A migration shim for old eval stores** — rejected: no-compat rule; the data is
  regenerable.

## Consequences

COR01's WS-Repo landed `Repo`/`Owner`/`WorkingDir`, re-keyed the eval store, and gave
`repo_root` a `cwd` (killing the 4× re-impl); WS-PR unified the two PR contexts.
Telemetry and logs for one repo now join on one stable key.

COR02 (identity threading) then pushed the identities through the layers that still
re-parsed raw strings. **`Sha`** joined the identity module — a validated FULL git
object id (40/64 hex), lowercase-normalized, equality full-vs-full and Sha-vs-Sha only
(a raw-string compare raises) — minted at the one wire read (`core_from_node`, so
`PR.head_sha` is a `Sha`) and keying review staleness. **`repo_from_slug`** became the
one canonical `owner/name` parser (lowercased to match `resolve_repo`, making Repo
identity case-insensitive end to end), so Tree planning, logging setup, and the review
paths share one Repo identity instead of hand-splitting slugs — mixed-case sources can
no longer fork one repo's Tree paths or log directories.

PROC03 (typed results, ADR-0028) then closed the arc at the adapter boundary: the
gh/git adapters *return* the identities instead of the strings they parse from.
`gh.current_repo` / `gh.repo_canonical` return a `Repo` (through `repo_from_slug`),
the typed PR read `gh.pr_core` returns a `PR` (through `core_from_node`), and the
git adapter's commit reads (`head_commit`, `merge_base`, `commits_between`,
`unpushed_shas`) return `Sha` values — threaded end to end through the review-diff
path (`ReviewView.base_sha` is a `Sha` minted where `baseRefOid` enters). The
old tuple read `gh.repo_slug()` is deleted (callers ride `current_repo().slug`);
raw JSON survives only where no core noun exists yet (`gh.pr_view`'s field-list
read, `gh.pr_meta`'s readiness node).
