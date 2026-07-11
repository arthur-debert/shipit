# TOL02-WS07 — lex rc cut: go/no-go note

The evidence workstream's closing note (issue #565, PRD story 48): one
`-release-rc` version of lex driven through the shipit release pipeline —
preflight → prepare → build → bundle → assert-bundle → publish — with
artifact inspection as the acceptance instrument. Evidence (Actions run
URLs, inspection output) is attached to #565 as comments; this note records
what was exercised, what was found, and what remains remote-unverified.

STATUS: FINAL — the composed pipeline ran green end-to-end on the hardest
rust consumer; verdict GO for the TOL02 pipeline leg, with a named
remote-unverified remainder owned by other epics (below).

## The cut

- Consumer: lex (`lex-fmt/lex`), campaign branch `shipit-rc`; version
  `0.19.4-release-rc` (tag-only, prerelease, live-fire — ADR-0041 semver
  suffix detection).
- Artifact map (authored on the campaign branch, the consumer-side
  declaration this run validates): two binaries off the one rust workspace
  — `lexd` (CLI; gh-release + crates + brew) and `lexd-lsp` (LSP;
  gh-release) — archive bundles, platforms darwin-arm64 / linux-x86_64 /
  linux-arm64 (native-runner lanes only, see gaps).
- Driven through the composed `wf-release.yml` graph (nested stage blocks,
  ADR-0040) — not a hand-assembled stage sequence. See finding 4 for where
  the block definitions resolved from.
- The two acceptance runs:
  - Green traversal:
    <https://github.com/lex-fmt/lex/actions/runs/29161944608>
  - Resumability re-dispatch:
    <https://github.com/lex-fmt/lex/actions/runs/29162247720>

## Stages exercised

Every stage below ran in ONE composed `wf-release.yml` dispatch (the
standard-consumer shape: one `uses:` line), the preflight plan driving the
fan — nothing re-derived in the blocks.

- **prepare** — cut `0.19.4-release-rc`, tag-only (branch ref never
  advanced), tag at commit `3a5d6dc`.
- **build** — the plan's matrix verbatim: 6 legs (`lexd` and `lexd-lsp` ×
  darwin-arm64 / linux-x86_64 / linux-arm64), each on its native runner.
- **bundle** — the `archive` composition per leg, narrowed to the matrix
  entry's artifact (`shipit release bundle --artifact`), emitting
  `<name>-<target>.tar.gz`.
