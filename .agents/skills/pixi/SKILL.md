---
name: pixi
description: >-
  shipit's verified knowledge of pixi — the provisioning/run substrate shipit is
  a thin layer over. Covers what pixi persists, its identifiers, its (near-absent)
  extension surface, and how shipit's spawn/Tree/eval machinery wires through it.
  Use when working with pixi, Tree provisioning, environment activation,
  `pixi run`/`pixi install`, diagnosing "which env is this running in?" issues, or
  deciding how to integrate a shipit concern WITH pixi rather than around it — so
  you never have to rediscover pixi's model from scratch.
metadata:
  type: reference
---

# pixi — the substrate (quick reference)

shipit is a **thin layer over pixi**: build _on_ it, not around it, and never
reinvent what it already does. These are the load-bearing, **verified** facts;
the full reference (with `file:line` citations and a refresh procedure) is
**`docs/dev/pixi.lex`** (source of truth) / `docs/dev/pixi.md`.

> Verified against **pixi 0.71.0 (2026-06-30)**. pixi moves fast — these facts
> drift. If anything below smells stale, re-run the refresh checks in
> `docs/dev/pixi.lex §9` before trusting it. **Never answer pixi questions from
> training memory** — read the doc or the CLI.

## The five things to know

1. **pixi is the parent of provisioning + hooks — NOT the agent session.**
   `pixi install` provisions a Tree; every Codex hook fires as
   `pixi run shipit hook <name>`. But `shipit spawn subagent` launches `Codex -p`
   as a **bare subprocess** (not under `pixi run`), so pixi is absent from the
   agent's process tree.

2. **There is no pixi run-id / env-UUID / on-disk log.** pixi persists only
   _static_ state (envs by name; `.pixi/envs/<env>/conda-meta/.pixi-environment-fingerprint`
   = "provisioned & consistent with the lock"; `pixi.lock`). So **the only stable
   per-run correlation key is Codex's `session_id`** (the transcript
   filename). Join observability on shipit-owned keys + `session_id` — pixi gives
   you nothing to join on.

3. **There is essentially no extension surface.** No plugin API, no SPI, no event
   hook. The only integration points are `[activation]` (env/scripts pixi runs on
   every activation) and task `depends-on` — and **both only fire when execution
   flows through `pixi run`**. **Do not design a "pixi plugin."** Integrate by
   declaring env in `[activation]` and **routing execution through `pixi run`**.

4. **Known P0 bug: a spawned agent runs OUTSIDE its Tree's pixi env.**
   `child_env()` (`src/shipit/spawn/launch.py`) does zero activation and `cwd=<tree>`
   does not activate pixi — so the agent inherits the coordinator's (or system)
   env, and its `python`/`pytest`/`shipit` resolve to the **wrong** environment.
   Fix: launch through pixi —
   `pixi run --manifest-path <tree>/pixi.toml --clean-env Codex -p …`
   (`--clean-env` also closes the inherited-`PIXI_*` leak, #167).

5. **Only `pixi list` / `pixi info` have `--json`.** `pixi run`/`pixi install`
   emit human stderr only. `pixi shell-hook [--json]` prints the activation script
   (the bridge to activate a Tree env outside `pixi run`).

## When to reach for the full doc

Open `docs/dev/pixi.lex` when you need: the exact `.pixi/` on-disk layout, the
full CLI surface, the complete shipit↔pixi contract (provision/hook/launch
seams), the ranked "lean on pixi harder" list, or the refresh procedure after a
pixi version bump.
