# Fleet Operations

## Context

The Portfolio is Shipit’s authoritative Repo list. Today `shipit fleet sweep`
contains specialized verification orchestration, while updates, status, and
arbitrary commands require manual loops. The missing capability is therefore
not merely four more CLI verbs: it is a reusable fleet execution foundation
that lets each operation supply its own work without reimplementing Portfolio
iteration, Tree lifecycle, and reporting.

Sweep also borrows human checkouts as clone donors, which is unsafe when users
and agents work concurrently.

Existing Tree creation, provisioning, cleanup, and reference-and-dissociate
cloning remain the execution substrate. Fleet operations add the missing
Shipit-owned donor cache and a substantial common orchestration layer around
that substrate.

## Problem

Manual fleet loops can race on human checkouts, duplicate Tree machinery, and
produce inconsistent results. Adding independent loops for every new verb would
make selection, lifecycle, failure, and output behavior drift further. Shipit
also lacks one view of fleet pins, update PRs, adoption, and internal package
edges.

## Goals

- Provide `shipit fleet verify`, `shipit fleet update`,
  `shipit fleet status`, `shipit fleet run`, and
  `shipit fleet tree-cache refresh`.
- Replace `sweep` with `verify` and user-facing `reconcile` language with
  `update` in one change across code, scripts, docs, and reports.
- Select Repos with either `--only` or `--exclude`; reject both together.
- Provide one shared fleet module/interface that owns Portfolio selection,
  selector validation, canonical Repo identity and order, serial execution,
  operation-appropriate Tree acquisition and cleanup, continue-on-error,
  structured per-Repo outcomes, and consistent human/JSON rendering.
- Reuse the appropriate existing Tree creation, provisioning, and cleanup paths
  without launching per-Repo agents.
- Never use human checkouts as donors.
- Continue after per-Repo failures and return deterministic human- and
  machine-readable results.

## Non-Goals

- Portfolio stack selection.
- A second Tree or install implementation.
- Parallel execution in the first version.
- Resolving package versions from `pixi.lock`.
- A guessed “migrated” verdict for consumer Repos.

## Proposed Shape

### Common fleet foundation

Each operation supplies its operation-specific work; the common fleet
implementation owns selection, iteration, lifecycle, and results. It does not
force every operation into one Tree shape: `verify` and `run` use fully
provisioned Trees as appropriate, `status` inspects the live remote through a
read-only Tree, `update` uses the writable Tree its update workflow requires,
and `tree-cache refresh` maintains donors without creating an operation Tree.
The cache remains a performance aid and the remote remains authoritative.

Operations run serially in canonical Repo order and continue after per-Repo
failure. Each selected Repo produces exactly one structured result containing
its identity, operation outcome/status, duration when available, and captured
stdout/stderr or actionable detail. PR operations include the full URL. Human
and JSON renderers consume the same results in deterministic order; any failed,
blocked, or pending result makes the aggregate command nonzero without hiding
other results.

### Operations

- `shipit fleet verify` retains the current sweep check-matrix behavior.
- `shipit fleet update [--version X.Y.Z]` updates to the latest stable Shipit
  release by default or an exact released version (including an explicitly
  requested prerelease). It opens or refreshes consumer update PRs, converts
  them from draft to Ready without requesting consumer review, arms auto-merge,
  and waits up to 25 minutes for terminal outcomes.
- `shipit fleet status` reads each live remote default branch and reports Repo,
  Shipit Version or Revision, the pin-change commit/date, and required
  reviewers. Composable facets add GitHub state (`--gh`), declared internal
  conda producer/consumer edges (`--deps`), and concrete Shipit adoption
  evidence (`--adoption`).
- `shipit fleet run -- <argv...>` executes exact argv in a normal, fully
  provisioned, disposable writable Tree.
- `shipit fleet tree-cache refresh` explicitly refreshes selected cache donors.

Released installs store one semantic `version = "x.y.z"`. Exceptional manual
testing stores a mutually exclusive full `revision`; fleet update writes only
Versions. During migration, a full SHA in the legacy `version` field is
dual-read as a legacy Revision pin and reported as such; the next released
fleet update atomically replaces it with a semantic Version. Dual-key and other
malformed states fail loudly.

