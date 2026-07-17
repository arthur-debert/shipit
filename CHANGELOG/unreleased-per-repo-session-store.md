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
  not.
  Both seams are **fail-open**: an unresolvable repo, an unwritable `~/.claude`,
  or no `~/.claude` at all (a CI runner, a container) costs a Tree or an install
  exactly nothing, and logs at DEBUG rather than warning on every single run —
  the store is additive, and without it a session merely keeps its memory to
  itself, which is the behaviour every session had before this existed.
