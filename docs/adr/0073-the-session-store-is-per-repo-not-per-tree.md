# The session store is per-repo, not per-Tree

> **Amends ADR-0027.** The session Tree gave every session an isolated *working
> directory*, which was the goal. It also, unintentionally, gave every session an
> isolated *memory and transcript namespace*, which was not. This ADR restores a
> single store per repo without touching the Tree-per-session decision.

Claude Code keys both session transcripts and auto-memory on
`~/.claude/projects/<slug>/`, where `<slug>` is the session's **cwd** with `/`
replaced by `-`. ADR-0027 gives every session a fresh Tree, hence a fresh cwd,
hence a **brand-new empty namespace on every launch**. Memory is not broken; it
is re-partitioned every session. Nothing is ever read back, and resume cannot
find a transcript from any directory but the one that wrote it.

## Context

The cost is measurable, and it is not small.

- **44 memory files are stranded** across 23 per-session stores under
  `~/.claude/projects/*-ephemeral-sess-*/memory/`.
- **The real store froze.** `~/.claude/projects/-Users-adebert-h-shipit/memory/`
  holds 33 files whose newest entry is **Jul 6** — the day ephemeral session
  Trees took over. Every memory written since went to a throwaway path.
- **The loss is demonstrable, not theoretical.** Two sessions
  (`sess-20260712-033739-26999`, `sess-20260712-182220-15276`) independently
  wrote the *same five* memories — `provisioning-doctrine.md`,
  `session-handoff-adp02.md`, `fleet-list-source.md`, … — because the later one
  could not see the earlier one's. What is stranded is not trivia; it includes
  `adp01-merge-cadence`, which records merge authority the owner personally
  granted.
- **21 of those stores now hold memory with no transcript at all** — the 30-day
  transcript cleanup reaped the `.jsonl` files and left the memory dirs as
  orphans.

The constraint is already documented as unfixable. `src/shipit/data/roles/coordinator.lex:15`
instructs every coordinator that session memory "is keyed to that Tree's
working-directory PATH … once the tree is gc'd, anything written there is
orphaned," and prescribes manually sweeping learnings into the repo before
ending. That is a workaround written around a bug, and it costs a coordinator's
attention at the end of every session.

**There is no configuration knob.** `~/.claude/settings.json` has no
memory- or project-path key; the slug derivation is hardcoded. No `CLAUDE.md`
or `.claude/` exists at any ancestor of the Trees root, so there is no
inheritance seam either.

**But the store is a plain path, and a symlink is honoured.** Verified against
Claude Code 2.1.212: pre-creating `~/.claude/projects/<slug>` as a symlink to a
shared directory causes the session to write its transcript **into the shared
target** rather than replacing the link — and a session started in one tree was
then **resumed from a different tree, recalling its prior context**. One symlink
fixes memory and resume together.

**Codex has no such bug and needs no change.** `~/.codex/memories/` is a single
global store; cwd is recorded *inside* the entry as `applies_to: cwd=…` —
metadata filter, not storage key. That is the shape Claude Code lacks and this
ADR emulates.

## Decision

**One session store per repo, shared by every Tree of that repo.**

- **Identity is the origin remote, not the path.** The store is keyed on
  `<owner>/<repo>` resolved from `origin` — consistent with `_repo_slug`
  (`registry.py:256-276`), which already resolves repo identity from the remote
  precisely because "the path shape … is not a reliable identity."
- **Location: `~/.claude/stores/<owner>/<repo>/`.** Outside `projects/`, so
  shipit-owned state is not confused with the harness's own cwd-slug dirs.
- **Tree creation plants the link.** `tree create` computes the deterministic
  slug for the Tree's path and creates `~/.claude/projects/<slug>` as a symlink
  to the repo's store, before the session starts. The slug is a pure function of
  the Tree path, so no coordination is required.
- **`shipit install` links the canonical checkout too**, so work done in the
  plain checkout and work done in a Tree share one store rather than splitting
  into two.

**Adoption is a defined, idempotent algorithm — not "link it".** The canonical
checkout's slug dir is the hard case and the common one: it **already exists as
a real directory with real content** (`-Users-adebert-h-shipit/` holds 33
memories and its transcripts). "Create a symlink" cannot express that, and the
two obvious readings are both wrong — clobbering destroys the content, skipping
leaves the store split in two forever. So the contract, for **any** slug dir,
generic and repo-agnostic:

1. **Already the correct symlink** → no-op. (Idempotence: re-running install, or
   re-creating a Tree, must be free.)
2. **Absent** → create the symlink.
3. **A real directory** → **adopt**: move its contents into the store, then
   replace it with the symlink. Adoption is content-preserving and never
   destructive.
