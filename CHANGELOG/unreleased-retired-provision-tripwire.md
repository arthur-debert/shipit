- install: `shipit install` now refuses a repo whose `pixi.toml` still calls the
  retired `shipit provision lexd` (ADR-0066), naming each offending task, its
  table, and the replacement (lexd rides the managed `[feature.shipit-lexd]`
  block, already on PATH in the lint env). These call sites are consumer-authored
  lane tasks (`lint-full`, `test-full`) that live outside every managed block, so
  no reconcile rewrites them: without the tripwire the pin bump lands green and
  the consumer's CI discovers the dead call later. The check judges only the task
  commands it can read exactly — plain, unquoted, unredirected word sequences,
  which is every one of the fleet's 14 call sites — and declines the rest rather
  than approximating a shell, so it never fails a valid manifest closed.
- cli: `shipit provision` is a tombstone. The verb is retired (ADR-0066) and
  restores nothing; it exists so a surviving call site fails with the remedy
  (lexd rides the Artifact channel; edit the pixi task) instead of click's
  `No such command 'provision'`. Hidden from `--help`, always exits 1.
