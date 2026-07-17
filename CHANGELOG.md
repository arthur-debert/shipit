<!-- generated - do not edit; fragments live in CHANGELOG/ (`shipit changelog render` regenerates this file) -->

# Changelog

## Unreleased

- `tree gc` now **reclaims a merged Tree without waiting out the age
  threshold** (#1009). The write ladder gated on age BEFORE it looked at the PR,
  so a Tree whose PR merged days ago — clean, nothing unpushed, the work safely
  on the remote — was kept purely because its directory mtime was under the
  two-week boundary. At a real merge rate that parks a fortnight of finished
  work: measured over a 503-Tree fleet, **421 Trees were kept by the age gate
  alone** while exactly one had a PR in flight, and the `kept: 500` the verb
  reported read as "500 Trees in use" when it only ever meant "500 Trees are
  recent". The gate was measuring throughput, not use.
  A merged PR is now decided FIRST, held only until the Tree has been **idle for
  12h**: the merge already proves the loss is safe, and the window covers the one
  thing age was really buying — a write Tree has no liveness signal (unlike an
  ephemeral session Tree, which has its pidfile), so an agent may still be
  working in a still-clean Tree whose PR has merged. That window's clock is time
  since the Tree's last local write, NOT time since the merge: what it needs to
  know is whether anyone is still working in the Tree, and idleness is the
  available proxy. Hours of idleness instead of weeks of age closes that hole
  without parking the fleet. This brings the write ladder in line with the
  ephemeral one (ADR-0027), which already checked the merge ahead of its liveness
  and age rungs.
  `--threshold` (14d by default) is unchanged and still governs the **unmerged**
  shapes — no PR, or a PR closed without merging — where age remains the only
  abandonment signal, and those still land in `stale` for a human rather than
  being deleted. Every never-lose-work guarantee is untouched: a dirty tree,
  unpushed commits, an unreadable commit list, an in-flight PR, or an unreadable
  PR state all still keep, whatever the Tree's age or merge state.

## 1.2.3 - 2026-07-16

- The managed bootstrap scripts (`bin/setup-dev-env.sh`, `agent-start`) now
  resolve their **repo root symlink-safely** (#994). `cd -P` resolves every
  path component physically, so a symlinked intermediate `bin` sent `..` to the
  LINK TARGET's parent, and a symlinked script path (`~/bin/agent-start` → the
  checkout's copy) was never followed at all — both landed outside the
  checkout, provisioning the wrong repo or rooting a coordinator session in it.
  Each script now follows its own link chain first — joining relative link
  targets against the directory physically holding the link, as the kernel does
  — then resolves the final directory logically. Every resolution step is
  fail-open: a missing or erroring `readlink`, or a `cd` into a directory that
  is gone, warns and uses the path as-is instead of aborting the script or
  silently degrading to a bare `.` root.
- `install --pr` now **returns the operator to their branch** when the
  reconcile adds a new managed path (#993). The reconcile commit is built on an
  isolated scratch index (#992), so a newly written managed file — the
  `.shipit-skills/` skill store, a fresh agent definition — sits on disk while
  the checkout's real index has never heard of it. Git refuses to switch away
  from a branch whose HEAD carries an untracked working-tree file
  (`error: The following untracked working tree files would be removed by
  checkout: .shipit-skills/…`), so the best-effort branch restore only logged
  the failure and left the operator sitting on the `shipit/install` scratch
  branch — the exact strand the #777 restore exists to prevent. (The pushed PR
  and the exit code were always correct, so scripted fan-out was unaffected.)
  The restore now stages the newly ADDED managed paths — and only those — into
  the real index immediately before the switch, so they are tracked and the
  checkout is a plain branch change: the added path is dropped, the reconcile
  stays in the PR, and the operator lands back on their branch. Whatever the
  operator had STAGED survives the flow untouched: a managed path they already
  track is deliberately left alone, and so is one they had staged for deletion
  (`git rm --cached`) — neither is a path the reconcile added, and staging over
  either would destroy index-only work with no commit to recover it from.

## 1.2.2 - 2026-07-15

- Local **AGY reviews** are faster: the `agy` reviewer backend now runs through
  a native reviewer agent on Gemini 3.5 Flash, cutting local review latency
  (#989, #990).
- `install --pr` reconcile now reliably **publishes retired-file deletions**
  (#991, #992). The MODE_PR commit previously staged deletions into the index
  with `git rm --cached` but then committed with a pathspec `git commit --
  <paths>` — git's partial-commit mode, which builds the tree from the WORKING
  TREE of the named paths and disregards the index, silently negating the
  staged deletions. A retired file whose working-tree copy survived reappeared
  in the commit, so every reconciled consumer kept stale files alongside their
  replacements — most visibly the `skills/` → `.shipit-skills/` skill-store
  move, where all 11 retired `skills/*` files were left behind. The reconcile
  commit is now built on an **isolated scratch index** seeded from
  `origin/<base>` (`read-tree`) into which only the managed paths are staged —
  writes via `git add`, retired-path deletions via `git rm --cached` — and
  published with a whole-index commit. This honors the deletions, keeps the
  scoping that excludes unrelated dirty consumer files, and is correct
  regardless of the Tree's cut point (a stale `shipit/install` head can no
  longer squash unrelated commits into the reconcile), all while leaving the
  operator's real index untouched.

## 1.2.1 - 2026-07-15

- `install --pr` builds its reconcile commit from an authoritative
  apply-recorded touched-set instead of a hand-enumerated "commit universe"
  (#986, the design fix behind #852/#984). `apply()` now records the exact set
  of shipit-owned paths it is responsible for AS it mutates — managed unit
  destinations (NOOP units included), the re-stamped `.shipit.toml`, a
  re-rendered `CHANGELOG.md`, rewritten hook files, and every non-KEEP retired
  path — and the MODE_PR commit publishes exactly that set, scoped to the paths
  that actually carry a staged diff against `origin/<base>`. Retired-path
  deletions are staged from the index with a new `git rm --cached
  --ignore-unmatch` primitive, so a retired file's absence is published as a
  deletion without ever touching the working tree: a consumer file that
  reappeared at a retired path is preserved, a NOOP retired path is never
  unlinked, and an absent or untracked path can no longer crash PR generation.
  This removes the per-category carry/skip rules (`Plan.retire_carries` and the
  universe enumeration) whose asymmetries drove the seven-round #984 whack-a-mole,
  closing the consumer-edit leak surface by construction.

## 1.2.0 - 2026-07-15

- `shipit repo new --stack rust <name> [parent]` creates a new local Repo
  with a complete, verified, shipit-managed baseline (GEN01, #944): it
  scaffolds a two-crate Cargo workspace (a `<name>` CLI over a `lib<name>`
  library), applies the managed install baseline, resolves the pixi lockfile,
  and certifies the Repo by running its lint, test, and build Checks — staging
  the whole tree in a sibling and publishing it with one atomic rename only
  after every Check passes, so a single initial commit lands on `main` and any
  failure leaves the destination untouched. `--stack` is repeatable for future
  multi-toolchain Repos but v1 supports one profile, `rust`. Creation is local
  only — it creates no GitHub repository, remote, or release policy, keeping it
  distinct from `shipit install`, which adopts and reconciles an existing
  repository. See `docs/spec/repo-new.md` for the exhaustive contract.

## 1.1.1 - 2026-07-14

- The standing sign e2e (#899): `shipit wf verify-canary` dispatches
  shipit-canary's blessed release caller through the full sign proof matrix
  on live GitHub — the composed `stage=full` chain (sign+notarize on a real
  macOS runner, the #873/#889 class) and the staged
  `prepare`→`build`→`sign`→`publish` relay (the real cross-run artifact
  hand-off, the #898 class) — watches every run to its verdict, prints the
  proof-citation and teardown blocks, and exits green only when every run
  is. The workflows.lex §9 runbook makes citing both green chains mandatory
  for any PR touching the sign/relay/wf-yml surface, and names the exact
  canary-side surface (signed darwin-arm64 artifact, blessed caller, the
  owner-pushed Apple secret set) the proof rides on.
- Provision the `tree-sitter` CLI on release runners (#890, closing the
  TOL02-WS17 provisioning inventory's open hole 7): `shipit install` now
  delivers a managed `pixi.toml#shipit-tree-sitter-release-deps` block
  (conda-forge `tree-sitter-cli`, pinned `0.25.*` in parity with the grammar
  consumer's devDependency line) whenever a repo declares a tree-sitter
  `[toolchains]` leg — no manifest signals a grammar, so the declaration is
  the signal, the same union mechanics as the wasm-pack→node-deps delivery.
  A pixi-managed builder missing at `shipit build` now fails naming the
  install reconcile that provisions it, instead of a bare not-found note.

### Fixed

- Standalone `wf-build` dispatches are now a relay-complete source run for the
  sign/publish stages: a new standalone-only `notes` job re-derives the
  `release-notes` artifact at the tag via the new read-only
  `shipit release notes` verb, so a staged chain whose sign/publish names a
  build run as its source no longer fails `carry-notes` with
  `Artifact not found for name: release-notes` (#898).

## 1.1.0 - 2026-07-13

- lanes: declared-secrets seam — a per-lane `secrets` allowlist routes one
  scoped token into a wf-checks lane, gated routing-only in the block so a
  private-source test surface can move onto a managed lane (#778)
- install: self-cert now gates shipped skill content against the delivered
  markdownlint config, so the managed set can't ship content that reds a
  consumer's lint gate (#777)
- install --pr: flow-robustness — restore the caller's branch, pre-clean stale
  lefthook `.old` hook backups before activation, and a transactional
  fail-closed that rolls back a half-applied write on self-cert failure (#777)
- wf-checks: document lane self-provisioning as the sanctioned rule for
  submodule- and system-dep-dependent suites (provision in the lane's own
  `run` task, not via a block knob) (#759)
- managed-content: qualify the adoption.md pointer and align the spec
  placeholder surfaced by consumer reviewers (#781)
- release: electron bundle composition + dmg/AppImage integrity tiers
  (TOL02-WS14, #790)
- release: tauri-cli bundle composition — darwin .app/.dmg + linux
  .AppImage/.deb (TOL02-WS15, #827)
- release: vscode-marketplace + open-vsx endpoints + per-target .vsix
  (TOL02-WS13, #789)
- release: tree-sitter composition + notify-downstreams cascade
  (TOL02-WS16, #792)
- release: wasm/npm build composition (wasm-pack → npm tarball)
  (TOL02-WS12, #788)
- release: wasm-pack mirrors the tarball's platform_independent guard (#828)
- sign: electron per-code-role JIT entitlements + top-level .app hardening
  (#829, #830)
- sign: validate reseal payload link targets in the mac-app leg (#812)
- review: `shipit pr review validate` + REVIEW_SCHEMA self-check (#826)
- RPE01: Role Profiles and Work Environments epic (#825)

## 1.0.0 - 2026-07-12

- First release of shipit as its own published artifact. The tag is the
  payload: consumers ride the `@v1` workflow refs (ADR-0010) and the git pin
  (ADR-0033); `advance-major` takes the floating `v1` branch over from this
  release on, retiring the manual branch-advance workaround.
- release: make the deb composition CI-viable — cargo-deb self-provisions
  through the managed pixi surface, the native triple-dir contract, and a deb
  tier in assert-bundle (#785)
- release: archive-leg mac codesign + notarize — raw darwin CLI binaries ride
  the same sign stage as mac-app bundles (TOL02-WS08, #800)
- release: per-stage dispatch — the wf-* stage blocks are self-sufficient
  standalone (plan facts re-derived at the tag when omitted), and the
  routing-only `stage` choice caller is the blessed consumer dispatch surface
  (TOL02-WS09, ADR-0054, #804)
- release: declare shipit's own release surface — the no-build `gh-release`
  artifact (the tag is the payload) plus the blessed stage-choice dispatch
  caller `shipit-release.yml`, cutting shipit through its own pipeline (#774)
- release: close the release-tool provisioning holes — rust (cargo-edit,
  cargo-deb) and twine ride the shipit-managed pixi blocks, uv joins the
  managed surface, a provisioning inventory + drift guard pins the set, and
  an unprovisioned tool fails loudly naming the install reconcile instead of
  installing at run time (TOL02-WS17, #797, #799, #803)
