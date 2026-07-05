# install + reconciliation — vendor the slow set, pull never push

> Status: **implemented** (shipped before the per-feature PRD convention)
> Origin: this capability was built from the retired roadmap §2, reproduced below so the
> design + verification rationale survives the roadmap's retirement.
> Decisions: `docs/adr/0003-install-reconciliation-pull-not-push.md` (open a PR,
> never admin-push) · `architecture.lex §2` (the slow/fast split).
> **Since superseded in part (PROC02, 2026-07):** the git primitives this plan put
> in `src/shipit/gh.py` now live in the dedicated git Tool adapter, `src/shipit/git.py`
> (ADR-0028; `src/shipit/gh.py` keeps only the `gh` surface).

## Original design (reproduced)

The `shipit install <path>` subcommand: vendor the small slow set into a consumer
repo, recording per-unit pristine hashes in `.shipit.toml`. On re-install,
hash-compare each managed unit against its stored pristine; open a PR with the
changes (never admin-push), surfacing any consumer-edited unit as an override in
the PR.

After this step shipit is independently useful.

### Reuse the Step 1 skeleton — do NOT re-scaffold (Step 1 is merged)

The CLI, the gh boundary, the config reader, and the test patterns already exist.
`install` is a NEW verb in the SAME shape:

- new verb `src/shipit/verbs/install.py`, attached in `cli.py` exactly like
  `gh-setup` (a thin click command forwarding to a `run(...)` that returns an
  int).
- EXTEND `src/shipit/gh.py` with the git + PR primitives install needs (branch,
  add, commit, push, pr-create). COPY them slim from release-core's `gh.py`
  (`git_add`, `git_commit_paths`, `git_current_branch`, `git_default_branch`,
  `pr_create`) — boundary only, no logic.
- EXTEND `src/shipit/config.py` to WRITE `.shipit.toml`, not just read it.
  `tomllib` is read-only — add a writer (the `tomli-w` dep, or hand-serialize the
  two small tables). This is the one genuinely new dependency decision.
- keep the pure reconciliation logic (hash compare + per-unit decision) OUT of
  the gh/fs boundary so it is unit-testable, exactly as `checks.py` is split from
  its gh calls.

Reference WITH CARE: release-core's `init` verb (`verbs/init.py`) is the closest
analog — it installs a managed tree from the wheel bundle and commits only
changed paths. But its model OVERWRITES managed paths and auto-commits to the
branch; it does NOT do pristine-hash override detection and does NOT open a PR.
Take its install-tree mechanics, NOT its overwrite-on-change behavior. Do NOT
port `sync.py` — that 72k-line drift engine is precisely what this design exists
to delete (`lessons-learned.lex §1c` and §4 "Push versus pull").

### The managed set (what "slow" means here)

Each managed unit is either a WHOLE FILE or a marker-delimited BLOCK in a
consumer-owned file. The architecture's slow set is "the bootstrap, the lefthook
caller, the skills, the AGENTS.md block" — but stage it to what exists now:

- `skills/` — whole files (the skills already in this repo: shipit-to-prd,
  shipit-to-issues, shipit-grill-with-docs, lex-primer — confirm the exact managed
  subset when defining the set). They must be bundled as PACKAGE DATA so the
  pip-installed `shipit` can vendor them — the same `importlib.resources`
  mechanism Step 1 used for `data/issue-labels.toml`. They are NOT packaged yet;
  add them.
- the AGENTS.md block — a shipit-managed SECTION injected into the consumer's OWN
  AGENTS.md, delimited by markers. Adopt release's convention (`sync.py`): an
  opening `<!-- Managed by shipit; do not edit. Regenerate via shipit install. -->`
  and a closing marker. Hash the BLOCK content, not the whole file, since the
  consumer owns the rest.
- the lefthook caller — DEFER to Step 3, where the lint check it calls exists. Add
  it to the managed set then.

### The .shipit.toml manifest

Step 1 defined `[secrets]`. Step 2 adds two tables:

```toml
[shipit]
version = "<shipit commit hash that last wrote the set>"

[managed]
"skills/shipit-to-prd/SKILL.md" = "sha256:..."
"AGENTS.md#shipit-block"       = "sha256:..."   # block, not whole file
```

`version` pins the shipit commit that last wrote the set (`architecture.lex §6`);
`[managed]` is the pristine map the next re-install compares against.

### The reconciliation algorithm (a hash compare, not a subsystem)

Per managed unit, exactly three cases — keep it this small (the moment it grows
features it becomes the drift engine, `lessons-learned.lex §4`):

- absent in the consumer → ADD it; record its hash.
- present, consumer-hash == stored pristine → UNCHANGED: overwrite with the new
  shipit content silently; update the stored pristine.
- present, consumer-hash != stored pristine → CONSUMER-EDITED: do NOT clobber.
  Surface the override in the PR — show shipit's intended content against the
  consumer's edit and leave the decision to the human.

A first install has no `[managed]` table, so every unit is the "absent" case.
"Surface the override" means make the divergence visible in the PR (e.g. the PR
body lists each overridden path with its diff), never a silent overwrite.

### PR mechanics — pull, never push

install stages onto a branch (e.g. `shipit/install`), commits the managed
changes, pushes the branch, and opens a DRAFT PR for a human to merge — the same
draft → shepherd → ready lifecycle shipit itself follows (`AGENTS.lex`). It NEVER
admin-pushes to main. The `--push` flag is the sole break-glass: a straight push
to main, reserved for bootstrapping a repo that cannot yet run the PR loop (the
README). Support `--dry-run` (print the plan, touch nothing), as Step 1's verbs
do.

## Decisions

The questions this PRD originally left open are now resolved and captured in
`docs/adr/0003-install-reconciliation-pull-not-push.md`:

- **bootstrap** = the `bin/shipit` minimal launcher, managed as a whole-file unit;
  the pinned-version pixi auto-provision is deferred to Step 5. **[Superseded by
  ADR-0033 (ADP00):** the pin mechanism landed differently — `bin/shipit`
  resolves `.shipit.toml`'s `[shipit].version` (a full git sha) and execs that
  build via `uv tool run`, NOT as a pinned `pixi` dependency. The pixi-dependency
  plan was considered and rejected (it duplicates the pin into `pixi.toml` /
  `pixi.lock` — a second source that can disagree — and is unusable by pre-env
  verbs like install/gh-setup); "Step 5" as a distinct step is retired.**]
- **block marker + block-hashing** — `<!-- Managed by shipit; do not edit. … -->`
  / `<!-- End shipit-managed block. -->`, hashing the block inner content only.

One question is NOT settled by the code and is left for the maintainer:

- **self-install** (`shipit install .` on shipit) — there is no special-casing in
  the verb and shipit's own `.shipit.toml` carries no `[managed]` table, so it was
  never dogfooded as an identity no-op nor explicitly ruled out of scope.

### Verified by

A fresh install on a test consumer opens a PR that adds the managed set and writes
the `[shipit]` / `[managed]` tables; a re-install after the consumer edits a
managed file opens a PR that SHOWS the override rather than silently clobbering
it; a re-install with no changes is a clean no-op (no PR, or an empty one),
proving churn tracks shipit cadence, not invocation count.