- **assert-bundle (scar #2)** — wf-publish's unsigned-path assert, 6 legs,
  each inspecting the bundle's MAIN binary. All six:
  `assert-bundle: ok — main binary 'lexd'` / `'lexd-lsp'`.
- **sign** — SKIPPED (lex declares no mac signing; the unsigned path — see
  the sign-leg section).
- **publish** — `release publish complete [live_fire=True prerelease=True
  published=2 skipped=2]`: the GH release only, crates + brew dropped by the
  central RC guard.

Artifact inspection (the acceptance instrument, not CI green) — the
downloaded gh-release assets carry the right binary per target:

```text
lexd-aarch64-apple-darwin/lexd               Mach-O 64-bit arm64
lexd-x86_64-unknown-linux-gnu/lexd           ELF x86-64
lexd-lsp-aarch64-apple-darwin/lexd-lsp       Mach-O 64-bit arm64
lexd-lsp-aarch64-unknown-linux-gnu/lexd-lsp  ELF ARM aarch64
```

RC guard verified LIVE (against the registries, not job status): the GH
release `v0.19.4-release-rc` existed, marked **prerelease**, notes =
coalesced changelog, 6 assets (CLI + LSP × 3 targets); **crates.io** `lexd`
latest 0.19.2 (no rc), **npm** `@lex-fmt/lex-wasm` latest 0.19.3 (rc
absent), **brew** skipped by the guard (no tap repo exists to land in).

Resumability (ADR-0009) exercised: a second dispatch of the SAME version
converged — prepare `resumed [sha=3a5d6dc]` (re-emitted the SAME tag SHA, no
re-bump); publish updated the SAME release (databaseId unchanged) to 6
assets via `--clobber`, duplicating nothing.

After evidence capture the rc tag and prerelease were torn down (both 404);
the branch ref never advanced.

## Findings — fixed in shipit (PRD story 49: zero lex-side patches)

- **Finding 1 — multi-artifact repos failed the unsigned-path assert**
  (code fix, commit `ee6ea6c`): wf-build's bundle step ran the whole-map walk, so
   every `bundle-<artifact>-<platform>` tree carried EVERY artifact's binary
   and wf-publish's per-artifact `assert-bundle` could never pass on a
   multi-artifact map — lex's two-binary map is the first consumer to hit
   it. Fix: `shipit release bundle --artifact` narrowing; wf-build passes
   its matrix entry's artifact; drift test pins the step shape.

- **Finding 2 — assert-bundle went blind on the plain-archive unsigned
  path** (code fix, commit `d958bc2` — THIS PR): GitHub Actions artifact upload/download
   STRIPS Unix exec bits, and `check_tree`'s loose-executable discovery
   keyed off `st_mode & 0o111`. wf-publish's assert runs over a cross-job
   artifact, so lex's downloaded staging binary arrived non-executable and,
   with no `.app`/reseal-payload in a plain tarball tree, the check found
   "nothing to assert" and failed loudly on all six unsigned legs. Fix: a
   plain-archive tier in `check_tree` that reads the `.tar.gz`/`.zip` IN
   PLACE and finds the main binary by the exec bit preserved INSIDE the
   archive header (transport-proof) — the same no-extraction shape as the
   reseal-payload path; the loose scan stays the last-resort tier. The real
   CI-built darwin tarball is clean (only the main binary is executable), so
   the tier reads genuine pipeline output correctly.

## Findings — operational / campaign-branch (no shipit-code or lex-source change; named owners)

- **Finding 3 — `@v1` refs are unresolvable before the first stable shipit
  release** (anticipated by the brief): `advance-major.yml` only moves the floating
   major branch on a stable release tag, and shipit has never cut one, so
   the composed chain's nested `@v1` refs pointed at a ref that did not
   exist. Campaign bootstrap: `v1` pushed manually at the campaign head;
   advance-major takes over from the first real release. Owner: the first
   real shipit release (ADP02's standing gate).

- **Finding 4 — private shipit cannot be `uses:`-called from org-owned
  consumers, BLOCKING for the @vN distribution model** (owner decision, deliberately
   not settled here): lex lives under the `lex-fmt` org while shipit is
   private under the `arthur-debert` user. GitHub shares a private repo's
   reusable workflows within the owner namespace only (`access_level: user`
   reaches user-owned repos; there is NO credential mechanism for
   cross-owner `uses:`), and the pixi/uv git-dependency pin hits the same
   wall on org runners. ADR-0010's consumer model ("consumers pin
   `arthur-debert/shipit/...@v1`") and ADR-0033's private-with-credentials
   stance contradict each other for every non-user-owned consumer; legacy
   `arthur-debert/release` is public for exactly this reason. Decision
   needed: make shipit public, or scope @vN distribution to user-owned
   repos. Campaign consequence: the five wf-* blocks were VENDORED verbatim
   onto the lex campaign branch (nested `uses:` rewritten to `./` local
   paths — with the blocks in-repo, local refs resolve by construction) and
   the pinned shipit build rode a committed wheel; the composed graph, stage
   wiring, matrix fan, and every release verb ran unmodified, but the
   REMOTE-@v1-REF RESOLUTION LEG IS REMOTE-UNVERIFIED. Owner: repo owner +
   ADP02.

- **Finding 5 — shipit's Actions access level was `none`** — even same-owner
  repos could not call its reusable workflows. Set to `user` via the API. This
   publisher-side setting is managed by nothing today; candidate for the
   install/gh-setup surface. Owner: follow-up issue.

- **Finding 6 — the wf-* blocks' `pixi run --locked ./bin/shipit` contract
  meets the ADR-0033 uv launcher unprovisioned on runners**: the launcher never
  consults PATH and uv-provisions the pin, but the runner leg (uv install plus
  a credential for the private clone) is explicitly deferred to ADP02
   (ADR-0033 consequences). The rc run failed exactly there (`shipit: uv is
   not on PATH`, exit 127). Campaign bridge: the launcher's sanctioned,
   announced `SHIPIT_EXEC` override pointing at the pixi-provisioned pinned
   build (vendored wheel, installed as a `[pypi-dependencies]` path dep).
   Owner: ADP02's runner leg — the blocks and the launcher need one
   provisioning contract on runners.

- **Finding 7 — a stale `dist/` was committed on the campaign branch**
  (campaign hygiene): a prior LOCAL macOS `shipit release bundle` left darwin
  `lexd`/`lexd-lsp` bundle trees committed under `dist/`; wf-build uploads
  `dist/**`, so every matrix entry's bundle artifact carried these stale
  cross-artifact trees (with macOS AppleDouble `._` sidecars), and
  assert-bundle correctly saw the WRONG binary set. Fix: remove the tracked
  `dist/` (`git rm -r`) and gitignore it on the campaign branch (build output
  is never tracked). No
  shipit-code or lex-source change. Adjacent observation for adoption:
   shipit's managed install could add `dist/` to a managed `.gitignore` so a
   consumer cannot commit build output into the upload sweep — a candidate
   for the install surface, not fixed here.

- **Finding 8 — same-version wheel re-vendor was cache-poisoned** (campaign
  vendoring): the re-vendored wheel kept version `0.0.1`, so the pixi.lock
   hash was unchanged and setup-pixi restored a cached env with the OLD
   wheel — the fix never ran on the runner. Bumped the vendored wheel to
   `0.0.2` and regenerated the lock so the cache key changed and the fixed
   build installed. A vendoring detail of the private-cross-owner bridge
   (finding 4), not a shipit contract; the real @vN distribution model pins
   by immutable git sha and never hits this.

## Sign leg — coverage stated from lex's actual declaration

Lex declares NO `sign = true` in its artifact map, so the rc ran the
UNSIGNED path (sign job skipped by the plan's empty sign projection;
`assert-bundle` ran on wf-publish's unsigned branch — the ADR-0040
placement). Two reasons, both recorded rather than silently elided:

- lex's legacy pipeline signs+notarizes RAW mac binaries (Developer ID
  codesign of the bare executables); shipit's mac signer signs the coupled
  `.app`/`.dmg` unit (the mac-app composition) and has no raw-binary
  composition today.
- Per the PRD's testing decisions read against this: the `.app`/`.dmg`
  signer's REMOTE verification lands with phos-app in ADP02, where that
  shape actually exists.

## Not exercised / remote-unverified remainder

- Remote `@v1` cross-repo ref resolution (finding 4) — owner decision +
  ADP02. The composed graph, stage wiring, matrix fan, RC guard, and every
  release verb ran unmodified via the vendored-verbatim blocks; only the
  cross-owner remote-ref RESOLUTION is unproven.
- wf-sign-mac (no sign declaration on lex; the `.app`/`.dmg` signer's remote
  proof is owned by phos-app / ADP02).
- Cross-compiled lanes: darwin-x86_64, linux-musl, windows — `shipit build`
  has no `--target` plumbing (the matrix's target triple is naming-only
  today), and lex's pixi manifest has no win-64 platform. Legacy lex
  coverage included musl x86_64 + windows x86_64; a future stage WS owns
  cross-target builds.
- The wasm/npm leg (`lex-wasm` via wasm-pack → `@lex-fmt/lex-wasm`): no
  shipit build/bundle composition exists for it, so it is NOT declared in
  the campaign artifact map and no wasm tarball rode the gh-release. The
  legacy caller's `wasm-packages` leg has no shipit equivalent yet — a named
  gap for lex adoption (ADP02 blocker for lex until owned by a stage WS).
- crates/npm/brew adapters' LIVE dispatch (the rc guard's entire point: they
  were planned OUT of a `-release-rc` cut, and verified skipped against the
  registries); their live-fire proof is the first real release (ADP02's
  standing gate).