4. **A symlink pointing somewhere else** → refuse, loudly, and change nothing.
   Something outside shipit owns that path and this ADR does not get to guess.

**Adoption defines its conflict semantics, or it is not a contract.** Moving
content in means filename collisions are certain, not hypothetical — the
measured stores already contain duplicate memory filenames across sessions
(`provisioning-doctrine.md` and four others exist in two different session
stores, with possibly diverged content). Therefore: a file that does not exist
in the target moves in. A file that does exist and is **byte-identical** is
dropped as a duplicate. A file that exists and **differs** is **kept, both
sides, under a non-colliding name** — never overwritten, never silently dropped,
never merged by machine. `MEMORY.md` is not special-cased by the algorithm: it
collides like any other file, and the *semantic* merge of divergent memories is
a judgement task, not a filesystem operation.

**Nothing is deleted from a source until its content is verified present in the
target.** The whole point of this ADR is that memory is irreplaceable; an
adoption that loses a file to save a directory entry has defeated it.

- **The existing stores merge in, once.** The 33 frozen files from
  `-Users-adebert-h-shipit/memory/` and the 44 stranded ones from the ephemeral
  stores are consolidated into `~/.claude/stores/arthur-debert/shipit/memory/`.
  Those counts are **this machine's**, and they are context for the migration —
  not a spec. The algorithm above is generic; the migration is one application
  of it, plus the human judgement that a machine merge cannot supply (which
  duplicates are genuinely the same, which memories this very epic falsifies).
- **`coordinator.lex:15` is rewritten.** Memory stops being a scratchpad that
  must be hand-swept before session end. Promoting durable learnings into the
  repo remains right for *decisions* (ADRs) and *process* (role docs) — but
  because those belong in the repo, not because memory leaks.

## Considered options

- **Leave it; keep hand-sweeping to the repo** (today's documented workaround).
  Rejected: it has demonstrably failed — 44 stranded files and a store frozen
  for 11 days is the evidence. It also taxes every session's final turn with
  work a symlink does for free.
- **Symlink only `<slug>/memory/`, not the whole project dir.** Fixes memory,
  leaves resume broken — the transcripts stay partitioned by cwd. Rejected: it
  solves half the bug for the same effort, and the two halves are one bug.
- **Wait for a supported memory-path setting.** Rejected: no such setting
  exists, and the loss is ongoing and permanent — memories not written today
  cannot be recovered later.
- **Copy memory into each new Tree at create time.** Rejected: it forks the
  store. Concurrent sessions would diverge and the last one to end would
  silently win. Sharing is the requirement; copying is its opposite.
- **A store per Tree that gc merges upward on reclaim.** Rejected: reclaim is
  not guaranteed to run before a store is useful, ADR-0072 deletes Trees whose
  memory would then need merging from a directory that is already gone, and it
  makes memory availability depend on gc having swept.

## Consequences

- **Resume works across Trees.** This is the ADR that fixes it, and note that
  *naming does not*: `session/resume.py` already resolves targets from durable
  per-repo JSONL logs keyed on shipit session ids, and `ResumeTarget.tree` is a
  recorded field rather than a lookup key. Resume was never broken by the layout;
  it was broken by the transcript being invisible from any other cwd. No naming
  scheme — flat, `<agent>-<id>`, or otherwise — would have fixed it.
- **This rides undocumented harness internals.** `~/.claude/projects/<cwd-slug>/`
  is not a published contract and a Claude Code update could move it. The
  failure mode is benign and visible: the symlink stops being consulted and
  behaviour reverts to today's per-session partition. It is worth the risk
  because the alternative is a store that is already, measurably, dead.
- **`MEMORY.md` becomes a contended file.** Today each session owns its own
  index, so concurrent writes cannot conflict; sharing one store means two
  parallel sessions can both rewrite `MEMORY.md` and the later write wins,
  losing a line. Transcripts are UUID-named and cannot collide. This is a real
  regression in exchange for memory existing at all, and the mitigation — the
  index is a small, append-shaped list of one-line pointers, so a lost update
  costs one recoverable line — is judged acceptable rather than solved.
- **The store outlives every Tree, by design.** ADR-0072 reclaims a Tree after
  48h idle; the store is not in the Tree and is never swept with it. This is the
  point: reclaiming a workspace must not destroy what was learned in it.
- **Per-repo, not per-session, is a deliberate loss of isolation.** All of a
  repo's sessions read each other's memory. That is the goal — the 33 frozen
  files are useful precisely because they are not scoped to one dead session —
  but it does mean a wrong memory written by one session misleads the next. The
  existing memory hygiene rules (verify before recommending; delete what proves
  wrong) carry the weight here.
