# Fundamental skills project into one real dir plus a whole-directory symlink

> The behavioural change lands in `install/units.py` `load_units()` (skill
> content is emitted ONCE, into `.agents/skills/<rel>`) and a new structural step
> `install` performs: `.claude/skills` is made a whole-directory symlink to
> `../.agents/skills`. This ADR records the doctrine. Issue #1088.

## Context

`shipit install` shipped the fundamental skill **store** (`.shipit-skills/`)
into consumers as whole-file managed units — but **no agent runtime reads that
directory**. Claude Code loads skills from `.claude/skills/`; agy/codex load
from `.agents/skills/`. So a consumer's `shipit install` produced an inert
dead-drop: a folder of `SKILL.md` files nothing invokes. The projection step
that makes skills loadable was never implemented in `install`; in shipit's own
repo it existed only as hand-committed symlinks that do not propagate to
consumers through the managed-unit machinery.

An earlier revision of this ADR projected the store into **both** discovery dirs
as byte-identical copies. The pivot below supersedes it: Claude needs the *same*
set agy/codex have, duplicated only because it reads a different directory — and
a symlink satisfies that without a second physical copy. Install writes one file
to one place.

## Decision

**One real managed dir, plus a whole-directory symlink.**

- **`.agents/skills/<rel>`** is the single real, managed, reconciled dir — the
  ONE place `load_units()` emits skill content. `skills_root()` stays the read
  source (`.shipit-skills/` in shipit-self / the force-included wheel data —
  **amended by #1115 below**: the store is now `src/shipit/data/skills/`,
  ordinary package data with no repo-root copy).
