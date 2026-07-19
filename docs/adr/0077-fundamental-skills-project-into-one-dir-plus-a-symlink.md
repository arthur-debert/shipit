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
  source (`.shipit-skills/` in shipit-self / the force-included wheel data).
- **`.claude/skills`** is a whole-directory **symlink** → `../.agents/skills`
  (relative, resolved from the link's own `.claude/` dir). It is **not** a
  content unit — a structural pointer `install` ensures, like lefthook
  activation. Committed/tracked, idempotent create-if-absent.
- **`.shipit-skills/`** stays source-only, unchanged.

### The everyone-but-claude split collapses, permanently

A whole-directory symlink means Claude sees **everything** `.agents/skills`
holds, including agents-only skills such as `pixi`. Per-surface targeting is
given up **by construction** — this is the accepted trade, not a deferral. There
is no slice 2; frontmatter surfaces / `pixi` vendoring are cancelled.

### Migration is path-scoped and pristine-checked — never content-hash retirement

Existing repos (and shipit-self, after the copies-both-dirs round) have a **real**
`.claude/skills/` dir of content today. Switching to the symlink requires
actively **removing** that real dir first — and it **must not** use
content-hash-global retirement. `.claude/skills/<x>/SKILL.md` is byte-identical
to `.shipit-skills/*` **and** `.agents/skills/*`, so a content-hash-global delete
would also nuke the source tree and the agents copy. That is the landmine.

Instead `install` runs a path-scoped, pristine-checked step
(`reconcile.plan_claude_skills_link`):

- `.claude/skills` **absent** → create the symlink (`LINK_CREATE`).
- `.claude/skills` a **real dir whose every file is shipit-pristine** → remove
  that dir and create the symlink (`LINK_MIGRATE`). "Pristine" is path-scoped:
  the file's content matches the current desired `.agents/skills/<rel>` content
  or a historically-shipped skill pristine (retired-files) — checked only for
  files **under `.claude/skills`**, and the removal touches only `.claude/skills`.
- **any consumer-modified file**, or a foreign symlink/non-dir at the path →
  **flag and skip** (`LINK_BLOCKED`): fail-safe, pull-not-push. Shipit warns and
  leaves the consumer's content intact; it never clobbers an intentional layout.

`apply` re-verifies over the gather→apply window before the destructive remove:
a file that changed or appeared aborts the migration. The removed pristine files
and the new symlink both ride the commit scope, so a committing mode publishes
the switch atomically.

### The symlink-write containment guard stays (defense-in-depth)

Whole-file reconciliation/writes still fail closed on a symlink in ANY
destination path component (`reconcile.symlinked_dests` /
`apply.reject_symlinked_dests`, every mode including `MODE_TREE`). Under this
design install never writes *through* `.claude/skills` — content goes only to the
real `.agents/skills` — so the guard does not trip in the happy path; it protects
the intentional symlink from accidental write-through, and it is a general
whole-file-unit guarantee.

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
  The `.claude/skills` migration is path-scoped (above); `.shipit-skills/*` is the
  pristine source, so a content-hash-global retirement would delete it. Orphaned
  `.shipit-skills/*` copies in already-installed consumers are accepted residue —
  `[managed]` drops the stale keys on the next install.
- **Managed markdownlint** lints the real `.agents/skills/*` (non-exempt stance
  kept). `.claude/skills` is a symlink to that same tree; it is exempted from the
  lint walk only if it is found to double-lint the identical files.