`shipit update` is a user-facing verb alias of the idempotent `shipit install`,
not a release resolver or another implementation. It updates only when the
target build has already been selected externally, as fleet orchestration does
or a manual `SHIPIT_EXEC` override can do. Invoking it through a Repo's ordinary
pin-wins launcher runs that Repo's currently pinned build.

The Shipit-owned Repo donor cache is concurrency-safe and performance-only.
Missing donors initialize automatically; existing donors refresh only through
`fleet tree-cache refresh`. Every temporary Tree still resolves the live GitHub
default branch, so stale cache objects cannot produce stale state.

Fleet update resolves the release once, opens all selected update PRs serially,
converts each generated draft PR to Ready without requesting consumer review,
arms auto-merge, then polls them together. This transition is part of the narrow
released-update exception: the released Shipit code was reviewed at its
producer, while consumer lint and tests prove compatibility. Passing required
checks and mergeability auto-merge. Failed checks or conflicts leave the PR open
and visible for follow-up. At timeout, auto-merge remains armed and the result
is `pending`.

## User / Agent Stories

1. As an operator, I want one fleet verification report.
2. As an operator, I want released Shipit updates to merge when consumer checks
   prove compatibility and to surface failures with direct PR links.
3. As an operator, I want live fleet status without trusting a stale cache.
4. As a human or agent, I want exact argv run in isolated Repo Trees.
5. As a concurrent user, I want cache maintenance that cannot corrupt donors or
   touch my checkouts.

## Risks And Rabbit Holes

- The donor cache must never become an authority or another checkout system.
- Separate per-verb loops would duplicate selection, lifecycle, failure, and
  rendering policy.
- An overgeneric callback abstraction would be equally harmful if it leaked or
  flattened the distinct Tree and lifecycle needs of each operation.
- `run` must remain argv, not a shell-string API.
- Status should report evidence, not infer migration or “last update.”
- Update auto-merge is a narrow release-update exception, not a new general PR
  lifecycle.

## Cross-Cutting Concerns

Output must follow the common result contract across fleet verbs. Credentials
flow only through existing GitHub and Shipit boundaries. The one-shot
terminology migration includes the historical fleet-sweep report and all
consumers.

## Testing / Verification

- Test the common foundation first: selectors are applied once, every selected
  Repo produces exactly one result, execution follows serial canonical order,
  later Repos run after a failure, Trees are cleaned up, and human/JSON
  rendering remains stable.
- Test operation adapters for their distinct Tree lifecycle and work: exact
  argv, verification checks, update outcomes, status facets, and cache refresh.
- Prove Tree-backed fleet verbs use ordinary Tree
  create/provision/cleanup paths.
- Exercise donor initialization and explicit refresh concurrently and after
  interruption; prove live remotes remain authoritative.
- Run update against disposable consumer Repos for current, merged, failed,
  blocked, and timed-out outcomes; verify generated drafts become Ready before
  auto-merge is armed, Version and managed files move together, and every PR
  result has a full URL.
- Cover the pin migration: dual-read a legacy SHA in `version` as a legacy
  Revision, rewrite it to a released Version with its managed files, and reject
  dual-key or malformed states.
- Run overlapping real fleet operations and confirm isolated Trees, a healthy
  shared cache, and untouched human checkouts.

## Workstream Hints

Build the common fleet foundation as the prerequisite/tracer seam, then attach
the operation adapters without duplicating its loop or result contract. The
Shipit-owned donor cache is a second significant foundation seam. Other likely
seams are status facets, update orchestration, and terminology/report
migration.

## Out Of Scope

Stack semantics, shell evaluation, human-checkout fallback, per-Repo agents,
fleet Revision rollout, and consumer review of generated release updates.

## Further Notes

- [ADR-0080: A Shipit pin is a release Version or development Revision](../adr/0080-shipit-pin-is-version-or-revision.md)
- [ADR-0081: Portfolio Trees borrow only from Shipit-owned donors](../adr/0081-portfolio-trees-use-shipit-owned-donors.md)
- [ADR-0082: Released Shipit updates use consumer checks and auto-merge](../adr/0082-released-shipit-updates-use-checks-and-auto-merge.md)
