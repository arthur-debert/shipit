- install: `shipit install` now makes the fundamental skills loadable by the
  agent runtimes (#1088). Skill content is a single real managed dir at
  `.agents/skills/<name>` (where agy/codex read it), and `.claude/skills` is a
  whole-directory symlink to it (`../.agents/skills`) that install ensures as a
  structural step — so Claude Code reads the identical set without a second
  physical copy. Previously install shipped the source-only `.shipit-skills/`
  store, which no runtime loads. The switch from an existing real `.claude/skills`
  dir is path-scoped and pristine-checked: a dir of shipit-pristine content is
  removed and symlinked; any consumer-modified file blocks and is left untouched
  (fail-safe), never via content-hash-global retirement (which would delete
  shipit's own byte-identical source). Whole-file managed writes also fail closed
  on a symlinked destination component in every mode, so an install never writes
  through a discovery-dir symlink onto a target outside the repo. See ADR-0077.
