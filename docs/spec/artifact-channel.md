# Artifact Channel

## Context

Repos in the portfolio share build artifacts. `lex-fmt/lex` produces `lexd`,
`lexd-lsp`, a wasm build, and (planned) a tree-sitter grammar; these power
downstream nvim, vscode, and tree-sitter repos. The legacy release system
carried bespoke code to fetch these cross-repo binary dependencies from
releases. shipit has not replaced that: the pieces it does have solve adjacent
problems, not this one.

The existing domain model constrains this feature:

- An **Artifact** is a named, distributable build product, declared in
  `.shipit.toml` `[artifacts]` and published to **Distribution endpoints**
  ([ADR-0007](../adr/0007-repo-as-path-toolchain-map.md), the `[artifacts]`
  map).
- **Dependency mode** already names the two ways a downstream consumes an
  upstream (CONTEXT.md): **source-pinned** rebuilds from a ref/version;
  **artifact-pinned** fetches a released Artifact by version. This feature
  realizes the *artifact-pinned* mode, which had no mechanism.
- The **content-key** ([ADR-0008](../adr/0008-content-addressed-artifact-identity.md))
  is build-once reuse of CI outputs — a *different* store and concern from
  consuming released, tagged artifacts by version.
- A **Release** publishes an Artifact set to its endpoints via **endpoint
  adapters**; publish is barrier-then-resumable with a `release`-before-
  `derived` ordering ([ADR-0009](../adr/0009-partial-release-prevention-barrier-then-resumable.md)).
- pixi is the substrate; shipit builds ON it rather than reinventing it
  (`docs/dev/pixi.md`). pixi already resolves, locks, and sha256-verifies
  versioned dependencies from channels.
- `shipit provision lexd`
  ([src/shipit/provision/lexd.py](../../src/shipit/provision/lexd.py)) is the
  one existing cross-repo release-asset fetcher — hard-coded to a single tool
  because `lexd` is "not on conda-forge."

This Spec realizes what the legacy roadmap sketched as **WF06** (the cross-repo
cascade / artifact-pinned consumption), but via a **conda channel**, not the
content-key store the WF06 sketch assumed.

## Problem

A downstream repo cannot declare "I depend on `lexd-lsp@0.19.3` from
`lex-fmt/lex`" and have shipit deliver that released binary, update it
transparently on a pin bump, and verify its integrity. The only mechanism that
comes close — `provision` — is hard-coded to one tool, pins in the shipit
binary (not per-consumer), and does not generalize.

## Goals

- A downstream declares a cross-repo artifact dependency once, by name +
  version, and consumes it as an ordinary pixi dependency (locked,
  sha256-verified, cross-platform).
- A pin bump updates transparently — the way `npm install` / `cargo` update —
  with no bespoke fetch code.
- Support both **open** artifacts (lex — public) and **private** artifacts
  (phos), with the cheapest correct access model for each.
- Retire `shipit provision lexd` by making `lexd` an ordinary channel package.
- Cross-repo update propagation is **instant** on an upstream release.
- The producer side is one more endpoint adapter, not new release orchestration.

## Non-Goals

- Replacing the content-key store (ADR-0008) or CI build-once reuse.
- Consuming CI *build-job* artifacts (ephemeral) — this consumes **released**,
  permanent artifacts only (CONTEXT.md: Artifact channel).
- Serving Intel-mac (osx-64) or musl consumers — no conda subdir / no pinned
  asset, matching today's `provision` refusal.
- Publishing to marketplace-class endpoints (VS Marketplace, Open VSX) — those
  remain separate endpoint adapters.

## Proposed Shape

**The Artifact channel** (CONTEXT.md): the portfolio's durable, versioned store
of published Artifacts, realized as **per-repo conda channels** in dedicated
object-storage buckets, consumed by downstreams in artifact-pinned mode.

- **Producer — `conda` Distribution endpoint** (derived). After `gh-release`,
  repackage each final release asset into a `.conda` (`rattler-build`), push to
  the producing repo's channel, reindex. Thin adapter, mirrors `brew`.
  ([ADR-0064](../adr/0064-artifact-channel-conda-for-cross-repo-consumption.md))
- **Store — two dedicated buckets** (separate lifecycle from sccache):
  public-read and private. Per-repo channel roots inside each — each repo is
  the sole writer of its own `repodata.json`, so index races are impossible.
  ([ADR-0065](../adr/0065-artifact-channel-access-tiers-two-buckets.md))
- **Access tiers** — tier derived from the producing repo's visibility. Public
  = authless HTTPS. Private = GCS HMAC via the S3-compat interop endpoint,
  credentials as env vars (Doppler locally, sccache path in CI), never
  `pixi auth login`.
  ([ADR-0065](../adr/0065-artifact-channel-access-tiers-two-buckets.md))
