- install: `shipit install` now refuses a repo whose `pixi.toml` still calls the
  retired `shipit provision lexd` (ADR-0066), naming each offending task, its
  line, and the replacement (lexd rides the managed `[feature.shipit-lexd]`
  block, already on PATH in the lint env). These call sites are consumer-authored
  lane tasks (`lint-full`, `test-full`) that live outside every managed block, so
  no reconcile rewrites them: without the tripwire the pin bump lands green and
  the consumer's CI discovers `No such command 'provision'` later. Detection
  matches the command string inside the parsed tasks — not a task name (one repo
  carries only the inline `provision lexd &&` prefix) and not prose, so the
  ADR-0066 comments the fleet's manifests carry are never flagged.
