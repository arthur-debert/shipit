# Zed extensions publish by a manually-gated registry PR; the tag is the release

shipit's release pipeline can build and package a Zed extension (a Rust crate
compiled to WASM, plus its committed grammar assets under `shared/`), but had
**no `zed` publish endpoint at all** — `src/shipit/release/publish.py` listed
Zed among the marketplace-class adapters that stay ABSENT until their repos
migrate. So `lex-fmt/zed-lex` cannot release through shipit (TOL03-WS02 #973).

The obstacle is that a Zed extension does not "publish" through an API we own.
It becomes available only when a pull request into **`zed-industries/extensions`**
— which bumps the extension's row in the registry's `extensions.toml` and
advances a git **submodule** to the newly-tagged extension source — is
**reviewed and merged by Zed's maintainers**. That is unlike every existing
endpoint: crates/npm/pypi/vscode/open-vsx are API pushes we authenticate; brew
pushes a formula to a tap *we own*. The registry is a foreign, review-gated
monorepo we neither own nor can push to directly. The posture therefore needs
deciding.

## Decision

**The tag is the release; the registry PR is a distinct, manually-gated step.**

- shipit does the part it owns end to end: the `zed` **composition** tarballs
  the extension's committed source — `extension.toml` plus the local `shared/`
  grammar assets (a **committed local copy — never a cross-repo grammar
  fetch**) — and the standard `gh-release` endpoint cuts the release. The
  **git tag `release prepare` creates is the authoritative release** (ADR-0041):
  the registry submodule points at `github.com/<owner/name>` at that tag.

- The `zed` **endpoint** is a **derived, stable-only, external, needs_repo**
  adapter that **renders the exact `extensions.toml` bump + submodule
  coordinates** (extension id, new version, source repo, tag) into the release
  output and **reports them** — the same render-vs-effect split `brew` uses,
  **minus the push**. It performs **no cross-repo write**, so it requires **no
  token**: `ENDPOINT_SECRETS["zed"] = ()`, like `gh-release`.

- Opening the PR against `zed-industries/extensions` — from a fork, with the
  submodule advanced — is a **human step**. shipit renders the coordinates so
  that step is mechanical and drift-free, but never fires it.

- Being **external** makes it RC-guarded: a `-release-rc` live-fire cut is
  `gh-release`-only, so a rehearsal never renders a registry entry. Being
  **stable_only** keeps a plain prerelease out of the registry (the registry
  serves stable versions), exactly like `brew`.

### Alternatives rejected

- **Registry-PR automation** — the endpoint forks `zed-industries/extensions`,
  advances the submodule, and opens the PR unattended on every release.
  Rejected: firing unattended pull requests into a foreign, review-gated
  monorepo is antisocial (registry churn, review-queue load) and fragile (fork
  lifecycle, submodule-must-point-at-a-pushed-tag ordering, PR de-duplication
  across resumes). The **merge is a maintainer gate shipit cannot own**, so
  automating everything up to it buys little and adds a cross-repo PAT and a
  brittle effect path. If the registry ever exposes an owned automation surface,
  this can be revisited as a wired-off-pending-token endpoint (the open-vsx
  precedent, #789) — the `secrets` seam is already in place.

- **A no-op / notify-only endpoint** — skip rendering, just gate on the
  release. Rejected: it leaves the maintainer to hand-assemble the
  `extensions.toml` row and submodule rev from memory, the exact drift the
  render forecloses.

## Consequences

- `lex-fmt/zed-lex` declares a `zed` bundle with its own payload (ADR-0077,
  #1092 — the extension states which files it ships: `leg = "rust"`, `payload =
  [{ path = "extension.toml", required = true }, { path = "shared" }, …]`) and
  `endpoints = ["gh-release", "zed"]`; a `-release-rc` cut produces the
  gh-release only (the RC guard skips `zed`), and a stable cut additionally
  renders the registry coordinates. Preflight/secrets derive the `zed` endpoint
  with **no extra secret** (only the ambient `RELEASE_TOKEN` for the prepare
  push), so a Zed-only repo provisions clean.

- The registry PR remains a documented manual step. This is the honest model
  for a dependency we do not own — the same "human validates and merges" gate
  the whole dev cycle already stops at, applied one hop downstream.

- Adding the endpoint is the closed-registry shape (ADR-0007/ADR-0064): a
  `bundle.COMPOSITIONS` entry, a `publish.ADAPTERS` entry, a
  `secretreq.ENDPOINT_SECRETS` entry, and the `config.ENDPOINTS` name — no
  derivation or planner code changes.