- **Consumer declaration** — `[artifact-deps.<pkg>]` in `.shipit.toml`
  (`repo` + `version` + optional `feature`), projected by `shipit install` into
  a managed pixi block. The key names the artifact and its conda package; tool
  artifacts (`lexd`, `lexd-lsp`) install a binary on PATH, data artifacts (wasm,
  grammar) install their files into the env.
  ([ADR-0064](../adr/0064-artifact-channel-conda-for-cross-repo-consumption.md))
- **`provision lexd` retires** — `lexd` becomes a public-channel package; the
  gate pin moves to a managed, non-consumer-editable lint block.
  ([ADR-0066](../adr/0066-provision-lexd-retires-onto-the-channel.md))
- **Updates — push, derived fan-out.** On stable release, dispatch to the
  derived consumer set; each opens its own draft bump PR; `pixi.lock`
  re-resolves. rc published but never auto-bumped.
  ([ADR-0067](../adr/0067-artifact-pinned-updates-push-derived-fanout.md))

### Consumer example

```toml
# downstream .shipit.toml
[artifact-deps.lexd-lsp]
repo    = "lex-fmt/lex"
version = "0.19.3"
# feature = "lint"   # optional; default = default env
```

`shipit install` projects the channel URL, the `[dependencies] lexd-lsp =
"0.19.3"` pin, and (private tier only) the `[s3-options]` block into pixi; pixi
does the resolve/lock/fetch.

## Design Decisions

The load-bearing decisions and their trade-offs are recorded as ADRs:

- [ADR-0064](../adr/0064-artifact-channel-conda-for-cross-repo-consumption.md)
  — conda channel as the artifact-pinned mechanism; `conda` derived endpoint;
  per-repo channels; `.shipit.toml` declaration; versions not hashes.
- [ADR-0065](../adr/0065-artifact-channel-access-tiers-two-buckets.md) — two
  buckets; public-authless / private-GCS-creds; capability-URL rejected.
- [ADR-0066](../adr/0066-provision-lexd-retires-onto-the-channel.md) —
  `provision lexd` retires; gate uniformity via a managed lint block.
- [ADR-0067](../adr/0067-artifact-pinned-updates-push-derived-fanout.md) —
  push propagation with a derived fan-out.
- [ADR-0070](../adr/0070-publish-fires-a-selectable-endpoint-subset.md) —
  `publish --endpoint` selects a subset of endpoints; the Release stays whole.
  This is what makes the seed below safe.
- [ADR-0071](../adr/0071-the-readiness-gate-is-the-served-subdirs-that-are-not-paused.md)
  — the readiness gate is the served subdirs that are not owner-paused; amends
  ADR-0066.

### Seeding the channel

The channel starts empty, and the obvious seed is unsafe: one release event
fires every declared endpoint of every artifact, so a stable `lex-fmt/lex`
release publishes the `.conda` **and** `lexd`→crates.io **and**
`@lex-fmt/lex-wasm`→npm — two irreversible, owner-gated third-party publishes.
The `-release-rc` live-fire guard is no help either: it skips *every* external
endpoint, and `conda` is external, so a rehearsal tag never seeds. Hence
ADR-0070.

The seed is an ordinary release run with an endpoint selector:

```sh
# an ORDINARY prerelease tag — NOT the reserved `-release-rc` suffix, which
# would skip `conda` along with every other external endpoint.
shipit release publish --endpoint gh-release --endpoint conda
```

