# The Artifact channel: artifact-pinned cross-repo consumption via per-repo conda channels

> **Amended by ADR-0076.** The **data** artifacts this ADR defers (the wasm
> build, the tree-sitter grammar — "not a released artifact yet … lands later
> as `noarch`") are no longer deferred: they ride the channel as `noarch:
> generic` conda packages through the same `conda` derived endpoint, extended
> with a noarch mode. See ADR-0076.

The portfolio shares build artifacts across repos: `lexd`, `lexd-lsp`, wasm,
and (later) the tree-sitter grammar — all produced by `lex-fmt/lex` — power
downstream nvim/vscode/treesitter repos. The legacy release system carried
bespoke code to fetch these cross-repo binary dependencies. shipit had no
general replacement: the **content-key** store (ADR-0008) targets CI
build-once reuse, `notify-downstreams` (the Cascade) only triggered rebuilds,
and `shipit provision lexd` was a single hard-coded tool. The glossary already
names the target concept — the **artifact-pinned Dependency mode** ("a
downstream fetches a released Artifact by version") — but nothing realized it.

## Decision

Realize artifact-pinned consumption as a **conda channel** — the **Artifact
channel** (CONTEXT.md) — reusing pixi's existing dependency machinery instead
of inventing a fetcher.

- An Artifact is published as a **versioned conda package** into the Artifact
  channel; a downstream declares it as an ordinary pixi dependency, so pixi
  resolves/locks/fetches it and a **pin bump re-resolves transparently**
  (`pixi.lock`, sha256-verified).
- **Producer side — a new `conda` Distribution endpoint**, alongside
  gh-release/crates/pypi/npm/brew. It is a **derived** endpoint (ADR-0009's
  `release`-before-`derived` ordering): it runs after `gh-release`, sources the
  **final release asset**, repackages it into a `.conda` (`rattler-build`,
  which indexes on build), pushes it to the repo's channel, and reindexes. A
  thin adapter over `rattler-build`, not a from-scratch packager — the same
  shape as `brew` (the other derived endpoint), which already consumes final
  release assets and already handles a private source repo.
- **Per-repo channels:** the channel root is the producing repo, so each repo
  is the sole writer of its own `repodata.json` and cross-repo index races are
  structurally impossible.
- **Dedicated buckets, separate from sccache:** the store is object storage
  (GCS) on its own lifecycle — a cache purge must never wipe published
  artifacts. Two tiers, two buckets (ADR-0065).
- **Consumer declaration in `.shipit.toml`:** `[artifact-deps.<pkg>]` names a
  `repo`, a `version`, and an optional `feature`, which `shipit install`
  **projects** into a managed pixi block (channel, pin, auth). The key doubles
  as the conda package name; a tool artifact (`lexd`, `lexd-lsp`) puts a binary
  on PATH, while a data artifact (wasm, grammar) installs its files into the
  env — the key names the package, not an executable contract. Ordinary
  conda-forge deps stay
  consumer-authored in pixi; only cross-repo artifact-pins live in
  `.shipit.toml`, because shipit must be able to reason about and bump them.
- **Versions, not commit hashes:** a conda channel resolves on *orderable*
  versions, and the git tag is the version authority (ADR-0041). Prereleases
  are published (**rc-inclusive**) for manual pin-testing.

### Alternatives rejected

- **The content-key store (ADR-0008) as the cross-repo source** — it keys CI
  build outputs for build-once reuse; consuming released, tagged, signed
  artifacts *by version* is a different concern, and pointing extensions at
  build-job outputs would be the wrong source.
- **A generalized release-asset fetcher, or adopting mise/aqua/cargo-binstall**
  — reimplements or bolts on a dependency + lockfile + transparent-update
  system pixi already has; the consumers are already pixi repos.
- **prefix.dev hosted private channels** — ~$60/mo for the portfolio's private
  repos; not justified when GCS is already in the project.
- **GitHub releases as the channel base** — conda derives package URLs from
  `<channel>/<subdir>/<filename>` and must own that path layout; release assets
  are a flat, uncontrolled namespace (and 302 to a signed host), so a release
  cannot *be* a conda channel.

## Consequences

- Feasibility proven live (rattler-build 0.68.0 + pixi 0.71.0): build `.conda`
  → channel → pixi resolve → run → version bump → transparent re-resolve.
- `lexd`/`lexd-lsp` package per-platform (osx-arm64/linux-64/linux-aarch64/
  win-64; **no osx-64 or musl subdir** — Intel-mac and musl consumers are
  unserved, matching today's `provision` refusal); **wasm is noarch**; the
  **tree-sitter grammar is not a released artifact yet** and lands later as
  `noarch` (its distributable is arch-independent source, not a compiled `.so`).
- The endpoint uses the release stage's **known asset names**, not a scrape by
  pattern (asset names have drifted between releases).
- **Content-key (ADR-0008) is orthogonal:** the `.conda` is derived from the
  release asset and keyed by the tag version, not the content-key.
- Access tiers, `provision` retirement, and the update mechanism are their own
  decisions — ADR-0065, ADR-0066, ADR-0067.