- **`.claude/skills`** is a whole-directory **symlink** → `../.agents/skills`
  (relative, resolved from the link's own `.claude/` dir). It is **not** a
  content unit — a structural pointer `install` ensures, like lefthook
  activation. Committed/tracked, idempotent create-if-absent.
- **`.shipit-skills/`** stays source-only, unchanged (**amended by #1115**: the
  store moved to `src/shipit/data/skills/` and the repo-root path is retired).

### The everyone-but-claude split collapses, permanently

A whole-directory symlink means Claude sees **everything** `.agents/skills`
holds, including agents-only skills such as `pixi`. Per-surface targeting is
given up **by construction** — this is the accepted trade, not a deferral. There
is no slice 2; frontmatter surfaces / `pixi` vendoring are cancelled.

### Adoption is create-only-when-absent — shipit never removes `.claude/skills`

`install` creates the symlink through a create-only-when-absent structural step
(`reconcile.plan_claude_skills_link`), and **shipit never removes an existing
`.claude/skills`**:

- `.claude/skills` **absent** → create the whole-dir symlink (`LINK_CREATE`).
- `.claude/skills` already the **exact managed symlink** → NOOP (`LINK_NOOP`).
- `.claude/skills` **anything else** — a real dir (the copies-round / pre-symlink
  layout), a real file, or a symlink pointing elsewhere → **BLOCK** in the
  fail-closed conflict idiom (`LINK_BLOCKED`), with guidance: *"`.claude/skills`
  already exists — remove it (relocate any of your own skills into
  `.agents/skills` first) and re-run to adopt the managed symlink."* Install
  deletes nothing and creates no link until the operator resolves it.

An earlier revision auto-removed a "shipit-pristine" real dir. That destructive
migration was **cut** (owner decision): a content-hash pristine check keyed on
`.agents/skills/*` + retired hashes is a content-hash-**global** notion (the
landmine — `.claude/skills/<x>/SKILL.md` is byte-identical to `.shipit-skills/*`
and `.agents/skills/*`), and the gather→apply window made re-verifying the
removal fragile. Refusing to remove anything dissolves that whole class: no
pristine check, no window re-hash, no `rmtree`, no `retired-files.toml` entry for
`.claude/skills/*` ever. Adopters (and shipit-self) do a **one-time manual
`rm -rf .claude/skills`** and re-run; **shipit-self was hand-migrated in this
PR** (its `.claude/skills` is committed as the symlink).

The gather→apply window is still handled without destruction: a CREATE planned
against an absent path re-checks at apply and stands down if the path is now
occupied — a NOOP if it is already the managed symlink, otherwise left untouched.

### The symlink-write containment guard stays (defense-in-depth)

Reconciliation/writes fail closed on a symlink in ANY destination path component
(`reconcile.symlinked_dests` / `apply.reject_symlinked_dests`, every mode
including `MODE_TREE`). It covers **every managed unit kind** — whole-file AND
block/splice: `write_unit` writes a block host via `dest.write_text`, which
follows a symlinked leaf or parent exactly as `write_bytes` does, so a symlinked
`AGENTS.md`, `pixi.toml`, or `.claude/settings.json` is the same containment
breach (overwriting a target outside the repo) and is refused identically — the
host being consumer-owned does not make the external write safe (#1088 review).
Under this design install never writes *through* `.claude/skills` — content goes
only to the real `.agents/skills` — so the guard does not trip in the happy path;
it protects the intentional symlink and every managed dest from accidental
write-through.

## Consequences

- **Install writes one file to one place.** No duplication; the managed set is
  `.agents/skills/*` plus the structural link. Q4 reconcile guarantees hold over
  the real dir: consumer edit → OVERRIDE (diff on the PR), retired skill →
  DELETE, re-install idempotent.
- **Claude and agy/codex share one physical tree.** A skill edit lands once;
  both runtimes see it. `pixi` (agents-only until now) becomes Claude-visible —
  accepted.
- **`.claude/skills` is a tracked symlink, not managed content.** It carries no
  `[managed]` hash; it is created/verified structurally each install and is a
  plan work-axis of its own (`nothing_to_do` accounts for a missing/real dir that
  must be linked, so a current managed set still gets the symlink; `changed_paths`
  carries it into the commit).
- **shipit-self dogfood:** `.agents/skills/*` are real files; `.claude/skills` is
  the whole-dir symlink → `../.agents/skills`; `.agents/skills/pixi` stays a real
  unmanaged file and is now Claude-visible through the link. The
  reconcile-to-noop drift test asserts this exact layout.
- **No `retired-files.toml` entry for `.claude/skills/*` or `.shipit-skills/*`.**
  shipit never removes `.claude/skills` (create-only-when-absent, above);
  `.shipit-skills/*` is the pristine source, so a content-hash-global retirement
  would delete it. Orphaned `.shipit-skills/*` copies in already-installed
  consumers are accepted residue — `[managed]` drops the stale keys on the next
  install. **The `.shipit-skills/*` half of this is AMENDED by #1115 below**; the
  `.claude/skills/*` half stands.
- **Managed markdownlint** lints the real `.agents/skills/*` (non-exempt stance
  kept) and needs NO `.claude/skills` ignore entry: `shipit lint` discovers files
  via `git ls-files`, which yields the whole-dir symlink as one non-`.md` entry,
  so the files under it are never enumerated through the link — no double-linting
  (verified).

## Amendment (#1115) — the source store moves under `src/`, and `.shipit-skills/*` IS retired

The "accepted residue" consequence above is **withdrawn**. It was accepted for a
specific reason, and that reason has been removed rather than worked around.

**What the residue cost.** Every consumer crossing the projection pin kept its
previously-delivered `.shipit-skills/*` files tracked and owned by nobody.
Dropping the `[managed]` keys does not delete files, so the repo ends up with two
divergent copies of each skill and the **unmanaged** one is the stale one — the
pre-#1066 copies still instruct the reader to run `shipit lint --fix` where the
current instruction is `pixi run lint --fix`. That is a dead mechanism that still
looks authoritative, read by the very agents these files exist to steer.
Confirmed hand-cleaned in two consumers (`lex-fmt/vscode`, `lex-fmt/mkdocs-lex`);
every portfolio repo but one was on the same path.

**Why it was accepted, and why that no longer holds.** A `retired-files.toml`
match is on CONTENT HASH and is GLOBAL — the pass has no producing-repo
exemption, and shipit dogfoods `shipit install` on itself. With the store at the
repo root, its own files ARE the pristine content, so `.shipit-skills/*` entries
would have made shipit delete its own source store on the next self-install.

The fix is subtraction, not a special case (a "not on the producing repo"
exemption in a mechanism whose whole value is uniformity was considered and
rejected). The store moves to **`src/shipit/data/skills/`**:

- the `[tool.hatch.build.targets.wheel.force-include]` entry
  `".shipit-skills" = "shipit/data/skills"` is **deleted** — `packages =
  ["src/shipit"]` already bundles the store as ordinary package data;
- the dual-root branch in `skills_root()` (bundled-Traversable else
  `parents[3]/.shipit-skills`) is **deleted** — one `shipit.data` resources
  lookup resolves identically in a wheel and a dev checkout.

The repo-root path is then live NOWHERE, so the eleven `.shipit-skills/<rel>`
entries in `retired-files.toml` are safe with no special-casing. Nothing else
about the design changes: `.agents/skills/<rel>` remains the one real dest,
`.claude/skills` remains the create-only-when-absent symlink with no retirement
of its own, and the retired pass keeps a **hand-edited** copy (content matching
no known shipped version) with a warning rather than destroying it.

The schema cannot express a rename, so this lands as a delete of the old path
plus the already-shipped unit at the new one — two facts linked by a manifest
comment and a test invariant.
