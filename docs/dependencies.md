# Cross-repo dependencies

**This is a reset (2026-07-20). This file wins.** If any other doc, issue, PR, code
comment, or agent handoff disagrees with it, **that thing is wrong and out of date** — do
not rebuild the old model from it.

**A cross-repo dependency is a plain conda package. That is the whole idea.**

## In — this is all there is

1. **Channel** — one URL per producing repo. Derived from the repo name, never typed by hand.
2. **Name** — the conda package name. The same name everywhere.
3. **Architecture-dependent** (one package per platform) **or universal** (one `noarch` package). There is no third kind.
4. **Producing** — publish the package. A program goes in `bin/`; that is what makes it a program, because `bin/` is on `PATH`. Everything else goes in `share/<name>/`.
5. **Consuming** — name the producing repo, pin the version in your own `pixi.toml`. `pixi lock` resolves, pins, and verifies it. A wrong name or version fails on your laptop, before anything ships.
6. **Shipping files inside your app** — `shipit stage` copies them out of the environment.

## Out — these are dead

Per-dependency-type special-casing — a cross-repo dependency's payload does not depend on
what kind of thing it is · Cascade and any automatic cross-repo version bumping ·
`fetch-deps`, `deps.json`, `lex-deps.json`, and any download-from-a-GitHub-release step ·
readiness gates, served-subdir bookkeeping, `channel verify` · a `version` key in
`[artifact-deps]` · the same version written down in two places.

(Not in scope here: how a repo *builds and releases its own* artifacts — the `vsix` /
`deb` / `tauri` / `archive` bundle compositions are a separate, live concern. This file is
only about one repo depending on another repo's output.)

Anything that uses or even mentions those is wrong. The version lives in exactly one
place: the consumer's `pixi.toml`.

**Working example to copy: `lex-fmt/nvim`.** It takes both kinds — a per-architecture
binary it runs off `PATH`, and a universal data package it stages into the app.
