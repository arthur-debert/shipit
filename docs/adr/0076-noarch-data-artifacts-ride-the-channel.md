# Cross-repo data artifacts ride the channel as noarch

ADR-0064 built the Artifact channel around **tool** artifacts (`lexd`,
`lexd-lsp`) — per-platform `.conda` packages keyed by target triple — and
deferred the portfolio's **data** artifacts (the wasm build, the tree-sitter
grammar) as "not a released artifact yet; lands later as `noarch`." The
owner's standing briefing (2026-07-18, shipit#1059) cancels that deferral:
**all** cross-repo dependencies ride the conda channel "in this fashion,"
data artifacts included. The grammar and wasm are no longer a separate
delivery problem waiting on a future mechanism — they are channel packages,
and the mechanism is the one ADR-0064 already shipped.

A data artifact is not arch-specific: `tree-sitter-lex`'s distributable is a
single platform-independent `tree-sitter.tar.gz`, and the wasm build is one
`.wasm` blob. conda has a name for this — a **noarch** package, published to
the channel's `noarch/` subdir, which every conda client reads alongside the
platform subdir it resolves. The channel already serves an (empty) `noarch/`.
The only missing piece is a producer mode that targets it.

## Decision

- **Cross-repo data artifacts are published to the Artifact channel as
  `noarch: generic` conda packages.** A *tool* artifact installs a binary on
  PATH; a *data* artifact installs its files into the env. The consumer side
  already draws no distinction — `[artifact-deps.<pkg>]` (ADR-0064) names a
  conda package, not an executable contract — so a data artifact is consumed
  by the same declaration a tool artifact is.

- **The mechanism is the existing `conda` derived endpoint, extended with a
  noarch mode** — not a new composition. The endpoint already repackages a
  final gh-release archive into a `.conda` (`rattler-build`; ADR-0064). Noarch
  mode changes only the recipe target and the destination subdir: a
  platform-independent artifact — one with a **tarball composition and no
  `platforms` list** (e.g. `tree-sitter-lex`'s `tree-sitter.tar.gz`) —
  repackages its **single** archive into one `noarch: generic` `.conda`
  published to `noarch/`, **instead of** keying a per-platform release asset
  to a per-platform subdir (`CONDA_SUBDIRS`,
  [src/shipit/release/publish.py](../../src/shipit/release/publish.py)). No
  consumer change is needed to resolve it — conda clients always read
  `noarch/` alongside their platform subdir.

- **Endpoints are additive.** A data artifact keeps the endpoints it already
  declares (gh-release, and npm for the wasm build) and **adds** `conda`
  alongside them. Nothing is removed producer-side; the `conda` endpoint is
  one more distribution target, exactly as ADR-0064 framed it.

- **Consumers migrate onto `[artifact-deps]`.** The three editor consumers
  that today fetch the grammar tarball bespoke — `lex-fmt/vscode`, `nvim`,
  `lexed` — retire their legacy cross-repo `fetch-deps`/`deps.json` fetch and
  declare the noarch grammar package via `[artifact-deps]`, staging its files
  into their bundle (the staging layout differs per editor; the declaration is
  uniform). `lexd-lsp` already landed this way (shipit#162).

- **`zed-lex` is excepted.** Zed resolves the grammar through its **native**
  grammar system — `extension.toml` `[grammars.lex]` names a repository +
  commit and Zed compiles from source — so its *runtime* grammar cannot come
  from conda without fighting Zed's design. Only `zed-lex`'s test harness
  pulls the tarball; it is out of the conda-consumer set for runtime.

- **Readiness is a single subdir, not a per-platform set.** ADR-0071's gate
  is the *served subdirs that are not owner-paused*, because a tool artifact
  fans out across `osx-arm64`/`linux-64`/`linux-aarch64`/`win-64` and a subdir
  can be paused (shipit#895). A noarch package has no such fan-out: `noarch/`
  is one platform-independent subdir with no `win-64` analogue to pause. A
  served data artifact is present when its `noarch/` package resolves — a
  single probe, not a per-platform sweep, and never subject to the pause
  subtraction.

### Alternatives rejected

- **A bespoke "data-artifact composition" built from scratch** — a separate
  assembler for tarball/wasm payloads. Rejected: the existing archive→`.conda`
  repackage already does the assembly; only the recipe's `noarch` flag and the
  destination subdir differ. A second composition would duplicate the endpoint
  ADR-0064 exists to be.

- **Keep the tree-sitter grammar on the legacy `fetch-deps` cross-repo
  fetch** — leave the one working data-delivery path in place. Rejected: that
  bespoke fetcher is exactly what ADR-0064 exists to retire, and keeping it for
  data while tools ride the channel **splits the dep-delivery mechanism** in
  two — the split the Artifact channel was built to end.

- **Per-editor bespoke handling** — let each of vscode/nvim/lexed keep its own
  fetch. Rejected: the three are uniform (identical `deps.json`/`fetch-deps`
  today), so three mechanisms would encode a difference that does not exist.
  The one genuine difference — `zed-lex`'s native grammar — is handled by the
  exception above, not by keeping the legacy path for everyone.

## Consequences

- **The noarch producer path must be built** — a small extension of the ARF02
  `conda` producer (recipe `noarch: generic`; publish to `noarch/`;
  served-set/gate handling that treats `noarch` as one always-present subdir),
  with a **real repackage test** that exercises an actual noarch build
  (shipit#1053's lesson: the conda producer path shipped untested and carried
  bugs).

- **`tree-sitter-lex` and `lex-wasm` gain a `conda` endpoint and seed.**
  `tree-sitter-lex` adds `conda` to `[artifacts.tree-sitter].endpoints`
  (keeping gh-release + `notify-downstreams`); `lex-wasm` adds `conda`
  (keeping npm). Every seed/release dispatch is owner-gated (shipit#1059).

- **The three editor consumers retire `fetch-deps` for the grammar** and
  resolve it as an ordinary pinned conda package via `[artifact-deps]` —
  locked and sha256-verified through `pixi.lock` like every other cross-repo
  pin, with transparent bumps (ADR-0067).

- **ADR-0064's and the spec's noarch deferral is removed.** ADR-0064's
  "tree-sitter grammar is not a released artifact yet … lands later as
  `noarch`" and `docs/spec/artifact-channel.md`'s Out-of-Scope deferral no
  longer describe the design; both are amended to point here. Data artifacts
  are in scope, and their shape is a noarch channel package.