> **Not yet runnable.** `--endpoint` is ADR-0070's decision, implemented by
> ARF02-WS01 (shipit#1000). Until that lands the flag does not exist and this
> command errors out — the channel stays unseedable, which is the point of the
> workstream. Do not reach for a manual `rattler-build` + upload instead: that
> bypasses the endpoint under test and escapes the parity drift guard.

The Release stays whole (every artifact builds, signs, and lands on the GitHub
release — ADR-0009); only distribution narrows. `crates` and `npm` are recorded
in the plan as selector-skipped. `conda` is rc-inclusive (ADR-0064), so a
prerelease seeds the channel for pin-testing; the `provision` cutover's gate
still requires a **stable** `lexd` (§Risks And Rabbit Holes).

Preview with a plan-only run first: the plan is the safety surface, and it
names every endpoint that will fire before anything external happens.

## Alternatives Considered

Covered per-decision in the ADRs. Summary: the content-key store (wrong source
— build outputs, not releases), a bespoke/adopted release-asset fetcher
(reinvents pixi's dependency system), prefix.dev private hosting (~$60/mo), GH
releases as a channel (conda must own the path layout), one bucket with
prefix-scoped IAM (leak-prone under UBLA), a capability URL (leaks via
`pixi.lock`), and consumer-poll (not instant).

## Risks And Rabbit Holes

- **GCS S3-interop validated live**, but two pixi 0.71.0 bugs are load-bearing
  and must be re-checked on a pin bump: `pixi config set s3-options.*` no-ops
  (template TOML directly); `pixi auth login --s3-*` is unwired (use env vars /
  `RATTLER_AUTH_FILE`).
- **Asset-name drift** between releases — the endpoint must use the release
  stage's known asset names, not a scrape by pattern.
- **Release-time portfolio scan** for the derived fan-out needs a cross-repo
  read token; keep it bounded (reuse `fleetsweep`), do not let it become a
  fleet crawl per release.
- **Bootstrap/self-hosting** for `lexd`: seed the channel once before the
  `provision` cutover; `lex-fmt/lex` lints against its prior release's `lexd`.
  The cutover (ADR-0066 — delete `provision`, move the pin into the managed
  lint block) is **gated on that seed** and MUST NOT land before it. Once
  `provision` is gone and `lexd` is an ordinary managed conda dependency, every
  managed repo's `pixi install` / lint solve resolves `lexd` from the channel
  with **no fallback** (the clean cutover retains none — ADR-0066), so a solve
  against an unseeded channel fails closed. This binds shipit's own gate too:
  shipit dogfoods the managed lint block byte-for-byte
  (`tests/test_install.py::test_packaged_lint_env_agrees_with_shipits_own_manifest`),
  so the cutover commit cannot even pass shipit's own pre-commit lint hook
  (`pixi run -e lint lint` re-solves the lint env) until the channel serves
  `lexd`. Readiness gate — all three required before the cutover PR can go
  green:
  1. the public bucket exists (WS03 — `shipit-artifacts-public`). **WS03 shipped
     an idempotent provisioner, not a provisioned bucket** — it is an opt-in
     operator entrypoint needing the operator's own `gcloud` credentials, so it
     never runs in CI or `pixi run test`, and the closed workstream implies no
     live infra. The bucket exists only once a human has run it; verify with the
     probe below rather than assuming;
  2. `lex-fmt/lex` has published a stable `lexd` release through the `conda`
     endpoint, so its per-repo channel holds `lexd` for every served subdir
     **that is not owner-paused** (ADR-0071) — today `osx-arm64`, `linux-64`,
     `linux-aarch64`. `win-64` stays in the closed served set but is not
     produced while Windows is paused (shipit#895); it re-enters this gate when
     the pause lifts. Still no osx-64 and no musl subdir; and
  3. the channel serves `repodata.json` authless for **each** non-paused served
     subdir — repodata is per-subdir, so a single probe can miss a partial
     publish; repeat the check for all three (the snippet is copy-pasteable —
     any non-zero exit means the gate is not yet met; re-add `win-64` when #895
     lifts):

     ```sh
     host="https://storage.googleapis.com"
     for subdir in osx-arm64 linux-64 linux-aarch64; do
       curl -fsS "$host/shipit-artifacts-public/lex-fmt/lex/$subdir/repodata.json" > /dev/null
     done
     ```

  Until all three hold, the cutover stays blocked and shipit keeps provisioning
  `lexd` via the pinned fetcher so its own gate self-hosts.
- **Lock discipline:** a bump re-resolves only through `pixi install`/`update`,
  which rewrites `pixi.lock`; the downstream must commit the updated lock.

## Cross-Cutting Concerns

- **Secrets:** the `conda` endpoint declares its write-credential requirement
  (`secretreq.ENDPOINT_SECRETS`), synced by gh-setup and validated by
  preflight; private-tier consumers need read creds (env-var delivered).
- **CI:** publish rides the release workflow after `gh-release`; consumers
  resolve at `pixi install` — public authless, private via the sccache
  credential path.
- **Platform matrix:** triple → subdir map is closed (osx-arm64, linux-64,
  linux-aarch64, win-64); no osx-64, no musl subdir.

## Testing / Verification

- **Proven live in spikes** (rattler-build 0.68.0 + pixi 0.71.0): full
  publish→consume→bump→re-resolve loop on a `file://` channel; both tiers on a
  real GCS bucket (private resolve + correct no-creds negative; public authless
  resolve).
- **Unit (pure cores):** subdir/triple mapping, `.conda` name derivation, the
  `[artifact-deps]` parse and its pixi-block projection, the derived-fan-out
  computation.
- **Adapter (through the exec seam):** recorded rattler-build / upload / index
  invocations; the receive-workflow bump edit.
- **Endpoint parity:** the `conda` entry mirrors `ENDPOINTS` /
  `ENDPOINT_SECRETS` / the publish adapter registry (drift-guarded like the
  others).

## Out Of Scope

- The tree-sitter grammar as a shipped artifact (not yet released; lands later
  as `noarch`).
- Marketplace endpoints; Intel-mac / musl coverage; non-Release deploys.

## Further Notes

- Realizes the legacy **WF06** intent (cross-repo cascade) but via a conda
  channel rather than the content-key store the WF06 sketch assumed; the
  content-key remains a separate concern (ADR-0008).
- Glossary updated (CONTEXT.md): **Artifact channel** added; **Cascade**
  sharpened to one push with two mode-dependent effects.
