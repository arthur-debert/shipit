# Spawn dogfood verification — the live E2E standing gate for `shipit spawn`

TRE03 builds shipit-owned subagent spawning ([ADR-0017](../adr/0017-shipit-owned-subagent-spawning.md),
[ADR-0018](../adr/0018-read-only-trees.md), [ADR-0019](../adr/0019-headless-claude-run-launch-contract.md)):
`shipit spawn subagent` creates an isolated **Tree** (a dissociated clone), launches a
headless `claude` Run rooted in it, and — for a write Run — has that Run open a draft
PR from the Tree's branch; for a **reviewer** Run it provisions a shared read-only
Tree and posts a review through the PR.

Every work stream is unit-tested with the `claude` spawn and the `gh` boundary
**faked**. Those tests prove the _shape_ shipit produces; they cannot prove the
load-bearing facts that exist only against live tooling. The **dogfood harness**
(`shipit.spawn.dogfood`) is that missing live counterpart: it drives the whole spawn
lifecycle end-to-end against a **separate scratch checkout** and asserts the standing
gate the maintainer wants codified.

This is the same shape as the [review-funnel verification harness](./review-app-provisioning.md)
(`pixi run -e review verify-funnel`): a non-default pixi env/task, excluded from the
CI/test gate, that hits live tooling on demand.

## What it asserts

When run live (`pixi run -e dogfood verify-spawn ...`), the harness asserts, as one
PASS/FAIL report:

- **Write Run → real draft PR.** A write Run lands in its Tree **on the planned
  `EPIC/WSnn` branch** (never `shipit/install`), `pixi` runs in the Tree, and it opens
  a **real, OPEN, DRAFT PR** from the Tree's branch.
- **Reviewer Run → shared read-only Tree + a real review.** A reviewer Run provisions
  a **shared** read-only Tree (a second reviewer on the same `(repo, branch)` reuses
  the clone) that is **genuinely non-writable**, and **posts a review** on the write
  Run's PR.
- **Fail-closed.** A forced Tree-create failure **fails closed** — loud diagnostic,
  nonzero exit, **no native-worktree fallback**.
- **The three isolation invariants on every spawned Tree** (the non-negotiables):
  1. **no cwd footgun** — the Tree is a distinct dir from the scratch checkout, and a
     write Run leaves the scratch checkout clean (the Run's writes land in the Tree);
  2. **dissociated clone, not a worktree** — `.git` is a directory and there is no
     `objects/info/alternates`;
  3. **Tree under the central root, NOT inside any `.claude` dir**.
- **No origin side effects** from provisioning — no `shipit/install` PR on origin
  (WS08 made provisioning local-only).

## Why it is opt-in and off the CI/test gate

A live run **spawns real `claude` Runs (token spend) and opens real PRs**, and needs a
scratch checkout plus a live `claude` login. So, exactly like `verify-funnel`:

- it is a standalone entry point (`python -m shipit.spawn.dogfood`, wired as
  `pixi run -e dogfood verify-spawn`) in the **non-default `dogfood` pixi env**, never
  part of `pixi run test` / CI / the `lint` required-check surface, and off the locked
  `pixi.lock` check;
- it **refuses to run** without an explicit `--scratch` checkout and target
  coordinates (or their `SHIPIT_DOGFOOD_*` env equivalents), so it can never fire by
  accident;
- pytest never collects it (it lives in `src/`, not `tests/`).

Its assertion + wiring logic _is_ covered by the normal test checks:
`tests/test_spawn_dogfood.py` drives the pure isolation assertions against planted
fixtures and the orchestration with every live seam faked, so the harness can't
silently rot.

## How to run it live (maintainer)

### Prerequisites

- A **scratch checkout** of the target repo, separate from the checkout building the
  feature — e.g. `~/h/scratch/shipit/`. Live runs spawn Runs and open PRs from here;
  keeping it separate means a live run never collides with feature work. Use a repo you
  are comfortable opening throwaway PRs against (a canary / scratch fork is ideal).
- A working **`claude` login** (the keychain / OAuth login the launcher relies on —
  ADR-0019 §3 scrubs `ANTHROPIC_API_KEY` so the keychain login is used).
- `gh` authenticated for the target repo (the Runs open PRs and post reviews via `gh`).
- The target `EPIC/WSnn` branch's umbrella base must exist on origin (the write Run
  cuts `EPIC/WSnn` and the reviewer reviews that head), and a real `--issue` for the
  write Run to implement.

### Invocation

```sh
pixi run -e dogfood verify-spawn \
  --scratch ~/h/scratch/shipit \
  --repo arthur-debert/shipit \
  --epic TRE03 \
  --ws 5 \
  --issue 159
```

or via env (same effect):

```sh
SHIPIT_DOGFOOD_SCRATCH=~/h/scratch/shipit \
SHIPIT_DOGFOOD_REPO=arthur-debert/shipit \
SHIPIT_DOGFOOD_EPIC=TRE03 \
SHIPIT_DOGFOOD_WS=5 \
SHIPIT_DOGFOOD_ISSUE=159 \
python -m shipit.spawn.dogfood
```

Optional: `--write-role <role>` (default `implementer`) sets the role of the write Run.

The harness exits `0` on a full PASS, `1` on any failed check, and prints a
line-per-check report.

### What it will create (read before running)

A live run is a **deliberate, side-effecting act**:

- it **spawns real `claude` Runs** (write + two reviewer Runs) — real token spend;
- the write Run **opens a real draft PR** from `EPIC/WSnn` on the target repo;
- the reviewer Runs **post a real review** on that PR;
- it materializes Trees under the central root (`SHIPIT_TREES_ROOT`, default
  `~/workspace/trees`).

Clean up afterwards by closing the draft PR and deleting its branch, and reclaiming
the Trees (`shipit tree` cleanup). The forced-fail-closed scenario sets a relative
`SHIPIT_TREES_ROOT` for that one spawn only, so it creates nothing.