## Explicit assertions (issue ACs)

- lex's legacy CI callers (`release.yml` → `arthur-debert/release
  rust-cli.yml@v3`, `ci.yml`, `test-rust.yml`, `sandbox-tests.yml`) are
  UNTOUCHED and still running — the campaign added a NEW caller
  (`shipit-release.yml`) alongside them (cutover canon).
- release-core retirement is NOT claimed: the standing gate (retire only
  after shipit cuts one real release of one real consumer, ADP02 story 34)
  stays on ADP02 — an rc is not that release.

## Verdict

**GO for the TOL02 pipeline leg.** The composed `wf-release.yml` graph drove
one `-release-rc` of lex — the hardest rust-side consumer (one repo-level
version fanning into a multi-artifact, multi-platform Release) — through
preflight → prepare → build → bundle → assert-bundle → publish, green, with
artifact inspection confirming the right binary per target, the RC guard
verified live against the registries, and resumability converging on
re-dispatch. Two failures the rc surfaced were fixed in shipit
(findings 1–2, zero lex-side patches); the rest are named campaign-branch or
operational items with owners.

The remote-unverified remainder is real and NAMED, owned outside this WS:
the cross-owner remote-`@v1` distribution leg (finding 4 — a visibility
decision + ADP02), the `.app`/`.dmg` mac-sign leg (phos-app / ADP02), and
cross-target + wasm/npm build coverage (future stage WSs). None of these is
a defect in the pipeline this WS proved; each is a distribution/coverage
frontier owned by its epic. TOL02's pipeline is proven end-to-end on a real
multi-artifact consumer — the bar PRD story 48 set for closing the epic.
