- install: `shipit install` now PROJECTS the fundamental skill store into the
  two dirs an agent runtime actually reads — `.claude/skills/<name>` (Claude
  Code) and `.agents/skills/<name>` (agy/codex) — instead of shipping the
  source-only `.shipit-skills/` store that no runtime loads (#1088). Each store
  skill fans out to BOTH discovery dirs as whole-file copies, so a consumer's
  skills are invocable after install, with the full managed-unit contract
  (re-install idempotent, consumer edit → OVERRIDE with a diff, retired skill →
  DELETE). Projection is copies not symlinks by construction (the managed writer
  is bytes-only and reconcile excludes symlinks); `.shipit-skills/` stays
  source-only and grows NO retirement entry, because content-hash-global
  retirement would delete shipit's own source — see ADR-0077. The
  everyone-but-claude split (per-surface targeting, pixi shipping agents-only) is
  deferred to slice 2.
