# Fundamental skills project into the discovery dirs as copies

> The behavioural change lands in `install/units.py` `load_units()`: the skill
> loop stops emitting `.shipit-skills/` dests and emits two whole-file units per
> store file instead — `.claude/skills/<rel>` and `.agents/skills/<rel>`. This
> ADR records the doctrine that shapes it. Slice 1 (issue #1088); the
> everyone-but-claude split is slice 2 and is **not** decided here.

## Context

`shipit install` shipped the fundamental skill **store** (`.shipit-skills/`)
into consumers as whole-file managed units — but **no agent runtime reads that
directory**. Claude Code loads skills from `.claude/skills/`; agy/codex load
from `.agents/skills/`. So a consumer's `shipit install` produced an inert
dead-drop: a folder of `SKILL.md` files nothing invokes. The projection step
that makes skills actually loadable was never implemented in `install`; in
shipit's own repo it existed only as hand-committed **symlinks** at
`.claude/skills/*` and `.agents/skills/*` pointing into `../../.shipit-skills/`,
and symlinks do not propagate to consumers through the managed-unit machinery.

## Decision

**Every fundamental store skill projects to both discovery dirs, as copies.**
`load_units()` reads `skills_root()` (unchanged) and emits, per store file, two
`Unit(kind="file")`s with distinct keys: `dest=.claude/skills/<rel>` and
`dest=.agents/skills/<rel>`. It stops emitting `.shipit-skills/` dests entirely.
Three doctrinal points make this the shape it is:

### `.shipit-skills/` is source-only

The store is shipit-self's human-edited skill source, read by `skills_root()`.
It is **never a shipped consumer dest** — shipping it (the prior behaviour) put
skill files where nothing loads them. Only the projected copies under the two
discovery dirs are managed units in a consumer.

### Projection is copies, not symlinks

This is **forced**, not a preference: the managed-unit writer is bytes-only
(`apply.py` `write_bytes`), and reconcile explicitly excludes symlinks from its
model (`reconcile.py` `retired_actual_hash` treats a symlink as a non-pristine
`"symlink"` sentinel). A managed unit's desired content is bytes with a
`sha256:` hash; a symlink can never hash to that, so a link would perpetually
reconcile as an OVERRIDE. Duplication is cheap (small markdown) and is paid at
the projection, not the source — the same content lands under both discovery
dirs.

### No `retired-files.toml` entries for `.shipit-skills/*`

Retirement is content-hash **global** (`reconcile.py` `decide_retired`): a
retired path is deleted from a consumer wherever its content matches ANY pinned
pristine hash. In shipit-self, `.shipit-skills/*` **is** the pristine source
tree — so a retirement entry keyed on that content would delete shipit's own
source on the next self-install. Therefore we add **nothing** for
`.shipit-skills/*`. The existing `skills/*` retirement records (the #921 store
move) are untouched; they retire the pre-#921 `skills/<rel>` location, which
never collides with the new `.claude/skills/` / `.agents/skills/` dests.

The cost is **orphaned residue**: a consumer that already installed the old
`.shipit-skills/*` copies keeps them after upgrading — they are no longer
managed, and with no retirement entry they are not swept. This is **accepted,
documented residue**, the same precedent as the pixi-block residue noted in
`units.py`. `[managed]` drops the stale `.shipit-skills/*` keys automatically on
the next install (`apply.py` re-stamps from the current decisions only), so the
residue is inert — untracked, unmanaged, and never re-proposed.

## Consequences

- All Q4 reconcile guarantees carry over unchanged, because the projected units
  are the same `Unit(kind="file")` shape as the agent-defs fan-out: consumer
  edit → OVERRIDE (flagged with a diff on the PR), retired skill → DELETE,
  re-install idempotent.
- **Managed markdownlint now lints the skills at two live dests in every
  consumer.** The managed `.markdownlintignore` deliberately does not exempt the
  skills tree; the fundamental store markdown must pass the gate at its source so
  it passes at both projected dests. The non-exempt stance is kept.
- **shipit-self's own `.claude/skills/*` and `.agents/skills/*` become real
  copies**, not symlinks — a byte-for-byte dogfood of what a consumer receives,
  and the precondition for the reconcile-to-noop drift test. `.agents/skills/pixi`
  stays an unmanaged real file (not projected in slice 1).
- **Deferred to slice 2:** the everyone-but-claude split (a per-surface
  targeting source of truth so `pixi` can ship agents-only), which is why slice 1
  projects every skill to both dirs unconditionally.
