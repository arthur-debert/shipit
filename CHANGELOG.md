<!-- generated - do not edit; fragments live in CHANGELOG/ (`shipit changelog render` regenerates this file) -->

# Changelog

## Unreleased

## 1.2.4 - 2026-07-18

- The **`agy` local reviewer works again** (#1006). It has been pinned to Gemini
  3.5 Flash since #990, and Flash goes *agentic* in `agy`'s headless `--print`
  mode: instead of reviewing the diff it is handed, it narrates its hunt for one
  and never emits a verdict. Every `agy-local` run therefore settled `failed`.
  The reviewer was not slow or wrong — it was **absent**, and had been for days.
  What made it invisible is worth recording, because nothing here misbehaved: a
  required reviewer that fails is *degraded*, and the PR engine deliberately
  declines to let a degraded reviewer block Ready — otherwise one broken
  reviewer would wedge every PR in the repo. So PRs kept flowing, green, with
  codex and Copilot passing, while the roster promised three required reviewers
  and delivered two. Measured on this repo: `agy-local` failed on **every PR of
  the TREE03 epic**, roughly ten review rounds, without ever once blocking one.
  A check that fails loudly on every run reads, over time, as furniture.
  `agy` returns to `pro` (Gemini 3.1 Pro (High)). The ~20% review-speed win that
  #989's spike measured for Flash is given up **deliberately**: a reviewer that
  never returns a verdict is not faster than one that does, it is not a reviewer.
- Every repo now has **one Claude Code session store, shared by all its Trees**
  (#1023). Claude Code keys session transcripts *and* auto-memory on
  `~/.claude/projects/<slug>/`, where the slug is the session's working
  directory — so a Tree per session (ADR-0027) handed every launch a brand-new
  empty namespace. Memory was never broken; it was re-partitioned every session
  and never read back, and resume could not find a transcript from any directory
  but the one that wrote it. The cost was measurable: 44 memory files stranded
  across 23 throwaway stores, and the real store frozen since the day session
  Trees took over.
  There is no configuration knob for that path — the derivation is hardcoded in
  the harness — but the store is a plain path and a **symlink is honoured**. So
  `tree create` now plants `~/.claude/projects/<slug>` as a symlink to the repo's
  store *before the session starts*, and `shipit install` links the canonical
  checkout the same way, so work in a Tree and work in the plain checkout share
  one store rather than splitting in two. One symlink fixes memory and resume
  together. The store is keyed on the **origin remote**, not the path —
  consistent with how Tree scanning already resolves repo identity, precisely
  because a path "is not a reliable identity" — and lives at
  `~/.claude/stores/<owner>/<repo>/`, outside `projects/` so shipit-owned state
  is never confused with the harness's own directories. The store is not in the
  Tree and is never swept with one: reclaiming a workspace no longer destroys
  what was learned in it.
  Planting is a defined, idempotent algorithm rather than "link it", because the
  canonical checkout's directory is the hard case and the common one: it already
  exists, with real memories in it. Clobbering would destroy them and skipping
  would leave the store split in two forever, so: an already-correct symlink is a
  no-op (re-running install is free), an absent one is created, a **real
  directory is adopted** — its contents merged into the store, then replaced by
  the link — and a symlink pointing somewhere else is **refused loudly, changing
  nothing**, since something outside shipit owns that path.
  Adoption is a recursive merge over relative paths, not a move of top-level
  entries: a slug directory holds `memory/` on both sides, so the first collision
  is directory-versus-directory, and moving the top-level entry would rename the
  whole tree into a layout Claude will not read. Every (source, target) type pair
  has a defined outcome — identical files are dropped as duplicates, **divergent
  files keep both** under a non-colliding name (never overwritten, never silently
  dropped, never machine-merged), directories merge, and a *type* conflict at any
  path is refused with both sides left untouched while the rest of the merge
  carries on. Symlinks are adopted, never followed. Nothing is deleted from a
  source until its content is verified present in the target, and a directory
  that could not be fully drained is never replaced by the link: memory is
  irreplaceable, and a store left split is recoverable where a deleted memory is
  not. Planting is **serialized per store**, so two checkouts of one repo
  migrating at the same time cannot both claim one destination and have the
  second's copy land on the first's memory — and two runs against the *same*
  checkout cannot race either: the second re-reads the directory once it has the
  lock and finds the first's link already there, rather than acting on what it
  saw before waiting. A refusal touches nothing on either side, including the
  store directory itself.
  `shipit install` plants the link on **every** run except `--dry-run`, including
  one where the managed set is already current: the link is not a managed file,
  so a clean plan says nothing about whether it exists — and an already-installed
  checkout is exactly the one with a store to migrate.
  Both seams are **fail-open**: an unresolvable repo, an unwritable `~/.claude`,
  or no `~/.claude` at all (a CI runner, a container) costs a Tree or an install
  exactly nothing, and logs at DEBUG rather than warning on every single run —
  the store is additive, and without it a session merely keeps its memory to
  itself, which is the behaviour every session had before this existed.
- spawn: the reviewer SPAWNED payload's `tree` now reports the reviewer's ACTUAL
  per-Run read-only Tree, not a speculative coordinate (#1039). ADR-0074 made
  review Trees per-Run with a minted UUID, so the flat-leaf naming
  `_launch_reviewer` reported and the UUID `review/producer.provision_review_tree`
  minted independently could no longer agree by computation — `payload["tree"]`
  named a plausible path the reviewer never ran in. The spawn boundary now mints
  the flat-leaf naming ONCE and threads it down through the review service
  (`run_detached_review` → `generate_review` → `run_fanout_review` →
  `provision_review_tree`) via a new optional `review_tree_naming` /
  `naming` parameter (default `None` = "mint your own", so the review adapters'
  own re-review path and every other caller are unchanged), so the producer clones
  the reviewer under that exact id. Two reviewers on the same head still mint
  distinct namings upstream, so their per-Run Trees — and payloads — still differ.
- `tree gc` now **reclaims a Tree on measured activity rather than proxies for
  it**, closing a bug that deleted a live session's worktree (#1018). One rule
  decides every Tree kind — review, ephemeral, and write alike:

  ```text
  KEEP  if  dirty  ||  unpushed  ||  idle < 48h
  ```

  The three ladders this replaces read fifteen inputs between them — a pidfile,
  a `ps` probe, the PR's state, the Tree's kind, and four separate time windows
  — to answer one question none of them measured: *is anyone working here?* The
  ephemeral ladder answered it from the clone root's mtime, which does not move
  when an agent edits under `src/` (measured lag: up to **10 hours**), and its
  last rung read age alone — so a single liveness false-negative deleted a clean,
  live Tree. `idle` is now measured directly, as the newest of any file's mtime
  under a pruned walk and `HEAD`'s commit stamp, so both an agent editing files
  and an agent committing deletions are seen.
  **Unknown is never idle.** A `git status` or `git rev-list` that fails, a walk
  that hits an unreadable directory or finds no eligible file, a `stat` that
  raises — each one KEEPS the Tree and is reported. A wrongly-kept Tree costs
  disk until the next sweep; a wrongly-deleted one costs work that no longer
  exists. That asymmetry is the whole design, and it matters more now that the
  sweep is on its way to running unattended (#1017).
  **48h is deliberately above the observed band, not inside it.** Across a live
  fleet, idle time separates with no overlap: every live Tree measured under 1h,
  every dead one over 41h. A Tree idle 41–48h simply waits for the next sweep,
  while the margin over the busiest live Tree stays 48×.
  The walk that measures this **prunes** `.git`, `.pixi`, `node_modules`,
  `target`, `.venv`, `dist`, `build`, and `__pycache__` — `.pixi` alone is ~97%
  of a Tree's file count, and unpruned the walk would cost more than everything
  it replaces. Measured across a live 155-Tree fleet: **6.8s end to end**, at
  ~7ms per Tree versus ~425ms unpruned.
  Acquiring a shared read-only review Tree now records activity, because a
  reviewer only ever *reads*: refreshing an already-current Tree rewrites no
  file, so an aged shared Tree handed to a reviewer could be reclaimed out from
  under the review that was using it.
  `--threshold` now sets the idle boundary. The `stale` bucket is gone: with one
  rule there is no ambiguous middle for a human to adjudicate.
- **`tree gc` now makes ZERO network calls, and the dead reclaim machinery is
  gone** (#1022). ADR-0072 replaced the liveness-and-PR-state reclaim ladder with
  one activity-based rule (`KEEP if dirty || unpushed || idle < 48h`); the earlier
  work left that rule reachable but the machinery it superseded still on disk. This
  removes it, with no change to the reclaim rule itself:
  - `session/liveness.py` (the pidfile, the `ps`/`jc` fork, the create-time
    tolerance, the argv host allow-list) and `tree/provision.py` (the pre-pin
    provisioning-commit record reader) retire, along with their tests. The
    `SessionStart` hook no longer writes a pidfile and the `WorktreeRemove`
    fast-path teardown no longer reads one or carves out provisioning commits — its
    never-lose-work floor is now exactly gc's own (dirty or unpushed).
  - **The entire `gh` network dependency leaves the Tree scan.** The per-repo
    `PrIndex` batch that fed a signal reclaim no longer reads is deleted, so
    `tree gc` (and `tree list`, which shares the scan) reads only the local
    filesystem and `git`. On the largest fleet ever observed this was the
    difference between a >10-minute sweep and a ~22-second one; the cost was the PR
    read, and it is gone. A test asserts the gather makes no `gh` call.
  - `tree list` drops its **PR** column with the `gh` read; `TreeRecord` no longer
    carries `pr`/`pr_state`. The stale bucket, the per-kind gc dispatch, and the
    unreachable `live_reviews` review-Tree rung are gone with the ladder.
  - Net change is a deletion of roughly 2,000 lines across source and tests.
- **TREE03 planning docs land: the Tree gets rethought** (#1020, epic #1019).
  Running Trees
  for a while exposed three failures with one root cause — the system infers
  what it could measure, and encodes in paths what it then refuses to trust.
  `tree gc` deleted a **live** session's worktree (#1018); session memory has
  been silently discarded since Jul 6 (44 files stranded across 23 throwaway
  stores); and the directory hierarchy is written on create and ignored on read.
  Three ADRs record the decisions, and `docs/spec/tree-rethink.md` is the
  authoritative Spec:

  - **ADR-0072 — reclaim is activity-based.** One rule for every Tree kind:
    `keep if dirty || unpushed || idle < 48h`, where idle is measured
    newest-file mtime over a pruned walk. Supersedes ADR-0027's five-rung
    ladder and the pidfile liveness beneath it. Across the live fleet, idle time
    separates with no overlap — every live Tree under 1h, every dead Tree over
    41h — so the threshold sits in a chasm, and the apparatus that existed to
    manage an ambiguous middle (a `ps`/`jc` probe, a PR-state network read, four
    tunable windows) is deleted rather than fixed.
  - **ADR-0073 — the session store is per-repo, not per-Tree.** Transcripts and
    memory are keyed on the session's cwd, and a Tree per session means a new
    empty namespace every launch. One store per repo, linked into place at
    tree-create, fixes memory and resume together.
  - **ADR-0074 — Trees are flat.** `<root>/<repo>-<agent>-<timestamp>-<id>`,
    one uniform shape. No ADR ever chose nesting: it was inherited from the
    branch grammar, which is slashed for a git ref-collision reason that has no
    filesystem analogue.

  Docs reconciled with the new model: `docs/dev/naming.lex` gains a §4 for the
  flat Tree-directory grammar, `CONTEXT.md` gains **Reclaim** / **Idle** /
  **Session store** and drops read-only-Tree sharing, and both
  `docs/dev/epics.lex` §7 and the coordinator role stop telling every session
  that its memory is doomed — memory now persists, so learnings get promoted to
  the repo because the repo is how knowledge reaches reviewers, not because
  memory leaks.
- tree: Trees are now **flat and self-describing** — one directory per Tree named
  `<repo>-<agent>-<timestamp>-<id>` under the central root, replacing the five
  nested `<owner>/<repo>/<kind>/[<code>/]<leaf>` shapes at two depths (ADR-0074,
  #1025). Repo leads the name so `ls | grep <repo>` is the tooling-free narrowing
  the hierarchy promised; `<agent>` is the backend binary (`claude`/`codex`/`agy`),
  minted once from the backend registry rather than smuggled in as a session-id
  prefix; `<timestamp>` (`%Y%m%d-%H%M%S`) gives `tree list` its first real
  **created** column; and `<id>` is a full UUID — never a pid, never truncated. Its
  provenance follows the creator: a coordinator session Tree carries the harness
  session UUID from the `WorktreeCreate` payload (so the dir name IS the
  `claude --resume` handle), while every spawned-Run and native-helper Tree mints
  its own. The `<kind>` and `<owner>` segments and `tree_kind()` are gone (reclaim
  is one uniform activity-based rule since ADR-0072, and repo identity comes from
  the origin remote); `session/current.py` now resolves a Tree from cwd with no
  depth arithmetic; and `resume.py` reads the backend from a recorded field instead
  of reverse-engineering it from the id prefix.
- tree: **review Trees are per-Run**, not shared. ADR-0018's read-only *mode*
  stands — a reviewer still gets a chmod'd read-only clone — but the deterministic
  `(repo, branch)` sharing is dropped along with its reuse/refresh/acquisition-stamp
  machinery: each reviewer Run gets its own flat Tree, dated by its own files like
  every other Tree (#1025). Old nested Trees are not migrated — they are reclaimed
  by attrition and coexist with the flat shape (`registry.scan` walks for `.git`
  markers and never parsed depth). Branch names are unchanged.

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
